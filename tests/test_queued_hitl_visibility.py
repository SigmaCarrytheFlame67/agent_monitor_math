from __future__ import annotations

import contextlib
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import jobs


class QueuedHitlVisibilityTests(unittest.TestCase):
    def test_active_harness_cannot_see_feedback_queued_for_next_turn(self) -> None:
        run_id = "plain_active_feedback"
        record = {
            "run_id": run_id,
            "engine": "plain",
            "problem_id": "visibility",
            "problem_text": "Prove the original claim.",
            "problems": [
                {"text": "Prove the original claim.", "source": "initial"}
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            workspace = runs / "workspaces" / run_id
            workspace.mkdir(parents=True)
            original = "Prove the original claim.\n"
            (workspace / "problem.txt").write_text(original, encoding="utf-8")

            with (
                patch.object(jobs, "RUNS_DIR", runs),
                patch.object(
                    jobs,
                    "_run_is_active",
                    return_value={"job_id": "active-job"},
                ),
                patch.object(jobs, "_load_run_record", return_value=record),
                patch.object(jobs, "_write_run") as write_run,
                patch.object(jobs, "_append_chat", return_value={"role": "user"}),
                patch.object(jobs, "list_chat", return_value=[]),
            ):
                result = jobs.send_human_message(
                    run_id,
                    "Use a different lemma on the next turn.",
                )

            self.assertEqual(result["status"], "queued")
            self.assertEqual(result["job_id"], "active-job")
            self.assertEqual(
                (workspace / "problem.txt").read_text(encoding="utf-8"),
                original,
            )
            self.assertFalse((workspace / "problems.json").exists())
            self.assertEqual(
                record["problems"][-1]["text"],
                "Use a different lemma on the next turn.",
            )
            write_run.assert_called_once_with(record)
            self.assertEqual(
                jobs._drain_human_feedback(workspace),
                ["Use a different lemma on the next turn."],
            )


class FeedbackSentDuringAFollowUpTests(unittest.TestCase):
    """The follow-up's final record keeps feedback recorded while it ran."""

    THEOREM = "Prove the theorem."
    EARLIER = "Try harder."
    QUESTION = "Is this still running?"

    def setUp(self) -> None:
        self.run_id = "kimi_feedback_during_follow_up"
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(jobs, "RUNS_DIR", root / "runs"))
        self.enterContext(patch.object(jobs, "CACHE_DIR", root / "cache"))
        self.workspace = root / "runs" / "workspaces" / self.run_id
        self.workspace.mkdir(parents=True)
        (self.workspace / "proof.md").write_text("# Proof\n\nA candidate.\n", encoding="utf-8")
        self.addCleanup(jobs._STOP_EVENTS.pop, self.run_id, None)
        jobs._stop_event(self.run_id).clear()
        jobs._write_run(
            {
                "run_id": self.run_id,
                "engine": "kimi",
                "problem_id": "adhoc",
                "status": "failed",
                "problem_text": self.THEOREM,
                "problems": [
                    {"text": self.THEOREM, "source": "initial"},
                    {"text": self.EARLIER, "source": "human"},
                ],
                "agents": [
                    {"trace_id": f"{self.run_id}::turn-1", "output": "first", "status": "finished"}
                ],
                "edges": [],
            }
        )

    def _follow_up(self, job_id: str, message: str, engine, **kwargs) -> None:
        from agent_monitor import auto_pipeline

        jobs._JOBS[job_id] = {
            "job_id": job_id, "run_id": self.run_id, "status": "running", "engine": "kimi",
        }
        self.addCleanup(jobs._JOBS.pop, job_id, None)
        with (
            patch.object(jobs, "_run_cli_engine_with_openclaude_retry", side_effect=engine),
            patch.object(jobs, "_launch_pending_feedback"),
            patch.object(auto_pipeline, "start"),
        ):
            jobs._execute_continue(
                job_id=job_id,
                run_id=self.run_id,
                engine="kimi",
                message=message,
                problem_text=self.THEOREM,
                model="kimi-k3",
                max_iterations=2,
                workspace=str(self.workspace),
                **kwargs,
            )

    def _failed(self, label: str) -> dict:
        return {
            "run_id": self.run_id,
            "status": "failed",
            "error": "provider call failed",
            "agents": [
                {"trace_id": f"{self.run_id}::{label}", "output": "failed", "status": "failed"}
            ],
            "edges": [],
            "totals": {},
        }

    def test_feedback_sent_during_a_follow_up_stays_a_session_problem(self) -> None:
        def engine_while_the_user_writes(**_kwargs):
            reply = jobs.send_human_message(self.run_id, self.QUESTION)
            self.assertEqual(reply["status"], "queued")
            # The engine keeps streaming; its live writes carry no problems.
            flush = jobs._live_output_flusher(
                run_id=self.run_id,
                engine="kimi",
                problem_id="adhoc",
                problem_text=self.THEOREM,
                started=time.time(),
                workspace=self.workspace,
            )
            flush("worker output")
            return self._failed("continue")

        # A plain Continue, during which the user asks something.
        self._follow_up(
            "job-continue",
            "Continue from where the previous session stopped. Keep improving proof.md.",
            engine_while_the_user_writes,
        )
        record = jobs._load_run_record(self.run_id)
        self.assertEqual(
            [problem["text"] for problem in record["problems"]],
            [self.THEOREM, self.EARLIER, self.QUESTION],
        )
        self.assertEqual(record["status"], "failed")

        # The queued follow-up then shows that question as this turn's.
        prompts: list[str] = []

        def capture(**kwargs):
            prompts.append(kwargs["problem_text"])
            return self._failed("queued")

        self._follow_up("job-queued", self.QUESTION, capture, problems_recorded=True)
        block = prompts[0].split("SESSION PROBLEMS:\n", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(
            block.splitlines(),
            [
                f"1. {self.THEOREM}",
                f"2. {self.EARLIER}",
                f"3. {self.QUESTION}  ← new this turn",
            ],
        )
        self.assertEqual(
            [problem["text"] for problem in jobs._load_run_record(self.run_id)["problems"]],
            [self.THEOREM, self.EARLIER, self.QUESTION],
        )

    # A message the user sends at one exact moment of a plain Continue.

    def _send_now(self, sender: dict) -> None:
        """Send QUESTION from another thread, giving it ample time to read and
        write the record unless the record lock holds it back."""
        if "thread" in sender:
            return

        def send() -> None:
            sender["reply"] = jobs.send_human_message(self.run_id, self.QUESTION)

        sender["thread"] = threading.Thread(target=send, daemon=True)
        sender["thread"].start()
        sender["thread"].join(timeout=0.5)

    def _continue_while_sending(self, sender: dict, *hooks) -> dict:
        """Run a plain Continue with ``hooks`` (patches that call _send_now)
        and return the final record.

        The follow-up stays active until the message is in, so the message
        is queued as in production; starting a follow-up from it fails."""
        original_update = jobs._update_job

        def update_once_the_message_is_in(job_id: str, **kwargs):
            if job_id == "job-continue" and "thread" in sender:
                sender["thread"].join(timeout=5)
            return original_update(job_id, **kwargs)

        def no_follow_up(*_args, **_kwargs):
            raise AssertionError("the message must be queued, not start a follow-up")

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch.object(jobs, "_update_job", side_effect=update_once_the_message_is_in)
            )
            stack.enter_context(
                patch.object(jobs, "_continue_with_message", side_effect=no_follow_up)
            )
            for hook in hooks:
                stack.enter_context(hook)
            self._follow_up(
                "job-continue",
                "Continue from where the previous session stopped. Keep improving proof.md.",
                lambda **_kwargs: self._failed("continue"),
            )
            sender["thread"].join(timeout=5)
        self.assertFalse(sender["thread"].is_alive())
        self.assertEqual(sender["reply"]["status"], "queued")
        record = jobs._load_run_record(self.run_id)
        self.assertEqual(record["status"], "failed")  # the follow-up's result
        self.assertEqual(record["continuation_count"], 1)
        self.assertEqual(
            [problem["text"] for problem in record["problems"]],
            [self.THEOREM, self.EARLIER, self.QUESTION],
        )
        return record

    def test_a_message_sent_as_the_follow_up_writes_its_result_keeps_both(self) -> None:
        """The message's read-and-write waits for the follow-up's final write
        instead of writing back a copy of the record read before it."""
        sender: dict = {}
        original_write = jobs._write_run_locked

        def final_write(run: dict):
            if run.get("job_id") == "job-continue" and run.get("status") == "failed":
                self._send_now(sender)
            return original_write(run)

        self._continue_while_sending(
            sender, patch.object(jobs, "_write_run_locked", side_effect=final_write)
        )

    def test_a_message_sent_as_the_follow_up_starts_its_turn_is_kept(self) -> None:
        """Recorded after the follow-up read the record, before its start write."""
        sender: dict = {}
        original_task = jobs._cgm_follow_up_task

        def task(**kwargs):  # the CGM task is built right before the start write
            self._send_now(sender)
            return original_task(**kwargs)

        self._continue_while_sending(
            sender, patch.object(jobs, "_cgm_follow_up_task", side_effect=task)
        )

    def test_a_message_sent_right_after_the_final_merge_read_is_kept(self) -> None:
        """The final merge reads the record under the lock it writes under."""
        sender: dict = {}
        original_merge = jobs._problems_with_recorded
        calls = []

        def merge(problems, run_id, **kw):
            merged = original_merge(problems, run_id, **kw)
            calls.append(run_id)
            if len(calls) == 2:  # the final write's merge
                self._send_now(sender)
            return merged

        self._continue_while_sending(
            sender, patch.object(jobs, "_problems_with_recorded", side_effect=merge)
        )

    def test_the_record_lock_is_reentrant(self) -> None:
        # Read-modify-write regions call _write_run, which takes it again.
        with jobs._RUN_WRITE_LOCK:
            self.assertTrue(jobs._RUN_WRITE_LOCK.acquire(timeout=1))
            jobs._RUN_WRITE_LOCK.release()


if __name__ == "__main__":
    unittest.main()
