"""Formal Lean coding-agent harness lifecycle regressions."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import auto_pipeline, console_server, lean_verify
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



class CodexAuditModelTests(_HarnessStateCleanup):
    """r2-lean-harness-3: the Codex audit runs on the harness's model."""

    CODEX_RUN = {
        "run_id": "codex-harness",
        "owner_id": 17,
        "auth_route": "codex_subscription",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "speed_mode": "standard",
    }

    def _audit(self, model: str) -> tuple[dict, list[str]]:
        from agent_monitor.engines_registry import CLI_ENGINES

        captured: dict = {}
        verdict = json.dumps(
            {"faithful": True, "severity": "ok", "verdict": "matches", "issues": [],
             "main_decl": "main", "statement_suggestion": ""}
        )

        def fake_run(argv, **_kwargs):
            captured["argv"] = list(argv)
            events = [
                {"type": "item.completed",
                 "item": {"id": "a", "type": "agent_message", "text": verdict}},
                {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}},
            ]
            return lean_verify.subprocess.CompletedProcess(
                argv, 0, "\n".join(json.dumps(event) for event in events), ""
            )

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / lean_verify.LEAN_DIRNAME).mkdir()
            with (
                patch("agent_monitor.auth.is_operator_account", return_value=True),
                patch("agent_monitor.codex_login.account_login_ready", return_value=True),
                patch("agent_monitor.codex_login.account_home", return_value=workspace / "codex"),
                patch(
                    "agent_monitor.codex_login.available_models",
                    return_value=["gpt-5.6-sol", "gpt-5.4-mini"],
                ),
                patch(
                    "agent_monitor.engines_registry._cli_available",
                    return_value=(True, CLI_ENGINES["codex"]["default_cmd"]),
                ),
                patch(
                    "agent_monitor.runners.codex_backend._run_codex_process",
                    side_effect=fake_run,
                ),
            ):
                result = lean_verify._harness_fidelity_audit(
                    workspace=workspace,
                    user={"id": 17, "is_admin": True},
                    run_record=self.CODEX_RUN,
                    model=model,
                    problem="Prove n + 0 = n.",
                    proof="Simplify.",
                    lean=FRESH_SOURCE,
                )
        return result, captured["argv"]

    def test_audit_is_bound_to_the_harness_model_and_run_tuning(self) -> None:
        result, argv = self._audit("gpt-5.6-sol")

        self.assertTrue(result["ok"])
        self.assertEqual(result["model"], "gpt-5.6-sol")
        self.assertEqual(argv[argv.index("--model") + 1], "gpt-5.6-sol")
        self.assertIn('model_reasoning_effort="high"', argv)
        prompt_index = len(argv) - 1
        self.assertLess(argv.index("--model"), prompt_index)

    def test_audit_follows_a_harness_model_other_than_the_runs(self) -> None:
        result, argv = self._audit("gpt-5.4-mini")

        self.assertEqual(result["model"], "gpt-5.4-mini")
        self.assertEqual(argv[argv.index("--model") + 1], "gpt-5.4-mini")

    def test_question_fallback_binds_only_a_model_the_codex_route_offers(self) -> None:
        cases = (("gpt-5.4-mini", "gpt-5.4-mini"), ("Codex subscription", None))
        for recorded, expected in cases:
            with self.subTest(recorded=recorded):
                with (
                    patch.object(lean_verify, "_llm_environment", return_value={}),
                    patch.object(lean_verify, "_chat", side_effect=ValueError("no API key")),
                    patch.object(
                        lean_verify,
                        "harness_model_options",
                        return_value={
                            "auth_route": "codex_subscription",
                            "models": ["gpt-5.6-sol", "gpt-5.4-mini"],
                            "selected": "gpt-5.6-sol",
                        },
                    ),
                    patch.object(
                        lean_verify,
                        "_codex_subscription_interaction",
                        return_value={"answer": "It compiles.", "lean": FRESH_SOURCE, "model": "m"},
                    ) as codex,
                ):
                    lean_verify._answer_lean_question(
                        workspace=Path("/tmp/unused-question"),
                        user={"id": 17, "is_admin": True},
                        run_record=self.CODEX_RUN,
                        question="Why does line 3 need simp?",
                        model=None,
                        lean_src=FRESH_SOURCE,
                        cached={"status": "verified", "model": recorded},
                    )
                self.assertEqual(codex.call_args.kwargs["model"], expected)



