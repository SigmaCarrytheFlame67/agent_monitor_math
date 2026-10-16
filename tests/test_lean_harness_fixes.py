"""Formal Lean coding-agent harness lifecycle regressions."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import console_server, lean_verify
from deploy.ansatze import auto_deploy
from tests.test_formal_harness_hardening import _checked_write


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


if __name__ == "__main__":
    unittest.main()
