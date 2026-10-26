from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import auto_pipeline


class AutoPipelinePendingRefreshTests(unittest.TestCase):
    def test_continuation_during_final_signature_starts_one_latest_refresh(self) -> None:
        first_final_started = threading.Event()
        release_first_final = threading.Event()
        second_finish = threading.Event()
        source_calls = 0
        wait_calls = 0
        batch_signatures: list[str] = []
        original_finish = auto_pipeline._finish

        def wait_for_source(*_args, **_kwargs):
            nonlocal wait_calls
            wait_calls += 1
            signature = "proof.md:old" if wait_calls == 1 else "proof.md:new"
            return signature, {"status": "finished", "engine": "math_harness"}

        def source_signature(*_args, **_kwargs):
            nonlocal source_calls
            source_calls += 1
            if source_calls == 1:
                first_final_started.set()
                self.assertTrue(release_first_final.wait(3))
                return "proof.md:old"
            return "proof.md:new"

        def run_batch(workspace, *_args, **_kwargs):
            state = auto_pipeline.load_state(workspace) or {}
            batch_signatures.append(str(state.get("source_signature") or ""))

        def finish(workspace):
            original_finish(workspace)
            if len(batch_signatures) >= 2:
                second_finish.set()

        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            key = str(workspace.resolve())
            try:
                with (
                    patch.object(auto_pipeline, "_wait_for_source", side_effect=wait_for_source),
                    patch.object(
                        auto_pipeline,
                        "_run_record",
                        return_value={"status": "finished", "engine": "math_harness"},
                    ),
                    patch.object(
                        auto_pipeline, "_source_signature", side_effect=source_signature
                    ),
                    patch.object(auto_pipeline, "_run_batch", side_effect=run_batch),
                    patch.object(auto_pipeline, "_finish", side_effect=finish),
                ):
                    auto_pipeline.start(
                        run_id="race-run",
                        workspace=workspace,
                        owner_id=7,
                        model="kimi-k3",
                        engine="math_harness",
                    )
                    self.assertTrue(first_final_started.wait(3))

                    queued = auto_pipeline.start(
                        run_id="race-run",
                        workspace=workspace,
                        owner_id=7,
                        model="kimi-k3",
                        engine="math_harness",
                    )
                    self.assertIs(queued.get("pending_refresh"), True)
                    release_first_final.set()
                    self.assertTrue(second_finish.wait(5))

                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        with auto_pipeline._ACTIVE_LOCK:
                            if key not in auto_pipeline._ACTIVE:
                                break
                        time.sleep(0.01)

                state = auto_pipeline.load_state(workspace) or {}
                self.assertEqual(batch_signatures, ["proof.md:old", "proof.md:new"])
                self.assertEqual(state.get("source_signature"), "proof.md:new")
                # Only the proof a full pass derived counts for later follow-ups.
                self.assertEqual(state.get("derived_signature"), "proof.md:new")
                self.assertEqual(state.get("rerun_count"), 1)
                self.assertNotIn("pending_refresh", state)
                with auto_pipeline._ACTIVE_LOCK:
                    self.assertNotIn(key, auto_pipeline._ACTIVE)
                    self.assertNotIn(key, auto_pipeline._PENDING_START)
            finally:
                release_first_final.set()
                with auto_pipeline._ACTIVE_LOCK:
                    active = auto_pipeline._ACTIVE.pop(key, None)
                    auto_pipeline._PENDING_START.pop(key, None)
                if active is not None:
                    active.join(timeout=3)

    def test_completed_internal_rerun_consumes_matching_pending_refresh(self) -> None:
        final_signature_started = threading.Event()
        release_final_signature = threading.Event()
        finished = threading.Event()
        batch_signatures: list[str] = []
        original_finish = auto_pipeline._finish

        def source_signature(*_args, **_kwargs):
            final_signature_started.set()
            self.assertTrue(release_final_signature.wait(3))
            return "proof.md:new"

        def run_batch(workspace, *_args, **_kwargs):
            state = auto_pipeline.load_state(workspace) or {}
            batch_signatures.append(str(state.get("source_signature") or ""))

        def finish(workspace):
            original_finish(workspace)
            finished.set()

        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            key = str(workspace.resolve())
            try:
                with (
                    patch.object(
                        auto_pipeline,
                        "_wait_for_source",
                        return_value=(
                            "proof.md:old",
                            {"status": "finished", "engine": "math_harness"},
                        ),
                    ),
                    patch.object(
                        auto_pipeline,
                        "_run_record",
                        return_value={"status": "finished", "engine": "math_harness"},
                    ),
                    patch.object(
                        auto_pipeline, "_source_signature", side_effect=source_signature
                    ),
                    patch.object(auto_pipeline, "_run_batch", side_effect=run_batch),
                    patch.object(auto_pipeline, "_finish", side_effect=finish),
                ):
                    auto_pipeline.start(
                        run_id="coalesced-run",
                        workspace=workspace,
                        owner_id=7,
                        model="kimi-k3",
                        engine="math_harness",
                    )
                    self.assertTrue(final_signature_started.wait(3))
                    queued = auto_pipeline.start(
                        run_id="coalesced-run",
                        workspace=workspace,
                        owner_id=7,
                        model="kimi-k3",
                        engine="math_harness",
                    )
                    self.assertIs(queued.get("pending_refresh"), True)
                    release_final_signature.set()
                    self.assertTrue(finished.wait(5))

                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        with auto_pipeline._ACTIVE_LOCK:
                            if key not in auto_pipeline._ACTIVE:
                                break
                        time.sleep(0.01)

                state = auto_pipeline.load_state(workspace) or {}
                self.assertEqual(batch_signatures, ["proof.md:old", "proof.md:new"])
                self.assertEqual(state.get("source_signature"), "proof.md:new")
                # Only the proof a full pass derived counts for later follow-ups.
                self.assertEqual(state.get("derived_signature"), "proof.md:new")
                self.assertEqual(state.get("rerun_count"), 1)
                self.assertNotIn("pending_refresh", state)
                with auto_pipeline._ACTIVE_LOCK:
                    self.assertNotIn(key, auto_pipeline._ACTIVE)
                    self.assertNotIn(key, auto_pipeline._PENDING_START)
            finally:
                release_final_signature.set()
                with auto_pipeline._ACTIVE_LOCK:
                    active = auto_pipeline._ACTIVE.pop(key, None)
                    auto_pipeline._PENDING_START.pop(key, None)
                if active is not None:
                    active.join(timeout=3)


