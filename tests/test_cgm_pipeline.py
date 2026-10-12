"""End-to-end CGM pipeline artifacts and their fail-closed engine-graph validation."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_monitor import engine_graph
from agent_monitor.cgm import closure as cgm_closure
from agent_monitor.cgm import curation as cgm_curation
from agent_monitor.cgm import parsing as cgm_parsing
from agent_monitor.cgm import schema as cgm_schema
from agent_monitor.runners import math_harness_runner as runner


PROBLEM = "Prove that for every natural number n, 1 + 3 + ... + (2n-1) = n^2."

STRATEGY = json.dumps(
    {
        "immediate_conclusions": [
            {"statement": "For n = 0 the sum is empty.", "justification": "Definition."}
        ],
        "obstacles": ["Index shifts are easy to get wrong."],
        "plans": [
            {
                "label": "Telescoping",
                "technique": "reduction",
                "approach": "Reduce to the telescoping identity k^2 - (k-1)^2 = 2k - 1.",
                "subgoals": [
                    {"id": "S1", "statement": "k^2 - (k-1)^2 = 2k - 1 for k >= 1."},
                    {
                        "id": "S2",
                        "statement": "The partial sums telescope.",
                        "depends_on": ["S1"],
                    },
                ],
                "risks": "Off-by-one errors.",
            },
            {
                "label": "Induction",
                "technique": "plain",
                "approach": "Induct on n.",
                "subgoals": [],
                "risks": "Base case.",
            },
        ],
    }
)

WORKER = """=== LEMMA L1 ===
Subgoal: S1
Uses: none
Statement:
For every integer k >= 1, k^2 - (k-1)^2 = 2k - 1.
Proof:
Expanding, (k-1)^2 = k^2 - 2k + 1, hence k^2 - (k-1)^2 = 2k - 1.
=== END ===

=== LEMMA L2 ===
Subgoal: S2
Uses: L1
Statement:
For every integer n >= 1, the sum of 2k - 1 over k = 1..n equals n^2.
Proof:
By L1: 2k - 1 = k^2 - (k-1)^2 for each k >= 1, so the sum telescopes to n^2 - 0^2 = n^2.
=== END ===

=== THEOREM ===
Uses: L2
Statement:
(the problem)
Proof:
For n = 0 both sides are 0. For n >= 1, by L2 the sum equals n^2.
=== END ===

=== FINDING obstacle ===
Claim: Pairing first and last terms needs parity cases.
Evidence:
For odd n the middle term is unpaired.
=== END ===

