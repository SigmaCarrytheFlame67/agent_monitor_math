#!/usr/bin/env python3
"""Tool-free Kimi K3 engine: the Ansatz CGM pipeline on a fixed Kimi route.

The ``kimi`` engine is the guest-safe entry point to the Math Agent Harness
CGM pipeline (``math_harness_runner.run_pipeline``): strategist, independent
workers, one cold model verifier per lemma, bounded repair and a curator, all
as plain chat-completion calls.  The route is pinned to
``Route("api", "kimi-k3", "api_chat")``; account subscriptions, command
overrides and user-selected endpoints never change it.

No-tools guarantees (unchanged from the former draft/critique/refine runner):

* no model-visible tools, function calls, shell commands or local agents; the
  trusted wrapper alone parses replies and writes artifacts in the workspace;
* the endpoint is never derived from the prompt or from a user-settable base
  URL.  Sponsored runs must use the attested loopback broker
  (``http://127.0.0.1:<port>/internal/v1``); own-key runs use the operator's
  ``AGENT_MONITOR_KIMI_BASE_URL`` (default ``kimi_k3.DEFAULT_BASE_URL``);
* ambient proxies are ignored and provider error text is key-redacted.

Model-verifier acceptance is never a Lean certificate.

Usage: ``kimi_runner.py --prompt-file PATH`` (or ``kimi_runner.py "<prompt>"``)
from a run workspace that contains the canonical ``problem.txt``.  Emits
Codex-shaped JSONL events on stdout.  Exit status: 0 when the pipeline
published its artifacts (accepted or not; a stop request during the
written-proof round after publication ends only that round), 2 for a missing
prompt, key or attested endpoint, 143 when a stop request (SIGTERM) ended the
run before publication (nothing is published), and 1 for any other failure.
"""
from __future__ import annotations

import os
import re
import signal
import stat
import sys
from pathlib import Path
from urllib.parse import urlsplit

if __package__:
    from .plain_runner import emit_item
    from .task_prompt import read_task_prompt
else:
    repository_root = Path(__file__).resolve().parents[2]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))
    from agent_monitor.runners.plain_runner import emit_item
    from agent_monitor.runners.task_prompt import read_task_prompt


