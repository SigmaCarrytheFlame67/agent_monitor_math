"""Offline tests: the Analyze graph (roll-up, projection, sanitizer, blueprint)."""
from __future__ import annotations

import json
import os

import pytest

from agent_monitor import proof_bridge, proof_graph
from agent_monitor.analyze import graph as ag


def seg(node_id, kind="lemma", *, number=None, proof=True, main=False, external=False, statement=None):
    return {"id": node_id, "kind": kind, "env": kind, "display": kind.capitalize(), "number": number,
            "title": None, "labels": [node_id], "file": "main.tex", "lines": [1, 2], "proof_lines": [3, 4] if proof else None,
            "statement": statement or f"Statement of {node_id}.", "proof": "Proof text." if proof else "",
            "has_proof": proof, "cites": [{"key": "Bou21", "note": None, "title": None}], "main_result": main,
            "external": external, "section": []}


def edge(used, user, provenance="ref", *, weak=False):
    item = {"from": used, "to": user, "label": "uses", "provenance": provenance, "context": "proof", "strength": 3}
    if weak:
        item["weak"] = True
    return item


def make(nodes, edges, results=None):
    segments = {"parser": "pylatexenc-2.11", "nodes": nodes, "edges": edges, "macros": {"\\R": "\\mathbb{R}"}}
    manuscript = {"title": "Paper", "format": "tex", "canonical_id": "paste:x", "warnings": []}
    graph = ag.build(segments, manuscript, generated_at="2026-10-11T00:00:00Z",
                     file_base_url="https://github.com/o/r/blob/" + "a" * 40 + "/build")
    ag.merge_annotations(graph, results or {})
    return ag.rollup(graph)


def verdict(value, **extra):
    return {"verdict": value, "verification_report": {"summary": "", "critical_errors": [], "gaps": []}, **extra}


def meta(graph, node_id):
    return next(node["meta"] for node in graph["nodes"] if node["id"] == node_id)


@pytest.mark.parametrize(
    ("a", "b", "c", "closure_c"),
    [
        ("checked", "checked", "checked", "checked"),
        ("error", "checked", "checked", "error"),
        ("gap", "error", "checked", "error"),
        ("gap", "checked", "checked", "gap"),
        ("unclear", "checked", "checked", "unclear"),
        ("not_checked", "checked", "checked", "not_checked"),
        ("unclear", "not_checked", "checked", "unclear"),
        ("checked", "checked", "gap", "gap"),
        ("checked", "checked", "not_checked", "not_checked"),
    ],
)
def test_closure_truth_table(a, b, c, closure_c):
    graph = make([seg("a"), seg("b"), seg("c", "theorem", main=True)], [edge("a", "b"), edge("b", "c")],
                 {"a": verdict(a), "b": verdict(b), "c": verdict(c)})
    assert meta(graph, "c")["closure"] == closure_c
    assert graph["main_closure"] == closure_c
    if closure_c in ("error", "gap") and a == closure_c and c != closure_c:
        assert meta(graph, "c")["closure_path"] == ["a", "b", "c"]


def test_blocking_path_takes_the_nearest_cause():
    graph = make([seg("a"), seg("b"), seg("c", "theorem", main=True)], [edge("a", "b"), edge("b", "c")],
                 {"a": verdict("error"), "b": verdict("error"), "c": verdict("checked")})
    assert meta(graph, "c")["closure_path"] == ["b", "c"]
    assert meta(graph, "b")["closure_path"] == ["b"]


def test_definitions_are_neutral_and_cited_results_make_closures_conditional():
    nodes = [seg("def", "definition", proof=False), seg("ext", "theorem", proof=False, external=True),
             seg("conj", "conjecture", proof=False), seg("lem"), seg("main", "theorem", main=True)]
    edges = [edge("def", "lem"), edge("ext", "lem"), edge("conj", "main"), edge("lem", "main")]
    results = {"lem": verdict("checked", external=[{"citation": "[EM12, Thm 1.1]", "statement_as_used": "s",
                                                    "hypotheses_checked": "yes"}]),
               "main": verdict("checked")}
    graph = make(nodes, edges, results)
    main = meta(graph, "main")
    assert main["closure"] == "checked"
    assert main["closure_external"] == 2  # the cited theorem node and [EM12, Thm 1.1]
    assert main["closure_assumptions"] == ["conj"]
    assert meta(graph, "def")["closure"] is None and meta(graph, "def")["verdict"] == "not_checked"
    assert meta(graph, "lem")["relies_on_external"] is True
    assert graph["coverage"] == {"checked": 2, "total": 2, "uninterpretable": 0}
    assert meta(graph, "ext")["checkable"] is False and meta(graph, "ext")["external_result"] is True