class ClaudeHarnessTranscriptTests(unittest.TestCase):
    """r2-lean-harness-4: the Claude Code harness transcript reaches the UI."""

    @staticmethod
    def _summary(events: list[dict]) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            ldir = workspace / lean_verify.LEAN_DIRNAME
            ldir.mkdir()
            (ldir / "harness.log").write_text(
                "\n".join(json.dumps(event) for event in events)
                + "\n[agent-monitor] a plain runtime line\n",
                encoding="utf-8",
            )
            lean_verify._write_cached(
                workspace,
                {"status": "verified", "harness": {"engine": "claude", "state": "finished"}},
            )
            return lean_verify.monitor_summary(workspace)

    def test_claude_stream_json_becomes_messages_commands_and_usage(self) -> None:
        summary = self._summary([
            {"type": "system", "subtype": "init", "model": "claude-sonnet-5-5",
             "tools": ["Bash", "Edit"], "session_id": "s"},
            {"type": "assistant", "message": {"id": "m1", "role": "assistant", "content": [
                {"type": "thinking", "thinking": "Check the file first."},
                {"type": "text", "text": "I will run the checker."},
                {"type": "tool_use", "id": "tu1", "name": "Bash",
                 "input": {"command": "./check.sh", "description": "Check"}},
            ], "usage": {"input_tokens": 10, "output_tokens": 5}}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu1",
                 "content": [{"type": "text", "text": "Proof.lean:3:2: error: unsolved goals"}],
                 "is_error": True},
            ]}},
            {"type": "assistant", "message": {"id": "m2", "role": "assistant", "content": [
                {"type": "tool_use", "id": "tu2", "name": "Edit",
                 "input": {"file_path": "/w/lean/Proof.lean", "old_string": "a", "new_string": "b"}},
            ]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu2", "content": "The file was updated."},
            ]}},
            {"type": "assistant", "message": {"id": "m3", "role": "assistant", "content": [
                {"type": "text", "text": "Done: Proof.lean compiles with no sorry."},
            ]}},
            {"type": "result", "subtype": "success", "is_error": False,
             "result": "Done: Proof.lean compiles with no sorry.", "total_cost_usd": 0.01,
             "usage": {"input_tokens": 100, "cache_read_input_tokens": 400,
                       "cache_creation_input_tokens": 50, "output_tokens": 60}},
        ])

        events = summary["conversation"]
        kinds = [event["kind"] for event in events]
        self.assertEqual(kinds[0], "runtime")
        self.assertIn("a plain runtime line", events[0]["content"])
        self.assertEqual(
            kinds[1:],
            ["reasoning", "agent_message", "command_execution", "command_execution", "agent_message"],
        )
        check, edit = events[3], events[4]
        self.assertEqual(check["command"], "./check.sh")
        self.assertIn("unsolved goals", check["content"])
        self.assertEqual(check["status"], "failed")
        self.assertEqual(edit["command"], "Edit /w/lean/Proof.lean")
        self.assertEqual(edit["status"], "completed")
        self.assertEqual(events[-1]["content"], "Done: Proof.lean compiles with no sorry.")
        self.assertEqual(summary["conversation_event_count"], 6)
        # The final total in the shape the monitor reads (input includes cache).
        self.assertEqual(
            summary["harness_usage"],
            {"input_tokens": 550, "cached_input_tokens": 400,
             "cache_write_input_tokens": 50, "output_tokens": 60},
        )

    def test_a_terminal_claude_error_is_shown_once(self) -> None:
        summary = self._summary([
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Not logged in · Please run /login"}]}},
            {"type": "result", "subtype": "success", "is_error": True,
             "result": "Not logged in · Please run /login", "usage": {}},
            {"type": "result", "subtype": "error_max_turns", "is_error": True,
             "errors": ["Reached the maximum number of turns (40)"], "usage": {}},
        ])

        contents = [event["content"] for event in summary["conversation"][1:]]
        self.assertEqual(
            contents,
            ["Not logged in · Please run /login", "Reached the maximum number of turns (40)"],
        )



