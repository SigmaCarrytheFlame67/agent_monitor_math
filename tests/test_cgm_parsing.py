"""Parsing contracts for CGM pipeline replies (tolerant but fail-closed)."""
from __future__ import annotations

import json
import unittest

from agent_monitor.cgm import parsing


WORKER = """=== LEMMA L1 ===
Subgoal: S1
Uses: none
Statement:
For every integer k >= 1, k^2 - (k-1)^2 = 2k - 1.
Proof:
Expand (k-1)^2 = k^2 - 2k + 1 and subtract from k^2.
=== END ===

=== LEMMA L2 ===
Uses: L1
Statement:
For every n >= 1, sum_{k=1}^n (2k-1) = n^2.
Proof:
By L1: each term equals k^2 - (k-1)^2; the sum telescopes to n^2.
=== END ===

=== THEOREM ===
Uses: L2
Statement:
For every natural number n, 1 + 3 + ... + (2n-1) = n^2.
Proof:
For n = 0 both sides are 0; for n >= 1 apply L2.
=== END ===

=== FINDING dead_end ===
Claim: Pairing terms fails for odd n.
Evidence:
The middle term has no partner.
=== END ===

=== STATUS ===
solved
=== END ==="""


class WorkerParsingTests(unittest.TestCase):
    def test_well_formed_blocks(self) -> None:
        candidate = parsing.parse_worker(WORKER)
        self.assertEqual([block.label for block in candidate.lemmas], ["L1", "L2"])
        self.assertEqual(candidate.lemmas[0].subgoal, "S1")
        self.assertEqual(candidate.lemmas[1].uses, ("L1",))
        self.assertIsNotNone(candidate.theorem)
        assert candidate.theorem is not None
        self.assertEqual(candidate.theorem.uses, ("L2",))
        self.assertEqual(candidate.status, "solved")
        self.assertEqual(candidate.findings[0].kind, "dead_end")
        self.assertEqual(parsing.closure_labels(candidate), ["L1", "L2"])
        # The canonical rendering parses back to the same candidate.
        self.assertEqual(parsing.parse_worker(parsing.render_candidate(candidate)), candidate)

    def test_fences_preambles_and_markdown_decoration_are_tolerated(self) -> None:
        decorated = (
            "Here is the candidate.\n\n```markdown\n"
            + WORKER.replace("=== THEOREM ===", "## === THEOREM ===")
            .replace("Uses: L2", "**Uses:** [L2]")
            .replace("Statement:\nFor every natural", "**Statement.** For every natural")
            .replace("Proof:\nFor n = 0", "*Proof.* For n = 0")
            .replace("solved", "**solved**")
            + "\n```"
        )
        candidate = parsing.parse_worker(decorated)
        assert candidate.theorem is not None
        self.assertEqual(candidate.theorem.uses, ("L2",))
        self.assertEqual(
            candidate.theorem.statement,
            "For every natural number n, 1 + 3 + ... + (2n-1) = n^2.",
        )
        self.assertTrue(candidate.theorem.proof.startswith("For n = 0"))
        self.assertEqual(candidate.status, "solved")

    def test_missing_end_markers_and_numeric_labels_are_recovered(self) -> None:
        text = (
            "=== LEMMA 1 ===\nUses: none\nStatement:\nA claim about integers.\n"
            "Proof:\nA sufficiently long argument for the claim.\n"
            "=== THEOREM ===\nUses: Lemma 1\nStatement:\nThe problem.\nProof:\n"
            "By L1 the problem follows directly and completely.\n"
        )
        candidate = parsing.parse_worker(text)
        self.assertEqual(candidate.lemmas[0].label, "L1")
        assert candidate.theorem is not None
        self.assertEqual(candidate.theorem.uses, ("L1",))
        self.assertEqual(candidate.status, "partial", "a missing STATUS is not 'solved'")

    def test_malformed_candidates_raise(self) -> None:
        cases = {
            "no blocks": "The answer is yes.",
            "unknown use": WORKER.replace("Uses: L1", "Uses: L7"),
            "cycle": WORKER.replace("=== LEMMA L1 ===\nSubgoal: S1\nUses: none", "=== LEMMA L1 ===\nUses: L2"),
            "self use": WORKER.replace("Uses: L1", "Uses: L2"),
            "duplicate label": WORKER.replace("=== LEMMA L2 ===", "=== LEMMA L1 ==="),
            "no statement": WORKER.replace("Statement:\nFor every integer k", "For every integer k"),
            "no proof": WORKER.replace(
                "Proof:\nExpand (k-1)^2 = k^2 - 2k + 1 and subtract from k^2.",
                "Expand everything.",
            ),
            "trivial proof": WORKER.replace(
                "Expand (k-1)^2 = k^2 - 2k + 1 and subtract from k^2.", "Clear."
            ),
            "solved without theorem": WORKER.split("=== THEOREM ===")[0]
            + "=== STATUS ===\nsolved\n=== END ===",
            "reserved label": WORKER.replace("=== LEMMA L2 ===", "=== LEMMA THEOREM ==="),
        }
        for label, text in cases.items():
            with self.subTest(case=label), self.assertRaises(parsing.ParseError):
                parsing.parse_worker(text)

    def test_lemma_limit_is_enforced(self) -> None:
        with self.assertRaises(parsing.ParseError):
            parsing.parse_worker(WORKER, max_lemmas=1)

    def test_pseudo_tool_markup_is_rejected_everywhere(self) -> None:
        for marker in parsing.PSEUDO_TOOL_MARKERS:
            with self.subTest(marker=marker):
                self.assertTrue(parsing.has_pseudo_tool_markup(f"x {marker} y"))
                with self.assertRaises(parsing.ParseError):
                    parsing.parse_worker(WORKER + f"\n{marker}")
                with self.assertRaises(parsing.ParseError):
                    parsing.parse_verifier(
                        '{"verdict":"correct","verification_report":{"summary":"'
                        + marker.replace('"', "")
                        + '","critical_errors":[],"gaps":[]},"repair_hints":""} '
                        + marker
                    )

    def test_stuck_candidate_with_counterexample(self) -> None:
        candidate = parsing.parse_worker(
            "=== FINDING counterexample ===\nClaim: n = 1 fails\nEvidence:\nCheck it.\n"
            "=== END ===\n=== STATUS ===\nstuck\n=== END ==="
        )
        self.assertIsNone(candidate.theorem)
        self.assertEqual(candidate.status, "stuck")
        self.assertEqual(candidate.findings[0].kind, "counterexample")
        self.assertEqual(parsing.closure_labels(candidate), [])


