"""Editing a published Ansatz CGM proof in the Sandbox never blocks the run's
next pass (a follow-up or Continue).

``proof.md`` is the one file of a CGM generation its owner may rewrite
(``POST /api/workspace/<run>/file``).  The edit unbinds the generation's
receipt: Facts shows the facts unattested, the write-up turns stale and the
derived views (DAGs, Lean, coverage) are marked stale.  The facts are still
the runner's own, so the next pass is shown the edited proof, retires that
generation as it retires any other and publishes its own ``proof.md`` bound to
its own receipt.  A receipt, target, problem or fact that no longer matches is
still rejected before any model call.  Every provider here is a stub.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_monitor import (
    auto_pipeline,
    console_server,
    engine_graph,
    jobs,
    proof_graph,
    run_sharing,
)
from agent_monitor.cgm import schema as cgm_schema
from agent_monitor.engines_registry import proof_prompt
from agent_monitor.runners import math_harness_runner as runner

from tests.test_cgm_concurrency import _load_fixture, _write_files
from tests.test_cgm_writeup import ACCEPTED, OK, WRITEUP, _pipeline as _writeup_pass
from tests.test_engine_graph import _write_legacy_generation
from tests.test_math_harness_runner import (
    PROBLEM,
    _accepted_outputs,
    _api_run,
    _curator,
    _ok,
    _strategist,
    _worker,
)

RECEIPT = "math_harness/project/engine_receipt.json"
FACTS = "math_harness/project/fact_graph/facts"
NOTE = "\n> Reader's note: the closure step also covers m + n = 0.\n"
DERIVED_STAGES = ("informal_dag", "lean", "formal_dag", "coverage")


def _renewed() -> list[object]:
    """An accepted follow-up pass whose facts differ from the first pass's."""
    return [_strategist(2), _worker("renewed"), _worker("other"), _ok(), _ok(), _curator()]


def _receipt(workspace: Path) -> dict:
    return json.loads((workspace / RECEIPT).read_text(encoding="utf-8"))


def _fact_bytes(workspace: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in (workspace / FACTS).iterdir()}


def _attestation(workspace: Path) -> str:
    return engine_graph.build(workspace, {"engine": "math_harness"})["provenance"]["attestation"]


def _errors(events: list[dict]) -> list[str]:
    return [
        event["item"]["message"]
        for event in events
        if event.get("type") == "item.completed" and event["item"].get("type") == "error"
    ]


def _published(workspace: Path) -> None:
    """A math_harness run as jobs launches it: problem.txt, no task argument."""
    (workspace / "problem.txt").write_text(PROBLEM + "\n", encoding="utf-8")
    code, calls, _events = _api_run(workspace, _accepted_outputs(), task=None)
    assert (code, len(calls)) == (0, 6)
    assert _attestation(workspace) == "harness-gate-receipt"


def _sandbox_save(workspace: Path, content: str) -> None:
    """The Sandbox editor's save (the route below adds mark_stale)."""
    code, result = console_server._save_sandbox_proof(
        workspace,
        content=content,
        expected_revision=console_server._file_revision(workspace / "proof.md"),
    )
    assert code == 200, result


def _kimi_pass(workspace: Path, replies: list[object], *, task: str | None) -> list[dict]:
    """One accepted pass on the Kimi route as kimi_runner runs it (run_pipeline
    with the console's prompt as task context), one call at a time."""
    queue = list(replies)
    calls: list[dict] = []

    def fake_api(system: str, user: str, *, model: str, timeout: int = 240, **kwargs):
        calls.append({"system": system, "user": user})
        if not queue:
            raise AssertionError("unexpected extra API call")
        return SimpleNamespace(
            text=queue.pop(0), usage={"input_tokens": 3, "output_tokens": 4}, model=model, provider="kimi"
        )

    env = {
        "AGENT_MONITOR_PROBLEM_ID": "case-1",
        "AGENT_MONITOR_CGM_CONCURRENCY": "1",
        "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "2",
    }
    with (
        patch.dict(os.environ, env, clear=True),
        patch.object(runner, "api_chat", side_effect=fake_api),
    ):
        result = runner.run_pipeline(
            workspace,
            PROBLEM,
            runner.Route("api", "kimi-k3", "api_chat"),
            task=task,
            emit=lambda _event: None,
        )
    assert result["accepted"] and len(calls) == len(replies), (result, len(calls))
    return calls


