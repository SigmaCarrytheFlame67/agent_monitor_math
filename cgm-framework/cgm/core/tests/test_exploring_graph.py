"""Smoke tests for cgm.core.exploring_graph — the typed-edge + annotation
overlay. Offline, stdlib-only. Runs standalone
(``python -m cgm.core.tests.test_exploring_graph``) and under pytest.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from cgm.core import ExploringGraph, GlobalMemory
from cgm.core._util import read_jsonl


def test_edges_roundtrip_and_validation():
    with tempfile.TemporaryDirectory() as d:
        g = ExploringGraph(Path(d))
        e1 = g.add_edge(src="plan1", dst="goal:main", edge_type="addresses",
                        author="high", rationale="plan targets the main goal")
        e2 = g.add_edge(src="plan1", dst="sub1", edge_type="decomposes-into",
                        author="high", technique="localization", and_group="p1")
        assert e1 != e2 and len(e1) == 16
        # filtered reads
        assert len(g.edges()) == 2
        assert [e["id"] for e in g.edges(node="sub1")] == [e2]
        assert [e["id"] for e in g.edges(edge_type="addresses")] == [e1]
        assert g.edges(node="sub1")[0]["technique"] == "localization"
        # validation: unknown type / technique / empty node
        for bad in (
            dict(src="a", dst="b", edge_type="causes", author="x"),
            dict(src="a", dst="b", edge_type="decomposes-into", author="x",
                 technique="magic"),
            dict(src="", dst="b", edge_type="supports", author="x"),
        ):
            try:
                g.add_edge(**bad)
                raise AssertionError(f"expected ValueError for {bad}")
            except ValueError:
                pass


def test_annotations_fold_latest_wins():
    with tempfile.TemporaryDirectory() as d:
        g = ExploringGraph(Path(d))
        g.annotate("plan1", author="curator", status="open", score=0.4, comment="early")
        g.annotate("plan1", author="curator", status="active", score=0.8,
                   comment="bridge fact landed", evidence_considered=["f1"])
        latest = g.latest_annotations()
        assert latest["plan1"]["score"] == 0.8
        assert latest["plan1"]["status"] == "active"
        # validation: bad status / out-of-range score
        for bad in (dict(status="paused"), dict(score=1.5)):
            try:
                g.annotate("plan1", author="curator", **bad)
                raise AssertionError(f"expected ValueError for {bad}")
            except ValueError:
                pass


def test_frontier_ranking_status_filter_and_staleness():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        gm = GlobalMemory(root)
        g = ExploringGraph(root)
        p1 = gm.append("plan", claim="route A", evidence="", author="high")
        p2 = gm.append("plan", claim="route B", evidence="", author="high2")
        d1 = gm.append("direction", claim="try duality", evidence="", author="xhigh")
        dead = gm.append("plan", claim="dead route", evidence="", author="high3")

        g.annotate(p2, author="curator", status="active", score=0.9, comment="best")
        g.annotate(p1, author="curator", status="open", score=0.3, comment="meh")
        g.annotate(dead, author="curator", status="dead", score=0.0, comment="killed")

        rows = g.frontier(top_k=10)
        ids = [r["node_id"] for r in rows]
        assert dead not in ids                      # dead filtered out
        assert ids[0] == p2 and ids[1] == p1        # scored, best first
        assert ids[2] == d1                          # unscored ranks last
        assert rows[2]["status"] == "open"           # default when unannotated
        assert rows[0]["stale"] is False

        # a NEW edge touching p2 after its annotation flips stale on
        g.add_edge(src="ce9", dst=p2, edge_type="refutes", author="high",
                   rationale="counterexample hits route B's step 2")
        rows = g.frontier(top_k=10)
        byid = {r["node_id"]: r for r in rows}
        assert byid[p2]["stale"] is True
        assert byid[p1]["stale"] is False

        # top_k truncation + kind filter
        assert len(g.frontier(top_k=1)) == 1
        only_dirs = g.frontier(kinds=["direction"])
        assert [r["node_id"] for r in only_dirs] == [d1]

        # files live under global_memory/ (overlay discipline)
        assert (root / "global_memory" / "edges.jsonl").exists()
        assert (root / "global_memory" / "annotations.jsonl").exists()
        # append-only: two annotation events for p1+p2+dead each recorded
        assert len(read_jsonl(root / "global_memory" / "annotations.jsonl")) == 3


def test_frontier_unlinked_nodes_rank_last_until_linked():
    """A plan/direction no edge touches is ``unlinked`` and sorts after every
    linked node (scored or not); declaring an edge moves it into the ranking."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        gm = GlobalMemory(root)
        g = ExploringGraph(root)
        linked_unscored = gm.append("plan", claim="linked, not scored", evidence="", author="high")
        lonely_scored = gm.append("direction", claim="scored but edgeless", evidence="", author="high2")
        lonely = gm.append("direction", claim="status-report-shaped post", evidence="", author="high3")
        g.add_edge(src=linked_unscored, dst="goal:main", edge_type="addresses", author="high")
        g.annotate(lonely_scored, author="curator", status="open", score=0.95, comment="c")

        rows = g.frontier(top_k=10)
        by = {r["node_id"]: r for r in rows}
        assert by[linked_unscored]["unlinked"] is False
        assert by[lonely_scored]["unlinked"] is True and by[lonely]["unlinked"] is True
        # linked-but-unscored beats unlinked-but-scored; among unlinked, scored first
        assert [r["node_id"] for r in rows] == [linked_unscored, lonely_scored, lonely]
        # a `report` entry is not a frontier node at all
        gm.append("report", claim="cycle 3 done, verified as 0123456789abcdef", evidence="", author="high")
        assert len(g.frontier(top_k=10)) == 3
        # linking the lonely direction lifts it into the ranked part
        g.add_edge(src=lonely, dst=linked_unscored, edge_type="refines", author="high3")
        rows = g.frontier(top_k=10)
        assert {r["node_id"]: r["unlinked"] for r in rows}[lonely] is False
        assert [r["node_id"] for r in rows][-1] == lonely_scored     # the only unlinked one left


