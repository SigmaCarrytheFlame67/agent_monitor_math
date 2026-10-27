"""What an analysis shows while it runs and when it ends (Analyze UX, 2026-10-11).

A live guest analysis showed the verifier's raw JSON in the Sandbox for the
whole Check stage, "Check… }" in the Progress panel, the pasted link as its
run-list label until the runner exited, and the runner's process log as the
closing chat message.  These tests pin what the server sends instead: the
runner's progress notes, the manuscript's title as soon as it is known, and a
plain closing summary.  Offline: fake runners and fetchers only.
"""
from __future__ import annotations

import json
import sys
import time
from collections import OrderedDict
from pathlib import Path

import pytest

pytest.importorskip("pylatexenc")

from agent_monitor import analyze_runs, jobs  # noqa: E402
from agent_monitor import console_server as console  # noqa: E402
from agent_monitor.analyze import report, sources  # noqa: E402
from agent_monitor.analyze import request as analysis_request  # noqa: E402
from agent_monitor.analyze.titles import plain_title, tex_text_accents  # noqa: E402
from agent_monitor.safe_fetch import FetchError  # noqa: E402

# Agent nodes as the console stores them for a running sponsored analysis
# (shapes copied from the live run of 2026-10-11, texts shortened).
PRETTY_REPLY = json.dumps({
    "claim": "There exist constants \\(c_1, c_2 > 0\\) such that ...",
    "role": "theorem", "depends_on": [{"id": "lem:greedy", "how": "applies it"}], "missing_dependencies": [],
    "external": [], "verification_report": {"summary": "All steps check out.", "critical_errors": [], "gaps": []},
    "verdict": "checked", "notes": "Both halves verified line by line.", "confidence": "high",
}, indent=2)


def _agent(index: int, output: str, *, kind: str = "reasoning", stage: str = "verify") -> dict:
    role = {"reasoning": "reasoning", "message": "assistant", "error": "reasoning"}[kind]
    return {"trace_id": f"r::turn-{index}", "stage_name": f"turn-{index} ({kind})", "role": role,
            "pipeline_stage": stage, "output": output, "status": "finished"}


def live_agents() -> list[dict]:
    outputs = [
        ("Ansatz Analyze · kimi-k3 via sponsored_kimi · up to 12 results · 16 calls · 2 in flight · no tools", "message", "scaffold"),
        ("Analyze · 1 Ingest · link", "reasoning", "scaffold"),
        ("Analyze · Ingest · Short Egyptian fractions · 11 files", "reasoning", "scaffold"),
        ("Analyze · 2 Segment", "reasoning", "work"),
        ("Analyze · Segment · 19 statements · 19 dependencies · pylatexenc-2.11", "reasoning", "work"),
        ("Analyze · 3 Check · checking up to 12 of 19 results", "reasoning", "verify"),
        ("Analyze · map", "reasoning", "verify"),
        ('{"summary": "The manuscript claims ...", "main_results": ["thm:main"]}', "message", "verify"),
        ("Analyze · Check · main results: Theorem 1.1, Corollary 1.2", "reasoning", "verify"),
        ("Analyze · Check · now checking Theorem 1.1, Corollary 1.2", "reasoning", "verify"),
        ("Analyze · check-1", "reasoning", "verify"),
        ("Analyze · check-2", "reasoning", "verify"),
        (PRETTY_REPLY, "message", "verify"),
        ("```json\n" + PRETTY_REPLY + "\n```", "message", "verify"),
        ("Analyze · check-2-low", "reasoning", "verify"),
        ("Analyze · Check · Theorem 1.1 · checked", "reasoning", "verify"),
        ("Analyze · Check · Corollary 1.2 · gap — The bound \\(|H| \\le X/4\\) is used without", "reasoning", "verify"),
        ("Analyze · check-3-retry-low", "reasoning", "verify"),
        ("Analyze · segment-1", "reasoning", "work"),
    ]
    return [_agent(index, text, kind=kind, stage=stage) for index, (text, kind, stage) in enumerate(outputs, 1)]


