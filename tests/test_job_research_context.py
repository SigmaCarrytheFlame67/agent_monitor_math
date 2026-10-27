"""A run whose research context cannot be read still runs, without it.

``_execute_job_body`` skips the context when the research database fails
(``sqlite3.Error``, an OS error, a bad value); ``sqlite3`` must be importable
there for that ``except`` clause to work at all.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import jobs


class ResearchContextFailureTests(unittest.TestCase):
    def _workspace(self, run_id: str, job_id: str) -> Path:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(jobs, "RUNS_DIR", root / "runs"))
        self.enterContext(patch.object(jobs, "CACHE_DIR", root / "cache"))
        workspace = root / "runs" / "workspaces" / run_id
        workspace.mkdir(parents=True)
        jobs._JOBS[job_id] = {"job_id": job_id, "run_id": run_id, "status": "running", "engine": "math_harness"}
        self.addCleanup(jobs._JOBS.pop, job_id, None)
        self.addCleanup(jobs._STOP_EVENTS.pop, run_id, None)
        jobs._stop_event(run_id).clear()
        return workspace

    def test_a_failing_research_database_is_skipped(self) -> None:
        for error in (sqlite3.OperationalError("database is locked"), OSError("unreadable"), ValueError("bad")):
            with self.subTest(error=type(error).__name__):
                run_id = f"math_harness_research_{type(error).__name__.lower()}"
                job_id = f"job-{run_id}"
                workspace = self._workspace(run_id, job_id)
                jobs._write_run({"run_id": run_id, "engine": "math_harness", "status": "running", "agents": []})
                seen: list[str] = []

                def engine(**kwargs) -> dict:
                    seen.append(str(kwargs.get("problem_text") or ""))
                    return {"run_id": run_id, "status": "finished", "agents": []}

                with (
                    patch("agent_monitor.research.compose_context", side_effect=error) as context,
                    patch.object(jobs, "_run_cli_engine_with_openclaude_retry", side_effect=engine),
                    patch.object(jobs, "_launch_pending_feedback"),
                    patch("agent_monitor.auto_pipeline.start"),
                ):
                    jobs._execute_job(
                        job_id=job_id, run_id=run_id, engine="math_harness", problem_id="p",
                        problem_text="Prove that 2 + 2 = 4.", problem_path=None, model=None,
                        max_iterations=1, workspace=str(workspace), owner_id=None, guest=False,
                    )
                self.assertTrue(context.called, "the research context was not consulted")
                self.assertEqual(jobs._JOBS[job_id]["status"], "finished", jobs._JOBS[job_id].get("error"))
                self.assertEqual(jobs._load_run_record(run_id)["status"], "finished")
                self.assertFalse((workspace / "dag_memory.md").exists())


if __name__ == "__main__":
    unittest.main()
