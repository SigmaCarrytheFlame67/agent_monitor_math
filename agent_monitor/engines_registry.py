"""Engine registry: built-in engines plus external CLI harnesses.

External harnesses (Codex CLI, OpenClaude, OpenHands) run as
subprocesses inside the per-run workspace. Command templates can be overridden
via environment variables so users can adapt to their local install.

Template placeholders: {prompt} {prompt_file} {workspace} {problem_file} {model_args}
"""
from __future__ import annotations

import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

BUILTIN_ENGINES: list[dict[str, Any]] = [
    {
        "id": "hermes",
        "label": "Hermes",
        "vendor": "Nous Research",
        "description": "Vendored agent harness — tools, code, subagents",
        "kind": "builtin",
        "url": "https://github.com/NousResearch/hermes-agent",
    },
    {
        "id": "improof",
        "label": "IMProof",
        "vendor": "FirstProof",
        "description": "Author–Critic ProofStack workflow (batch-2)",
        "kind": "builtin",
        "url": "https://github.com/1stproof/batch-2",
    },
]

# Retired engine IDs remain valid provenance on historical run records, but are
# deliberately absent from the new-run catalog.  In particular, ``ucla`` must
# never be treated as an alias for the replacement Math Agent Harness: doing so
# would dispatch an old continuation through a different implementation.
LEGACY_ENGINE_IDS = frozenset({"ucla", "danus"})


def retired_engine_error(engine: str) -> str | None:
    """Why a run of a retired engine cannot be continued, else ``None``.

    UCLA keeps its explicit legacy continuation path; Danus has no runner.
    """
    if engine == "danus":
        return (
            "Danus has been retired. This run stays readable, but it cannot be "
            "continued; start a new run instead."
        )
    return None

# ── External CLI harnesses ────────────────────────────────────────────────
# Each defines: availability check + default command template.
# Tool-enabled agents write proof.md inside the workspace (cwd); text-only
# runners receive a response contract and save the returned write-up themselves.

_PROOF_INSTRUCTION = (
    "You are solving an informal mathematics proof problem. "
    "Work inside the current directory. Maintain your evolving proof in "
    "./proof.md as a Markdown document (use $...$ / $$...$$ for math, headings "
    "for structure); update it as the proof develops. "
    "The problem statement is in ./problem.txt.\n\nPROBLEM:\n{problem}"
)

_RESPONSE_PROOF_INSTRUCTION = (
    "You are solving an informal mathematics proof problem. "
    "Return the mathematical write-up directly as Markdown, beginning with "
    "a level-one heading. Use $...$ / $$...$$ for math and headings for structure. "
    "Do not wrap the document in a code fence or add delivery instructions. "
    "The application automatically saves your response as the proof artifact. "
    "No filesystem action is requested; omit file-access commentary and do not "
    "ask the reader to save or copy the response. Preserve all mathematical "
    "qualifications, unresolved gaps, and uncertainty. "
    "The complete problem statement follows.\n\nPROBLEM:\n{problem}"
)

# Shared by every engine so a run's references are auditable and the console can
# extract them. Kept strict about not inventing sources — a fabricated citation
# is worse than an inline proof of the same fact.
CITATION_REQUIREMENTS = (
    "CITATIONS (required):\n"
    "- Justify each non-obvious step by a definition, a numbered lemma of your "
    "own, or a citation marker like [1].\n"
    "- End proof.md with a `## References` section listing every marker you "
    "used, one per line, formatted as: `[1] <identifier> — <what it gives you>`. "
    "Use resolvable identifiers where they exist (arXiv:2401.01234, "
    "doi:10.1007/..., book + theorem number, URL); for classical results the "
    "name is enough (e.g. `Euclid's lemma`).\n"
    "- Tag steps you proved yourself as `self-derived` rather than citing "
    "literature for them.\n"
    "- Never invent a reference. If you cannot locate a real source, write "
    "`[uncited: standard result]` and prove the fact inline instead.\n"
    "- Every listed reference must be cited somewhere in the text, and every "
    "marker in the text must appear in the list.\n"
)