EXPECTED_PROGRESS = [
    "1 Ingest · link",
    "Ingest · Short Egyptian fractions · 11 files",
    "2 Segment",
    "Segment · 19 statements · 19 dependencies · pylatexenc-2.11",
    "3 Check · checking up to 12 of 19 results",
    "Check · main results: Theorem 1.1, Corollary 1.2",
    "Check · Theorem 1.1 · checked",
    "Check · Corollary 1.2 · gap — The bound \\(|H| \\le X/4\\) is used without",
]


def test_the_live_sandbox_shows_progress_notes_never_model_output():
    data = {"run_id": "r", "status": "running", "engine": "analyze", "run_kind": "analyze", "agents": live_agents()}
    text = console._analysis_progress_text(data)
    assert text.split("\n") == EXPECTED_PROGRESS
    for leak in ('"claim"', '"verdict"', "```", "{", "check-2", "check-3-retry-low", "segment-1",
                 "Analyze · map", "sponsored_kimi", "main_results"):
        assert leak not in text, leak
    # "Now checking" is kept only while those checks are in flight (as the last line).
    waiting = {**data, "agents": data["agents"] + [_agent(99, "Analyze · Check · now checking Lemma 2.3")]}
    assert console._analysis_progress_text(waiting).split("\n")[-1] == "Check · now checking Lemma 2.3"
    assert "now checking Theorem 1.1" not in console._analysis_progress_text(waiting)
    # The Sandbox payload of a running analysis carries exactly that text.
    preview = console._analysis_preview("no-such-run", data)
    assert preview["kind"] == "analysis" and preview["has_report"] is False
    assert preview["text"] == text and preview["source"] == "live"


def test_a_billed_call_with_no_usable_reply_adds_no_progress_line():
    # The harness books such a call's usage on a node of its own (Max (API)
    # review, 2026-10-27): it opens with the bare call marker, which both
    # manuscript runners' progress views drop.
    from unittest.mock import patch

    from agent_monitor.runners import math_harness_runner as harness
    from agent_monitor.runners.api_backend import APIBackendError

    for label, stage, progress in (
        ("Analyze", "check-4-retry-low", console._analysis_progress_text),
        ("Readability", "rewrite-3-low", console._readability_progress_text),
    ):
        events: list[dict] = []
        ctx = harness._Ctx(
            route=harness.Route("openai", "gpt-6-astra", "api_chat"), budgets=harness.Budgets(max_calls=4),
            started=time.monotonic(), env={"OPENAI_API_KEY": "test-key"},
            emitter=harness._RunEmitter(events.append), label=label,
        )
        turn = ctx.open_turn(stage, 1)
        billed = APIBackendError("OpenAI returned an empty response for gpt-6-astra",
                                 usage={"input_tokens": 900, "output_tokens": 64_000})
        with patch.object(harness, "api_chat", side_effect=billed), pytest.raises(harness.ProviderCallError):
            harness._provider_call(ctx, turn, stage=stage, system="s", user="u", effort=None)
        notes = [event["item"]["text"] for event in events
                 if event.get("type") == "item.completed" and event["item"].get("type") == "reasoning"]
        # The call's own marker, then the usage's node: both the bare marker.
        assert notes == [f"{label} · {stage}"] * 2, notes
        data = {"status": "running", "agents": [_agent(index, text) for index, text in enumerate(notes, 1)]}
        assert progress(data) == "", (label, progress(data))


def test_an_analysis_that_failed_shows_its_error_not_its_process_log():
    agents = [
        _agent(1, "Ansatz Analyze · kimi-k3 via sponsored_kimi · up to 30 results", kind="message", stage="scaffold"),
        _agent(2, "Analyze · 1 Ingest · link", stage="scaffold"),
        _agent(3, "The source could not be used: arXiv has no paper with that identifier", kind="error", stage="scaffold"),
        {"trace_id": "r::analyze_final", "stage_name": "analyze session summary", "role": "orchestrator",
         "pipeline_stage": "finalize", "status": "failed",
         "output": "◦ session analyze…\nassistant · Ansatz Analyze · kimi-k3\nstage · 1 Ingest · link\n"
                   "error · The source could not be used: arXiv has no paper with that identifier"},
    ]
    data = {"status": "failed", "agents": agents,
            "error": "Ansatz Analyze exited with code 1: The source could not be used: arXiv has no paper with that identifier"}
    assert console._analysis_progress_text(data).split("\n") == [
        "1 Ingest · link", "The source could not be used: arXiv has no paper with that identifier"]
    # Nothing recorded at all: the run's error, once it has ended (never while it runs).
    assert console._analysis_progress_text({"status": "failed", "agents": [], "error": "Engine unavailable"}) == "Engine unavailable"
    assert console._analysis_progress_text({"status": "running", "agents": [], "error": "x"}) == ""