class JsonParsingTests(unittest.TestCase):
    def test_last_object_with_the_required_key_wins(self) -> None:
        text = (
            'Example: {"verdict": "wrong"}. Final answer:\n```json\n'
            '{"verification_report":{"summary":"ok","critical_errors":[],"gaps":[]},'
            '"verdict":"correct","repair_hints":""}\n```'
        )
        self.assertEqual(parsing.find_json_object(text, required_key="verdict")["verdict"], "correct")
        with self.assertRaises(parsing.ParseError):
            parsing.find_json_object("no json at all", required_key="verdict")
        with self.assertRaises(parsing.ParseError):
            parsing.find_json_object('{"other": 1}', required_key="verdict")

    def test_unescaped_tex_is_repaired_before_parsing(self) -> None:
        raw = '{"verdict":"wrong","note":"\\frac{1}{2} \\neq \\alpha, \\nabla f \\to 0","ok":"line\\nnext"}'
        value = parsing.find_json_object(raw, required_key="verdict")
        self.assertEqual(value["note"], "\\frac{1}{2} \\neq \\alpha, \\nabla f \\to 0")
        self.assertEqual(value["ok"], "line\nnext")
        escaped = json.dumps({"verdict": "x", "note": "\\sum_{i} \\beta"})
        self.assertEqual(
            parsing.find_json_object(escaped, required_key="verdict")["note"],
            "\\sum_{i} \\beta",
        )