def proof_prompt(
    problem_text: str,
    workspace: Path | None = None,
    preamble: str = "",
    *,
    response_only: bool = False,
) -> str:
    """Task prompt for CLI engines.

    ``preamble`` carries the operator's persona (identity / skills / memory) for
    engines that cannot read the agent home themselves.
    ``response_only`` supplies the problem inline and lets the runner persist
    the returned Markdown instead of asking the model to operate on files.
    """
    instruction = _RESPONSE_PROOF_INSTRUCTION if response_only else _PROOF_INSTRUCTION
    text = instruction.format(problem=problem_text)
    if workspace is not None and not response_only:
        # Embedded agents (e.g. openclaw) may run tools from their own home
        # workspace — pin the absolute path so proof.md lands in the run dir.
        text = (
            f"Your working directory for this task is: {workspace} "
            f"(absolute path — write proof.md THERE).\n" + text
        )
    citations = (
        CITATION_REQUIREMENTS.replace("End proof.md", "End the write-up")
        if response_only else CITATION_REQUIREMENTS
    )
    text = f"{text}\n\n{citations}"
    if preamble.strip():
        text = f"{preamble.strip()}\n\n{text}"
    return text


CLI_ENGINES: dict[str, dict[str, Any]] = {
    # Keep the replacement in the former UCLA card position: built-ins render
    # first, then CLI engines in insertion order.  The runner is the bounded,
    # response-only Ansatz CGM pipeline (strategist, workers, independent model
    # verifier, curator) on any account route; it never exposes tools.
    "math_harness": {
        "id": "math_harness",
        "label": "Ansatz CGM harness",
        "vendor": "Ansatz · CGM",
        "description": (
            "Response-only CGM pipeline on any account route · verified fact "
            "DAG and exploring graph (model-verified, not a Lean certificate)"
        ),
        "kind": "cli",
        "url": None,
        "check_file": "agent_monitor/runners/math_harness_runner.py",
        "cmd_env": "MATH_HARNESS_CMD",
        "default_cmd": (
            "{python} -u "
            "{root}/agent_monitor/runners/math_harness_runner.py"
        ),
        "install_hint": "Ansatz CGM harness runner is missing",
        "ready_detail": "bounded response-only CGM pipeline ready",
        "parser_style": "codex",
    },
    # The same CGM pipeline pinned to a tool-free Kimi K3 route. This is the
    # default engine for guests and new accounts, which cannot use the
    # coding-agent CLIs; the id stays ``kimi`` for stored runs and settings.
    "kimi": {
        "id": "kimi",
        "label": "Ansatz CGM · Kimi K3",
        "vendor": "Ansatz · Kimi K3",
        "description": (
            "Tool-free, guest-safe CGM pipeline on Kimi K3 · verified fact DAG "
            "(model-verified, not a Lean certificate)"
        ),
        "kind": "cli",
        "url": None,
        "check_file": "agent_monitor/runners/kimi_runner.py",
        "cmd_env": "KIMI_PROOF_CMD",
        "default_cmd": (
            "{python} -u "
            "{root}/agent_monitor/runners/kimi_runner.py --prompt-file {prompt_file}"
        ),
        "install_hint": "Add a Kimi API key in Settings",
        "ready_detail": "tool-free Kimi K3 CGM pipeline ready",
        "parser_style": "codex",
        "supported_models": ["kimi-k3"],
    },
    "codex": {
        "id": "codex",
        "label": "Codex CLI",
        "vendor": "OpenAI",
        "description": "Terminal coding agent (codex exec)",
        "kind": "cli",
        "url": "https://github.com/openai/codex",
        "binary": "codex",
        "binary_candidates": ["~/.npm-global/bin/codex"],
        "cmd_env": "CODEX_CMD",
        # --json emits JSONL events (incl. per-turn token usage) instead of TTY text.
        # Landlock (workspace-write) denies all writes on this kernel, so run
        # unsandboxed — each run already gets its own workspace directory.
        # "-" reads the prompt from stdin (see prompt_on_stdin): one argv
        # string is limited to 128 KiB, and a prompt argument with a piped
        # stdin makes Codex log "Reading additional input from stdin...".
        "default_cmd": 'codex exec --json --cd {workspace} --sandbox danger-full-access --skip-git-repo-check {model_args} -',
        "install_hint": "npm install -g @openai/codex  (or: brew install --cask codex)",
    },
    "claude": {
        "id": "claude",
        "label": "Claude Code",
        "vendor": "Anthropic",
        "description": "Official Claude coding agent using a linked Pro / Max account",
        "kind": "cli",
        "url": "https://github.com/anthropics/claude-code",
        "binary": "claude",
        "binary_candidates": ["~/.local/bin/claude", "~/.npm-global/bin/claude"],
        "cmd_env": "CLAUDE_CMD",
        # The prompt goes in a file: one argv string is limited to 128 KiB.
        "default_cmd": (
            "{python} -u "
            "{root}/agent_monitor/runners/claude_runner.py --prompt-file {prompt_file}"
        ),
        "install_hint": "Install the official Claude Code CLI and link a Claude Pro / Max account",
        "parser_style": "claude",
        "supported_models": ["sonnet", "opus", "fable", "claude-haiku-4-5"],
        "model_note": (
            "Claude Code subscription models: sonnet, opus, fable, "
            "claude-haiku-4-5"
        ),
    },
    "openclaude": {
        "id": "openclaude",
        "label": "OpenClaude",
        "vendor": "Gitlawb",
        "description": "Open-source coding-agent CLI, multi-provider",
        "kind": "cli",
        "url": "https://github.com/Gitlawb/openclaude",
        "binary": "openclaude",
        "binary_candidates": ["~/.npm-global/bin/openclaude"],
        "cmd_env": "OPENCLAUDE_CMD",
        # The wrapper selects Codex subscription auth when CODEX_HOME is
        # present, otherwise preserving the OpenAI API-key path.
        "default_cmd": (
            "{python} -u "
            "{root}/agent_monitor/runners/openclaude_runner.py {prompt}"
        ),
        "install_hint": "npm install -g @gitlawb/openclaude@latest",
        "supported_models": ["gpt-5.6-sol"],
        "model_note": "Codex OAuth adapter: gpt-5.6-sol (codexplan) only",
    },
    "openhands": {
        "id": "openhands",
        "label": "OpenHands",
        "vendor": "OpenHands",
        "description": "AI-driven development agent (headless CLI)",
        "kind": "cli",
        "url": "https://github.com/OpenHands/openhands",
        "binary": "openhands",
        "binary_candidates": ["~/.local/bin/openhands"],
        "cmd_env": "OPENHANDS_CMD",
        # Wrapper uses OpenHands Codex ACP for subscription users and execs the
        # original headless API-key CLI otherwise.
        "default_cmd": (
            "{python} -u "
            "{root}/agent_monitor/runners/openhands_subscription_runner.py {prompt}"
        ),
        "install_hint": "uv tool install openhands   (openhands.dev)",
        # The subscription wrapper normalizes both ACP and codex-exec fallback
        # output to Codex JSONL events.  The post-run OpenHands state reader is
        # still invoked by jobs.py for native/API-key runs.
        "parser_style": "codex",
    },
    "openclaw": {
        "id": "openclaw",
        "label": "OpenClaw",
        "vendor": "OpenClaw",
        "description": "Personal AI assistant agent (embedded local run)",
        "kind": "cli",
        "url": "https://github.com/openclaw/openclaw",
        "binary": "openclaw",
        "binary_candidates": ["~/.npm-global/bin/openclaw"],
        "cmd_env": "OPENCLAW_CMD",
        # Wrapper chooses OpenClaw's Codex app-server route for subscription
        # users and keeps the prior API-key route as fallback.
        "default_cmd": (
            "{python} -u "
            "{root}/agent_monitor/runners/openclaw_runner.py {prompt} {ws_name}"
        ),
        # openclaw needs node >=24.15; setup.sh installs one to ~/.local/node24.
        "extra_path": ["~/.local/node24/bin", "~/.npm-global/bin"],
        "install_hint": "npm install -g --prefix ~/.npm-global openclaw@latest (needs Node >= 24.15)",
        "parser_style": "openclaw",
    },
    "deepagents": {
        "id": "deepagents",
        "label": "DeepAgents",
        "vendor": "LangChain",
        "description": "LangGraph deep agent — planning, filesystem, sub-agents",
        "kind": "cli",
        "url": "https://github.com/langchain-ai/deepagents",
        "check_file": "engines/deepagents/.venv/bin/python",
        "cmd_env": "DEEPAGENTS_CMD",
        "default_cmd": (
            "{root}/engines/deepagents/.venv/bin/python -u "
            "{root}/agent_monitor/runners/deepagents_runner.py --prompt-file {prompt_file}"
        ),
        "install_hint": "./setup.sh  (creates engines/deepagents/.venv)",
        "parser_style": "codex",
    },
    "plain": {
        "id": "plain",
        "label": "Plain (no harness)",
        "vendor": "baseline",
        "description": "Single LLM call — no scaffolding, the baseline",
        "kind": "cli",
        "url": None,
        "check_file": "agent_monitor/runners/plain_runner.py",
        "cmd_env": "PLAIN_CMD",
        "default_cmd": (
            "{python} -u "
            "{root}/agent_monitor/runners/plain_runner.py --prompt-file {prompt_file}"
        ),
        "install_hint": "needs OPENAI_API_KEY",
        "parser_style": "codex",
    },
    "metaharness": {
        "id": "metaharness",
        "label": "Meta-Harness",
        "vendor": "Stanford IRIS",
        "description": "Harness-evolution loop: solver → evaluator → proposer",
        "kind": "cli",
        "url": "https://github.com/stanford-iris-lab/meta-harness",
        "check_file": "agent_monitor/runners/metaharness_runner.py",
        "cmd_env": "METAHARNESS_CMD",
        "default_cmd": (
            "{python} -u "
            "{root}/agent_monitor/runners/metaharness_runner.py --prompt-file {prompt_file}"
        ),
        "install_hint": "./setup.sh  (needs OPENAI_API_KEY)",
        "parser_style": "codex",
    },
    "deepseek_harness": {
        "id": "deepseek_harness",
        "label": "DeepSeek Harness",
        "vendor": "DeepSeek AI",
        "description": "Official headless harness · native API / Codex compatibility",
        "kind": "cli",
        "url": "https://github.com/deepseek-ai/deepseek-harness",
        "check_file": "agent_monitor/runners/deepseek_harness_runner.py",
        "required_binary": "npx",
        "cmd_env": "DEEPSEEK_HARNESS_CMD",
        "default_cmd": (
            "{python} -u "
            "{root}/agent_monitor/runners/deepseek_harness_runner.py {prompt}"
        ),
        "install_hint": "needs npx and DEEPSEEK_API_KEY, or a linked Codex account",
        "ready_detail": "official dsh headless adapter ready",
        "parser_style": "codex",
    },
    # Ansatz Analyze checks an existing manuscript result by result into a
    # verdict-annotated DAG.  It is the only engine of the Analyze mode
    # (``run_kind: "analyze"``), never serves a Prove request, and is
    # response-only and tool-free on every route (sponsored Kimi K3, an own
    # API key, or an administrator's Codex / Claude Code account).  The runner
    # reads ``analysis/request.json`` from its workspace, so the command has no
    # ``{prompt}``.  It is listed only while AGENT_MONITOR_ANALYZE_ENABLED is on.
    "analyze": {
        "id": "analyze",
        "label": "Ansatz Analyze",
        "vendor": "Ansatz · Analyze",
        "description": (
            "Checks an existing manuscript result by result into a verdict DAG · "
            "tool-free · checked by a model verifier, not a certificate"
        ),
        "kind": "cli",
        "url": None,
        "check_file": "agent_monitor/runners/analyze_runner.py",
        "cmd_env": "ANALYZE_CMD",
        "default_cmd": (
            "{python} -u "
            "{root}/agent_monitor/runners/analyze_runner.py"
        ),
        "install_hint": "Ansatz Analyze runner is missing",
        "ready_detail": "tool-free manuscript analysis ready",
        "parser_style": "codex",
        "modes": ["analyze"],
    },
    # Ansatz Readability rewrites an existing manuscript part by part for a
    # reader (keeping every statement and argument), with an independent
    # model check of each part, a paper map and a notation table.  It is the
    # only engine of the Readability mode (``run_kind: "readability"``) and is
    # response-only and tool-free on every route (sponsored Kimi K3, an own
    # Kimi key, or an administrator's Codex / Claude Code account).  The
    # runner reads ``readability/request.json``; it is listed only while
    # AGENT_MONITOR_READABILITY_ENABLED is on.
    "readability": {
        "id": "readability",
        "label": "Ansatz Readability",
        "vendor": "Ansatz · Readability",
        "description": (
            "Rewrites an existing manuscript so a reader can follow it, keeping every statement · "
            "paper map and notation · checked against the original by a model, not a certificate"
        ),
        "kind": "cli",
        "url": None,
        "check_file": "agent_monitor/runners/readability_runner.py",
        "cmd_env": "READABILITY_CMD",
        "default_cmd": (
            "{python} -u "
            "{root}/agent_monitor/runners/readability_runner.py"
        ),
        "install_hint": "Ansatz Readability runner is missing",
        "ready_detail": "tool-free readable versions ready",
        "parser_style": "codex",
        "modes": ["readability"],
    },
}