def _manuscript(workspace: Path, title: str) -> None:
    (workspace / "analysis").mkdir(parents=True, exist_ok=True)
    (workspace / "analysis" / "manuscript.json").write_text(json.dumps({"title": title, "format": "tex"}))


def test_the_running_label_is_the_manuscript_title_once_it_was_read(tmp_path):
    analysis_request.write(tmp_path, source={"kind": "url", "url": "https://arxiv.org/abs/2302.05537"}, caps={},
                           focus="Lemma 3.2", formalize=False, role="member", route="api_key", model="m",
                           reasoning_effort="default", sponsored=False)
    assert jobs._analyze_running_label(tmp_path) == ""
    _manuscript(tmp_path, "On a conjecture of Erd\\H{o}s and $\\pi$")
    assert jobs._analyze_running_label(tmp_path) == "Analyze · On a conjecture of Erdős and π\nFocus: Lemma 3.2"


def test_the_run_finished_line_counts_an_analysis_in_model_calls():
    # Run C: 53 process-log entries, 15 model calls; the line agrees with the Progress pane's chip.
    run = {"agents": [{}] * 53, "analyze": {"summary": {"calls": 15}},
           "totals": {"input_tokens": 68049, "output_tokens": 71422, "cost_usd": 0.8613}}
    assert jobs._run_finished_line(run, "finished", 1396.4, analyze=True) == \
        "✓ run finished · 15 model calls · in 68049 / out 71422 tokens · $0.8613 · 1396s"
    assert jobs._run_finished_line({"analyze": {"summary": {"calls": 1}}}, "stopped", 9, analyze=True) == \
        "✗ run stopped · 1 model call · in 0 / out 0 tokens · 9s"
    # Other runs keep their agent count.
    assert jobs._run_finished_line(run, "failed", 61, analyze=False) == \
        "✗ run failed · 53 agents · in 68049 / out 71422 tokens · $0.8613 · 61s"


def test_the_closing_message_is_a_plain_summary():
    summary = {"checked": 9, "gap": 3, "error": 0, "unclear": 0, "not_checked": 7,
               "coverage": {"checked": 12, "total": 19}}
    finished = jobs._analyze_chat_summary({"analyze": {"summary": summary}}, "finished")
    assert finished.startswith("Analysis finished: 12 of 19 results checked (9 with no issue found, 3 with gaps, 7 not checked).")
    assert "not a certificate" in finished
    # A statement without a proof is never checkable: it is not "not checked" here.
    whole = jobs._analyze_chat_summary({"analyze": {"summary": {
        "checked": 2, "gap": 4, "error": 2, "unclear": 0, "not_checked": 1, "coverage": {"checked": 8, "total": 8}}}}, "finished")
    assert whole.startswith("Analysis finished: 8 of 8 results checked (2 with no issue found, 4 with gaps, 2 with errors).")
    stopped = jobs._analyze_chat_summary({"analyze": {"summary": {**summary, "gap": 1}}}, "stopped")
    assert stopped.startswith("Analysis stopped:") and "1 with a gap" in stopped
    nothing = {"checked": 0, "gap": 0, "error": 0, "unclear": 0, "not_checked": 5, "coverage": {"checked": 0, "total": 5}}
    failed = jobs._analyze_chat_summary({
        "analyze": {"summary": nothing},
        "agents": [_agent(1, "No result could be checked: the provider refused", kind="error")],
        "error": "Ansatz Analyze exited with code 1: No result could be checked: the provider refused"}, "failed")
    assert failed == ("Analysis failed: no result was checked. The report in the Sandbox says why.\n\n"
                      "No result could be checked: the provider refused")
    unpublished = jobs._analyze_chat_summary(
        {"analyze": {}, "agents": [], "error": "Ansatz Analyze exited with code 1: The source could not be used: x"},
        "failed")
    assert unpublished == "Analysis failed; no report was published.\n\nThe source could not be used: x"
    for text in (finished, stopped, failed, unpublished):
        assert "◦ session" not in text and "{" not in text and "stage ·" not in text


