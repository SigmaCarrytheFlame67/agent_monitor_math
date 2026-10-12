"""Tests for cgm.gateway — role gating + tool wiring over cgm.core.

The verify service is mocked (we replace ``server._verify``), so fact_submit is
exercised without a live verifier or codex. Config is read from the environment
at call time, so each test sets CGM_* around a temp project dir.

Runs standalone (``python -m cgm.gateway.tests.test_gateway``) and under pytest.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from cgm.core import ExploringGraph, FactGraph, GlobalMemory
from cgm.gateway import build_app, tools_for
from cgm.gateway import server


@contextmanager
def _env(**kv):
    """Temporarily set env vars (None deletes), restore after."""
    old = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def _mock_verify(verdict, repair_hints="", raise_exc=None):
    """Replace server._verify with a stub; restore after."""
    orig = server._verify

    def fake(statement, proof):
        if raise_exc is not None:
            raise raise_exc
        return {"verdict": verdict, "repair_hints": repair_hints,
                "verification_report": {"summary": "mock"}}

    server._verify = fake
    try:
        yield
    finally:
        server._verify = orig


def test_role_table():
    # main can never fabricate a fact
    assert "fact_submit" not in tools_for("main")
    assert "fact_revoke" in tools_for("main")
    # verifier is read-only: literature lookup ONLY
    assert tools_for("verifier") == ["search_arxiv_theorems"]
    # worker is the only role that can submit a fact
    assert "fact_submit" in tools_for("worker")
    # all three get the read view + literature grounding
    for r in ("worker", "main", "verifier"):
        assert "search_arxiv_theorems" in tools_for(r)
    # unknown / misconfigured role fails CLOSED to the read-only verifier set
    assert tools_for("nope") == tools_for("verifier")
    assert "fact_submit" not in tools_for("nope") and "gm_add" not in tools_for("nope")
    # build_app registers without error for every role
    for r in ("worker", "main", "verifier", "all"):
        assert build_app(r) is not None


def test_gm_and_fact_search_over_temp_project():
    with tempfile.TemporaryDirectory() as d, _env(
        CGM_PROJECT_DIR=d, CGM_AGENTS_ROOT=None, CGM_AUTHOR="tester"
    ):
        out = server.gm_add("plan", claim="reduce to q>=2", evidence="")
        assert out["kind"] == "plan" and out["id"]
        # a fresh plan/direction has no edge yet: the reply says so and tells the
        # worker to graph_link it; other kinds carry no such hint
        assert out["unlinked"] is True and "graph_link" in out["hint"]
        rep = server.gm_add("report", claim="cycle 2 done; hand-off to high2", evidence="")
        assert rep["kind"] == "report" and "unlinked" not in rep and "hint" not in rep
        hits = server.gm_search("reduce")
        assert hits["results_by_kind"]["plan"]["count"] == 1
        # fact_search over an empty graph is well-formed
        assert server.fact_search("anything")["results"] == []


def test_fact_submit_accept_writes_fact_and_traces():
    with tempfile.TemporaryDirectory() as d, _env(
        CGM_PROJECT_DIR=d, CGM_AGENTS_ROOT=None, CGM_AUTHOR="worker_high",
        CGM_VERIFY_URL="http://mock", CGM_PROBLEM_ID="P",
    ), _mock_verify("correct"):
        res = server.fact_submit(statement="S(n)=n^2", proof="induction; QED")
        assert res["accepted"] is True and res["fact_id"]
        # the fact really landed in the graph
        fg = FactGraph(Path(d))
        assert fg.exists(res["fact_id"])
        # a verification trace was always written to global memory
        gm = GlobalMemory(Path(d))
        traces = gm.read("verification")
        assert traces and traces[-1]["verdict"] == "correct"
        assert traces[-1]["fact_id"] == res["fact_id"]


def test_fact_submit_reject_writes_nothing_but_traces():
    with tempfile.TemporaryDirectory() as d, _env(
        CGM_PROJECT_DIR=d, CGM_AGENTS_ROOT=None, CGM_AUTHOR="worker_high",
        CGM_VERIFY_URL="http://mock", CGM_PROBLEM_ID="P",
    ), _mock_verify("wrong", repair_hints="gap in step 2"):
        res = server.fact_submit(statement="bad", proof="hand-wave")
        assert res["accepted"] is False and res["repair_hints"] == "gap in step 2"
        fg = FactGraph(Path(d))
        assert fg.list() == []  # nothing written
        gm = GlobalMemory(Path(d))
        assert gm.read("verification")[-1]["verdict"] == "wrong"  # but traced


def test_fact_submit_verify_error_is_clean():
    with tempfile.TemporaryDirectory() as d, _env(
        CGM_PROJECT_DIR=d, CGM_AGENTS_ROOT=None, CGM_AUTHOR="w",
        CGM_VERIFY_URL="http://mock", CGM_PROBLEM_ID="P",
    ), _mock_verify("correct", raise_exc=RuntimeError("service down")):
        res = server.fact_submit(statement="s", proof="p")
        assert res["accepted"] is False and res["verdict"] == "error"
        assert "service down" in res["error"]


def test_fact_submit_revoked_predecessor_skips_verifier():
    with tempfile.TemporaryDirectory() as d, _env(
        CGM_PROJECT_DIR=d, CGM_AGENTS_ROOT=None, CGM_AUTHOR="worker_high",
        CGM_VERIFY_URL="http://mock", CGM_PROBLEM_ID="P",
    ):
        fg = FactGraph(Path(d))
        base = fg.add(problem_id="P", author="w", statement="A holds", proof="pf A")
        fg.revoke(base, reason="A was wrong")
        with patch.object(server, "_verify") as verify:
            res = server.fact_submit(statement="B from A", proof="uses A", predecessors=[base])
        verify.assert_not_called()
        assert res["accepted"] is False and res["verdict"] == "error"
        assert res["error"] == f"predecessor_revoked: {base}"
        assert res["repair_hints"]
        assert fg.list() == []
        assert GlobalMemory(Path(d)).read("verification") == []


def test_fact_submit_accept_but_write_failed_still_traces():
    # A predecessor revoked DURING verification is still rejected at write time.
    with tempfile.TemporaryDirectory() as d, _env(
        CGM_PROJECT_DIR=d, CGM_AGENTS_ROOT=None, CGM_AUTHOR="worker_high",
        CGM_VERIFY_URL="http://mock", CGM_PROBLEM_ID="P",
    ), _mock_verify("correct"):
        fg = FactGraph(Path(d))
        base = fg.add(problem_id="P", author="w", statement="A holds", proof="pf A")
        original_verify = server._verify

        def revoke_during_verify(statement, proof):
            fg.revoke(base, reason="A was wrong")
            return original_verify(statement, proof)

        with patch.object(server, "_verify", side_effect=revoke_during_verify) as verify:
            res = server.fact_submit(statement="B from A", proof="uses A", predecessors=[base])
        verify.assert_called_once()
        assert fg.list() == []
        assert res["accepted"] is True and res["fact_id"] is None and res["write_error"]
        # verdict:correct is STILL traced even though the write failed
        assert GlobalMemory(Path(d)).read("verification")[-1]["verdict"] == "correct"


def test_fact_submit_glossary_check_never_blocks():
    # a raising undefined_symbols must not block submission (advisory heuristic)
    orig = FactGraph.undefined_symbols

    def boom(self, **kw):
        raise RuntimeError("glossary heuristic bug")

    FactGraph.undefined_symbols = boom
    try:
        with tempfile.TemporaryDirectory() as d, _env(
            CGM_PROJECT_DIR=d, CGM_AGENTS_ROOT=None, CGM_AUTHOR="w",
            CGM_VERIFY_URL="http://mock", CGM_PROBLEM_ID="P",
        ), _mock_verify("correct"):
            res = server.fact_submit(statement="X thing", proof="because")
            assert res["accepted"] is True and res["undefined_symbols"] == []
    finally:
        FactGraph.undefined_symbols = orig


def test_fact_submit_nondict_verify_body_is_clean():
    # a valid-JSON but non-dict verify response must not crash the gate
    with tempfile.TemporaryDirectory() as d, _env(
        CGM_PROJECT_DIR=d, CGM_AGENTS_ROOT=None, CGM_AUTHOR="w",
        CGM_VERIFY_URL="http://mock", CGM_PROBLEM_ID="P",
    ):
        orig = server._verify
        server._verify = lambda statement, proof: ["not", "a", "dict"]
        try:
            res = server.fact_submit(statement="s", proof="p")
            assert res["accepted"] is False and res["verdict"] == "error"
            assert "non-dict" in res["error"]
            assert FactGraph(Path(d)).list() == []  # nothing written
        finally:
            server._verify = orig


def test_role_env_default_and_build_app():
    # build_app(None) reads CGM_ROLE (server._role) — exercises the env branch
    with _env(CGM_ROLE="worker"):
        assert server._role() == "worker"
        app = build_app()  # role=None -> defaults to _role() (env)
        assert app is not None
    with _env(CGM_ROLE=None):
        assert server._role() == "verifier"  # unset falls back read-only (fail-closed)


def test_project_by_name_without_agents_root_raises():
    # a project name is given but CGM_AGENTS_ROOT is unset -> RuntimeError
    with _env(CGM_AGENTS_ROOT=None, CGM_PROJECT_DIR="/tmp/whatever"):
        try:
            server._project("proj_a")
            assert False, "should require CGM_AGENTS_ROOT to resolve by name"
        except RuntimeError as e:
            assert "CGM_AGENTS_ROOT" in str(e)


def test_verify_http_roundtrip_and_errors():
    # exercise the REAL _verify (local HTTP, offline-safe on 127.0.0.1)
    import http.server
    import threading

    captured = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            captured["body"] = self.rfile.read(n).decode("utf-8")
            captured["ctype"] = self.headers.get("Content-Type")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"verdict": "correct", "verification_report": {"ok": true}}')

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/verify"
    try:
        # not set -> RuntimeError
        with _env(CGM_VERIFY_URL=None):
            try:
                server._verify("s", "p")
                assert False, "should raise when CGM_VERIFY_URL unset"
            except RuntimeError as e:
                assert "CGM_VERIFY_URL" in str(e)
        # a real POST round-trip; the body is the JSON we sent
        with _env(CGM_VERIFY_URL=url, CGM_VERIFY_TIMEOUT="5"):
            out = server._verify("S(n)=n^2", "induction")
            assert out["verdict"] == "correct"
        assert '"statement": "S(n)=n^2"' in captured["body"]
        assert captured["ctype"] == "application/json"
        # a garbage timeout falls back to the default (no crash)
        with _env(CGM_VERIFY_URL=url, CGM_VERIFY_TIMEOUT="not-an-int"):
            assert server._verify("s", "p")["verdict"] == "correct"
    finally:
        srv.shutdown()


def test_fact_revoke_cascades():
    with tempfile.TemporaryDirectory() as d, _env(
        CGM_PROJECT_DIR=d, CGM_AGENTS_ROOT=None, CGM_AUTHOR="main_agent",
    ):
        fg = FactGraph(Path(d))
        base = fg.add(problem_id="P", author="w", statement="A holds", proof="pf A")
        child = fg.add(problem_id="P", author="w", statement="B from A", proof="uses A",
                       predecessors=[base])
        out = server.fact_revoke(base, reason="A was wrong")
        assert set(out["revoked"]) == {base, child}
        assert not fg.exists(base) and not fg.exists(child)


def test_search_arxiv_theorems_delegates(monkeypatch=None):
    # the tool is a thin wrapper over cgm.integrations.search; stub it (offline)
    orig = server._arxiv_search
    server._arxiv_search = lambda query, num_results=10: {
        "query": query, "num_results": num_results, "results": [{"title": "T"}]}
    try:
        out = server.search_arxiv_theorems("Beatty sequence", num_results=3)
        assert out["query"] == "Beatty sequence" and out["num_results"] == 3
        assert out["results"] == [{"title": "T"}]
    finally:
        server._arxiv_search = orig


def test_project_resolution_by_name_and_validation():
    with tempfile.TemporaryDirectory() as root:
        (Path(root) / "proj_a").mkdir()
        with _env(CGM_AGENTS_ROOT=root, CGM_PROJECT_DIR=None, CGM_AUTHOR="main_agent"):
            # main addresses a project by name
            out = server.gm_add("master_guidance", claim="try route X", evidence="", project="proj_a")
            assert out["id"]
            assert GlobalMemory(Path(root) / "proj_a").read("master_guidance")
            # path-escape / bad names are rejected
            for bad in ("../evil", "a/b", "", "/abs"):
                try:
                    server.gm_search("x", project=bad)
                    assert False, f"should reject project name {bad!r}"
                except RuntimeError:
                    pass
            # unknown project rejected
            try:
                server.gm_search("x", project="missing")
                assert False, "should reject unknown project"
            except RuntimeError:
                pass


def test_main_module_builds_and_runs():
    # `python -m cgm.gateway` builds an app from CGM_ROLE and calls .run();
    # stub FastMCP.run so no stdio server actually starts.
    import runpy
    from mcp.server.fastmcp import FastMCP

    orig_run = FastMCP.run
    calls = {"n": 0}
    FastMCP.run = lambda self, *a, **k: calls.__setitem__("n", calls["n"] + 1)
    try:
        with _env(CGM_ROLE="verifier"):
            runpy.run_module("cgm.gateway", run_name="__main__")
        assert calls["n"] == 1
    finally:
        FastMCP.run = orig_run


def test_exploring_graph_tools_and_curator_role():
    # role gating: worker links + reads frontier but never annotates; curator
    # annotates + reads but never links/publishes; verifier gets none of them.
    assert "graph_link" in tools_for("worker") and "graph_frontier" in tools_for("worker")
    assert "graph_annotate" not in tools_for("worker")
    assert "graph_annotate" in tools_for("curator") and "graph_frontier" in tools_for("curator")
    for absent in ("graph_link", "gm_add", "fact_submit", "fact_revoke"):
        assert absent not in tools_for("curator")
    assert "graph_frontier" in tools_for("main") and "graph_link" not in tools_for("main")
    for t in ("graph_link", "graph_annotate", "graph_frontier"):
        assert t not in tools_for("verifier")

    with tempfile.TemporaryDirectory() as d, _env(
        CGM_PROJECT_DIR=d, CGM_AGENTS_ROOT=None, CGM_AUTHOR="tester"
    ):
        plan = server.gm_add("plan", claim="route via duality", evidence="")["id"]
        # worker declares a typed edge alongside the finding
        edge = server.graph_link(plan, "goal:main", "addresses",
                                 rationale="plan targets the project goal")
        assert edge["id"] and edge["type"] == "addresses"
        # unknown edge type is rejected loudly
        try:
            server.graph_link(plan, "goal:main", "causes")
            assert False, "should reject unknown edge type"
        except ValueError:
            pass
        # unannotated plan appears on the frontier as open/unscored
        rows = server.graph_frontier()["frontier"]
        assert [r["node_id"] for r in rows] == [plan]
        assert rows[0]["status"] == "open" and rows[0]["score"] is None
        # curator annotates; fold shows up in the frontier
        server.graph_annotate(plan, status="active", score=0.7,
                              comment="bridge fact landed", evidence_considered=["f1"])
        rows = server.graph_frontier()["frontier"]
        assert rows[0]["score"] == 0.7 and rows[0]["status"] == "active"
        assert rows[0]["stale"] is False
        # a newer edge flips staleness
        server.graph_link("ce1", plan, "refutes", rationale="counterexample to step 2")
        assert server.graph_frontier()["frontier"][0]["stale"] is True
        # dead nodes leave the frontier
        server.graph_annotate(plan, status="dead", score=0.0, comment="killed by ce1")
        assert server.graph_frontier()["frontier"] == []


# --------------------------------------------------------------------------- #
# ABLATION: self-verify gate (CGM_SELF_VERIFY_GATE)                         #
# --------------------------------------------------------------------------- #

def test_selfverify_accept_writes_fact_and_flags_the_gate():
    """A 'correct' self-verdict writes the fact and traces gate='self_verify'
    WITHOUT the verify service ever being called."""
    called = []
    orig = server._verify
    server._verify = lambda s, p: called.append(1)
    try:
        with tempfile.TemporaryDirectory() as d, _env(
            CGM_PROJECT_DIR=d, CGM_AGENTS_ROOT=None, CGM_AUTHOR="worker_high",
            CGM_VERIFY_URL="http://mock", CGM_PROBLEM_ID="P",
        ):
            res = server.fact_submit_selfverify(
                statement="S(n)=n^2", proof="induction; QED",
                self_verdict="correct", self_review_report="x" * 250)
            assert res["accepted"] is True and res["fact_id"]
            assert FactGraph(Path(d)).exists(res["fact_id"])
            tr = GlobalMemory(Path(d)).read("verification")[-1]
            assert tr["verdict"] == "correct" and tr["gate"] == "self_verify"
            assert called == []  # the independent verifier is never consulted
    finally:
        server._verify = orig


def test_selfverify_wrong_writes_nothing_but_traces():
    with tempfile.TemporaryDirectory() as d, _env(
        CGM_PROJECT_DIR=d, CGM_AGENTS_ROOT=None, CGM_AUTHOR="w",
        CGM_VERIFY_URL="http://mock", CGM_PROBLEM_ID="P",
    ):
        res = server.fact_submit_selfverify(
            statement="bad", proof="hand-wave",
            self_verdict="wrong", self_review_report="y" * 250)
        assert res["accepted"] is False and res["verdict"] == "wrong"
        assert FactGraph(Path(d)).list() == []
        assert GlobalMemory(Path(d)).read("verification")[-1]["verdict"] == "wrong"


def test_selfverify_rejects_bad_verdict_and_missing_report():
    with tempfile.TemporaryDirectory() as d, _env(
        CGM_PROJECT_DIR=d, CGM_AGENTS_ROOT=None, CGM_AUTHOR="w",
        CGM_VERIFY_URL="http://mock", CGM_PROBLEM_ID="P",
    ):
        bad = server.fact_submit_selfverify(
            statement="s", proof="p", self_verdict="probably",
            self_review_report="z" * 250)
        assert bad["accepted"] is False and bad["verdict"] == "error"
        short = server.fact_submit_selfverify(
            statement="s", proof="p", self_verdict="correct", self_review_report="ok")
        assert short["accepted"] is False and short["verdict"] == "error"
        # neither wrote anything, and neither left a verdict trace
        assert FactGraph(Path(d)).list() == []
        assert GlobalMemory(Path(d)).read("verification") == []


def test_selfverify_switch_swaps_the_tool_and_default_is_untouched():
    with _env(CGM_SELF_VERIFY_GATE="1", CGM_FACT_TEXT_MODE=None):
        assert server._self_verify_gate() is True
        assert build_app("worker") is not None
    with _env(CGM_SELF_VERIFY_GATE=None, CGM_FACT_TEXT_MODE=None):
        assert server._self_verify_gate() is False
    # text mode wins when both are set (fact store swap is the outer variable)
    with _env(CGM_SELF_VERIFY_GATE="1", CGM_FACT_TEXT_MODE="1"):
        assert build_app("worker") is not None


def main() -> None:
    test_role_table()
    print("  [ok] role table (main no fact_submit; verifier read-only; worker submits)")
    test_exploring_graph_tools_and_curator_role()
    print("  [ok] exploring graph: role gating + link/annotate/frontier round-trip")
    test_role_env_default_and_build_app()
    print("  [ok] build_app reads CGM_ROLE; _role default")
    test_project_by_name_without_agents_root_raises()
    print("  [ok] project-by-name without CGM_AGENTS_ROOT -> RuntimeError")
    test_verify_http_roundtrip_and_errors()
    print("  [ok] _verify HTTP round-trip + unset-URL + bad-timeout fallback")
    test_fact_revoke_cascades()
    print("  [ok] fact_revoke cascades to descendants")
    test_search_arxiv_theorems_delegates()
    print("  [ok] search_arxiv_theorems delegates to integrations.search")
    test_main_module_builds_and_runs()
    print("  [ok] python -m cgm.gateway builds app + calls run()")
    test_gm_and_fact_search_over_temp_project()
    print("  [ok] gm_add / gm_search / fact_search over a temp project")
    test_fact_submit_accept_writes_fact_and_traces()
    print("  [ok] fact_submit accept -> writes fact + verification trace")
    test_fact_submit_reject_writes_nothing_but_traces()
    print("  [ok] fact_submit reject -> writes nothing, still traces")
    test_fact_submit_verify_error_is_clean()
    print("  [ok] fact_submit verify-error -> clean error, no verdict")
    test_fact_submit_revoked_predecessor_skips_verifier()
    test_fact_submit_accept_but_write_failed_still_traces()
    print("  [ok] fact_submit accept-but-write-failed -> fact_id None + write_error, still traces correct")
    test_fact_submit_glossary_check_never_blocks()
    print("  [ok] fact_submit glossary heuristic never blocks submission")
    test_fact_submit_nondict_verify_body_is_clean()
    print("  [ok] fact_submit non-dict verify body -> clean error, nothing written")
    test_project_resolution_by_name_and_validation()
    print("  [ok] project resolution by name + path-escape validation")
    test_selfverify_accept_writes_fact_and_flags_the_gate()
    print("  [ok] ABLATION self-verify accept -> fact written, verifier never called")
    test_selfverify_wrong_writes_nothing_but_traces()
    print("  [ok] ABLATION self-verify wrong -> nothing written, still traced")
    test_selfverify_rejects_bad_verdict_and_missing_report()
    print("  [ok] ABLATION self-verify rejects bad verdict / missing review report")
    test_selfverify_switch_swaps_the_tool_and_default_is_untouched()
    print("  [ok] ABLATION self-verify switch swaps fact_submit; default untouched")
    print("ALL GATEWAY TESTS PASSED")


if __name__ == "__main__":
    main()


# --------------------------------------------------------------------------- #
# promotion back-link (fact_submit closes the awareness -> truth loop)         #
# --------------------------------------------------------------------------- #

def test_fact_submit_backlinks_the_promoted_finding():
    """On accept with a source_id: the finding is stamped + advanced, and the
    promoted-to edge is declared. Verifiable kinds get `verified`; judgment
    kinds get `supported` (their lifecycle has no `verified`)."""
    with tempfile.TemporaryDirectory() as d, _env(
        CGM_PROJECT_DIR=d, CGM_AGENTS_ROOT=None, CGM_AUTHOR="worker_high",
        CGM_VERIFY_URL="http://mock", CGM_PROBLEM_ID="P",
        CGM_DISABLE_EXPLORING_GRAPH=None, CGM_FACT_TEXT_MODE=None,
    ), _mock_verify("correct"):
        gm = GlobalMemory(Path(d))
        src_conc = gm.append("conclusion", "S(n)=n^2 for all n", "checked to n=100", "worker_high")
        src_plan = gm.append("plan", "induct on n", "", "worker_high")

        res = server.fact_submit(statement="S(n)=n^2", proof="induction; QED",
                                 source_id=src_conc)
        assert res["promoted_from"] == src_conc and res["promoted_to_edge"] is True
        entry = [e for e in gm.read("conclusion") if e["id"] == src_conc][0]
        assert entry["status"] == "verified" and entry["fact_id"] == res["fact_id"]

        res2 = server.fact_submit(statement="T(n)=2n", proof="direct; QED",
                                  source_id=src_plan)
        plan = [e for e in gm.read("plan") if e["id"] == src_plan][0]
        assert plan["status"] == "supported" and plan["fact_id"] == res2["fact_id"]

        edges = ExploringGraph(Path(d)).edges(edge_type="promoted-to")
        assert {(e["src"], e["dst"]) for e in edges} == {
            (src_conc, res["fact_id"]), (src_plan, res2["fact_id"])}

        # resubmitting identical content must not duplicate the edge
        server.fact_submit(statement="S(n)=n^2", proof="induction; QED", source_id=src_conc)
        assert len(ExploringGraph(Path(d)).edges(edge_type="promoted-to")) == 2


def test_fact_submit_backlink_is_advisory():
    """A source_id that names nothing is reported, never raised — the fact is
    already written. A reject back-links nothing. With the overlay ablated off
    the status is still stamped, but no edge is written."""
    with tempfile.TemporaryDirectory() as d, _env(
        CGM_PROJECT_DIR=d, CGM_AGENTS_ROOT=None, CGM_AUTHOR="worker_high",
        CGM_VERIFY_URL="http://mock", CGM_PROBLEM_ID="P",
        CGM_DISABLE_EXPLORING_GRAPH=None, CGM_FACT_TEXT_MODE=None,
    ):
        gm = GlobalMemory(Path(d))
        src = gm.append("conclusion", "claim", "evidence", "worker_high")

        with _mock_verify("correct"):
            res = server.fact_submit(statement="A", proof="pa", source_id="finding:" + src)
        assert res["accepted"] is True and res["fact_id"]
        assert "promoted_from" not in res
        # the hint names the exact repair
        assert "pass it bare" in res["backlink_skipped"]

        with _mock_verify("wrong", repair_hints="gap"):
            server.fact_submit(statement="B", proof="pb", source_id=src)
        assert [e for e in gm.read("conclusion") if e["id"] == src][0]["status"] == "unverified"

    with tempfile.TemporaryDirectory() as d, _env(
        CGM_PROJECT_DIR=d, CGM_AGENTS_ROOT=None, CGM_AUTHOR="worker_high",
        CGM_VERIFY_URL="http://mock", CGM_PROBLEM_ID="P",
        CGM_DISABLE_EXPLORING_GRAPH="1", CGM_FACT_TEXT_MODE=None,
    ), _mock_verify("correct"):
        gm = GlobalMemory(Path(d))
        src = gm.append("conclusion", "claim", "evidence", "worker_high")
        res = server.fact_submit(statement="C", proof="pc", source_id=src)
        assert res["promoted_from"] == src and "promoted_to_edge" not in res
        assert [e for e in gm.read("conclusion") if e["id"] == src][0]["status"] == "verified"
        assert ExploringGraph(Path(d)).edges(edge_type="promoted-to") == []


def test_fact_submit_selfverify_backlinks_too():
    """The ablation variant records the promotion identically — the only
    variable in that ablation is who judges."""
    with tempfile.TemporaryDirectory() as d, _env(
        CGM_PROJECT_DIR=d, CGM_AGENTS_ROOT=None, CGM_AUTHOR="worker_high",
        CGM_PROBLEM_ID="P", CGM_DISABLE_EXPLORING_GRAPH=None,
        CGM_FACT_TEXT_MODE=None,
    ):
        gm = GlobalMemory(Path(d))
        src = gm.append("conclusion", "claim", "evidence", "worker_high")
        res = server.fact_submit_selfverify(
            statement="S", proof="p", self_verdict="correct",
            self_review_report="x" * 250, source_id=src)
        assert res["promoted_from"] == src
        assert [e for e in gm.read("conclusion") if e["id"] == src][0]["fact_id"] == res["fact_id"]


# --------------------------------------------------------------------------- #
# fact_neighbors (one graph hop out from a verified fact)                      #
# --------------------------------------------------------------------------- #

def test_fact_neighbors_default_and_optin():
    with tempfile.TemporaryDirectory() as d, _env(
        CGM_PROJECT_DIR=d, CGM_AGENTS_ROOT=None, CGM_AUTHOR="worker_high",
        CGM_DISABLE_EXPLORING_GRAPH=None, CGM_FACT_TEXT_MODE=None,
    ):
        fg = FactGraph(Path(d))
        a = fg.add(problem_id="P", author="w", statement="Base lemma A.", proof="pa")
        b = fg.add(problem_id="P", author="w", statement="Builds on A.", proof="pb",
                   predecessors=[a])
        c = fg.add(problem_id="P", author="w", statement="Sibling via goal.", proof="pc")
        xg = ExploringGraph(Path(d))
        xg.add_edge(src=b, dst="goal:g", edge_type="supports", author="w")
        xg.add_edge(src=c, dst="goal:g", edge_type="promoted-to", author="w")

        res = server.fact_neighbors(b)
        assert res["statement"] == "Builds on A."
        assert [p["fact_id"] for p in res["predecessors"]] == [a]
        assert res["predecessors"][0]["statement"] == "Base lemma A."
        via = {h["fact_id"]: h for h in res["via_exploring"]}
        assert c in via and "goal:g" in via[c]["path"]
        # dependents: count always present, bodies opt-in
        assert "dependents" not in res and res["dependent_count"] == 0
        res_a = server.fact_neighbors(a, include=["dependents"])
        assert res_a["dependent_count"] == 1
        assert [x["fact_id"] for x in res_a["dependents"]] == [b]

        assert "error" in server.fact_neighbors("feedfacefeedface")


def test_fact_neighbors_respects_ablation_and_limit():
    with tempfile.TemporaryDirectory() as d, _env(
        CGM_PROJECT_DIR=d, CGM_AGENTS_ROOT=None, CGM_AUTHOR="worker_high",
        CGM_DISABLE_EXPLORING_GRAPH="1", CGM_FACT_TEXT_MODE=None,
    ):
        fg = FactGraph(Path(d))
        preds = [fg.add(problem_id="P", author="w", statement=f"L{i}.", proof=f"p{i}")
                 for i in range(4)]
        top = fg.add(problem_id="P", author="w", statement="Top.", proof="pt",
                     predecessors=preds)
        res = server.fact_neighbors(top, limit=2)
        assert len(res["predecessors"]) == 2          # limit honoured
        assert "via_exploring" not in res             # overlay ablated off


# --------------------------------------------------------------------------- #
# lesson_add (deployment-level curator retrospectives)                         #
# --------------------------------------------------------------------------- #

def test_lesson_add_writes_stamped_deployment_record():
    with tempfile.TemporaryDirectory() as proj, tempfile.TemporaryDirectory() as dep, _env(
        CGM_PROJECT_DIR=proj, CGM_AGENTS_ROOT=None, CGM_AUTHOR="curator",
        CGM_LESSONS_FILE=str(Path(dep) / "lessons.jsonl"),
    ):
        r = server.lesson_add("pattern p", "evidence e", "adjustment a")
        assert r["recorded"] is True
        r2 = server.lesson_add("second", "e2", "a2")
        rows = [__import__("json").loads(l)
                for l in (Path(dep) / "lessons.jsonl").read_text().splitlines()]
        assert len(rows) == 2                                  # append-only
        assert rows[0]["author"] == "curator"
        assert rows[0]["source_project"] == Path(proj).name
        assert rows[0]["timestamp_utc"] and rows[0]["adjustment"] == "adjustment a"


def test_lesson_add_field_validation():
    with tempfile.TemporaryDirectory() as proj, tempfile.TemporaryDirectory() as dep, _env(
        CGM_PROJECT_DIR=proj, CGM_AGENTS_ROOT=None, CGM_AUTHOR="curator",
        CGM_LESSONS_FILE=str(Path(dep) / "lessons.jsonl"),
    ):
        assert "required" in server.lesson_add("", "e", "a")["error"]
        assert "required" in server.lesson_add("p", "  ", "a")["error"]
        assert "exceeds" in server.lesson_add("p" * 2001, "e", "a")["error"]
        assert not (Path(dep) / "lessons.jsonl").exists()       # nothing written


def test_lesson_add_role_gating():
    from cgm.gateway.roles import ROLE_TOOLS
    assert "lesson_add" in ROLE_TOOLS["curator"]
    for role in ("worker", "main", "verifier"):
        assert "lesson_add" not in ROLE_TOOLS[role]


# --------------------------------------------------------------------------- #
# memory_recall (cross-project memory)                                         #
# --------------------------------------------------------------------------- #

def _mk_project(root: Path, name: str, batch=None, dead_end=None, fact=None):
    p = root / name
    (p / "global_memory").mkdir(parents=True)
    meta = {"name": name}
    if batch:
        meta["batch"] = batch
    (p / "project.json").write_text(__import__("json").dumps(meta))
    if dead_end:
        GlobalMemory(p).append("dead_end", dead_end, "", "w", verifiable=False)
    if fact:
        FactGraph(p).add(problem_id=name, author="w", statement=fact, proof="pf")
    return p


def test_memory_recall_off_by_default_and_on_typo():
    with tempfile.TemporaryDirectory() as root, _env(
        CGM_PROJECT_DIR=str(_mk_project(Path(root), "cur")),
        CGM_AGENTS_ROOT=root, CGM_MEMORY_SCOPE=None,
    ):
        assert "off" in server.memory_recall("anything")["error"]
    with tempfile.TemporaryDirectory() as root, _env(
        CGM_PROJECT_DIR=str(_mk_project(Path(root), "cur")),
        CGM_AGENTS_ROOT=root, CGM_MEMORY_SCOPE="everything",   # typo -> off
    ):
        assert "error" in server.memory_recall("anything")


def test_memory_recall_scope_all_with_provenance():
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as dep:
        rp = Path(root)
        cur = _mk_project(rp, "cur", dead_end="my own dead end about widgets")
        _mk_project(rp, "other", dead_end="naive pairing merge is not monotone for widgets",
                    fact="Widget lemma: every widget has a co-widget.")
        with _env(CGM_PROJECT_DIR=str(cur), CGM_AGENTS_ROOT=root,
                  CGM_MEMORY_SCOPE="all",
                  CGM_LESSONS_FILE=str(Path(dep) / "l.jsonl")):
            server.lesson_add("over-scored widget assembly plans", "evidence", "cap at 0.7")
            r = server.memory_recall("widget pairing merge")
            assert r["memory_scope"] == "all"
            assert r["projects_searched"] == ["other"]          # never itself
            assert r["methodology"][0]["provenance"]["project"] == "other"
            assert all("my own" not in (m.get("claim") or "") for m in r["methodology"])
            f = server.memory_recall("co-widget lemma")["facts"][0]
            assert f["provenance"]["kind"] == "fact" and "re-verify" in f["note"]
            l = server.memory_recall("widget assembly scoring")["lessons"][0]
            assert l["provenance"]["kind"] == "lesson" and l["adjustment"] == "cap at 0.7"


def test_memory_recall_scope_batch_learning_curve():
    """batch scope: problem N sees earlier problems of ITS run and nothing
    historical (historical projects carry no / another batch label)."""
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as dep:
        rp = Path(root)
        cur = _mk_project(rp, "mem02", batch="fpb-mem")
        _mk_project(rp, "mem01", batch="fpb-mem", dead_end="mem-batch doodad trap")
        _mk_project(rp, "fpb01_orig", dead_end="historical doodad answer route")
        _mk_project(rp, "ab3_x", batch="ablation", dead_end="other-batch doodad")
        lf = Path(dep) / "l.jsonl"
        with _env(CGM_PROJECT_DIR=str(rp / "mem01"), CGM_AGENTS_ROOT=root,
                  CGM_MEMORY_SCOPE="all", CGM_LESSONS_FILE=str(lf),
                  CGM_AUTHOR="curator"):
            server.lesson_add("same-batch doodad lesson", "e", "a")
        import json as _j
        with lf.open("a") as f:
            f.write(_j.dumps({"source_project": "fpb01_orig", "pattern": "old doodad lesson",
                              "evidence": "e", "adjustment": "a", "timestamp_utc": "t"}) + "\n")
        with _env(CGM_PROJECT_DIR=str(cur), CGM_AGENTS_ROOT=root,
                  CGM_MEMORY_SCOPE="batch", CGM_LESSONS_FILE=str(lf)):
            r = server.memory_recall("doodad")
            assert r["memory_scope"] == "batch"
            assert r["projects_searched"] == ["mem01"]           # history invisible
            assert [m["provenance"]["project"] for m in r["methodology"]] == ["mem01"]
            assert [l["pattern"] for l in r["lessons"]] == ["same-batch doodad lesson"]
        # batch scope on an unlabelled project sees nothing (fail-closed)
        nolabel = _mk_project(rp, "stray")
        with _env(CGM_PROJECT_DIR=str(nolabel), CGM_AGENTS_ROOT=root,
                  CGM_MEMORY_SCOPE="batch", CGM_LESSONS_FILE=str(lf)):
            assert server.memory_recall("doodad")["projects_searched"] == []


def test_memory_recall_role_gating():
    from cgm.gateway.roles import ROLE_TOOLS
    for role in ("worker", "main"):
        assert "memory_recall" in ROLE_TOOLS[role]
    for role in ("curator", "verifier"):
        assert "memory_recall" not in ROLE_TOOLS[role]