# Console modes: every engine proves (``run_kind: "prove"``) unless its spec
# names other modes.  The listing advertises them so the browser shows the
# Analyze mode only when this server offers it.
DEFAULT_MODES = ("prove",)
ANALYZE_ENGINE_ID = "analyze"
READABILITY_ENGINE_ID = "readability"
# Engines that read a manuscript with the Analyze parsers.
MANUSCRIPT_ENGINE_IDS = frozenset({ANALYZE_ENGINE_ID, READABILITY_ENGINE_ID})
# Parser packages the Analyze runner imports (pinned in pyproject.toml).
ANALYZE_PACKAGES = ("pylatexenc", "pypdf")


def engine_modes(engine_id: str) -> list[str]:
    """The console modes ``engine_id`` runs (``["prove"]`` for proof engines)."""
    spec = CLI_ENGINES.get(engine_id) or {}
    return list(spec.get("modes") or DEFAULT_MODES)


def analyze_missing_packages() -> list[str]:
    """Analyze parser packages this interpreter cannot import."""
    return [name for name in ANALYZE_PACKAGES if importlib.util.find_spec(name) is None]


def _analyze_enabled() -> bool:
    from agent_monitor.analyze import limits as analyze_limits

    return analyze_limits.enabled()


def _readability_enabled() -> bool:
    from agent_monitor.readability import limits as readability_limits

    return readability_limits.enabled()