def _produced_state(**changes) -> dict:
    """A full pass on proof.md:same that produced every view (Lean unfaithful)."""
    state = {
        "version": 2,
        "run_id": "settled-run",
        "proof_engine": "kimi",
        "status": "partial",
        "proof_outcome": "attention",
        "source_signature": "proof.md:same",
        "derived_signature": "proof.md:same",
        "rerun_count": 0,
        "finished_at": "2026-10-26T17:16:57+00:00",
        "stages": {
            "informal_dag": {"status": "done", "outcome": "success"},
            "lean": {
                "status": "done",
                "outcome": "attention",
                "result": {"status": "unfaithful", "outcome": "attention"},
            },
            "formal_dag": {"status": "done", "outcome": "attention"},
            "coverage": {"status": "done", "outcome": "attention"},
        },
    }
    state.update(changes)
    return state


def _failed_lean_state() -> dict:
    """The same pass, except that its Lean translation failed."""
    blocked = {"status": "skipped", "outcome": "blocked"}
    return _produced_state(
        proof_outcome="error",
        stages={
            "informal_dag": {"status": "done", "outcome": "success"},
            "lean": {"status": "error", "outcome": "error"},
            "formal_dag": dict(blocked),
            "coverage": dict(blocked),
        },
    )


class AutoPipelineSettledPassTests(unittest.TestCase):
    """start() when no pass is alive: a follow-up that published nothing new
    must not pay for views of the same proof that already exist."""

    def _start(
        self,
        state: dict | None,
        *,
        signature: str = "proof.md:same",
        record: dict | None = None,
    ) -> tuple[dict, bool, dict | None, dict | None]:
        """Return (start's answer, whether a pass ran, state before, after)."""
        ran = threading.Event()
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            key = str(workspace.resolve())
            if state is not None:
                auto_pipeline._write_state(workspace, state)
            before = auto_pipeline.load_state(workspace)
            try:
                with (
                    patch.object(
                        auto_pipeline,
                        "_run_record",
                        return_value=record or {"status": "failed", "engine": "kimi"},
                    ),
                    patch.object(auto_pipeline, "_source_signature", return_value=signature),
                    patch.object(auto_pipeline, "_run", side_effect=lambda *_a, **_k: ran.set()),
                ):
                    answer = auto_pipeline.start(
                        run_id="settled-run",
                        workspace=workspace,
                        owner_id=7,
                        model="kimi-k3",
                        engine="kimi",
                    )
                    with auto_pipeline._ACTIVE_LOCK:
                        active = auto_pipeline._ACTIVE.get(key)
                    if active is not None:
                        active.join(timeout=3)
                return answer, ran.is_set(), before, auto_pipeline.load_state(workspace)
            finally:
                with auto_pipeline._ACTIVE_LOCK:
                    auto_pipeline._ACTIVE.pop(key, None)
                    auto_pipeline._PENDING_START.pop(key, None)

    def test_views_of_the_same_proof_are_not_derived_again(self) -> None:
        with patch.object(
            auto_pipeline, "_write_state", wraps=auto_pipeline._write_state
        ) as write_state:
            answer, ran, before, after = self._start(_produced_state())

        self.assertFalse(ran)
        self.assertEqual(answer, before)
        self.assertEqual(after, before)
        self.assertEqual(write_state.call_count, 1)  # the seed only: start() wrote nothing

    def test_an_informal_only_pass_is_not_repeated(self) -> None:
        off = {"status": "off", "outcome": "not_run"}
        state = _produced_state(
            status="done",
            formalize=False,
            stages={
                "informal_dag": {"status": "done", "outcome": "success"},
                "lean": dict(off),
                "formal_dag": dict(off),
                "coverage": dict(off),
            },
        )
        _answer, ran, before, after = self._start(
            state, record={"status": "failed", "engine": "kimi", "formalize": False}
        )

        self.assertFalse(ran)
        self.assertEqual(after, before)

    def test_a_proof_whose_last_pass_failed_somewhere_is_derived_again(self) -> None:
        answer, ran, _before, after = self._start(_failed_lean_state())

        self.assertTrue(ran)
        self.assertEqual(answer["status"], "running")
        self.assertEqual(after["stages"]["lean"]["status"], "waiting")

    def test_a_new_proof_is_derived(self) -> None:
        answer, ran, _before, after = self._start(
            _produced_state(), signature="proof.md:new"
        )

        self.assertTrue(ran)
        self.assertEqual(answer["status"], "running")
        self.assertEqual(after["stages"]["lean"]["status"], "waiting")

    def test_only_a_full_pass_that_produced_every_view_of_this_proof_counts(self) -> None:
        stages = _produced_state()["stages"]
        without_coverage = {k: v for k, v in stages.items() if k != "coverage"}
        cases = {
            "no earlier pass": (None, None),
            "interrupted by a restart": (
                _produced_state(interruption={"reason": "console restarted"}), None
            ),
            "made stale by an edit": (_produced_state(status="stale"), None),
            "stopped": (_produced_state(status="stopped"), None),
            "still marked running": (_produced_state(status="running"), None),
            "a stage still waiting": (
                _produced_state(stages={**stages, "coverage": {"status": "waiting"}}),
                None,
            ),
            "a stage missing": (_produced_state(stages=without_coverage), None),
            "a stage blocked": (
                _produced_state(
                    stages={**stages, "coverage": {"status": "skipped", "outcome": "blocked"}}
                ),
                None,
            ),
            "a newer request pending": (_produced_state(pending_refresh=True), None),
            # Written by a formal-only refresh, or before derived_signature existed.
            "no full pass recorded": (_produced_state(derived_signature=None), None),
            "a full pass of another proof": (
                _produced_state(derived_signature="proof.md:older"), None
            ),
            "formalization asked for since": (
                _produced_state(formalize=False),
                {"status": "failed", "engine": "kimi"},
            ),
        }
        for name, (state, record) in cases.items():
            with self.subTest(name):
                answer, ran, _before, _after = self._start(state, record=record)
                self.assertTrue(ran)
                self.assertEqual(answer["status"], "running")