FAKE_RUNNER = r'''
import json, sys, time
from pathlib import Path
analysis = Path("analysis")
print(json.dumps({"type": "item.completed", "item": {"type": "reasoning", "text": "Analyze · 1 Ingest · link"}}), flush=True)
analysis.mkdir(exist_ok=True)
(analysis / "manuscript.json").write_text(json.dumps({"title": "A fake manuscript about $\\pi$", "format": "tex"}))
print(json.dumps({"type": "item.completed", "item": {"type": "reasoning", "text": "Analyze · 2 Segment"}}), flush=True)
release = Path(sys.argv[1])
deadline = time.time() + 60
while not release.exists() and time.time() < deadline:
    time.sleep(0.1)
print(json.dumps({"type": "item.completed", "item": {"type": "error", "message": "No result could be checked: fake", "terminal": True}}), flush=True)
sys.exit(1)
'''


def _wait(predicate, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    return None


def test_a_running_analysis_is_listed_by_its_title_and_ends_with_a_plain_summary(tmp_path, monkeypatch):
    runner = tmp_path / "fake_runner.py"
    runner.write_text(FAKE_RUNNER)
    release = tmp_path / "release"
    monkeypatch.setenv("ANALYZE_CMD", f"{sys.executable} -u {runner} {release}")
    monkeypatch.setenv("AGENT_MONITOR_DISABLE_AUTO_PIPELINE", "1")
    monkeypatch.setattr(jobs, "_estimate_cost", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(analyze_runs, "_RESOLVED_TITLES", OrderedDict())
    job = jobs.start_job(engine="analyze", run_kind="analyze", source={"url": "https://arxiv.org/abs/2302.05537"},
                         model="deepseek-test", user=None, auth_route="api_key")
    run_id = job["run_id"]
    try:
        # No preview happened: the link first, the manuscript's title while it still runs.
        assert jobs._load_run_record(run_id)["problem_text_preview"] == "Analyze · https://arxiv.org/abs/2302.05537"
        titled = _wait(lambda: (lambda record: record.get("status") == "running"
                                and record.get("problem_text_preview") == "Analyze · A fake manuscript about π"
                                and record)(jobs._load_run_record(run_id)))
        assert titled, jobs._load_run_record(run_id)
        listed = {entry["run_id"]: entry for entry in json.loads((jobs._cache_dir() / "manifest.json").read_text())["runs"]}
        assert listed[run_id]["problem_preview"] == "Analyze · A fake manuscript about π"
    finally:
        release.write_text("go")
    assert _wait(lambda: jobs.get_job(job["job_id"])["status"] != "running")
    # The job's status is written just before its closing chat lines.
    assert _wait(lambda: str(jobs.list_chat(run_id)[-1].get("content") or "").startswith("Analysis failed"))
    chat = jobs.list_chat(run_id)
    assert chat[-1]["role"] == "system"
    assert chat[-1]["content"] == "Analysis failed; no report was published.\n\nNo result could be checked: fake"
    assert chat[-2]["content"].startswith("✗ run failed · 0 model calls · in ")
    assert not any("◦ session" in str(message.get("content")) for message in chat)


def test_previews_tell_a_lookup_that_may_work_later_from_a_link_that_cannot_work():
    class Fetcher:
        def __init__(self, error):
            self.error = error

        def get(self, url, **_kwargs):
            raise self.error

    slow = sources.resolve("https://github.com/openai/math", fetcher=Fetcher(FetchError("timed out", kind="timeout")))
    missing = sources.resolve("https://github.com/someone/missing-repo",
                              fetcher=Fetcher(FetchError("HTTP 404", kind="not_found", status=404)))
    assert slow.ok is False and slow.as_dict()["retryable"] is True
    assert missing.ok is False and "retryable" not in missing.as_dict()


def test_tex_text_accents_in_titles_and_report_prose():
    assert tex_text_accents("Erd\\H{o}s, P\\'olya, Schr\\\"{o}dinger, Szemer\\'edi, {\\\"o}, Bj{\\o}rn") == \
        "Erdős, Pólya, Schrödinger, Szemerédi, ö, Bjørn"
    # Math and other commands are left alone; an escaped apostrophe stays.
    assert tex_text_accents("$\\\"o$ and \\(\\H{o}\\), \\Hom, \\cdot, \\vec{v}, it\\'s") == \
        "$\\\"o$ and \\(\\H{o}\\), \\Hom, \\cdot, \\vec{v}, it\\'s"
    assert plain_title("Erd\\H{o}s problems on $\\pi$") == "Erdős problems on π"
    assert report._cell("The manuscript claims to prove Erd\\H{o}s's conjecture that 1<=a<b, \\(|x|\\)") == \
        "The manuscript claims to prove Erdős's conjecture that 1&lt;=a&lt;b, \\(\\|x\\|\\)"


def test_the_report_names_the_route_and_limits_in_words_and_skips_a_link_identifier():
    from test_analyze_report import fixture

    graph, manuscript, receipt, paper_map = fixture()
    text = report.render(graph, manuscript, {**receipt, "degraded": ["paper-map-failed", "provider-failure", "x-new"]},
                         paper_map=paper_map)
    assert "via the site's sponsored access" in text and "sponsored_kimi" not in text
    assert ("Limits reached: the overview call failed, so results were ordered by the manuscript's structure; "
            "the model provider did not answer every check; x-new.") in text
    linked = report.render(graph, {**manuscript, "canonical_id": "https:https://example.org/paper.pdf"}, receipt)
    assert "**Identifier:**" not in linked


# ---------------------------------------------------------------------------
# Cross-references in model prose: "Corollary~\ref{cor:counting}" reads
# "Corollary 1.2" (a label the analysis does not know is shown bare).
# ---------------------------------------------------------------------------

from agent_monitor import run_sharing  # noqa: E402
from agent_monitor.analyze import graph as analysis_graph  # noqa: E402
from agent_monitor.analyze import latex_structure  # noqa: E402
from agent_monitor.analyze.titles import tex_text_refs  # noqa: E402

from test_analyze_runner import provider  # noqa: E402,F401  (fixture import)

REFS = {"cor:counting": ("Corollary 1.2", "1.2"), "sec:prescribed": ("Section 7", "7")}


def test_tex_text_refs_reads_cross_references_as_text():
    assert tex_text_refs("from 'the construction in Section~\\ref{sec:prescribed}'", REFS) == \
        "from 'the construction in Section 7'"
    assert tex_text_refs("in the proof of Corollary~\\ref{cor:counting}", REFS) == "in the proof of Corollary 1.2"
    assert tex_text_refs("\\cref{cor:counting}, \\Cref{sec:prescribed,cor:counting} and \\autoref{x}", REFS) == \
        "Corollary 1.2, Section 7, Corollary 1.2 and (x)"
    assert tex_text_refs("by \\eqref{eq:supply-size} and \\hyperref[sec:x]{the last section}", REFS) == \
        "by (supply-size) and the last section"
    assert tex_text_refs("\\S\\ref{sec:prescribed}, \\S~\\ref{sec:a}, \\S 3 and \\cite[Thm. 2]{Smith, Jones}", REFS) == \
        "§7, §(a), §3 and [Smith, Jones]"
    # A label the analysis does not know reads without its command and prefix.
    assert tex_text_refs("the construction in Section~\\ref{sec:prescribed}") == "the construction in Section (prescribed)"
    assert tex_text_refs("\\cref{lem:gone}, \\cref{plain}, Theorem~\\ref{mainthm}, \\eqref{eq:a,cor:counting}", REFS) == \
        "Lemma (gone), (plain), Theorem (mainthm), (a, 1.2)"
    # Only ties become spaces ("~" in model prose can mean "of the order of"); math is left as written.
    assert tex_text_refs("Lemma~3.2, N(b) ~ log log b, $\\ref{x}~3$ and \\(\\eqref{y}\\)", REFS) == \
        "Lemma 3.2, N(b) ~ log log b, $\\ref{x}~3$ and \\(\\eqref{y}\\)"
    assert tex_text_refs("no commands here", REFS) == "no commands here" and tex_text_refs(None) == ""


TEX_WITH_SECTIONS = r"""\documentclass{amsart}
\newtheorem{lemma}{Lemma}[section]
\newtheorem{corollary}[lemma]{Corollary}
\begin{document}
\section{Preliminaries}\label{sec:prelim}
\begin{lemma}\label{lem:a} Every $x$ works. \end{lemma}
\begin{proof} Trivial; see Section~\ref{sec:main}. \end{proof}
\section{The main argument}
\label{sec:main}
\subsection*{An unnumbered step}\label{ssec:step}
\begin{corollary}\label{cor:b} By Lemma~\ref{lem:a}. \end{corollary}
\begin{proof} Clear. \end{proof}
\end{document}
"""


def test_sections_keep_their_labels_and_the_graph_names_them(tmp_path):
    (tmp_path / "main.tex").write_text(TEX_WITH_SECTIONS)
    segments = latex_structure.parse_project(tmp_path)
    assert [(section["title"], section["labels"]) for section in segments["sections"]] == [
        ("Preliminaries", ["sec:prelim"]), ("The main argument", ["sec:main"]), ("An unnumbered step", ["ssec:step"])]
    refs = analysis_graph.segments_ref_labels(segments)
    assert refs["lem:a"] == ("Lemma 1.1", "1.1") and refs["cor:b"] == ("Corollary 2.1", "2.1")
    assert refs["sec:main"] == ("Section 2", "2") and refs["ssec:step"] == ("“An unnumbered step”", "“An unnumbered step”")
    graph = analysis_graph.build({**segments, "nodes": [{**node, "id": node["labels"][0]} for node in segments["nodes"]]},
                                 {"title": "T"}, generated_at="2026-10-11T00:00:00Z")
    clean = analysis_graph.sanitize(graph)
    assert clean["ref_labels"]["sec:main"] == {"name": "Section 2", "number": "2"}
    assert analysis_graph.ref_labels(clean)["cor:b"] == ("Corollary 2.1", "2.1")
    assert analysis_graph.ref_labels(clean)["sec:prelim"] == ("Section 1", "1")
    # Stored graphs are bounded: odd labels and values are dropped, old graphs have none.
    odd = analysis_graph.sanitize({**graph, "ref_labels": {"bad label": {"name": "x"}, "ok": "not a mapping",
                                                         "fine:1": {"name": "Section 9" * 50}}})
    assert list(odd["ref_labels"]) == ["fine:1"] and len(odd["ref_labels"]["fine:1"]["name"]) == 200
    assert analysis_graph.sanitize({**graph, "ref_labels": None})["ref_labels"] == {}


def test_the_report_reads_model_references_as_the_manuscripts_results():
    from test_analyze_report import fixture

    graph, manuscript, receipt, paper_map = fixture()
    node = next(item for item in graph["nodes"] if item["meta"]["verdict"] == "gap")
    target = next(item for item in graph["nodes"] if item["meta"].get("labels") and item is not node)
    label, name = target["meta"]["labels"][0], target["label"]
    node["meta"]["issues"] = [{"type": "gap", "location": "proof of \\S\\ref{sec:prescribed}",
                               "issue": f"asserted from Lemma~\\ref{{{label}}} and \\eqref{{eq:missing}} via \\cref{{{label}}}"}]
    graph["ref_labels"] = {"sec:prescribed": {"name": "Section 7", "number": "7"}}
    text = report.render(graph, manuscript, receipt,
                         paper_map={**paper_map, "summary": "It follows Corollary~\\ref{nowhere}."})
    number = target["meta"]["number"]
    assert f"proof of §7: asserted from Lemma {number} and (missing) via {name}" in text
    assert "It follows Corollary (nowhere)." in text
    assert "\\ref{" not in text and "\\eqref{" not in text and "\\cref{" not in text and "~\\" not in text


def test_progress_notes_name_the_results_a_finding_cites(tmp_path, provider):
    from test_analyze_runner import default_script, own_key_env, verdict_json
    from agent_monitor.runners import analyze_runner

    server = provider(default_script({"lem:sum": lambda _user: verdict_json(
        "gap", "the step uses Lemma~\\ref{lem:rep} and Section~\\ref{sec:proofs} without checking")}))
    paper = PAPER_WITH_SECTION_LABEL
    analysis_request.write(tmp_path, source={"kind": "paste", "text": paper}, caps={}, focus="", formalize=False,
                           role="member", route="api_key", model="deepseek-test", reasoning_effort="default",
                           sponsored=False)
    events = []
    analyze_runner.run_analysis(tmp_path, analysis_request.load(tmp_path), env=own_key_env(server),
                                sink=events.append, sleep=lambda _s: None)
    notes = [event["item"]["text"] for event in events
             if event.get("type") == "item.completed" and event["item"].get("type") == "reasoning"]
    gap = next(note for note in notes if note.startswith("Analyze · Check · Lemma 2.2 · gap"))
    assert gap == "Analyze · Check · Lemma 2.2 · gap — the step uses Lemma 2.1 and Section 2 without checking"
    # The new report reads the same way.
    assert "Lemma 2.1 and Section 2" in (tmp_path / "analysis" / "report.md").read_text()


PAPER_WITH_SECTION_LABEL = r"""\documentclass{article}
\newtheorem{theorem}{Theorem}[section]
\newtheorem{lemma}[theorem]{Lemma}
\title{Sums of evens}
\begin{document}
\section{Introduction}
\begin{theorem}\label{thm:main} The sum of two even integers is even. \end{theorem}
\section{Proofs}\label{sec:proofs}
\begin{lemma}\label{lem:rep} Every even integer is $2k$. \end{lemma}
\begin{proof} By definition. \end{proof}
\begin{lemma}\label{lem:sum} $2a+2b=2(a+b)$. \end{lemma}
\begin{proof} Distributivity. \end{proof}
\begin{proof}[Proof of Theorem~\ref{thm:main}] By Lemma~\ref{lem:rep} and Lemma~\ref{lem:sum}. \end{proof}
\end{document}
"""


def test_a_shared_report_written_before_reads_its_references_too(tmp_path, provider):
    from test_analyze_runner import default_script, own_key_env
    from agent_monitor.runners import analyze_runner

    server = provider(default_script())
    run_id = "analyze_share_refs_1"
    workspace = run_sharing.RUNS_DIR / "workspaces" / run_id
    workspace.mkdir(parents=True, exist_ok=True)
    analysis_request.write(workspace, source={"kind": "paste", "text": PAPER_WITH_SECTION_LABEL}, caps={}, focus="",
                           formalize=False, role="member", route="api_key", model="deepseek-test",
                           reasoning_effort="default", sponsored=False)
    analyze_runner.run_analysis(workspace, analysis_request.load(workspace), env=own_key_env(server),
                                sink=lambda _e: None, sleep=lambda _s: None)
    # A report written before references were resolved (as on 2026-10-11).
    report_path = workspace / "analysis" / "report.md"
    report_path.write_text(report_path.read_text() + "\n- gap: proof — see Lemma~\\ref{lem:rep} and \\S\\ref{sec:proofs}.\n")
    from agent_monitor.analyze import record as analyze_record

    record = {"run_id": run_id, "engine": "analyze", "run_kind": "analyze", "status": "finished",
              "workspace": str(workspace), "owner_id": 5, "analyze": analyze_record.run_block(workspace) or {}}
    snapshot = run_sharing.snapshot(run_id, record)
    assert "- gap: proof — see Lemma 2.1 and §2." in snapshot["proof"]
    statements = {node["id"]: node["statement"] for node in snapshot["informal_graph"]["nodes"]}
    assert "\\ref" not in json.dumps(statements)


# Analyses parsed before sections kept their labels (Run C of 2026-10-11): the
# label is read once from the stored copy, right after the section heading.
OLD_STYLE_SOURCE = {
    "main.tex": "\\section{Introduction}\\label{sec:intro}\nText.\n"
                "\\section{Preserving a prescribed denominator}\n% the label follows\n\\label{sec:prescribed}\n"
                "\\subsection{A step \\label{ssec:step}}\nMore.\n"
                "\\section{Unlabelled}\nA \\label{eq:not-a-section} later.\n",
    "appendix.tex": "\\section{Technical proofs}\\label{app:tech}\n",
}
OLD_STYLE_SECTIONS = [
    {"level": "section", "title": "Introduction", "number": "1", "file": "main.tex", "line": 1},
    {"level": "section", "title": "Preserving a prescribed denominator", "number": "7", "file": "main.tex", "line": 3},
    {"level": "subsection", "title": "A step", "number": "7.1", "file": "main.tex", "line": 6},
    {"level": "section", "title": "Unlabelled", "number": "8", "file": "main.tex", "line": 8},
    {"level": "section", "title": "Technical proofs", "number": "A", "file": "appendix.tex", "line": 1},
    {"level": "section", "title": "Escape", "number": "9", "file": "../outside.tex", "line": 1},
]


def _old_style_workspace(root: Path, sections=OLD_STYLE_SECTIONS) -> Path:
    source = root / "analysis" / "source"
    source.mkdir(parents=True)
    for name, text in OLD_STYLE_SOURCE.items():
        (source / name).write_text(text)
    (root / "analysis" / "outside.tex").write_text("\\section{Escape}\\label{sec:escape}\n")
    (root / "analysis" / "segments.json").write_text(json.dumps({"sections": sections, "nodes": [], "edges": []}))
    return root


def test_older_analyses_read_section_labels_from_their_stored_copy(tmp_path):
    workspace = _old_style_workspace(tmp_path)
    labels = analysis_graph.source_section_labels(workspace)
    assert labels == {
        "sec:intro": {"name": "Section 1", "number": "1"},
        "sec:prescribed": {"name": "Section 7", "number": "7"},
        "ssec:step": {"name": "Section 7.1", "number": "7.1"},
        "app:tech": {"name": "Appendix A", "number": "A"},
    }  # never a label further down a section, never a file outside the stored copy
    # Segments that record section labels already said everything: nothing is read.
    newer = _old_style_workspace(tmp_path / "newer", [{**OLD_STYLE_SECTIONS[0], "labels": []}])
    assert analysis_graph.source_section_labels(newer) == {}
    # The analysis graph of such a run names its sections when it is served.
    graph = analysis_graph.build({"sections": OLD_STYLE_SECTIONS, "nodes": [], "edges": []}, {"title": "T"},
                                 generated_at="2026-10-11T00:00:00Z")
    graph.pop("ref_labels")  # as written before 2026-10-12
    (workspace / "analysis" / "analysis_graph.json").write_text(json.dumps(graph))
    loaded = analysis_graph.load(workspace)
    assert loaded["ref_labels"]["sec:prescribed"] == {"name": "Section 7", "number": "7"}
    assert tex_text_refs("from 'the construction in Section~\\ref{sec:prescribed}'", analysis_graph.ref_labels(loaded)) == \
        "from 'the construction in Section 7'"


def test_a_symlinked_stored_file_is_never_read(tmp_path):
    workspace = _old_style_workspace(tmp_path)
    secret = tmp_path / "secret.tex"
    secret.write_text("\\section{Secret}\\label{sec:secret}\n")
    (workspace / "analysis" / "source" / "linked.tex").symlink_to(secret)
    sections = [{"level": "section", "title": "Secret", "number": "1", "file": "linked.tex", "line": 1}]
    (workspace / "analysis" / "segments.json").write_text(json.dumps({"sections": sections}))
    assert analysis_graph.source_section_labels(workspace) == {}
