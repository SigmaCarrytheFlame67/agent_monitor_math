"""Runs started as "informal only" skip the automatic Lean stages."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from agent_monitor import auto_pipeline, jobs, lean_verify


def test_runs_formalize_unless_started_informal_only() -> None:
    assert auto_pipeline.formalization_requested(None)
    assert auto_pipeline.formalization_requested({})
    assert auto_pipeline.formalization_requested({"formalize": True})
    assert not auto_pipeline.formalization_requested({"formalize": False})


def test_informal_only_batch_builds_only_the_informal_dag(tmp_path: Path) -> None:
    def informal(workspace, *_args):
        auto_pipeline._set_stage(workspace, "informal_dag", "done", detail="ready", outcome="success")

    record = {"run_id": "plain_informal", "engine": "plain", "formalize": False}
    with (
        patch.object(auto_pipeline, "_informal_worker", side_effect=informal) as informal_worker,
        patch.object(lean_verify, "generate") as generate,
    ):
        auto_pipeline._run_batch(tmp_path, record, None, "gpt-6-astra", "plain")

    informal_worker.assert_called_once()
    generate.assert_not_called()
    stages = auto_pipeline.load_state(tmp_path)["stages"]
    for name in ("lean", "formal_dag", "coverage"):
        assert stages[name]["status"] == "off"
        assert stages[name]["outcome"] == "not_run"
        assert "∎ Verify" in stages[name]["detail"]
    # Off is not a failure: the run's derived work is complete.
    assert auto_pipeline._aggregate_status(stages) == ("done", "not_run")
    assert auto_pipeline.formal_stages_off(tmp_path)


def test_start_shows_formal_stages_off_from_the_first_poll(tmp_path: Path) -> None:
    with (
        patch.dict(os.environ, {"AGENT_MONITOR_DISABLE_AUTO_PIPELINE": ""}),
        patch.object(auto_pipeline, "_run_record", return_value={"formalize": False}),
        patch.object(auto_pipeline, "_run"),
    ):
        state = auto_pipeline.start(
            run_id="plain_informal", workspace=tmp_path, owner_id=None, model=None, engine="plain"
        )

    assert state["formalize"] is False
    assert state["stages"]["informal_dag"]["status"] == "waiting"
    assert {state["stages"][name]["status"] for name in ("lean", "formal_dag", "coverage")} == {"off"}


def test_formalized_runs_are_not_reported_as_off(tmp_path: Path) -> None:
    auto_pipeline._write_state(tmp_path, {"stages": {"lean": {"status": "done"}}})
    assert not auto_pipeline.formal_stages_off(tmp_path)


def test_run_record_keeps_the_choice_when_a_runner_rewrites_it() -> None:
    jobs._write_run({"run_id": "plain_keep_choice", "engine": "plain", "status": "running", "formalize": False})
    jobs._write_run({"run_id": "plain_keep_choice", "engine": "plain", "status": "finished"})

    assert jobs._load_run_record("plain_keep_choice")["formalize"] is False