_ROOT = Path(__file__).resolve().parent.parent
CODEX_SUBSCRIPTION_ENGINES = frozenset(
    {"hermes", "improof", "math_harness", "codex", "openclaude", "openhands", "openclaw", "deepagents", "metaharness", "plain", "deepseek_harness", "analyze", "readability"}
)
CLAUDE_SUBSCRIPTION_ENGINES = frozenset(
    {"claude", "improof", "math_harness", "openclaw", "deepagents", "plain", "metaharness", "analyze", "readability"}
)
# Manual Formal Lean harnesses must have a real root ``Proof.lean`` contract.
# This is deliberately narrower than the informal/auth capability sets above:
# OpenHands and DeepSeek Harness currently require ``proof.md`` at runner exit.
# Advertising those
# engines in Lean Verification makes a successful child look like a missing
# formal proof, so keep them fail-closed until their runners validate a fresh
# ``Proof.lean`` themselves.
FORMAL_LEAN_ENGINES = frozenset(
    {"codex", "claude", "openclaude", "openclaw", "deepagents"}
)
ENGINE_AUTH_MODES: dict[str, list[str]] = {
    "kimi": ["api_key"],
    "hermes": ["codex_subscription", "api_key"],
    "improof": ["codex_subscription", "claude_subscription", "api_key"],
    "math_harness": ["codex_subscription", "claude_subscription", "api_key"],
    "ucla": ["api_key"],
    "codex": ["codex_subscription", "api_key"],
    "claude": ["claude_subscription"],
    "openclaude": ["codex_subscription", "api_key"],
    "openhands": ["codex_subscription", "api_key"],
    "openclaw": ["codex_subscription", "claude_subscription", "api_key"],
    "deepagents": ["codex_subscription", "claude_subscription", "api_key"],
    "plain": ["codex_subscription", "claude_subscription", "api_key"],
    "metaharness": ["codex_subscription", "claude_subscription", "api_key"],
    "deepseek_harness": ["codex_subscription", "api_key"],
    "analyze": ["codex_subscription", "claude_subscription", "api_key"],
    "readability": ["codex_subscription", "claude_subscription", "api_key"],
}