class VerifierParsingTests(unittest.TestCase):
    @staticmethod
    def _report(verdict: str, *, errors=(), gaps=(), hints: str = "") -> str:
        return json.dumps(
            {
                "verification_report": {
                    "summary": "s",
                    "critical_errors": list(errors),
                    "gaps": list(gaps),
                },
                "verdict": verdict,
                "repair_hints": hints,
            }
        )

    def test_cgm_contract_round_trips(self) -> None:
        report = parsing.parse_verifier(self._report("correct"))
        self.assertEqual(report.verdict, "correct")
        self.assertEqual(report.repair_hints, "")
        self.assertEqual(
            report.as_json(),
            {
                "verification_report": {"summary": "s", "critical_errors": [], "gaps": []},
                "verdict": "correct",
                "repair_hints": "",
            },
        )
        wrong = parsing.parse_verifier(
            self._report(
                "wrong",
                gaps=[{"location": "Lemma 2", "issue": "missing bound"}],
                hints="Prove the bound.",
            )
        )
        self.assertEqual(wrong.verdict, "wrong")
        self.assertEqual(wrong.gaps[0].location, "Lemma 2")
        self.assertFalse(wrong.normalized)

    def test_any_finding_forces_wrong_and_hints_are_never_empty(self) -> None:
        forced = parsing.parse_verifier(
            self._report("correct", errors=[{"location": "proof", "issue": "false step"}])
        )
        self.assertEqual(forced.verdict, "wrong")
        self.assertTrue(forced.normalized)
        self.assertIn("false step", forced.repair_hints)
        bare = parsing.parse_verifier(self._report("wrong"))
        self.assertEqual(bare.verdict, "wrong")
        self.assertTrue(bare.repair_hints)

    def test_contract_violations_raise(self) -> None:
        cases = {
            "unknown verdict": self._report("needs_revision"),
            "no report": json.dumps({"verdict": "correct", "repair_hints": ""}),
            "hints on correct": self._report("correct", hints="Rewrite everything."),
            "bad list": json.dumps(
                {
                    "verification_report": {"summary": "s", "critical_errors": "x", "gaps": []},
                    "verdict": "wrong",
                    "repair_hints": "h",
                }
            ),
            "issue without text": self._report("wrong", gaps=[{"location": "x"}], hints="h"),
        }
        for label, text in cases.items():
            with self.subTest(case=label), self.assertRaises(parsing.ParseError):
                parsing.parse_verifier(text)
        self.assertEqual(
            parsing.parse_verifier(self._report("correct", hints="None.")).verdict, "correct"
        )

    def test_conflicting_verdict_objects_fail_closed(self) -> None:
        # The verdict gates acceptance: a later "correct" object -- an echoed
        # template, a quoted blob, or a second answer -- must never override
        # an explicit "wrong" (or be chosen between at all).
        wrong = self._report("wrong", gaps=[{"location": "L1", "issue": "gap"}], hints="Fix L1.")
        ok = self._report("correct")
        cases = {
            "wrong then correct": f"{wrong}\n\nFor reference, the required format is:\n{ok}",
            "echo inside prose": f"{wrong}\nThe proof ends with the text {ok}",
            "correct then wrong": f"{ok}\n{wrong}",
            "two identical": f"{ok}\n```json\n{ok}\n```",
            "invalid template then answer": (
                '{"verification_report": {...}, "verdict": "correct" | "wrong"}\n' + ok
            ),
            "unparseable wrong then correct": (
                '{"verification_report": {"summary": "bad"}, "verdict": "wrong", }\n' + ok
            ),
            "escaped key": wrong.replace('"verdict"', '"\\u0076erdict"') + "\n" + ok,
            "correct then escaped wrong": ok + "\n" + wrong.replace('"verdict"', '"\\u0076erdict"'),
        }
        for label, text in cases.items():
            with self.subTest(case=label), self.assertRaises(parsing.ParseError):
                parsing.parse_verifier(text)

    def test_single_verdict_object_tolerates_fences_and_prose(self) -> None:
        ok = self._report("correct")
        for label, text in {
            "bare": ok,
            "fenced": f"```json\n{ok}\n```",
            "preamble": f"Here is my report.\n\n{ok}\n\nDone.",
            "other object without a verdict": f'Context: {{"note": "x"}}\n{ok}',
        }.items():
            with self.subTest(case=label):
                self.assertEqual(parsing.parse_verifier(text).verdict, "correct")

    def test_keys_outside_the_output_contract_are_rejected(self) -> None:
        extra_top = json.loads(self._report("correct"))
        extra_top["confidence"] = 0.9
        nested = json.loads(self._report("correct"))
        nested["verification_report"]["verdict"] = "wrong"
        extra_report = json.loads(self._report("correct"))
        extra_report["verification_report"]["notes"] = "x"
        for label, value in {
            "extra top-level key": extra_top,
            "nested verdict": nested,
            "extra report key": extra_report,
        }.items():
            with self.subTest(case=label), self.assertRaises(parsing.ParseError):
                parsing.parse_verifier(json.dumps(value))

    def test_too_many_json_objects_fail_closed(self) -> None:
        text = "{}" * 70 + self._report("correct")
        with self.assertRaises(parsing.ParseError):
            parsing.parse_verifier(text)
        with self.assertRaises(parsing.ParseError):
            parsing.find_json_object(text, required_key="verdict")

    def test_report_strings_are_line_normalized(self) -> None:
        text = json.dumps(
            {
                "verification_report": {
                    "summary": "Sound. All steps check.\u0085Done.",
                    "critical_errors": [],
                    "gaps": [{"location": "L1 step 2", "issue": "a b"}],
                },
                "verdict": "wrong",
                "repair_hints": "Fix it.",
            },
            ensure_ascii=False,
        )
        report = parsing.parse_verifier(text)
        rendered = json.dumps(report.as_json(), ensure_ascii=False)
        for separator in (" ", " ", "\x85"):
            self.assertNotIn(separator, rendered)
        self.assertEqual(report.summary, "Sound.\nAll steps check.\nDone.")
        self.assertEqual(report.gaps[0].location, "L1 step 2")
        self.assertEqual(report.gaps[0].issue, "a\nb")
        self.assertEqual(report.repair_hints, "Fix\nit.")


