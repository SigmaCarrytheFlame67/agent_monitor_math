"""Offline contract tests for the CGM-pipeline Math Agent Harness runner."""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_monitor import engine_graph, kimi_k3
from agent_monitor.cgm import closure as cgm_closure
from agent_monitor.cgm import prompts as cgm_prompts
from agent_monitor.cgm import schema as cgm_schema
from agent_monitor.runners import math_harness_runner as runner


PROBLEM = "Prove that the sum of two even integers is even."


def _strategist(count: int = 2, *, technique: str = "plain", bare: bool = False) -> str:
    plans = [
        {
            "label": f"Route {index}",
            "technique": technique,
            "approach": f"Use independent characterization {index} of even integers.",
            "subgoals": [
                {"id": "S1", "statement": f"Closure step {index} for integer sums."}
            ],
            "risks": f"Check integer closure in route {index}.",
        }
        for index in range(1, count + 1)
    ]
    if bare:
        return json.dumps({"plans": plans})
    return json.dumps(
        {
            "immediate_conclusions": [
                {
                    "statement": "Even means divisible by two.",
                    "justification": "Definition.",
                    "fragile": False,
                }
            ],
            "obstacles": ["Negative integers must be covered."],
            "plans": plans,
        }
    )


def _worker(tag: str, *, lemma: bool = True, status: str = "solved") -> str:
    blocks = []
    uses = "none"
    if lemma:
        blocks.append(
            "=== LEMMA L1 ===\n"
            "Subgoal: S1\n"
            "Uses: none\n"
            "Statement:\n"
            f"For all integers m and n, 2m + 2n = 2(m + n). [{tag}]\n"
            "Proof:\n"
            "Distributivity of multiplication over addition in Z gives "
            "2(m + n) = 2m + 2n for all integers m and n.\n"
            "=== END ==="
        )
        uses = "L1"
    blocks.append(
        "=== THEOREM ===\n"
        f"Uses: {uses}\n"
        "Statement:\n"
        f"{PROBLEM}\n"
        "Proof:\n"
        f"Let a = 2m and b = 2n with integers m and n ({tag}). "
        + ("By L1: 2m + 2n = 2(m + n). " if lemma else "Then a + b = 2(m + n). ")
        + "Since m + n is an integer, a + b is even. This covers all integers.\n"
        "=== END ==="
    )
    blocks.append(
        "=== FINDING example ===\n"
        "Claim: 2 + 4 = 6 is even\n"
        "Evidence:\n"
        "6 = 2 * 3.\n"
        "=== END ==="
    )
    blocks.append(f"=== STATUS ===\n{status}\n=== END ===")
    return "\n\n".join(blocks)


def _ok(summary: str = "Every step is justified.") -> str:
    return json.dumps(
        {
            "verification_report": {
                "summary": summary,
                "critical_errors": [],
                "gaps": [],
            },
            "verdict": "correct",
            "repair_hints": "",
        }
    )


def _wrong(issue: str = "The final divisibility step is not justified.") -> str:
    return json.dumps(
        {
            "verification_report": {
                "summary": "One gap.",
                "critical_errors": [],
                "gaps": [{"location": "THEOREM", "issue": issue}],
            },
            "verdict": "wrong",
            "repair_hints": "Give the missing divisibility implication explicitly.",
        }
    )


def _curator() -> str:
    return json.dumps(
        {
            "annotations": [
                {"node": "N1", "status": "active", "score": 0.6, "comment": "Root (N2)."},
                {"node": "N2", "status": "active", "score": 0.8, "comment": "Solid (N1)."},
            ]
        }
    )


def _accepted_outputs() -> list[object]:
    return [
        _strategist(2),
        _worker("first"),
        _worker("second"),
        _ok(),
        _ok(),
        _curator(),
    ]


@contextmanager
def _in_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _api_run(
    workspace: Path,
    outputs: list[object],
    *,
    env: dict[str, str] | None = None,
    provider_sequence: list[str] | None = None,
    task: str | None = PROBLEM,
) -> tuple[int, list[dict], list[dict]]:
    calls: list[dict] = []
    events: list[dict] = []
    queue = list(outputs)
    providers = list(provider_sequence or [])

    def fake_api(system: str, user: str, *, model: str, timeout: int = 240, **kwargs):
        # The effort is a per-call argument; the process environment is never
        # used to pass it.
        assert "KIMI_REASONING_EFFORT" not in os.environ
        calls.append(
            {
                "system": system,
                "user": user,
                "model": model,
                "timeout": timeout,
                "effort": kwargs.get("effort"),
                "env": kwargs.get("env"),
                "retry_allowance": kwargs.get("retry_allowance"),
            }
        )
        if not queue:
            raise AssertionError("unexpected extra API call")
        value = queue.pop(0)
        if isinstance(value, BaseException):
            raise value
        provider = providers.pop(0) if providers else "kimi"
        return SimpleNamespace(
            text=value,
            usage={"input_tokens": 7, "output_tokens": 11},
            model="kimi-k3" if provider == "kimi" else "gpt-test",
            provider=provider,
        )

    # Queued replies are handed out in call order, which is only defined one
    # call at a time; tests/test_cgm_concurrency.py covers concurrent runs.
    run_env = {
        "AGENT_MONITOR_SELECTED_MODEL": "kimi-k3",
        "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "2",
        "AGENT_MONITOR_MAX_ITERATIONS": "20",
        "AGENT_MONITOR_PROBLEM_ID": "case-1",
        "AGENT_MONITOR_CGM_CONCURRENCY": "1",
        **(env or {}),
    }
    with (
        _in_directory(workspace),
        patch.dict(runner.os.environ, run_env, clear=True),
        patch.object(
            runner.sys,
            "argv",
            (
                ["math_harness_runner.py"]
                if task is None
                else ["math_harness_runner.py", task]
            ),
        ),
        patch.object(runner, "codex_subscription_enabled", return_value=False),
        patch.object(runner, "claude_subscription_enabled", return_value=False),
        patch.object(runner, "api_chat", side_effect=fake_api),
        patch.object(runner, "emit", side_effect=events.append),
    ):
        returncode = runner.main()
    return returncode, calls, events


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _project(workspace: Path) -> Path:
    return workspace / "math_harness" / "project"


def _receipt(workspace: Path) -> dict:
    return json.loads((_project(workspace) / "engine_receipt.json").read_text())


def _usage_stages(events: list[dict]) -> list[str]:
    return [event["stage"] for event in events if event.get("type") == "turn.completed"]