# ``~/.npm-global/bin`` comes first: it holds the exactly pinned, vetted Codex
# build (``npm i -g --prefix ~/.npm-global @openai/codex@0.160.0``), while
# ``~/.local/bin/codex`` may point at a self-updating standalone install.
_DEFAULT_EXTRA_PATHS = [
    "~/.npm-global/bin",
    "~/.local/node24/bin",
    "~/.local/bin",
    "~/.cargo/bin",
]


def tool_search_path() -> str:
    """PATH that includes user-level Node / npm / uv tool installs."""
    extras = [str(Path(p).expanduser()) for p in _DEFAULT_EXTRA_PATHS if Path(p).expanduser().is_dir()]
    extras.append(os.environ.get("PATH", ""))
    return os.pathsep.join(extras)


def which_tool(binary: str) -> str | None:
    return shutil.which(binary, path=tool_search_path())

def _cli_available(spec: dict[str, Any]) -> tuple[bool, str | None]:
    """Return (available, resolved_command_template)."""
    override = os.environ.get(spec.get("cmd_env") or "", "")
    if override:
        return True, override
    if spec.get("dir_env"):
        d = os.environ.get(spec["dir_env"], "")
        if not d or not Path(d).is_dir():
            return False, None
        if not spec.get("default_cmd"):
            return False, None
        return True, spec["default_cmd"]
    if spec.get("explicit_command_required"):
        # An engine that bypasses approvals or sandboxing runs only through an
        # operator-supplied command override, its explicit safety gate.
        return False, None
    if spec.get("check_file"):
        if not (_ROOT / spec["check_file"]).exists():
            return False, None
        required_binary = spec.get("required_binary")
        if required_binary and not which_tool(required_binary):
            return False, None
        return True, spec.get("default_cmd")
    binary = spec.get("binary")
    if binary and which_tool(binary):
        return True, spec.get("default_cmd")
    for cand in spec.get("binary_candidates") or []:
        if Path(cand).expanduser().exists():
            return True, spec.get("default_cmd")
    return False, None


