"""The written-proof ("write-up") round after an accepted Ansatz CGM run."""
from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_monitor import engine_graph, jobs, run_sharing
from agent_monitor.cgm import parsing as cgm_parsing
from agent_monitor.cgm import prompts as cgm_prompts
from agent_monitor.cgm import schema as cgm_schema
from agent_monitor.cgm import writeup as cgm_writeup
from agent_monitor.runners import math_harness_runner as runner

from tests.test_cgm_max import _Providers, _run_max
from tests.test_cgm_pipeline import CURATOR, CURATOR_ANY, OK, PROBLEM, STRATEGY, WORKER
from tests.test_math_harness_runner import (
    PROBLEM as EVEN_PROBLEM,
    _accepted_outputs,
    _api_run,
    _curator,
    _ok,
    _strategist,
    _worker,
)

REPO = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "cgm_phase1_artifacts.json"
RECEIPT = "math_harness/project/engine_receipt.json"
SIDECAR = "math_harness/project/writeup.json"

# The accepted run of tests/test_cgm_pipeline.py: L1, L2 (uses L1), THEOREM
# (uses L2); 7 CGM calls.
ACCEPTED = [STRATEGY, WORKER, WORKER.replace("Pairing", "Grouping"), OK, OK, OK, CURATOR]

BODY = (
    "**Theorem.** For every natural number $n$, $1 + 3 + \\dots + (2n-1) = n^2$.\n\n"
    "**Proof.** The sum telescopes. For every integer $k \\ge 1$,\n"
    "$$k^2 - (k-1)^2 = 2k - 1,$$\n"
    "so for $n \\ge 1$\n"
    "$$\\sum_{k=1}^{n} (2k-1) = \\sum_{k=1}^{n} \\bigl(k^2 - (k-1)^2\\bigr) = n^2 - 0^2 = n^2.$$\n"
    "For $n = 0$ both sides are $0$. $\\blacksquare$"
)
WRITEUP = f"=== WRITEUP ===\n{BODY}\n=== END ==="
REVISED_BODY = BODY.replace("The sum telescopes.", "We treat $n = 0$ first; otherwise the sum telescopes.")
REVISED = f"=== WRITEUP ===\n{REVISED_BODY}\n=== END ==="


def _wrong(issue: str = "The case $n = 0$ is *not* justified <b>here</b>.") -> str:
    return json.dumps(
        {
            "verification_report": {
                "summary": "One gap.",
                "critical_errors": [],
                "gaps": [{"location": "Proof, last line", "issue": issue}],
            },
            "verdict": "wrong",
            "repair_hints": "Justify the case n = 0 explicitly.",
        }
    )


def _pipeline(
    workspace: Path,
    replies: list[object],
    *,
    env: dict[str, str] | None = None,
    on_call=None,
) -> tuple[dict, list[dict], list[dict]]:
    """run_pipeline on a scripted Kimi route, one call at a time, write-up on."""
    queue = list(replies)
    calls: list[dict] = []
    events: list[dict] = []

    def fake_api(system: str, user: str, *, model: str, timeout: int = 240, **kwargs):
        calls.append({"system": system, "user": user, "effort": kwargs.get("effort")})
        if on_call is not None:
            on_call(system, user)
        if not queue:
            raise AssertionError("unexpected extra API call")
        value = queue.pop(0)
        if isinstance(value, BaseException):
            raise value
        return SimpleNamespace(
            text=value, usage={"input_tokens": 3, "output_tokens": 4}, model=model, provider="kimi"
        )

    run_env = {
        "AGENT_MONITOR_PROBLEM_ID": "odd-sum",
        "AGENT_MONITOR_CGM_CONCURRENCY": "1",
        "AGENT_MONITOR_CGM_WRITEUP": "1",
        **(env or {}),
    }
    with (
        patch.dict(os.environ, run_env, clear=True),
        patch.object(runner, "api_chat", side_effect=fake_api),
    ):
        result = runner.run_pipeline(
            workspace, PROBLEM, runner.Route("api", "kimi-k3", "api_chat"), emit=events.append
        )
    return result, calls, events


def _sidecar(workspace: Path) -> dict:
    return json.loads((workspace / SIDECAR).read_text(encoding="utf-8"))


def _receipt(workspace: Path) -> dict:
    return json.loads((workspace / RECEIPT).read_text(encoding="utf-8"))


def _attestation(workspace: Path) -> str:
    return engine_graph.build(workspace, {"engine": "math_harness"})["provenance"]["attestation"]


def _usage_stages(events: list[dict]) -> list[str]:
    return [event["stage"] for event in events if event.get("type") == "turn.completed"]


class WriteupPromptTests(unittest.TestCase):
    def test_system_prompt_is_bounded_tool_free_and_outside_the_cgm_pack(self) -> None:
        text = cgm_writeup.WRITEUP_SYSTEM
        self.assertLessEqual(len(text.encode("utf-8")), runner.MAX_SYSTEM_PROMPT_BYTES)
        self.assertGreater(len(text), 1_000)
        forbidden = (
            "gm_add", "gm_search", "gm_get", "fact_submit", "fact_search",
            "fact_neighbors", "graph_link", "graph_frontier", "graph_annotate",
            "search_arxiv", "TASK.md", "local_memory", "download", "mcp",
            "web search", "websearch", "webfetch", "curl", "wget", "$verify",
            "$direct", "$construct", "fact_graph/", "results/", "problem.md",
        )
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token.lower(), text.lower())
        for rule in ("**Theorem.**", "**Conventions.**", "**Claim 1.**", "$\\blacksquare$",
                     "=== WRITEUP ===", "=== END ===", "Never mention the labels"):
            self.assertIn(rule, text)
        # The CGM prompt pack, and so its pinned hash, is unchanged.
        self.assertNotIn(text, cgm_prompts.all_system_prompts().values())
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        recorded = {
            json.loads(case["files"][RECEIPT])["prompt_pack_sha256"]
            for case in fixture["cases"].values()
        }
        self.assertEqual(recorded, {cgm_prompts.prompt_pack_sha256()})

    def test_prompt_hash_is_versioned_and_sensitive(self) -> None:
        first = cgm_writeup.writeup_prompt_sha256()
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertEqual(cgm_writeup.WRITEUP_PROMPT_VERSION, "cgm-writeup@1")
        with patch.object(cgm_writeup, "WRITEUP_SYSTEM", cgm_writeup.WRITEUP_SYSTEM + " "):
            self.assertNotEqual(first, cgm_writeup.writeup_prompt_sha256())

    def test_requests_fence_the_problem_the_blocks_and_the_check(self) -> None:
        blocks = [
            cgm_writeup.VerifiedBlock("L1", (), "S1.", "P1."),
            cgm_writeup.VerifiedBlock("THEOREM", ("L1",), "The problem.", "By L1."),
        ]
        prompt = cgm_writeup.writeup_prompt("The problem.", blocks)
        self.assertIn("<UNTRUSTED_PROBLEM>\nThe problem.\n</UNTRUSTED_PROBLEM>", prompt)
        self.assertIn(
            "<VERIFIED_FACTS>\n[L1] (uses: none)\nStatement:\nS1.\nProof:\nP1.\n\n"
            "[THEOREM] (uses: L1)\nStatement:\nThe problem.\nProof:\nBy L1.\n</VERIFIED_FACTS>",
            prompt,
        )
        report = cgm_parsing.parse_verifier(_wrong())
        fix = cgm_writeup.writefix_prompt("The problem.", blocks, "Old text.", report)
        self.assertTrue(fix.startswith(prompt))
        self.assertIn("<PREVIOUS_WRITEUP>\nOld text.\n</PREVIOUS_WRITEUP>", fix)
        self.assertIn(
            "<CHECK_REPORT>\ncritical errors: none\n"
            "gaps: Proof, last line: The case $n = 0$ is *not* justified <b>here</b>.\n"
            "repair hints: Justify the case n = 0 explicitly.\n</CHECK_REPORT>",
            fix,
        )
        self.assertTrue(fix.endswith("under the same rules and output contract."))