MODEL_ID = "kimi-k3"
OPERATOR_BASE_VAR = "AGENT_MONITOR_KIMI_BASE_URL"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
# Account routes, alternate providers and ambient proxies must never select a
# different transport for this engine.
_DORMANT_ENV_NAMES = (
    "AGENT_MONITOR_CODEX_SUBSCRIPTION",
    "AGENT_MONITOR_CLAUDE_SUBSCRIPTION",
    "KIMI_BASE_URL",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def sponsored_base(raw: str) -> str | None:
    """Return the attested sponsored loopback base, or ``None``."""
    value = str(raw or "").strip().rstrip("/")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _LOOPBACK_HOSTS
        or not port
        or parsed.path != "/internal/v1"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return None
    return value


def operator_base(raw: str | None) -> str:
    """The operator-only endpoint for own-key runs (HTTPS, no credentials)."""
    from agent_monitor import kimi_k3

    value = str(raw or "").strip().rstrip("/")
    if not value:
        return kimi_k3.DEFAULT_BASE_URL
    parsed = urlsplit(value)
    try:
        parsed.port
    except ValueError:
        return kimi_k3.DEFAULT_BASE_URL
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return kimi_k3.DEFAULT_BASE_URL
    return value


class ProblemFileError(ValueError):
    """The canonical problem statement cannot be used; the text is user-safe."""


def _read_problem(root: Path, limit: int) -> str | None:
    """Read the canonical ``problem.txt`` without following links."""
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(root / "problem.txt", flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ProblemFileError("problem.txt cannot be opened") from exc
    try:
        state = os.fstat(fd)
        if not stat.S_ISREG(state.st_mode):
            raise ProblemFileError("problem.txt is not a regular file")
        if state.st_size > limit:
            raise ProblemFileError(
                f"the problem statement is {state.st_size:,} bytes; the Ansatz "
                f"CGM pipeline accepts at most {limit:,} bytes of UTF-8 text"
            )
        payload = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(payload) > limit:
        raise ProblemFileError(
            f"the problem statement exceeds {limit:,} bytes of UTF-8 text"
        )
    try:
        return payload.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ProblemFileError("problem.txt is not UTF-8 text") from exc


def _redact(text: str, *secrets: str) -> str:
    value = " ".join(str(text or "").split())
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[redacted]")
    return value[-600:]


def _empty_workers_message(exc: BaseException) -> str:
    """A plain sentence when no worker answered because its replies came back
    empty (the reasoning used the whole output budget), the most common
    sponsored failure; "" otherwise."""
    text = str(exc)
    if "no worker produced a usable candidate" not in text or "returned an empty response" not in text:
        return ""
    retry = "Please try again; a shorter or more focused statement often helps."
    summary = re.search(r"\((finish_reason=[^()]*)\)", text)
    if summary and "finish_reason=length" in summary.group(1):
        return f"Kimi used its whole answer budget on reasoning and returned no proof text ({summary.group(1)}). {retry}"
    return f"Kimi returned no proof text. {retry}"


def _failure_message(exc: BaseException) -> str:
    """What happened and what to do, when a sponsored Kimi provider failure
    ended the run (``kimi_k3.failure_message``); "" otherwise.

    The message speaks for the whole run, so it says that no tokens were used
    only when no call of the run can have been processed (``exc.billed``).
    Built from the failure's typed status fields only, so it never carries a
    prompt, a reply, a key or the endpoint.
    """
    from agent_monitor import kimi_k3

    failure = getattr(exc, "failure", None)
    if not isinstance(failure, kimi_k3.CallFailure):
        return ""
    return kimi_k3.failure_message(failure, billed=getattr(exc, "billed", True) is not False)


def main() -> int:
    """Run one CGM proof on the pinned Kimi route (see the module docstring).

    ``run_pipeline`` is called directly from this thread, never from a helper
    thread: it intercepts SIGTERM (the console's Stop) only when it runs on the
    process main thread, which is where ``__main__`` calls this function.
    """
    try:
        prompt = read_task_prompt(sys.argv)
    except (OSError, UnicodeDecodeError) as exc:
        emit_item(
            {
                "type": "error",
                "message": f"Kimi could not read its task prompt ({type(exc).__name__})",
                "terminal": True,
            }
        )
        return 2
    if not prompt.strip():
        emit_item({"type": "error", "message": "Enter a problem to prove"})
        return 2
    key = str(os.environ.get("KIMI_API_KEY") or "").strip()
    if not key:
        emit_item({"type": "error", "message": "Add a Kimi API key in Settings"})
        return 2

    if os.environ.get("AGENT_MONITOR_SPONSORED_KIMI") == "1":
        base = sponsored_base(os.environ.get("KIMI_API_BASE", ""))
        if base is None:
            emit_item(
                {"type": "error", "message": "Sponsored Kimi endpoint attestation failed"}
            )
            return 2
    else:
        # KIMI_API_BASE is user-settable; only the operator variable counts.
        base = operator_base(os.environ.get(OPERATOR_BASE_VAR))

    for name in _DORMANT_ENV_NAMES:
        os.environ.pop(name, None)
    os.environ["KIMI_API_BASE"] = base
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "*"

    from agent_monitor.runners import math_harness_runner as harness

    try:
        root = Path.cwd().resolve(strict=True)
        problem = _read_problem(root, harness.MAX_PROBLEM_BYTES)
        if problem is None:
            problem = prompt.strip()
        route = harness.Route("api", MODEL_ID, "api_chat")
        budgets = harness.Budgets.from_env(os.environ)
        # Called on this (the main) thread so run_pipeline's SIGTERM handler
        # applies: a stop abandons in-flight calls and publishes nothing.
        result = harness.run_pipeline(
            root, problem, route, budgets=budgets, task=prompt
        )
    except ProblemFileError as exc:
        emit_item(
            {
                "type": "error",
                "message": f"Kimi could not start this proof: {exc}",
                "terminal": True,
            }
        )
        return 1
    except harness.RunCancelled:
        # RunCancelled is a MathHarnessError, so it must be caught first.
        emit_item(
            {
                "type": "error",
                "message": "Kimi run stopped before publication; nothing was published",
                "terminal": True,
            }
        )
        return 128 + int(signal.SIGTERM)
    except harness.MathHarnessError as exc:
        emit_item(
            {
                "type": "error",
                "message": "Kimi could not finish this proof: "
                + (_failure_message(exc) or _empty_workers_message(exc) or _redact(str(exc), key, base)),
                "terminal": True,
            }
        )
        return 1
    except (OSError, UnicodeError, ValueError) as exc:
        emit_item(
            {
                "type": "error",
                "message": f"Kimi could not finish this proof ({type(exc).__name__})",
                "terminal": True,
            }
        )
        return 1

    emit_item(
        {
            "type": "file_change",
            "changes": [
                {"path": path}
                for path in [*(result.get("written") or []), *harness.writeup_paths(result)]
            ],
        }
    )
    facts = len(result.get("fact_ids") or [])
    status = (
        f"accepted by the independent model verifier ({facts} fact"
        f"{'s' if facts != 1 else ''}; not a Lean certificate)"
        if result.get("accepted")
        else "not verifier-accepted (unattested candidate)"
    )
    written_proof = harness.writeup_status_line(result)
    if written_proof:
        status += f"\n{written_proof}"
    observed = result.get("observed_model")
    emit_item(
        {
            "type": "agent_message",
            "text": (
                f"Ansatz CGM · Kimi K3 completed · {status}\n\n"
                f"{str(result.get('proof_text') or '')[:6000]}"
            ),
            "model": observed,
            "model_source": "response" if observed else None,
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