def _builtin_available(engine_id: str) -> tuple[bool, str]:
    if engine_id == "hermes":
        ok = (_ROOT / "engines" / "hermes_core" / "agent").is_dir()
        return ok, "embedded Hermes core" if ok else "Hermes core is missing"
    if engine_id == "improof":
        entry = _ROOT / "engines" / "improof" / "scripts" / "run_workflow.py"
        configured_python = os.environ.get("IMPROOF_PYTHON")
        local_python = _ROOT / "engines" / "improof" / ".venv" / "bin" / "python"
        runtime = bool((configured_python and Path(configured_python).is_file()) or local_python.is_file())
        ok = entry.is_file()
        detail = "workflow runtime ready" if runtime else "source ready; set IMPROOF_PYTHON for the isolated runtime"
        return ok, detail if ok else "IMProof entrypoint is missing"
    if engine_id == "ucla":
        entry = Path(os.environ.get("UCLA_ENTRY") or _ROOT / "engines" / "ucla" / "harness_0518_Final.py")
        return entry.is_file(), "UCLA harness entrypoint" if entry.is_file() else "UCLA entrypoint is missing"
    return False, "unknown engine"


def list_engines() -> list[dict[str, Any]]:
    """All engines with availability, auth and health info for the UI."""
    out: list[dict[str, Any]] = []
    for engine in BUILTIN_ENGINES:
        ok, detail = _builtin_available(engine["id"])
        out.append({
            **engine,
            "available": ok,
            "hint": None if ok else detail,
            "health": "ready" if ok else "setup_required",
            "health_detail": detail,
            "auth_modes": ENGINE_AUTH_MODES.get(engine["id"], []),
            "subscription_supported": engine["id"] in CODEX_SUBSCRIPTION_ENGINES,
            "claude_subscription_supported": engine["id"] in CLAUDE_SUBSCRIPTION_ENGINES,
            "formal_lean_supported": engine["id"] in FORMAL_LEAN_ENGINES,
            "modes": list(DEFAULT_MODES),
        })
    for spec in CLI_ENGINES.values():
        if spec["id"] == ANALYZE_ENGINE_ID and not _analyze_enabled():
            # AGENT_MONITOR_ANALYZE_ENABLED=0 hides the Analyze mode entirely.
            continue
        if spec["id"] == READABILITY_ENGINE_ID and not _readability_enabled():
            # So does AGENT_MONITOR_READABILITY_ENABLED=0 for Readability.
            continue
        ok, _cmd = _cli_available(spec)
        hint = spec.get("install_hint")
        if ok and spec["id"] in MANUSCRIPT_ENGINE_IDS:
            missing = analyze_missing_packages()
            if missing:
                ok = False
                mode = "Analyze" if spec["id"] == ANALYZE_ENGINE_ID else "Readability"
                hint = (
                    f"{mode} needs the Python package(s) " + ", ".join(missing)
                    + " installed on this server"
                )
        out.append({
            "id": spec["id"],
            "label": spec["label"],
            "vendor": spec["vendor"],
            "description": spec["description"],
            "kind": "cli",
            "url": spec["url"],
            "available": ok,
            "hint": None if ok else hint,
            "health": "ready" if ok else "setup_required",
            "health_detail": (spec.get("ready_detail") or "runner available") if ok else hint,
            "auth_modes": ENGINE_AUTH_MODES.get(spec["id"], []),
            "subscription_supported": spec["id"] in CODEX_SUBSCRIPTION_ENGINES,
            "claude_subscription_supported": spec["id"] in CLAUDE_SUBSCRIPTION_ENGINES,
            "formal_lean_supported": spec["id"] in FORMAL_LEAN_ENGINES,
            "risk_level": spec.get("risk_level"),
            "risk_note": spec.get("risk_note"),
            "supported_models": list(spec.get("supported_models") or []),
            "model_note": spec.get("model_note"),
            "modes": engine_modes(spec["id"]),
        })
    return out