class ParseWriteupTests(unittest.TestCase):
    def test_the_body_between_the_markers(self) -> None:
        self.assertEqual(cgm_writeup.parse_writeup(WRITEUP, ["L1", "L2", "THEOREM"]), BODY)
        wrapped = f"Here is the proof.\n```markdown\n{WRITEUP}\n```\n"
        self.assertEqual(cgm_writeup.parse_writeup(wrapped, ["L1"]), BODY)
        decorated = WRITEUP.replace("=== WRITEUP ===", "**=== WRITEUP ===**")
        self.assertEqual(cgm_writeup.parse_writeup(decorated, ["L1"]), BODY)

    def test_contract_violations_raise(self) -> None:
        cases = {
            "cut off": f"=== WRITEUP ===\n{BODY}",
            "no markers": BODY,
            "two writeups": f"{WRITEUP}\n{WRITEUP}",
            "empty": "=== WRITEUP ===\n\n=== END ===",
            "heading": WRITEUP.replace("**Proof.**", "## Proof\n\n"),
            "code fence": WRITEUP.replace("For $n = 0$", "```\ncode\n```\nFor $n = 0$"),
            "html": WRITEUP.replace("The sum telescopes.", "The sum <b>telescopes</b>."),
            "html comment": WRITEUP.replace("The sum telescopes.", "<!-- note --> The sum telescopes."),
            "fact id": WRITEUP.replace("The sum telescopes.", "By fact 0123456789abcdef, it telescopes."),
            "label": WRITEUP.replace("The sum telescopes.", "By L1, the sum telescopes."),
            "label with suffix": WRITEUP.replace("The sum telescopes.", "By L1's identity it telescopes."),
            "theorem label": WRITEUP.replace("The sum telescopes.", "As THEOREM states, it telescopes."),
            "tool markup": WRITEUP.replace("The sum telescopes.", '<invoke name="x">'),
            "too large": f"=== WRITEUP ===\n{'x' * (cgm_writeup.MAX_BODY_BYTES + 1)}\n=== END ===",
        }
        for name, text in cases.items():
            with self.subTest(name), self.assertRaises(cgm_parsing.ParseError) as raised:
                cgm_writeup.parse_writeup(text, ["L1", "L2", "THEOREM"])
            if name == "cut off":
                self.assertIn("=== END ===", str(raised.exception))
            if name == "label":
                self.assertIn("label L1", str(raised.exception))

    def test_labels_inside_mathematics_and_long_numbers_are_fine(self) -> None:
        allowed = BODY.replace(
            "The sum telescopes.",
            "In $L^1$ and $L_1$ terms, \\(L1\\) and \\[L1\\] are notation, L10 is another name, "
            "and $2^{64} = 18446744073709551616$ has twenty digits; the sum telescopes.",
        )
        text = f"=== WRITEUP ===\n{allowed}\n=== END ==="
        self.assertEqual(cgm_writeup.parse_writeup(text, ["L1", "THEOREM"]), allowed)

    def test_forbidden_labels(self) -> None:
        labels = ["L1", "L2", "A", "AM-GM", "THEOREM"]
        self.assertEqual(cgm_writeup.forbidden_labels(labels, "Prove it."), ["L1", "L2", "THEOREM"])
        # A label the problem itself uses may appear (restating the problem).
        self.assertEqual(
            cgm_writeup.forbidden_labels(labels, "Show that the L1 norm of $L2$ is finite."),
            ["L2", "THEOREM"],
        )


