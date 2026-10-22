#!/usr/bin/env python3
"""Ansatz Readability: rewrite a manuscript so that a reader can follow it.

Run from a run workspace that holds ``readability/request.json`` (written by
``jobs.start_job`` with :mod:`agent_monitor.readability.request`)::

    python -u agent_monitor/runners/readability_runner.py             # a readability run
    python -u agent_monitor/runners/readability_runner.py --ask <id>  # one question about it

Stages (``pipeline.stage`` events, ids shared with the CGM pipeline):
``scaffold`` 1 Ingest (fetch or unpack the source into ``readability/source``,
segment it as Analyze does, which for Markdown and text also reads a CGM
fact closure and result headings (``readability.structure``), read it in
order and split it into parts), ``work`` 2 Guide (one call: what the
manuscript shows, its roadmap, notation and the role of each result; short
and at low effort on bounded routes), ``verify`` 3 Rewrite (one call per
part, in waves, with the printed names of the labels it cites), ``curate`` 4
Check (one independent call per rewritten part; outside guest and sponsored
runs, a part with major problems is rewritten once more and checked again)
and ``finalize`` 5 Publish (leftover LaTeX references written out, each
part's headings under the title's).  Outputs, under ``readability/``:
``original.txt`` (the manuscript in reading order), ``manuscript.json``,
``segments.json``, ``readable.md``, ``paper_map.json``, ``notation.json``,
``guide.json``, ``check.json`` and ``receipt.json`` (written last; its
``notes`` say why a stage's first reply could not be read).  Every check is a
model's judgement, never a certificate.

The calls reuse the CGM response-only turn machinery of
``math_harness_runner`` and the routes of ``analyze_runner`` (which also
provides the ingest and the sandboxed segmentation), except that an API key
runs Kimi K3 only: sponsored Kimi K3 through the loopback broker, an own Kimi
key on the operator's endpoint, or an administrator's Codex or Claude Code
account (response-only, tool-free, one call in flight); Max is refused.
Nothing from the manuscript is compiled or executed.

The plan keeps one check for every part it rewrites: parts beyond the run's
part cap or its call budget are not rewritten, and ``readable.md`` shows them
in their original form, marked; nothing is dropped silently.  A guide that
fails is replaced by a deterministic outline; the rewrite never depends on
it.  A rewrite that keeps everything but an anchor (``[[result:<id>]]``) of a
result is accepted after its format retry, with a coverage issue.

Exit status: 0 published, 2 configuration (request, route or credentials;
nothing was sent to a model), 143 stopped by SIGTERM (what had settled is
published), 1 any other failure (including when no part could be rewritten;
what exists is still published).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Callable, Mapping

if not __package__:
    # The registry launches this file by absolute path from the run workspace.
    repository_root = Path(__file__).resolve().parents[2]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))

from agent_monitor import safe_fetch
from agent_monitor.analyze import latex_structure, sources, text_structure
from agent_monitor.analyze.prompts import utf8_len
from agent_monitor.cgm.parsing import ParseError
from agent_monitor.readability import ask as readability_ask
from agent_monitor.readability import document as readability_document
from agent_monitor.readability import limits as readability_limits
from agent_monitor.readability import paper_map, parse, prompts, references, report
from agent_monitor.readability import request as readability_request
from agent_monitor.readability import structure as readability_structure
from agent_monitor.runners import analyze_runner as runner
from agent_monitor.runners import math_harness_runner as harness
from agent_monitor.runners.analyze_runner import AnalyzeError, ConfigError, Stopped

STAGES = {"scaffold": "1 Ingest", "work": "2 Guide", "verify": "3 Rewrite", "curate": "4 Check", "finalize": "5 Publish"}
LABEL = "Readability"
ROUTES = runner.RouteRules(mode="Readability", run="readability run", runs="readability runs",
                           model_env="AGENT_MONITOR_READABILITY_MODEL", kimi_only=True)
RECEIPT_SCHEMA = "ansatze-readability-receipt-v1"
CHECK_SCHEMA = "ansatze-readability-check-v1"
GUIDE_SCHEMA = "ansatze-readability-guide-v1"
ANSWER_SCHEMA = "ansatze-readability-answer-v1"
MANUSCRIPT_SCHEMA = "ansatze-readability-manuscript-v1"
# One question: the call, its low-effort retry and a format reminder.
ASK_MAX_CALLS = 4
ASK_SECONDS = 900
# A rewrite on a Claude Code account is a long reply (the "rewrite" stage
# family has no timeout of its own in the harness; repairs are "revision").
REWRITE_CLAUDE_TIMEOUT = "1200"
_CLAUDE_TIMEOUT_ENV = ("AGENT_MONITOR_MATH_HARNESS_CLAUDE_REWRITE_TIMEOUT",
                       "AGENT_MONITOR_MATH_HARNESS_CLAUDE_TIMEOUT", "AGENT_MONITOR_CLAUDE_TIMEOUT")
_LOCK_BUSY = "Another readability step is running for this run; try again when it finishes"
# A retried call's stage ("rewrite-2-retry", "guide-low") and the stage it retries.
_RETRY_SUFFIX_RE = re.compile(r"(?:-retry)?(?:-low)?$")
MAX_NOTES = 40


def _json(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _text_file(path: Path, limit: int = 8 * 1024 * 1024) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            return ""
        with open(path, "rb") as handle:
            return handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


@dataclass
class _Outcome:
    """What happened to one part."""

    part: readability_document.Part
    status: str = "not-rewritten"
    reason: str = ""
    markdown: str = ""
    missing: list[str] = field(default_factory=list)
    verdict: str = ""
    summary: str = ""
    issues: list[dict[str, Any]] = field(default_factory=list)
    repaired: bool = False


class _Logged:
    """A reply parser that keeps, per stage, why the first reply it refused
    could not be read (the parser's own message, bounded) and which reply
    that was (its sha256, as the call log has it), so the receipt says why a
    format reminder was sent.  A rewrite's ``lenient`` result stays
    reachable."""

    def __init__(self, parse_reply: Callable[[str], Any], stage: str,
                 errors: dict[str, tuple[str, str]]) -> None:
        self.parse_reply = parse_reply
        self.stage = stage
        self.errors = errors

    def __call__(self, text: str) -> Any:
        try:
            return self.parse_reply(text)
        except ParseError as exc:
            message = "".join(char for char in " ".join(str(exc).split()) if char.isprintable())
            self.errors.setdefault(self.stage, (hashlib.sha256(text.encode("utf-8")).hexdigest(),
                                                message if len(message) <= 120 else message[:119] + "…"))
            raise

    @property
    def lenient(self) -> Any:
        return getattr(self.parse_reply, "lenient", None)


@dataclass
class _Call:
    """One model call of a wave and what came of it."""

    outcome: _Outcome
    stage: str
    system: str
    user: str
    parse: Callable[[str], Any]
    family: str
    limit: int
    result: Any = None
    error: str = ""
    # The call was answered and failed (as opposed to never sent, or cut
    # short by the time limit or a stop).
    failed: bool = False
    job: Any = None


class ReadabilityRun(runner.ManuscriptRun):
    """One readability run (or one question) of a run workspace."""

    LABEL = LABEL
    STAGES = STAGES
    FOLDER = "readability"
    ROUTES = ROUTES
    LIMITS = readability_limits
    API_TIMEOUT_ENV = "AGENT_MONITOR_READABILITY_API_TIMEOUT"
    STALE_DIRS = ("source",)
    STALE_FILES = ("manuscript.json", "segments.json", "pdf_text.txt", "original.txt", "readable.md",
                   "paper_map.json", "notation.json", "guide.json", "check.json", "receipt.json")
    # Segmentation belongs to Ingest; the model locates nothing (the calls
    # are kept for the rewrite), and a manuscript without numbered results
    # is still rewritten.
    SEGMENT_STAGE = None
    MODEL_SEGMENTS = False
    REQUIRE_RESULTS = False

    def __init__(self, workspace: Path, req: Mapping[str, Any], *, env: Mapping[str, str], **kwargs: Any) -> None:
        env = dict(env)
        if not any(str(env.get(name) or "").strip() for name in _CLAUDE_TIMEOUT_ENV):
            env[_CLAUDE_TIMEOUT_ENV[0]] = REWRITE_CLAUDE_TIMEOUT
        super().__init__(workspace, req, env=env, **kwargs)
        bounded = self.info.bounded
        self.part_chars = prompts.BOUNDED_PART_CHARS if bounded else prompts.PART_CHARS
        self.guide_bytes = prompts.BOUNDED_GUIDE_BYTES if bounded else prompts.GUIDE_BYTES
        self.summary_bytes = prompts.BOUNDED_GUIDE_SUMMARY_BYTES if bounded else prompts.GUIDE_SUMMARY_BYTES
        self.references_bytes = prompts.BOUNDED_REFERENCES_BYTES if bounded else prompts.REFERENCES_BYTES
        self.audience = str(self.req.get("audience") or readability_request.DEFAULT_AUDIENCE)
        self.focus = str(self.req.get("focus") or "")
        self.guide: dict[str, Any] = {}
        self.guide_source = "outline"
        # Per stage, the first reply its parser refused and why (receipt notes).
        self.format_errors: dict[str, tuple[str, str]] = {}

    # -- 1 Ingest -------------------------------------------------------------

    def segment_text_file(self, path: Path, file: str, fmt: str) -> dict[str, Any]:
        """Analyze's text segmentation, which also reads a CGM fact closure
        and Markdown result headings (``readability.structure``), in the same
        sandboxed child."""
        try:
            return readability_structure.segment_sandboxed(path, self.folder / ".segments-text.json", file=file,
                                                           fmt=fmt, cancel=self.cancel)
        except latex_structure.SandboxCancelled:
            raise harness.RunCancelled(f"the {self.ROUTES.run} was stopped") from None
        except text_structure.TextStructureError as exc:
            raise AnalyzeError(f"The source could not be segmented: {exc}") from None

    def read_document(self, bundle: sources.Bundle, segments: Mapping[str, Any],
                      manuscript: dict[str, Any]) -> readability_document.Document:
        """The manuscript in reading order; writes ``original.txt`` and
        completes ``manuscript.json``."""
        fmt = str(segments.get("format") or "")
        root_file = str(segments.get("root_file") or "")
        try:
            if fmt == "tex":
                document = readability_document.tex_document(bundle.root, root_file or None)
            elif fmt == "pdf":
                document = readability_document.pdf_document(self.folder / "pdf_text.txt", file=root_file)
            elif fmt in ("md", "txt") and root_file:
                document = readability_document.text_document(bundle.root / root_file, file=root_file, fmt=fmt)
            else:
                raise readability_document.DocumentError("The source has no TeX, Markdown, text or PDF file to read")
        except (readability_document.DocumentError, OSError) as exc:
            message = str(exc) if isinstance(exc, readability_document.DocumentError) else type(exc).__name__
            raise AnalyzeError(f"The manuscript could not be read: {message}") from None
        if not document.text.strip():
            raise AnalyzeError("The manuscript has no text to make readable")
        # A result outside the text that was read is never rewritten: say so.
        unread = [prompts.printed(node) for node in segments.get("nodes") or [] if document.locate(node) is None]
        if unread:
            document.warnings.append("Results outside the text that was read, so not rewritten: "
                                     + ", ".join(dict.fromkeys(unread[:12])))
        runner._write_bytes(self.folder / "original.txt", document.text.encode("utf-8"))
        manuscript.update({
            "schema": MANUSCRIPT_SCHEMA,
            "bytes": len(document.text.encode("utf-8")),
            "macros": dict(list((segments.get("macros") or {}).items())[:paper_map.MAX_MACROS]),
            "warnings": list(dict.fromkeys(list(manuscript.get("warnings") or []) + document.warnings))[:20],
        })
        runner._write_json(self.folder / "manuscript.json", manuscript)
        return document

    def plan(self, document: readability_document.Document,
             segments: Mapping[str, Any]) -> list[readability_document.Part]:
        parts = readability_document.split_parts(document, segments.get("nodes") or [], target=self.part_chars)
        located = sum(len(part.nodes) for part in parts)
        self.note(f"Ingest · reading order: {len(parts)} part{'s' if len(parts) != 1 else ''}, "
                  f"{len(document.text):,} characters, {located} results placed")
        return parts

    # -- 2 Guide ----------------------------------------------------------------

    def run_guide(self, segments: Mapping[str, Any], manuscript: Mapping[str, Any],
                  document: readability_document.Document, parts: list[readability_document.Part],
                  *, planned: int) -> None:
        """The reading guide, only when ``planned`` calls (the planned
        rewrites and checks) stay available after it; otherwise, or when it
        fails, a deterministic outline."""
        nodes = segments.get("nodes") or []
        self.stage("work", f"reading guide for {len(nodes)} result{'s' if len(nodes) != 1 else ''}")
        self.guide = outline_guide(nodes, title=str(manuscript.get("title") or ""))
        if self.ctx.remaining() < planned + 1:
            self.degraded.append("guide-skipped-for-budget")
            self.note("Guide · skipped to keep the model calls for the rewrite; using the manuscript's outline")
            return
        results_limit, manuscript_limit = self.guide_bytes
        text = document.text
        if utf8_len(text) > manuscript_limit:
            headings = [" ".join(f"{section.get('number') or ''} {section.get('title') or ''}".split())
                        for section in segments.get("sections") or []] or [part.title for part in parts]
            text = prompts.manuscript_outline(title=str(manuscript.get("title") or ""),
                                              abstract=str(segments.get("abstract") or ""),
                                              intro=str(segments.get("intro") or ""), headings=headings[:80],
                                              limit=manuscript_limit)
        bounded = self.info.bounded
        user = prompts.guide_prompt(audience=self.audience, focus=self.focus,
                                    results=prompts.results_list(nodes, results_limit), manuscript=text,
                                    bounded=bounded)
        ids = [node["id"] for node in nodes]
        try:
            job = harness._run_single(
                self.ctx, "guide", reserve=planned,
                work=lambda turn: harness._call_and_parse(
                    self.ctx, turn, stage="guide", system=prompts.READABLE_GUIDE_SYSTEM, user=user,
                    limit=prompts.GUIDE_REPLY_BYTES,
                    parse=_Logged(partial(parse.parse_guide, node_ids=ids), "guide", self.format_errors),
                    family=prompts.BOUNDED_GUIDE_FAMILY if bounded else prompts.GUIDE_FAMILY))
            guide, _reply = harness._job_result(job)
        except harness.RunCancelled:
            raise
        except harness.ProviderCallError as exc:
            self._note_failure(exc)
            self.degraded.append("guide-failed")
        except (harness._BudgetExhausted, harness.MathHarnessError, ParseError):
            self.degraded.append("guide-failed")
        else:
            self.guide = {**guide, "title": guide.get("title") or self.guide["title"]}
            self.guide_source = "model"
            self.note(f"Guide · {guide['one_line']}"[:600])
            return
        self.note("Guide · not available; using the manuscript's outline")

    def guide_text(self, macros: Mapping[str, str], part_text: str) -> str:
        """The guide as a rewrite sees it, with the author's macros the part uses."""
        text = prompts.guide_summary(self.guide, self.summary_bytes)
        used = [f"{name} := {body}" for name, body in list(macros.items())[:paper_map.MAX_MACROS]
                if re.search(re.escape(name) + r"(?![A-Za-z])", part_text)]
        if used:
            text += "\n\nAuthor macros used in this part (keep using them):\n" + prompts.fence("\n".join(used[:60]))
        return text

    # -- waves of calls --------------------------------------------------------

    def _halt(self) -> str:
        """Why no further call may start, or ``""``."""
        if self.cancel.is_set():
            self.stopped = True
            return self._stop_reason()
        if self.ctx.deadline_passed():
            return self._stop_reason()
        if self.permanent_failure:
            return self.provider_failure or "the model provider refused the run"
        return ""

    def run_calls(self, calls: list[_Call], *, follow_up: int, doing: str) -> None:
        """Send ``calls`` in waves of ``ctx.concurrency`` (document order).
        Every call keeps ``follow_up`` calls for what must follow it (a
        rewrite its check) and one for each call still waiting; when retries
        have used too much, the last calls wait for good.  A call that cannot
        be sent gets the reason.  On a stop, the calls of the wave that had
        already finished keep their results (they are published)."""
        pending = list(calls)
        reserve = follow_up * len(calls)
        reason = ""
        while pending:
            reason = self._halt()
            if reason:
                break
            while pending and self.ctx.remaining() - reserve - len(pending) < 0:
                pending.pop().error = self._stop_reason()
                reserve -= follow_up
            if not pending:
                break
            wave = pending[: self.ctx.concurrency]
            spare = self.ctx.remaining() - reserve - len(pending)
            started: list[_Call] = []
            for call, limit in zip(wave, harness._turn_limits(len(wave), spare)):
                try:
                    turn = self.ctx.open_turn(call.stage, limit)
                except harness.RunCancelled:
                    self.stopped = True
                    break
                except harness._BudgetExhausted:
                    break
                call.job = harness._TurnJob(turn, partial(
                    harness._call_and_parse, self.ctx, turn, stage=call.stage, system=call.system,
                    user=call.user, limit=call.limit, parse=call.parse, family=call.family))
                started.append(call)
            if not started:
                reason = self._stop_reason()
                break
            pending = pending[len(started):]
            self.note(f"{doing} · now working on " + ", ".join(_name(call.outcome.part) for call in started))
            try:
                harness._run_turns(self.ctx, [call.job for call in started])
            except harness.RunCancelled:
                # The unfinished calls were abandoned; the finished ones settle.
                self.stopped = True
                reason = self._stop_reason()
            for call in started:
                self._settle(call)
                if call.result is None:
                    reserve -= follow_up  # nothing to follow up
            if reason:
                break
        for call in pending:
            call.error = call.error or reason or self._stop_reason()

    def _settle(self, call: _Call) -> None:
        turn_job = call.job
        error = turn_job.error
        if turn_job.abandoned or isinstance(error, (harness._BudgetExhausted, harness._TurnAbandoned)):
            call.error = self._stop_reason()
            return
        if error is None:
            call.result, _reply = turn_job.outcome
            return
        if isinstance(error, ParseError) and getattr(call.parse, "lenient", None) is not None:
            # Everything but an anchor was kept: accepted, with a coverage issue.
            call.result = call.parse.lenient
            return
        if isinstance(error, harness.RunCancelled) or (
                isinstance(error, harness.ProviderCallError) and (self.cancel.is_set() or self.ctx.deadline_passed())):
            # Cut short by the run's own deadline or a stop, not refused.
            call.error = self._stop_reason()
            return
        call.failed = True
        if isinstance(error, ParseError):
            call.error = f"the model's reply could not be read ({error})"[:400]
        elif isinstance(error, harness.PromptBoundError):
            call.error = "the part is too long for one model call"
        elif isinstance(error, harness.EmptyReplyError):
            low = ", even at low effort" if harness._is_kimi_route(self.info.route) else ""
            call.error = f"the model returned an empty answer{low}"
        elif isinstance(error, harness.ProviderCallError):
            call.error = self._note_failure(error)
        else:
            call.error = "the model call failed"

    # -- 3 Rewrite --------------------------------------------------------------

    def rewrite_call(self, outcome: _Outcome, *, document: readability_document.Document,
                     by_id: Mapping[str, Mapping[str, Any]], macros: Mapping[str, str], total: int,
                     math_fidelity: str | None, refs: Mapping[str, tuple[str, str]]) -> _Call:
        part = outcome.part
        text = part.text(document)
        stage = f"rewrite-{part.index}"
        user = prompts.rewrite_prompt(audience=self.audience, focus=self.focus,
                                      guide=self.guide_text(macros, text),
                                      results=[by_id[node_id] for node_id in part.nodes if node_id in by_id],
                                      part=text, index=part.index, total=total, math_fidelity=math_fidelity,
                                      references=references.prompt_block(refs, text, self.references_bytes))
        return _Call(outcome, stage, prompts.READABLE_REWRITE_SYSTEM, user,
                     _Logged(parse.RewriteParser(part.nodes), stage, self.format_errors), prompts.REWRITE_FAMILY,
                     prompts.REWRITE_REPLY_BYTES)

    def rewrite(self, outcomes: list[_Outcome], calls: list[_Call], *, total: int) -> None:
        self.stage("verify", f"rewriting {len(calls)} of {total} part{'s' if total != 1 else ''}")
        # Every part rewritten keeps one call for its check.
        self.run_calls(calls, follow_up=1, doing="Rewrite")
        for call in calls:
            outcome = call.outcome
            if call.result is None:
                outcome.status = "not-rewritten"
                outcome.reason = f"the rewrite failed: {call.error}" if call.failed else call.error
                self.note(f"Rewrite · {_name(outcome.part)} · not rewritten — {outcome.reason}"[:600])
                continue
            outcome.status, outcome.reason = "unchecked", ""
            outcome.markdown, outcome.missing = call.result["markdown"], list(call.result["missing"])
            missing = f" (no anchor for {len(outcome.missing)} result(s))" if outcome.missing else ""
            self.note(f"Rewrite · {_name(outcome.part)} · rewritten{missing}")

    # -- 4 Check ----------------------------------------------------------------

    def check_call(self, outcome: _Outcome, *, document: readability_document.Document, total: int,
                   math_fidelity: str | None, recheck: bool = False) -> _Call | None:
        user = prompts.check_prompt(original=outcome.part.text(document), rewritten=outcome.markdown,
                                    index=outcome.part.index, total=total, math_fidelity=math_fidelity)
        if utf8_len(user) > prompts.MAX_PROMPT_BYTES:
            return None
        stage = f"{'recheck' if recheck else 'check'}-{outcome.part.index}"
        return _Call(outcome, stage, prompts.READABLE_CHECK_SYSTEM, user,
                     _Logged(parse.parse_check, stage, self.format_errors), prompts.CHECK_FAMILY,
                     prompts.CHECK_REPLY_BYTES)

    def check(self, outcomes: list[_Outcome], *, document: readability_document.Document, total: int,
              math_fidelity: str | None) -> None:
        rewritten = [outcome for outcome in outcomes if outcome.status == "unchecked"]
        self.stage("curate", f"checking {len(rewritten)} rewritten part{'s' if len(rewritten) != 1 else ''}")
        calls = []
        for outcome in rewritten:
            call = self.check_call(outcome, document=document, total=total, math_fidelity=math_fidelity)
            if call is None:
                outcome.reason = "the part and its rewrite are too long to check in one call"
            else:
                calls.append(call)
        self.run_calls(calls, follow_up=0, doing="Check")
        for call in calls:
            self._judge(call.outcome, call)

    def _judge(self, outcome: _Outcome, call: _Call) -> None:
        if call.result is None:
            outcome.reason = f"the check failed: {call.error}" if call.failed else call.error
            self.note(f"Check · {_name(outcome.part)} · not checked — {outcome.reason}"[:600])
            return
        outcome.verdict, outcome.summary = call.result["verdict"], call.result["summary"]
        outcome.issues = list(call.result["issues"])
        outcome.status = "faithful" if outcome.verdict == "faithful" and not outcome.missing else "issues"
        major = [issue for issue in outcome.issues if issue["severity"] == "major"]
        detail = (f" — {len(major)} major issue(s): {' '.join(major[0]['problem'].split())[:120]}" if major
                  else " — a result has no anchor" if outcome.missing else "")
        self.note(f"Check · {_name(outcome.part)} · {outcome.status}{detail}")

    def repair(self, outcomes: list[_Outcome], rewrite_calls: Mapping[int, _Call], *,
               document: readability_document.Document, total: int, math_fidelity: str | None) -> None:
        """One more rewrite of each part whose check found major problems,
        checked again; the repair is kept only with a verdict of its own."""
        if self.info.bounded or self.stopped:
            return
        wanted = [outcome for outcome in outcomes if outcome.verdict == "unfaithful"]
        wanted = wanted[: max(0, self.ctx.remaining() // 2)]
        if not wanted:
            return
        repairs = []
        for outcome in wanted:
            first = rewrite_calls[outcome.part.index]
            user = prompts.repair_prompt(rewrite=first.user, previous=outcome.markdown,
                                         issues=[issue for issue in outcome.issues if issue["severity"] == "major"])
            stage = f"repair-{outcome.part.index}"
            repairs.append(_Call(_Outcome(outcome.part), stage, prompts.READABLE_REWRITE_SYSTEM, user,
                                 _Logged(parse.RewriteParser(outcome.part.nodes), stage, self.format_errors),
                                 prompts.REPAIR_FAMILY, prompts.REWRITE_REPLY_BYTES))
        self.run_calls(repairs, follow_up=1, doing="Repair")
        rechecks = []
        for call in repairs:
            if call.result is None:
                continue
            call.outcome.markdown, call.outcome.missing = call.result["markdown"], list(call.result["missing"])
            recheck = self.check_call(call.outcome, document=document, total=total, math_fidelity=math_fidelity,
                                      recheck=True)
            if recheck is not None:
                rechecks.append(recheck)
        self.run_calls(rechecks, follow_up=0, doing="Check")
        by_index = {outcome.part.index: outcome for outcome in wanted}
        for call in rechecks:
            if call.result is None:
                self.note(f"Check · {_name(call.outcome.part)} · the repair could not be checked; the first rewrite is kept")
                continue
            kept = by_index[call.outcome.part.index]
            kept.markdown, kept.missing, kept.repaired = call.outcome.markdown, call.outcome.missing, True
            self._judge(kept, call)

    # -- 5 Publish ---------------------------------------------------------------

    def counts(self, outcomes: list[_Outcome]) -> dict[str, int]:
        """The parts meant to be rewritten, by status; the references, kept
        as in the original by design, are counted apart."""
        statuses = [outcome.status for outcome in outcomes if outcome.part.kind == "text"]
        return {
            "total": len(statuses),
            "rewritten": sum(1 for status in statuses if status != "not-rewritten"),
            "faithful": statuses.count("faithful"),
            "issues": statuses.count("issues"),
            "unchecked": statuses.count("unchecked"),
            "not_rewritten": statuses.count("not-rewritten"),
            "kept": len(outcomes) - len(statuses),
        }

    def notes(self) -> list[str]:
        """Why the first reply of a stage could not be read, in the order of
        the calls: the parser's message when its parser refused that reply,
        or, when a format reminder followed a reply refused before it was
        parsed (too long, or tool-call markup), that it was."""
        first: dict[str, str | None] = {}  # the reply each stage read first
        reminded: set[str] = set()
        for record in self.ctx.calls:
            stage = str(record.get("stage") or "")
            base = _RETRY_SUFFIX_RE.sub("", stage)
            if "-retry" in stage[len(base):]:
                reminded.add(base)
            else:
                # After an empty reply, its low-effort retry's is the one read.
                first[base] = record.get("response_sha256")
        notes = []
        for base, reply in first.items():
            logged = self.format_errors.get(base)
            if logged is not None and logged[0] == reply:
                notes.append(f"{base}: {logged[1]}")
            elif base in reminded:
                notes.append(f"{base}: the reply was refused before it was read (too long, or tool-call markup)")
        return notes[:MAX_NOTES]

    def receipt(self, segments: Mapping[str, Any], manuscript: Mapping[str, Any],
                counts: Mapping[str, int]) -> dict[str, Any]:
        calls = self.ctx.calls
        return {
            "schema": RECEIPT_SCHEMA,
            "certificate": False,
            "model": self.info.route.model,
            "provider": self.info.route.provider,
            "route": self.info.name,
            "transport": self.info.route.transport,
            "calls": len(calls),
            "call_log": [{key: record.get(key) for key in ("stage", "provider", "model", "usage", "response_sha256", "effort")}
                         for record in calls],
            "started_at": self.started_at,
            "finished_at": runner._utc_now(),
            "prompt_pack_sha256": prompts.PROMPT_PACK_SHA256,
            "parser": segments.get("parser"),
            "source_bundle_sha256": manuscript.get("bundle_sha256"),
            "caps": self.caps.as_dict(),
            "role": self.role,
            "audience": self.audience,
            "guide": self.guide_source,
            "stopped": self.stopped,
            "degraded": sorted(set(self.degraded)),
            "notes": self.notes(),
            "parts": dict(counts),
        }

    def publish(self, outcomes: list[_Outcome], *, document: readability_document.Document,
                segments: Mapping[str, Any], manuscript: Mapping[str, Any]) -> dict[str, Any]:
        self.stage("finalize")
        nodes = segments.get("nodes") or []
        labels = {node["id"]: prompts.printed(node) for node in nodes}
        refs = references.label_map(segments)
        title = str(manuscript.get("title") or self.guide.get("title") or "")
        counts = self.counts(outcomes)
        model_guide = self.guide if self.guide_source == "model" else None
        part_of = {node_id: outcome.part.index for outcome in outcomes for node_id in outcome.part.nodes}
        parts = [{
            "index": outcome.part.index,
            "title": outcome.part.title,
            "kind": outcome.part.kind,
            "lines": [outcome.part.start + 1, outcome.part.end],
            "chars": outcome.part.chars(document),
            "nodes": list(outcome.part.nodes),
            "status": outcome.status,
            "reason": outcome.reason,
            "verdict": outcome.verdict,
            "summary": outcome.summary,
            "issues": outcome.issues,
            "missing": outcome.missing,
            "repaired": outcome.repaired,
            "markdown": outcome.markdown,
        } for outcome in outcomes]
        text = report.render(title=title, model=self.info.route.model, route=self.info.name,
                             audience=self.audience, guide=self.guide, parts=parts,
                             originals={outcome.part.index: outcome.part.text(document) for outcome in outcomes
                                        if outcome.status == "not-rewritten"},
                             labels=labels, counts=counts, math_fidelity=segments.get("math_fidelity"),
                             references=refs)
        written = {
            "readability/readable.md": text.encode("utf-8"),
            "readability/paper_map.json": paper_map.build(segments, title=title, parts=part_of, guide=model_guide),
            "readability/notation.json": paper_map.notation(model_guide, segments.get("macros") or {}, document.text),
            "readability/guide.json": {"schema": GUIDE_SCHEMA, "source": self.guide_source, **self.guide},
            "readability/check.json": {"schema": CHECK_SCHEMA, "counts": counts,
                                       "parts": [{key: value for key, value in part.items() if key != "markdown"}
                                                 for part in parts]},
        }
        receipt = self.receipt(segments, manuscript, counts)
        for path, value in written.items():
            target = self.workspace / path
            if isinstance(value, bytes):
                runner._write_bytes(target, value)
            else:
                runner._write_json(target, value)
        runner._write_json(self.folder / "receipt.json", receipt)
        paths = [*written, "readability/receipt.json"]
        self.emit({"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": path} for path in paths]}})
        glance = report.at_a_glance(self.guide, labels, refs)
        status = (f"{counts['rewritten']} of {counts['total']} parts rewritten · {counts['faithful']} faithful, "
                  f"{counts['issues']} with issues, {counts['unchecked']} unchecked · checked by a model, not a certificate")
        self.emit({"type": "item.completed", "item": {
            "type": "agent_message", "text": f"Ansatz Readability completed · {status}" + (f"\n\n{glance[:6000]}" if glance else "")}})
        return {"counts": counts, "receipt": receipt, "written": paths, "stopped": self.stopped,
                "readable_path": str(self.folder / "readable.md")}

    # -- the run -------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        try:
            bundle = self.ingest()
            segments, manuscript = self.segment(bundle)
            document = self.read_document(bundle, segments, manuscript)
            parts = self.plan(document, segments)
        except harness.RunCancelled:
            raise Stopped("The readability run was stopped before anything was rewritten; nothing was published") from None
        outcomes = [_Outcome(part) for part in parts]
        text_parts = [outcome for outcome in outcomes if outcome.part.kind == "text"]
        todo = list(text_parts)
        for outcome in outcomes:
            if outcome.part.kind == "references":
                outcome.reason = "references are kept as in the original"
        for outcome in todo[self.caps.max_parts:]:
            outcome.reason = f"this run rewrites at most {self.caps.max_parts} part{'s' if self.caps.max_parts != 1 else ''}"
            self.degraded.append("part-limit")
        todo = todo[: self.caps.max_parts]
        # One rewrite and one check per planned part, and one call for the guide.
        fits = max(0, (self.ctx.remaining() - 1) // 2)
        for outcome in todo[fits:]:
            outcome.reason = self._stop_reason()
        todo = todo[:fits]
        by_id = {node["id"]: node for node in segments.get("nodes") or []}
        macros = segments.get("macros") or {}
        fidelity = segments.get("math_fidelity")
        refs = references.label_map(segments)
        try:
            self.run_guide(segments, manuscript, document, parts, planned=2 * len(todo))
            calls = [self.rewrite_call(outcome, document=document, by_id=by_id, macros=macros, total=len(parts),
                                       math_fidelity=fidelity, refs=refs) for outcome in todo]
            self.rewrite(outcomes, calls, total=len(parts))
            if not self.stopped:
                self.check(outcomes, document=document, total=len(parts), math_fidelity=fidelity)
            if not self.stopped:
                self.repair(outcomes, {call.outcome.part.index: call for call in calls}, document=document,
                            total=len(parts), math_fidelity=fidelity)
        except harness.RunCancelled:
            self.stopped = True
        for outcome in outcomes:
            if outcome.status == "unchecked" and not outcome.reason:
                outcome.reason = self._stop_reason()
        published = self.publish(outcomes, document=document, segments=segments, manuscript=manuscript)
        if text_parts and not self.stopped and not published["counts"]["rewritten"]:
            # What exists is published, but the run is not a success.
            reason = next((outcome.reason for outcome in text_parts if outcome.reason), "every rewrite failed")
            raise AnalyzeError(f"No part could be rewritten: {reason}")
        return published


def _name(part: readability_document.Part) -> str:
    return f"part {part.index} ({part.title})" if part.title else f"part {part.index}"


def outline_guide(nodes: list[Mapping[str, Any]], *, title: str) -> dict[str, Any]:
    """The deterministic guide used when the model's is not available: the
    title and the main results as the manuscript states them."""
    mains = [node for node in nodes if node.get("main_result")][:8]
    return {
        "title": title,
        "one_line": "",
        "main_results": [{"id": node["id"], "plain": " ".join(str(node.get("statement") or "").split())[:600]}
                         for node in mains],
        "key_idea": "",
        "roadmap": [],
        "notation": [],
        "terms": [],
        "map": [],
        "extra_edges": [],
        "reader_notes": [],
    }


def run_readability(
    workspace: Path,
    req: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    sink: Callable[[dict[str, Any]], None] | None = None,
    fetcher: safe_fetch.SafeFetcher | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Run one readability run in ``workspace`` (see the module docstring).

    Raises :class:`ConfigError`, ``AnalyzeError`` or :class:`Stopped`; a
    SIGTERM after the parts are planned publishes what has settled and
    returns with ``stopped: True``.
    """
    missing = runner.missing_packages()
    if missing:
        raise ConfigError(
            "Readability is not fully installed on this server (missing Python package: " + ", ".join(missing)
            + "); please tell the operator")
    run = ReadabilityRun(workspace, req, env=env if env is not None else os.environ, sink=sink,
                         fetcher=fetcher, sleep=sleep)
    token = harness._RUN_EMITTER.set(run.emitter)
    try:
        with harness._stop_on_sigterm(run.cancel), runner.analysis_lock(run.folder, busy=_LOCK_BUSY):
            run.emit({"type": "thread.started", "thread_id": "readability"})
            run.emit({"type": "item.completed", "item": {"type": "agent_message", "text": (
                f"Ansatz Readability · {run.info.route.model} via {run.info.name} · up to {run.caps.max_parts} "
                f"parts · {run.caps.max_calls} calls · {run.caps.concurrency} in flight · no tools")}})
            return run.run()
    finally:
        run.emitter.close()
        harness._RUN_EMITTER.reset(token)


# ---------------------------------------------------------------------------
# a question about a finished run
# ---------------------------------------------------------------------------


def _usage(calls: list[dict[str, Any]]) -> dict[str, int]:
    total: dict[str, int] = {}
    for record in calls:
        for key, value in (record.get("usage") or {}).items():
            if isinstance(value, int) and not isinstance(value, bool):
                total[key] = total.get(key, 0) + value
    return total


def run_ask(
    workspace: Path,
    ask_id: str,
    *,
    route_env: Mapping[str, str],
    sink: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Answer the question ``readability/asks/<ask_id>.json`` with one model
    call on the run's route; writes and returns the answer.  Raises
    ``AnalyzeError`` (or :class:`ConfigError`, :class:`Stopped`) with a
    public-safe message; a failed answer is written too."""
    root = Path(workspace).resolve()
    folder = root / "readability"
    try:
        req = readability_request.load(root)
        question = readability_ask.load_request(root, ask_id)
    except (readability_request.RequestError, readability_ask.AskError) as exc:
        raise ConfigError(str(exc)) from None
    receipt = _json(folder / "receipt.json")
    if not receipt or receipt.get("schema") != RECEIPT_SCHEMA:
        raise ConfigError("This run has no readable version to ask about yet")
    run = ReadabilityRun(root, req, env=route_env, sink=sink, cancel=threading.Event(),
                         caps_override={"max_calls": ASK_MAX_CALLS, "max_seconds": ASK_SECONDS})
    guide = _json(folder / "guide.json") or {}
    segments = _json(folder / "segments.json") or {}
    check = _json(folder / "check.json") or {}
    paper = paper_map.load(root) or {"nodes": []}
    nodes = segments.get("nodes") or []
    limit = prompts.BOUNDED_ASK_CONTEXT_BYTES if run.info.bounded else prompts.ASK_CONTEXT_BYTES
    context, part = readability_ask.context(
        ask=question, readable=_text_file(folder / "readable.md"),
        original_lines=_text_file(folder / "original.txt").split("\n"),
        parts=[item for item in check.get("parts") or [] if isinstance(item, dict) and isinstance(item.get("index"), int)],
        node_parts={node["id"]: node.get("part") for node in paper["nodes"]}, limit=limit)
    user = prompts.ask_prompt(question=question["question"], passage=question["selection"], context=context,
                              guide=prompts.guide_summary(guide, run.summary_bytes),
                              results=prompts.results_list(nodes, run.guide_bytes[0]))
    ids = [node["id"] for node in nodes if isinstance(node, dict) and node.get("id")]
    answer: dict[str, Any] = {"schema": ANSWER_SCHEMA, "ask_id": ask_id, "part": part,
                              "model": run.info.route.model, "route": run.info.name}
    token = harness._RUN_EMITTER.set(run.emitter)
    failure = ""
    try:
        with harness._stop_on_sigterm(run.cancel):
            run.emit({"type": "thread.started", "thread_id": "readability-ask"})
            try:
                job = harness._run_single(
                    run.ctx, "ask", reserve=0,
                    work=lambda turn: harness._call_and_parse(
                        run.ctx, turn, stage="ask", system=prompts.READABLE_ASK_SYSTEM, user=user,
                        limit=prompts.ASK_REPLY_BYTES, parse=partial(parse.parse_answer, node_ids=ids),
                        family=prompts.ASK_FAMILY))
                parsed, reply = harness._job_result(job)
                # Leftover LaTeX references read as in the readable version.
                parsed["answer"] = references.resolve(parsed["answer"], references.label_map(segments))
                answer.update(parsed, status="done", model=(reply.observed_model or reply.model or run.info.route.model))
            except harness.RunCancelled:
                raise Stopped("The question was stopped; nothing was answered") from None
            except harness.ProviderCallError as exc:
                failure = run._failure_reason(exc)[0]
            except harness.PromptBoundError:
                failure = "the question and its context are too long for one model call"
            except ParseError as exc:
                failure = f"the answer could not be read ({exc})"[:300]
            except (harness._BudgetExhausted, harness.MathHarnessError):
                failure = "the question reached its time or call limit"
    finally:
        run.emitter.close()
        harness._RUN_EMITTER.reset(token)
    answer.update(usage=_usage(run.ctx.calls), calls=run.ctx.calls_used, created_at=runner._utc_now())
    if failure:
        answer.update(status="failed", error=failure)
    readability_ask.write_answer(root, ask_id, answer)
    if failure:
        raise AnalyzeError(f"The question could not be answered: {failure}")
    return answer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ansatz Readability runner")
    parser.add_argument("--ask", metavar="ASK_ID")
    args = parser.parse_args(argv)
    workspace = Path.cwd()
    env = dict(os.environ)
    # Space arXiv requests across every runner and the console, not per process.
    sources.ARXIV_SLOT_PATH = "auto"
    try:
        req = readability_request.load(workspace)
    except readability_request.RequestError as exc:
        harness.emit_item({"type": "error", "message": str(exc), "terminal": True})
        return 2
    try:
        # Kimi routes must not see ambient proxies (urllib reads os.environ).
        info_env = dict(env)
        runner.configure_route(info_env, req, ROUTES)
        for name in runner._DORMANT_ENV:
            if name not in info_env:
                os.environ.pop(name, None)
        if info_env.get("NO_PROXY") == "*":
            os.environ["NO_PROXY"] = os.environ["no_proxy"] = "*"
        if args.ask:
            result = run_ask(workspace, args.ask, route_env=env)
            harness.emit({"type": "readability.ask", "result": result})
            return 0
        outcome = run_readability(workspace, req, env=env)
    except AnalyzeError as exc:  # ConfigError (2) and Stopped (143) included
        harness.emit_item({"type": "error", "message": runner.public_message(str(exc), env), "terminal": True})
        return exc.exit_code
    except harness.MathHarnessError as exc:
        harness.emit_item({"type": "error", "message": runner.public_message(f"Readability failed: {exc}", env),
                           "terminal": True})
        return 1
    except (OSError, ValueError) as exc:
        harness.emit_item({"type": "error", "message": f"Readability failed closed ({type(exc).__name__})",
                           "terminal": True})
        return 1
    if outcome.get("stopped"):
        harness.emit_item({"type": "error", "message": "The readability run was stopped; what had settled was published",
                           "terminal": True})
        return 128 + int(signal.SIGTERM)
    return 0


if __name__ == "__main__":
    # Run as the importable module, so its exceptions are the classes main() catches.
    from agent_monitor.runners import readability_runner as _module

    sys.exit(_module.main())
