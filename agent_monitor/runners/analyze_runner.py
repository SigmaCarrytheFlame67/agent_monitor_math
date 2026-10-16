#!/usr/bin/env python3
"""Ansatz Analyze: check an existing manuscript into a verdict-annotated DAG.

Run from a run workspace that holds ``analysis/request.json`` (written by
``jobs.start_job`` with :mod:`agent_monitor.analyze.request`)::

    python -u agent_monitor/runners/analyze_runner.py            # an analysis
    python -u agent_monitor/runners/analyze_runner.py --recheck <node_id>

Stages (``pipeline.stage`` events, ids shared with the CGM pipeline):
``scaffold`` 1 Ingest (fetch or unpack the source into ``analysis/source``),
``work`` 2 Segment (statement DAG), ``verify`` 3 Check (one paper-map call,
then one tool-free verifier call per result, by priority, within the caps),
``curate`` 4 Roll-up (closure verdicts) and ``finalize`` 5 Report.  Outputs:
``analysis/manuscript.json``, ``segments.json``, ``nodes/<id>.json``,
``analysis_graph.json``, ``report.md``, ``receipt.json`` (written last) and
the deterministic projection ``proof_graph.json``.  Every verdict is a model
verifier's judgement, never a certificate.

Model calls reuse the CGM response-only turn machinery of
``math_harness_runner`` (routes, reserved call budgets, the low-effort retry
of an empty Kimi reply, one format reminder, sponsored transport retries,
abandonment at the deadline and on SIGTERM).  Nothing from the manuscript is
compiled or executed; the parsers run in resource-limited subprocesses.

Environment contract (set by ``jobs.py`` exactly as for the ``kimi`` and
``math_harness`` engines):

* sponsored Kimi: ``AGENT_MONITOR_SPONSORED_KIMI=1``, ``KIMI_API_KEY`` (the
  broker token from ``sponsored_kimi_client.issue_credentials(engine=
  "math_harness", ...)``), ``KIMI_API_BASE=http://127.0.0.1:<port>/internal/v1``
  (attested here), ``AGENT_MONITOR_MAX_OUTPUT_TOKENS=8192``;
* own key: the provider key (``KIMI_API_KEY``, ``OPENAI_API_KEY``, ...) and
  ``AGENT_MONITOR_SELECTED_MODEL``; own-key Kimi uses the operator endpoint
  ``AGENT_MONITOR_KIMI_BASE_URL`` (a user-settable ``KIMI_API_BASE`` is ignored);
* admin subscriptions: ``AGENT_MONITOR_CODEX_SUBSCRIPTION=1`` + ``CODEX_HOME``
  or ``AGENT_MONITOR_CLAUDE_SUBSCRIPTION=1`` + ``CLAUDE_CONFIG_DIR`` (tool-free,
  response-only, one call in flight); Max mode is refused;
* guests: ``AGENT_MONITOR_GUEST=1``; optional ``AGENT_MONITOR_ANALYZE_GITHUB_TOKEN``
  (sent only with the GitHub connector's own api.github.com calls, never with
  a link someone pasted) and the ``AGENT_MONITOR_ANALYZE_*`` caps.

The parser packages (pylatexenc, pypdf) are checked before anything runs: a
missing one is a configuration error (exit 2), not a property of the
manuscript.

Exit status: 0 published, 2 configuration (request, route or credentials),
143 stopped by SIGTERM (results that had settled are still published), 1 any
other failure (including when every attempted check failed: provider errors,
empty answers, uninterpretable output or the time limit; the structure is
still published).
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

if not __package__:
    # The registry launches this file by absolute path from the run workspace.
    repository_root = Path(__file__).resolve().parents[2]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))

from agent_monitor import kimi_k3, safe_fetch
from agent_monitor.analyze import graph as analysis_graph
from agent_monitor.analyze import latex_structure, limits, prompts, report, sources, text_structure, verdicts
from agent_monitor.analyze import request as analysis_request
from agent_monitor.analyze.titles import tex_text_refs
from agent_monitor.cgm.parsing import ParseError
from agent_monitor.runners import math_harness_runner as harness

STAGES = {"scaffold": "1 Ingest", "work": "2 Segment", "verify": "3 Check", "curate": "4 Roll-up", "finalize": "5 Report"}
LABEL = "Analyze"
MAX_REPLY_BYTES = 64_000
RECHECK_SECONDS = 900
RECHECK_MAX_CALLS = 8
GITHUB_TOKEN_ENV = "AGENT_MONITOR_ANALYZE_GITHUB_TOKEN"
_KIND_WORDS = "theorem|lemma|proposition|corollary|claim|definition|conjecture|remark|fact|observation"
_FOCUS_RE = re.compile(rf"\b({_KIND_WORDS})s?\s+((?:[A-Z]\.)?\d+(?:\.\d+)*[a-z]?)", re.I)
_DORMANT_ENV = (
    "KIMI_BASE_URL", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
)
# Claude Code / Codex and own-key API failure kinds (ClaudeBackendError.kind,
# CodexBackendError.kind, APIBackendError.kind) that every further check
# would hit as well.
_SUBSCRIPTION_STOP_KINDS = frozenset({"auth", "usage_limit", "model"})


class AnalyzeError(RuntimeError):
    """A public-safe failure (exit 1)."""

    exit_code = 1


class ConfigError(AnalyzeError):
    """The request, route or credentials are unusable (exit 2)."""

    exit_code = 2


class Stopped(AnalyzeError):
    """SIGTERM before anything could be published (exit 143)."""

    exit_code = 128 + int(signal.SIGTERM)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _write_json(path: Path, value: Any) -> None:
    _write_bytes(path, json.dumps(value, ensure_ascii=False, indent=1).encode("utf-8"))


@contextmanager
def analysis_lock(analysis: Path) -> Iterator[None]:
    """One writer at a time for a run's ``analysis`` folder."""
    analysis.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(analysis / ".lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AnalyzeError("Another analysis step is running for this run; try again when it finishes") from None
        yield
    finally:
        os.close(descriptor)


# ---------------------------------------------------------------------------
# route and environment
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteInfo:
    route: harness.Route
    name: str
    sponsored: bool
    guest: bool


def configure_route(env: dict[str, str], req: Mapping[str, Any]) -> RouteInfo:
    """Select and attest the provider route (mutates ``env`` like kimi_runner:
    a Kimi route never uses a user-settable base or an ambient proxy)."""
    from agent_monitor.runners.kimi_runner import operator_base, sponsored_base

    guest = str(env.get("AGENT_MONITOR_GUEST") or "").strip() == "1" or req["role"] == "guest"
    if harness._max_mode(env):
        raise ConfigError("Max is not available for Analyze; choose one account route")
    use_codex = harness.codex_subscription_enabled(env)
    use_claude = harness.claude_subscription_enabled(env)
    if use_codex or use_claude:
        if use_codex and use_claude:
            raise ConfigError("Codex and Claude account routes cannot both be active")
        if req["role"] != "admin" or guest:
            raise ConfigError("Account subscriptions are available to administrators only")
        try:
            route = harness._codex_route(env) if use_codex else harness._claude_route(env)
        except harness.MathHarnessError as exc:
            raise ConfigError(str(exc)) from None
        return RouteInfo(route, "codex_subscription" if use_codex else "claude_subscription", False, guest)
    model = str(
        env.get("AGENT_MONITOR_ANALYZE_MODEL")
        or env.get("MATH_HARNESS_MODEL")
        or env.get("AGENT_MONITOR_SELECTED_MODEL")
        or req.get("model")
        or env.get("AGENT_MONITOR_OPENAI_MODEL")
        or ""
    ).strip()
    if not model:
        raise ConfigError("No model was selected for this analysis")
    sponsored = str(env.get("AGENT_MONITOR_SPONSORED_KIMI") or "").strip() == "1"
    if req.get("sponsored") and not sponsored:
        raise ConfigError("The sponsored Kimi route is not configured for this run")
    kimi = model.lower().startswith("kimi")
    if sponsored:
        if not kimi or model != kimi_k3.MODEL_ID:
            raise ConfigError("Sponsored analyses use Kimi K3")
        base = sponsored_base(env.get("KIMI_API_BASE", ""))
        if base is None:
            raise ConfigError("Sponsored Kimi endpoint attestation failed")
        if not str(env.get("KIMI_API_KEY") or "").strip():
            raise ConfigError("The sponsored Kimi credential is missing")
    elif kimi:
        if not str(env.get("KIMI_API_KEY") or "").strip():
            raise ConfigError("Add a Kimi API key in Settings")
        base = operator_base(env.get(operator_var()))
    if kimi:
        for name in _DORMANT_ENV:
            env.pop(name, None)
        env["KIMI_API_BASE"] = base
        env["NO_PROXY"] = env["no_proxy"] = "*"
    if guest and not kimi:
        raise ConfigError("Guest analyses use Kimi K3")
    return RouteInfo(harness.Route("api", model, "api_chat"), "sponsored_kimi" if sponsored else "api_key", sponsored, guest)


def operator_var() -> str:
    from agent_monitor.runners.kimi_runner import OPERATOR_BASE_VAR

    return OPERATOR_BASE_VAR


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------


@dataclass
class _Job:
    node_id: str
    part: int
    parts: int
    prompt: str
    stage: str = ""
    turn_job: Any = None


class AnalysisRun:
    """One analysis (or one re-check) of a run workspace."""

    def __init__(
        self,
        workspace: Path,
        req: Mapping[str, Any],
        *,
        env: Mapping[str, str],
        sink: Callable[[dict[str, Any]], None] | None = None,
        fetcher: safe_fetch.SafeFetcher | None = None,
        cancel: threading.Event | None = None,
        sleep: Callable[[float], None] = time.sleep,
        caps_override: Mapping[str, int] | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.analysis = self.workspace / "analysis"
        self.req = dict(req)
        self.env = dict(env)
        self.info = configure_route(self.env, self.req)
        role = "guest" if self.info.guest else self.req["role"]
        self.role = role
        self.caps = limits.caps_for(
            role,
            sponsored=self.info.sponsored or role == "guest",
            env=self.env,
            requested={**self.req.get("caps", {}), **(caps_override or {})},
            subscription=self.info.route.transport in ("codex_exec", "claude_code"),
        )
        self.fetcher = fetcher
        self.sleep = sleep
        self.cancel = cancel or threading.Event()
        self.emitter = harness._RunEmitter(sink or harness.emit)
        self.started_at = _utc_now()
        api_timeout = harness._bounded_int(
            self.env,
            ("AGENT_MONITOR_ANALYZE_API_TIMEOUT", "AGENT_MONITOR_CGM_API_TIMEOUT"),
            default=kimi_k3.SPONSORED_TIMEOUT_SECONDS if self.info.sponsored else 300,
            minimum=60,
            maximum=900,
        )
        bounded = self.info.sponsored or self.info.guest
        self.ctx = harness._Ctx(
            route=self.info.route,
            budgets=harness.Budgets(max_calls=self.caps.max_calls, max_seconds=self.caps.max_seconds,
                                    guest=bounded, concurrency=self.caps.concurrency),
            started=time.monotonic(),
            env=self.env,
            api_timeout=api_timeout,
            concurrency=self.caps.concurrency,
            cancel=self.cancel,
            emitter=self.emitter,
            sponsored_kimi=self.info.sponsored,
            kimi_retries=kimi_k3.RetryAllowance(kimi_k3.SPONSORED_RUN_RETRIES) if self.info.sponsored else None,
            label=LABEL,
        )
        self.budget = prompts.BOUNDED_BUDGET if bounded else prompts.FULL_BUDGET
        self.degraded: list[str] = []
        self.stopped = False
        self.provider_failure: str | None = None
        self.permanent_failure = False
        self.check_seq = 0
        self.attempted = False
        self.paper_map: dict[str, Any] = {}

    # -- events -------------------------------------------------------------

    def emit(self, obj: dict[str, Any]) -> None:
        self.emitter.deliver(obj)

    def note(self, text: str) -> None:
        self.emit({"type": "item.completed", "item": {"type": "reasoning", "text": f"{LABEL} · {text}"[:2000]}})

    def stage(self, stage_id: str, detail: str = "") -> None:
        label = STAGES[stage_id]
        self.emit({"type": "pipeline.stage", "stage": stage_id, "label": label, "detail": detail[:200]})
        self.note(label + (f" · {detail}" if detail else ""))

    def check_stop(self) -> None:
        if self.cancel.is_set():
            raise harness.RunCancelled("the analysis was stopped")

    # -- stages -------------------------------------------------------------

    def ingest(self) -> sources.Bundle:
        source = self.req["source"]
        self.stage("scaffold", {"url": "link", "paste": "pasted text", "upload": "uploaded files"}[source["kind"]])
        for name in ("source", "nodes"):
            shutil.rmtree(self.analysis / name, ignore_errors=True)
        for name in ("manuscript.json", "segments.json", "analysis_graph.json", "report.md", "receipt.json", "pdf_text.txt"):
            (self.analysis / name).unlink(missing_ok=True)
        remaining = max(10.0, min(safe_fetch.BUNDLE_SECONDS, self.ctx.deadline - time.monotonic()))
        try:
            bundle = sources.fetch_bundle(
                source,
                self.analysis / "source",
                workspace=self.workspace,
                role=self.role,
                fetcher=self.fetcher,
                github_token=str(self.env.get(GITHUB_TOKEN_ENV) or "") or None,
                # Stop cuts a fetch short (the budget's watchdog closes its socket).
                budget=safe_fetch.Budget(seconds=remaining, cancel=self.cancel),
                sleep=self.sleep,
            )
        except sources.SourceError as exc:
            self.check_stop()
            raise AnalyzeError(f"The source could not be used: {exc}") from None
        except (OSError, ValueError) as exc:
            self.check_stop()
            raise AnalyzeError(f"The source could not be unpacked or stored ({type(exc).__name__})") from None
        self.check_stop()
        files = bundle.files()
        detail = f"{bundle.title or 'manuscript'} · {len(files)} file{'s' if len(files) != 1 else ''}"
        self.note(f"Ingest · {detail}")
        for warning in bundle.warnings[:6]:
            self.note(f"Ingest · {warning}")
        return bundle

    def segment(self, bundle: sources.Bundle) -> tuple[dict[str, Any], dict[str, Any]]:
        self.stage("work")
        files = bundle.files()
        fmt = sources.bundle_format(files)
        segments: dict[str, Any] | None = None
        warnings: list[str] = []
        fallback_text = ""
        fallback_file = ""
        latex_statements = 0
        # Every parser runs in a resource-limited child; Stop ends it at once.
        if fmt == "tex":
            temporary = self.analysis / ".segments-tex.json"
            try:
                segments = latex_structure.run_sandboxed(bundle.root, temporary, cancel=self.cancel)
            except latex_structure.SandboxCancelled:
                raise harness.RunCancelled("the analysis was stopped") from None
            except latex_structure.ParserUnavailable as exc:
                raise ConfigError(f"{exc}; please tell the operator") from None
            except latex_structure.LatexStructureError as exc:
                warnings.append(f"LaTeX structure: {exc}")
            finally:
                temporary.unlink(missing_ok=True)
            latex_statements = len((segments or {}).get("nodes") or [])
            tex_files = sorted((item for item in files if item["role"] == "tex"), key=lambda item: -item["bytes"])
            root_name = (segments or {}).get("root_file") or (tex_files[0]["path"] if tex_files else "")
            if root_name:
                fallback_file = root_name
                fallback_text = _read_capped(bundle.root / root_name)
            if (segments is None or not segments.get("nodes")) and root_name:
                try:
                    text_doc = self.segment_text_file(bundle.root / root_name, root_name, "txt")
                except AnalyzeError as exc:
                    warnings.append(str(exc))
                    text_doc = None
                if text_doc is not None and (segments is None or text_doc["nodes"]):
                    text_doc["warnings"] = warnings + ["No theorem environments were found; headings were used"]
                    text_doc["format"] = "tex"
                    segments = text_doc
        elif fmt in ("md", "txt"):
            main = max((item for item in files if item["role"] == fmt), key=lambda item: (item["bytes"], item["path"]))
            segments = self.segment_text_file(bundle.root / main["path"], main["path"], fmt)
            fallback_file = main["path"]
            fallback_text = text_structure.normalize(_read_capped(bundle.root / main["path"]))
        elif fmt == "pdf":
            pdf = next(item for item in files if item["role"] == "pdf")
            try:
                segments = text_structure.pdf_document(bundle.root / pdf["path"], self.analysis / "pdf_text.txt",
                                                       file=pdf["path"], cancel=self.cancel)
            except latex_structure.SandboxCancelled:
                raise harness.RunCancelled("the analysis was stopped") from None
            except latex_structure.ParserUnavailable as exc:
                raise ConfigError(f"{exc}; please tell the operator") from None
            except text_structure.TextStructureError as exc:
                raise AnalyzeError(str(exc)) from None
            fallback_file = pdf["path"]
            fallback_text = "\n".join(_read_capped(self.analysis / "pdf_text.txt").split("\n" + text_structure.PAGE_MARK + "\n"))
        else:
            raise AnalyzeError("The source has no TeX, Markdown, text or PDF file to analyze")
        if segments is None:
            raise AnalyzeError("No theorems, lemmas or other results were found in the source"
                               + (f" ({warnings[0]})" if warnings else ""))
        segments.setdefault("warnings", [])
        segments["warnings"] = warnings + [item for item in segments["warnings"] if item not in warnings]
        # The model only locates statements when no structure was found: a
        # TeX statement found by the LaTeX parser (with its labels, references
        # and included files) is never traded for model-located quotes.
        if (not latex_statements and len(segments["nodes"]) < text_structure.MIN_STRUCTURED_NODES
                and fallback_text.strip()):
            modelled = self.model_segments(fallback_text, fallback_file, segments.get("format") or fmt)
            if modelled is not None and len(modelled["nodes"]) > len(segments["nodes"]):
                modelled["warnings"] = segments["warnings"] + modelled["warnings"]
                modelled["title"] = segments.get("title") or modelled["title"]
                modelled["abstract"] = segments.get("abstract") or ""
                modelled["intro"] = segments.get("intro") or fallback_text[:20_000]
                modelled["macros"] = segments.get("macros") or {}
                modelled["bibliography"] = segments.get("bibliography") or {}
                segments = modelled
        if not segments["nodes"]:
            if self.provider_failure:
                # The model's provider failed, which says nothing about the manuscript.
                raise AnalyzeError(f"The results in the source could not be located: {self.provider_failure}")
            raise AnalyzeError("No theorems, lemmas or other results were found in the source"
                               + (f" ({segments['warnings'][0]})" if segments["warnings"] else ""))
        _write_json(self.analysis / "segments.json", segments)
        manuscript = bundle.manuscript(root_file=segments.get("root_file"), fmt=segments.get("format") or fmt)
        if not manuscript["title"]:
            manuscript["title"] = segments.get("title") or {
                "paste": "Pasted manuscript", "upload": "Uploaded manuscript"}.get(bundle.source_kind, "")
        if segments.get("math_fidelity"):
            manuscript["math_fidelity"] = segments["math_fidelity"]
        manuscript["warnings"] = list(dict.fromkeys(manuscript["warnings"] + segments["warnings"]))[:20]
        manuscript["parser"] = segments.get("parser")
        if bundle.file_base_url:
            manuscript["file_base_url"] = bundle.file_base_url
        _write_json(self.analysis / "manuscript.json", manuscript)
        edges = sum(1 for edge in segments["edges"] if not edge.get("weak"))
        self.note(f"Segment · {len(segments['nodes'])} statements · {edges} dependencies · {segments.get('parser')}")
        return segments, manuscript

    def segment_text_file(self, path: Path, file: str, fmt: str) -> dict[str, Any]:
        """Heading and header segmentation of one text file, in the sandboxed child."""
        try:
            return text_structure.segment_sandboxed(path, self.analysis / ".segments-text.json", file=file, fmt=fmt,
                                                    cancel=self.cancel)
        except latex_structure.SandboxCancelled:
            raise harness.RunCancelled("the analysis was stopped") from None
        except text_structure.TextStructureError as exc:
            raise AnalyzeError(f"The source could not be segmented: {exc}") from None

    def model_segments(self, text: str, file: str, fmt: str) -> dict[str, Any] | None:
        """Model-assisted segmentation (verbatim quotes only), budget permitting."""
        reserve = 2
        chunks = text_structure.chunks(text, limit=max(0, min(4, self.ctx.remaining() - reserve)))
        if not chunks:
            return None
        self.note(f"Segment · few statements found; asking the model to locate results in {len(chunks)} chunk(s)")
        items: list[dict[str, Any]] = []
        for index, (offset, chunk) in enumerate(chunks, 1):
            self.check_stop()
            try:
                job = harness._run_single(
                    self.ctx,
                    f"segment-{index}",
                    reserve=reserve,
                    work=lambda turn, chunk=chunk, index=index: harness._call_and_parse(
                        self.ctx, turn, stage=f"segment-{index}", system=prompts.SEGMENT_PROMPT,
                        user=prompts.segment_prompt(chunk, index, len(chunks)), limit=MAX_REPLY_BYTES,
                        parse=verdicts.parse_segments, family=prompts.LIGHT_FAMILY),
                )
                found, _reply = harness._job_result(job)
            except harness.RunCancelled:
                raise
            except (harness._BudgetExhausted, harness.MathHarnessError, ParseError) as exc:
                self.degraded.append("model-segmentation-incomplete")
                if (isinstance(exc, harness.ProviderCallError) and not isinstance(exc, harness.EmptyReplyError)
                        and not self.cancel.is_set() and not self.ctx.deadline_passed()):
                    # Say why (a lapsed sign-in, a usage limit, an invalid key), and
                    # send no more chunks when each would be refused the same way.
                    self._note_failure(exc)
                    if self.permanent_failure:
                        break
                continue
            items.extend(dict(item, _chunk=(offset, len(chunk))) for item in found)
        if not items:
            return None
        document = text_structure.apply_model_segments(text, items, file=file, fmt=fmt)
        covered = sum(len(chunk) for _offset, chunk in chunks)
        if covered < len(text):
            document["warnings"].append(
                f"The model looked for statements in the first {max(1, covered // 1000)} KB of "
                f"{max(1, len(text) // 1000)} KB of text only")
        return document

    # -- checks ---------------------------------------------------------------

    def run_paper_map(self, segments: Mapping[str, Any], planned: int) -> None:
        """One paper-map call, only when ``planned`` calls (the planned
        checks) stay available after it; its retries never use them."""
        nodes = segments["nodes"]
        if self.permanent_failure:
            return  # refused for good while locating results: the map would be too
        if not nodes or self.ctx.remaining() < planned + 1:
            if nodes:
                self.degraded.append("paper-map-skipped-for-budget")
            return
        user = prompts.paper_map_prompt(
            title=str(segments.get("title") or ""), abstract=str(segments.get("abstract") or ""),
            intro=str(segments.get("intro") or ""), results=nodes, focus=str(self.req.get("focus") or ""))
        evidence = " ".join([str(segments.get("title") or ""), str(segments.get("abstract") or ""),
                             str(segments.get("intro") or "")] + [str(node.get("statement") or "") for node in nodes])
        ids = [node["id"] for node in nodes]
        try:
            job = harness._run_single(
                self.ctx, "map", reserve=planned,
                work=lambda turn: harness._call_and_parse(
                    self.ctx, turn, stage="map", system=prompts.PAPER_MAP_PROMPT, user=user, limit=MAX_REPLY_BYTES,
                    parse=partial(verdicts.parse_paper_map, node_ids=ids, evidence=evidence),
                    family=prompts.LIGHT_FAMILY))
            self.paper_map, _reply = harness._job_result(job)
        except harness.RunCancelled:
            raise
        except harness.ProviderCallError as exc:
            self._note_failure(exc)
            self.degraded.append("paper-map-failed")
        except (harness._BudgetExhausted, harness.MathHarnessError, ParseError):
            self.degraded.append("paper-map-failed")
        if self.paper_map.get("main_results"):
            # Progress names results as the manuscript does ("Theorem 1.1"), not by label id.
            labels = {node["id"]: prompts.node_label(node) for node in nodes}
            self.note("Check · main results: " + ", ".join(
                labels.get(node_id, node_id) for node_id in self.paper_map["main_results"][:6]))

    def priority(self, segments: Mapping[str, Any]) -> list[dict[str, Any]]:
        return prioritize(segments, focus=str(self.req.get("focus") or ""), paper_map=self.paper_map)

    def prompts_for(self, node: Mapping[str, Any], segments: Mapping[str, Any]) -> list[str]:
        sp_limit, pred_limit, notation_limit, bib_limit = self.budget
        statement = prompts.clip(str(node.get("statement") or ""), max(2000, sp_limit // 3))
        part_limit = max(2000, sp_limit - prompts.utf8_len(statement) - 600)
        proof = str(node.get("proof") or "")
        parts = prompts.split_proof(proof, part_limit) if proof.strip() else [""]
        by_id = {item["id"]: item for item in segments["nodes"]}
        predecessors = []
        for edge in segments["edges"]:
            if edge["to"] == node["id"] and edge["from"] in by_id:
                used = dict(by_id[edge["from"]])
                used["weak"] = bool(edge.get("weak"))
                used["external_result"] = bool(used.get("external"))
                predecessors.append(used)
        predecessor_text = prompts.predecessor_block(predecessors, pred_limit)
        notation = prompts.notation_block(segments.get("macros") or {}, statement + proof, [], notation_limit,
                                          context=str(node.get("context") or ""))
        references = prompts.references_block(node.get("cites") or [], segments.get("bibliography") or {}, bib_limit)
        return [
            prompts.node_prompt(node, statement=statement, proof_part=part, part=index, parts=len(parts),
                                predecessors=predecessor_text, notation=notation, references=references,
                                math_fidelity=segments.get("math_fidelity"))
            for index, part in enumerate(parts, 1)
        ]

    def check(self, segments: Mapping[str, Any], *, only: str | None = None) -> dict[str, dict[str, Any]]:
        """Check results by priority within the caps; returns per-node results."""
        nodes = segments["nodes"]
        results: dict[str, dict[str, Any]] = {}
        prompt_lists: dict[str, list[str]] = {}
        # Progress notes name what a finding cites ("Corollary 1.2", not \ref{cor:x});
        # a re-check of an older analysis reads its section labels from the stored copy.
        self.progress_refs = analysis_graph.segments_ref_labels(segments)
        for label, value in analysis_graph.source_section_labels(self.workspace).items():
            self.progress_refs.setdefault(label, (value["name"], value["number"]))

        def prompts_of(node: Mapping[str, Any]) -> list[str]:
            if node["id"] not in prompt_lists:
                prompt_lists[node["id"]] = self.prompts_for(node, segments)
            return prompt_lists[node["id"]]

        if only is None:
            checkable_count = sum(1 for node in nodes if _checkable(node))
            planned_estimate = min(self.caps.max_nodes, checkable_count)
            self.stage("verify", f"checking up to {planned_estimate} of {checkable_count} results")
            # The map runs only when it costs no planned check: count the
            # calls the planned checks need (a long proof is checked in parts).
            self.run_paper_map(segments, self.planned_calls(self.priority(segments), prompts_of))
            ranked = self.priority(segments)
        else:
            self.stage("verify", f"re-checking {only}")
            ranked = [node for node in nodes if node["id"] == only]
        jobs: list[_Job] = []
        planned_nodes = 0
        first_calls = 0
        known_ids = [node["id"] for node in nodes]
        for node in ranked:
            prompt_list = prompts_of(node)
            if self.info.guest and len(prompt_list) > 1:
                results[node["id"]] = _not_checked(node, "Too long for a guest check (sign in to check long proofs)")
                continue
            if planned_nodes >= self.caps.max_nodes:
                results[node["id"]] = _not_checked(node, "Not checked: the result-check limit for this run was reached")
                continue
            if first_calls + len(prompt_list) > self.ctx.remaining():
                results[node["id"]] = _not_checked(node, "Not checked: the model-call budget for this run was used")
                continue
            planned_nodes += 1
            first_calls += len(prompt_list)
            for index, prompt in enumerate(prompt_list, 1):
                jobs.append(_Job(node["id"], index, len(prompt_list), prompt))
        if not jobs:
            return results
        self.attempted = True
        by_id = {node["id"]: node for node in nodes}
        parts: dict[str, list[Any]] = {}
        pending = list(jobs)
        stop_reason = ""
        while pending:
            if self.cancel.is_set():
                self.stopped = True
                stop_reason = "Not checked: the analysis was stopped"
                break
            if self.ctx.deadline_passed():
                stop_reason = "Not checked: " + self._stop_reason()
                break
            if self.permanent_failure:
                stop_reason = f"Not checked: {self.provider_failure or 'the model provider refused the run'}"
                break
            wave = pending[: self.ctx.concurrency]
            later = len(pending) - len(wave)
            fit = self.ctx.remaining() - later
            if fit < 1:
                stop_reason = "Not checked: the model-call budget for this run was used"
                break
            wave = wave[:fit]
            turn_jobs = []
            for job, limit in zip(wave, harness._turn_limits(len(wave), fit - len(wave))):
                self.check_seq += 1
                job.stage = f"check-{self.check_seq}"
                try:
                    turn = self.ctx.open_turn(job.stage, limit)
                except harness._BudgetExhausted:
                    break
                job.turn_job = harness._TurnJob(turn, partial(
                    harness._call_and_parse, self.ctx, turn, stage=job.stage,
                    system=prompts.ANALYZE_VERIFIER_SYSTEM, user=job.prompt, limit=MAX_REPLY_BYTES,
                    parse=partial(verdicts.parse_node_verdict, known_ids=known_ids),
                    family=prompts.VERIFIER_FAMILY))
                turn_jobs.append(job)
            if not turn_jobs:
                stop_reason = "Not checked: the model-call budget for this run was used"
                break
            pending = pending[len(turn_jobs):]
            # Progress: which results this wave checks (a check can take minutes).
            checking = list(dict.fromkeys(
                prompts.node_label(by_id[job.node_id]) + (f" (part {job.part} of {job.parts})" if job.parts > 1 else "")
                for job in turn_jobs))
            self.note("Check · now checking " + ", ".join(checking))
            try:
                harness._run_turns(self.ctx, [job.turn_job for job in turn_jobs])
            except harness.RunCancelled:
                self.stopped = True
                for job in turn_jobs:
                    parts.setdefault(job.node_id, []).append(("stopped", job, "the analysis was stopped"))
                stop_reason = "Not checked: the analysis was stopped"
                break
            for job in turn_jobs:
                parts.setdefault(job.node_id, []).append(self._settle(job))
            for node_id in sorted({job.node_id for job in turn_jobs}):
                settled = parts[node_id]
                total = next(job.parts for job in jobs if job.node_id == node_id)
                if len(settled) == total:
                    results[node_id] = self._node_result(by_id[node_id], settled)
                    self._progress(by_id[node_id], results[node_id])
        for job in pending:
            if job.node_id not in results:
                results[job.node_id] = _not_checked(by_id[job.node_id], stop_reason or "Not checked")
        for node_id, settled in parts.items():
            if node_id not in results:
                results[node_id] = self._node_result(by_id[node_id], settled, incomplete=stop_reason or "Not checked")
        return results

    def planned_calls(self, ranked: list[dict[str, Any]], prompts_of: Callable[[Mapping[str, Any]], list[str]]) -> int:
        """Calls the checks would take (the first ``max_nodes`` results by
        priority, one call per proof part; long proofs are skipped for guests)."""
        total = planned = 0
        for node in ranked:
            if planned >= self.caps.max_nodes:
                break
            calls = len(prompts_of(node))
            if self.info.guest and calls > 1:
                continue
            planned += 1
            total += calls
        return total

    def _stop_reason(self) -> str:
        if self.cancel.is_set():
            return "the analysis was stopped"
        if self.ctx.deadline_passed():
            if "time-limit" not in self.degraded:
                self.degraded.append("time-limit")
            return "the analysis reached its time limit"
        if "budget" not in self.degraded:
            self.degraded.append("budget")
        return "the model-call budget for this run was used"

    def _settle(self, job: _Job) -> tuple[str, _Job, Any]:
        turn_job = job.turn_job
        error = turn_job.error
        if turn_job.abandoned or isinstance(error, (harness._BudgetExhausted, harness._TurnAbandoned)):
            return ("stopped", job, self._stop_reason())
        if error is None:
            verdict, reply = turn_job.outcome
            return ("verdict", job, (verdict, reply))
        if isinstance(error, harness.PromptBoundError):
            return ("failed", job, "The check prompt exceeded its size bound")
        if isinstance(error, harness.EmptyReplyError):
            # No judgement at all (Kimi's reasoning can use the whole output
            # budget).  Only Kimi routes retry at low effort.
            low = ", even at low effort" if harness._is_kimi_route(self.info.route) else ""
            return ("failed", job, f"the model returned an empty answer{low}")
        if isinstance(error, ParseError):
            return ("verdict", job, (verdicts.uninterpretable(str(error)), harness._job_reply(turn_job)))
        if isinstance(error, harness.ProviderCallError):
            if self.cancel.is_set() or self.ctx.deadline_passed():
                # The call was cut short by the run's own deadline or a stop,
                # not refused by the provider.
                return ("stopped", job, self._stop_reason())
            message = self._note_failure(error)
            return ("failed", job, message)
        if isinstance(error, harness.RunCancelled):
            return ("stopped", job, "the analysis was stopped")
        return ("failed", job, "The check failed")

    def _note_failure(self, error: harness.ProviderCallError) -> str:
        failure = getattr(error, "failure", None)
        message = ""
        if isinstance(failure, kimi_k3.CallFailure):
            message = kimi_k3.failure_message(failure, billed=self.ctx.run_processed())
            if failure.permanent:
                self.permanent_failure = True
        if not message:
            # A linked Claude Code / Codex account that must sign in again, has
            # reached its usage limit, or cannot use the model fails every
            # further check the same way, as does an own API key the provider
            # rejects or that is out of credit: say why, and start no more checks.
            cause = error.__cause__
            kind = getattr(cause, "kind", None)
            if kind in _SUBSCRIPTION_STOP_KINDS:
                message = " ".join(str(cause).split())[:400]
                self.permanent_failure = True
            elif kind == "timeout":
                message = "The model did not answer this check in time (" + " ".join(str(cause).split())[:200] + ")"
            elif failure is None and isinstance(cause, harness.APIBackendError):
                # An own key's provider says why (the key is redacted where it is raised).
                message = " ".join(str(cause).split())[:400]
        if not message:
            message = "The model provider did not answer this check"
        self.provider_failure = message
        if "provider-failure" not in self.degraded:
            self.degraded.append("provider-failure")
        return message

    def _node_result(self, node: Mapping[str, Any], settled: list[tuple[str, _Job, Any]], *,
                     incomplete: str = "") -> dict[str, Any]:
        settled = sorted(settled, key=lambda item: item[1].part)
        stages = [item[1].stage for item in settled]
        calls = [record for record in self.ctx.calls if record.get("stage", "").split("-low")[0].split("-retry")[0] in stages]
        effort = next((record.get("effort") for record in calls if record.get("effort")), None)
        observed = None
        verdict_parts = []
        failures = []
        for kind, _job, value in settled:
            if kind == "verdict":
                verdict, reply = value
                verdict_parts.append(verdict)
                if reply is not None:
                    observed = getattr(reply, "observed_model", None) or observed
            elif kind in ("failed", "stopped"):
                failures.append(str(value or ""))
        total = settled[0][1].parts if settled else 1
        base = {
            "schema": "ansatze-analyze-node-v1",
            "id": node["id"],
            "label": prompts.node_label(node),
            "parts": total,
            "calls": [record.get("stage") for record in calls],
            "checked_by": {"model": observed or self.info.route.model, "route": self.info.name, "effort": effort or ""},
            "prompt_sha256": [_sha256(item[1].prompt) for item in settled],
            "response_sha256": [record.get("response_sha256") for record in calls if record.get("response_sha256")],
            "checked_at": _utc_now(),
        }
        if len(verdict_parts) == total:
            merged = verdicts.merge_parts(verdict_parts)
            result = {**base, **merged.as_dict()}
            if not merged.interpreted:
                result["reason"] = "The verifier output could not be interpreted"
        else:
            reason = next((item for item in failures if item), "") or incomplete or "Not checked"
            if not reason.startswith("Not checked"):
                reason = f"Not checked: {reason}"
            result = {**base, "verdict": "not_checked", "reason": reason,
                      "verification_report": {"summary": "", "critical_errors": [], "gaps": []}}
        if calls:
            _write_json(self.analysis / "nodes" / f"{node['id']}.json", result)
        return result

    def _progress(self, node: Mapping[str, Any], result: Mapping[str, Any]) -> None:
        verdict = result.get("verdict")
        detail = ""
        report_block = result.get("verification_report") or {}
        first = (report_block.get("critical_errors") or report_block.get("gaps") or [None])[0]
        if first:
            issue = tex_text_refs(first.get("issue"), getattr(self, "progress_refs", None))
            detail = " — " + " ".join(issue.split())[:100]
        elif verdict == "not_checked":
            detail = " — " + str(result.get("reason") or "")[:100]
        self.note(f"Check · {prompts.node_label(node)} · {verdict}{detail}")

    # -- roll-up and publication ---------------------------------------------------

    def rollup(self, segments: Mapping[str, Any], manuscript: Mapping[str, Any],
               results: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        self.stage("curate")
        graph = analysis_graph.build(segments, manuscript, generated_at=_utc_now(),
                                     file_base_url=manuscript.get("file_base_url"))
        claims = {node["id"] for node in graph["nodes"] if node["kind"] in analysis_graph.CLAIM_KINDS}
        mains = [node_id for node_id in self.paper_map.get("main_results") or [] if node_id in claims]
        if mains:
            for node in graph["nodes"]:
                node["meta"]["main_result"] = node["id"] in mains
        analysis_graph.merge_annotations(graph, results)
        analysis_graph.rollup(graph)
        clean = analysis_graph.sanitize(graph)
        if not clean["ref_labels"]:
            # Segments parsed before sections kept their labels (a re-check of an older analysis).
            clean["ref_labels"] = analysis_graph.source_section_labels(self.workspace)
        closure = clean.get("main_closure")
        if clean.get("dependency_basis") == "model":
            self.note("Roll-up · dependencies not established (the model located the statements)")
        else:
            self.note("Roll-up · main results: " + (closure or "none identified"))
        return clean

    def receipt(self, segments: Mapping[str, Any], manuscript: Mapping[str, Any]) -> dict[str, Any]:
        calls = self.ctx.calls
        return {
            "schema": "ansatze-analyze-receipt-v1",
            "certificate": False,
            "lean_verified": False,
            "model": self.info.route.model,
            "provider": self.info.route.provider,
            "route": self.info.name,
            "transport": self.info.route.transport,
            "calls": len(calls),
            "call_log": [{key: record.get(key) for key in ("stage", "provider", "model", "usage", "response_sha256", "effort")}
                         for record in calls],
            "started_at": self.started_at,
            "finished_at": _utc_now(),
            "prompt_pack_sha256": prompts.PROMPT_PACK_SHA256,
            "parser": segments.get("parser"),
            "source_bundle_sha256": manuscript.get("bundle_sha256"),
            "caps": self.caps.as_dict(),
            "role": self.role,
            "stopped": self.stopped,
            "degraded": sorted(set(self.degraded)),
            "paper_map": self.paper_map or None,
        }

    def publish(self, graph: dict[str, Any], segments: Mapping[str, Any], manuscript: Mapping[str, Any],
                *, base_receipt: Mapping[str, Any] | None = None,
                recheck: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self.stage("finalize")
        receipt = self.receipt(segments, manuscript)
        if base_receipt is not None:
            # A re-check extends the original receipt: same start, all calls,
            # the run's own caps, role and limits; the re-check's caps and
            # limits are recorded with the re-check itself.
            entry = dict(recheck or {})
            entry["caps"] = receipt["caps"]
            if receipt["degraded"]:
                entry["degraded"] = receipt["degraded"]
            receipt["started_at"] = base_receipt.get("started_at") or receipt["started_at"]
            receipt["call_log"] = list(base_receipt.get("call_log") or []) + receipt["call_log"]
            receipt["calls"] = len(receipt["call_log"])
            for key in ("caps", "role"):
                if base_receipt.get(key):
                    receipt[key] = base_receipt[key]
            receipt["degraded"] = sorted(set(base_receipt.get("degraded") or []))
            receipt["stopped"] = bool(base_receipt.get("stopped"))
            receipt["paper_map"] = base_receipt.get("paper_map")
            receipt["rechecks"] = list(base_receipt.get("rechecks") or []) + ([entry] if recheck else [])
        text = report.render(graph, manuscript, receipt, paper_map=self.paper_map,
                             formalize=bool(self.req.get("formalize")))
        projection = analysis_graph.to_proof_graph(graph)
        _write_json(self.analysis / "analysis_graph.json", graph)
        _write_json(self.workspace / "proof_graph.json", projection)
        _write_bytes(self.analysis / "report.md", text.encode("utf-8"))
        _write_json(self.analysis / "receipt.json", receipt)
        written = ["analysis/analysis_graph.json", "proof_graph.json", "analysis/report.md", "analysis/receipt.json"]
        self.emit({"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": path} for path in written]}})
        summary = analysis_graph.summary(graph, calls=receipt["calls"], model=self.info.route.model, route=self.info.name)
        coverage = graph.get("coverage") or {}
        counts = graph.get("counts") or {}
        status = (f"checked {coverage.get('checked', 0)} of {coverage.get('total', 0)} results · "
                  f"{counts.get('error', 0)} error(s), {counts.get('gap', 0)} gap(s), {counts.get('unclear', 0)} unclear"
                  " · model verifier, not a certificate")
        self.emit({"type": "item.completed", "item": {"type": "agent_message",
                                                      "text": f"Ansatz Analyze completed · {status}\n\n{text[:6000]}"}})
        return {"summary": summary, "report_path": str(self.analysis / "report.md"), "receipt": receipt,
                "written": written, "stopped": self.stopped}


def prioritize(segments: Mapping[str, Any], *, focus: str = "",
           paper_map: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Checkable results by priority: the focus, the main results and their
    dependency closure (breadth first), then the closure of the terminal
    results (theorems, propositions and corollaries nothing else uses,
    latest first), then the rest in document order."""
    nodes = segments["nodes"]
    by_id = {node["id"]: node for node in nodes}
    checkable = [node for node in nodes if _checkable(node)]
    paper_map = paper_map or {}
    focus_ids = list(paper_map.get("focus_results") or [])
    for match in _FOCUS_RE.finditer(focus):
        for node in nodes:
            if (node.get("display") or "").lower() == match.group(1).lower() and node.get("number") == match.group(2):
                focus_ids.append(node["id"])
    for token in re.findall(r"[A-Za-z0-9:_.-]{3,80}", focus):
        for node in nodes:
            if token in (node.get("labels") or []) or token.lower() == node["id"]:
                focus_ids.append(node["id"])
    mains = list(paper_map.get("main_results") or []) or [node["id"] for node in nodes if node.get("main_result")]
    predecessors: dict[str, list[str]] = {}
    for edge in segments["edges"]:
        predecessors.setdefault(edge["to"], []).append(edge["from"])
    used = {edge["from"] for edge in segments["edges"] if not edge.get("weak")}
    rank = {"theorem": 0, "proposition": 1, "corollary": 2}
    terminal = sorted(
        (index, node["id"]) for index, node in enumerate(nodes)
        if node["id"] not in used and node.get("kind") in rank and _checkable(node)
    )
    terminal_ids = [node_id for _index, node_id in sorted(terminal, key=lambda item: (rank[by_id[item[1]]["kind"]], -item[0]))]
    ordered: list[str] = []
    seen: set[str] = set()
    # Two breadth-first passes: the focus and main results, then every
    # terminal result at once (shallow coverage of each chain before depth).
    for roots in (list(dict.fromkeys(focus_ids + mains)), terminal_ids):
        queue = list(roots)
        while queue:
            current = queue.pop(0)
            if current in seen or current not in by_id:
                continue
            seen.add(current)
            ordered.append(current)
            queue.extend(sorted(predecessors.get(current, [])))
    ordered += [node["id"] for node in nodes if node["id"] not in seen]
    allowed = {node["id"] for node in checkable}
    return [by_id[node_id] for node_id in ordered if node_id in allowed]


def _read_capped(path: Path) -> str:
    """At most the text segmentation cap of a file, decoded leniently."""
    with open(path, "rb") as handle:
        return handle.read(text_structure.MAX_TEXT_CHARS * 4).decode("utf-8", "replace")


def missing_packages() -> list[str]:
    """Parser packages Analyze needs that this interpreter cannot import."""
    return [name for name in ("pylatexenc", "pypdf") if importlib.util.find_spec(name) is None]


def _checkable(node: Mapping[str, Any]) -> bool:
    return (str(node.get("kind")) in analysis_graph.CLAIM_KINDS and bool(node.get("has_proof"))
            and not node.get("external"))


def _not_checked(node: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {"id": node["id"], "verdict": "not_checked", "reason": reason,
            "verification_report": {"summary": "", "critical_errors": [], "gaps": []}}


def run_analysis(
    workspace: Path,
    req: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    sink: Callable[[dict[str, Any]], None] | None = None,
    fetcher: safe_fetch.SafeFetcher | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Run one analysis in ``workspace`` (see the module docstring).

    Raises :class:`ConfigError`, :class:`AnalyzeError` or :class:`Stopped`;
    a SIGTERM after segmentation publishes what has settled and returns with
    ``stopped: True``.
    """
    missing = missing_packages()
    if missing:
        raise ConfigError(
            "Analyze is not fully installed on this server (missing Python package: " + ", ".join(missing)
            + "); please tell the operator")
    run = AnalysisRun(workspace, req, env=env if env is not None else os.environ, sink=sink,
                      fetcher=fetcher, sleep=sleep)
    token = harness._RUN_EMITTER.set(run.emitter)
    try:
        with harness._stop_on_sigterm(run.cancel), analysis_lock(run.analysis):
            run.emit({"type": "thread.started", "thread_id": "analyze"})
            run.emit({"type": "item.completed", "item": {"type": "agent_message", "text": (
                f"Ansatz Analyze · {run.info.route.model} via {run.info.name} · up to {run.caps.max_nodes} "
                f"results · {run.caps.max_calls} calls · {run.caps.concurrency} in flight · no tools")}})
            try:
                bundle = run.ingest()
                segments, manuscript = run.segment(bundle)
            except harness.RunCancelled:
                raise Stopped("The analysis was stopped before any result was checked; nothing was published") from None
            try:
                results = run.check(segments)
            except harness.RunCancelled:
                run.stopped = True
                results = {}
            graph = run.rollup(segments, manuscript, results)
            outcome = run.publish(graph, segments, manuscript)
            judged = [result for result in results.values() if result.get("verdict") != "not_checked"]
            if run.attempted and not run.stopped and not any(result.get("interpreted", True) for result in judged):
                # Every attempted check failed (or no verifier output could be
                # interpreted): the structure is published, the run is not a success.
                if run.provider_failure:
                    reason = run.provider_failure
                elif judged:
                    reason = "the verifier output could not be interpreted"
                else:
                    reason = next((str(result.get("reason")).removeprefix("Not checked: ")
                                   for result in results.values() if result.get("reason")), "every check failed")
                raise AnalyzeError(f"No result could be checked: {reason}")
            return outcome
    finally:
        run.emitter.close()
        harness._RUN_EMITTER.reset(token)


def public_message(text: str, env: Mapping[str, str]) -> str:
    """``text`` without any credential value from ``env`` (keys, tokens)."""
    value = " ".join(str(text or "").split())
    for name, secret in env.items():
        upper = name.upper()
        if secret and len(secret) >= 8 and upper.endswith(("_API_KEY", "_API_KEYS", "_TOKEN", "_AUTH_TOKEN", "_SECRET")):
            value = value.replace(secret, "[redacted]")
    return value[:600]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ansatz Analyze runner")
    parser.add_argument("--recheck", metavar="NODE_ID")
    args = parser.parse_args(argv)
    workspace = Path.cwd()
    env = dict(os.environ)
    # Space arXiv requests across every runner and the console, not per process.
    sources.ARXIV_SLOT_PATH = "auto"
    try:
        req = analysis_request.load(workspace)
    except analysis_request.RequestError as exc:
        harness.emit_item({"type": "error", "message": str(exc), "terminal": True})
        return 2
    try:
        if args.recheck:
            from agent_monitor.analyze import recheck

            result = recheck.run(workspace, args.recheck, route_env=env, process_env=True)
            harness.emit({"type": "analyze.recheck", "result": result})
            return 0
        # Kimi routes must not see ambient proxies (urllib reads os.environ).
        info_env = dict(env)
        configure_route(info_env, req)
        for name in _DORMANT_ENV:
            if name not in info_env:
                os.environ.pop(name, None)
        if info_env.get("NO_PROXY") == "*":
            os.environ["NO_PROXY"] = os.environ["no_proxy"] = "*"
        outcome = run_analysis(workspace, req, env=env)
    except AnalyzeError as exc:  # ConfigError (2) and Stopped (143) included
        harness.emit_item({"type": "error", "message": public_message(str(exc), env), "terminal": True})
        return exc.exit_code
    except harness.MathHarnessError as exc:
        harness.emit_item({"type": "error", "message": public_message(f"Analyze failed: {exc}", env),
                           "terminal": True})
        return 1
    except (OSError, ValueError) as exc:
        harness.emit_item({"type": "error", "message": f"Analyze failed closed ({type(exc).__name__})", "terminal": True})
        return 1
    if outcome.get("stopped"):
        harness.emit_item({"type": "error", "message": "The analysis was stopped; the results that had settled were published",
                           "terminal": True})
        return 128 + int(signal.SIGTERM)
    return 0


if __name__ == "__main__":
    # Run as the importable module: agent_monitor.analyze.recheck imports it,
    # and its exceptions must be the classes main() catches.
    from agent_monitor.runners import analyze_runner as _module

    sys.exit(_module.main())