class WriteupFileTests(unittest.TestCase):
    def test_headers_by_status_and_problems_as_plain_text(self) -> None:
        report = cgm_parsing.parse_verifier(_wrong())
        accepted = cgm_writeup.render_writeup("accepted", BODY, None)
        self.assertEqual(
            accepted, f"# Written proof\n\n{cgm_writeup.STATUS_NOTES['accepted']}\n\n{BODY}\n"
        )
        self.assertIn("not a Lean or other formal certificate", accepted)
        unchecked = cgm_writeup.render_writeup("unchecked", BODY, None)
        self.assertIn("This text has not been checked; the verified facts remain the proof.", unchecked)
        rejected = cgm_writeup.render_writeup("rejected", BODY, report)
        self.assertIn("this write-up is NOT accepted; the verified facts remain the proof.", rejected)
        self.assertTrue(
            rejected.endswith(
                "\n**Problems found by the check.**\n\n"
                "- Proof, last line: The case \\$n = 0\\$ is \\*not\\* justified \\<b\\>here\\</b\\>.\n"
            )
        )
        bare = cgm_parsing.parse_verifier(
            json.dumps(
                {
                    "verification_report": {"summary": "Wrong\nsummary.", "critical_errors": [], "gaps": []},
                    "verdict": "wrong",
                    "repair_hints": "x",
                }
            )
        )
        self.assertTrue(cgm_writeup.render_writeup("rejected", BODY, bare).endswith("- Wrong summary.\n"))

    def test_describe_binds_the_sidecar_to_these_bytes_and_this_generation(self) -> None:
        payload = cgm_writeup.render_writeup("accepted", BODY, None).encode()
        proof, receipt = b"# Proof\n", json.dumps({"binding_sha256": "b" * 64}).encode()
        sidecar = {
            "schema": cgm_writeup.SIDECAR_SCHEMA,
            "status": "accepted",
            "basis": {
                "proof_sha256": hashlib.sha256(proof).hexdigest(),
                "receipt_binding_sha256": "b" * 64,
            },
            "writeup_sha256": hashlib.sha256(payload).hexdigest(),
            "routes": {"writer": {"provider": "api", "model": "kimi-k3", "transport": "api_chat"}},
            "observed_model": None,
            "check": {
                "verdict": "correct", "summary": "Sound.",
                "critical_errors": [], "gaps": [{"location": "x", "issue": "y"}],
            },
        }

        def describe(record: dict | None, **current: bytes | None) -> dict:
            raw = None if record is None else json.dumps(record).encode()
            return cgm_writeup.describe(payload, raw, **{"proof": proof, "receipt": receipt, **current})

        self.assertEqual(
            describe(sidecar),
            {
                "status": "accepted", "fresh": True, "model": "kimi-k3", "provider": "api",
                "check": {"verdict": "correct", "summary": "Sound.",
                          "issues": [{"kind": "gap", "location": "x", "issue": "y"}]},
            },
        )
        self.assertFalse(describe(sidecar, proof=b"# Proof\nedited\n")["fresh"])
        self.assertFalse(describe(sidecar, receipt=json.dumps({"binding_sha256": "c" * 64}).encode())["fresh"])
        self.assertFalse(describe(sidecar, receipt=None)["fresh"])
        for broken in (None, {**sidecar, "schema": "other"}, {**sidecar, "status": "proved"},
                       {**sidecar, "writeup_sha256": "0" * 64}):
            view = describe(broken)
            self.assertEqual((view["status"], view["fresh"], view["check"]), ("unchecked", False, None))
        edited = cgm_writeup.describe(b"edited", json.dumps(sidecar).encode(), proof=proof, receipt=receipt)
        self.assertEqual(edited["status"], "unchecked")


