"""Offline tests: the deterministic Analyze report (snapshot)."""
from __future__ import annotations

import os
from pathlib import Path

from agent_monitor.analyze import graph as ag
from agent_monitor.analyze import report

SNAPSHOT = Path(__file__).parent / "fixtures" / "analyze" / "report_snapshot.md"
SHA = "adc7f1241b42e322a6451854ab7e4b4c146bf78a"


def _seg(node_id, kind, number, *, proof=True, main=False, external=False, title=None):
    return {"id": node_id, "kind": kind, "env": kind, "display": kind.capitalize(), "number": number,
            "title": title, "labels": [node_id], "file": "sections/03-topology.tex", "lines": [461, 467],
            "proof_lines": [469, 536] if proof else None, "statement": f"Statement {number}.",
            "proof": "Proof." if proof else "", "has_proof": proof, "cites": [], "main_result": main,
            "external": external, "section": []}


def fixture():
    nodes = [
        _seg("def:brane", "definition", "2.1", proof=False),
        _seg("top:framed-source", "lemma", "3.2"),
        _seg("top:negative-doubles", "lemma", "3.7"),
        _seg("top:brane", "proposition", "3.8"),
        _seg("thm:main", "theorem", "1.1", main=True, title="Main theorem"),
        _seg("lem:unproved", "lemma", "4.1", proof=False),
        _seg("lem:late", "lemma", "5.2"),
    ]
    edges = [
        {"from": "def:brane", "to": "top:framed-source", "label": "uses", "provenance": "ref", "context": "statement"},
        {"from": "top:framed-source", "to": "top:negative-doubles", "label": "uses", "provenance": "ref", "context": "proof"},
        {"from": "top:negative-doubles", "to": "top:brane", "label": "uses", "provenance": "ref", "context": "proof"},
        {"from": "top:brane", "to": "thm:main", "label": "uses", "provenance": "ref", "context": "proof"},
        {"from": "lem:unproved", "to": "thm:main", "label": "uses", "provenance": "ref", "context": "proof"},
        {"from": "top:brane", "to": "lem:late", "label": "uses", "provenance": "inferred", "context": "proof",
         "basis": "document-order", "weak": True},
    ]
    results = {
        "top:framed-source": {"verdict": "checked", "verification_report": {"summary": "Fine.", "critical_errors": [], "gaps": []},
                              "external": [{"citation": "[EM12, Thm 1.1]", "statement_as_used": "cancellation",
                                            "hypotheses_checked": "partly"}],
                              "checked_by": {"model": "kimi-k3", "route": "sponsored_kimi", "effort": "high"}},
        "top:negative-doubles": {"verdict": "error", "verification_report": {
            "summary": "Sign.", "critical_errors": [
                {"location": "eq. top:trace-signs", "issue": "reverse traces have sign -1 | not +1 <script>x</script>"},
                {"location": "last paragraph", "issue": "the count is -2m, see https://evil.example/x"}],
            "gaps": []}},
        "top:brane": {"verdict": "checked", "verification_report": {"summary": "", "critical_errors": [], "gaps": []}},
        "thm:main": {"verdict": "gap", "verification_report": {"summary": "", "critical_errors": [],
                                                              "gaps": [{"location": "Step 4", "issue": "uses an unstated bound"}]}},
        "lem:late": {"verdict": "not_checked", "reason": "Not checked: the model-call budget for this run was used"},
    }
    manuscript = {
        "title": "Algebraicity of Weil classes on split abelian eightfolds",
        "version": "September 18, 2026",
        "resolved_url": f"https://github.com/openai/math/tree/{SHA}/preprints/Weil-Paper",
        "canonical_id": f"github:openai/math@{SHA}:preprints/Weil-Paper",
        "license": "Apache-2.0",
        "format": "tex",
        "bundle_sha256": "0123456789abcdef" * 4,
        "warnings": ["Withdrawn by its authors on October 6, 2026; see the withdrawal notice"],
        "computational": ["verify/check.py"],
        "upstream": {"openai_math": {
            "withdrawn": True, "withdrawn_on": "October 6, 2026",
            "notice_url": "https://github.com/openai/math/blob/main/preprints/Weil-Paper/README.md",
            "family": "032", "family_title": "The rational Hodge conjecture for CM abelian varieties",
            "lean_note": "Upstream Lean (v4.34.1 + dependencies); not re-checked by this server",
        }},
    }
    graph = ag.build({"parser": "pylatexenc-2.11", "nodes": nodes, "edges": edges}, manuscript,
                     generated_at="2026-10-11T00:00:00Z",
                     file_base_url=f"https://github.com/openai/math/blob/{SHA}/preprints/Weil-Paper/build")
    ag.merge_annotations(graph, results)
    ag.rollup(graph)
    graph = ag.sanitize(graph)
    receipt = {"model": "kimi-k3", "route": "sponsored_kimi", "calls": 7, "parser": "pylatexenc-2.11",
               "finished_at": "2026-10-11T12:34:56Z", "degraded": ["budget"], "stopped": False}
    paper_map = {"summary": "The manuscript claims that every rational Weil class is algebraic."}
    return graph, manuscript, receipt, paper_map


