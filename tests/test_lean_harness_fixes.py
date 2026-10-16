"""Formal Lean coding-agent harness lifecycle regressions."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import console_server, lean_verify
from deploy.ansatze import auto_deploy
from tests.test_formal_harness_hardening import _checked_write
from tests.test_formal_ui_api import SCRIPT, _js_function, _run_node


FRESH_SOURCE = "theorem main (n : Nat) : n + 0 = n := by simp\n"
_CLAUDE_RUN = {
    "run_id": "claude-harness",
    "owner_id": 17,
    "auth_route": "claude_subscription",
    "model": "sonnet",
}


def _workspace_with_fresh_artifact(root: Path) -> tuple[Path, dict]:
    """A workspace whose Proof.lean changed after its pre-run fingerprint."""
    ldir = root / lean_verify.LEAN_DIRNAME
    ldir.mkdir()
    target = ldir / lean_verify.PROOF_FILENAME
    target.write_text("theorem old : True := by trivial\n", encoding="utf-8")
    _source, initial, _reason = lean_verify._regular_root_lean_artifact(root)
    target.write_text(FRESH_SOURCE, encoding="utf-8")
    return root, initial


def _running_base(workspace: Path, *, engine: str = "claude") -> dict:
    record = lean_verify._running_record(
        workspace,
        engine=engine,
        label="Claude Code",
        model="sonnet",
        lean_src="theorem old : True := by trivial\n",
        prev={},
    )
    lean_verify._write_cached(workspace, record)
    return record


def _js_function_with_defaults(name: str) -> str:
    """Like _js_function, for a signature with destructured defaults."""
    start = SCRIPT.index(f"function {name}(")
    opening = SCRIPT.index("){", start) + 1
    depth = 0
    for index in range(opening, len(SCRIPT)):
        if SCRIPT[index] == "{":
            depth += 1
        elif SCRIPT[index] == "}":
            depth -= 1
            if depth == 0:
                return SCRIPT[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {name}")


class _HarnessStateCleanup(unittest.TestCase):
    def tearDown(self) -> None:
        with lean_verify._HARNESS_LOCK:
            lean_verify._HARNESS_JOBS.clear()
        with lean_verify._FORMAL_OPERATIONS_GUARD:
            lean_verify._FORMAL_OPERATIONS.clear()


class AuditSetupFailureTests(_HarnessStateCleanup):
    """r2-lean-harness-1: a failed audit sign-in must not lose the result."""

    def test_disconnected_claude_audit_is_a_failed_audit_not_an_exception(self) -> None:
        with (
            patch("agent_monitor.auth.is_operator_account", return_value=True),
            patch("agent_monitor.claude_login.account_login_ready", return_value=False),
        ):
            result = lean_verify._harness_fidelity_audit(
                workspace=Path("/tmp/unused-formal-audit"),
                user={"id": 17},
                run_record=_CLAUDE_RUN,
                model="sonnet",
                problem="Prove True.",
                proof="Trivial.",
                lean="theorem main : True := by trivial\n",
            )

        self.assertFalse(result["ok"])
        self.assertIn("not connected", result["error"])

    def test_verified_harness_result_survives_a_disconnected_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, initial = _workspace_with_fresh_artifact(Path(tmp))
            _running_base(workspace)
            with (
                patch.object(lean_verify, "_write_and_run", side_effect=_checked_write),
                patch.object(lean_verify, "toolchain_status", return_value={}),
                patch("agent_monitor.auth.is_operator_account", return_value=True),
                patch("agent_monitor.claude_login.account_login_ready", return_value=False),
            ):
                result = lean_verify._harness_finish(
                    workspace=workspace,
                    engine="claude",
                    label="Claude Code",
                    log="",
                    exit_code=0,
                    elapsed=1.0,
                    user={"id": 17},
                    run_record=_CLAUDE_RUN,
                    model="sonnet",
                    problem="Prove True.",
                    informal="Trivial.",
                    audit=True,
                    initial_fingerprint=initial,
                    stopped=False,
                )
            saved = lean_verify.load_cached(workspace)

        # Kernel-accepted, exactly as when the audit provider is unreachable.
        self.assertEqual(result["status"], "verified")
        self.assertEqual(saved["status"], "verified")
        self.assertEqual(len(saved["attempts"]), 1)
        self.assertEqual(saved["lean"], FRESH_SOURCE)
        self.assertFalse(saved["fidelity"]["audit"]["ok"])
        self.assertIn("not connected", saved["fidelity"]["audit"]["error"])

    def test_worker_failure_closes_the_running_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            ldir = workspace / lean_verify.LEAN_DIRNAME
            ldir.mkdir()
            base = _running_base(workspace)
            token = lean_verify._reserve_formal_operation(workspace, "harness/claude")
            with (
                patch.object(
                    lean_verify,
                    "_harness_finish",
                    side_effect=RuntimeError("audit setup exploded"),
                ),
                self.assertRaisesRegex(RuntimeError, "audit setup exploded"),
            ):
                lean_verify._harness_worker_guarded(
                    operation_token=token,
                    workspace=workspace,
                    ldir=ldir,
                    argv=[sys.executable, "-c", "print('agent done')"],
                    engine="claude",
                    label="Claude Code",
                    user={"id": 17},
                    model="sonnet",
                    run_record=_CLAUDE_RUN,
                    env={},
                    problem="Prove True.",
                    informal="Trivial.",
                    base=base,
                    initial_fingerprint=None,
                    audit=True,
                )
            saved = lean_verify.load_cached(workspace)
            released = lean_verify._reserve_formal_operation(workspace, "after")
            lean_verify._release_formal_operation(workspace, released)

        self.assertEqual(saved["status"], "failed")
        self.assertNotEqual(saved["harness"]["state"], "running")
        self.assertIn("audit setup exploded", saved["notes"])
        self.assertIn("audit setup exploded", saved["log"])
        self.assertEqual(saved["lean"], base["lean"])



class OrphanedHarnessRecordTests(_HarnessStateCleanup):
    """r2-lean-harness-0: a restart must not leave a harness running for good."""

    @staticmethod
    def _orphan(root: Path, run_id: str) -> Path:
        workspace = root / run_id
        workspace.mkdir()
        record = _running_base(workspace)
        record["attempts"] = [{"round": 0, "action": "check"}]
        record["chat"] = [{"role": "user", "content": "Prove it."}]
        lean_verify._write_cached(workspace, record)
        return workspace

    def test_startup_sweep_closes_records_left_running_by_a_previous_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._orphan(root, "run-orphan")
            finished = root / "run-finished"
            finished.mkdir()
            lean_verify._write_cached(finished, {"status": "verified", "lean": "x"})
            self.assertTrue(auto_deploy.record_busy(lean_verify.load_cached(workspace)))

            closed = lean_verify.reconcile_orphaned_harnesses(root)
            saved = lean_verify.load_cached(workspace)
            untouched = lean_verify.load_cached(finished)

        self.assertEqual(closed, ["run-orphan"])
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["harness"]["state"], "interrupted")
        self.assertIn("did not finish", saved["notes"])
        self.assertEqual(saved["attempts"], [{"round": 0, "action": "check"}])
        self.assertEqual(saved["chat"], [{"role": "user", "content": "Prove it."}])
        self.assertIn("theorem old", saved["lean"])
        self.assertFalse(auto_deploy.record_busy(saved))
        self.assertEqual(untouched, {"status": "verified", "lean": "x"})

    def test_a_live_harness_record_is_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._orphan(Path(tmp), "run-live")
            before = lean_verify.load_cached(workspace)
            token = lean_verify._reserve_formal_operation(workspace, "harness/claude")
            try:
                self.assertFalse(lean_verify.reconcile_orphaned_harness(workspace))
                self.assertEqual(lean_verify.reconcile_orphaned_harnesses(Path(tmp)), [])
            finally:
                lean_verify._release_formal_operation(workspace, token)
            after = lean_verify.load_cached(workspace)

        self.assertEqual(after, before)

    def test_lean_tab_persists_the_end_of_an_orphaned_harness(self) -> None:
        handler = object.__new__(console_server.Handler)
        handler.path = "/api/run/run-orphan/lean_verify"
        handler._resolve_path = lambda: "/api/run/run-orphan/lean_verify"
        handler._require_user = lambda _path: {"id": 17, "is_admin": True}
        handler._owns_run = lambda _rid, _user: True
        sent: list[tuple[int, dict]] = []
        handler._send = lambda code, payload, *args, **kwargs: sent.append(
            (code, json.loads(payload))
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._orphan(Path(tmp), "run-orphan")
            with (
                patch.object(console_server, "_workspace_for_run", return_value=workspace),
                patch.object(console_server, "_run_record_for", return_value=_CLAUDE_RUN),
                patch.object(
                    lean_verify,
                    "harness_model_options",
                    return_value={"auth_route": "claude_subscription", "models": [], "selected": ""},
                ),
                patch.object(lean_verify, "harness_engines", return_value=[]),
            ):
                handler.do_GET()
            saved = lean_verify.load_cached(workspace)

        self.assertEqual(sent[0][0], 200)
        self.assertEqual(sent[0][1]["status"], "failed")
        self.assertFalse(sent[0][1]["harness_running"])
        self.assertEqual(saved["status"], "failed")
        self.assertFalse(auto_deploy.record_busy(saved))

    def test_console_startup_runs_the_sweep(self) -> None:
        class Server:
            def __init__(self, *_args) -> None:
                pass

            def serve_forever(self) -> None:
                pass

        with (
            patch.object(console_server, "ensure_data_dirs"),
            patch.object(console_server, "_open_account_database"),
            patch.object(console_server.job_manager, "reconcile_stale_running_runs", return_value=[]),
            patch.object(console_server.job_manager, "remove_legacy_codex_key_homes", return_value=0),
            patch.object(console_server, "start_guest_retention_thread", return_value=None),
            patch("agent_monitor.usage_ledger.start_backfill_thread"),
            patch.object(console_server, "ConsoleHTTPServer", Server),
            patch.object(
                lean_verify, "reconcile_orphaned_harnesses", return_value=["run-orphan"]
            ) as sweep,
            patch("builtins.print"),
        ):
            console_server.main(port=1)

        sweep.assert_called_once_with(console_server.WORKSPACES_ROOT)



class HarnessTimeoutTests(_HarnessStateCleanup):
    """r2-lean-harness-6: a harness killed at its limit says it timed out."""

    def test_timeout_is_recorded_as_a_timeout_not_exit_minus_nine(self) -> None:
        child = (
            "import pathlib, time\n"
            f"pathlib.Path('Proof.lean').write_text({FRESH_SOURCE!r})\n"
            "print('iterating with ./check.sh', flush=True)\n"
            "time.sleep(30)\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            ldir = workspace / lean_verify.LEAN_DIRNAME
            ldir.mkdir()
            base = _running_base(workspace)
            with (
                patch.dict(os.environ, {"AGENT_MONITOR_LEAN_HARNESS_TIMEOUT": "1"}),
                patch.object(lean_verify, "_write_and_run", side_effect=_checked_write),
                patch.object(lean_verify, "toolchain_status", return_value={}),
            ):
                lean_verify._harness_worker(
                    workspace=workspace,
                    ldir=ldir,
                    argv=[sys.executable, "-c", child],
                    engine="claude",
                    label="Claude Code",
                    user={"id": 17},
                    model="sonnet",
                    run_record=_CLAUDE_RUN,
                    env={"PATH": os.environ.get("PATH", "")},
                    problem="Prove n + 0 = n.",
                    informal="Simplify.",
                    base=base,
                    initial_fingerprint=None,
                    audit=False,
                )
            saved = lean_verify.load_cached(workspace)

        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["harness"]["state"], "timed_out")
        self.assertTrue(saved["harness"]["timed_out"])
        self.assertFalse(saved["harness"]["artifact_accepted"])
        self.assertIn("timed out", saved["notes"])
        self.assertIn("1s limit", saved["notes"])
        self.assertNotIn("exited -9", saved["notes"])
        self.assertIn("1s limit", saved["log"])

    def test_a_stop_at_the_deadline_is_still_a_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, initial = _workspace_with_fresh_artifact(Path(tmp))
            with (
                patch.object(lean_verify, "_write_and_run", side_effect=_checked_write),
                patch.object(lean_verify, "toolchain_status", return_value={}),
            ):
                result = lean_verify._harness_finish(
                    workspace=workspace,
                    engine="claude",
                    label="Claude Code",
                    log="",
                    exit_code=-9,
                    elapsed=5.0,
                    user=None,
                    run_record=None,
                    model=None,
                    problem="Prove n + 0 = n.",
                    informal="Simplify.",
                    audit=False,
                    initial_fingerprint=initial,
                    stopped=True,
                    timeout_limit=5,
                )

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["harness"]["state"], "stopped")
        self.assertFalse(result["harness"]["timed_out"])
        self.assertIn("stopped by user", result["notes"])



class HarnessStopWindowTests(_HarnessStateCleanup):
    """r2-lean-harness-5: Stop works while the agent launches or is checked."""

    @staticmethod
    def _launch(workspace: Path, argv: list[str]) -> threading.Thread:
        """Register and start a worker exactly as _start_harness_reserved does."""
        ldir = workspace / lean_verify.LEAN_DIRNAME
        ldir.mkdir(exist_ok=True)
        base = _running_base(workspace)
        token = lean_verify._reserve_formal_operation(workspace, "harness/claude")
        job = {
            "engine": "claude",
            "label": "Claude Code",
            "started_at": time.time(),
            "state": "running",
            "proc": None,
            "stopping": False,
            "operation_token": token,
        }
        thread = threading.Thread(
            target=lean_verify._harness_worker_guarded,
            kwargs={
                "operation_token": token,
                "workspace": workspace,
                "ldir": ldir,
                "argv": argv,
                "engine": "claude",
                "label": "Claude Code",
                "user": None,
                "model": "sonnet",
                "run_record": None,
                "env": {"PATH": os.environ.get("PATH", "")},
                "problem": "Prove n + 0 = n.",
                "informal": "Simplify.",
                "base": base,
                "initial_fingerprint": None,
                "audit": False,
            },
            daemon=True,
        )
        job["thread"] = thread
        with lean_verify._HARNESS_LOCK:
            lean_verify._HARNESS_JOBS[lean_verify._harness_key(workspace)] = job
            thread.start()
        return thread

    def test_stop_during_launch_stops_the_agent(self) -> None:
        real_popen = lean_verify.subprocess.Popen
        stop_results: list[object] = []

        def popen_after_stop(*args, **kwargs):
            # Stop lands after registration, before the process exists.
            if not stop_results:
                try:
                    stop_results.append(lean_verify.stop_harness(workspace))
                except ValueError as exc:
                    stop_results.append(exc)
            return real_popen(*args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            started = time.monotonic()
            with (
                patch.object(lean_verify.subprocess, "Popen", side_effect=popen_after_stop),
                patch.object(lean_verify, "toolchain_status", return_value={}),
            ):
                thread = self._launch(
                    workspace, [sys.executable, "-c", "import time; time.sleep(6)"]
                )
                thread.join(15)
            elapsed = time.monotonic() - started
            saved = lean_verify.load_cached(workspace)

        self.assertEqual(stop_results, [{"stopping": True}])
        self.assertLess(elapsed, 5)
        self.assertEqual(saved["status"], "stopped")
        self.assertEqual(saved["harness"]["state"], "stopped")

    def test_stop_while_the_result_is_checked_reports_finishing(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def slow_finish(**_kwargs):
            entered.set()
            release.wait(10)  # the sandboxed check and the fidelity audit
            return {}

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            with patch.object(lean_verify, "_harness_finish", side_effect=slow_finish):
                thread = self._launch(workspace, [sys.executable, "-c", "print('done')"])
                try:
                    self.assertTrue(entered.wait(10))
                    job = lean_verify.harness_job(workspace)
                    stopped = lean_verify.stop_harness(workspace)
                    with lean_verify._HARNESS_LOCK:
                        flag = lean_verify._HARNESS_JOBS[
                            lean_verify._harness_key(workspace)
                        ]["stopping"]
                finally:
                    release.set()
                    thread.join(10)

        self.assertEqual(job["state"], "finishing")
        self.assertEqual(stopped, {"stopping": False, "finishing": True})
        self.assertFalse(flag)

    def test_harness_button_is_not_a_stop_button_while_finishing(self) -> None:
        output = _run_node(
            """
