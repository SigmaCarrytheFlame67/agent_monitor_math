"""fact_neighbors' core halves: the DAG reverse index and the overlay hop."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from cgm.core import ExploringGraph, FactGraph


def _graph(d):
    """a ← b ← c, plus d citing a: a's dependents are {b, d}."""
    fg = FactGraph(Path(d))
    a = fg.add(problem_id="P", author="w", statement="A", proof="pa")
    b = fg.add(problem_id="P", author="w", statement="B", proof="pb", predecessors=[a])
    c = fg.add(problem_id="P", author="w", statement="C", proof="pc", predecessors=[b])
    e = fg.add(problem_id="P", author="w", statement="D", proof="pd", predecessors=[a])
    return fg, a, b, c, e


def test_neighbors_both_directions():
    with tempfile.TemporaryDirectory() as d:
        fg, a, b, c, e = _graph(d)
        nb = fg.neighbors(a)
        assert nb["predecessors"] == []
        assert nb["dependents"] == sorted([b, e])
        nb = fg.neighbors(b)
        assert nb["predecessors"] == [a]
        assert nb["dependents"] == [c]
        # one hop only — c is a descendant of a but not a dependent
        assert c not in fg.neighbors(a)["dependents"]


def test_neighbors_unknown_id_raises():
    with tempfile.TemporaryDirectory() as d:
        fg, *_ = _graph(d)
        try:
            fg.neighbors("feedfacefeedface")
            assert False, "expected ValueError"
        except ValueError:
            pass


def test_fact_hops_via_shared_goal():
    """f1 —supports→ goal:g ←promoted-to— f2: two overlay steps, non-facts skipped."""
    with tempfile.TemporaryDirectory() as d:
        fg, a, b, c, e = _graph(d)
        xg = ExploringGraph(Path(d))
        xg.add_edge(src=a, dst="goal:g", edge_type="supports", author="w")
        xg.add_edge(src=b, dst="goal:g", edge_type="promoted-to", author="w")
        xg.add_edge(src="finding123", dst="goal:g", edge_type="addresses", author="w")
        xg.add_edge(src=a, dst=e, edge_type="depends-on", author="w")

        hops = xg.fact_hops(a, fg.exists)
        by = {h["fact_id"]: h for h in hops}
        assert set(by) == {b, e}                      # finding123 / goal:g are not facts
        assert by[b]["via"] == "goal:g"
        assert "supports" in by[b]["path"] and "promoted-to" in by[b]["path"]
        assert "via" not in by[e]                     # direct edge, one step
        # from the goal's other side the hop is symmetric
        assert a in {h["fact_id"] for h in xg.fact_hops(b, fg.exists)}


def test_fact_hops_dedup_and_cap():
    with tempfile.TemporaryDirectory() as d:
        fg, a, b, c, e = _graph(d)
        xg = ExploringGraph(Path(d))
        for g in ("goal:x", "goal:y"):                # b reachable via two goals
            xg.add_edge(src=a, dst=g, edge_type="supports", author="w")
            xg.add_edge(src=b, dst=g, edge_type="supports", author="w")
        hops = xg.fact_hops(a, fg.exists)
        assert [h["fact_id"] for h in hops].count(b) == 1
        assert xg.fact_hops(a, fg.exists, max_results=0) == []


def test_descendants_reads_each_fact_once_and_revoke_preserves_other_facts():
    with tempfile.TemporaryDirectory() as d:
        fg, a, b, c, e = _graph(d)
        shared = fg.add(problem_id="P", author="w", statement="Shared", proof="pf",
                        predecessors=[c, e, e])
        unrelated = fg.add(problem_id="P", author="w", statement="Unrelated", proof="pf")
        facts = fg.list()
        with patch.object(fg, "get_raw", wraps=fg.get_raw) as read:
            descendants = fg.descendants(a)
        assert set(descendants) == {b, c, e, shared}
        assert len(descendants) == 4  # shared paths and duplicate edges count once
        assert sorted(call.args[0] for call in read.call_args_list) == facts
        assert fg.descendants(shared) == []
        assert fg.descendants("unknown") == []
        assert set(fg.revoke(a, "invalid base")) == {a, b, c, e, shared}
        assert fg.list() == [unrelated]
        assert fg.descendants(a) == []  # no stale index after revocation