class MathHarnessAcceptedArtifactTests(unittest.TestCase):
    def test_no_argument_uses_canonical_problem_file_without_argv_leak(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            (workspace / "problem.txt").write_text(PROBLEM, encoding="utf-8")
            returncode, calls, _events = _api_run(
                workspace, _accepted_outputs(), task=None
            )
            self.assertEqual(returncode, 0)
            self.assertEqual(len(calls), 6)
            self.assertIn(PROBLEM, calls[0]["user"])
            self.assertIn(runner.DEFAULT_TASK_DIRECTIVE, calls[0]["user"])
            receipt = _receipt(workspace)
            self.assertEqual(
                receipt["problem_sha256"],
                hashlib.sha256(PROBLEM.encode()).hexdigest(),
            )
            self.assertEqual(
                receipt["task_sha256"],
                hashlib.sha256(runner.DEFAULT_TASK_DIRECTIVE.encode()).hexdigest(),
            )

    def test_verified_closure_writes_cgm_graph_and_bound_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, calls, events = _api_run(workspace, _accepted_outputs())
            self.assertEqual(returncode, 0)
            self.assertEqual(len(calls), 6)
            self.assertEqual(
                [call["system"] for call in calls],
                [
                    cgm_prompts.STRATEGIST_SYSTEM,
                    cgm_prompts.worker_system("plain"),
                    cgm_prompts.worker_system("plain"),
                    cgm_prompts.VERIFIER_SYSTEM,
                    cgm_prompts.VERIFIER_SYSTEM,
                    cgm_prompts.CURATOR_SYSTEM,
                ],
            )
            project = _project(workspace)
            self.assertEqual(
                (project / "PROBLEM.md").read_text(encoding="utf-8"),
                f"# Problem\n\n{PROBLEM}\n",
            )
            # The lemma, then the target citing it, both content-addressed.
            lemma_statement = (
                "For all integers m and n, 2m + 2n = 2(m + n). [first]"
            )
            lemma_proof = (
                "Distributivity of multiplication over addition in Z gives "
                "2(m + n) = 2m + 2n for all integers m and n."
            )
            lemma_id = cgm_schema.compute_fact_id(
                problem_id="case-1",
                predecessors=[],
                glossary_introduces={},
                statement=lemma_statement,
                proof=lemma_proof,
            )
            facts_dir = project / "fact_graph" / "facts"
            fact_names = sorted(path.name for path in facts_dir.iterdir())
            self.assertEqual(len(fact_names), 2)
            self.assertIn(f"{lemma_id}.md", fact_names)
            target_id = (project / "TARGET.md").read_text().strip()
            target_bytes = (facts_dir / f"{target_id}.md").read_bytes()
            self.assertIn(f"predecessors: [{lemma_id}]".encode(), target_bytes)
            self.assertIn(b"glossary_introduces: {}", target_bytes)
            self.assertIn(b"external_refs: []", target_bytes)
            self.assertIn(f"author: {runner.AUTHOR}".encode(), target_bytes)
            self.assertIn(f"Cited facts: L1 = fact {lemma_id}.".encode(), target_bytes)

            dag = engine_graph.load_fact_dag(workspace)
            assert dag is not None
            self.assertEqual([item["fact_id"] for item in dag], [lemma_id, target_id])
            proof_path = workspace / "proof.md"
            self.assertEqual(
                proof_path.read_text(encoding="utf-8"),
                cgm_closure.render_verified_closure(dag, target_id),
            )
            self.assertIn("not a Lean or other formal certificate", proof_path.read_text())

            receipt = _receipt(workspace)
            self.assertEqual(receipt["schema_version"], 3)
            self.assertEqual(receipt["engine"], "math_harness")
            self.assertEqual(receipt["source"], runner.ADAPTER_SOURCE)
            self.assertEqual(receipt["verdict"], "accepted")
            self.assertEqual(receipt["semantics"], "model-verifier")
            self.assertIs(receipt["certificate"], False)
            self.assertIs(receipt["lean_verified"], False)
            self.assertEqual(receipt["target_fact_id"], target_id)
            self.assertEqual(
                [item["fact_id"] for item in receipt["facts"]], [lemma_id, target_id]
            )
            self.assertEqual(receipt["provider"], "kimi")
            self.assertEqual(receipt["model"], "kimi-k3")
            self.assertEqual(
                receipt["prompt_pack_sha256"], cgm_prompts.prompt_pack_sha256()
            )
            self.assertEqual(
                receipt["source_sha256"],
                hashlib.sha256(Path(runner.__file__).read_bytes()).hexdigest(),
            )
            self.assertEqual(
                receipt["proof_sha256"],
                hashlib.sha256(proof_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                receipt["binding_sha256"], cgm_schema.receipt_binding_digest(receipt)
            )
            for name, digest in receipt["memory"].items():
                with self.subTest(memory=name):
                    self.assertEqual(
                        hashlib.sha256(
                            (project / "global_memory" / name).read_bytes()
                        ).hexdigest(),
                        digest,
                    )
            self.assertEqual(
                receipt["workflow"]["calls"],
                {
                    "strategist": 1,
                    "worker": 2,
                    "verify": 2,
                    "repair": 0,
                    "curator": 1,
                    "total": 6,
                },
            )
            self.assertEqual(
                receipt["workflow"]["stages"],
                ["scaffold", "work", "verify", "curate", "finalize"],
            )
            self.assertEqual(
                [item["verdict"] for item in receipt["verdicts"]], ["correct", "correct"]
            )
            self.assertIs(receipt["workflow"]["tools_enabled"], False)
            self.assertEqual(
                receipt["workflow"]["response_only_contract"],
                {"transport": "api_chat", "isolation_version": 1},
            )
            self.assertEqual(
                list(workspace.rglob("*receipt*.json")),
                [project / "engine_receipt.json"],
                "the project receipt is the only authoritative receipt",
            )

            facts = engine_graph.build(workspace, {"engine": "math_harness"})
            self.assertEqual(facts["status"], "complete")
            self.assertEqual(facts["selection"]["target_ids"], [target_id])
            self.assertEqual(facts["provenance"]["attestation"], "harness-gate-receipt")
            self.assertEqual(facts["provenance"]["receipt_status"], "verifier-correct")
            self.assertIs(facts["provenance"]["certificate"], False)
            self.assertEqual(
                facts["graph"]["edges"],
                [{"from": lemma_id, "to": target_id, "label": "fact dependency"}],
            )
            workflow = engine_graph.build(
                workspace, {"engine": "math_harness"}, view="workflow"
            )
            self.assertEqual(workflow["status"], "complete")
            self.assertIs(workflow["provenance"]["memory_receipt_bound"], True)

            from agent_monitor import jobs

            proof_before_analysis = proof_path.read_bytes()
            jobs._attach_analysis(
                {
                    "status": "finished",
                    "engine": "math_harness",
                    "model": "kimi-k3",
                    "agents": [],
                    "totals": {},
                    "pipeline": [],
                    "edges": [],
                },
                workspace,
            )
            self.assertEqual(proof_path.read_bytes(), proof_before_analysis)
            self.assertEqual(
                engine_graph.build(workspace, {"engine": "math_harness"})["provenance"][
                    "attestation"
                ],
                "harness-gate-receipt",
            )

            plans = _read_jsonl(project / "global_memory" / "plan.jsonl")
            attempts = _read_jsonl(project / "global_memory" / "proof_attempt.jsonl")
            verifications = _read_jsonl(project / "global_memory" / "verification.jsonl")
            edges = _read_jsonl(project / "global_memory" / "edges.jsonl")
            annotations = _read_jsonl(project / "global_memory" / "annotations.jsonl")
            self.assertEqual([row["kind"] for row in plans], ["plan", "plan"])
            self.assertEqual(len(attempts), 2)
            self.assertEqual(attempts[0]["status"], "verified")
            self.assertEqual(attempts[0]["fact_id"], target_id)
            self.assertEqual(
                [row["status"] for row in verifications], ["supported", "supported"]
            )
            edge_types = {edge["type"] for edge in edges}
            self.assertTrue(
                {
                    "addresses",
                    "decomposes-into",
                    "alternative-of",
                    "refines",
                    "promoted-to",
                    "supports",
                    "blocks",
                }.issubset(edge_types)
            )
            decompositions = [e for e in edges if e["type"] == "decomposes-into"]
            self.assertTrue(all(e["technique"] == "plain" for e in decompositions))
            self.assertEqual(
                {e["and_group"] for e in decompositions}, {"plan-1", "plan-2"}
            )
            by_node = {row["node_id"]: row for row in annotations}
            self.assertEqual(by_node["goal:root"]["status"], "closed")
            self.assertEqual(by_node["goal:root"]["score"], 1.0)
            self.assertEqual(by_node["goal:p1-s1"]["status"], "closed")
            self.assertEqual((project / "global_memory" / "example.jsonl").exists(), True)

            all_files = [path for path in workspace.rglob("*") if path.is_file()]
            self.assertTrue(all_files)
            for path in all_files:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path)

            event_types = [event.get("type") for event in events]
            self.assertIn("thread.started", event_types)
            self.assertTrue(
                any(
                    event.get("type") == "item.completed"
                    and (event.get("item") or {}).get("type") == "file_change"
                    for event in events
                )
            )
            self.assertEqual(
                _usage_stages(events),
                ["strategist", "worker-1", "worker-2", "verify-1", "verify-2", "curator"],
            )
            usage_events = [e for e in events if e.get("type") == "turn.completed"]
            self.assertEqual(sum(e["usage"]["input_tokens"] for e in usage_events), 42)
            self.assertEqual(
                [e["stage"] for e in events if e.get("type") == "pipeline.stage"],
                ["scaffold", "work", "verify", "curate", "finalize"],
            )

    def test_problem_txt_is_canonical_while_wrapped_argv_remains_task_context(self) -> None:
        wrapped_task = (
            "Your working directory is /tmp/example. Read problem.txt.\n\n"
            "PROBLEM:\n" + PROBLEM + "\n\nUse the supplied library if relevant."
        )
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            (workspace / "problem.txt").write_text(PROBLEM + "\n", encoding="utf-8")
            returncode, calls, _events = _api_run(
                workspace,
                _accepted_outputs(),
                task=wrapped_task,
                env={"AGENT_MONITOR_PROBLEM_ID": "stable-problem-id"},
            )
            self.assertEqual(returncode, 0)
            self.assertIn(wrapped_task, calls[0]["user"])
            for call in calls[3:5]:
                self.assertNotIn("Your working directory", call["user"])
            project = _project(workspace)
            self.assertEqual(
                (project / "PROBLEM.md").read_text(encoding="utf-8"),
                f"# Problem\n\n{PROBLEM}\n",
            )
            receipt = _receipt(workspace)
            self.assertEqual(receipt["problem_id"], "stable-problem-id")
            self.assertEqual(
                receipt["task_sha256"], hashlib.sha256(wrapped_task.encode()).hexdigest()
            )
            for fact in (project / "fact_graph" / "facts").glob("*.md"):
                text = fact.read_text()
                self.assertIn("problem_id: stable-problem-id", text)
                self.assertNotIn("Your working directory", text)

    def test_archive_compatible_fact_id_is_whitespace_stable(self) -> None:
        first = runner.compute_fact_id(
            problem_id="P",
            predecessors=["bbbbbbbbbbbbbbbb", "aaaaaaaaaaaaaaaa"],
            glossary_introduces={"z": "last", "a": "first"},
            statement=" A\n B ",
            proof=" C\tD ",
        )
        second = runner.compute_fact_id(
            problem_id="P",
            predecessors=["aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"],
            glossary_introduces={"a": "first", "z": "last"},
            statement="A B",
            proof="C D",
        )
        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{16}$")

    def test_independent_verifier_sees_one_fact_and_predecessor_statements_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, calls, _events = _api_run(workspace, _accepted_outputs())
            self.assertEqual(returncode, 0)
            lemma_call, theorem_call = calls[3], calls[4]
            self.assertIn("labelled L1", lemma_call["user"])
            self.assertIn("<PREDECESSOR_STATEMENTS>\nnone\n", lemma_call["user"])
            self.assertIn("labelled THEOREM", theorem_call["user"])
            self.assertIn("[L1] For all integers m and n", theorem_call["user"])
            # Statements only: never the predecessor's proof, the plan, other
            # candidates, or any authorship framing.
            self.assertNotIn("Distributivity", theorem_call["user"])
            for call in (lemma_call, theorem_call):
                self.assertNotIn("second", call["user"])
                self.assertNotIn("Route 1", call["user"])
                self.assertNotIn("worker", call["user"].lower())
            self.assertIn(f"<STATEMENT>\n{PROBLEM}\n</STATEMENT>", theorem_call["user"])


class MathHarnessRepairAndDegradationTests(unittest.TestCase):
    def test_rejected_target_is_repaired_and_reverified_with_cached_lemmas(self) -> None:
        repaired = _worker("first").replace(
            "Since m + n is an integer",
            "Since Z is closed under addition, m + n is an integer, so by the "
            "definition of evenness",
        )
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, calls, events = _api_run(
                workspace,
                [
                    _strategist(2),
                    _worker("first"),
                    _worker("second"),
                    _ok(),
                    _wrong(),
                    repaired,
                    _ok(),
                    _curator(),
                ],
            )
            self.assertEqual(returncode, 0)
            self.assertEqual(len(calls), 8)
            repair_call = calls[5]
            self.assertEqual(repair_call["system"], cgm_prompts.worker_system("plain"))
            self.assertIn("Give the missing divisibility implication", repair_call["user"])
            self.assertIn('<VERIFIER_REPORT block="THEOREM">', repair_call["user"])
            self.assertIn("<VERIFIER_ACCEPTED>L1</VERIFIER_ACCEPTED>", repair_call["user"])
            # The unchanged lemma keeps its cached verdict; only the theorem
            # is re-verified.
            self.assertIn("labelled THEOREM", calls[6]["user"])
            self.assertEqual(
                _usage_stages(events),
                [
                    "strategist", "worker-1", "worker-2", "verify-1", "verify-2",
                    "repair-1", "verify-3", "curator",
                ],
            )
            receipt = _receipt(workspace)
            self.assertEqual(receipt["verdict"], "accepted")
            self.assertEqual(receipt["workflow"]["calls"]["repair"], 1)
            project = _project(workspace)
            attempts = _read_jsonl(project / "global_memory" / "proof_attempt.jsonl")
            self.assertEqual(len(attempts), 3)
            self.assertEqual(attempts[2]["links"]["predecessors"], [attempts[0]["id"]])
            self.assertEqual(attempts[2]["status"], "verified")
            edges = _read_jsonl(project / "global_memory" / "edges.jsonl")
            self.assertIn(
                (attempts[0]["id"], attempts[2]["id"], "refines"),
                {(e["src"], e["dst"], e["type"]) for e in edges},
            )
            workflow = engine_graph.build(
                workspace, {"engine": "math_harness"}, view="workflow"
            )
            self.assertEqual(workflow["status"], "complete")

    def test_exhausted_repairs_publish_a_labelled_unattested_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, calls, _events = _api_run(
                workspace,
                [
                    _strategist(2),
                    _worker("first"),
                    _worker("second"),
                    _ok(),
                    _wrong(),
                    _ok(),
                    _wrong(),
                    _curator(),
                ],
                env={"AGENT_MONITOR_CGM_MAX_REPAIRS": "0"},
            )
            self.assertEqual(returncode, 0)
            self.assertEqual(len(calls), 8)
            project = _project(workspace)
            self.assertFalse((project / "TARGET.md").exists())
            proof = (workspace / "proof.md").read_text(encoding="utf-8")
            self.assertTrue(proof.startswith("# Proof candidate (not verifier-accepted)"))
            self.assertIn("NOT accepted by the independent model verifier", proof)
            self.assertIn("(verifier-accepted)", proof)
            receipt = _receipt(workspace)
            self.assertEqual(receipt["verdict"], "unattested")
            self.assertIsNone(receipt["target_fact_id"])
            self.assertEqual(len(receipt["facts"]), 1)
            self.assertFalse(receipt["facts"][0]["is_target"])
            facts = engine_graph.build(workspace, {"engine": "math_harness"})
            self.assertEqual(facts["status"], "partial")
            self.assertEqual(facts["provenance"]["attestation"], "unattested")
            self.assertEqual(facts["provenance"]["receipt_status"], "verifier-partial")
            self.assertIs(facts["graph"]["nodes"][0]["meta"]["verifier_accepted"], True)
            dag = engine_graph.load_fact_dag(workspace)
            assert dag is not None
            self.assertEqual([item["attested"] for item in dag], [False])
            self.assertEqual([item["is_target"] for item in dag], [False])
            workflow = engine_graph.build(
                workspace, {"engine": "math_harness"}, view="workflow"
            )
            self.assertEqual(workflow["status"], "partial")
            goal = next(n for n in workflow["graph"]["nodes"] if n["id"] == "goal:root")
            self.assertNotEqual(goal["meta"]["status"], "closed")

    def test_partial_continuation_retires_the_prior_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            accepted, _, _ = _api_run(workspace, _accepted_outputs())
            self.assertEqual(accepted, 0)
            project = _project(workspace)
            old_facts = set((project / "fact_graph" / "facts").glob("*.md"))
            self.assertEqual(len(old_facts), 2)

            partial, _, _ = _api_run(
                workspace,
                [
                    _strategist(1, bare=True),
                    _worker("new", lemma=False),
                    _wrong(),
                    _curator(),
                ],
                env={
                    "AGENT_MONITOR_CGM_MAX_REPAIRS": "0",
                    "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1",
                },
            )
            self.assertEqual(partial, 0)
            self.assertFalse((project / "TARGET.md").exists())
            self.assertTrue(all(not path.exists() for path in old_facts))
            self.assertEqual(list((project / "fact_graph" / "facts").iterdir()), [])
            self.assertEqual(_receipt(workspace)["verdict"], "unattested")
            graph = engine_graph.build(workspace, {"engine": "math_harness"})
            self.assertEqual(graph["status"], "none")
            self.assertNotEqual(graph["provenance"]["attestation"], "harness-gate-receipt")
            self.assertIsNone(engine_graph.load_fact_dag(workspace))
            # Optional memory files the new generation did not emit are retired
            # with the old generation (the receipt binds the exact file set).
            memory_names = {path.name for path in (project / "global_memory").iterdir()}
            self.assertNotIn("conclusion.jsonl", memory_names)
            self.assertNotIn("obstacle.jsonl", memory_names)
            self.assertEqual(memory_names, set(_receipt(workspace)["memory"]))
            workflow = engine_graph.build(
                workspace, {"engine": "math_harness"}, view="workflow"
            )
            self.assertEqual(workflow["status"], "partial")

    def test_accepted_continuation_replaces_the_prior_generation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            first_rc, _, _ = _api_run(workspace, _accepted_outputs())
            self.assertEqual(first_rc, 0)
            project = _project(workspace)
            old = set((project / "fact_graph" / "facts").glob("*.md"))
            second_rc, _, _ = _api_run(
                workspace,
                [
                    _strategist(2),
                    _worker("renewed"),
                    _worker("other"),
                    _ok(),
                    _ok(),
                    _curator(),
                ],
            )
            self.assertEqual(second_rc, 0)
            new = set((project / "fact_graph" / "facts").glob("*.md"))
            self.assertEqual(len(new), 2)
            self.assertTrue(all(not path.exists() for path in old))
            receipt = _receipt(workspace)
            self.assertEqual(
                sorted(f"{item['fact_id']}.md" for item in receipt["facts"]),
                sorted(path.name for path in new),
            )
            self.assertEqual(
                engine_graph.build(workspace, {"engine": "math_harness"})["provenance"][
                    "attestation"
                ],
                "harness-gate-receipt",
            )

    def test_malformed_worker_gets_one_format_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, calls, events = _api_run(
                workspace,
                [
                    _strategist(1),
                    "I think the answer is yes, because evens add up.",
                    _worker("fixed"),
                    _ok(),
                    _ok(),
                    _curator(),
                ],
                env={"AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1"},
            )
            self.assertEqual(returncode, 0)
            self.assertIn("<FORMAT_REMINDER>", calls[2]["user"])
            self.assertIn("<PREVIOUS_REPLY>", calls[2]["user"])
            self.assertIn("worker-1-retry", _usage_stages(events))
            receipt = _receipt(workspace)
            self.assertEqual(receipt["workflow"]["retries"]["format"], 1)
            self.assertEqual(receipt["verdict"], "accepted")

    def test_a_failed_worker_does_not_kill_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, calls, _events = _api_run(
                workspace,
                [
                    _strategist(2),
                    runner.APIBackendError("upstream 503"),
                    _worker("second"),
                    _ok(),
                    _ok(),
                    _curator(),
                ],
            )
            self.assertEqual(returncode, 0)
            self.assertEqual(len(calls), 6)
            receipt = _receipt(workspace)
            self.assertEqual(receipt["verdict"], "accepted")
            self.assertIn("worker-provider-failure", receipt["workflow"]["degraded"])
            self.assertIsNone(receipt["calls"][1]["response_sha256"])
            attempts = _read_jsonl(
                _project(workspace) / "global_memory" / "proof_attempt.jsonl"
            )
            self.assertIn("provider call failed", attempts[0]["evidence"])
            self.assertEqual(attempts[1]["status"], "verified")
            self.assertEqual(
                engine_graph.build(
                    workspace, {"engine": "math_harness"}, view="workflow"
                )["status"],
                "complete",
            )

    def test_all_unusable_workers_fail_closed_without_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, calls, events = _api_run(
                workspace,
                [_strategist(1), "no blocks here", "still none"],
                env={"AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1"},
            )
            self.assertEqual(returncode, 1)
            self.assertEqual(len(calls), 3)
            self.assertFalse((workspace / "proof.md").exists())
            self.assertFalse((workspace / "math_harness").exists())
            terminal = [
                e["item"] for e in events
                if e.get("type") == "item.completed" and (e.get("item") or {}).get("terminal")
            ]
            self.assertEqual(len(terminal), 1)
            self.assertIn("no worker produced a usable candidate", terminal[0]["message"])

    def test_unusable_strategist_degrades_to_one_direct_plan(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, calls, _events = _api_run(
                workspace,
                [
                    "not json",
                    "still not json",
                    _worker("direct", lemma=False),
                    _ok(),
                    _curator(),
                ],
            )
            self.assertEqual(returncode, 0)
            self.assertIn("Label: Direct proof", calls[2]["user"])
            receipt = _receipt(workspace)
            self.assertEqual(receipt["verdict"], "accepted")
            self.assertIn("strategist-fallback-plan", receipt["workflow"]["degraded"])
            self.assertEqual(len(receipt["facts"]), 1)

    def test_provider_error_and_provider_change_fail_without_fallback_or_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, calls, _events = _api_run(
                workspace,
                [runner.APIBackendError("provider unavailable")],
            )
            self.assertEqual(returncode, 1)
            self.assertEqual(len(calls), 1)
            self.assertEqual(list(workspace.iterdir()), [])

        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, _calls, _events = _api_run(
                workspace,
                _accepted_outputs(),
                provider_sequence=["kimi", "openai", "kimi", "kimi", "kimi", "kimi"],
            )
            self.assertEqual(returncode, 1)
            self.assertFalse((workspace / "proof.md").exists())
            self.assertFalse((workspace / "math_harness").exists())

    def test_pseudo_tool_markup_is_never_accepted(self) -> None:
        tool_reply = _worker("tool") + "\n<function_calls><invoke name=\"x\"></invoke></function_calls>"
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, calls, _events = _api_run(
                workspace,
                [_strategist(1), tool_reply, tool_reply],
                env={"AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1"},
            )
            self.assertEqual(returncode, 1)
            self.assertEqual(len(calls), 3)
            self.assertIn("pseudo-tool", calls[2]["user"])
            self.assertEqual(list(workspace.iterdir()), [])

    def test_oversized_replies_are_rejected_by_the_per_reply_byte_cap(self) -> None:
        self.assertGreaterEqual(runner.MAX_WORKER_BYTES, 100_000)
        self.assertLessEqual(runner.MAX_PROOF_BYTES, engine_graph.MAX_PROOF_ARTIFACT_BYTES)
        with tempfile.TemporaryDirectory() as raw, patch.object(runner, "MAX_WORKER_BYTES", 200):
            workspace = Path(raw)
            returncode, calls, _events = _api_run(
                workspace,
                [_strategist(1), _worker("big"), _worker("big again")],
                env={"AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1"},
            )
            self.assertEqual(returncode, 1)
            self.assertEqual(len(calls), 3)
            self.assertIn("byte limit", calls[2]["user"])
            self.assertEqual(list(workspace.iterdir()), [])

    def test_unusable_verifier_output_is_an_error_never_an_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, _calls, _events = _api_run(
                workspace,
                [
                    _strategist(1),
                    _worker("only", lemma=False),
                    "Looks right to me!",
                    "Definitely correct.",
                    _curator(),
                ],
                env={"AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1"},
            )
            self.assertEqual(returncode, 0)
            receipt = _receipt(workspace)
            self.assertEqual(receipt["verdict"], "unattested")
            self.assertEqual([v["verdict"] for v in receipt["verdicts"]], ["error"])
            rows = _read_jsonl(_project(workspace) / "global_memory" / "verification.jsonl")
            self.assertEqual(rows[0]["status"], "open")
            self.assertIn("verifier-unparsable", receipt["workflow"]["degraded"])

    def test_inconsistent_correct_verdict_is_treated_as_wrong(self) -> None:
        inconsistent = json.dumps(
            {
                "verification_report": {
                    "summary": "claims success",
                    "critical_errors": [{"location": "THEOREM", "issue": "bad step"}],
                    "gaps": [],
                },
                "verdict": "correct",
                "repair_hints": "",
            }
        )
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, calls, _events = _api_run(
                workspace,
                [
                    _strategist(1),
                    _worker("only", lemma=False),
                    inconsistent,
                    _curator(),
                ],
                env={
                    "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1",
                    "AGENT_MONITOR_CGM_MAX_REPAIRS": "0",
                },
            )
            self.assertEqual(returncode, 0)
            self.assertEqual(len(calls), 4)
            receipt = _receipt(workspace)
            self.assertEqual(receipt["verdict"], "unattested")
            self.assertEqual([v["verdict"] for v in receipt["verdicts"]], ["wrong"])

    def test_kimi_empty_reply_is_retried_once_at_low_effort(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, calls, events = _api_run(
                workspace,
                [
                    runner.APIBackendError("Kimi returned an empty response for kimi-k3"),
                    _strategist(1),
                    _worker("only", lemma=False),
                    _ok(),
                    _curator(),
                ],
                env={"AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1"},
            )
            self.assertEqual(returncode, 0)
            self.assertEqual([call["effort"] for call in calls], ["high", "low", "high", "high", "low"])
            self.assertEqual(_usage_stages(events)[0], "strategist-low")
            receipt = _receipt(workspace)
            self.assertEqual(receipt["workflow"]["retries"]["empty"], 1)
            self.assertEqual(receipt["calls"][0]["stage"], "strategist")
            self.assertIsNone(receipt["calls"][0]["response_sha256"])
            self.assertEqual(receipt["verdict"], "accepted")
            # The process environment is restored after each call.
            self.assertNotIn("KIMI_REASONING_EFFORT", os.environ)

    def test_reasoning_effort_policy_per_stage(self) -> None:
        effort = runner._stage_effort
        self.assertEqual(
            [effort(family, {}) for family in ("strategist", "worker", "verify", "repair", "curator")],
            ["high", "high", "high", "high", "low"],
        )
        guest = {"AGENT_MONITOR_MAX_OUTPUT_TOKENS": "4096", "KIMI_REASONING_EFFORT": "high"}
        self.assertEqual(
            [effort(family, guest) for family in ("strategist", "worker", "verify", "repair")],
            ["high", "low", "high", "low"],
        )
        self.assertEqual(effort("worker", {"AGENT_MONITOR_API_MAX_TOKENS": "16384"}), "high")
        self.assertEqual(effort("verify", {"KIMI_REASONING_EFFORT": "max"}), "max")
        self.assertEqual(
            effort("worker", {**guest, "AGENT_MONITOR_CGM_REASONING_EFFORT": "max"}), "max"
        )
        self.assertEqual(effort("curator", {"AGENT_MONITOR_CGM_REASONING_EFFORT": "max"}), "low")
        self.assertEqual(effort("worker", {"KIMI_REASONING_EFFORT": "bogus"}), "high")

    def test_curator_failure_keeps_deterministic_annotations(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, _calls, _events = _api_run(
                workspace,
                [
                    _strategist(1),
                    _worker("only"),
                    _ok(),
                    _ok(),
                    "no json",
                    "still no json",
                ],
                env={"AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1"},
            )
            self.assertEqual(returncode, 0)
            receipt = _receipt(workspace)
            self.assertEqual(receipt["verdict"], "accepted")
            self.assertIn("curator-deterministic", receipt["workflow"]["degraded"])
            annotations = _read_jsonl(
                _project(workspace) / "global_memory" / "annotations.jsonl"
            )
            by_node = {row["node_id"]: row for row in annotations}
            self.assertEqual(by_node["goal:root"]["status"], "closed")
            self.assertEqual(
                engine_graph.build(
                    workspace, {"engine": "math_harness"}, view="workflow"
                )["status"],
                "complete",
            )

    def test_total_call_budget_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, calls, _events = _api_run(
                workspace,
                [
                    _strategist(3),
                    _worker("first"),
                    _worker("second"),
                    _ok(),
                    _curator(),
                ],
                env={
                    "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "3",
                    "AGENT_MONITOR_CGM_MAX_CALLS": "5",
                },
            )
            self.assertEqual(returncode, 0)
            self.assertEqual(len(calls), 5)
            receipt = _receipt(workspace)
            self.assertEqual(receipt["workflow"]["calls"]["total"], 5)
            self.assertEqual(receipt["workflow"]["budgets"]["max_calls"], 5)
            self.assertEqual(receipt["verdict"], "unattested")
            self.assertIn("verification-stopped-by-budget", receipt["workflow"]["degraded"])

    def test_oversized_problem_is_rejected_before_any_call(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            calls: list[object] = []
            with (
                _in_directory(workspace),
                patch.object(
                    runner.sys,
                    "argv",
                    ["math_harness_runner.py", "x" * (runner.MAX_PROBLEM_BYTES + 1)],
                ),
                patch.object(runner, "api_chat", side_effect=lambda *a, **k: calls.append(a)),
                patch.object(runner, "emit"),
            ):
                self.assertEqual(runner.main(), 1)
            self.assertEqual(calls, [])
            self.assertEqual(list(workspace.iterdir()), [])

    def test_oversized_problem_file_and_task_fail_before_any_call(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            (workspace / "problem.txt").write_text(
                "x" * (runner.MAX_PROBLEM_BYTES + 1), encoding="utf-8"
            )
            calls: list[object] = []
            with (
                _in_directory(workspace),
                patch.object(runner.sys, "argv", ["runner", PROBLEM]),
                patch.object(runner, "api_chat", side_effect=lambda *a, **k: calls.append(a)),
                patch.object(runner, "emit"),
            ):
                self.assertEqual(runner.main(), 1)
            self.assertEqual(calls, [])

        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            calls = []
            with (
                _in_directory(workspace),
                patch.object(
                    runner.sys,
                    "argv",
                    ["runner", "x" * (runner.MAX_TASK_BYTES + 1)],
                ),
                patch.object(runner, "api_chat", side_effect=lambda *a, **k: calls.append(a)),
                patch.object(runner, "emit"),
            ):
                self.assertEqual(runner.main(), 2)
            self.assertEqual(calls, [])


def _sponsored_failure(
    code: int, kind: str, *, elapsed: float = 0.4, **fields: object
) -> runner.APIBackendError:
    """What api_chat raises for a typed sponsored Kimi failure."""
    error = {"message": "safe", "type": kind, "forwarded": False, **fields}
    failure = kimi_k3.call_failure(code, {"error": error}, elapsed=elapsed)
    return runner.APIBackendError(kimi_k3.failure_message(failure), failure=failure)


def _busy() -> runner.APIBackendError:
    """A fast upstream 503 the transport retries could not get through."""
    return _sponsored_failure(
        503, "upstream_rejected", forwarded=True, upstream_status=503, upstream_elapsed_ms=300
    )


def _timed_out() -> runner.APIBackendError:
    return _sponsored_failure(504, "upstream_timeout", forwarded=True, elapsed=600.0)


_SPONSORED_ENV = {
    "AGENT_MONITOR_SPONSORED_KIMI": "1",
    "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1",
}


class SponsoredKimiResilienceTests(unittest.TestCase):
    """A busy or slow sponsored provider degrades the run instead of ending it."""

    def _run(self, workspace: Path, outputs: list[object], *, env: dict[str, str] | None = None):
        waits: list[float] = []

        def no_wait(_ctx, seconds: float) -> bool:
            waits.append(seconds)
            return True

        with patch.object(runner, "_wait_for_provider", side_effect=no_wait):
            returncode, calls, events = _api_run(
                workspace, outputs, env={**_SPONSORED_ENV, **(env or {})}
            )
        return returncode, calls, events, waits

    @staticmethod
    def _notes(events: list[dict]) -> list[str]:
        return [
            str((event.get("item") or {}).get("text") or "")
            for event in events
            if event.get("type") == "item.completed"
            and (event.get("item") or {}).get("type") == "reasoning"
        ]

    def test_busy_strategist_waits_then_retries_in_the_same_call(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, calls, events, waits = self._run(
                workspace,
                [_busy(), _busy(), _strategist(1), _worker("only", lemma=False), _ok(), _curator()],
            )
            self.assertEqual(returncode, 0)
            self.assertEqual(waits, [30.0, 60.0])
            self.assertEqual(len(calls), 6)
            notes = self._notes(events)
            for seconds in (30, 60):
                self.assertIn(
                    "Math Harness · 2 Explore · strategist · Kimi's provider is busy; "
                    f"retrying in {seconds} s (no tokens used)",
                    notes,
                )
            receipt = _receipt(workspace)
            # Nothing was processed before the retry: still one strategist call.
            self.assertEqual(
                [call["stage"] for call in receipt["calls"]],
                ["strategist", "worker-1", "verify-1", "curator"],
            )
            self.assertIsNotNone(receipt["calls"][0]["response_sha256"])
            self.assertEqual(receipt["verdict"], "accepted")
            self.assertNotIn("strategist-fallback-plan", receipt["workflow"]["degraded"])
            self.assertIsNotNone(
                engine_graph._cgm_receipt(
                    (_project(workspace) / "engine_receipt.json").read_bytes()
                )
            )

    def test_a_strategist_that_stays_busy_falls_back_to_the_direct_plan(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, calls, events, waits = self._run(
                workspace,
                [_busy()] * 4 + [_worker("direct", lemma=False), _ok(), _curator()],
            )
            self.assertEqual(returncode, 0)
            self.assertEqual(waits, [30.0, 60.0, 120.0])
            self.assertIn("Label: Direct proof", calls[4]["user"])
            receipt = _receipt(workspace)
            self.assertEqual(
                [call["stage"] for call in receipt["calls"]],
                ["strategist", "worker-1", "verify-1", "curator"],
            )
            self.assertIsNone(receipt["calls"][0]["response_sha256"])
            for reason in ("strategist-provider-failure", "strategist-fallback-plan"):
                self.assertIn(reason, receipt["workflow"]["degraded"])
            self.assertEqual(receipt["verdict"], "accepted")
            self.assertIn(
                "Math Harness · 2 Explore · the strategist call was refused "
                "(Kimi's provider is busy; no tokens used); using one direct plan",
                self._notes(events),
            )

    def test_an_ambiguous_strategist_failure_falls_back_without_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, calls, _events, waits = self._run(
                workspace,
                [_timed_out(), _worker("direct", lemma=False), _ok(), _curator()],
            )
            self.assertEqual(returncode, 0)
            self.assertEqual(waits, [])
            self.assertEqual(len(calls), 4)
            receipt = _receipt(workspace)
            self.assertIn("strategist-fallback-plan", receipt["workflow"]["degraded"])
            self.assertEqual(receipt["verdict"], "accepted")

        # An untyped failure (an older broker, a mocked provider) is ambiguous too.
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, _calls, _events, waits = self._run(
                workspace,
                [
                    runner.APIBackendError("HTTP 502 from upstream"),
                    _worker("direct", lemma=False),
                    _ok(),
                    _curator(),
                ],
            )
            self.assertEqual((returncode, waits), (0, []))

    def test_a_failure_every_later_call_would_repeat_still_aborts(self) -> None:
        for error in (
            _sponsored_failure(429, "sponsor_quota", retry_after=60),
            _sponsored_failure(401, "unauthorized"),
            _sponsored_failure(429, "gateway_quota", retry_after=1800),
        ):
            with self.subTest(kind=error.failure.kind), tempfile.TemporaryDirectory() as raw:
                workspace = Path(raw)
                returncode, calls, events, waits = self._run(workspace, [error])
                self.assertEqual(returncode, 1)
                self.assertEqual((len(calls), waits), (1, []))
                self.assertEqual(list(workspace.iterdir()), [])

    def test_other_routes_keep_aborting_on_a_strategist_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            waits: list[float] = []
            with patch.object(
                runner, "_wait_for_provider", side_effect=lambda _c, s: waits.append(s) or True
            ):
                returncode, calls, _events = _api_run(workspace, [_busy()])
            self.assertEqual((returncode, len(calls), waits), (1, 1, []))
            self.assertEqual(list(workspace.iterdir()), [])

    def test_busy_waits_never_eat_into_the_run_margin(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, _calls, _events, waits = self._run(
                workspace,
                [_busy(), _worker("direct", lemma=False), _ok(), _curator()],
                env={"AGENT_MONITOR_CGM_MAX_SECONDS": "320"},
            )
            self.assertEqual(returncode, 0)
            self.assertEqual(waits, [])
            self.assertIn("strategist-fallback-plan", _receipt(workspace)["workflow"]["degraded"])

    def test_a_rescue_worker_runs_after_every_worker_was_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, calls, events, waits = self._run(
                workspace,
                [_strategist(1), _busy(), _worker("rescued", lemma=False), _ok(), _curator()],
            )
            self.assertEqual(returncode, 0)
            self.assertEqual(waits, [60.0])
            # The rescue carries out plan 1 again, word for word.
            self.assertEqual(calls[2]["user"], calls[1]["user"])
            receipt = _receipt(workspace)
            self.assertEqual(
                [call["stage"] for call in receipt["calls"]],
                ["strategist", "worker-1", "worker-2", "verify-1", "curator"],
            )
            self.assertEqual(receipt["workflow"]["calls"]["worker"], 2)
            self.assertEqual(receipt["selected_worker"], 1)
            self.assertEqual(receipt["selected_attempt"], 2)
            self.assertEqual(receipt["verdict"], "accepted")
            for reason in ("worker-provider-failure", "worker-busy-rescue"):
                self.assertIn(reason, receipt["workflow"]["degraded"])
            # The strategist's reply was billed, so the note makes no claim
            # about tokens although every worker call was refused unprocessed.
            notes = self._notes(events)
            self.assertIn(
                "Math Harness · 2 Explore · Kimi's provider is busy; retrying in 60 s", notes
            )
            self.assertFalse(any("no tokens used" in note for note in notes))
            self.assertIsNotNone(
                engine_graph._cgm_receipt(
                    (_project(workspace) / "engine_receipt.json").read_bytes()
                )
            )
            workflow = engine_graph.build(workspace, {"engine": "math_harness"}, view="workflow")
            self.assertEqual(workflow["status"], "complete")

    def test_a_run_refused_from_the_start_says_no_tokens_were_used(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, _calls, events, waits = self._run(
                workspace,
                [_busy()] * 5 + [_worker("rescued", lemma=False), _ok(), _curator()],
            )
            self.assertEqual(returncode, 0)
            self.assertEqual(waits, [30.0, 60.0, 120.0, 60.0])
            notes = self._notes(events)
            self.assertIn(
                "Math Harness · 2 Explore · the strategist call was refused "
                "(Kimi's provider is busy; no tokens used); using one direct plan",
                notes,
            )
            self.assertIn(
                "Math Harness · 2 Explore · Kimi's provider is busy; retrying in 60 s "
                "(no tokens used)",
                notes,
            )
            self.assertEqual(_receipt(workspace)["verdict"], "accepted")

    def test_two_refused_workers_get_one_rescue_turn(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, _calls, _events, waits = self._run(
                workspace,
                [
                    _strategist(2),
                    _busy(),
                    _busy(),
                    _worker("rescued", lemma=False),
                    _ok(),
                    _curator(),
                ],
                env={"AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "2"},
            )
            self.assertEqual(returncode, 0)
            self.assertEqual(waits, [60.0])
            stages = [call["stage"] for call in _receipt(workspace)["calls"]]
            self.assertEqual(stages[:4], ["strategist", "worker-1", "worker-2", "worker-3"])
            self.assertTrue(cgm_schema.canonical_call_order(stages))

    def test_no_rescue_after_an_ambiguous_or_repeated_worker_failure(self) -> None:
        for outputs in (
            [_strategist(1), _timed_out()],
            [_strategist(1), _busy(), _busy()],
        ):
            with self.subTest(outputs=len(outputs)), tempfile.TemporaryDirectory() as raw:
                workspace = Path(raw)
                returncode, calls, events, waits = self._run(workspace, outputs)
                self.assertEqual(returncode, 1)
                self.assertEqual(len(calls), len(outputs))
                self.assertEqual(waits, [] if len(outputs) == 2 else [60.0])
                terminal = [
                    e["item"] for e in events
                    if e.get("type") == "item.completed" and (e.get("item") or {}).get("terminal")
                ]
                self.assertIn("no worker produced a usable candidate", terminal[-1]["message"])
                self.assertFalse((workspace / "math_harness").exists())

    def _pipeline_error(
        self,
        outputs: list[object],
        *,
        env: dict[str, str] | None = None,
        waits: list[float] | None = None,
    ) -> runner.MathHarnessError:
        """The error a sponsored run fails with, given its replies in order."""
        queue = list(outputs)
        paused = waits if waits is not None else []

        def fake_api(system, user, **_kwargs):
            value = queue.pop(0)
            if isinstance(value, BaseException):
                raise value
            return SimpleNamespace(
                text=value, usage={"input_tokens": 7}, model="kimi-k3", provider="kimi"
            )

        with (
            tempfile.TemporaryDirectory() as raw,
            patch.dict(
                os.environ,
                {**_SPONSORED_ENV, "AGENT_MONITOR_CGM_CONCURRENCY": "1", **(env or {})},
                clear=True,
            ),
            patch.object(runner, "api_chat", side_effect=fake_api),
            patch.object(
                runner,
                "_wait_for_provider",
                side_effect=lambda _ctx, seconds: paused.append(seconds) or True,
            ),
        ):
            with self.assertRaises(runner.MathHarnessError) as raised:
                runner.run_pipeline(
                    Path(raw),
                    PROBLEM,
                    runner.Route("api", "kimi-k3", "api_chat"),
                    emit=lambda _event: None,
                )
        self.assertEqual(queue, [])
        return raised.exception

    def test_the_last_provider_failure_explains_a_failed_worker_stage(self) -> None:
        busy = _busy()
        error = self._pipeline_error([_strategist(1), _busy(), busy])
        self.assertIs(error.failure, busy.failure)
        # The strategist's reply was processed, so the run used tokens.
        self.assertTrue(error.billed)

    def test_a_run_refused_from_the_start_counts_as_unbilled(self) -> None:
        busy = _busy()
        # The strategist (sent again after each of its three waits), the
        # direct plan's worker and the rescue worker are all refused.
        error = self._pipeline_error([_busy()] * 5 + [busy])
        self.assertIs(error.failure, busy.failure)
        self.assertFalse(error.billed)

    def test_a_billed_run_never_says_no_tokens_in_its_error_text(self) -> None:
        # Worker 1's replies were billed but unusable; worker 2 was refused
        # unprocessed.  No single provider failure explains the run, so its
        # error carries the last worker's text, without the call-level claim.
        error = self._pipeline_error(
            [_strategist(2), "Not a worker reply.", "Still not one.", _busy()],
            env={"AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "2"},
        )
        self.assertIsNone(error.failure)
        self.assertIn(
            "worker-2 provider call failed: Kimi's provider is temporarily overloaded "
            "and refused the request (HTTP 503). Please try again in a few minutes.",
            str(error),
        )
        self.assertNotIn("No tokens were used", str(error))
        # A run nothing of which was processed keeps the claim.
        error = self._pipeline_error([_busy()] * 6)
        self.assertIn("No tokens were used", str(error))

    def test_a_failure_that_may_have_been_processed_explains_the_run_first(self) -> None:
        timed_out = _timed_out()
        error = self._pipeline_error(
            [_strategist(2), timed_out, _busy()],
            env={"AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "2"},
        )
        self.assertIs(error.failure, timed_out.failure)
        self.assertTrue(error.billed)

    def test_no_rescue_when_a_worker_turn_was_billed_before_its_refusal(self) -> None:
        empty = runner.APIBackendError(
            "Kimi returned an empty response for kimi-k3 "
            "(finish_reason=length, output_tokens=8192)",
            usage={"input_tokens": 900, "output_tokens": 8192},
        )
        # An empty, billed completion and then a refused low-effort retry; an
        # unparsable, billed reply and then a refused format reminder.
        for billed_first in (empty, "This is not a worker reply."):
            with self.subTest(first=type(billed_first).__name__):
                waits: list[float] = []
                error = self._pipeline_error(
                    [_strategist(1), billed_first, _busy()], waits=waits
                )
                # No rescue turn replays a plan the provider already billed.
                self.assertEqual(waits, [])
                self.assertTrue(error.billed)
                self.assertIn("no worker produced a usable candidate", str(error))

    def test_strategist_notes_claim_no_tokens_only_when_none_was_billed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            # The first strategist reply is billed but unusable; its format
            # reminder is refused through all three waits.
            returncode, _calls, events, waits = self._run(
                workspace,
                ["This is not a strategy."]
                + [_busy()] * 4
                + [_worker("direct", lemma=False), _ok(), _curator()],
            )
            self.assertEqual(returncode, 0)
            self.assertEqual(waits, [30.0, 60.0, 120.0])
            notes = self._notes(events)
            self.assertIn(
                "Math Harness · 2 Explore · strategist-retry · Kimi's provider is busy; "
                "retrying in 30 s",
                notes,
            )
            self.assertIn(
                "Math Harness · 2 Explore · the strategist call was refused "
                "(Kimi's provider is busy); using one direct plan",
                notes,
            )
            self.assertFalse(any("no tokens used" in note for note in notes))
            receipt = _receipt(workspace)
            # The billed but unusable first reply keeps its tokens.
            self.assertEqual(
                receipt["calls"][0]["usage"], {"input_tokens": 7, "output_tokens": 11}
            )

    def test_busy_notes_name_this_site_or_its_broker_when_they_refused(self) -> None:
        cases = (
            (
                _sponsored_failure(429, "gateway_busy", retry_after=2),
                "Kimi is busy on this site; retrying in 30 s (no tokens used)",
            ),
            (
                _sponsored_failure(429, "gateway_quota", retry_after=20),
                "Kimi is busy on this site; retrying in 30 s (no tokens used)",
            ),
            (
                runner.APIBackendError(
                    "unreachable", failure=kimi_k3.call_failure(0, "connection refused")
                ),
                "The included Kimi service could not be reached; retrying in 30 s "
                "(no tokens used)",
            ),
            (
                _sponsored_failure(502, "upstream_unavailable"),
                "Kimi's provider could not be reached; retrying in 30 s (no tokens used)",
            ),
        )
        for refused, expected in cases:
            with self.subTest(kind=refused.failure.kind or refused.failure.status):
                with tempfile.TemporaryDirectory() as raw:
                    returncode, _calls, events, waits = self._run(
                        Path(raw),
                        [refused, _strategist(1), _worker("only", lemma=False), _ok(), _curator()],
                    )
                self.assertEqual((returncode, waits), (0, [30.0]))
                self.assertIn(
                    f"Math Harness · 2 Explore · strategist · {expected}", self._notes(events)
                )

    def test_sponsored_calls_get_630_seconds_and_share_one_retry_allowance(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, calls, _events, _waits = self._run(
                workspace, [_strategist(1), _worker("only", lemma=False), _ok(), _curator()]
            )
            self.assertEqual(returncode, 0)
            self.assertEqual({call["timeout"] for call in calls}, {630})
            allowances = {id(call["retry_allowance"]) for call in calls}
            self.assertEqual(len(allowances), 1)
            self.assertIsInstance(calls[0]["retry_allowance"], kimi_k3.RetryAllowance)

        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            _code, calls, _events, _waits = self._run(
                workspace,
                [_strategist(1), _worker("only", lemma=False), _ok(), _curator()],
                env={"AGENT_MONITOR_CGM_API_TIMEOUT": "200"},
            )
            self.assertEqual({call["timeout"] for call in calls}, {200})

        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            _code, calls, _events = _api_run(
                workspace,
                [_strategist(1), _worker("only", lemma=False), _ok(), _curator()],
                env={"AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1"},
            )
            self.assertEqual({call["timeout"] for call in calls}, {300})
            self.assertEqual({call["retry_allowance"] for call in calls}, {None})

    def test_a_failed_but_billed_call_keeps_its_usage(self) -> None:
        empty = runner.APIBackendError(
            "Kimi returned an empty response for kimi-k3 "
            "(finish_reason=length, output_tokens=8192)",
            usage={"input_tokens": 900, "output_tokens": 8192},
        )
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, _calls, events = _api_run(
                workspace,
                [empty, _strategist(1), _worker("only", lemma=False), _ok(), _curator()],
                env={"AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1"},
            )
            self.assertEqual(returncode, 0)
            self.assertEqual(_usage_stages(events)[:2], ["strategist", "strategist-low"])
            receipt = _receipt(workspace)
            self.assertEqual(receipt["calls"][0]["stage"], "strategist")
            self.assertIsNone(receipt["calls"][0]["response_sha256"])
            self.assertEqual(
                receipt["calls"][0]["usage"], {"input_tokens": 900, "output_tokens": 8192}
            )


class MathHarnessProviderRoutingTests(unittest.TestCase):
    def test_codex_route_fails_closed_without_backend_isolation_contract(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            env = {
                "AGENT_MONITOR_CODEX_SUBSCRIPTION": "1",
                "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1",
            }
            with (
                _in_directory(workspace),
                patch.dict(runner.os.environ, env, clear=True),
                patch.object(runner.sys, "argv", ["runner", PROBLEM]),
                patch.object(runner, "codex_subscription_enabled", return_value=True),
                patch.object(runner, "claude_subscription_enabled", return_value=False),
                patch.object(
                    runner._codex_backend,
                    "RESPONSE_ONLY_TOOL_ISOLATION_VERSION",
                    0,
                ),
                patch.object(runner, "codex_exec") as codex,
                patch.object(runner, "api_chat") as api,
                patch.object(runner, "emit"),
            ):
                self.assertEqual(runner.main(), 1)
            codex.assert_not_called()
            api.assert_not_called()
            self.assertEqual(list(workspace.iterdir()), [])

    def test_codex_backend_command_is_tool_free_and_workspace_isolated(self) -> None:
        captured: dict[str, object] = {}

        def fake_process(command, *, input, timeout, env, cwd, on_stdout_line=None):
            if command[1:] == ["--version"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    runner._codex_backend.RESPONSE_ONLY_CODEX_CLI_VERSION + "\n",
                    "",
                )
            if command[1:] == ["debug", "models", "--bundled"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps(
                        {
                            "models": [
                                {
                                    "slug": "gpt-5.6-sol",
                                    "apply_patch_tool_type": "freeform",
                                    "web_search_tool_type": "web_search",
                                    "supports_search_tool": True,
                                }
                            ]
                        }
                    ),
                    "",
                )
            captured.update(
                command=list(command),
                input=input,
                timeout=timeout,
                env=dict(env),
                cwd=cwd,
            )
            catalog_setting = next(
                item
                for item in command
                if item.startswith("model_catalog_json=")
            )
            catalog_path = Path(json.loads(catalog_setting.split("=", 1)[1]))
            captured["catalog"] = json.loads(catalog_path.read_text(encoding="utf-8"))
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("response", encoding="utf-8")
            return subprocess.CompletedProcess(
                command,
                0,
                '{"type":"turn.completed","usage":{"input_tokens":3}}\n',
                "",
            )

        with (
            tempfile.TemporaryDirectory() as raw,
            tempfile.TemporaryDirectory() as home_raw,
            patch.object(
                runner._codex_backend.shutil,
                "which",
                return_value=str(Path(home_raw) / "codex"),
            ),
            patch.object(
                runner._codex_backend,
                "_run_codex_process",
                side_effect=fake_process,
            ),
        ):
            workspace = Path(raw)
            launcher = Path(home_raw) / "codex"
            launcher.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
            launcher.chmod(0o755)
            result = runner._codex_backend.codex_exec(
                "system",
                "user",
                model="gpt-5.6-sol",
                workspace=workspace,
                sandbox="read-only",
                enable_tools=False,
                source_env={
                    "CODEX_HOME": home_raw,
                    "PATH": os.defpath,
                    "AGENT_MONITOR_CODEX_LEGACY_LANDLOCK": "0",
                },
            )
        self.assertEqual(result.text, "response")
        command = captured["command"]
        self.assertIsInstance(command, list)
        assert isinstance(command, list)
        self.assertEqual(
            runner._codex_backend.RESPONSE_ONLY_TOOL_ISOLATION_VERSION,
            5,
        )
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("--strict-config", command)
        self.assertIn("--ephemeral", command)
        for capability in (
            "apps",
            "artifact",
            "auth_elicitation",
            "browser_use",
            "browser_use_external",
            "browser_use_full_cdp_access",
            "code_mode",
            "code_mode_host",
            "computer_use",
            "goals",
            "hooks",
            "image_generation",
            "in_app_browser",
            "memories",
            "multi_agent",
            "network_proxy",
            "plugins",
            "remote_plugin",
            "request_permissions_tool",
            "skill_mcp_dependency_install",
            "skill_search",
            "standalone_web_search",
            "tool_call_mcp_elicitation",
            "tool_suggest",
            "view_image",
            "workspace_dependencies",
            "shell_tool",
            "unified_exec",
        ):
            self.assertTrue(
                any(
                    command[index : index + 2] == ["--disable", capability]
                    for index in range(len(command) - 1)
                ),
                capability,
            )
        self.assertIn('web_search="disabled"', command)
        self.assertIn("tools.web_search=false", command)
        self.assertIn("tools.update_plan.enabled=false", command)
        self.assertIn(
            "tools.experimental_request_user_input.enabled=false",
            command,
        )
        catalog = captured["catalog"]
        self.assertIsInstance(catalog, dict)
        assert isinstance(catalog, dict)
        self.assertEqual(len(catalog["models"]), 1)
        catalog_model = catalog["models"][0]
        self.assertNotIn("apply_patch_tool_type", catalog_model)
        self.assertNotIn("web_search_tool_type", catalog_model)
        self.assertIs(catalog_model["supports_search_tool"], False)
        self.assertIs(catalog_model["supports_parallel_tool_calls"], False)
        self.assertNotIn("--cd", command)
        self.assertNotEqual(Path(str(captured["cwd"])), workspace.resolve())
        self.assertTrue(Path(str(captured["cwd"])).name.startswith("agent-monitor-codex-"))

    def test_codex_route_is_read_only_tool_free_and_never_falls_back(self) -> None:
        outputs = [_strategist(1), _worker("codex", lemma=False), _ok(), _curator()]
        calls: list[dict] = []
        events: list[dict] = []

        def fake_codex(system: str, user: str, **kwargs):
            calls.append({"system": system, "user": user, **kwargs})
            kwargs["emit_event"](
                {"type": "item.completed", "item": {"type": "reasoning", "text": "native"}}
            )
            return SimpleNamespace(text=outputs.pop(0), usage={"input_tokens": 1})

        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            env = {
                "AGENT_MONITOR_CODEX_SUBSCRIPTION": "1",
                "AGENT_MONITOR_CODEX_MODEL": "gpt-5.6-sol",
                "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1",
            }
            with (
                _in_directory(workspace),
                patch.dict(runner.os.environ, env, clear=True),
                patch.object(runner.sys, "argv", ["runner", PROBLEM]),
                patch.object(runner, "codex_subscription_enabled", return_value=True),
                patch.object(runner, "claude_subscription_enabled", return_value=False),
                patch.object(runner, "codex_exec", side_effect=fake_codex),
                patch.object(runner, "claude_code_exec") as claude,
                patch.object(runner, "api_chat") as api,
                patch.object(runner, "emit", side_effect=events.append),
            ):
                self.assertEqual(runner.main(), 0)
            self.assertEqual(len(calls), 4)
            for call in calls:
                self.assertEqual(call["sandbox"], "read-only")
                self.assertIs(call["enable_tools"], False)
                # A read-only snapshot of the run environment, taken once per
                # run (concurrent turns never share the mutable os.environ).
                self.assertEqual(call["source_env"], env)
                self.assertIsNot(call["source_env"], runner.os.environ)
                self.assertEqual(call["model"], "gpt-5.6-sol")
            claude.assert_not_called()
            api.assert_not_called()
            receipt = _receipt(workspace)
            self.assertEqual(receipt["provider"], "codex_subscription")
            self.assertEqual(receipt["verdict"], "accepted")
            self.assertIsNone(receipt["calls"][0]["effort"])
            self.assertEqual(
                receipt["workflow"]["response_only_contract"],
                {"transport": "codex_exec", "isolation_version": 5},
            )
            self.assertTrue(any((event.get("item") or {}).get("text") == "native" for event in events))
            self.assertEqual(
                engine_graph.build(workspace, {"engine": "math_harness"})["provenance"][
                    "response_only_isolation_version"
                ],
                5,
            )

    def test_claude_route_is_single_turn_tool_free_and_projects_events(self) -> None:
        outputs = [_strategist(1), _worker("claude", lemma=False), _ok(), _curator()]
        calls: list[dict] = []
        events: list[dict] = []

        def fake_claude(system: str, user: str, **kwargs):
            calls.append({"system": system, "user": user, **kwargs})
            kwargs["emit_event"](
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "claude native"}]},
                }
            )
            return SimpleNamespace(
                text=outputs.pop(0),
                usage={"output_tokens": 2},
                model="claude-sonnet-resolved",
            )

        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            env = {
                "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
                "AGENT_MONITOR_CLAUDE_MODEL": "sonnet",
                "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1",
            }
            with (
                _in_directory(workspace),
                patch.dict(runner.os.environ, env, clear=True),
                patch.object(runner.sys, "argv", ["runner", PROBLEM]),
                patch.object(runner, "codex_subscription_enabled", return_value=False),
                patch.object(runner, "claude_subscription_enabled", return_value=True),
                patch.object(runner, "claude_code_exec", side_effect=fake_claude),
                patch.object(runner, "codex_exec") as codex,
                patch.object(runner, "api_chat") as api,
                patch.object(runner, "emit", side_effect=events.append),
            ):
                self.assertEqual(runner.main(), 0)
            self.assertEqual(len(calls), 4)
            for call, expected_timeout in zip(calls, (600.0, 1200.0, 600.0, 600.0)):
                self.assertIs(call["enable_tools"], False)
                self.assertEqual(call["max_turns"], 1)
                self.assertEqual(call["timeout"], expected_timeout)
                # A read-only snapshot of the run environment, taken once per
                # run (concurrent turns never share the mutable os.environ).
                self.assertEqual(call["source_env"], env)
                self.assertIsNot(call["source_env"], runner.os.environ)
                self.assertNotIn("workspace", call)
            codex.assert_not_called()
            api.assert_not_called()
            receipt = _receipt(workspace)
            self.assertEqual(receipt["provider"], "claude_subscription")
            self.assertEqual(receipt["model"], "claude-sonnet-resolved")
            self.assertEqual(
                receipt["workflow"]["response_only_contract"],
                {"transport": "claude_code", "isolation_version": 1},
            )
            self.assertEqual(
                runner._claude_backend.RESPONSE_ONLY_TOOL_ISOLATION_VERSION,
                1,
            )
            self.assertTrue(
                any((event.get("item") or {}).get("text") == "claude native" for event in events)
            )

    def test_claude_failure_preserves_completed_stage_usage_without_retry(self) -> None:
        calls: list[dict] = []
        events: list[dict] = []

        def fake_claude(system: str, user: str, **kwargs):
            calls.append({"system": system, "user": user, **kwargs})
            if len(calls) == 1:
                return SimpleNamespace(
                    text=_strategist(1),
                    usage={"input_tokens": 13, "output_tokens": 5},
                    model="claude-fable-resolved",
                )
            raise runner.ClaudeBackendError("Claude Code timed out after 1200s")

        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            env = {
                "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
                "AGENT_MONITOR_CLAUDE_MODEL": "fable",
                "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1",
            }
            with (
                _in_directory(workspace),
                patch.dict(runner.os.environ, env, clear=True),
                patch.object(runner.sys, "argv", ["runner", PROBLEM]),
                patch.object(runner, "codex_subscription_enabled", return_value=False),
                patch.object(runner, "claude_subscription_enabled", return_value=True),
                patch.object(runner, "claude_code_exec", side_effect=fake_claude),
                patch.object(runner, "emit", side_effect=events.append),
            ):
                self.assertEqual(runner.main(), 1)

            self.assertEqual(len(calls), 2, "a timed-out stage is never replayed")
            self.assertEqual([call["timeout"] for call in calls], [600.0, 1200.0])
            usage_events = [event for event in events if event.get("type") == "turn.completed"]
            self.assertEqual(
                usage_events,
                [
                    {
                        "type": "turn.completed",
                        "stage": "strategist",
                        "usage": {
                            "input_tokens": 13,
                            "output_tokens": 5,
                            "cached_input_tokens": 0,
                            # Claude usage keeps its cache apart from input;
                            # the server parser folds it in (Codex semantics).
                            "input_excludes_cache": True,
                        },
                    }
                ],
            )
            self.assertFalse((workspace / "proof.md").exists())

    def test_claude_stage_timeout_override_precedence_and_bounds(self) -> None:
        self.assertEqual(runner._claude_stage_timeout("candidate-2", {}), 1200.0)
        self.assertEqual(runner._claude_stage_timeout("worker-2", {}), 1200.0)
        self.assertEqual(runner._claude_stage_timeout("repair-1-retry", {}), 1200.0)
        self.assertEqual(runner._claude_stage_timeout("critic", {}), 600.0)
        self.assertEqual(runner._claude_stage_timeout("verify-4", {}), 600.0)
        self.assertEqual(runner._claude_stage_timeout("strategist", {}), 600.0)
        self.assertEqual(
            runner._claude_stage_timeout(
                "worker-2",
                {
                    "AGENT_MONITOR_CLAUDE_TIMEOUT": "700",
                    "AGENT_MONITOR_MATH_HARNESS_CLAUDE_TIMEOUT": "900",
                    "AGENT_MONITOR_MATH_HARNESS_CLAUDE_CANDIDATE_TIMEOUT": "1500",
                },
            ),
            1500.0,
        )
        self.assertEqual(
            runner._claude_stage_timeout(
                "planner", {"AGENT_MONITOR_MATH_HARNESS_CLAUDE_TIMEOUT": "1"}
            ),
            60.0,
        )
        self.assertEqual(
            runner._claude_stage_timeout(
                "revision", {"AGENT_MONITOR_CLAUDE_TIMEOUT": "99999"}
            ),
            3600.0,
        )
        with self.assertRaisesRegex(runner.MathHarnessError, "must be a number"):
            runner._claude_stage_timeout(
                "planner", {"AGENT_MONITOR_CLAUDE_TIMEOUT": "invalid"}
            )
        budget = runner.claude_pipeline_budget_seconds({})
        self.assertEqual(budget, min(16 * 1200, 5400) + 300)

    def test_invalid_claude_timeout_fails_precisely_before_provider_call(self) -> None:
        events: list[dict] = []
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            env = {
                "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
                "AGENT_MONITOR_CLAUDE_MODEL": "fable",
                "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1",
                "AGENT_MONITOR_MATH_HARNESS_CLAUDE_TIMEOUT": "invalid",
            }
            with (
                _in_directory(workspace),
                patch.dict(runner.os.environ, env, clear=True),
                patch.object(runner.sys, "argv", ["runner", PROBLEM]),
                patch.object(runner, "codex_subscription_enabled", return_value=False),
                patch.object(runner, "claude_subscription_enabled", return_value=True),
                patch.object(runner, "claude_code_exec") as claude,
                patch.object(runner, "emit", side_effect=events.append),
            ):
                self.assertEqual(runner.main(), 1)

        claude.assert_not_called()
        terminal = [
            event["item"]
            for event in events
            if event.get("type") == "item.completed"
            and (event.get("item") or {}).get("terminal") is True
        ]
        self.assertEqual(len(terminal), 1)
        self.assertIn("must be a number", terminal[0]["message"])

    def test_claude_route_fails_closed_without_backend_isolation_contract(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            env = {
                "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
                "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1",
            }
            with (
                _in_directory(workspace),
                patch.dict(runner.os.environ, env, clear=True),
                patch.object(runner.sys, "argv", ["runner", PROBLEM]),
                patch.object(runner, "codex_subscription_enabled", return_value=False),
                patch.object(runner, "claude_subscription_enabled", return_value=True),
                patch.object(
                    runner._claude_backend,
                    "RESPONSE_ONLY_TOOL_ISOLATION_VERSION",
                    0,
                ),
                patch.object(runner, "claude_code_exec") as claude,
                patch.object(runner, "api_chat") as api,
                patch.object(runner, "emit"),
            ):
                self.assertEqual(runner.main(), 1)
            claude.assert_not_called()
            api.assert_not_called()
            self.assertEqual(list(workspace.iterdir()), [])

    def test_codex_error_does_not_fallback_to_api_or_claude(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            env = {
                "AGENT_MONITOR_CODEX_SUBSCRIPTION": "1",
                "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1",
            }
            with (
                _in_directory(workspace),
                patch.dict(runner.os.environ, env, clear=True),
                patch.object(runner.sys, "argv", ["runner", PROBLEM]),
                patch.object(runner, "codex_subscription_enabled", return_value=True),
                patch.object(runner, "claude_subscription_enabled", return_value=False),
                patch.object(
                    runner,
                    "codex_exec",
                    side_effect=runner.CodexBackendError("account unavailable"),
                ) as codex,
                patch.object(runner, "claude_code_exec") as claude,
                patch.object(runner, "api_chat") as api,
                patch.object(runner, "emit"),
            ):
                self.assertEqual(runner.main(), 1)
            codex.assert_called_once()
            claude.assert_not_called()
            api.assert_not_called()
            self.assertEqual(list(workspace.iterdir()), [])

    def test_conflicting_subscription_routes_fail_before_backend_call(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            with (
                _in_directory(workspace),
                patch.object(runner.sys, "argv", ["runner", PROBLEM]),
                patch.object(runner, "codex_subscription_enabled", return_value=True),
                patch.object(runner, "claude_subscription_enabled", return_value=True),
                patch.object(runner, "codex_exec") as codex,
                patch.object(runner, "claude_code_exec") as claude,
                patch.object(runner, "api_chat") as api,
                patch.object(runner, "emit"),
            ):
                self.assertEqual(runner.main(), 1)
            codex.assert_not_called()
            claude.assert_not_called()
            api.assert_not_called()


class MathHarnessBoundsAndSecurityTests(unittest.TestCase):
    def test_candidate_count_is_bounded_by_workers_and_iterations(self) -> None:
        self.assertEqual(
            runner.candidate_count(
                {
                    "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "99",
                    "AGENT_MONITOR_MAX_ITERATIONS": "2",
                }
            ),
            2,
        )
        self.assertEqual(
            runner.candidate_count(
                {
                    "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "3",
                    "AGENT_MONITOR_MAX_ITERATIONS": "0",
                }
            ),
            1,
        )
        self.assertEqual(
            runner.candidate_count({"MATH_HARNESS_MAX_ITERATIONS": "1"}),
            1,
        )
        self.assertEqual(runner.candidate_count({}), 3)
        self.assertEqual(runner.candidate_count({"AGENT_MONITOR_GUEST": "1"}), 2)
        self.assertEqual(
            runner.candidate_count({"AGENT_MONITOR_CGM_MAX_WORKERS": "1"}), 1
        )
        with self.assertRaises(runner.MathHarnessError):
            runner.candidate_count(
                {"AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "many"}
            )

    def test_three_workers_are_independent_and_prompts_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, calls, _events = _api_run(
                workspace,
                [
                    _strategist(3),
                    _worker("unique-A"),
                    _worker("unique-B"),
                    _worker("unique-C"),
                    _ok(),
                    _ok(),
                    _curator(),
                ],
                env={
                    "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "500",
                    "AGENT_MONITOR_MAX_ITERATIONS": "3",
                },
            )
            self.assertEqual(returncode, 0)
            self.assertEqual(len(calls), 7)
            self.assertTrue(
                all(
                    len(call["user"].encode("utf-8")) <= runner.MAX_MODEL_PROMPT_BYTES
                    and len(call["system"].encode("utf-8")) <= runner.MAX_SYSTEM_PROMPT_BYTES
                    for call in calls
                )
            )
            worker_calls = calls[1:4]
            self.assertIn("Label: Route 1", worker_calls[0]["user"])
            self.assertNotIn("Label: Route 2", worker_calls[0]["user"])
            self.assertNotIn("unique-A", worker_calls[1]["user"])
            self.assertNotIn("unique-B", worker_calls[2]["user"])
            for verifier_call in calls[4:6]:
                self.assertNotIn("unique-B", verifier_call["user"])
                self.assertNotIn("unique-C", verifier_call["user"])
            receipt = _receipt(workspace)
            self.assertEqual(receipt["workflow"]["calls"]["worker"], 3)
            self.assertEqual(len(receipt["calls"]), 7)

    def test_static_symlink_attacks_fail_before_any_model_call(self) -> None:
        for target_name in ("math_harness", "proof.md", "problem.txt"):
            with self.subTest(target=target_name), tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as outside_raw:
                workspace = Path(raw)
                outside = Path(outside_raw)
                sentinel = outside / "sentinel"
                sentinel.write_text("unchanged", encoding="utf-8")
                target = workspace / target_name
                if target_name == "math_harness":
                    target.symlink_to(outside, target_is_directory=True)
                else:
                    target.symlink_to(sentinel)
                calls: list[object] = []
                with (
                    _in_directory(workspace),
                    patch.object(runner.sys, "argv", ["runner", PROBLEM]),
                    patch.object(runner, "codex_subscription_enabled", return_value=False),
                    patch.object(runner, "claude_subscription_enabled", return_value=False),
                    patch.object(runner, "api_chat", side_effect=lambda *a, **k: calls.append(a)),
                    patch.object(runner, "emit"),
                ):
                    self.assertEqual(runner.main(), 1)
                self.assertEqual(calls, [])
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")

    def test_symlinked_nested_memory_and_fact_directories_are_rejected(self) -> None:
        for branch in ("global_memory", "fact_graph"):
            with self.subTest(branch=branch), tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as outside_raw:
                workspace = Path(raw)
                project = workspace / "math_harness" / "project"
                project.mkdir(parents=True)
                (project / branch).symlink_to(Path(outside_raw), target_is_directory=True)
                calls: list[object] = []
                with (
                    _in_directory(workspace),
                    patch.object(runner.sys, "argv", ["runner", PROBLEM]),
                    patch.object(runner, "codex_subscription_enabled", return_value=False),
                    patch.object(runner, "claude_subscription_enabled", return_value=False),
                    patch.object(runner, "api_chat", side_effect=lambda *a, **k: calls.append(a)),
                    patch.object(runner, "emit"),
                ):
                    self.assertEqual(runner.main(), 1)
                self.assertEqual(calls, [])

    def test_symlinked_optional_memory_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as outside_raw:
            workspace = Path(raw)
            memory = workspace / "math_harness" / "project" / "global_memory"
            memory.mkdir(parents=True)
            sentinel = Path(outside_raw) / "sentinel"
            sentinel.write_text("unchanged", encoding="utf-8")
            (memory / "dead_end.jsonl").symlink_to(sentinel)
            returncode, calls, _events = _api_run(workspace, _accepted_outputs())
            self.assertEqual(returncode, 1)
            self.assertEqual(calls, [])
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")

    def test_unexpected_fact_entry_fails_before_any_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            facts = workspace / "math_harness" / "project" / "fact_graph" / "facts"
            facts.mkdir(parents=True)
            (facts / "README.md").write_text("user material", encoding="utf-8")
            returncode, calls, _events = _api_run(workspace, _accepted_outputs())
            self.assertEqual(returncode, 1)
            self.assertEqual(calls, [])
            self.assertEqual(
                (facts / "README.md").read_text(encoding="utf-8"),
                "user material",
            )

    def test_interrupted_adapter_fact_temp_is_cleaned_before_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            facts = workspace / "math_harness" / "project" / "fact_graph" / "facts"
            facts.mkdir(parents=True)
            orphan = facts / (
                ".0123456789abcdef.md.tmp-"
                "0123456789abcdef01234567"
            )
            orphan.write_bytes(b"interrupted adapter write")
            orphan.chmod(0o600)
            returncode, calls, _events = _api_run(workspace, _accepted_outputs())
            self.assertEqual(returncode, 0)
            self.assertEqual(len(calls), 6)
            self.assertFalse(orphan.exists())
            graph = engine_graph.build(workspace, {"engine": "math_harness"})
            self.assertEqual(graph["status"], "complete")
            self.assertEqual(
                graph["provenance"]["attestation"],
                "harness-gate-receipt",
            )

    def test_corrupt_prior_fact_is_not_classified_as_adapter_owned(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            first, _calls, _events = _api_run(workspace, _accepted_outputs())
            self.assertEqual(first, 0)
            fact = sorted(
                (workspace / "math_harness" / "project" / "fact_graph" / "facts").glob("*.md")
            )[0]
            original = fact.read_bytes()
            fact.write_bytes(original + b"\ncorruption\n")
            second, calls, _events = _api_run(workspace, _accepted_outputs())
            self.assertEqual(second, 1)
            self.assertEqual(calls, [])
            self.assertEqual(fact.read_bytes(), original + b"\ncorruption\n")

    def test_prior_receipt_with_wrong_isolation_contract_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            first, _calls, _events = _api_run(workspace, _accepted_outputs())
            self.assertEqual(first, 0)
            receipt_path = workspace / "math_harness" / "project" / "engine_receipt.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["workflow"]["response_only_contract"]["isolation_version"] = 99
            receipt["binding_sha256"] = cgm_schema.receipt_binding_digest(receipt)
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            second, calls, _events = _api_run(workspace, _accepted_outputs())
            self.assertEqual(second, 1)
            self.assertEqual(calls, [])
            self.assertEqual(
                json.loads(receipt_path.read_text())["workflow"][
                    "response_only_contract"
                ]["isolation_version"],
                99,
            )

    def test_prior_receipt_with_broken_binding_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            first, _calls, _events = _api_run(workspace, _accepted_outputs())
            self.assertEqual(first, 0)
            receipt_path = workspace / "math_harness" / "project" / "engine_receipt.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["model"] = "some-other-model"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            second, calls, _events = _api_run(workspace, _accepted_outputs())
            self.assertEqual(second, 1)
            self.assertEqual(calls, [])

    def test_atomic_write_stays_on_open_parent_during_directory_swap(self) -> None:
        with (
            tempfile.TemporaryDirectory() as raw,
            tempfile.TemporaryDirectory() as outside_raw,
        ):
            workspace = Path(raw)
            project = workspace / "math_harness" / "project"
            project.mkdir(parents=True)
            moved = workspace / "math_harness" / "project-original"
            outside = Path(outside_raw)
            real_replace = os.replace
            swapped = False

            def swap_then_replace(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    os.rename(project, moved)
                    project.symlink_to(outside, target_is_directory=True)
                return real_replace(
                    src,
                    dst,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )

            with patch.object(runner.os, "replace", side_effect=swap_then_replace):
                runner._atomic_write(
                    workspace,
                    ("math_harness", "project", "PROBLEM.md"),
                    b"safe bytes",
                )
            self.assertFalse((outside / "PROBLEM.md").exists())
            self.assertEqual((moved / "PROBLEM.md").read_bytes(), b"safe bytes")

    def test_remove_regular_stays_on_open_parent_during_directory_swap(self) -> None:
        with (
            tempfile.TemporaryDirectory() as raw,
            tempfile.TemporaryDirectory() as outside_raw,
        ):
            workspace = Path(raw)
            project = workspace / "math_harness" / "project"
            project.mkdir(parents=True)
            (project / "TARGET.md").write_text("generated", encoding="utf-8")
            moved = workspace / "math_harness" / "project-original"
            outside = Path(outside_raw)
            outside_target = outside / "TARGET.md"
            outside_target.write_text("sentinel", encoding="utf-8")
            real_unlink = os.unlink
            swapped = False

            def swap_then_unlink(path, *, dir_fd=None):
                nonlocal swapped
                if not swapped and path == "TARGET.md":
                    swapped = True
                    os.rename(project, moved)
                    project.symlink_to(outside, target_is_directory=True)
                return real_unlink(path, dir_fd=dir_fd)

            with patch.object(runner.os, "unlink", side_effect=swap_then_unlink):
                runner._remove_regular(
                    workspace,
                    ("math_harness", "project", "TARGET.md"),
                )
            self.assertEqual(outside_target.read_text(encoding="utf-8"), "sentinel")
            self.assertFalse((moved / "TARGET.md").exists())

    def test_prior_fact_scan_rejects_a_symlinked_project(self) -> None:
        with (
            tempfile.TemporaryDirectory() as raw,
            tempfile.TemporaryDirectory() as outside_raw,
        ):
            workspace = Path(raw)
            (workspace / "math_harness").mkdir()
            (workspace / "math_harness" / "project").symlink_to(
                Path(outside_raw),
                target_is_directory=True,
            )
            with self.assertRaises(runner.MathHarnessError):
                runner._prior_generated_fact_ids(workspace)

    def test_runner_never_imports_archive_or_invokes_processes(self) -> None:
        source = inspect.getsource(runner)
        self.assertNotIn("from danus", source)
        self.assertNotIn("import danus", source)
        self.assertNotIn("Math_Agent_Harness-main", source)
        self.assertNotIn("import cgm", source)
        self.assertNotIn("from cgm", source)
        self.assertNotIn("import subprocess", source)
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            with (
                patch.object(subprocess, "Popen") as popen,
                patch.object(subprocess, "run") as process_run,
            ):
                returncode, _calls, _events = _api_run(workspace, _accepted_outputs())
            self.assertEqual(returncode, 0)
            popen.assert_not_called()
            process_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