@pytest.mark.parametrize("bridge", ["remark", "definition", "example", "notation", "question"])
@pytest.mark.parametrize(
    ("a", "t", "closure_t"),
    [
        ("error", "checked", "error"),
        ("gap", "checked", "gap"),
        ("unclear", "checked", "unclear"),
        ("not_checked", "checked", "not_checked"),
        ("checked", "checked", "checked"),
        ("checked", "gap", "gap"),
    ],
)
def test_statements_without_a_verdict_pass_the_closure_through(bridge, a, t, closure_t):
    # Lemma a is used by a remark (or definition, ...) whose statement cites
    # it; the theorem's proof cites the remark.  The remark has no verdict of
    # its own, but what it depends on still reaches the theorem.
    nodes = [seg("a"), seg("r", bridge, proof=False), seg("t", "theorem", main=True)]
    edges = [edge("a", "r"), edge("r", "t")]
    graph = make(nodes, edges, {"a": verdict(a), "t": verdict(t)})
    assert meta(graph, "r")["closure"] is None and meta(graph, "r")["verdict"] == "not_checked"
    assert meta(graph, "t")["closure"] == closure_t
    assert graph["main_closure"] == closure_t
    if closure_t in ("error", "gap") and a == closure_t and t == "checked":
        assert meta(graph, "t")["closure_path"] == ["a", "r", "t"]


def test_transparent_statements_carry_external_results_and_assumptions():
    nodes = [seg("ext", "theorem", proof=False, external=True), seg("conj", "conjecture", proof=False),
             seg("lem"), seg("rem", "remark", proof=False), seg("def", "definition", proof=False),
             seg("main", "theorem", main=True)]
    edges = [edge("ext", "rem"), edge("conj", "rem"), edge("lem", "def"), edge("rem", "main"), edge("def", "main")]
    results = {"lem": verdict("gap", verification_report={"summary": "", "critical_errors": [],
                                                          "gaps": [{"location": "x", "issue": "y"}]},
                              external=[{"citation": "[EM12, Thm 1]", "statement_as_used": "s",
                                         "hypotheses_checked": "yes"}]),
               "main": verdict("checked")}
    graph = make(nodes, edges, results)
    main = meta(graph, "main")
    assert main["closure"] == "gap" and main["closure_path"] == ["lem", "def", "main"]
    assert main["closure_external"] == 2  # the cited theorem (through the remark) and [EM12, Thm 1]
    assert main["closure_assumptions"] == ["conj"]
    assert meta(graph, "rem")["closure_assumptions"] == ["conj"]


def test_uninterpretable_output_is_not_counted_as_checked():
    garbled = {"verdict": "unclear", "interpreted": False, "reason": "The verifier output could not be interpreted",
               "verification_report": {"summary": "s", "critical_errors": [], "gaps": []}}
    graph = make([seg("a"), seg("b"), seg("m", "theorem", main=True)], [edge("a", "m"), edge("b", "m")],
                 {"a": garbled, "b": verdict("checked"), "m": dict(garbled)})
    assert graph["coverage"] == {"checked": 1, "total": 3, "uninterpretable": 2}
    assert meta(graph, "a")["interpreted"] is False and meta(graph, "b")["interpreted"] is True
    assert graph["counts"]["unclear"] == 2
    clean = ag.sanitize(graph)
    assert clean["coverage"] == {"checked": 1, "total": 3, "uninterpretable": 2}
    assert next(node for node in clean["nodes"] if node["id"] == "a")["meta"]["interpreted"] is False
    summary = ag.summary(clean, calls=3, model="m", route="api_key")
    assert summary["coverage"] == {"checked": 1, "total": 3, "uninterpretable": 2}


def test_model_located_statements_get_no_closure():
    segments = {"parser": "text-1+model-segments", "nodes": [seg("lemma-1"), seg("theorem-2", "theorem", main=True)],
                "edges": [edge("lemma-1", "theorem-2", "model")]}
    graph = ag.build(segments, {"title": "P"}, generated_at="2026-10-11T00:00:00Z")
    ag.merge_annotations(graph, {"lemma-1": verdict("error", verification_report={
        "summary": "", "critical_errors": [{"location": "l", "issue": "i"}], "gaps": []}),
        "theorem-2": verdict("checked")})
    ag.rollup(graph)
    assert graph["dependency_basis"] == "model"
    assert meta(graph, "theorem-2")["closure"] is None and graph["main_closure"] is None
    assert ag.sanitize(graph)["dependency_basis"] == "model"
    structural = make([seg("a"), seg("m", "theorem", main=True)], [edge("a", "m")], {"a": verdict("checked")})
    assert structural["dependency_basis"] == "structure"