let CURRENT_USER={id:1,guest:false};
let runId='run-a';
let lastSandboxRun=null;
let leanEngines=[{id:'claude',available:true}];
const leanPendingRuns=new Map(), leanRunningRuns=new Map();
const els={};
function $(id){ return els[id]||(els[id]={id,textContent:'',title:'',disabled:false,style:{},dataset:{},value:''}); }
$('lean-engine').value='claude';
function fillLeanModels(){}
"""
            + _js_function("syncLeanControls")
            + """
const state=()=>({text:$('lean-harness').textContent,disabled:$('lean-harness').disabled});
leanRunningRuns.set('run-a',{label:'Claude Code',finishing:true});
syncLeanControls();
const finishing=state();
leanRunningRuns.set('run-a',{label:'Claude Code'});
syncLeanControls();
const running=state();
console.log(JSON.stringify({finishing,running}));
"""
        )
        self.assertNotIn("Stop", output["finishing"]["text"])
        self.assertTrue(output["finishing"]["disabled"])
        self.assertIn("Stop", output["running"]["text"])
        self.assertFalse(output["running"]["disabled"])

    def test_unchanged_lean_payload_still_tracks_the_harness_state(self) -> None:
        output = _run_node(
            """
let runId='run-a', leanRunKey='', leanDirty=false;
const calls=[];
const els={};
function $(id){ return els[id]||(els[id]={style:{},dataset:{},textContent:'',value:'',innerHTML:'',title:'',scrollTop:0,clientHeight:0,scrollHeight:0}); }
function setLeanBadge(){} function setStatementsLeanBadge(){} function leanForAnalysis(){ return false; }
function leanStatus(){} function leanToolchainLabel(){ return ''; } function escapeHtml(v){ return String(v); }
function leanAttemptAxioms(){ return ''; } function leanAttemptLabel(){ return ''; } function leanEditorText(){ return ''; }
function renderLeanFidelity(){} function renderLeanCitations(){} function renderLeanChat(){}
function setLeanRunning(running,label,target,finishing){ calls.push([running,!!finishing]); }
"""
            + _js_function_with_defaults("renderLean")
            + """
const d={status:'running',harness_running:true,generated_at:'t',action:'harness',log:'x',
  harness:{label:'Claude Code',state:'running'}};
renderLean(d);
renderLean({...d});
renderLean({...d,harness:{label:'Claude Code',state:'finishing'}});
console.log(JSON.stringify(calls));
"""
        )
        # A quiet agent leaves the payload unchanged: polling must continue,
        # and the move to "finishing" must still reach the Harness button.
        self.assertEqual(output, [[True, False], [True, False], [True, True]])


if __name__ == "__main__":
    unittest.main()