=== STATUS ===
solved
=== END ==="""

OK = json.dumps(
    {
        "verification_report": {"summary": "Sound.", "critical_errors": [], "gaps": []},
        "verdict": "correct",
        "repair_hints": "",
    }
)
# Aliases follow the snapshot order: N1 root, N2 plan 1, N3-N4 its subgoals,
# N5 plan 2.
CURATOR = json.dumps(
    {"annotations": [{"node": "N5", "status": "active", "score": 0.9, "comment": "Strong (N1)."}]}
)


def _run(
    workspace: Path,
    outputs: list[object],
    *,
    env: dict[str, str] | None = None,
    problem: str = PROBLEM,
    **pipeline_kwargs,
):
    queue = list(outputs)
    events: list[dict] = []
    calls: list[dict] = []

    def fake_api(system: str, user: str, *, model: str, timeout: int = 240, **kwargs):
        calls.append({"system": system, "user": user, "effort": kwargs.get("effort")})
        value = queue.pop(0)
        if isinstance(value, BaseException):
            raise value
        return SimpleNamespace(
            text=value, usage={"input_tokens": 3, "output_tokens": 4}, model=model, provider="kimi"
        )

    # The queued replies are handed out in call order, which is only defined
    # one call at a time; order-independent concurrency is covered by
    # tests/test_cgm_concurrency.py.
    run_env = {
        "AGENT_MONITOR_PROBLEM_ID": "odd-sum",
        "AGENT_MONITOR_CGM_CONCURRENCY": "1",
        **(env or {}),
    }
    with (
        patch.dict(os.environ, run_env, clear=True),
        patch.object(runner, "api_chat", side_effect=fake_api),
    ):
        result = runner.run_pipeline(
            workspace,
            problem,
            runner.Route("api", "kimi-k3", "api_chat"),
            emit=events.append,
            **pipeline_kwargs,
        )
    return result, calls, events


def _accepted(workspace: Path):
    return _run(workspace, [STRATEGY, WORKER, WORKER.replace("Pairing", "Grouping"), OK, OK, OK, CURATOR])


def _rewrite_receipt(workspace: Path, mutate) -> None:
    path = workspace / "math_harness" / "project" / "engine_receipt.json"
    receipt = json.loads(path.read_text())
    mutate(receipt)
    receipt["binding_sha256"] = cgm_schema.receipt_binding_digest(receipt)
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")


class RunPipelineEntryPointTests(unittest.TestCase):
    def test_run_pipeline_returns_result_and_emits_through_callback(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch.object(runner, "emit") as stdout_emit:
            workspace = Path(raw)
            result, calls, events = _accepted(workspace)
            stdout_emit.assert_not_called()
        self.assertEqual(result["status"], "accepted")
        self.assertTrue(result["accepted"])
        self.assertEqual(result["calls"], 7)
        self.assertEqual(len(calls), 7)
        self.assertEqual(len(result["fact_ids"]), 3)
        self.assertEqual(result["target_fact_id"], result["fact_ids"][-1])
        self.assertEqual(result["stages"], list(cgm_schema.STAGE_ORDER))
        self.assertEqual([v["verdict"] for v in result["verdicts"]], ["correct"] * 3)
        self.assertIn("proof.md", result["written"])
        self.assertEqual(result["written"][-1], "math_harness/project/engine_receipt.json")
        self.assertTrue(result["proof_text"].startswith("# Proof\n"))
        self.assertEqual(
            [event["label"] for event in events if event.get("type") == "pipeline.stage"],
            ["1 Scaffold", "2 Explore", "3 Verify", "4 Curate", "5 Synthesize"],
        )

    def test_multi_lemma_dag_facts_and_explore_views(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            result, _calls, _events = _accepted(workspace)
            dag = engine_graph.load_fact_dag(workspace)
            assert dag is not None
            self.assertEqual(
                [set(item) for item in dag],
                [{"fact_id", "statement", "proof", "predecessors", "is_target", "attested"}] * 3,
            )
            lemma1, lemma2, target = dag
            self.assertEqual(lemma1["predecessors"], [])
            self.assertEqual(lemma2["predecessors"], [lemma1["fact_id"]])
            self.assertEqual(target["predecessors"], [lemma2["fact_id"]])
            self.assertEqual([item["is_target"] for item in dag], [False, False, True])
            self.assertTrue(all(item["attested"] for item in dag))
            self.assertEqual(target["statement"], PROBLEM)
            self.assertTrue(lemma2["proof"].startswith(f"Cited facts: L1 = fact {lemma1['fact_id']}."))
            self.assertEqual(
                (workspace / "proof.md").read_text(),
                engine_graph.render_fact_closure(dag, target["fact_id"]),
            )
            self.assertEqual(result["proof_text"], (workspace / "proof.md").read_text())

            facts = engine_graph.build(workspace, {"engine": "math_harness"})
            self.assertEqual(facts["provenance"]["attestation"], "harness-gate-receipt")
            self.assertEqual(facts["provenance"]["pipeline"], "cgm")
            self.assertEqual(
                [node["kind"] for node in facts["graph"]["nodes"]],
                ["lemma", "lemma", "conclusion"],
            )
            self.assertEqual(
                [node["meta"]["label"] for node in facts["graph"]["nodes"]],
                ["L1", "L2", "THEOREM"],
            )
            self.assertEqual(len(facts["graph"]["edges"]), 2)

            workflow = engine_graph.build(workspace, {"engine": "math_harness"}, view="workflow")
            self.assertEqual(workflow["provenance"]["adapter"], "cgm-exploring-graph@1")
            self.assertIs(workflow["provenance"]["certificate"], False)
            self.assertIs(workflow["provenance"]["lean_verified"], False)
            kinds = {node["meta"]["workflow_kind"] for node in workflow["graph"]["nodes"]}
            self.assertEqual(
                kinds,
                {"goal", "plan", "proof_attempt", "verification", "fact", "conclusion", "obstacle"},
            )
            for node in workflow["graph"]["nodes"]:
                self.assertIn(node["kind"], engine_graph._ALLOWED_NODE_KINDS)
            labels = {edge["label"] for edge in workflow["graph"]["edges"]}
            self.assertTrue(
                {
                    "addresses", "decomposes-into", "depends-on", "reduces-to",
                    "alternative-of", "refines", "promoted-to", "supports", "blocks",
                    "verifier-input", "verifier-accepted",
                }.issubset(labels)
            )
            by_id = {node["id"]: node for node in workflow["graph"]["nodes"]}
            self.assertEqual(by_id["goal:root"]["meta"]["status"], "closed")
            self.assertEqual(by_id["goal:root"]["meta"]["score"], 1.0)
            self.assertEqual(by_id["goal:p1-s1"]["meta"]["technique"], "plain")
            self.assertEqual(by_id["goal:p1-s1"]["meta"]["and_group"], "plan-1")
            self.assertEqual(by_id["goal:p1-s2"]["meta"]["status"], "closed")
            plans = [n for n in workflow["graph"]["nodes"] if n["meta"]["workflow_kind"] == "plan"]
            self.assertEqual(plans[0]["meta"]["status"], "closed")
            # meta.technique is the strategist's plan technique; a goal
            # transformation's decomposition edges carry the CGM tag "plain".
            self.assertEqual(plans[0]["meta"]["technique"], "reduction")
            self.assertEqual(plans[0]["meta"]["decomposition_technique"], "plain")
            self.assertEqual(plans[0]["meta"]["and_group"], "plan-1")
            # A plan without subgoals still reports its technique.
            self.assertEqual(plans[1]["meta"]["technique"], "plain")
            self.assertIsNone(plans[1]["meta"]["decomposition_technique"])
            self.assertIsNone(plans[1]["meta"]["and_group"])
            self.assertEqual(plans[1]["meta"]["score"], 0.9)
            self.assertIn("Strong", plans[1]["meta"]["comment"])
            # Cache round trip for both views.
            self.assertEqual(engine_graph.rebuild(workspace, {"engine": "math_harness"}, view="workflow"), workflow)
            self.assertEqual(engine_graph.load_cached(workspace, view="workflow"), workflow)


class ClosureRenderingTests(unittest.TestCase):
    FACTS = [
        {"fact_id": "b" * 16, "statement": "B", "proof": "uses a", "predecessors": ["a" * 16]},
        {"fact_id": "a" * 16, "statement": "A", "proof": "base", "predecessors": []},
        {"fact_id": "c" * 16, "statement": "C", "proof": "uses b", "predecessors": ["b" * 16]},
        {"fact_id": "d" * 16, "statement": "D", "proof": "unrelated", "predecessors": []},
    ]

    def test_rendering_is_deterministic_topological_and_closure_only(self) -> None:
        rendered = cgm_closure.render_verified_closure(self.FACTS, "c" * 16)
        self.assertEqual(rendered, cgm_closure.render_verified_closure(list(reversed(self.FACTS)), "c" * 16))
        self.assertLess(rendered.index("## Lemma 1 · fact " + "a" * 16), rendered.index("## Lemma 2 · fact " + "b" * 16))
        self.assertIn("## Main theorem · fact " + "c" * 16, rendered)
        self.assertIn(f"**Uses:** Lemma 2 (fact {'b' * 16}).", rendered)
        self.assertNotIn("unrelated", rendered)
        self.assertTrue(rendered.endswith("\n") and not rendered.endswith("\n\n"))
        self.assertEqual(cgm_closure.closure_order(self.FACTS, "c" * 16), ["a" * 16, "b" * 16, "c" * 16])

    def test_malformed_dags_raise(self) -> None:
        cyclic = [
            {"fact_id": "a" * 16, "statement": "A", "proof": "p", "predecessors": ["b" * 16]},
            {"fact_id": "b" * 16, "statement": "B", "proof": "p", "predecessors": ["a" * 16]},
        ]
        for facts, target in (
            (cyclic, "a" * 16),
            (self.FACTS, "e" * 16),
            (self.FACTS + [self.FACTS[0]], "c" * 16),
            ([{"fact_id": "a" * 16, "statement": "A", "proof": "p", "predecessors": ["f" * 16]}], "a" * 16),
        ):
            with self.subTest(target=target), self.assertRaises(cgm_closure.ClosureError):
                cgm_closure.render_verified_closure(facts, target)
        with self.assertRaises(engine_graph.EngineGraphError):
            engine_graph.render_fact_closure(cyclic, "a" * 16)

    def test_fact_serialization_round_trips_through_the_validator(self) -> None:
        statement = cgm_schema.normalize_text("A statement with odd\r\nline breaks  ")
        proof = cgm_schema.normalize_text("## proof\nA proof containing a heading line.\n")
        fact_id = cgm_schema.compute_fact_id(
            problem_id="p", predecessors=[], glossary_introduces={}, statement=statement, proof=proof
        )
        payload = cgm_schema.serialize_fact(
            fact_id=fact_id, problem_id="p", author="a", predecessors=[], statement=statement, proof=proof
        ).encode()
        parsed = engine_graph._parse_fact(payload, fact_id, "x")
        self.assertEqual((parsed.statement, parsed.proof), (statement, proof))


class AttestationTamperTests(unittest.TestCase):
    def _assert_unattested(self, workspace: Path) -> None:
        facts = engine_graph.build(workspace, {"engine": "math_harness"})
        self.assertEqual(facts["provenance"]["attestation"], "unattested")
        self.assertEqual(facts["provenance"]["receipt_status"], "invalid")
        dag = engine_graph.load_fact_dag(workspace)
        if dag is not None:
            self.assertFalse(any(item["attested"] for item in dag))

    def test_each_tampering_revokes_attestation_or_fails_closed(self) -> None:
        def edit_proof(ws: Path) -> None:
            path = ws / "proof.md"
            path.write_text(path.read_text() + "\nAn appended remark.\n")

        def whitespace_proof(ws: Path) -> None:
            path = ws / "proof.md"
            path.write_text(path.read_text().replace("\n\n", "\n \n", 1))

        def edit_problem(ws: Path) -> None:
            (ws / "math_harness/project/PROBLEM.md").write_text("# Problem\n\nA different problem.\n")

        def drop_target(ws: Path) -> None:
            (ws / "math_harness/project/TARGET.md").unlink()

        def forge_certificate(ws: Path) -> None:
            _rewrite_receipt(ws, lambda r: r.update(certificate=True))

        def forge_lean(ws: Path) -> None:
            _rewrite_receipt(ws, lambda r: r.update(lean_verified=True))

        def unbound_field(ws: Path) -> None:
            path = ws / "math_harness/project/engine_receipt.json"
            receipt = json.loads(path.read_text())
            receipt["model"] = "forged-model"
            path.write_text(json.dumps(receipt))

        def extra_fact(ws: Path) -> None:
            statement, proof = "An unrelated verified-looking fact.", "Trivially."
            fact_id = cgm_schema.compute_fact_id(
                problem_id="odd-sum", predecessors=[], glossary_introduces={},
                statement=statement, proof=proof,
            )
            (ws / f"math_harness/project/fact_graph/facts/{fact_id}.md").write_text(
                cgm_schema.serialize_fact(
                    fact_id=fact_id, problem_id="odd-sum", author=runner.AUTHOR,
                    predecessors=[], statement=statement, proof=proof,
                )
            )

        def unknown_receipt_field(ws: Path) -> None:
            _rewrite_receipt(ws, lambda r: r.update(extra="field"))

        def wrong_fact_hash(ws: Path) -> None:
            _rewrite_receipt(ws, lambda r: r["facts"][0].update(sha256="0" * 64))

        def verdict_downgrade(ws: Path) -> None:
            _rewrite_receipt(ws, lambda r: r.update(verdict="unattested"))

        def bad_contract(ws: Path) -> None:
            _rewrite_receipt(
                ws, lambda r: r["workflow"]["response_only_contract"].update(isolation_version=9)
            )

        def call_count_mismatch(ws: Path) -> None:
            _rewrite_receipt(ws, lambda r: r["workflow"]["calls"].update(total=99))

        # Schema 3: calls in canonical turn order, bound retries, verdict
        # replies and bounded concurrency.
        def calls_in_completion_order(ws: Path) -> None:
            def swap(r: dict) -> None:
                r["calls"][1], r["calls"][2] = r["calls"][2], r["calls"][1]

            _rewrite_receipt(ws, swap)

        def retry_count_forged(ws: Path) -> None:
            _rewrite_receipt(ws, lambda r: r["workflow"]["retries"].update(format=1))

        def concurrency_out_of_bounds(ws: Path) -> None:
            _rewrite_receipt(
                ws,
                lambda r: r["workflow"]["budgets"].update(
                    concurrency=cgm_schema.MAX_CONCURRENCY + 1
                ),
            )

        def guest_concurrency_above_ceiling(ws: Path) -> None:
            _rewrite_receipt(
                ws, lambda r: r["workflow"]["budgets"].update(guest=True, concurrency=3)
            )

        def schema_downgrade(ws: Path) -> None:
            _rewrite_receipt(ws, lambda r: r.update(schema_version=2))

        def old_policy_on_schema_three(ws: Path) -> None:
            _rewrite_receipt(
                ws,
                lambda r: r["workflow"].update(
                    verification_policy=cgm_schema.VERIFICATION_POLICIES[2]
                ),
            )

        def call_order_label(ws: Path) -> None:
            _rewrite_receipt(ws, lambda r: r["workflow"].update(call_order="completion@1"))

        def verdict_reply_rebound(ws: Path) -> None:
            _rewrite_receipt(ws, lambda r: r["verdicts"][0].update(response_sha256="0" * 64))

        for label, tamper in (
            ("proof edited", edit_proof),
            ("proof whitespace", whitespace_proof),
            ("problem edited", edit_problem),
            ("target removed", drop_target),
            ("certificate forged", forge_certificate),
            ("lean forged", forge_lean),
            ("unbound field", unbound_field),
            ("extra fact", extra_fact),
            ("unknown receipt field", unknown_receipt_field),
            ("fact hash", wrong_fact_hash),
            ("verdict downgrade with target", verdict_downgrade),
            ("isolation contract", bad_contract),
            ("call count", call_count_mismatch),
            ("calls in completion order", calls_in_completion_order),
            ("retry count", retry_count_forged),
            ("concurrency out of bounds", concurrency_out_of_bounds),
            ("guest concurrency", guest_concurrency_above_ceiling),
            ("schema downgrade", schema_downgrade),
            ("old policy on schema 3", old_policy_on_schema_three),
            ("call order label", call_order_label),
            ("verdict reply rebound", verdict_reply_rebound),
        ):
            with self.subTest(tamper=label), tempfile.TemporaryDirectory() as raw:
                workspace = Path(raw)
                _accepted(workspace)
                tamper(workspace)
                self._assert_unattested(workspace)
                if label not in {"proof whitespace"}:
                    with self.assertRaises(engine_graph.EngineGraphError):
                        engine_graph.build(workspace, {"engine": "math_harness"}, view="workflow")

    def test_fact_content_edit_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            _accepted(workspace)
            fact = next(
                path
                for path in (workspace / "math_harness/project/fact_graph/facts").glob("*.md")
                if "Expanding" in path.read_text()
            )
            fact.write_text(fact.read_text().replace("Expanding", "Clearly"))
            with self.assertRaisesRegex(engine_graph.EngineGraphError, "content id mismatch"):
                engine_graph.build(workspace, {"engine": "math_harness"})
            self.assertIsNone(engine_graph.load_fact_dag(workspace))

    def test_memory_tampering_fails_the_explore_view_closed(self) -> None:
        def edit_row(ws: Path) -> None:
            path = ws / "math_harness/project/global_memory/plan.jsonl"
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            rows[0]["evidence"] += " (edited)"
            path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))

        def rebind_edited_row(ws: Path) -> None:
            path = ws / "math_harness/project/global_memory/edges.jsonl"
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            rows[0]["type"] = "supersedes"
            payload = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
            path.write_text(payload)
            _rewrite_receipt(
                ws,
                lambda r: r["memory"].update(
                    {"edges.jsonl": hashlib.sha256(payload.encode()).hexdigest()}
                ),
            )

        def forge_annotation(ws: Path) -> None:
            path = ws / "math_harness/project/global_memory/annotations.jsonl"
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            forged = next(row for row in rows if row["status"] != "closed")
            forged["status"] = "closed"
            forged["score"] = 1.0
            payload = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
            path.write_text(payload)
            _rewrite_receipt(
                ws,
                lambda r: r["memory"].update(
                    {"annotations.jsonl": hashlib.sha256(payload.encode()).hexdigest()}
                ),
            )

        def rebind_plan_evidence(ws: Path, edit) -> None:
            path = ws / "math_harness/project/global_memory/plan.jsonl"
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            rows[0]["evidence"] = edit(rows[0]["evidence"])
            payload = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
            path.write_text(payload)
            _rewrite_receipt(
                ws,
                lambda r: r["memory"].update(
                    {"plan.jsonl": hashlib.sha256(payload.encode()).hexdigest()}
                ),
            )

        def plan_technique_mismatch(ws: Path) -> None:
            # Plan 1 decomposes with tag "plain"; "witness" would tag "witness".
            rebind_plan_evidence(
                ws, lambda text: text.replace("Technique: reduction", "Technique: witness", 1)
            )

        def plan_technique_missing(ws: Path) -> None:
            rebind_plan_evidence(ws, lambda text: text.split("\n\n", 1)[1])

        def stray_file(ws: Path) -> None:
            (ws / "math_harness/project/global_memory/direction.jsonl").write_text("{}\n")

        def missing_file(ws: Path) -> None:
            (ws / "math_harness/project/global_memory/obstacle.jsonl").unlink()

        for label, tamper in (
            ("row edited", edit_row),
            ("unemitted edge type", rebind_edited_row),
            ("annotation closes an unproved node", forge_annotation),
            ("plan technique contradicts its decomposition", plan_technique_mismatch),
            ("plan technique missing", plan_technique_missing),
            ("stray memory file", stray_file),
            ("missing memory file", missing_file),
        ):
            with self.subTest(tamper=label), tempfile.TemporaryDirectory() as raw:
                workspace = Path(raw)
                _accepted(workspace)
                before = engine_graph.build(workspace, {"engine": "math_harness"}, view="workflow")
                self.assertEqual(before["status"], "complete")
                tamper(workspace)
                with self.assertRaises(engine_graph.EngineGraphError):
                    engine_graph.build(workspace, {"engine": "math_harness"}, view="workflow")


class EngineDispatchTests(unittest.TestCase):
    def test_load_fact_dag_is_none_without_a_fact_graph(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            self.assertIsNone(engine_graph.load_fact_dag(workspace))
            (workspace / "math_harness" / "project" / "fact_graph" / "facts").mkdir(parents=True)
            self.assertIsNone(engine_graph.load_fact_dag(workspace))
            self.assertIsNone(engine_graph.load_fact_dag(workspace / "missing"))

    def test_kimi_engine_renders_only_when_a_project_receipt_exists(self) -> None:
        run = {"run_id": "k", "engine": "kimi", "agents": [{"trace_id": "t", "stage_name": "Draft"}]}
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            for view in ("facts", "workflow"):
                self.assertEqual(
                    engine_graph.build(workspace, run, view=view)["graph_role"], "execution"
                )
            _accepted(workspace)
            self.assertEqual(
                engine_graph.build(workspace, run)["provenance"]["attestation"],
                "harness-gate-receipt",
            )
            self.assertEqual(
                engine_graph.build(workspace, run, view="workflow")["graph_role"], "workflow"
            )
            self.assertEqual(
                engine_graph.build(workspace, {"engine": "plain"})["graph_role"], "execution"
            )


class BudgetTests(unittest.TestCase):
    def test_defaults_bounds_and_guest_ceilings(self) -> None:
        defaults = runner.Budgets.from_env({})
        self.assertEqual(
            (defaults.max_calls, defaults.max_workers, defaults.max_repairs, defaults.guest),
            (16, 3, 2, False),
        )
        clamped = runner.Budgets.from_env(
            {"AGENT_MONITOR_CGM_MAX_CALLS": "1000", "AGENT_MONITOR_CGM_MAX_REPAIRS": "-3"}
        )
        self.assertEqual((clamped.max_calls, clamped.max_repairs), (64, 0))
        for marker in ("AGENT_MONITOR_GUEST", "AGENT_MONITOR_PUBLIC_KIMI", "AGENT_MONITOR_SPONSORED_KIMI"):
            with self.subTest(marker=marker):
                guest = runner.Budgets.from_env(
                    {
                        marker: "1",
                        "AGENT_MONITOR_CGM_MAX_CALLS": "64",
                        "AGENT_MONITOR_CGM_MAX_WORKERS": "3",
                        "AGENT_MONITOR_CGM_MAX_REPAIRS": "4",
                        "AGENT_MONITOR_CGM_MAX_SECONDS": "99999",
                    }
                )
                self.assertTrue(guest.guest)
                self.assertEqual(
                    (guest.max_calls, guest.max_workers, guest.max_repairs, guest.max_seconds),
                    (12, 2, 1, 2400),
                )
        lowered = runner.Budgets.from_env({"AGENT_MONITOR_GUEST": "1", "AGENT_MONITOR_CGM_MAX_CALLS": "6"})
        self.assertEqual(lowered.max_calls, 6)
        with self.assertRaises(runner.MathHarnessError):
            runner.Budgets.from_env({"AGENT_MONITOR_CGM_MAX_CALLS": "lots"})

    def test_guest_run_never_exceeds_twelve_calls(self) -> None:
        wrong = json.dumps(
            {
                "verification_report": {
                    "summary": "gap",
                    "critical_errors": [],
                    "gaps": [{"location": "THEOREM", "issue": "missing case"}],
                },
                "verdict": "wrong",
                "repair_hints": "Handle n = 0.",
            }
        )
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            outputs: list[object] = [STRATEGY, WORKER, WORKER.replace("Pairing", "Grouping")]
            outputs += [OK, OK, wrong, WORKER.replace("For n = 0", "When n = 0"), wrong]
            outputs += [OK, OK, wrong, CURATOR] + [OK] * 20
            result, calls, _events = _run(workspace, outputs, env={"AGENT_MONITOR_GUEST": "1"})
            self.assertLessEqual(len(calls), 12)
            receipt = json.loads((workspace / "math_harness/project/engine_receipt.json").read_text())
            self.assertTrue(receipt["workflow"]["budgets"]["guest"])
            self.assertEqual(receipt["workflow"]["calls"]["total"], len(calls))
            self.assertLessEqual(receipt["workflow"]["calls"]["repair"], 1)
            self.assertEqual(result["status"], "unattested")
            engine_graph.build(workspace, {"engine": "math_harness"}, view="workflow")


# Annotates whichever frontier aliases exist (unknown aliases are ignored).
CURATOR_ANY = json.dumps(
    {
        "annotations": [
            {"node": f"N{index}", "status": "active", "score": 0.5, "comment": "Open."}
            for index in range(1, 9)
        ]
    }
)


def _empty_kimi() -> Exception:
    return runner.APIBackendError("Kimi returned an empty response for kimi-k3")


def _receipt(workspace: Path) -> dict:
    return json.loads((workspace / "math_harness/project/engine_receipt.json").read_text())


def _assert_views_build(test: unittest.TestCase, workspace: Path) -> tuple[dict, dict]:
    facts = engine_graph.build(workspace, {"engine": "math_harness"}, view="facts")
    workflow = engine_graph.build(workspace, {"engine": "math_harness"}, view="workflow")
    auto = engine_graph.build(workspace, {"engine": "math_harness"})
    test.assertEqual(auto["graph_role"], facts["graph_role"])
    return facts, workflow


class RetryAndDegradationRegressionTests(unittest.TestCase):
    def test_call_stage_grammar_accepts_a_low_effort_retry_after_a_format_retry(self) -> None:
        family = cgm_schema.call_family
        for stage, expected in {
            "strategist": "strategist",
            "strategist-low": "strategist",
            "strategist-retry": "strategist",
            "strategist-retry-low": "strategist",
            "worker-1-retry-low": "worker",
            "verify-12-retry-low": "verify",
            "repair-2-retry-low": "repair",
            "curator-retry-low": "curator",
        }.items():
            with self.subTest(stage=stage):
                self.assertEqual(family(stage), expected)
        for stage in (
            "worker-1-low-retry", "worker-1-retry-retry", "worker-1-low-low",
            "worker-retry-low", "strategist-1-retry", "verify-0", "critic-retry-low",
        ):
            with self.subTest(stage=stage):
                self.assertIsNone(family(stage))

    def test_format_retry_then_empty_kimi_reply_then_success_publishes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            outputs: list[object] = [
                STRATEGY,
                "not blocks at all",  # worker-1: malformed
                "",  # worker-1-retry: empty text
                WORKER,  # worker-1-retry-low
                WORKER.replace("Pairing", "Grouping"),  # worker-2
                "garbage",  # verify-1: malformed
                _empty_kimi(),  # verify-1-retry: empty (provider error)
                OK,  # verify-1-retry-low
                OK,
                OK,
                CURATOR,
            ]
            result, calls, _events = _run(workspace, outputs)
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(len(calls), len(outputs))
            receipt = _receipt(workspace)
            stages = [call["stage"] for call in receipt["calls"]]
            self.assertIn("worker-1-retry-low", stages)
            self.assertIn("verify-1-retry-low", stages)
            self.assertEqual(receipt["workflow"]["retries"], {"format": 2, "empty": 2})
            self.assertEqual(receipt["workflow"]["calls"]["worker"], 4)
            self.assertEqual(receipt["workflow"]["calls"]["verify"], 5)
            efforts = {call["stage"]: call["effort"] for call in receipt["calls"]}
            self.assertEqual(efforts["worker-1-retry-low"], "low")
            facts, workflow = _assert_views_build(self, workspace)
            self.assertEqual(facts["provenance"]["attestation"], "harness-gate-receipt")
            self.assertEqual(workflow["status"], "complete")

    def test_empty_strategist_reply_degrades_to_the_direct_plan(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            outputs: list[object] = [_empty_kimi(), _empty_kimi(), WORKER, OK, OK, OK, CURATOR_ANY]
            result, calls, _events = _run(workspace, outputs)
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(len(calls), 7)
            self.assertIn("strategist-fallback-plan", result["degraded"])
            receipt = _receipt(workspace)
            self.assertEqual(
                [call["stage"] for call in receipt["calls"][:2]],
                ["strategist", "strategist-low"],
            )
            self.assertEqual(receipt["workflow"]["calls"]["strategist"], 2)
            _facts, workflow = _assert_views_build(self, workspace)
            plans = [n for n in workflow["graph"]["nodes"] if n["meta"]["workflow_kind"] == "plan"]
            self.assertEqual(len(plans), 1)
            self.assertEqual(plans[0]["meta"]["technique"], "plain")

    def test_other_strategist_provider_failures_still_abort_without_artifacts(self) -> None:
        # On a user-key (or any non-sponsored) route; see the next test.
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            with self.assertRaises(runner.ProviderCallError):
                _run(workspace, [runner.APIBackendError("HTTP 502 from upstream")])
            self.assertEqual([p for p in workspace.rglob("*") if p.is_file()], [])

    def test_a_sponsored_strategist_provider_failure_degrades_to_the_direct_plan(self) -> None:
        # A sponsored Kimi run continues as an empty reply would: the 502 may
        # have been processed, so it is not replayed; the direct plan runs.
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            outputs: list[object] = [
                runner.APIBackendError("HTTP 502 from upstream"),
                WORKER,
                OK,
                OK,
                OK,
                CURATOR_ANY,
            ]
            result, calls, _events = _run(
                workspace, outputs, env={"AGENT_MONITOR_SPONSORED_KIMI": "1"}
            )
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(len(calls), len(outputs))
            for reason in ("strategist-provider-failure", "strategist-fallback-plan"):
                self.assertIn(reason, result["degraded"])
            receipt = _receipt(workspace)
            self.assertEqual(receipt["calls"][0]["stage"], "strategist")
            self.assertIsNone(receipt["calls"][0]["response_sha256"])
            self.assertEqual(receipt["workflow"]["calls"]["strategist"], 1)
            _facts, workflow = _assert_views_build(self, workspace)
            plans = [n for n in workflow["graph"]["nodes"] if n["meta"]["workflow_kind"] == "plan"]
            self.assertEqual(len(plans), 1)
            self.assertEqual(plans[0]["meta"]["technique"], "plain")

    def test_empty_verifier_reply_is_an_error_verdict_not_a_stop(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            outputs: list[object] = [
                STRATEGY,
                WORKER,
                WORKER.replace("Pairing", "Grouping"),
                _empty_kimi(),  # verify-1 (worker 1, L1)
                _empty_kimi(),  # verify-1-low
                OK,
                OK,
                OK,  # worker 2's closure
                CURATOR,
            ]
            result, calls, _events = _run(workspace, outputs)
            self.assertEqual(len(calls), len(outputs))
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(
                [item["verdict"] for item in result["verdicts"]],
                ["error", "correct", "correct", "correct"],
            )
            self.assertIn("verifier-empty", result["degraded"])
            _assert_views_build(self, workspace)

    def test_unicode_line_separators_in_model_output_keep_both_views_valid(self) -> None:
        separated = json.dumps(
            {
                "verification_report": {
                    "summary": "Sound. All steps check. Done.\x85",
                    "critical_errors": [],
                    "gaps": [],
                },
                "verdict": "correct",
                "repair_hints": "",
            },
            ensure_ascii=False,
        )
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            outputs: list[object] = [
                STRATEGY,
                WORKER,
                "junk line one",  # worker-2: unparsable, excerpt kept
                "junk line two\x85",  # worker-2-retry
                separated,
                OK,
                OK,
                CURATOR,
            ]
            result, _calls, _events = _run(workspace, outputs)
            self.assertEqual(result["status"], "accepted")
            receipt = _receipt(workspace)
            memory = workspace / "math_harness/project/global_memory"
            for name, count in receipt["workflow"]["rows"].items():
                payload = (memory / name).read_bytes()
                with self.subTest(file=name):
                    for raw_separator in (" ", " ", "\x85"):
                        self.assertNotIn(raw_separator.encode("utf-8"), payload)
                    self.assertEqual(len(payload.decode("utf-8").splitlines()), count)
            attempts = (memory / "proof_attempt.jsonl").read_text()
            self.assertIn("junk\\u2029line two", attempts)
            facts, workflow = _assert_views_build(self, workspace)
            self.assertEqual(facts["provenance"]["attestation"], "harness-gate-receipt")
            self.assertEqual(workflow["status"], "complete")

    def test_jsonl_reader_splits_rows_on_newline_only(self) -> None:
        rows = engine_graph._workflow_jsonl(
            '{"a": "x y z\x85"}\n{"b": 1}\n'.encode("utf-8"), "t.jsonl"
        )
        self.assertEqual(rows, [{"a": "x y z\x85"}, {"b": 1}])
        for payload in (b'{"a": 1}\n\n', b'{"a": 1}', b'{"a": 1}\n{"b"\n'):
            with self.subTest(payload=payload), self.assertRaises(engine_graph.EngineGraphError):
                engine_graph._workflow_jsonl(payload, "t.jsonl")
        self.assertEqual(engine_graph._workflow_jsonl(b"", "t.jsonl"), [])

    def test_oversized_block_gets_a_format_retry(self) -> None:
        huge = WORKER.replace(
            "Expanding, (k-1)^2",
            "Expanding step. " * (cgm_parsing.MAX_BLOCK_PROOF_BYTES // 16 + 1)
            + "\nExpanding, (k-1)^2",
        )
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            result, calls, _events = _run(
                workspace,
                [STRATEGY, huge, WORKER, OK, OK, OK, CURATOR],
                env={"AGENT_MONITOR_CGM_MAX_WORKERS": "1"},
            )
            self.assertEqual(result["status"], "accepted")
            self.assertIn("block L1 proof exceeds", calls[2]["user"])

    def test_verifier_prompt_over_its_bound_is_never_clipped_or_sent(self) -> None:
        problem = "Prove the following statement. " + "Let x be real. " * 2600
        self.assertLessEqual(len(problem.encode()), runner.MAX_PROBLEM_BYTES)
        statement = "For every integer k >= 1 the bound holds " + "and so on " * 780
        lemmas = "\n\n".join(
            f"=== LEMMA L{index} ===\nUses: none\nStatement:\n{statement}(case {index}).\n"
            f"Proof:\nDirect computation for case {index} verifies the bound.\n=== END ==="
            for index in (1, 2, 3)
        )
        theorem_proof = "Combine L1, L2 and L3. " * 2600
        worker = (
            f"{lemmas}\n\n=== THEOREM ===\nUses: L1, L2, L3\nStatement:\n(the problem)\n"
            f"Proof:\n{theorem_proof}\n=== END ===\n\n=== STATUS ===\nsolved\n=== END ==="
        )
        cgm_parsing.parse_worker(worker)  # within the per-block limits
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            result, calls, _events = _run(
                workspace,
                [STRATEGY, worker, OK, OK, OK, CURATOR_ANY],
                env={"AGENT_MONITOR_CGM_MAX_WORKERS": "1"},
                problem=problem,
            )
            self.assertEqual(result["status"], "unattested")
            self.assertEqual(len(calls), 6)  # no THEOREM verifier call
            self.assertIn("verifier-prompt-too-large", result["degraded"])
            self.assertEqual([v["label"] for v in result["verdicts"]], ["L1", "L2", "L3"])
            verifier_prompts = [c["user"] for c in calls if c["system"] == runner.cgm_prompts.VERIFIER_SYSTEM]
            for index, prompt in enumerate(verifier_prompts, 1):
                self.assertIn(f"{statement}(case {index}).", prompt)
                self.assertLessEqual(len(prompt.encode()), runner.MAX_MODEL_PROMPT_BYTES)
            proof = (workspace / "proof.md").read_text()
            self.assertTrue(proof.startswith("# Proof candidate (not verifier-accepted)"))
            self.assertIn("too large to send to the verifier", proof)
            self.assertFalse((workspace / "math_harness/project/TARGET.md").exists())
            facts, _workflow = _assert_views_build(self, workspace)
            self.assertNotEqual(facts["provenance"].get("attestation"), "harness-gate-receipt")

    def test_caller_problem_id_is_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            result, _calls, _events = _run(
                workspace,
                [STRATEGY, WORKER, WORKER.replace("Pairing", "Grouping"), OK, OK, OK, CURATOR],
                problem_id="odd\nauthor: evil",
            )
            self.assertEqual(result["status"], "accepted")
            for path in (workspace / "math_harness/project/fact_graph/facts").iterdir():
                header = path.read_text().split("---")[1]
                self.assertIn("problem_id: odd-author-evil\n", header)
                self.assertEqual(header.count("author:"), 1)
            self.assertEqual(runner._safe_problem_id("p", {}, "\n..\n"), runner._safe_problem_id("p", {}))

    def test_publication_errors_keep_the_math_harness_error_contract(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            with (
                patch.object(
                    runner, "_self_check_facts", side_effect=engine_graph.EngineGraphError("boom")
                ),
                self.assertRaisesRegex(runner.MathHarnessError, "failed closed: EngineGraphError"),
            ):
                _accepted(workspace)
            self.assertFalse((workspace / "math_harness/project/engine_receipt.json").exists())


class CurationTests(unittest.TestCase):
    def test_and_or_propagation_is_mechanical(self) -> None:
        note = cgm_parsing.CuratorNote
        plans = [
            cgm_curation.PlanShape("p1", ("g1", "g2")),
            cgm_curation.PlanShape("p2", ("g3",)),
            cgm_curation.PlanShape("p3", ()),
        ]
        rows = cgm_curation.propagate(
            plans=plans,
            closed_goals={"g1": ["f1"], "g2": ["f2"]},
            root_closed_by=None,
            notes={
                "g3": note("dead", 0.2, "refuted by N7", ()),
                "p3": note("closed", 0.9, "claims closure", ()),
                "goal:root": note("closed", 0.9, "", ()),
                "g1": note("dead", 0.0, "", ()),
            },
        )
        by_node = {row.node_id: row for row in rows}
        self.assertEqual([row.node_id for row in rows], ["goal:root", "p1", "p2", "p3", "g1", "g2", "g3"])
        self.assertEqual(by_node["g1"].status, "closed")  # verified fact beats the model
        self.assertEqual(by_node["p1"].status, "active")  # assembly not yet verified
        self.assertEqual((by_node["p2"].status, by_node["p2"].score), ("dead", 0.0))
        self.assertEqual(by_node["p3"].status, "open")  # the model can never close
        self.assertEqual(by_node["goal:root"].status, "active")
        closed = cgm_curation.propagate(
            plans=[cgm_curation.PlanShape("p1", ("g1",), accepted=True)],
            closed_goals={},
            root_closed_by="t" * 16,
            notes={},
        )
        self.assertEqual([(row.status, row.score) for row in closed[:2]], [("closed", 1.0), ("closed", 1.0)])


if __name__ == "__main__":
    unittest.main()