def all_engine_ids() -> set[str]:
    """Engine IDs accepted for new runs (historical IDs are excluded)."""
    return {e["id"] for e in BUILTIN_ENGINES} | set(CLI_ENGINES.keys())


def known_engine_ids() -> set[str]:
    """Every startable or historical engine ID understood by stored records."""
    return all_engine_ids() | set(LEGACY_ENGINE_IDS)


def supported_models(engine_id: str) -> list[str]:
    """Engine-specific allowlist; empty means the account/provider list applies."""
    return list((CLI_ENGINES.get(engine_id) or {}).get("supported_models") or [])


# Where ``{prompt_file}`` puts the task prompt.  A dot-file, which the run's
# file list and export skip (the prompt can carry the agent profile); the
# wrapper deletes it once read, the console once the wrapper has ended.
PROMPT_FILENAME = ".agent-monitor-prompt"


def _write_prompt_file(workspace: Path, prompt: str) -> Path:
    """Write the task prompt for a wrapper that reads it from a file.

    One argv string is limited to 128 KiB on Linux, which a long problem in a
    3-byte script (Chinese, Japanese, Korean) exceeds.  The file is created
    afresh, never through a link an earlier agent left in the workspace.
    """
    path = Path(workspace).absolute() / PROMPT_FILENAME
    # The workspace is the agent's: it may have left anything under this name.
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8", newline="") as handle:
        handle.write(prompt)
    return path