def test_cycle_checks_stay_fast_on_dense_graphs():
    import time

    from agent_monitor.analyze.dag import Reachability

    reach = Reachability()
    started = time.monotonic()
    count = 256
    for later in range(count):
        for earlier in range(later):
            assert reach.add(earlier, later)
    assert time.monotonic() - started < 5
    assert reach.reaches(0, count - 1) and not reach.reaches(count - 1, 0)
    assert not reach.add(count - 1, 0) and not reach.add(5, 5 - 5)


def test_weak_and_model_edges_never_change_a_closure():
    graph = make([seg("a"), seg("b", main=True)], [edge("a", "b", "inferred", weak=True)],
                 {"a": verdict("error"), "b": verdict("checked", depends_on=[{"id": "a", "how": "x"}])})
    assert meta(graph, "b")["closure"] == "checked"
    model_edges = [item for item in graph["edges"] if item["provenance"] == "model"]
    assert model_edges == []  # an edge between them already exists (weak), none is duplicated
    graph = make([seg("a"), seg("b", main=True)], [], {"a": verdict("error"),
                                                       "b": verdict("checked", depends_on=[{"id": "a", "how": "x"}])})
    assert [(item["from"], item["to"]) for item in graph["edges"] if item["provenance"] == "model"] == [("a", "b")]
    assert meta(graph, "b")["closure"] == "checked"


def test_model_edges_that_would_close_a_cycle_are_refused():
    graph = make([seg("a"), seg("b")], [edge("a", "b")],
                 {"a": verdict("checked", depends_on=[{"id": "b", "how": "circular"}])})
    assert [(item["from"], item["to"]) for item in graph["edges"]] == [("a", "b")]


def test_annotations_and_counts():
    graph = make([seg("a"), seg("b", proof=False), seg("c", "remark", proof=False)], [],
                 {"a": verdict("gap", verification_report={"summary": "s", "critical_errors": [],
                                                           "gaps": [{"location": "Step 2", "issue": "skipped"}]},
                               checked_by={"model": "kimi-k3", "route": "sponsored_kimi", "effort": "high"},
                               notes="n", confidence="low", claim="restated")})
    a = meta(graph, "a")
    assert a["issues"] == [{"type": "gap", "location": "Step 2", "issue": "skipped"}]
    assert a["checked_by"]["route"] == "sponsored_kimi" and a["claim_restated"] == "restated"
    assert meta(graph, "b")["reason"] == "No proof was located by the parser"
    assert meta(graph, "c")["reason"] == "A remark, not a claim with a proof"
    assert graph["counts"] == {"checked": 0, "gap": 1, "error": 0, "unclear": 0, "not_checked": 2}
    assert meta(graph, "a")["source_ref"]["url"].endswith("/build/main.tex#L1-L4")


def test_projection_survives_the_console_sanitizer_and_cache_loader(tmp_path):
    nodes = [seg("def", "definition", proof=False), seg("lem-1"), seg("ext", "theorem", proof=False, external=True),
             seg("thm:main", "theorem", main=True, statement="A " * 1000)]
    graph = make(nodes, [edge("def", "lem-1"), edge("lem-1", "thm:main"), edge("ext", "thm:main"),
                         edge("lem-1", "ext", "inferred", weak=True)],
                 {"lem-1": verdict("checked"), "thm:main": verdict("error", verification_report={
                     "summary": "", "critical_errors": [{"location": "p", "issue": "i"}], "gaps": []})})
    envelope = ag.to_proof_graph(graph)
    assert envelope["model"] == "analyze-graph" and envelope["derivation"] == "deterministic"
    assert envelope["certificate"] is False and envelope["attested"] is False
    clean = proof_graph._sanitize(envelope["graph"])
    assert clean["nodes"] == envelope["graph"]["nodes"] and clean["edges"] == envelope["graph"]["edges"]
    kinds = {node["id"]: node["kind"] for node in clean["nodes"]}
    assert kinds == {"def": "definition", "lem-1": "lemma", "ext": "assumption", "thm:main": "conclusion"}
    assert len(clean["nodes"][3]["statement"]) == 1200
    assert ("lem-1", "ext") not in {(item["from"], item["to"]) for item in clean["edges"]}  # weak edges stay out
    (tmp_path / "proof_graph.json").write_text(json.dumps(envelope))
    loaded = proof_graph.load_cached(tmp_path)
    assert loaded is not None and loaded["model"] == "analyze-graph"
    assert loaded["graph"]["nodes"] == clean["nodes"]