def test_report_snapshot_and_determinism():
    graph, manuscript, receipt, paper_map = fixture()
    first = report.render(graph, manuscript, receipt, paper_map=paper_map, formalize=True)
    assert first == report.render(graph, manuscript, receipt, paper_map=paper_map, formalize=True)
    if os.environ.get("UPDATE_ANALYZE_SNAPSHOT") == "1":
        SNAPSHOT.write_text(first, encoding="utf-8")
    assert first == SNAPSHOT.read_text(encoding="utf-8")


def test_report_escapes_model_text_and_states_the_caveats():
    graph, manuscript, receipt, paper_map = fixture()
    text = report.render(graph, manuscript, receipt, paper_map=paper_map)
    assert "<script>" not in text and "&lt;script&gt;" in text
    assert "https://evil.example" not in text and "[link removed]" in text
    assert "sign -1 \\| not +1" in text
    assert "not a certificate" in text and "kimi-k3 via sponsored_kimi" in text
    assert "Checked **4 of 5**" in text
    # Its own verdict is a gap, but the closure is the error two steps up.
    assert "own verdict: gap; with everything it depends on: **error**" in text
    assert "blocked by Lemma 3.7 → Proposition 3.8 → Theorem 1.1" in text
    assert "the notice was not shown to the verifier" in text
    assert "No formalization was requested" in text
    assert "1 × Not checked: the model-call budget for this run was used" in text
    assert "1 × No proof was located by the parser" in text
    assert "No proof found" not in text


def test_model_text_never_becomes_a_link():
    graph, manuscript, receipt, paper_map = fixture()
    hostile = ("[the fix](//evil.example/x) [x](javascript:alert(1)) ![img](//evil.example/p.png) "
               "[a](HTTPS:/\\evil.example) [b] (data:text/html,x) <https://evil.example> javascript:alert(2)")
    node = next(node for node in graph["nodes"] if node["id"] == "top:negative-doubles")
    node["meta"]["issues"][0]["issue"] = hostile
    node["meta"]["notes"] = hostile
    paper_map = {"summary": hostile}
    manuscript = dict(manuscript, warnings=[hostile], title="Title ![t](//evil.example/t.png)")
    text = report.render(graph, manuscript, receipt, paper_map=paper_map)
    import re

    assert not re.search(r"\]\(", text.replace("](https://github.com/openai/math/blob/", ""))
    assert "javascript:" not in text and "data:text" not in text and "evil.example>" not in text
    assert "&lt;" in text
    # The report's own source links are still links.
    assert "](https://github.com/openai/math/blob/" in text


def test_uninterpretable_results_are_reported_as_such():
    graph, manuscript, receipt, paper_map = fixture()
    for node in graph["nodes"]:
        if node["id"] in ("top:brane", "thm:main"):
            node["meta"].update(verdict="unclear", interpreted=False, issues=[],
                                reason="The verifier output could not be interpreted")
    ag.rollup(graph)
    text = report.render(graph, manuscript, receipt, paper_map=paper_map)
    assert "Checked **2 of 5**" in text
    assert "| unclear | 0 |" in text and "| no usable verifier output | 2 |" in text
    assert "own verdict: unclear (no usable verifier output)" in text
    assert "2 × The verifier output could not be interpreted, so no judgement is recorded" in text
    assert "Every statement was checked" not in text
    findings = text.split("## Findings")[1].split("##")[0]
    assert "Proposition 3.8" not in findings and "Lemma 3.7" in findings


def test_model_located_statements_are_reported_without_a_closure():
    nodes = [_seg("lemma-1", "lemma", "1"), _seg("theorem-2", "theorem", "2", main=True)]
    edges = [{"from": "lemma-1", "to": "theorem-2", "label": "uses", "provenance": "model", "context": "proof"}]
    graph = ag.build({"parser": "text-1+model-segments", "nodes": nodes, "edges": edges}, {"title": "P"},
                     generated_at="2026-10-11T00:00:00Z")
    ag.merge_annotations(graph, {"lemma-1": {"verdict": "error", "verification_report": {
        "summary": "", "critical_errors": [{"location": "l", "issue": "wrong"}], "gaps": []}},
        "theorem-2": {"verdict": "checked", "verification_report": {"summary": "", "critical_errors": [], "gaps": []}}})
    ag.rollup(graph)
    text = report.render(ag.sanitize(graph), {"title": "P"}, {"model": "m", "route": "api_key", "calls": 2})
    assert "with everything it depends on" not in text
    assert "its dependencies were not established (the statements were located by the model)" in text
    assert "the model located the statements" in text