class WorkerBlockLimitTests(unittest.TestCase):
    def test_oversized_blocks_are_a_format_error(self) -> None:
        long_proof = WORKER.replace(
            "Expand (k-1)^2", "x " * (parsing.MAX_BLOCK_PROOF_BYTES // 2) + "Expand (k-1)^2"
        )
        with self.assertRaisesRegex(parsing.ParseError, "block L1 proof exceeds"):
            parsing.parse_worker(long_proof)
        long_statement = WORKER.replace(
            "For every integer k >= 1,",
            "For every integer k >= 1 " + "and " * parsing.MAX_BLOCK_STATEMENT_BYTES + ",",
        )
        with self.assertRaisesRegex(parsing.ParseError, "block L1 statement exceeds"):
            parsing.parse_worker(long_statement)

    def test_theorem_statement_is_exempt_because_the_problem_is_verified(self) -> None:
        restated = WORKER.replace(
            "For every natural number n, 1 + 3 + ... + (2n-1) = n^2.",
            "For every natural number n " + "(restated) " * 1000 + "the identity holds.",
        )
        candidate = parsing.parse_worker(restated)
        self.assertIsNotNone(candidate.theorem)


class StrategistAndCuratorParsingTests(unittest.TestCase):
    def test_strategist_plans_are_normalized_and_bounded(self) -> None:
        text = json.dumps(
            {
                "immediate_conclusions": [{"statement": "c", "fragile": True}],
                "obstacles": ["o"],
                "plans": [
                    {
                        "label": "A",
                        "technique": "Induction",
                        "approach": "Induct.",
                        "subgoals": [
                            {"id": "S1", "statement": "base"},
                            {"id": "S2", "statement": "step", "depends_on": ["S1", "S9"]},
                        ],
                        "risks": "r",
                    },
                    {"label": "A", "technique": "plain", "approach": "Duplicate label."},
                    {"label": "B", "technique": "reduce", "approach": "Reduce."},
                    {"label": "C", "technique": "witness", "approach": "Construct."},
                    {"label": "D", "technique": "invariant", "approach": "Invariant."},
                ],
            }
        )
        strategy = parsing.parse_strategist(text, max_plans=3)
        self.assertEqual([plan.label for plan in strategy.plans], ["A", "B", "C"])
        self.assertEqual(
            [plan.technique for plan in strategy.plans], ["plain", "reduction", "witness"]
        )
        self.assertEqual(strategy.plans[0].subgoals[1].depends_on, ("S1",))
        self.assertTrue(strategy.conclusions[0].fragile)
        with self.assertRaises(parsing.ParseError):
            parsing.parse_strategist('{"plans": []}', max_plans=3)
        with self.assertRaises(parsing.ParseError):
            parsing.parse_strategist('{"plans": [{"label": "x"}]}', max_plans=3)
        fallback = parsing.fallback_strategy()
        self.assertTrue(fallback.fallback)
        self.assertEqual(len(fallback.plans), 1)

    def test_curator_notes_are_filtered_to_known_frontier_nodes(self) -> None:
        text = json.dumps(
            {
                "annotations": [
                    {"node": "N1", "status": "active", "score": 0.4, "comment": "see N3 and N9"},
                    {"node": "N2", "status": "bogus", "score": 7, "comment": "x"},
                    {"node": "N8", "status": "open", "score": 0.1, "comment": "unknown"},
                    {"node": "N1", "status": "dead", "score": 0.0, "comment": "duplicate"},
                ]
            }
        )
        notes = parsing.parse_curator(text, aliases=["N1", "N2"], citable=["N1", "N2", "N3"])
        self.assertEqual(set(notes), {"N1", "N2"})
        self.assertEqual(notes["N1"].status, "active")
        self.assertEqual(notes["N1"].cited, ("N3",))
        self.assertIsNone(notes["N2"].status)
        self.assertIsNone(notes["N2"].score)
        with self.assertRaises(parsing.ParseError):
            parsing.parse_curator('{"annotations": []}', aliases=["N1"])


if __name__ == "__main__":
    unittest.main()