def test_sanitize_bounds_and_repairs_hostile_graphs():
    raw = make([seg("a"), seg("b")], [edge("a", "b")], {"a": verdict("checked")})
    raw["nodes"].append({"id": "BAD ID", "kind": "lemma", "meta": {}})
    raw["nodes"].append({"id": "a", "kind": "lemma", "meta": {}})  # duplicate
    raw["nodes"][0]["meta"]["verdict"] = "proved"
    raw["nodes"][0]["meta"]["source_ref"]["url"] = "javascript:alert(1)"
    raw["nodes"][1]["statement"] = "x" * 20000
    raw["edges"].append(edge("b", "a"))  # would close a cycle
    raw["edges"].append({"from": "a", "to": "b", "label": "uses", "provenance": "magic"})
    raw["source"]["resolved_url"] = "http://insecure.example/x"
    clean = ag.sanitize(raw)
    assert [node["id"] for node in clean["nodes"]] == ["a", "b"]
    assert clean["nodes"][0]["meta"]["verdict"] == "not_checked"
    assert "url" not in clean["nodes"][0]["meta"]["source_ref"]
    assert len(clean["nodes"][1]["statement"]) == 8000
    assert [(item["from"], item["to"]) for item in clean["edges"]] == [("a", "b")]
    assert "resolved_url" not in clean["source"]
    with pytest.raises(ValueError):
        ag.sanitize({"schema": "something-else"})


def test_sanitize_keeps_references_over_weak_and_model_edges_that_close_a_cycle():
    raw = make([seg("t", "theorem", main=True), seg("l"), seg("p", "proposition")], [edge("l", "p"), edge("p", "t")])
    guess = edge("t", "l", "inferred", weak=True)
    guess["strength"] = 0
    proposal = edge("t", "p", "model")
    proposal["strength"] = 1
    raw["edges"][:0] = [guess, proposal]  # stored first, as an older parser could
    clean = ag.sanitize(raw)
    assert [(item["from"], item["to"]) for item in clean["edges"]] == [("l", "p"), ("p", "t")]


def test_load_refuses_links_oversize_and_garbage(tmp_path):
    graph = make([seg("a", main=True)], [], {"a": verdict("checked")})
    folder = tmp_path / "analysis"
    folder.mkdir()
    (folder / "analysis_graph.json").write_text(json.dumps(graph))
    assert ag.load(tmp_path)["nodes"][0]["id"] == "a"
    (folder / "analysis_graph.json").write_text("{not json")
    assert ag.load(tmp_path) is None
    (folder / "analysis_graph.json").unlink()
    outside = tmp_path / "elsewhere.json"
    outside.write_text(json.dumps(graph))
    os.symlink(outside, folder / "analysis_graph.json")
    assert ag.load(tmp_path) is None
    assert ag.load(tmp_path / "missing") is None


def test_lean_blueprint_is_topological_with_one_target_and_feeds_the_guidance():
    nodes = [seg("def", "definition", proof=False), seg("lem-a"), seg("lem-b"), seg("thm", "theorem", main=True)]
    graph = make(nodes, [edge("def", "lem-a"), edge("lem-a", "lem-b"), edge("lem-b", "thm"), edge("lem-a", "thm")],
                 {"lem-a": verdict("checked", claim="Self-contained A.")})
    facts = ag.lean_blueprint(graph, ["thm", "lem-a", "ghost"])
    assert [fact["fact_id"] for fact in facts] == ["def", "lem-a", "thm"]
    assert [fact["fact_id"] for fact in facts if fact["is_target"]] == ["thm"]
    assert facts[1]["statement"] == "Self-contained A." and facts[1]["predecessors"] == ["def"]
    assert facts[2]["predecessors"] == ["lem-a"]
    text = proof_bridge.guidance_from_facts(facts)
    assert "fact_id: thm  (TARGET" in text and "fact_id: lem-a" in text
    only_lemma = ag.lean_blueprint(graph, ["lem-b"])
    assert [fact["fact_id"] for fact in only_lemma if fact["is_target"]] == ["lem-b"]
    assert ag.lean_blueprint(graph, ["ghost"]) == []


def test_summary_block_for_the_run_record():
    graph = make([seg("a"), seg("m", "theorem", main=True)], [edge("a", "m")],
                 {"a": verdict("gap", verification_report={"summary": "", "critical_errors": [],
                                                          "gaps": [{"location": "x", "issue": "y"}]}),
                  "m": verdict("checked")})
    summary = ag.summary(graph, calls=3, model="kimi-k3", route="sponsored_kimi")
    assert summary == {"nodes": 2, "checked": 1, "gap": 1, "error": 0, "unclear": 0, "not_checked": 0,
                       "coverage": {"checked": 2, "total": 2, "uninterpretable": 0}, "main_results": ["m"],
                       "main_closure": "gap",
                       "calls": 3, "model": "kimi-k3", "route": "sponsored_kimi"}