class WriteupRoundTests(unittest.TestCase):
    def test_accepted_writeup_is_published_beside_the_untouched_generation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            result, calls, events = _pipeline(workspace, [*ACCEPTED, WRITEUP, OK])
            self.assertEqual(len(calls), 9)
            payload = (workspace / "writeup.md").read_bytes()
            self.assertEqual(
                payload.decode(),
                f"# Written proof\n\n{cgm_writeup.STATUS_NOTES['accepted']}\n\n{BODY}\n",
            )
            self.assertEqual(
                result["writeup"],
                {
                    "status": "accepted",
                    "path": "writeup.md",
                    "sidecar": SIDECAR,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "checker_verdict": "correct",
                    "fixes": 0,
                    "calls": 2,
                    "model": "kimi-k3",
                    "observed_model": None,
                    "degraded": [],
                },
            )
            # The CGM generation is exactly what it is without the round.
            receipt = _receipt(workspace)
            self.assertEqual(result["calls"], 7)
            self.assertEqual(receipt["workflow"]["calls"]["total"], 7)
            self.assertEqual(
                [call["stage"] for call in receipt["calls"]],
                ["strategist", "worker-1", "worker-2", "verify-1", "verify-2", "verify-3", "curator"],
            )
            self.assertEqual(receipt["proof_rendering"], cgm_schema.PROOF_RENDERING_VERIFIED_V2)
            self.assertNotIn("writeup", json.dumps(receipt))
            self.assertEqual(result["written"][-1], RECEIPT)
            self.assertNotIn("writeup.md", result["written"])
            dag = engine_graph.load_fact_dag(workspace)
            self.assertEqual(
                (workspace / "proof.md").read_text(),
                engine_graph.render_fact_closure(
                    dag, result["target_fact_id"], labels=engine_graph.load_proof_labels(workspace)
                ),
            )
            self.assertEqual(result["proof_text"], (workspace / "proof.md").read_text())
            self.assertEqual(_attestation(workspace), "harness-gate-receipt")

            sidecar = _sidecar(workspace)
            self.assertEqual(
                set(sidecar),
                {
                    "schema", "status", "basis", "writeup_sha256", "bytes", "prompt_version",
                    "prompt_sha256", "routes", "calls", "check", "fixes", "degraded",
                    "observed_model", "certificate", "created_at",
                },
            )
            self.assertEqual(sidecar["schema"], "ansatze-cgm-writeup-v1")
            self.assertEqual(sidecar["status"], "accepted")
            self.assertEqual(
                sidecar["basis"],
                {
                    "receipt_binding_sha256": receipt["binding_sha256"],
                    "proof_sha256": hashlib.sha256((workspace / "proof.md").read_bytes()).hexdigest(),
                    "target_fact_id": result["target_fact_id"],
                    "proof_rendering": cgm_schema.PROOF_RENDERING_VERIFIED_V2,
                },
            )
            self.assertEqual(sidecar["writeup_sha256"], result["writeup"]["sha256"])
            self.assertEqual(sidecar["bytes"], len(payload))
            self.assertEqual(sidecar["prompt_version"], "cgm-writeup@1")
            self.assertEqual(sidecar["prompt_sha256"], cgm_writeup.writeup_prompt_sha256())
            route = {"provider": "api", "model": "kimi-k3", "transport": "api_chat"}
            self.assertEqual(sidecar["routes"], {"writer": route, "checker": route})
            self.assertEqual([call["stage"] for call in sidecar["calls"]], ["writeup", "writecheck"])
            self.assertEqual(
                set(sidecar["calls"][0]),
                {"stage", "provider", "model", "usage", "response_sha256", "effort"},
            )
            self.assertEqual(sidecar["check"]["verdict"], "correct")
            self.assertIs(sidecar["certificate"], False)
            self.assertRegex(sidecar["created_at"], r"Z$")
            for name in ("writeup.md", SIDECAR):
                self.assertEqual(stat.S_IMODE((workspace / name).stat().st_mode), 0o600)
            self.assertEqual(list(workspace.rglob("*receipt*.json")), [workspace / RECEIPT])

            # The writer saw the problem and the verified blocks in written
            # order: no fact id and no "Cited facts" line.
            writer, checker = calls[7], calls[8]
            self.assertEqual(writer["system"], cgm_writeup.WRITEUP_SYSTEM)
            self.assertIn(f"<UNTRUSTED_PROBLEM>\n{PROBLEM}\n</UNTRUSTED_PROBLEM>", writer["user"])
            positions = [writer["user"].index(f"[{label}] (uses: ") for label in ("L1", "L2", "THEOREM")]
            self.assertEqual(positions, sorted(positions))
            self.assertIn("[L2] (uses: L1)", writer["user"])
            self.assertNotIn("Cited facts", writer["user"])
            for fact_id in result["fact_ids"]:
                self.assertNotIn(fact_id, writer["user"])
            # The checker is the CGM verifier with no predecessors.
            self.assertEqual(checker["system"], cgm_prompts.VERIFIER_SYSTEM)
            self.assertEqual(checker["user"], runner._verifier_prompt("THEOREM", PROBLEM, BODY, []))
            # Usage is booked per call; no new pipeline stage.
            self.assertEqual(_usage_stages(events)[-3:], ["curator", "writeup", "writecheck"])
            self.assertEqual(result["stages"], list(cgm_schema.STAGE_ORDER))
            self.assertEqual(
                [event["stage"] for event in events if event.get("type") == "pipeline.stage"][-1],
                "finalize",
            )
            notes = [
                event["item"]["text"]
                for event in events
                if (event.get("item") or {}).get("type") == "reasoning"
            ]
            self.assertIn("Math Harness · Write-up · writeup.md · accepted", notes)
            self.assertIn("Write-up · writeup", notes)

    def test_a_rejected_writeup_is_revised_once_and_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            result, calls, _events = _pipeline(workspace, [*ACCEPTED, WRITEUP, _wrong(), REVISED, OK])
            self.assertEqual(result["writeup"]["status"], "accepted")
            self.assertEqual(result["writeup"]["fixes"], 1)
            self.assertEqual(result["writeup"]["calls"], 4)
            self.assertEqual(
                (workspace / "writeup.md").read_text(),
                f"# Written proof\n\n{cgm_writeup.STATUS_NOTES['accepted']}\n\n{REVISED_BODY}\n",
            )
            self.assertEqual(
                [call["stage"] for call in _sidecar(workspace)["calls"]],
                ["writeup", "writecheck", "writefix", "writecheck"],
            )
            fix = calls[9]
            self.assertEqual(fix["system"], cgm_writeup.WRITEUP_SYSTEM)
            self.assertIn(f"<PREVIOUS_WRITEUP>\n{BODY}\n</PREVIOUS_WRITEUP>", fix["user"])
            self.assertIn("gaps: Proof, last line: The case $n = 0$", fix["user"])
            self.assertEqual(calls[10]["user"], runner._verifier_prompt("THEOREM", PROBLEM, REVISED_BODY, []))

    def test_a_writeup_still_rejected_after_its_fixes_lists_the_problems(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            result, calls, _events = _pipeline(
                workspace, [*ACCEPTED, WRITEUP, _wrong(), REVISED, _wrong("Still unclear.")]
            )
            self.assertEqual(len(calls), 11)
            writeup = result["writeup"]
            self.assertEqual(
                (writeup["status"], writeup["checker_verdict"], writeup["fixes"]), ("rejected", "wrong", 1)
            )
            text = (workspace / "writeup.md").read_text()
            self.assertIn(cgm_writeup.STATUS_NOTES["rejected"], text)
            self.assertIn(REVISED_BODY, text)
            self.assertTrue(text.endswith("**Problems found by the check.**\n\n- Proof, last line: Still unclear.\n"))
            check = _sidecar(workspace)["check"]
            self.assertEqual(check["verdict"], "wrong")
            self.assertEqual(check["gaps"], [{"location": "Proof, last line", "issue": "Still unclear."}])
            self.assertEqual(_attestation(workspace), "harness-gate-receipt")

    def test_an_unreadable_check_leaves_the_writeup_unchecked(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            result, _calls, _events = _pipeline(
                workspace, [*ACCEPTED, WRITEUP, "Looks fine to me.", "Still fine."]
            )
            writeup = result["writeup"]
            self.assertEqual((writeup["status"], writeup["checker_verdict"]), ("unchecked", None))
            self.assertEqual(writeup["degraded"], ["writecheck-unparsable"])
            self.assertEqual(
                [call["stage"] for call in _sidecar(workspace)["calls"]],
                ["writeup", "writecheck", "writecheck-retry"],
            )
            self.assertIn(cgm_writeup.STATUS_NOTES["unchecked"], (workspace / "writeup.md").read_text())
            self.assertIsNone(_sidecar(workspace)["check"])

    def test_a_cut_off_reply_gets_the_format_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            truncated = WRITEUP.replace("\n=== END ===", "")
            result, calls, _events = _pipeline(workspace, [*ACCEPTED, truncated, WRITEUP, OK])
            self.assertEqual(result["writeup"]["status"], "accepted")
            self.assertEqual(
                [call["stage"] for call in _sidecar(workspace)["calls"]],
                ["writeup", "writeup-retry", "writecheck"],
            )
            self.assertIn("<FORMAT_REMINDER>", calls[8]["user"])
            self.assertIn("was it cut off?", calls[8]["user"])

    def test_leaked_labels_and_ids_are_sent_back_but_mathematics_is_not(self) -> None:
        for leak in ("By L1, the sum telescopes.", "By fact 0123456789abcdef the sum telescopes."):
            with self.subTest(leak=leak), tempfile.TemporaryDirectory() as raw:
                workspace = Path(raw)
                leaked = WRITEUP.replace("The sum telescopes.", leak)
                result, calls, _events = _pipeline(workspace, [*ACCEPTED, leaked, WRITEUP, OK])
                self.assertEqual(result["writeup"]["status"], "accepted")
                self.assertIn("<FORMAT_REMINDER>", calls[8]["user"])
                self.assertNotIn(leak, (workspace / "writeup.md").read_text())
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            notation = WRITEUP.replace("The sum telescopes.", "As in $L^1$, the sum telescopes.")
            result, calls, _events = _pipeline(workspace, [*ACCEPTED, notation, OK])
            self.assertEqual((result["writeup"]["status"], len(calls)), ("accepted", 9))

    def test_a_failed_writer_publishes_no_writeup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            result, calls, _events = _pipeline(workspace, [*ACCEPTED, "No markers.", "Still none."])
            self.assertEqual(len(calls), 9)
            self.assertEqual(
                result["writeup"],
                {
                    "status": "failed", "path": None, "sidecar": None, "sha256": None,
                    "checker_verdict": None, "fixes": 0, "calls": 2, "model": "kimi-k3",
                    "observed_model": None, "degraded": ["writeup-unparsable"],
                },
            )
            self.assertFalse((workspace / "writeup.md").exists())
            self.assertFalse((workspace / SIDECAR).exists())
            self.assertEqual(_attestation(workspace), "harness-gate-receipt")
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            outage = runner.APIBackendError("provider unavailable")
            result, _calls, _events = _pipeline(workspace, [*ACCEPTED, outage])
            self.assertEqual(result["writeup"]["status"], "failed")
            self.assertEqual(result["writeup"]["degraded"], ["writeup-provider-failure"])
            self.assertEqual(result["status"], "accepted")

    def test_the_round_is_off_unless_switched_on(self) -> None:
        for env in ({"AGENT_MONITOR_CGM_WRITEUP": ""}, {"AGENT_MONITOR_CGM_WRITEUP": "0"}):
            with self.subTest(env=env), tempfile.TemporaryDirectory() as raw:
                workspace = Path(raw)
                result, calls, _events = _pipeline(workspace, ACCEPTED, env=env)
                self.assertEqual(len(calls), 7)
                self.assertNotIn("writeup", result)
                self.assertFalse((workspace / "writeup.md").exists())

    def test_an_unattested_run_gets_no_writeup(self) -> None:
        wrong_theorem = [STRATEGY, WORKER, OK, OK, _wrong(), CURATOR_ANY]
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            result, calls, _events = _pipeline(
                workspace,
                wrong_theorem,
                env={"AGENT_MONITOR_CGM_MAX_WORKERS": "1", "AGENT_MONITOR_CGM_MAX_REPAIRS": "0"},
            )
            self.assertEqual(result["status"], "unattested")
            self.assertEqual(len(calls), 6)
            self.assertNotIn("writeup", result)
            self.assertNotIn(cgm_writeup.WRITEUP_SYSTEM, [call["system"] for call in calls])
            self.assertFalse((workspace / "writeup.md").exists())

    def test_a_stop_request_during_the_check_keeps_the_run_published(self) -> None:
        def stop_at_the_check(system: str, user: str) -> None:
            if system == cgm_prompts.VERIFIER_SYSTEM and "**Theorem.**" in user:
                runner._stop_active_runs()

        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            result, _calls, _events = _pipeline(
                workspace, [*ACCEPTED, WRITEUP, OK], on_call=stop_at_the_check
            )
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(result["writeup"]["status"], "unchecked")
            self.assertIn("writeup-stopped", result["writeup"]["degraded"])
            self.assertIn(cgm_writeup.STATUS_NOTES["unchecked"], (workspace / "writeup.md").read_text())
            self.assertEqual(_attestation(workspace), "harness-gate-receipt")

    def test_sigterm_during_the_writeup_exits_zero_with_the_run_published(self) -> None:
        driver = (
            "import json, sys, time\n"
            "from pathlib import Path\n"
            "from types import SimpleNamespace\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from agent_monitor.cgm import prompts, writeup\n"
            "from agent_monitor.runners import math_harness_runner as runner\n"
            "marker = Path(sys.argv[2])\n"
            "OK = json.dumps({'verification_report': {'summary': 'Sound.', 'critical_errors': [],"
            " 'gaps': []}, 'verdict': 'correct', 'repair_hints': ''})\n"
            "def fake_api(system, user, **kwargs):\n"
            "    if system == writeup.WRITEUP_SYSTEM:\n"
            "        marker.write_text('held')\n"
            "        time.sleep(60)\n"
            "    if system == prompts.STRATEGIST_SYSTEM:\n"
            "        text = json.dumps({'plans': [{'label': 'A', 'technique': 'plain',"
            " 'approach': 'Direct.', 'subgoals': [], 'risks': 'none'}]})\n"
            "    elif system == prompts.VERIFIER_SYSTEM:\n"
            "        text = OK\n"
            "    elif system == prompts.CURATOR_SYSTEM:\n"
            "        text = json.dumps({'annotations': []})\n"
            "    else:\n"
            "        text = ('=== THEOREM ===\\nUses: none\\nStatement:\\n(the problem)\\nProof:\\n'"
            " 'Both are even, so their sum 2a + 2b = 2(a + b) is even.\\n=== END ===\\n\\n'"
            " '=== STATUS ===\\nsolved\\n=== END ===')\n"
            "    return SimpleNamespace(text=text, usage={}, model='kimi-k3', provider='kimi')\n"
            "runner.api_chat = fake_api\n"
            "sys.exit(runner.main())\n"
        )
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw) / "ws"
            workspace.mkdir()
            (workspace / "problem.txt").write_text(EVEN_PROBLEM, encoding="utf-8")
            marker = Path(raw) / "held"
            env = {
                "PATH": os.environ.get("PATH", ""),
                "AGENT_MONITOR_ENV_PATH": os.devnull,
                "AGENT_MONITOR_DATA_DIR": str(Path(raw) / "data"),
                "AGENT_MONITOR_SELECTED_MODEL": "kimi-k3",
                "AGENT_MONITOR_CGM_CONCURRENCY": "1",
                "AGENT_MONITOR_CGM_MAX_WORKERS": "1",
                "AGENT_MONITOR_CGM_WRITEUP": "1",
            }
            process = subprocess.Popen(
                [sys.executable, "-c", driver, str(REPO), str(marker)],
                cwd=workspace,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 30
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(marker.exists(), "the write-up call never started")
                started = time.monotonic()
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=20)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
            self.assertEqual(process.returncode, 0, stderr[-2000:])
            self.assertLess(time.monotonic() - started, 10)
            items = [
                (json.loads(line).get("item") or {}) for line in stdout.splitlines() if line.strip()
            ]
            self.assertFalse(any(item.get("terminal") for item in items))
            final = items[-1]
            self.assertEqual(final["type"], "agent_message")
            self.assertIn("Written proof: not written", final["text"])
            self.assertEqual(_attestation(workspace), "harness-gate-receipt")
            self.assertFalse((workspace / "writeup.md").exists())

    def test_a_new_generation_retires_the_previous_writeup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            _pipeline(workspace, [*ACCEPTED, WRITEUP, OK])
            self.assertTrue((workspace / "writeup.md").exists())
            # A continuation without the round (or an unattested outcome)
            # never keeps the earlier write-up.
            result, _calls, _events = _pipeline(workspace, ACCEPTED, env={"AGENT_MONITOR_CGM_WRITEUP": "0"})
            self.assertEqual(result["status"], "accepted")
            self.assertFalse((workspace / "writeup.md").exists())
            self.assertFalse((workspace / SIDECAR).exists())
            _pipeline(workspace, [*ACCEPTED, WRITEUP, OK])
            self.assertTrue((workspace / "writeup.md").exists())
            unattested, _calls, _events = _pipeline(
                workspace,
                [STRATEGY, WORKER, OK, OK, _wrong(), CURATOR_ANY],
                env={"AGENT_MONITOR_CGM_MAX_WORKERS": "1", "AGENT_MONITOR_CGM_MAX_REPAIRS": "0"},
            )
            self.assertEqual(unattested["status"], "unattested")
            self.assertFalse((workspace / "writeup.md").exists())
            self.assertFalse((workspace / SIDECAR).exists())

    def test_guest_runs_get_four_calls_and_no_fix(self) -> None:
        guest = {"AGENT_MONITOR_GUEST": "1", "AGENT_MONITOR_MAX_OUTPUT_TOKENS": "8192"}
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            result, calls, _events = _pipeline(workspace, [*ACCEPTED, WRITEUP, _wrong()], env=guest)
            writeup = result["writeup"]
            self.assertEqual((writeup["status"], writeup["fixes"], writeup["calls"]), ("rejected", 0, 2))
            self.assertEqual(len(calls), 9)
            # Long-output writer turns use low effort under the 8192-token ceiling.
            self.assertEqual(calls[7]["effort"], "low")
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            replies = [*ACCEPTED, "No markers.", WRITEUP, "Fine.", "Fine again.", "never sent"]
            result, calls, _events = _pipeline(
                workspace,
                replies,
                env={**guest, "AGENT_MONITOR_CGM_WRITEUP_MAX_CALLS": "12", "AGENT_MONITOR_CGM_WRITEUP_MAX_FIXES": "2"},
            )
            self.assertEqual(result["writeup"]["calls"], 4)
            self.assertEqual(len(calls), 11)
        budgets, fixes = runner._writeup_limits(
            {**guest, "AGENT_MONITOR_CGM_WRITEUP_MAX_CALLS": "12", "AGENT_MONITOR_CGM_WRITEUP_MAX_SECONDS": "3600"}
        )
        self.assertEqual((budgets.max_calls, budgets.max_seconds, budgets.guest, fixes), (4, 1200, True, 0))
        budgets, fixes = runner._writeup_limits({})
        self.assertEqual((budgets.max_calls, budgets.max_seconds, budgets.concurrency, fixes), (6, 1800, 1, 1))
        budgets, fixes = runner._writeup_limits({"AGENT_MONITOR_CGM_ROUTE_MODE": "max"})
        self.assertEqual(budgets.max_seconds, 3600)
        budgets, fixes = runner._writeup_limits(
            {"AGENT_MONITOR_CGM_WRITEUP_MAX_CALLS": "99", "AGENT_MONITOR_CGM_WRITEUP_MAX_FIXES": "9"}
        )
        self.assertEqual((budgets.max_calls, fixes), (12, 2))

    def test_an_invalid_setting_skips_the_round_but_not_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            result, calls, _events = _pipeline(
                workspace, ACCEPTED, env={"AGENT_MONITOR_CGM_WRITEUP_MAX_CALLS": "many"}
            )
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(len(calls), 7)
            self.assertEqual(result["writeup"]["status"], "failed")
            self.assertEqual(result["writeup"]["degraded"], ["writeup-settings-invalid"])

    def test_symlinked_writeup_targets_fail_before_any_model_call(self) -> None:
        for target_name in ("writeup.md", SIDECAR):
            with (
                self.subTest(target=target_name),
                tempfile.TemporaryDirectory() as raw,
                tempfile.TemporaryDirectory() as outside_raw,
            ):
                workspace = Path(raw)
                sentinel = Path(outside_raw) / "sentinel"
                sentinel.write_text("unchanged", encoding="utf-8")
                target = workspace / target_name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(sentinel)
                code, calls, _events = _api_run(
                    workspace, _accepted_outputs(), env={"AGENT_MONITOR_CGM_WRITEUP": "1"}
                )
                self.assertEqual((code, calls), (1, []))
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")

    def test_interrupted_writeup_temps_are_cleaned_before_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            project = workspace / "math_harness" / "project"
            project.mkdir(parents=True)
            orphans = [
                workspace / ".writeup.md.tmp-0123456789abcdef01234567",
                project / ".writeup.json.tmp-0123456789abcdef01234567",
            ]
            for orphan in orphans:
                orphan.write_bytes(b"interrupted adapter write")
                orphan.chmod(0o600)
            runner._cleanup_adapter_temps(workspace)
            self.assertFalse(any(orphan.exists() for orphan in orphans))

    def test_writeup_stages_use_the_historical_timeouts_and_efforts(self) -> None:
        self.assertEqual(runner._claude_stage_timeout("writeup", {}), 1200.0)
        self.assertEqual(runner._claude_stage_timeout("writefix-retry", {}), 1200.0)
        self.assertEqual(runner._claude_stage_timeout("writecheck", {}), 600.0)
        self.assertEqual(
            runner._claude_stage_timeout(
                "writeup", {"AGENT_MONITOR_MATH_HARNESS_CLAUDE_CANDIDATE_TIMEOUT": "900"}
            ),
            900.0,
        )


class WriteupEntryPointTests(unittest.TestCase):
    def test_main_lists_the_writeup_and_keeps_the_closure_as_its_final_message(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            body = BODY.replace("natural number $n$, $1 + 3 + \\dots + (2n-1) = n^2$", "pair of even integers")
            replies = [*_accepted_outputs(), f"=== WRITEUP ===\n{body}\n=== END ===", _ok()]
            code, calls, events = _api_run(workspace, replies, env={"AGENT_MONITOR_CGM_WRITEUP": "1"})
            self.assertEqual(code, 0)
            self.assertEqual(len(calls), 8)
            items = [event["item"] for event in events if event.get("type") == "item.completed"]
            changes = next(item for item in items if item["type"] == "file_change")["changes"]
            self.assertEqual([change["path"] for change in changes][-3:], [RECEIPT, "writeup.md", SIDECAR])
            final = items[-1]
            self.assertEqual(final["type"], "agent_message")
            head, _blank, proof = final["text"].partition("\n\n")
            self.assertEqual(
                head.split("\n"),
                [
                    "Math Harness completed · accepted by the independent model verifier "
                    "(2 facts; not a Lean certificate)",
                    "Written proof: checked by an independent model verifier "
                    "(writeup.md; not a Lean certificate)",
                ],
            )
            self.assertEqual(proof, (workspace / "proof.md").read_text()[:6000])

    def test_main_exits_zero_when_the_writer_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            replies = [*_accepted_outputs(), "No markers.", "Still none."]
            code, _calls, events = _api_run(workspace, replies, env={"AGENT_MONITOR_CGM_WRITEUP": "1"})
            self.assertEqual(code, 0)
            self.assertFalse((workspace / "writeup.md").exists())
            items = [event["item"] for event in events if event.get("type") == "item.completed"]
            self.assertFalse(any(item.get("terminal") for item in items))
            self.assertIn("Written proof: not written", items[-1]["text"])
            changes = next(item for item in items if item["type"] == "file_change")["changes"]
            self.assertEqual(changes[-1]["path"], RECEIPT)

    def test_main_without_the_round_keeps_its_message(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            code, _calls, events = _api_run(Path(raw), _accepted_outputs())
            self.assertEqual(code, 0)
            final = [event["item"] for event in events if event.get("type") == "item.completed"][-1]
            self.assertNotIn("Written proof", final["text"])

    def test_kimi_runner_reports_the_writeup(self) -> None:
        from agent_monitor.runners import kimi_runner

        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            result = {
                "accepted": True,
                "fact_ids": ["a" * 16],
                "written": ["proof.md", RECEIPT],
                "proof_text": "# Proof\n",
                "observed_model": None,
                "writeup": {"status": "rejected", "path": "writeup.md", "sidecar": SIDECAR},
            }
            events: list[dict] = []
            previous = Path.cwd()
            os.chdir(workspace)
            try:
                with (
                    patch.dict(os.environ, {"KIMI_API_KEY": "sk-test-kimi-123456789"}, clear=True),
                    patch.object(sys, "argv", ["kimi_runner.py", "Prove it."]),
                    patch.object(runner, "run_pipeline", return_value=result),
                    patch.object(kimi_runner, "emit_item", side_effect=events.append),
                ):
                    self.assertEqual(kimi_runner.main(), 0)
            finally:
                os.chdir(previous)
            self.assertEqual(
                [change["path"] for change in events[0]["changes"]],
                ["proof.md", RECEIPT, "writeup.md", SIDECAR],
            )
            self.assertEqual(
                events[-1]["text"],
                "Ansatz CGM · Kimi K3 completed · accepted by the independent model verifier "
                "(1 fact; not a Lean certificate)\n"
                "Written proof: not accepted: the independent check found problems (writeup.md); "
                "the verified facts remain the proof\n\n# Proof\n",
            )


class WriteupMaxTests(unittest.TestCase):
    def test_codex_writes_and_claude_code_checks(self) -> None:
        providers = _Providers(
            claude_replies=[_strategist(2), _ok(), _ok(), _curator(), _ok()],
            codex_replies=[
                _worker("first"),
                _worker("second"),
                "=== WRITEUP ===\n**Theorem.** The sum of two even integers is even.\n\n"
                "**Proof.** Write $a = 2m$ and $b = 2n$; then $a + b = 2(m + n)$. $\\blacksquare$\n"
                "=== END ===",
            ],
        )
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            code, _events = _run_max(workspace, providers, {"AGENT_MONITOR_CGM_WRITEUP": "1"})
            self.assertEqual(code, 0)
            writer, checker = providers.codex_calls[-1], providers.claude_calls[-1]
            self.assertEqual(writer["system"], cgm_writeup.WRITEUP_SYSTEM)
            self.assertEqual(writer["model"], "gpt-6-astra")
            self.assertNotIn("CLAUDE_CONFIG_DIR", writer["source_env"])
            self.assertEqual(checker["system"], cgm_prompts.VERIFIER_SYSTEM)
            self.assertTrue(checker["user"].startswith("Verify the candidate fact labelled THEOREM."))
            self.assertIn("<PREDECESSOR_STATEMENTS>\nnone\n</PREDECESSOR_STATEMENTS>", checker["user"])
            self.assertEqual(checker["max_turns"], 1)
            self.assertEqual(checker["timeout"], 600.0)  # the verifier's (critic) limit
            sidecar = _sidecar(workspace)
            self.assertEqual(sidecar["status"], "accepted")
            self.assertEqual(
                sidecar["routes"],
                {
                    "writer": {"provider": "codex_subscription", "model": "gpt-6-astra", "transport": "codex_exec"},
                    "checker": {"provider": "claude_subscription", "model": "fable", "transport": "claude_code"},
                },
            )
            self.assertEqual(
                [(call["stage"], call["provider"]) for call in sidecar["calls"]],
                [("writeup", "codex_subscription"), ("writecheck", "claude_subscription")],
            )
            receipt = _receipt(workspace)
            self.assertEqual(receipt["schema_version"], cgm_schema.HYBRID_RECEIPT_SCHEMA_VERSION)
            self.assertEqual(receipt["proof_rendering"], cgm_schema.PROOF_RENDERING_VERIFIED_V2)
            self.assertEqual(receipt["workflow"]["calls"]["total"], 6)
            self.assertEqual(_attestation(workspace), "harness-gate-receipt")


class WriteupReaderTests(unittest.TestCase):
    def _published(self, workspace: Path) -> None:
        _pipeline(workspace, [*ACCEPTED, WRITEUP, OK])

    def test_sandbox_preview_carries_the_writeup_and_its_freshness(self) -> None:
        from agent_monitor import console_server

        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            self._published(workspace)
            with patch.object(console_server, "_workspace_for_run", return_value=workspace):
                preview = console_server._sandbox_preview("run-1", {"engine": "math_harness"})
                block = preview["writeup"]
                self.assertEqual(
                    set(block),
                    {"text", "status", "fresh", "path", "revision", "mtime", "check", "model", "provider"},
                )
                self.assertEqual(block["text"], (workspace / "writeup.md").read_text())
                self.assertEqual((block["status"], block["fresh"], block["path"]), ("accepted", True, "writeup.md"))
                self.assertEqual(block["check"], {"verdict": "correct", "summary": "Sound.", "issues": []})
                self.assertEqual((block["model"], block["provider"]), ("kimi-k3", "api"))
                self.assertEqual(preview["text"], (workspace / "proof.md").read_text())
                # A plain run, or a non-CGM engine, has no write-up block.
                self.assertNotIn("writeup", console_server._sandbox_preview("run-1", {"engine": "plain"}))
                # A Sandbox edit of proof.md leaves the write-up stale.
                status, _saved = console_server._save_sandbox_proof(
                    workspace, content=(workspace / "proof.md").read_text() + "\nEdited.\n"
                )
                self.assertEqual(status, 200)
                block = console_server._sandbox_preview("run-1", {"engine": "kimi"})["writeup"]
                self.assertEqual((block["status"], block["fresh"]), ("accepted", False))
                # A write-up its sidecar does not describe is not checked.
                (workspace / "writeup.md").write_text("# Written proof\n\nSomething else.\n")
                block = console_server._sandbox_preview("run-1", {"engine": "kimi"})["writeup"]
                self.assertEqual((block["status"], block["check"]), ("unchecked", None))

    def test_shared_snapshot_includes_the_writeup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runs = Path(raw) / "runs"
            workspace = runs / "workspaces" / "run-1"
            workspace.mkdir(parents=True)
            self._published(workspace)
            with (
                patch.object(run_sharing, "RUNS_DIR", runs),
                patch.object(run_sharing, "CACHE_DIR", Path(raw) / "cache"),
            ):
                shared = run_sharing.snapshot("run-1", {"engine": "math_harness"})
                self.assertEqual(shared["writeup"], (workspace / "writeup.md").read_text())
                self.assertEqual((shared["writeup_status"], shared["writeup_fresh"]), ("accepted", True))
                self.assertEqual(shared["proof"], (workspace / "proof.md").read_text())
                plain = run_sharing.snapshot("run-1", {"engine": "plain"})
                self.assertNotIn("writeup", plain)
                (workspace / "proof.md").write_text("# Proof\n\nEdited.\n")
                self.assertIs(run_sharing.snapshot("run-1", {"engine": "math_harness"})["writeup_fresh"], False)


class WriteupJobsTests(unittest.TestCase):
    def test_the_console_switches_decide_the_round_for_each_cgm_run(self) -> None:
        def flag(engine: str, run_env: dict[str, str], service: dict[str, str]) -> str | None:
            env = dict(run_env)
            with patch.dict(os.environ, service, clear=True):
                jobs._apply_cgm_writeup_env(env, engine=engine)
            return env.get("AGENT_MONITOR_CGM_WRITEUP")

        guest = {"AGENT_MONITOR_GUEST": "1"}
        sponsored = {"AGENT_MONITOR_SPONSORED_KIMI": "1"}
        self.assertEqual(flag("math_harness", {}, {}), "1")
        self.assertEqual(flag("kimi", guest, {}), "1")
        self.assertEqual(flag("kimi", {}, {"AGENT_MONITOR_CGM_WRITEUP": "0"}), "0")
        self.assertEqual(flag("kimi", guest, {"AGENT_MONITOR_CGM_WRITEUP_GUESTS": "0"}), "0")
        self.assertEqual(flag("math_harness", sponsored, {"AGENT_MONITOR_CGM_WRITEUP_GUESTS": "off"}), "0")
        self.assertEqual(flag("math_harness", {}, {"AGENT_MONITOR_CGM_WRITEUP_GUESTS": "0"}), "1")
        # A run's own environment cannot switch the round on.
        self.assertEqual(
            flag("kimi", {**guest, "AGENT_MONITOR_CGM_WRITEUP": "1"}, {"AGENT_MONITOR_CGM_WRITEUP_GUESTS": "0"}),
            "0",
        )
        self.assertIsNone(flag("plain", {}, {}))
        self.assertIsNone(flag("analyze", guest, {}))

    def test_watchdogs_leave_room_for_the_round(self) -> None:
        on = {"AGENT_MONITOR_CGM_WRITEUP": "1"}
        self.assertEqual(jobs._cli_timeout_seconds("kimi", {}), 6_000)
        self.assertEqual(jobs._cli_timeout_seconds("kimi", on), 6_000 + 1_800 + 60)
        guest = {**on, "AGENT_MONITOR_GUEST": "1"}
        self.assertEqual(jobs._cli_timeout_seconds("kimi", guest), 2_400 + 300 + 300 + 1_200 + 60)
        sponsored = {**guest, "AGENT_MONITOR_SPONSORED_KIMI": "1", "AGENT_MONITOR_CGM_API_TIMEOUT": "900"}
        self.assertLessEqual(jobs._cli_timeout_seconds("kimi", sponsored), 6_600)
        self.assertEqual(jobs._cli_timeout_seconds("kimi", sponsored), 2_400 + 900 + 300 + 1_200 + 60)
        claude = {
            **on,
            "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
            "AGENT_MONITOR_MATH_HARNESS_CLAUDE_TIMEOUT": "3600",
            "AGENT_MONITOR_CGM_MAX_SECONDS": "14400",
        }
        self.assertEqual(jobs._cli_timeout_seconds("math_harness", claude), 14_700 + 1_860)
        max_env = {
            **on,
            "AGENT_MONITOR_CGM_ROUTE_MODE": "max",
            "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
            "AGENT_MONITOR_CGM_MAX_SECONDS": "10800",
            "AGENT_MONITOR_MATH_HARNESS_CLAUDE_TIMEOUT": "1800",
            "AGENT_MONITOR_CLI_TIMEOUT": "3600",
        }
        self.assertEqual(jobs._cli_timeout_seconds("math_harness", max_env), 10_800 + 300 + 3_660)
        # An invalid write-up setting adds nothing: the runner skips the round.
        self.assertEqual(
            jobs._cli_timeout_seconds("kimi", {**on, "AGENT_MONITOR_CGM_WRITEUP_MAX_SECONDS": "soon"}), 6_000
        )

    def test_the_pipeline_waits_for_the_round(self) -> None:
        from agent_monitor import auto_pipeline, cgm_max

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(auto_pipeline.max_run_wait_seconds(), cgm_max.RUN_SECONDS + 3_660 + 600)
            self.assertEqual(auto_pipeline.cgm_run_wait_seconds("kimi"), 6_000 + 1_860 + 300)
        with patch.dict(os.environ, {"AGENT_MONITOR_CGM_WRITEUP": "0"}, clear=True):
            self.assertEqual(auto_pipeline.max_run_wait_seconds(), cgm_max.RUN_SECONDS + 600)
            self.assertEqual(auto_pipeline.cgm_run_wait_seconds("kimi"), 6_000 + 300)


if __name__ == "__main__":
    unittest.main()