def discard_prompt_file(workspace: Path) -> None:
    """Remove a prompt file its wrapper never read (it could not start, or was
    stopped first)."""
    try:
        (Path(workspace) / PROMPT_FILENAME).unlink(missing_ok=True)
    except OSError:
        pass


def prompt_on_stdin(engine_id: str, argv: list[str] | None) -> bool:
    """True when a Codex command reads its task prompt from stdin (``-``).

    The Kimi Codex wrapper (runners/kimi_codex_runner.py) takes ``-`` too.
    An operator's ``CODEX_CMD`` may still pass ``{prompt}`` as an argument.
    """
    return engine_id == "codex" and bool(argv) and argv[-1] == "-"


def send_prompt_on_stdin(proc: subprocess.Popen[str], prompt: str) -> None:
    """Write the task prompt to ``proc``'s stdin and close it, from a thread.

    The prompt can exceed the pipe buffer, and the caller must meanwhile read
    the CLI's output and keep its stop and timeout checks running.
    """
    stdin = proc.stdin
    if stdin is None:
        return

    def write() -> None:
        try:
            stdin.write(prompt)
        except (OSError, ValueError):
            pass  # The CLI exited early; its exit status says why.
        finally:
            try:
                stdin.close()
            except (OSError, ValueError):
                pass

    threading.Thread(target=write, name=f"cli-prompt-{proc.pid}", daemon=True).start()


def build_cli_command(
    engine_id: str,
    *,
    prompt: str,
    workspace: Path,
    problem_file: Path,
    model: str | None = None,
) -> list[str] | None:
    """Resolve a CLI engine's command as argv list, or None if unavailable.

    ``{prompt_file}`` writes the prompt to a file in ``workspace`` (see
    ``_write_prompt_file``) and stands for its path.
    """
    spec = CLI_ENGINES.get(engine_id)
    if not spec:
        return None
    ok, template = _cli_available(spec)
    if not ok or not template:
        return None
    argv: list[str] = []
    prompt_path: Path | None = None
    for token in shlex.split(template):
        if token == "{model_args}":
            if model:
                argv.extend(["--model", model])
            continue
        token = token.replace("{root}", str(_ROOT))
        token = token.replace("{python}", sys.executable)
        token = token.replace("{home}", str(Path.home()))
        token = token.replace("{workspace}", str(workspace))
        token = token.replace("{ws_name}", workspace.name)
        token = token.replace("{problem_file}", str(problem_file))
        if "{prompt_file}" in token:
            prompt_path = prompt_path or _write_prompt_file(workspace, prompt)
            token = token.replace("{prompt_file}", str(prompt_path))
        if "{prompt}" in token:
            token = token.replace("{prompt}", prompt)
        argv.append(token)
    return argv


def engine_extra_path(engine_id: str) -> list[str]:
    """Expanded PATH prefixes an engine's subprocess needs (e.g. newer node)."""
    spec = CLI_ENGINES.get(engine_id) or {}
    extras = [str(Path(p).expanduser()) for p in spec.get("extra_path") or []]
    seen = set(extras)
    for p in _DEFAULT_EXTRA_PATHS:
        d = str(Path(p).expanduser())
        if d not in seen:
            extras.append(d)
            seen.add(d)
    return extras
