"""The calibration exam's core: labelling, censoring, last-open score, AUC."""
from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "curator_calibration",
    Path(__file__).resolve().parents[3] / "scripts" / "curator-calibration.py")
cal = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cal)


def _write(project_dir: Path, anns):
    gm = project_dir / "global_memory"
    gm.mkdir(parents=True)
    with (gm / "annotations.jsonl").open("w") as f:
        for a in anns:
            f.write(json.dumps(a) + "\n")


def test_collect_labels_censoring_and_last_open_score(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _write(root / "p1", [
            # scored twice while open, then closed -> positive, feature = 0.7 (the LAST open score)
            {"node_id": "a", "status": "open",   "score": 0.3},
            {"node_id": "a", "status": "active", "score": 0.7},
            {"node_id": "a", "status": "closed", "score": 0.9},   # terminal score must NOT be the feature
            # scored then dead -> negative
            {"node_id": "b", "status": "open", "score": 0.8},
            {"node_id": "b", "status": "dead"},
            # still open at the end -> censored, excluded
            {"node_id": "c", "status": "open", "score": 0.5},
            # terminal but never scored while open -> excluded
            {"node_id": "d", "status": "closed"},
        ])
        monkeypatch.setattr(cal, "PROJECTS_ROOT", root)
        rows = cal.collect(["p1"])
        by = {r["node_id"]: r for r in rows}
        assert set(by) == {"a", "b"}
        assert by["a"]["label"] is True and by["a"]["score"] == 0.7
        assert by["b"]["label"] is False and by["b"]["score"] == 0.8


def test_auc_exact():
    mk = lambda s, l: {"score": s, "label": l}
    assert cal.auc([mk(0.9, True), mk(0.1, False)]) == 1.0    # perfect
    assert cal.auc([mk(0.1, True), mk(0.9, False)]) == 0.0    # inverted
    assert cal.auc([mk(0.5, True), mk(0.5, False)]) == 0.5    # tie counts half
    assert cal.auc([mk(0.5, True)]) is None                   # one class only


def test_summarize_shapes(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _write(root / "p1", [
            {"node_id": "goal:x", "status": "open", "score": 0.9},
            {"node_id": "goal:x", "status": "closed"},
            {"node_id": "goal:y", "status": "open", "score": 0.2},
            {"node_id": "goal:y", "status": "dead"},
        ])
        monkeypatch.setattr(cal, "PROJECTS_ROOT", root)
        s = cal.summarize(cal.collect(["p1"]))
        assert s["auc"] == 1.0 and s["n_closed"] == 1 and s["n_dead_blocked"] == 1
        assert s["by_kind"]["goal"]["n"] == 2
        assert sum(c["n"] for c in s["calibration_curve"]) == 2
        assert len(s["worst_calls"]) == 2
