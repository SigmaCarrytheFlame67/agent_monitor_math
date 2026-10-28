"""Each turn's proof is kept (proof_turns), for one proof card per turn."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import jobs, proof_turns


class ProofTurnsModuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_a_turn_keeps_its_proof_as_it_was_when_it_ended(self) -> None:
        (self.ws / "proof.md").write_text("# Claim\n\nFirst proof.\n", encoding="utf-8")
        n = proof_turns.begin(self.ws, started_at="2026-10-28T10:00:00+00:00")
        self.assertEqual(n, 1)
        self.assertEqual(proof_turns.summary(self.ws)[0]["status"], "running")
        self.assertIsNone(proof_turns.summary(self.ws)[0]["finished_at"])
        proof_turns.end(self.ws, n, status="finished", finished_at="2026-10-28T10:05:00+00:00")
        # The next turn rewrites proof.md; the first turn's copy stays.
        (self.ws / "proof.md").write_text("# Claim\n\nSecond proof.\n", encoding="utf-8")
        m = proof_turns.begin(self.ws, started_at="2026-10-28T10:06:00+00:00")
        proof_turns.end(self.ws, m, status="failed", finished_at="2026-10-28T10:09:00+00:00")
        turns = proof_turns.summary(self.ws)
        self.assertEqual([t["n"] for t in turns], [1, 2])
        self.assertEqual([t["status"] for t in turns], ["finished", "failed"])
        self.assertEqual([t["file"] for t in turns], [".proof_turns/1.md", ".proof_turns/2.md"])
        self.assertEqual((self.ws / ".proof_turns" / "1.md").read_text(encoding="utf-8"), "# Claim\n\nFirst proof.\n")
        self.assertEqual((self.ws / ".proof_turns" / "2.md").read_text(encoding="utf-8"), "# Claim\n\nSecond proof.\n")
        self.assertEqual(turns[0]["chars"], len("# Claim\n\nFirst proof.\n"))
        self.assertEqual(turns[0]["finished_at"], "2026-10-28T10:05:00+00:00")

    def test_a_turn_ends_once(self) -> None:
        (self.ws / "proof.md").write_text("A\n", encoding="utf-8")
        n = proof_turns.begin(self.ws, started_at="t1")
        proof_turns.end(self.ws, n, status="finished", finished_at="t2")
        (self.ws / "proof.md").write_text("B\n", encoding="utf-8")
        proof_turns.end(self.ws, n, status="failed", finished_at="t3")
        turn = proof_turns.summary(self.ws)[0]
        self.assertEqual((turn["status"], turn["finished_at"]), ("finished", "t2"))
        self.assertEqual((self.ws / ".proof_turns" / "1.md").read_text(encoding="utf-8"), "A\n")

    def test_a_turn_without_a_proof_keeps_none_and_latex_is_kept_as_latex(self) -> None:
        n = proof_turns.begin(self.ws, started_at="t1")
        proof_turns.end(self.ws, n, status="failed", finished_at="t2")
        self.assertEqual(proof_turns.summary(self.ws)[0]["file"], "")
        (self.ws / "proof.md").write_text("   \n", encoding="utf-8")
        (self.ws / "proof.tex").write_text("\\begin{proof}x\\end{proof}", encoding="utf-8")
        m = proof_turns.begin(self.ws, started_at="t3")
        proof_turns.end(self.ws, m, status="finished", finished_at="t4")
        self.assertEqual(proof_turns.summary(self.ws)[1]["file"], ".proof_turns/2.tex")

    def test_a_turn_left_running_by_a_restart_is_interrupted_when_the_next_starts(self) -> None:
        proof_turns.begin(self.ws, started_at="t1")
        proof_turns.begin(self.ws, started_at="t2")
        turns = proof_turns.summary(self.ws)
        self.assertEqual([(t["n"], t["status"], t["finished_at"]) for t in turns], [(1, "interrupted", "t2"), (2, "running", None)])

    def test_no_workspace_or_a_bad_index_records_nothing_and_breaks_nothing(self) -> None:
        self.assertIsNone(proof_turns.begin(self.ws / "missing", started_at="t"))
        proof_turns.end(self.ws / "missing", 1, status="finished", finished_at="t")
        self.assertEqual(proof_turns.summary(None), [])
        (self.ws / ".proof_turns").mkdir()
        (self.ws / ".proof_turns" / "index.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(proof_turns.summary(self.ws), [])
        self.assertEqual(proof_turns.begin(self.ws, started_at="t"), 1)

    def test_the_index_keeps_the_latest_turns_only(self) -> None:
        with patch.object(proof_turns, "MAX_TURNS", 3):
            for i in range(5):
                n = proof_turns.begin(self.ws, started_at=f"s{i}")
                proof_turns.end(self.ws, n, status="finished", finished_at=f"f{i}")
        self.assertEqual([t["n"] for t in proof_turns.summary(self.ws)], [3, 4, 5])

    def test_a_symlinked_folder_is_never_written_through(self) -> None:
        outside = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.ws / ".proof_turns").symlink_to(outside, target_is_directory=True)
        self.assertIsNone(proof_turns.begin(self.ws, started_at="t"))
        self.assertEqual(list(outside.iterdir()), [])

    def test_the_folder_is_hidden_from_the_run_file_list(self) -> None:
        from agent_monitor import console_server

        (self.ws / "proof.md").write_text("A\n", encoding="utf-8")
        n = proof_turns.begin(self.ws, started_at="t1")
        proof_turns.end(self.ws, n, status="finished", finished_at="t2")
        paths = [f["path"] for f in console_server._list_workspace_files(self.ws)]
        self.assertEqual(paths, ["proof.md"])
        # ...but served by the workspace file endpoint, which the card reads.
        self.assertIsNotNone(console_server._workspace_target(self.ws, ".proof_turns/1.md"))


class ProofTurnsWorkerTest(unittest.TestCase):
    """A follow-up turn records itself, and ends before queued feedback starts."""

    def setUp(self) -> None:
        self.run_id = "kimi_proof_turns"
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(jobs, "RUNS_DIR", root / "runs"))
        self.enterContext(patch.object(jobs, "CACHE_DIR", root / "cache"))
        self.workspace = root / "runs" / "workspaces" / self.run_id
        self.workspace.mkdir(parents=True)
        (self.workspace / "proof.md").write_text("# Proof\n\nTurn one.\n", encoding="utf-8")
        self.addCleanup(jobs._STOP_EVENTS.pop, self.run_id, None)
        jobs._stop_event(self.run_id).clear()
        jobs._write_run({
            "run_id": self.run_id, "engine": "kimi", "problem_id": "adhoc", "status": "finished",
            "problem_text": "Prove it.", "problems": [{"text": "Prove it.", "source": "initial"}],
            "agents": [{"trace_id": f"{self.run_id}::turn-1", "output": "first", "status": "finished"}],
            "edges": [],
        })

    def _follow_up(self, engine, launched) -> None:
        from agent_monitor import auto_pipeline

        job_id = "job_turn_2"
        jobs._JOBS[job_id] = {"job_id": job_id, "run_id": self.run_id, "status": "running", "engine": "kimi"}
        self.addCleanup(jobs._JOBS.pop, job_id, None)
        with (
            patch.object(jobs, "_run_cli_engine_with_openclaude_retry", side_effect=engine),
            patch.object(jobs, "_launch_pending_feedback", side_effect=launched),
            patch.object(auto_pipeline, "start"),
        ):
            jobs._execute_continue(
                job_id=job_id, run_id=self.run_id, engine="kimi", message="keep trying!",
                problem_text="Prove it.", model="kimi-k3", max_iterations=2,
                workspace=str(self.workspace),
            )

    def test_a_follow_up_keeps_its_proof_and_ends_before_queued_feedback_starts(self) -> None:
        seen = {}

        def engine(*args, **kwargs):
            seen["during"] = proof_turns.summary(self.workspace)
            (self.workspace / "proof.md").write_text("# Proof\n\nTurn two.\n", encoding="utf-8")
            return {"run_id": self.run_id, "status": "finished", "edges": [], "totals": {},
                    "agents": [{"trace_id": f"{self.run_id}::turn-2", "output": "second", "status": "finished"}]}

        def launched(*args, **kwargs):
            seen["at_launch"] = proof_turns.summary(self.workspace)

        self._follow_up(engine, launched)
        self.assertEqual([(t["n"], t["status"]) for t in seen["during"]], [(1, "running")])
        self.assertEqual([(t["n"], t["status"]) for t in seen["at_launch"]], [(1, "finished")])
        kept = proof_turns.summary(self.workspace)[0]
        self.assertTrue(kept["finished_at"])
        self.assertEqual((self.workspace / kept["file"]).read_text(encoding="utf-8"), (self.workspace / "proof.md").read_text(encoding="utf-8"))

    def test_a_failed_follow_up_is_recorded_failed(self) -> None:
        def engine(*args, **kwargs):
            raise RuntimeError("provider call failed")

        self._follow_up(engine, lambda *a, **k: None)
        self.assertEqual([t["status"] for t in proof_turns.summary(self.workspace)], ["failed"])

    def test_an_analysis_follow_up_records_no_turn(self) -> None:
        record = jobs._load_run_record(self.run_id)
        record["run_kind"] = "analyze"
        jobs._write_run(record)

        def engine(*args, **kwargs):
            return {"run_id": self.run_id, "status": "finished", "edges": [], "totals": {},
                    "agents": [{"trace_id": f"{self.run_id}::turn-2", "output": "second", "status": "finished"}]}

        self._follow_up(engine, lambda *a, **k: None)
        self.assertFalse((self.workspace / ".proof_turns").exists())

    def test_a_deleted_run_gets_no_turn_folder(self) -> None:
        with patch.object(jobs, "_run_deleted", return_value=True):
            self.assertIsNone(jobs._proof_turn_begin(self.run_id, self.workspace))
            jobs._proof_turn_end(self.run_id, self.workspace, 1)
        self.assertFalse((self.workspace / ".proof_turns").exists())


class ProofTurnsWiringTest(unittest.TestCase):
    def test_first_runs_and_follow_ups_both_record_their_turn_and_the_api_lists_them(self) -> None:
        import inspect

        from agent_monitor import console_server

        execute = inspect.getsource(jobs._execute_job)
        body = inspect.getsource(jobs._execute_job_body)
        follow = inspect.getsource(jobs._execute_continue)
        # The first run: only a proof run records turns; the body ends the turn before its queued feedback starts.
        self.assertIn('turn = _proof_turn_begin(run_id, ws) if run_kind == "prove" else None', execute)
        self.assertLess(body.index("_proof_turn_end(run_id, ws, turn)"), body.index("_launch_pending_feedback("))
        self.assertLess(follow.index("_proof_turn_end(run_id, ws, turn)"), follow.index("_launch_pending_feedback("))
        self.assertIn('data["proof_turns"] = proof_turns.summary(ws)', inspect.getsource(console_server))


if __name__ == "__main__":
    unittest.main()