class HarnessDerivedViewTests(_HarnessStateCleanup):
    """r2-lean-harness-2: a harness result reaches the pipeline's views."""

    RUN = {**_CLAUDE_RUN, "engine": "math_harness"}

    def _run_worker(self, workspace: Path, child: str) -> list[dict]:
        ldir = workspace / lean_verify.LEAN_DIRNAME
        ldir.mkdir(exist_ok=True)
        (ldir / lean_verify.PROOF_FILENAME).write_text(
            "theorem old : True := by trivial\n", encoding="utf-8"
        )
        _source, initial, _reason = lean_verify._regular_root_lean_artifact(workspace)
        base = _running_base(workspace)
        refreshes: list[dict] = []

        def refresh(**kwargs):
            # The harness reservation must be released before the refresh.
            probe = lean_verify._reserve_formal_operation(workspace, "probe")
            lean_verify._release_formal_operation(workspace, probe)
            refreshes.append(kwargs)
            return True

        token = lean_verify._reserve_formal_operation(workspace, "harness/claude")
        with (
            patch.object(lean_verify, "_write_and_run", side_effect=_checked_write),
            patch.object(lean_verify, "toolchain_status", return_value={}),
            patch.object(auto_pipeline, "refresh_formal", side_effect=refresh),
        ):
            lean_verify._harness_worker_guarded(
                operation_token=token,
                run_id="run-derived",
                workspace=workspace,
                ldir=ldir,
                argv=[sys.executable, "-c", child],
                engine="claude",
                label="Claude Code",
                user={"id": 17, "is_admin": True},
                model="sonnet",
                run_record=self.RUN,
                env={"PATH": os.environ.get("PATH", "")},
                problem="Prove n + 0 = n.",
                informal="Simplify.",
                base=base,
                initial_fingerprint=initial,
                audit=False,
            )
        return refreshes

    def test_a_harness_that_changed_proof_lean_refreshes_the_formal_views(self) -> None:
        child = f"import pathlib; pathlib.Path('Proof.lean').write_text({FRESH_SOURCE!r})"
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            refreshes = self._run_worker(workspace, child)
            saved = lean_verify.load_cached(workspace)

        self.assertEqual(saved["status"], "verified")
        self.assertEqual(
            refreshes,
            [{
                "run_id": "run-derived",
                "workspace": workspace,
                "owner_id": 17,
                "model": "sonnet",
                "engine": "math_harness",
            }],
        )

    def test_an_unchanged_proof_lean_spends_no_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            refreshes = self._run_worker(Path(tmp).resolve(), "print('nothing to do')")

        self.assertEqual(refreshes, [])

    def test_start_harness_hands_the_run_id_to_its_worker(self) -> None:
        captured: dict = {}

        class FakeThread:
            def __init__(self, *, kwargs, **_unused):
                captured.update(kwargs)

            def start(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("agent_monitor.claude_login.account_home", return_value=Path(tmp) / "c"),
                patch("agent_monitor.claude_login.account_login_ready", return_value=True),
                patch("agent_monitor.auth.is_operator_account", return_value=True),
                patch("agent_monitor.engines_registry._cli_available", return_value=(True, "echo {prompt}")),
                patch.object(lean_verify, "toolchain_status", return_value={"available": True}),
                patch("agent_monitor.lean_verify.threading.Thread", FakeThread),
            ):
                lean_verify.start_harness(
                    workspace=Path(tmp),
                    run_record=self.RUN,
                    user={"id": 17, "is_admin": True},
                    engine="claude",
                    run_id="run-derived",
                )

        self.assertEqual(captured["run_id"], "run-derived")

    def _post_harness(self, workspace: Path, pipeline: dict) -> tuple[list, object]:
        auto_pipeline._write_state(workspace, pipeline)
        body = json.dumps({"action": "harness", "engine": "claude", "model": "sonnet"}).encode()
        handler = object.__new__(console_server.Handler)
        handler.path = "/api/run/run-derived/lean_verify"
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler._resolve_path = lambda: "/api/run/run-derived/lean_verify"
        handler._reject_cross_origin_write = lambda: False
        handler._require_user = lambda _path: {"id": 17, "is_admin": True}
        handler._owns_run = lambda _rid, _user: True
        sent: list = []
        handler._send = lambda code, payload, *args, **kwargs: sent.append(
            (code, json.loads(payload), kwargs.get("headers") or {})
        )
        with (
            patch.object(console_server, "_workspace_for_run", return_value=workspace),
            patch.object(console_server, "_run_record_for", return_value=self.RUN),
            patch.object(
                lean_verify, "start_harness", return_value={"status": "running"}
            ) as start,
        ):
            handler.do_POST()
        return sent, start

    def test_harness_waits_while_the_pipeline_may_still_run_its_lean_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sent, start = self._post_harness(
                Path(tmp),
                {"status": "running", "stages": {"lean": {"status": "waiting"}}},
            )

        start.assert_not_called()
        self.assertEqual(sent[0][0], 409)
        self.assertIn("pipeline", sent[0][1]["error"])
        self.assertEqual(sent[0][2].get("Retry-After"), "20")

    def test_harness_starts_when_formal_stages_are_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sent, start = self._post_harness(
                Path(tmp),
                {"status": "running", "stages": {"lean": {"status": "off"}}},
            )

        self.assertEqual(sent[0][0], 200)
        self.assertEqual(start.call_args.kwargs["run_id"], "run-derived")

    def test_harness_button_waits_for_the_pipeline_but_stop_never_does(self) -> None:
        output = _run_node(
            """
let CURRENT_USER={id:1,guest:false};
let runId='run-a';
let lastSandboxRun={run_id:'run-a',auto_pipeline:{status:'running',stages:{lean:{status:'running'}}}};
let leanEngines=[{id:'claude',available:true}];
const leanPendingRuns=new Map(), leanRunningRuns=new Map();
const els={};
function $(id){ return els[id]||(els[id]={id,textContent:'',title:'',disabled:false,style:{},dataset:{},value:''}); }
$('lean-engine').value='claude';
function fillLeanModels(){}
"""
            + _js_function("syncLeanControls")
            + """
const state=()=>({text:$('lean-harness').textContent,disabled:$('lean-harness').disabled,check:$('lean-check').disabled});
syncLeanControls();
const waiting=state();
leanRunningRuns.set('run-a',{label:'Claude Code'});
syncLeanControls();
const stop=state();
leanRunningRuns.delete('run-a');
lastSandboxRun.auto_pipeline.stages.lean.status='off';
syncLeanControls();
const off=state();
console.log(JSON.stringify({waiting,stop,off}));
"""
        )
        self.assertTrue(output["waiting"]["disabled"])
        self.assertFalse(output["waiting"]["check"])  # admins' other actions unchanged
        self.assertIn("Stop", output["stop"]["text"])
        self.assertFalse(output["stop"]["disabled"])
        self.assertFalse(output["off"]["disabled"])


if __name__ == "__main__":
    unittest.main()
