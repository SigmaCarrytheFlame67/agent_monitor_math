"""Shared one-shot Codex CLI backend for subscription-authenticated runners."""
from __future__ import annotations

import json
import math
import os
import signal
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping


class CodexBackendError(RuntimeError):
    """Raised when a one-shot Codex CLI call cannot produce a response.

    ``kind`` classifies failures a caller should not simply retry: ``"auth"``
    (the linked account must sign in again), ``"usage_limit"`` (plan usage or
    quota; a bare 429 may be transient and is not one), ``"model"`` (the model
    is unavailable to this account or build), ``"timeout"`` (no answer within
    the per-call limit); ``None`` otherwise.
    """

    def __init__(self, message: str = "", *, kind: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind


def _failure_kind(message: str) -> str | None:
    """Classify a Codex CLI failure message (see ``CodexBackendError.kind``)."""
    rendered = str(message or "").lower()
    if (
        "usage limit" in rendered
        or "quota exceeded" in rendered
        or "exceeded your current quota" in rendered
        or "insufficient_quota" in rendered
        or ("hit your" in rendered and "limit" in rendered)
    ):
        return "usage_limit"
    if (
        "401 unauthorized" in rendered
        or "access token could not be refreshed" in rendered
        or "sign in again" in rendered
        or "not logged in" in rendered
        or "log out and sign in" in rendered
    ):
        return "auth"
    if (
        "model is not supported" in rendered
        or "not supported when using codex" in rendered
        or "not in the vetted bundled catalog" in rendered
        or "model_not_found" in rendered
        or ("does not exist" in rendered and "model" in rendered)
    ):
        return "model"
    return None


def _event_failure_message(events: list[dict]) -> str:
    """The CLI's own reason for a failed turn, without its JSONL framing.

    Codex 0.160 ends a failed turn with ``turn.failed`` (``error.message``)
    after an ``error`` event and writes nothing useful to stderr; the first
    ``error`` item is often only the nonfatal ``use_legacy_landlock``
    deprecation notice and must never be reported as the cause.
    """
    for event in reversed(events):
        if event.get("type") in {"turn.failed", "turn.cancelled"}:
            detail = event.get("error") or event.get("message") or ""
            if isinstance(detail, dict):
                detail = detail.get("message") or json.dumps(detail, ensure_ascii=False)
            if str(detail).strip():
                return " ".join(str(detail).split())
    for event in reversed(events):
        # Item-level errors are nonfatal notices (the landlock deprecation,
        # "Falling back from WebSockets ..."); only top-level errors count.
        if event.get("type") == "error" and str(event.get("message") or "").strip():
            message = " ".join(str(event["message"]).split())
            if not message.startswith("Reconnecting..."):
                return message
    return ""


# Callers that persist a no-tools provenance claim can fail closed on this
# contract instead of assuming that an `enable_tools` keyword is enforced.
# Version 5: the safe model catalog also strips ``tool_mode`` and the
# multi-agent fields, so "responses lite" models no longer receive the
# code-mode and multi-agent tools inside an ``additional_tools`` input item.
# Version-4 claims predate that fix and are no longer trusted.
RESPONSE_ONLY_TOOL_ISOLATION_VERSION = 5
# Exact CLI builds whose response-only turn was re-vetted end to end with the
# loopback provider in tests/test_codex_response_only_contract.py: the request
# must carry no tool anywhere (neither the top-level ``tools`` field nor an
# ``input`` item that carries tools, which is how "responses lite" models
# deliver them) and must run from an empty backend-owned cwd. A newer version
# alone does not prove tool isolation; re-run that harness before adding one.
RESPONSE_ONLY_CODEX_CLI_VERSIONS = frozenset(
    {"codex-cli 0.153.4", "codex-cli 0.157.1", "codex-cli 0.160.0"}
)
# The vetted build operators are told to install when none is present.
RESPONSE_ONLY_CODEX_CLI_VERSION = "codex-cli 0.160.0"
RESPONSE_ONLY_CODEX_INSTALL_HINT = (
    "npm i -g --prefix ~/.npm-global @openai/codex@"
    + RESPONSE_ONLY_CODEX_CLI_VERSION.rsplit(" ", 1)[-1]
)

_MAX_BUNDLED_MODEL_CATALOG_BYTES = 2 * 1024 * 1024
_MODEL_CATALOG_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class CodexResult:
    text: str
    usage: dict[str, int]


def _read_only_sandbox_args(
    source_env: Mapping[str, str] | None = None,
) -> list[str]:
    """Select Landlock when Linux cannot create bubblewrap's loopback."""
    source = source_env if source_env is not None else os.environ
    configured = source.get("AGENT_MONITOR_CODEX_LEGACY_LANDLOCK")
    enabled = sys.platform.startswith("linux") if configured is None else (
        configured.strip().lower() in {"1", "true", "yes", "on"}
    )
    args: list[str] = []
    if enabled:
        args.extend(["--enable", "use_legacy_landlock"])
    args.extend(["--sandbox", "read-only"])
    return args


def _legacy_landlock_notice(event: dict) -> bool:
    """Recognize the CLI's nonfatal deprecation notice, not real errors."""
    item = event.get("item") or {}
    message = str(item.get("message") or "")
    return (
        event.get("type") == "item.completed"
        and item.get("type") == "error"
        and "use_legacy_landlock" in message
        and "deprecated" in message.lower()
    )


def forward_nonterminal(event: dict, emit: Callable[[dict], None]) -> None:
    """Forward one event of a Codex call whose failure the caller tolerates.

    The run's stream parser ends the whole run on a raw ``turn.failed`` or
    ``turn.cancelled``.  A tolerated call (an audit pass that only degrades
    the result) shows its failure as a non-terminal error item instead; the
    caller reports a fatal failure itself.
    """
    if event.get("type") in {"turn.failed", "turn.cancelled"}:
        detail = event.get("error") or event.get("message") or event.get("type")
        if isinstance(detail, dict):
            detail = detail.get("message") or json.dumps(detail, ensure_ascii=False)
        event = {
            "type": "item.completed",
            "item": {
                "type": "error",
                "message": "provider call failed: "
                + " ".join(str(detail).split())[-1200:],
            },
        }
    emit(event)


def subscription_enabled(source_env: Mapping[str, str] | None = None) -> bool:
    source = source_env if source_env is not None else os.environ
    selected = source.get("AGENT_MONITOR_SELECTED_MODEL", "").strip().lower()
    return (
        source.get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1"
        and not selected.startswith("kimi-")
    )


def _kimi_model(model: str | None) -> bool:
    return str(model or "").strip().lower().startswith("kimi-")


def _response_only_args(*, enable_tools: bool) -> list[str]:
    """Disable ambient Codex capabilities for one deterministic model turn.

    `--sandbox read-only` limits writes but is not a no-tools contract: without
    these flags Codex can still inherit shell, web, app, plugin, and workspace
    capabilities from its normal runtime.  All shared one-shot callers are
    response-only unless they explicitly request the shell/unified executor.
    """
    args = [
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--disable", "apps",
        "--disable", "artifact",
        "--disable", "auth_elicitation",
        "--disable", "browser_use",
        "--disable", "browser_use_external",
        "--disable", "browser_use_full_cdp_access",
        "--disable", "code_mode",
        "--disable", "code_mode_host",
        "--disable", "computer_use",
        "--disable", "enable_mcp_apps",
        "--disable", "goals",
        "--disable", "hooks",
        "--disable", "image_generation",
        "--disable", "in_app_browser",
        "--disable", "memories",
        "--disable", "multi_agent",
        "--disable", "network_proxy",
        "--disable", "plugins",
        "--disable", "plugin_sharing",
        "--disable", "remote_plugin",
        "--disable", "request_permissions_tool",
        "--disable", "skill_mcp_dependency_install",
        "--disable", "skill_search",
        "--disable", "sleep_tool",
        "--disable", "standalone_web_search",
        "--disable", "tool_call_mcp_elicitation",
        "--disable", "tool_suggest",
        "--disable", "view_image",
        "--disable", "workspace_dependencies",
        "-c", 'web_search="disabled"',
        "-c", "tools.web_search=false",
    ]
    if not enable_tools:
        args.extend(
            [
                "--disable",
                "shell_tool",
                "--disable",
                "unified_exec",
                "-c",
                "tools.update_plan.enabled=false",
                "-c",
                "tools.experimental_request_user_input.enabled=false",
                # Bundled skills are instructions to open SKILL.md files, which
                # a tool-free turn cannot do; keep them out of the prompt and
                # out of the account's CODEX_HOME.
                "-c",
                "skills.include_instructions=false",
                "-c",
                "skills.bundled.enabled=false",
            ]
        )
    return args


def _metadata_command(
    command: list[str],
    *,
    env: dict[str, str],
    cwd: str,
) -> subprocess.CompletedProcess[str]:
    """Run one local-only Codex metadata command with a bounded lifetime."""
    try:
        return _run_codex_process(
            command,
            input="",
            timeout=_MODEL_CATALOG_TIMEOUT_SECONDS,
            env=env,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired as exc:
        raise CodexBackendError("Codex response-only metadata check timed out") from exc
    except OSError as exc:
        raise CodexBackendError(
            f"could not inspect the Codex response-only model catalog: {exc}"
        ) from exc


def _write_response_only_model_catalog(
    codex: str,
    *,
    model: str | None,
    runtime_path: str,
    temp_dir: str,
) -> Path:
    """Create a vetted catalog whose model entries cannot advertise patch tools.

    Feature switches alone do not disable every orchestration tool: nested
    switches control planning and user input, while the model catalog selects
    patch, experimental, code-mode and multi-agent tools. Code-mode catalog
    entries (``tool_mode``) add an ``exec`` orchestration tool, and
    ``multi_agent_version`` adds the spawn_agent/send_message collaboration
    tools, even when every related feature switch is disabled. Response-only
    callers use the exact bundled entry with these capabilities removed. The
    CLI builds in ``RESPONSE_ONLY_CODEX_CLI_VERSIONS`` are pinned exactly and
    checked against a loopback provider because future clients may
    reinterpret these config fields or add default tools.
    """
    metadata_env = {
        "PATH": runtime_path,
        "HOME": temp_dir,
        "CODEX_HOME": temp_dir,
        "NO_COLOR": "1",
    }
    version = _metadata_command(
        [codex, "--version"], env=metadata_env, cwd=temp_dir
    )
    observed = version.stdout.strip()
    if version.returncode or observed not in RESPONSE_ONLY_CODEX_CLI_VERSIONS:
        vetted = " or ".join(
            repr(item) for item in sorted(RESPONSE_ONLY_CODEX_CLI_VERSIONS)
        )
        raise CodexBackendError(
            f"Codex response-only mode requires vetted CLI {vetted}; found "
            f"{observed or 'unavailable'!r} at {codex}. Install a vetted build "
            f"(for example `{RESPONSE_ONLY_CODEX_INSTALL_HINT}`) and put it "
            "first on the service PATH; unvetted builds are refused because "
            "they may add tools to a response-only turn"
        )

    bundled = _metadata_command(
        [codex, "debug", "models", "--bundled"],
        env=metadata_env,
        cwd=temp_dir,
    )
    raw = bundled.stdout.encode("utf-8")
    if bundled.returncode:
        raise CodexBackendError("Codex could not provide its bundled model catalog")
    if not raw or len(raw) > _MAX_BUNDLED_MODEL_CATALOG_BYTES:
        raise CodexBackendError("Codex bundled model catalog is empty or oversized")
    try:
        payload = json.loads(bundled.stdout)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise CodexBackendError("Codex bundled model catalog is invalid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise CodexBackendError("Codex bundled model catalog has an invalid schema")
    source_models = payload["models"]
    if not 1 <= len(source_models) <= 64 or not all(
        isinstance(item, dict) for item in source_models
    ):
        raise CodexBackendError("Codex bundled model catalog has invalid entries")

    selected = str(model or "").strip()
    if selected:
        source_models = [
            item for item in source_models if str(item.get("slug") or "") == selected
        ]
        if not source_models:
            raise CodexBackendError(
                f"Codex response-only model {selected!r} is not in the vetted bundled catalog"
            )
    safe_models: list[dict] = []
    for source_model in source_models:
        slug = str(source_model.get("slug") or "").strip()
        if not slug or len(slug) > 128:
            raise CodexBackendError("Codex bundled model catalog contains an invalid slug")
        safe_model = dict(source_model)
        safe_model.pop("apply_patch_tool_type", None)
        safe_model["experimental_supported_tools"] = []
        # Without removing ``tool_mode`` and ``multi_agent_version`` both
        # vetted builds still send ``exec``/``wait`` and the six collaboration
        # tools in an ``additional_tools`` input item (loopback-verified).
        safe_model.pop("tool_mode", None)
        safe_model.pop("multi_agent_version", None)
        safe_model.pop("multi_agent_reasoning_effort", None)
        safe_model["node_repl_disabled"] = True
        safe_model["include_apps_usage_instructions"] = False
        safe_model["include_plugin_usage_instructions"] = False
        safe_model["include_skills_usage_instructions"] = False
        safe_model["supports_search_tool"] = False
        safe_model["supports_parallel_tool_calls"] = False
        safe_model.pop("web_search_tool_type", None)
        safe_models.append(safe_model)

    rendered = json.dumps(
        {"models": safe_models}, ensure_ascii=True, separators=(",", ":")
    ).encode("utf-8")
    if not rendered or len(rendered) > _MAX_BUNDLED_MODEL_CATALOG_BYTES:
        raise CodexBackendError("Codex response-only model catalog is oversized")
    catalog_path = Path(temp_dir) / "response-only-models.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(catalog_path, flags, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise CodexBackendError("could not create the response-only model catalog") from exc
    return catalog_path


def _kimi_codex_args(base_url: str, *, enable_tools: bool) -> list[str]:
    """Codex config for one isolated Kimi Responses invocation."""
    args = [
        *_response_only_args(enable_tools=enable_tools),
        "-c", 'model_provider="kimi"',
        "-c", 'model_providers.kimi.name="Kimi K3"',
        "-c", f'model_providers.kimi.base_url="{base_url}"',
        "-c", 'model_providers.kimi.env_key="KIMI_API_KEY"',
        "-c", 'model_providers.kimi.wire_api="responses"',
        "-c", "model_providers.kimi.requires_openai_auth=false",
        # The loopback proxy replaces this with the provider's selected
        # low/high/max spelling. Codex itself validates only its own enum.
        "-c", 'model_reasoning_effort="high"',
    ]
    return args


def _stop_process_group(process: subprocess.Popen[str]) -> None:
    """Stop a timed-out Codex process and any tool subprocesses it started."""
    from agent_monitor.process_control import terminate_descendants

    terminate_descendants(process.pid)
    if os.name != "posix" and process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            try:
                process.terminate()
            except OSError:
                pass
    else:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
    else:
        try:
            process.kill()
        except OSError:
            pass
    process.communicate(timeout=3)


def _run_codex_process(
    command: list[str],
    *,
    input: str,
    timeout: float,
    env: dict[str, str],
    cwd: str | None,
) -> subprocess.CompletedProcess[str]:
    """Run Codex in its own process group so timeout cleanup is complete."""
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=cwd,
        start_new_session=os.name == "posix",
    )
    try:
        stdout, stderr = process.communicate(input=input, timeout=timeout)
    except BaseException:
        _stop_process_group(process)
        raise
    else:
        _stop_process_group(process)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def codex_exec(
    system: str,
    user: str,
    *,
    model: str | None = None,
    timeout: float | None = None,
    emit_event: Callable[[dict], None] | None = None,
    workspace: str | Path | None = None,
    sandbox: str = "read-only",
    enable_tools: bool = False,
    source_env: Mapping[str, str] | None = None,
    allow_empty_text: bool = False,
) -> CodexResult:
    """Run one isolated ``codex exec`` turn using an explicit source env.

    ``source_env`` is intentionally supported for in-process derived tools:
    they must bind the subprocess to the run owner's ``CODEX_HOME`` rather
    than whichever login or API credentials happen to exist in the server's
    global environment.  The child receives an allowlisted environment only.

    A completed turn without a final message is an error unless
    ``allow_empty_text`` is set: a tool-enabled caller whose deliverable is a
    workspace file checks that file instead.
    """
    source = source_env if source_env is not None else os.environ
    use_kimi = _kimi_model(model)
    if not use_kimi and not source.get("CODEX_HOME", "").strip():
        raise CodexBackendError("CODEX_HOME is required for Codex subscription calls")
    if use_kimi and not source.get("KIMI_API_KEY", "").strip():
        raise CodexBackendError("KIMI_API_KEY is required for Kimi Codex calls")
    if timeout is None:
        try:
            timeout = float(source.get("AGENT_MONITOR_CODEX_TIMEOUT", "600"))
        except ValueError as exc:
            raise CodexBackendError("AGENT_MONITOR_CODEX_TIMEOUT must be a number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise CodexBackendError("Codex timeout must be greater than zero")

    prompt = f"SYSTEM INSTRUCTIONS:\n{system}\n\nUSER REQUEST:\n{user}"
    # Derived Lean/DAG calls may receive an intentionally tiny account-bound
    # environment containing only subscription markers and CODEX_HOME.  The
    # Codex launcher itself can still be found through this process's trusted
    # service PATH, but its `#!/usr/bin/env node` shebang also needs that PATH
    # inside the child.  Preserve an explicit source PATH first and append the
    # service PATH as runtime support; this copies no provider credentials.
    from agent_monitor.engines_registry import tool_search_path

    runtime_path_parts: list[str] = []
    for raw_path in (source.get("PATH"), tool_search_path(), os.defpath):
        for part in str(raw_path or "").split(os.pathsep):
            if part and part not in runtime_path_parts:
                runtime_path_parts.append(part)
    runtime_path = os.pathsep.join(runtime_path_parts)
    codex = shutil.which("codex", path=runtime_path)
    if not codex:
        raise CodexBackendError("codex executable was not found")
    try:
        resolved_codex = Path(codex).resolve(strict=True)
    except OSError as exc:
        raise CodexBackendError("codex executable could not be resolved") from exc
    if not resolved_codex.is_file():
        raise CodexBackendError("codex executable is not a regular file")
    codex = str(resolved_codex)
    allowed_env = (
        "PATH",
        "HOME",
        "CODEX_HOME",
        "LANG",
        "LC_ALL",
        "TERM",
        "NO_COLOR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "AGENT_MONITOR_PYTHON",
    )
    env = {key: source[key] for key in allowed_env if source.get(key)}
    env["PATH"] = runtime_path
    if use_kimi:
        for key in ("KIMI_API_KEY", "KIMI_API_BASE", "KIMI_REASONING_EFFORT"):
            if source.get(key):
                env[key] = source[key]
    env["NO_COLOR"] = "1"
    with tempfile.TemporaryDirectory(prefix="agent-monitor-codex-") as temp_dir:
        env["CODEX_HOME"] = temp_dir if use_kimi else source["CODEX_HOME"]
        last_message = Path(temp_dir) / "last-message.txt"
        proxy = None
        if use_kimi:
            from agent_monitor import kimi_k3
            from agent_monitor.kimi_responses_proxy import KimiResponsesProxy

            proxy = KimiResponsesProxy(
                upstream_base=source.get("KIMI_API_BASE") or kimi_k3.DEFAULT_BASE_URL,
                reasoning_effort=source.get("KIMI_REASONING_EFFORT"),
            )
            proxy.__enter__()
        try:
            response_only_catalog = None
            if not enable_tools:
                response_only_catalog = _write_response_only_model_catalog(
                    codex,
                    model=model,
                    runtime_path=runtime_path,
                    temp_dir=temp_dir,
                )
            sandbox_args = (
                _read_only_sandbox_args(source)
                if sandbox == "read-only"
                else ["--sandbox", sandbox]
            )
            command = [
                codex,
                "exec",
                "--strict-config",
                "--json",
                "--output-last-message",
                str(last_message),
                *sandbox_args,
                "--skip-git-repo-check",
            ]
            if enable_tools and workspace is not None:
                command.extend(["--cd", str(Path(workspace).resolve())])
            if proxy is not None:
                command.extend(_kimi_codex_args(proxy.base_url, enable_tools=enable_tools))
            else:
                from agent_monitor.run_tuning import codex_config_args

                command.extend(_response_only_args(enable_tools=enable_tools))
                command.extend(codex_config_args(source, model))
            if response_only_catalog is not None:
                command.extend(
                    [
                        "-c",
                        "model_catalog_json="
                        + json.dumps(str(response_only_catalog)),
                    ]
                )
            if model:
                command.extend(["--model", model])
            command.append("-")

            try:
                completed = _run_codex_process(
                    command,
                    input=prompt,
                    timeout=timeout,
                    env=env,
                    # A no-tools turn must not inherit its caller's run
                    # workspace as ambient context.  The backend-owned empty
                    # directory contains only its output file and disappears
                    # after this invocation.
                    cwd=(
                        str(Path(workspace).resolve())
                        if enable_tools and workspace is not None
                        else temp_dir
                    ),
                )
            except subprocess.TimeoutExpired as exc:
                raise CodexBackendError(
                    f"codex exec timed out after {timeout:g}s", kind="timeout"
                ) from exc
            except OSError as exc:
                raise CodexBackendError(f"could not start codex exec: {exc}") from exc
        finally:
            if proxy is not None:
                proxy.__exit__(None, None, None)

        events: list[dict] = []
        # Codex semantics: cached input is part of input_tokens.  Keep the
        # cached and reasoning parts so callers can show and price them.
        usage = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
        }
        terminal_event: dict | None = None
        for line in completed.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            events.append(event)
            if emit_event is not None and not _legacy_landlock_notice(event):
                emit_event(event)
            if event.get("type") == "turn.completed":
                event_usage = event.get("usage") or {}
                for key in usage:
                    try:
                        usage[key] += int(event_usage.get(key) or 0)
                    except (TypeError, ValueError):
                        pass
            if event.get("type") in {"turn.completed", "turn.failed", "turn.cancelled"}:
                terminal_event = event

        failure = _event_failure_message(events)
        if completed.returncode:
            if failure:
                # The CLI's own reason (usage limit, expired login, model not
                # available ...) instead of the raw JSONL tail.
                raise CodexBackendError(
                    f"codex exec failed: {failure[-1000:]}", kind=_failure_kind(failure)
                )
            detail = (completed.stderr or completed.stdout or "").strip()
            suffix = f": {detail[-1000:]}" if detail else ""
            raise CodexBackendError(
                f"codex exec exited with status {completed.returncode}{suffix}",
                kind=_failure_kind(detail),
            )
        if terminal_event is None:
            raise CodexBackendError("codex exec ended without a terminal turn event")
        if terminal_event.get("type") != "turn.completed":
            detail = failure or ""
            suffix = f": {str(detail)[-500:]}" if detail else ""
            raise CodexBackendError(
                f"codex exec terminal event was {terminal_event.get('type')}{suffix}",
                kind=_failure_kind(detail),
            )

        try:
            text = last_message.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise CodexBackendError("codex exec did not write its last message") from exc
        if not text:
            for event in reversed(events):
                item = event.get("item") or {}
                if item.get("type") == "agent_message" and str(item.get("text") or "").strip():
                    text = str(item["text"]).strip()
                    break
        if not text and not allow_empty_text:
            raise CodexBackendError("codex exec returned an empty response")
        # The cached and reasoning parts appear only when the CLI reported them.
        return CodexResult(text=text, usage={key: value for key, value in usage.items()
                                             if value or key in {"input_tokens", "output_tokens"}})


def codex_subscription_exec(
    system: str,
    user: str,
    *,
    source_env: Mapping[str, str],
    model: str | None = None,
    timeout: float | None = None,
    emit_event: Callable[[dict], None] | None = None,
    workspace: str | Path | None = None,
) -> CodexResult:
    """Run a response-only turn that can use only linked Codex OAuth.

    This fail-closed entry point is for Lean/DAG sidecars.  It requires the
    immutable subscription marker produced by ``_configure_auth_route``,
    rejects API-backed Kimi models, and delegates with the exact source
    environment so ``codex_exec`` cannot consult process-global credentials.
    """
    if source_env.get("AGENT_MONITOR_CODEX_SUBSCRIPTION") != "1":
        raise CodexBackendError(
            "AGENT_MONITOR_CODEX_SUBSCRIPTION=1 is required for a Codex subscription response"
        )
    if _kimi_model(model):
        raise CodexBackendError(
            "Kimi is an API model and cannot be used by a Codex subscription response"
        )
    raw_home = str(source_env.get("CODEX_HOME") or "").strip()
    if not raw_home:
        raise CodexBackendError(
            "CODEX_HOME is required for a Codex subscription response"
        )
    from agent_monitor import codex_login

    codex_home = Path(raw_home).expanduser()
    if not codex_login.account_login_ready(codex_home):
        raise CodexBackendError(
            "CODEX_HOME does not contain a connected Codex subscription login"
        )
    return codex_exec(
        system,
        user,
        model=model,
        timeout=timeout,
        emit_event=emit_event,
        workspace=workspace,
        sandbox="read-only",
        enable_tools=False,
        source_env=source_env,
    )