class EditedGenerationRunnerTests(unittest.TestCase):
    def test_the_next_pass_retires_an_edited_generation_and_binds_its_own_proof(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            _published(workspace)
            old = _fact_bytes(workspace)
            first = _receipt(workspace)
            edited = (workspace / "proof.md").read_text(encoding="utf-8") + NOTE
            _sandbox_save(workspace, edited)
            # The edit unbinds the receipt for readers; the facts stay owned.
            self.assertEqual(_attestation(workspace), "unattested")
            self.assertEqual(
                sorted(runner._prior_generated_fact_ids(workspace)),
                sorted(item["fact_id"] for item in first["facts"]),
            )

            code, calls, events = _api_run(workspace, _renewed(), task=None)
            self.assertEqual((code, len(calls), _errors(events)), (0, 6, []))
            self.assertTrue(set(old).isdisjoint(_fact_bytes(workspace)))
            receipt = _receipt(workspace)
            self.assertNotEqual(receipt["binding_sha256"], first["binding_sha256"])
            self.assertEqual(
                sorted(f"{item['fact_id']}.md" for item in receipt["facts"]),
                sorted(_fact_bytes(workspace)),
            )
            # The pass published its own proof.md, bound to its own receipt.
            proof = (workspace / "proof.md").read_bytes()
            self.assertEqual(receipt["proof_sha256"], hashlib.sha256(proof).hexdigest())
            self.assertNotIn(b"Reader's note", proof)
            self.assertEqual(_attestation(workspace), "harness-gate-receipt")

    def test_an_edit_over_the_runners_own_proof_bound_does_not_block_the_next_pass(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            _published(workspace)
            # The Sandbox accepts 400 KB; the runner's own proof stays smaller.
            edited = (workspace / "proof.md").read_text(encoding="utf-8") + NOTE * 4_500
            self.assertLess(runner.MAX_PROOF_BYTES + 10_000, len(edited.encode()))
            self.assertLess(len(edited.encode()), 400_000)
            _sandbox_save(workspace, edited)
            code, calls, events = _api_run(workspace, _renewed(), task=None)
            self.assertEqual((code, len(calls), _errors(events)), (0, 6, []))
            self.assertEqual(_attestation(workspace), "harness-gate-receipt")

    def test_an_edit_saved_while_a_pass_runs_does_not_fail_its_publication(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            _published(workspace)
            real_run_cgm = runner._run_cgm

            def run_then_edit(*args, **kwargs):
                result = real_run_cgm(*args, **kwargs)
                # Saved after every call, just before publication starts.
                _sandbox_save(workspace, (workspace / "proof.md").read_text(encoding="utf-8") + NOTE)
                return result

            with patch.object(runner, "_run_cgm", side_effect=run_then_edit):
                code, calls, events = _api_run(workspace, _renewed(), task=None)
            # The pass publishes, as the editor's "New engine version" expects;
            # its own proof.md replaces the edit and is bound to its receipt.
            self.assertEqual((code, len(calls), _errors(events)), (0, 6, []))
            proof = (workspace / "proof.md").read_bytes()
            self.assertNotIn(b"Reader's note", proof)
            self.assertEqual(_receipt(workspace)["proof_sha256"], hashlib.sha256(proof).hexdigest())
            self.assertEqual(_attestation(workspace), "harness-gate-receipt")

    def test_every_recorded_generation_stays_owned_after_an_edit(self) -> None:
        cases = {
            name: lambda workspace, files=case["files"]: _write_files(workspace, files)
            for name, case in _load_fixture()["cases"].items()
        }
        cases["legacy single-critic adapter"] = _write_legacy_generation
        for name, write in cases.items():
            with self.subTest(generation=name), tempfile.TemporaryDirectory() as raw:
                workspace = Path(raw)
                write(workspace)
                owned = runner._prior_generated_fact_ids(workspace)
                self.assertTrue(owned)
                proof = workspace / "proof.md"
                proof.write_text(proof.read_text(encoding="utf-8") + NOTE, encoding="utf-8")
                self.assertEqual(runner._prior_generated_fact_ids(workspace), owned)

    def test_a_tampered_generation_is_still_rejected_after_an_edit(self) -> None:
        def rewrite_receipt(workspace: Path, change, *, rebind: bool) -> None:
            receipt = _receipt(workspace)
            change(receipt)
            if rebind:
                receipt["binding_sha256"] = cgm_schema.receipt_binding_digest(receipt)
            (workspace / RECEIPT).write_text(json.dumps(receipt), encoding="utf-8")

        def lemma_id(workspace: Path) -> str:
            receipt = _receipt(workspace)
            return next(
                item["fact_id"]
                for item in receipt["facts"]
                if item["fact_id"] != receipt["target_fact_id"]
            )

        project = Path("math_harness/project")
        tamperings = {
            "a fact's bytes": (
                lambda ws: (ws / FACTS / f"{lemma_id(ws)}.md").write_bytes(
                    (ws / FACTS / f"{lemma_id(ws)}.md").read_bytes() + b"\nappended\n"
                ),
                "existing fact does not match its ownership receipt",
            ),
            "a receipt field, binding left as it was": (
                lambda ws: rewrite_receipt(ws, lambda r: r.update(model="another-model"), rebind=False),
                "existing facts are not owned by this adapter generation",
            ),
            "a forged fact hash, binding recomputed": (
                lambda ws: rewrite_receipt(
                    ws, lambda r: r["facts"][0].update(sha256="0" * 64), rebind=True
                ),
                "existing fact does not match its ownership receipt",
            ),
            "the target": (
                lambda ws: (ws / project / "TARGET.md").write_text(f"{lemma_id(ws)}\n"),
                "existing target does not match its ownership receipt",
            ),
            "the problem": (
                lambda ws: (ws / project / "PROBLEM.md").write_text("# Problem\n\nProve that 1 = 2.\n"),
                "existing problem does not match its ownership receipt",
            ),
            "an unlisted fact": (
                lambda ws: (ws / FACTS / "0123456789abcdef.md").write_text("---\nfact_id: x\n---\n"),
                "existing facts are not owned by this adapter generation",
            ),
            "a planted file": (
                lambda ws: (ws / FACTS / "notes.txt").write_text("planted"),
                "fact directory contains an unexpected artifact",
            ),
            "the receipt removed": (
                lambda ws: (ws / RECEIPT).unlink(),
                "existing facts have no adapter ownership receipt",
            ),
        }
        for name, (tamper, message) in tamperings.items():
            with self.subTest(tampering=name), tempfile.TemporaryDirectory() as raw:
                workspace = Path(raw)
                _published(workspace)
                edited = (workspace / "proof.md").read_text(encoding="utf-8") + NOTE
                _sandbox_save(workspace, edited)
                tamper(workspace)
                before = _fact_bytes(workspace)
                receipt = (workspace / RECEIPT).read_bytes() if (workspace / RECEIPT).exists() else None
                code, calls, events = _api_run(workspace, _renewed(), task=None)
                self.assertEqual((code, calls, _errors(events)), (1, [], [message]))
                # Nothing was retired or published.
                self.assertEqual(_fact_bytes(workspace), before)
                self.assertEqual(
                    (workspace / RECEIPT).read_bytes() if (workspace / RECEIPT).exists() else None,
                    receipt,
                )
                self.assertEqual((workspace / "proof.md").read_text(encoding="utf-8"), edited)

    def test_a_generation_changed_during_a_pass_still_fails_its_publication(self) -> None:
        def rewrite_a_fact(workspace: Path) -> None:
            fact = sorted((workspace / FACTS).iterdir())[0]
            fact.write_bytes(fact.read_bytes() + b"\nappended\n")

        def remove_the_generation(workspace: Path) -> None:
            for path in [workspace / RECEIPT, workspace / "math_harness/project/TARGET.md"]:
                path.unlink()
            for path in (workspace / FACTS).iterdir():
                path.unlink()

        cases = {
            "a fact rewritten": (rewrite_a_fact, "existing fact does not match its ownership receipt"),
            "the generation removed": (
                remove_the_generation, "existing fact generation changed during the run"
            ),
        }
        for name, (change, message) in cases.items():
            with self.subTest(change=name), tempfile.TemporaryDirectory() as raw:
                workspace = Path(raw)
                _published(workspace)
                edited = (workspace / "proof.md").read_text(encoding="utf-8") + NOTE
                _sandbox_save(workspace, edited)
                state: dict[str, object] = {}
                real_run_cgm = runner._run_cgm

                def run_then_change(*args, **kwargs):
                    result = real_run_cgm(*args, **kwargs)
                    change(workspace)
                    state["facts"] = _fact_bytes(workspace)
                    state["receipt"] = (
                        (workspace / RECEIPT).read_bytes() if (workspace / RECEIPT).exists() else None
                    )
                    return result

                with patch.object(runner, "_run_cgm", side_effect=run_then_change):
                    code, calls, events = _api_run(workspace, _renewed(), task=None)
                # The publication re-check still guards the generation after an
                # edit: nothing is retired or written, and the edit is kept.
                self.assertEqual((code, len(calls), _errors(events)), (1, 6, [message]))
                self.assertEqual(_fact_bytes(workspace), state["facts"])
                self.assertEqual(
                    (workspace / RECEIPT).read_bytes() if (workspace / RECEIPT).exists() else None,
                    state["receipt"],
                )
                self.assertEqual((workspace / "proof.md").read_text(encoding="utf-8"), edited)

    def test_a_proof_replaced_by_a_link_or_directory_during_a_pass_fails_before_any_write(self) -> None:
        for follow_up, kind in ((True, "link"), (True, "directory"), (False, "directory")):
            with (
                self.subTest(follow_up=follow_up, target=kind),
                tempfile.TemporaryDirectory() as raw,
                tempfile.TemporaryDirectory() as outside_raw,
            ):
                workspace = Path(raw)
                outside = Path(outside_raw) / "outside.md"
                outside.write_text("outside the workspace\n", encoding="utf-8")
                if follow_up:
                    _published(workspace)
                    _sandbox_save(workspace, (workspace / "proof.md").read_text(encoding="utf-8") + NOTE)
                    replies = _renewed()
                else:
                    (workspace / "problem.txt").write_text(PROBLEM + "\n", encoding="utf-8")
                    replies = _accepted_outputs()
                project = workspace / "math_harness" / "project"
                before = {
                    path.relative_to(workspace): path.read_bytes()
                    for path in sorted(project.rglob("*")) if path.is_file()
                } if project.exists() else {}
                proof = workspace / "proof.md"
                real_run_cgm = runner._run_cgm

                def run_then_replace(*args, **kwargs):
                    result = real_run_cgm(*args, **kwargs)
                    proof.unlink(missing_ok=True)
                    if kind == "link":
                        proof.symlink_to(outside)
                    else:
                        proof.mkdir()
                    return result

                with patch.object(runner, "_run_cgm", side_effect=run_then_replace):
                    code, calls, events = _api_run(workspace, replies, task=None)
                self.assertEqual(
                    (code, len(calls), _errors(events)), (1, 6, ["unsafe artifact target: proof.md"])
                )
                # Refused before the previous generation was retired and before
                # any file of the new one was written; the link was not followed.
                after = {
                    path.relative_to(workspace): path.read_bytes()
                    for path in sorted(project.rglob("*")) if path.is_file()
                }
                self.assertEqual(after, before)
                self.assertEqual(outside.read_text(encoding="utf-8"), "outside the workspace\n")
                # Once proof.md is a file again, the next pass publishes.
                if proof.is_symlink():
                    proof.unlink()
                else:
                    proof.rmdir()
                proof.write_text("# Restored by hand\n", encoding="utf-8")
                code, calls, events = _api_run(workspace, _renewed(), task=None)
                self.assertEqual((code, len(calls), _errors(events)), (0, 6, []))
                self.assertEqual(_attestation(workspace), "harness-gate-receipt")

    def test_a_pass_that_fails_before_publication_keeps_the_edit_for_the_next(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            _published(workspace)
            edited = (workspace / "proof.md").read_text(encoding="utf-8") + NOTE
            _sandbox_save(workspace, edited)
            facts, receipt = _fact_bytes(workspace), (workspace / RECEIPT).read_bytes()
            code, calls, _events = _api_run(
                workspace, [runner.APIBackendError("provider unavailable")], task=None
            )
            self.assertEqual((code, len(calls)), (1, 1))
            self.assertEqual((workspace / "proof.md").read_text(encoding="utf-8"), edited)
            self.assertEqual((_fact_bytes(workspace), (workspace / RECEIPT).read_bytes()), (facts, receipt))
            self.assertEqual(_attestation(workspace), "unattested")
            # The failure is not sticky: the next pass retires and publishes.
            code, calls, events = _api_run(workspace, _renewed(), task=None)
            self.assertEqual((code, len(calls), _errors(events)), (0, 6, []))
            self.assertTrue(set(facts).isdisjoint(_fact_bytes(workspace)))
            self.assertEqual(_attestation(workspace), "harness-gate-receipt")

    def test_a_receipt_rebound_to_the_edited_proof_still_attests_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            _published(workspace)
            edited = (workspace / "proof.md").read_text(encoding="utf-8") + NOTE
            _sandbox_save(workspace, edited)
            receipt = _receipt(workspace)
            receipt["proof_sha256"] = hashlib.sha256(edited.encode("utf-8")).hexdigest()
            receipt["binding_sha256"] = cgm_schema.receipt_binding_digest(receipt)
            (workspace / RECEIPT).write_text(json.dumps(receipt), encoding="utf-8")
            # Attestation recomputes proof.md from the facts: they never vouch
            # for a proof they did not produce, whatever the receipt claims.
            facts = engine_graph.build(workspace, {"engine": "math_harness"})
            self.assertEqual(facts["provenance"]["attestation"], "unattested")
            self.assertFalse(any(node["meta"]["verifier_accepted"] for node in facts["graph"]["nodes"]))
            dag = engine_graph.load_fact_dag(workspace)
            assert dag is not None
            self.assertFalse(any(item["attested"] for item in dag))


@contextmanager
def _console(workspace: Path, record: dict):
    """The console on a loopback port, signed in as the run's owner."""
    with (
        patch.object(console_server, "BASE_PATH", "/test"),
        patch.object(console_server.Handler, "_current_user", lambda _self: {"id": 1, "name": "Owner"}),
        patch.object(jobs, "run_owner", lambda _run_id: 1),
        patch.object(console_server, "_workspace_for_run", lambda _run_id: workspace),
        patch.object(console_server, "_run_record_for", lambda _run_id: dict(record)),
    ):
        server = ThreadingHTTPServer(("127.0.0.1", 0), console_server.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def post(path: str, body: dict) -> tuple[int, dict]:
            connection = http.client.HTTPConnection(*server.server_address, timeout=10)
            try:
                connection.request(
                    "POST", "/test" + path, body=json.dumps(body),
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        try:
            yield post
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class SandboxEditViewsTests(unittest.TestCase):
    def test_the_sandbox_save_unbinds_the_facts_and_marks_every_derived_view_stale(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(run_sharing, "RUNS_DIR", root / "runs"))
        self.enterContext(patch.object(run_sharing, "CACHE_DIR", root / "cache"))
        run_id = "math_harness_sandbox_edit"
        workspace = root / "runs" / "workspaces" / run_id
        workspace.mkdir(parents=True)
        self.enterContext(patch.object(console_server, "_workspace_for_run", lambda _run_id: workspace))
        record = {"run_id": run_id, "engine": "math_harness", "status": "finished", "owner_id": 1}
        # An accepted generation with an accepted write-up, and the derived
        # views of it, as a finished automatic pass leaves them.
        _writeup_pass(workspace, [*ACCEPTED, WRITEUP, OK])
        outline = proof_graph.load_or_build_informal(workspace)
        self.assertEqual((outline["model"], outline["attested"]), (proof_graph.FACT_GRAPH_MODEL, True))
        auto_pipeline._write_state(workspace, {
            "status": "done",
            "formalize": True,
            "derived_signature": auto_pipeline._source_signature(workspace, record),
            "stages": {name: {"status": "done", "outcome": "success"} for name in DERIVED_STAGES},
        })
        self.assertTrue(auto_pipeline._derived_current_source(
            auto_pipeline.load_state(workspace), workspace, record
        ))
        owned = runner._prior_generated_fact_ids(workspace)
        facts_before = engine_graph.load_or_build(workspace, record, view="facts")
        self.assertEqual(facts_before["provenance"]["attestation"], "harness-gate-receipt")
        preview = console_server._sandbox_preview(run_id, record)
        self.assertIs(preview["writeup"]["fresh"], True)

        edited = (workspace / "proof.md").read_text(encoding="utf-8") + NOTE
        with _console(workspace, record) as post:
            status, saved = post(
                f"/api/workspace/{run_id}/file",
                {"path": "proof.md", "content": edited, "expected_revision": preview["revision"]},
            )
        self.assertEqual((status, saved.get("ok")), (200, True), saved)
        self.assertEqual((workspace / "proof.md").read_text(encoding="utf-8"), edited)

        # Every derived view is stale, so the next pass derives them again.
        state = auto_pipeline.load_state(workspace)
        self.assertEqual(state["status"], "stale")
        self.assertNotIn("derived_signature", state)
        for name in DERIVED_STAGES:
            self.assertEqual(
                (state["stages"][name]["status"], state["stages"][name]["detail"]),
                ("stale", "proof.md was edited in Sandbox"),
            )
        self.assertFalse(auto_pipeline._derived_current_source(state, workspace, record))
        # Facts and the Outline keep the facts, never as verifier-accepted.
        facts = engine_graph.load_or_build(workspace, record, view="facts")
        self.assertEqual(
            (facts["provenance"]["attestation"], facts["provenance"]["receipt_status"]),
            ("unattested", "invalid"),
        )
        self.assertEqual(len(facts["graph"]["nodes"]), len(facts_before["graph"]["nodes"]))
        self.assertFalse(any(node["meta"]["verifier_accepted"] for node in facts["graph"]["nodes"]))
        outline = proof_graph.load_or_build_informal(workspace)
        self.assertEqual(
            (outline["attested"], outline["basis"]),
            (False, proof_graph.FACT_GRAPH_UNATTESTED_BASIS),
        )
        # The write-up is kept, as stale; a share shows the same.
        writeup = console_server._sandbox_preview(run_id, record)["writeup"]
        self.assertEqual((writeup["status"], writeup["fresh"]), ("accepted", False))
        shared = run_sharing.snapshot(run_id, record)
        self.assertEqual(shared["proof"], edited)
        self.assertIs(shared["writeup_fresh"], False)
        self.assertEqual(shared["graphs"]["facts"]["provenance"]["attestation"], "unattested")
        # The runner still owns that generation, so the next pass retires it.
        self.assertEqual(runner._prior_generated_fact_ids(workspace), owned)
        _writeup_pass(workspace, [*ACCEPTED, WRITEUP, OK])
        self.assertEqual(_attestation(workspace), "harness-gate-receipt")
        self.assertNotIn("Reader's note", (workspace / "proof.md").read_text(encoding="utf-8"))
        self.assertIs(console_server._sandbox_preview(run_id, record)["writeup"]["fresh"], True)


class SandboxEditFollowUpTests(unittest.TestCase):
    def test_a_kimi_follow_up_is_shown_the_edited_proof_and_publishes(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(jobs, "RUNS_DIR", root / "runs"))
        self.enterContext(patch.object(jobs, "CACHE_DIR", root / "cache"))
        run_id, job_id = "kimi_sandbox_edit", "job-kimi-sandbox-edit"
        workspace = root / "runs" / "workspaces" / run_id
        workspace.mkdir(parents=True)
        (workspace / "problem.txt").write_text(PROBLEM + "\n", encoding="utf-8")
        _kimi_pass(workspace, _accepted_outputs(), task=None)
        old = _fact_bytes(workspace)
        jobs._write_run({
            "run_id": run_id, "engine": "kimi", "problem_id": "case-1", "status": "finished",
            "problem_text": PROBLEM, "problems": [{"text": PROBLEM, "source": "initial"}],
            "agents": [{"trace_id": f"{run_id}::first", "output": "first session", "status": "finished"}],
            "edges": [],
        })
        jobs._JOBS[job_id] = {"job_id": job_id, "run_id": run_id, "status": "running", "engine": "kimi"}
        self.addCleanup(jobs._JOBS.pop, job_id, None)
        self.addCleanup(jobs._STOP_EVENTS.pop, run_id, None)
        jobs._stop_event(run_id).clear()
        edited = (workspace / "proof.md").read_text(encoding="utf-8") + NOTE
        _sandbox_save(workspace, edited)
        self.assertEqual(jobs._continuation_proof_excerpt(workspace), ("proof.md", edited[:8000]))
        seen: dict = {}

        def kimi_engine(**kwargs) -> dict:
            # As _run_cli_engine and kimi_runner: the prompt is the task.
            seen["prompt"] = kwargs["problem_text"]
            task = proof_prompt(kwargs["problem_text"], workspace=workspace, response_only=True)
            seen["calls"] = _kimi_pass(workspace, _renewed(), task=task)
            return {
                "run_id": run_id, "engine": "kimi", "status": "finished",
                "agents": [{"trace_id": f"{run_id}::turn-1", "output": "Math Harness completed", "status": "finished"}],
            }

        with (
            patch.object(jobs, "_run_cli_engine", side_effect=kimi_engine),
            # problem.txt stays the canonical problem the stub's THEOREM restates.
            patch.object(jobs, "_write_session_problems"),
            patch.object(jobs, "_launch_pending_feedback"),
            patch.object(auto_pipeline, "start") as derived,
        ):
            jobs._execute_continue(
                job_id=job_id, run_id=run_id, engine="kimi",
                message="Say where the case m + n = 0 is used.", problem_text=PROBLEM,
                model=None, max_iterations=2, workspace=str(workspace),
            )
        self.assertEqual(jobs._JOBS[job_id]["status"], "finished")
        self.assertEqual(jobs._load_run_record(run_id)["status"], "finished")
        # A CGM follow-up task carries the previous write-up to build on: here
        # the owner's edited proof.md (jobs._cgm_follow_up_task).
        self.assertIn(
            "THE PREVIOUS TURN'S WRITE-UP (proof.md, at most 8,000 characters): "
            "build on it — reuse what is correct and address the feedback.\n"
            f"{edited[:8000]}",
            seen["prompt"],
        )
        # The strategist saw the edited proof as task context.
        self.assertIn(NOTE.strip(), seen["calls"][0]["user"])
        self.assertTrue(set(old).isdisjoint(_fact_bytes(workspace)))
        receipt = _receipt(workspace)
        proof = (workspace / "proof.md").read_bytes()
        self.assertEqual(receipt["proof_sha256"], hashlib.sha256(proof).hexdigest())
        self.assertEqual(_attestation(workspace), "harness-gate-receipt")
        self.assertTrue(derived.called)


if __name__ == "__main__":
    unittest.main()