class AutoPipelineLeanActionDuringAFollowUpTests(unittest.TestCase):
    """A Lean action (Check, Compile, ...) refreshes only the formal views of
    whatever proof is on disk; it must not stand for deriving that proof."""

    RECORD = {"status": "finished", "engine": "codex", "formalize": True}

    def _passes_after(self, *, new_proof: bool, lean_status: str = "unfaithful") -> list[str]:
        derived: list[str] = []

        def batch(workspace, run_record, *_args, **_kwargs):
            # Stands in for the paid informal DAG, Lean, formal DAG and coverage.
            derived.append(auto_pipeline._source_signature(workspace, run_record))
            for name in ("informal_dag", "lean", "formal_dag", "coverage"):
                auto_pipeline._set_stage(workspace, name, "done", outcome="success")

        def start(workspace: Path) -> None:
            auto_pipeline.start(
                run_id="lean-action-run",
                workspace=workspace,
                owner_id=7,
                model="m",
                engine="codex",
            )
            key = str(workspace.resolve())
            with auto_pipeline._ACTIVE_LOCK:
                active = auto_pipeline._ACTIVE.get(key)
            if active is not None:
                active.join(timeout=10)
                self.assertFalse(active.is_alive())

        from agent_monitor import lean_verify, proof_bridge, proof_graph

        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            key = str(workspace.resolve())
            (workspace / "proof.md").write_text("# Claim\n\nThe first proof.\n", encoding="utf-8")
            try:
                with (
                    patch.object(auto_pipeline, "_run_record", return_value=dict(self.RECORD)),
                    patch.object(auto_pipeline, "_run_user", return_value={"id": 7}),
                    patch.object(auto_pipeline, "_run_batch", side_effect=batch),
                    patch.object(auto_pipeline, "_writeup_follows_proof", return_value=False),
                    patch.object(
                        lean_verify,
                        "load_cached",
                        return_value={"status": lean_status, "model": "m"},
                    ),
                    patch.object(
                        proof_graph,
                        "generate",
                        return_value={
                            "model": "m",
                            "source": "Proof.lean",
                            "graph": {"nodes": [], "edges": []},
                        },
                    ),
                    patch.object(
                        proof_bridge, "build", return_value={"status": "unverified", "summary": {}}
                    ),
                ):
                    start(workspace)  # the run's own pass
                    if new_proof:  # the follow-up publishes a new proof ...
                        (workspace / "proof.md").write_text(
                            "# Claim\n\nThe follow-up's proof.\n", encoding="utf-8"
                        )
                    # ... and the owner presses Check before it ends.
                    (workspace / "lean").mkdir(exist_ok=True)
                    (workspace / "lean" / "Proof.lean").write_text(
                        "theorem main : True := trivial\n", encoding="utf-8"
                    )
                    auto_pipeline._refresh_formal_once(
                        run_id="lean-action-run",
                        workspace=workspace,
                        owner_id=7,
                        model="m",
                        engine="codex",
                    )
                    start(workspace)  # the follow-up ends
                    final = auto_pipeline._source_signature(workspace, self.RECORD)
            finally:
                with auto_pipeline._ACTIVE_LOCK:
                    auto_pipeline._ACTIVE.pop(key, None)
                    auto_pipeline._PENDING_START.pop(key, None)
        self.assertEqual(derived[-1], final)
        return derived

    def test_the_follow_ups_new_proof_is_still_derived(self) -> None:
        self.assertEqual(len(self._passes_after(new_proof=True)), 2)

    def test_an_unchanged_proof_is_still_not_derived_again(self) -> None:
        self.assertEqual(len(self._passes_after(new_proof=False)), 1)

    def test_an_unchanged_proof_whose_lean_check_failed_is_derived_again(self) -> None:
        # As before this change: a pass that failed somewhere gets another try.
        self.assertEqual(len(self._passes_after(new_proof=False, lean_status="failed")), 2)


if __name__ == "__main__":
    unittest.main()