def main() -> None:
    test_edges_roundtrip_and_validation()
    print("  [ok] edges: round-trip, filters, type/technique/empty validation")
    test_annotations_fold_latest_wins()
    print("  [ok] annotations: append-only fold, latest wins, validation")
    test_frontier_ranking_status_filter_and_staleness()
    print("  [ok] frontier: ranking, dead filter, staleness, kinds, top_k")
    test_frontier_unlinked_nodes_rank_last_until_linked()
    print("  [ok] frontier: unlinked plan/direction rank last; report is not a node")
    print("ALL EXPLORING-GRAPH TESTS PASSED")


if __name__ == "__main__":
    main()


def test_frontier_includes_open_symbolic_goals():
    import tempfile
    from pathlib import Path
    from cgm.core import ExploringGraph
    with tempfile.TemporaryDirectory() as d:
        g = ExploringGraph(Path(d))
        g.annotate("goal:open-gap", author="curator", status="open", score=0.8, comment="unclaimed")
        g.annotate("goal:done", author="curator", status="closed", score=0.9)
        rows = g.frontier(top_k=10)
        by = {r["node_id"]: r for r in rows}
        assert "goal:open-gap" in by and by["goal:open-gap"]["kind"] == "goal"
        assert by["goal:open-gap"]["claim"] == "goal:open-gap"
        assert "goal:done" not in by                      # terminal states stay hidden
        # explicit kinds without "goal" excludes them (back-compat escape hatch)
        assert all(r["kind"] != "goal" for r in g.frontier(kinds=["plan", "direction"]))
        # staleness works on goal nodes too
        g.add_edge(src="goal:open-gap", dst="goal:other", edge_type="depends-on", author="w")
        assert {r["node_id"]: r for r in g.frontier()}["goal:open-gap"]["stale"] is True


if __name__ == "__main__" and "test_frontier_includes_open_symbolic_goals" in dir():
    pass
