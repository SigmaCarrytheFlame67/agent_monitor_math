"""A run deleted while its Lean/DAG views are being derived stops quietly.

In production (2026-10-27) an owner deleted a run while its informal DAG
model call was in flight: the pipeline thread then crashed writing into the
removed workspace.  Now the pass makes no further model call, writes nothing
(and never recreates the workspace), and ends without an exception; anything
a call in flight wrote after the deletion is removed again.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import auto_pipeline, jobs, lean_verify, proof_bridge, proof_graph

_GRAPH = {"model": "m", "source": "proof.md", "graph": {"nodes": [{"id": "n1"}], "edges": []}}


class DeletedRunPipelineTests(unittest.TestCase):
    def _workspace(self, run_id: str) -> Path:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        workspace = root / run_id
        workspace.mkdir()
        (workspace / "proof.md").write_text("# Claim\n\nA proof.\n", encoding="utf-8")
        self.addCleanup(jobs._DELETED_RUNS.discard, run_id)
        return workspace

    @staticmethod
    def _delete(workspace: Path) -> None:
        """What jobs.delete_run does to a run: mark it, then remove its files."""
        jobs._DELETED_RUNS.add(workspace.name)
        shutil.rmtree(workspace)

    def _run(self, workspace: Path, record: dict, *, informal, lean) -> list[BaseException]:
        thread_errors: list[BaseException] = []
        with (
            patch.object(auto_pipeline, "_wait_for_source", return_value=("sig-1", dict(record))),
            patch.object(auto_pipeline, "_run_record", return_value=dict(record)),
            patch.object(auto_pipeline, "_run_user", return_value={"id": 7}),
            patch.object(auto_pipeline, "_writeup_follows_proof", return_value=False),
            patch.object(proof_graph, "generate", side_effect=informal) as graph,
            patch.object(proof_graph, "fact_graph_result", return_value=None),
            patch.object(lean_verify, "generate", side_effect=lean) as lean_call,
            patch.object(proof_bridge, "build", return_value={"status": "unverified", "summary": {}}),
            patch.object(threading, "excepthook", side_effect=lambda args: thread_errors.append(args.exc_value)),
        ):
            auto_pipeline._run(workspace.name, workspace, 7, "m", "kimi")
        self.graph_calls, self.lean_calls = graph.call_count, lean_call.call_count
        return thread_errors

    def test_deleted_during_the_informal_dag_call(self) -> None:
        # The sponsored route derives serially: informal DAG first, then Lean.
        record = {"run_id": "deleted-informal", "engine": "kimi", "status": "finished",
                  "credential_source": "sponsored_kimi"}
        for outcome in ("the write fails", "the call returns"):
            with self.subTest(outcome=outcome):
                workspace = self._workspace(record["run_id"])

                def informal(**_kwargs):
                    self._delete(workspace)
                    if outcome == "the write fails":
                        # proof_graph's cache write into the removed workspace.
                        raise FileNotFoundError(str(workspace / ".proof-graph-x.tmp"))
                    return dict(_GRAPH)

                errors = self._run(workspace, record, informal=informal, lean=lambda **_k: {"status": "verified"})
                self.assertEqual(errors, [])
                self.assertEqual((self.graph_calls, self.lean_calls), (1, 0))
                self.assertFalse(workspace.exists(), "the deleted workspace was recreated")
                jobs._DELETED_RUNS.discard(record["run_id"])

    def test_deleted_during_the_lean_call(self) -> None:
        # Informal DAG and Lean in parallel threads; Lean's own writes would
        # recreate the workspace after the deletion.
        record = {"run_id": "deleted-lean", "engine": "kimi", "status": "finished", "credential_source": "user"}
        workspace = self._workspace(record["run_id"])

        stop_checks: list[bool] = []

        def lean(**kwargs):
            stop_checks.append(kwargs["should_stop"]())  # the session's own check
            self._delete(workspace)
            stop_checks.append(kwargs["should_stop"]())
            (workspace / "lean").mkdir(parents=True)
            (workspace / "lean" / "Proof.lean").write_text("theorem t : True := trivial\n", encoding="utf-8")
            return {"status": "verified"}

        errors = self._run(workspace, record, informal=lambda **_k: dict(_GRAPH), lean=lean)
        self.assertEqual(errors, [])
        self.assertEqual(self.lean_calls, 1)
        self.assertEqual(stop_checks, [False, True])
        # No formal DAG call after the deletion (only the informal one).
        self.assertLessEqual(self.graph_calls, 1)
        self.assertFalse(workspace.exists(), "files written after the deletion were left behind")

    def test_a_lean_session_stops_before_its_next_call_once_the_run_is_deleted(self) -> None:
        import json

        workspace = self._workspace("deleted-lean-session")
        calls: list[str] = []

        def model(env, model_id, prompt, **_kwargs):
            calls.append(prompt[:40])
            self._delete(workspace)  # the deletion lands during the first call
            return model_id, json.dumps({"lean": "theorem main : True := trivial\n", "notes": "", "title": "t"}), "openai"

        with (
            patch.object(lean_verify, "_call_llm", side_effect=model),
            patch.object(lean_verify, "_llm_environment", return_value={"OPENAI_API_KEY": "test-key"}),
            patch.object(lean_verify, "_write_and_run") as compile_call,
            self.assertRaises(lean_verify.SessionStopped),
        ):
            lean_verify.generate(
                workspace=workspace, run_record={"run_id": workspace.name}, user={"id": 7, "is_admin": True},
                model="gpt-6-astra", should_stop=lambda: auto_pipeline._run_gone(workspace),
            )
        self.assertEqual(len(calls), 1)
        compile_call.assert_not_called()
        self.assertFalse(workspace.exists())

    def _real_session(self, workspace: Path, *, prove: bool, stop_after: int | None = None,
                      delete_after: int | None = None, fingerprint=None, proof_closes: bool = True) -> dict:
        """The real generate(), _chat and _audit_faithfulness with a scripted
        model (_call_llm) and Lean checker; the run stops, or is deleted, once
        the given number of compiles returned."""
        from tests.test_formal_pipeline_statement_first import AUDIT_OK, PROVED, SKELETON, _GenerateHarness, _fenced

        (workspace / "problem.txt").write_text("Prove n + 0 = n.", encoding="utf-8")
        run = _GenerateHarness(self, [], ["ok"] * 8, fingerprint=fingerprint)
        seen = {"compiles": 0, "stopped": False, "calls_after_stop": 0, "probes_after_stop": 0}

        def model(_env, model_id, prompt, **_kwargs):
            seen["calls_after_stop"] += seen["stopped"]
            if "independent, strict reviewer of a Lean 4 autoformalization" in prompt:
                return model_id, json.dumps(AUDIT_OK), "openai"
            proved = PROVED if proof_closes else SKELETON  # a proof that keeps its sorry
            return model_id, _fenced(proved if "LOCKED SKELETON" in prompt else SKELETON), "openai"

        def check(lean_dir, uses_mathlib, main_decl=None):
            result = run.check(lean_dir, uses_mathlib, main_decl)
            seen["compiles"] += 1
            if seen["compiles"] in (stop_after, delete_after):
                seen["stopped"] = True
                if seen["compiles"] == delete_after:
                    self._delete(workspace)
            return result

        def probe(*args, **kwargs):
            seen["probes_after_stop"] += seen["stopped"]
            return run.fake_fingerprint(*args, **kwargs)

        def goals(*_args, **_kwargs):
            seen["probes_after_stop"] += seen["stopped"]
            return "line 6:\nn : ℕ\n⊢ n + 0 = n"

        with (
            patch("agent_monitor.settings.resolved_user_env", return_value={}),
            patch.object(lean_verify, "_proof_source", return_value=("proof.md", "By simp.")),
            patch.object(lean_verify, "_llm_environment", return_value={"OPENAI_API_KEY": "test-key"}),
            patch.object(lean_verify, "_call_llm", side_effect=model),
            patch.object(lean_verify, "_run_lean_check", side_effect=check),
            patch.object(lean_verify, "_sorry_goals", side_effect=goals),
            patch.object(lean_verify, "_statement_fingerprint", side_effect=probe),
            patch.object(lean_verify, "toolchain_status", return_value={"available": True}),
            self.assertRaises(lean_verify.SessionStopped),
        ):
            lean_verify.generate(
                workspace=workspace, run_record={}, user={"id": 7}, model="gpt-test", prove=prove,
                should_stop=(lambda: auto_pipeline._run_gone(workspace)) if delete_after else (lambda: seen["stopped"]),
            )
        return seen

    def test_a_stop_during_either_audit_ends_the_session(self) -> None:
        # The audits' own model calls find the stop (_audit_faithfulness
        # turns any other failure into "no audit").  Nothing runs or is
        # written after it: no model call, no Lean probe, no lean_verify.json.
        unconfirmed = lambda _source, _main: {"ok": False, "error": "probe failed"}  # noqa: E731
        for name, prove, stop_after, fingerprint in (
            ("statement audit, statements only", False, 1, None),
            ("statement audit, then proof", True, 1, None),
            ("final audit of an unconfirmed statement", True, 2, unconfirmed),
        ):
            with self.subTest(name), tempfile.TemporaryDirectory() as raw:
                workspace = Path(raw)
                seen = self._real_session(workspace, prove=prove, stop_after=stop_after, fingerprint=fingerprint)
                self.assertEqual((seen["calls_after_stop"], seen["probes_after_stop"]), (0, 0))
                self.assertIsNone(lean_verify.load_cached(workspace))

    def test_a_stop_before_a_sorry_repair_skips_its_lean_probe(self) -> None:
        # The proof compiles but keeps a sorry: the next round would first ask
        # Lean for the open goals (a probe), then call the model.
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            seen = self._real_session(workspace, prove=True, stop_after=2, proof_closes=False)
            self.assertEqual((seen["calls_after_stop"], seen["probes_after_stop"]), (0, 0))
            self.assertIsNone(lean_verify.load_cached(workspace))

    def test_nothing_is_persisted_under_a_stop(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            token = lean_verify._SHOULD_STOP.set(lambda: True)
            try:
                with self.assertRaises(lean_verify.SessionStopped):
                    lean_verify._persist(workspace, lean_src="theorem t : True := trivial", status="verified",
                                         uses_mathlib=False, attempts=[])
            finally:
                lean_verify._SHOULD_STOP.reset(token)
            self.assertEqual(list(workspace.iterdir()), [])

    def test_a_session_under_a_stop_neither_probes_nor_restores(self) -> None:
        # Each Lean probe and the restore of the best file check the stop
        # themselves, whatever stopped the session's calls before.
        session = object.__new__(lean_verify._FormalSession)
        session.lock = {"main_decl": "main", "sha256": "x"}
        session._fingerprints = {}
        session.lean = "theorem main : True := trivial\n"
        session.uses_mathlib = False
        with tempfile.TemporaryDirectory() as raw:
            session.workspace = Path(raw)
            token = lean_verify._SHOULD_STOP.set(lambda: True)
            try:
                with patch.object(lean_verify, "_statement_fingerprint") as probe:
                    with self.assertRaises(lean_verify.SessionStopped):
                        session._fingerprint(session.lean)
                    probe.assert_not_called()
                with self.assertRaises(lean_verify.SessionStopped):
                    session._restore_current()
            finally:
                lean_verify._SHOULD_STOP.reset(token)
            self.assertEqual(list(Path(raw).iterdir()), [])  # nothing restored

    def test_a_run_deleted_during_the_last_compile_is_not_recreated(self) -> None:
        workspace = self._workspace("deleted-last-compile")
        seen = self._real_session(workspace, prove=True, delete_after=2)
        self.assertEqual((seen["calls_after_stop"], seen["probes_after_stop"]), (0, 0))
        self.assertFalse(workspace.exists(), "the deleted workspace was recreated")

    def test_an_audit_never_turns_a_stop_into_no_audit(self) -> None:
        token = lean_verify._SHOULD_STOP.set(lambda: True)
        try:
            with patch.object(lean_verify, "_call_llm") as model, self.assertRaises(lean_verify.SessionStopped):
                lean_verify._audit_faithfulness({"OPENAI_API_KEY": "k"}, "gpt-test", problem="p", proof="q", lean="l")
            model.assert_not_called()
        finally:
            lean_verify._SHOULD_STOP.reset(token)

    def test_a_manual_verify_stops_when_its_run_is_deleted(self) -> None:
        # The console's Lean actions (Verify, Revise, Audit) stop too, and say
        # why; the run deleted from another tab is not recreated.
        from agent_monitor import console_server

        workspace = self._workspace("deleted-manual-verify")
        handler = object.__new__(console_server.Handler)
        sent: list[tuple[int, dict]] = []
        handler._send = lambda code, payload, *_a, **_k: sent.append((code, json.loads(payload)))
        calls: list[str] = []

        def session(**_kwargs):
            calls.append("model call")
            self._delete(workspace)
            lean_verify._stop_if_asked()  # the session's next model call
            calls.append("after the deletion")
            return {"status": "verified"}

        with (
            patch.object(console_server, "_derived_record_for", return_value={}),
            patch.object(lean_verify, "_generate", side_effect=session),
        ):
            handler._lean_verify_action(workspace.name, workspace, {"id": 7, "is_admin": True}, "verify",
                                        None, None, "", {}, None)
        self.assertEqual(calls, ["model call"])
        self.assertEqual(sent, [(410, {"error": "This run was deleted; its Lean work stopped."})])
        self.assertIsNone(lean_verify._SHOULD_STOP.get())  # reset for the handler's next request
        self.assertFalse(workspace.exists())

    def test_a_stop_inside_a_repair_round_ends_the_session(self) -> None:
        # Repair rounds run guarded (a failed round keeps the best file so
        # far), but a stop is never taken for a failed round.
        from tests.test_formal_pipeline_statement_first import AUDIT_OK, PROVED, SKELETON, _GenerateHarness, _fenced

        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            (workspace / "problem.txt").write_text("Prove n + 0 = n.", encoding="utf-8")
            run = _GenerateHarness(self, [_fenced(SKELETON), _fenced(PROVED), _fenced(PROVED), _fenced(PROVED)],
                                   ["ok", "fail", "ok", "ok"])
            stopped: list[bool] = []

            def chat(*args, **kwargs):
                reply = run.chat(*args, **kwargs)
                if len(run.prompts) == 3:  # the first repair's call: the run is deleted meanwhile
                    stopped.append(True)
                return reply

            with (
                patch("agent_monitor.settings.resolved_user_env", return_value={}),
                patch.object(lean_verify, "_proof_source", return_value=("proof.md", "By simp.")),
                patch.object(lean_verify, "_chat", side_effect=chat),
                patch.object(lean_verify, "_run_lean_check", side_effect=run.check),
                patch.object(lean_verify, "_audit_faithfulness", side_effect=lambda *a, **k: dict(AUDIT_OK)),
                patch.object(lean_verify, "_sorry_goals", return_value="line 6:\nn : ℕ\n⊢ n + 0 = n"),
                patch.object(lean_verify, "_statement_fingerprint", side_effect=run.fake_fingerprint),
                patch.object(lean_verify, "toolchain_status", return_value={"available": True}),
                self.assertRaises(lean_verify.SessionStopped),
            ):
                lean_verify.generate(workspace=workspace, run_record={}, user={"id": 7}, model="gpt-test",
                                     max_repairs=3, should_stop=lambda: bool(stopped))
            self.assertEqual(len(run.prompts), 3)  # no repair after the stop
            self.assertIsNone(lean_verify.load_cached(workspace))

    def test_the_lean_model_call_itself_checks_the_stop(self) -> None:
        token = lean_verify._SHOULD_STOP.set(lambda: True)
        try:
            with patch.object(lean_verify, "_call_llm") as model, self.assertRaises(lean_verify.SessionStopped):
                lean_verify._chat({"OPENAI_API_KEY": "test-key"}, "gpt-6-astra", "prompt")
            model.assert_not_called()
        finally:
            lean_verify._SHOULD_STOP.reset(token)
        # Without a stop check (every other caller), nothing changes.
        with patch.object(lean_verify, "_call_llm", return_value=("gpt-6-astra", "reply", "openai")):
            self.assertEqual(lean_verify._chat({"OPENAI_API_KEY": "k"}, "gpt-6-astra", "prompt")[1], "reply")

    def test_a_run_deleted_while_a_stage_is_recorded_makes_no_dag_call(self) -> None:
        # The deletion lands during the stage write that precedes a DAG
        # model call (informal or formal): the call is never made.
        real_set_stage = auto_pipeline._set_stage
        for stage in ("informal_dag", "formal_dag"):
            with self.subTest(stage=stage):
                record = {"run_id": f"deleted-at-{stage}", "engine": "kimi", "status": "finished",
                          "credential_source": "sponsored_kimi"}  # serial: informal DAG, then Lean
                workspace = self._workspace(record["run_id"])

                def set_stage(ws, name, status, *args, stage=stage, workspace=workspace, **kwargs):
                    real_set_stage(ws, name, status, *args, **kwargs)
                    if name == stage and status == "running":
                        self._delete(workspace)

                calls: list[str] = []

                def graph(**kwargs):
                    calls.append(kwargs["kind"])
                    return dict(_GRAPH)

                def lean(workspace=workspace, **_kwargs):
                    (workspace / "lean").mkdir(exist_ok=True)  # the formal DAG reads Proof.lean
                    (workspace / "lean" / "Proof.lean").write_text("theorem t : True := trivial\n", encoding="utf-8")
                    return {"status": "verified"}

                with patch.object(auto_pipeline, "_set_stage", side_effect=set_stage):
                    errors = self._run(workspace, record, informal=graph, lean=lambda **kwargs: lean(**kwargs))
                self.assertEqual(errors, [])
                self.assertEqual(calls, [] if stage == "informal_dag" else ["informal"])
                self.assertFalse(workspace.exists())

    def test_state_is_never_written_into_a_deleted_run(self) -> None:
        workspace = self._workspace("deleted-state")
        self._delete(workspace)
        auto_pipeline._set_stage(workspace, "informal_dag", "running")
        self.assertFalse(workspace.exists())

    def test_a_refresh_queued_for_a_deleted_run_never_runs(self) -> None:
        workspace = self._workspace("deleted-refresh")
        self._delete(workspace)
        key = str(workspace.resolve())
        with auto_pipeline._FORMAL_REFRESH_LOCK:
            auto_pipeline._FORMAL_REFRESH_ACTIVE[key] = threading.current_thread()
        with patch.object(auto_pipeline, "_refresh_formal_once") as refresh:
            auto_pipeline._formal_refresh_loop(workspace.name, workspace, 7, "m", "kimi")
        refresh.assert_not_called()
        with auto_pipeline._FORMAL_REFRESH_LOCK:
            self.assertNotIn(key, auto_pipeline._FORMAL_REFRESH_ACTIVE)

    def test_any_error_of_a_deleted_run_ends_its_pass_quietly(self) -> None:
        record = {"run_id": "deleted-batch", "engine": "kimi", "status": "finished", "credential_source": "sponsored_kimi"}
        workspace = self._workspace(record["run_id"])

        def batch(*_args, **_kwargs):
            self._delete(workspace)
            raise RuntimeError("a write into the removed workspace failed")

        with (
            patch.object(auto_pipeline, "_wait_for_source", return_value=("sig-1", dict(record))),
            patch.object(auto_pipeline, "_run_record", return_value=dict(record)),
            patch.object(auto_pipeline, "_run_user", return_value={"id": 7}),
            patch.object(auto_pipeline, "_run_batch", side_effect=batch),
        ):
            auto_pipeline._run(workspace.name, workspace, 7, "m", "kimi")  # no exception
        self.assertFalse(workspace.exists())

    def test_a_pass_waiting_for_a_deleted_run_stops_waiting(self) -> None:
        import time as clock

        workspace = self._workspace("deleted-while-waiting")
        self._delete(workspace)
        started = clock.monotonic()
        with patch.object(auto_pipeline, "_run_record", return_value={"status": "running"}):
            self.assertEqual(auto_pipeline._wait_for_source(workspace.name, workspace, timeout=60), ("", None))
        self.assertLess(clock.monotonic() - started, 5)

    def test_deleting_a_run_tolerates_files_its_pass_removes_meanwhile(self) -> None:
        # jobs.delete_run removes the workspace while the run's pass may be
        # removing what it wrote after the deletion: each entry the pass got
        # to first is gone already, which is fine (Python 3.11 reports every
        # such entry, 3.12+ only the top level).
        import os as real_os

        real_unlink = real_os.unlink
        for name in ("the pass removed each file first", "the pass removed the whole tree"):
            with self.subTest(name), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                run_id = "deleted-concurrently"
                workspace = root / "runs" / "workspaces" / run_id
                for index in range(3):
                    (workspace / f"part{index}").mkdir(parents=True)
                    for leaf in range(4):
                        (workspace / f"part{index}" / f"f{leaf}.txt").write_text("x", encoding="utf-8")
                self.addCleanup(jobs._DELETED_RUNS.discard, run_id)
                state = {"raced": False, "inside": False}

                def racing_unlink(path, *args, name=name, workspace=workspace, state=state, **kwargs):
                    if state["inside"]:
                        return real_unlink(path, *args, **kwargs)  # the pass's own removal
                    if name == "the pass removed the whole tree":
                        if not state["raced"]:
                            state["raced"] = state["inside"] = True
                            try:
                                shutil.rmtree(workspace)  # the pass's cleanup wins the race
                            finally:
                                state["inside"] = False
                    else:
                        real_unlink(path, *args, **kwargs)  # the pass removed this file first
                    return real_unlink(path, *args, **kwargs)  # FileNotFoundError

                with (
                    patch.object(jobs, "RUNS_DIR", root / "runs"),
                    patch.object(jobs, "CACHE_DIR", root / "cache"),
                    patch("os.unlink", side_effect=racing_unlink),
                ):
                    removed = jobs.delete_run(run_id)
                self.assertIn(str(workspace), removed["deleted"])
                self.assertFalse(workspace.exists())
        # Any other error still fails the deletion.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "runs" / "workspaces" / "not-ours" / "sub").mkdir(parents=True)
            (root / "runs" / "workspaces" / "not-ours" / "sub" / "f.txt").write_text("x", encoding="utf-8")
            with (
                patch.object(jobs, "RUNS_DIR", root / "runs"),
                patch.object(jobs, "CACHE_DIR", root / "cache"),
                patch("os.unlink", side_effect=PermissionError("not ours")),
                self.assertRaises(PermissionError),
            ):
                jobs.delete_run("not-ours")
            jobs._DELETED_RUNS.discard("not-ours")

    def test_a_pass_whose_run_is_deleted_after_its_views_stops_waiting_for_the_run(self) -> None:
        # A member run still running after its first pass: the pass waits for
        # its end to derive again, unless the run is deleted meanwhile.
        import time as clock

        record = {"run_id": "deleted-after-views", "engine": "plain", "status": "running", "credential_source": "user"}
        workspace = self._workspace(record["run_id"])
        batches: list[str] = []
        polls: list[str] = []

        def run_record(_run_id):
            if batches:  # the pass derived its views and now waits for the run's end
                polls.append(record["status"])
                if len(polls) == 1:
                    self._delete(workspace)
            return dict(record)

        started = clock.monotonic()
        with (
            patch.object(auto_pipeline, "_wait_for_source", return_value=("sig-1", dict(record))),
            patch.object(auto_pipeline, "_run_record", side_effect=run_record),
            patch.object(auto_pipeline, "_run_user", return_value={"id": 7}),
            patch.object(auto_pipeline, "_run_batch", side_effect=lambda *_a, **_k: batches.append("done")),
            patch.dict("os.environ", {"AGENT_MONITOR_AUTO_PIPELINE_TIMEOUT": "60"}),
        ):
            auto_pipeline._run(workspace.name, workspace, 7, "m", "plain")
        self.assertLess(clock.monotonic() - started, 10)
        self.assertEqual((batches, polls[:1]), (["done"], ["running"]))  # it did wait
        self.assertFalse(workspace.exists())

    def test_the_pass_waits_as_long_as_a_three_hour_run_may_last(self) -> None:
        # Meta-Harness on an OpenAI model at Ultra may now run three hours
        # (jobs' watchdog); its pass must still wait for the final proof,
        # which the run writes at its end.
        import os as real_os

        from agent_monitor.jobs import _LONG_EFFORT_CLI_TIMEOUT

        long_wait = float(_LONG_EFFORT_CLI_TIMEOUT + 300)
        environ = {k: v for k, v in real_os.environ.items() if k != "AGENT_MONITOR_AUTO_PIPELINE_TIMEOUT"}
        ultra = {"auth_route": "api_key", "reasoning_effort": "max"}
        for engine, profile, expected in (
            ("metaharness", ultra, long_wait), ("plain", {**ultra, "reasoning_effort": "xhigh"}, long_wait),
            ("deepagents", {}, long_wait),
            # A default-effort run, or one on a linked account, keeps the usual wait.
            ("plain", {"auth_route": "api_key", "reasoning_effort": "default"}, 7200.0),
            ("metaharness", {"auth_route": "codex_subscription", "reasoning_effort": "max"}, 7200.0),
            ("openclaw", ultra, 7200.0),
        ):
            with self.subTest(engine=engine, profile=profile):
                workspace = self._workspace(f"wait-{engine}-{len(profile)}-{profile.get('auth_route', '')}")
                record = {"run_id": workspace.name, "engine": engine, "status": "finished", **profile}
                waits: list[float] = []

                def wait(_run_id, _workspace, *, timeout, record=record, waits=waits):
                    waits.append(timeout)
                    return "", dict(record)

                with (
                    patch.dict("os.environ", environ, clear=True),
                    patch.object(auto_pipeline, "_wait_for_source", side_effect=wait),
                    patch.object(auto_pipeline, "_run_record", return_value=dict(record)),
                ):
                    auto_pipeline._run(workspace.name, workspace, 7, "m", engine)
                self.assertEqual(waits, [expected])

    def test_a_live_run_still_fails_loudly(self) -> None:
        # Only a deleted run's errors are quiet.
        record = {"run_id": "live-run", "engine": "kimi", "status": "finished", "credential_source": "sponsored_kimi"}
        workspace = self._workspace(record["run_id"])
        with self.assertRaises(RuntimeError):
            with (
                patch.object(auto_pipeline, "_wait_for_source", return_value=("sig-1", dict(record))),
                patch.object(auto_pipeline, "_run_record", return_value=dict(record)),
                patch.object(auto_pipeline, "_run_user", return_value={"id": 7}),
                patch.object(auto_pipeline, "_run_batch", side_effect=RuntimeError("boom")),
            ):
                auto_pipeline._run(workspace.name, workspace, 7, "m", "kimi")
        self.assertTrue(workspace.exists())


if __name__ == "__main__":
    unittest.main()
