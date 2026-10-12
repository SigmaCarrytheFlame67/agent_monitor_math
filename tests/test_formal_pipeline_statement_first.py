"""Statement-first Formal (Lean) pipeline: parsing, lock, loop and soundness."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import (
    auto_pipeline,
    engine_graph,
    lean_broker,
    lean_verify,
    proof_bridge,
    proof_graph,
)


def _release_fields() -> dict[str, str]:
    toolchain_tree = "3" * 64
    mathlib_tree = "4" * 64
    identity = {
        "schema": 1,
        "lean_toolchain": lean_broker.EXPECTED_LEAN_TOOLCHAIN,
        "lean_version": lean_broker.EXPECTED_TOOLCHAIN,
        "mathlib_input_rev": lean_broker.EXPECTED_MATHLIB_INPUT_REV,
        "mathlib_rev": lean_broker.EXPECTED_MATHLIB_REV,
        "toolchain_tree_sha256": toolchain_tree,
        "mathlib_tree_sha256": mathlib_tree,
    }
    encoded = (json.dumps(identity, sort_keys=True, separators=(",", ":")) + "\n").encode()
    return {
        "release_id": hashlib.sha256(encoded).hexdigest(),
        "toolchain_tree_sha256": toolchain_tree,
        "mathlib_tree_sha256": mathlib_tree,
    }


def _bound_check(lean_dir: Path, uses_mathlib: bool, *, ok: bool = True, stdout: str = "") -> dict:
    """What a successful `_run_lean_check` returns for the exact file bytes."""
    source = (Path(lean_dir) / lean_verify.PROOF_FILENAME).read_bytes()
    return {
        "ok": ok,
        "status": "verified" if ok else "failed",
        "exit_code": 0 if ok else 1,
        "duration_s": 0.01,
        "command": ["lean-broker", "mathlib" if uses_mathlib else "core"],
        "toolchain": lean_broker.EXPECTED_TOOLCHAIN,
        "sandbox": lean_broker.SANDBOX_MARKER,
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "checker_profile": "mathlib" if uses_mathlib else "core",
        "stdout": stdout,
        "stderr": "",
        **_release_fields(),
    }


def _fenced(source: str, meta: dict | None = None) -> str:
    return (
        "```lean\n" + source.strip() + "\n```\n```json\n"
        + json.dumps(meta or {"main_decl": "main"}) + "\n```"
    )


SKELETON = """import Mathlib

/-- The claim.
Informal: target -/
theorem main (n : ℕ) : n + 0 = n := by
  sorry
"""
PROVED = SKELETON.replace("  sorry", "  simp")
DRIFTED = PROVED.replace("theorem main (n : ℕ) : n + 0 = n", "theorem main : (0 : ℕ) + 0 = 0")
AUDIT_OK = {"ok": True, "faithful": True, "severity": "ok", "issues": []}


class _GenerateHarness:
    """Run generate() with scripted model replies and checker outcomes."""

    def __init__(
        self,
        test: unittest.TestCase,
        replies,
        checks,
        audits=None,
        guidance=None,
        fingerprint=None,
    ):
        self.test = test
        self.replies = list(replies)
        self.checks = list(checks)
        self.audits = list(audits or [AUDIT_OK, AUDIT_OK])
        self.guidance = guidance
        self.prompts: list[str] = []
        self.main_decls: list[str | None] = []
        self.fingerprint_calls: list[str] = []
        # Lean's statement fingerprint, faked: by default it follows the text
        # lock (same statement text -> same fingerprint); tests override it to
        # model what only Lean can see.
        self.fingerprint = fingerprint or self._text_fingerprint

    @staticmethod
    def _text_fingerprint(source: str, main_decl: str) -> dict:
        lock = lean_verify._statement_lock(source, main_decl)
        if lock is None:
            return {"ok": False, "error": "no main"}
        return {"ok": True, "fingerprint": lock["sha256"], "decl": lock["main_decl"]}

    def fake_fingerprint(self, source: str, *, uses_mathlib: bool, main_decl: str, timeout=None) -> dict:
        self.fingerprint_calls.append(source)
        return self.fingerprint(source, main_decl)

    def chat(self, env, model, prompt, timeout=None):
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return ("gpt-test", reply)

    def check(self, lean_dir: Path, uses_mathlib: bool, main_decl: str | None = None) -> dict:
        self.main_decls.append(main_decl)
        outcome = self.checks.pop(0)
        if outcome == "missing":
            return {
                "ok": False,
                "status": "toolchain_missing",
                "stderr": "Lean checker broker unavailable; refused to run Proof.lean through any local fallback",
                "stdout": "",
            }
        if outcome == "fail":
            return _bound_check(
                lean_dir,
                uses_mathlib,
                ok=False,
                stdout="/tmp/p/Proof.lean:5:2: error: unknown identifier 'foo'\n",
            )
        return _bound_check(lean_dir, uses_mathlib)

    def run(
        self,
        workspace: Path,
        *,
        user: dict | None = None,
        problem: str = "Prove n + 0 = n for all naturals n.",
        **kwargs,
    ) -> tuple[dict, dict]:
        (workspace / "problem.txt").write_text(problem, encoding="utf-8")
        with (
            patch("agent_monitor.settings.resolved_user_env", return_value={}),
            patch.object(lean_verify, "_proof_source", return_value=("proof.md", "By simp.")),
            patch.object(lean_verify, "_chat", side_effect=self.chat),
            patch.object(lean_verify, "_run_lean_check", side_effect=self.check),
            patch.object(lean_verify, "_audit_faithfulness", side_effect=lambda *a, **k: dict(self.audits.pop(0))) as audits,
            patch.object(lean_verify, "_sorry_goals", return_value="line 6:\nn : ℕ\n⊢ n + 0 = n"),
            patch.object(lean_verify, "_statement_fingerprint", side_effect=self.fake_fingerprint),
            patch.object(lean_verify, "toolchain_status", return_value={"available": True}),
        ):
            result = lean_verify.generate(
                workspace=workspace,
                run_record={},
                user=user or {"id": 7},
                model="gpt-test",
                guidance=self.guidance,
                **kwargs,
            )
        return result, {"audit_calls": audits.call_count}


class PayloadParsingTests(unittest.TestCase):
    def test_fenced_lean_plus_json_metadata_is_the_primary_contract(self) -> None:
        payload = lean_verify._parse_model_payload(
            "Here is the file.\n" + _fenced(PROVED, {"main_decl": "main", "notes": "done", "title": "t"})
        )
        self.assertEqual(payload["lean"].strip(), PROVED.strip())
        self.assertEqual(payload["main_decl"], "main")
        self.assertEqual(payload["notes"], "done")

    def test_uses_mathlib_is_derived_from_imports_never_the_model_flag(self) -> None:
        legacy = json.dumps({"lean": "import Mathlib\ntheorem main : True := trivial", "uses_mathlib": False})
        self.assertTrue(lean_verify._parse_model_payload(legacy)["uses_mathlib"])
        core = json.dumps({"lean": "theorem main : True := trivial", "uses_mathlib": True})
        self.assertFalse(lean_verify._parse_model_payload(core)["uses_mathlib"])
        self.assertTrue(lean_verify._detect_mathlib("  import Mathlib\n"))
        self.assertFalse(lean_verify._detect_mathlib("-- no imports\ntheorem x : True := trivial"))

    def test_legacy_json_with_a_lean_fence_inside_the_string_still_parses(self) -> None:
        legacy = json.dumps(
            {"lean": "```lean\ntheorem main : 1 = 1 := rfl\n```", "notes": "legacy", "main_decl": "main"}
        )
        payload = lean_verify._parse_model_payload(legacy)
        self.assertEqual(payload["lean"], "theorem main : 1 = 1 := rfl")
        self.assertEqual(payload["notes"], "legacy")

    def test_json_newline_before_exists_is_not_rewritten_as_tex(self) -> None:
        # proof_graph's TeX repair turns `\n` + `exists` into `\\nexists`;
        # strict parsing must come first for Lean payloads.
        source = "theorem main : ∃ n : Nat, n = 0 := by\nexists 0"
        payload = lean_verify._parse_model_payload(json.dumps({"lean": source}))
        self.assertEqual(payload["lean"], source)

    def test_audit_json_with_a_lean_fence_inside_a_field_is_extracted(self) -> None:
        reply = (
            "Review:\n```json\n"
            + json.dumps(
                {
                    "faithful": False,
                    "severity": "major",
                    "statement_suggestion": "```lean\ntheorem main (n : ℕ) : P n\n```",
                }
            )
            + "\n```"
        )
        data = lean_verify._extract_json_object(reply)
        self.assertEqual(data["severity"], "major")

    def test_raw_lean_and_garbage_replies(self) -> None:
        raw = lean_verify._parse_model_payload("theorem main : True := trivial")
        self.assertIn("raw Lean", raw["notes"])
        with self.assertRaises(ValueError):
            lean_verify._parse_model_payload("I cannot help with that.")

    def test_truncated_reply_keeps_the_open_lean_fence(self) -> None:
        payload = lean_verify._parse_model_payload("```lean\nimport Mathlib\ntheorem main : True := by\n  triv")
        self.assertIn("theorem main", payload["lean"])


class SharedJsonExtractionTests(unittest.TestCase):
    def test_extract_json_skips_foreign_language_fences_only(self) -> None:
        # Informal behavior is unchanged for ```json / bare fences and prose.
        self.assertEqual(proof_graph._extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(proof_graph._extract_json('```\n{"a": 3}\n```'), {"a": 3})
        self.assertEqual(proof_graph._extract_json('Here: {"a": 4} done'), {"a": 4})
        # A ```lean fence inside a Formal DAG statement is no longer mistaken
        # for the payload.
        graph = {"nodes": [{"id": "n1", "statement": "```lean\ntheorem x : True\n```"}]}
        self.assertEqual(proof_graph._extract_json(json.dumps(graph)), graph)


class DiagnosticsTests(unittest.TestCase):
    def test_first_errors_with_line_context_not_the_tail(self) -> None:
        source = "\n".join(f"line{i}" for i in range(1, 41))
        log = "\n".join(
            f"/tmp/x/Proof.lean:{n}:2: error: problem number {n}" for n in range(3, 40, 3)
        )
        digest = lean_verify._error_digest({"stdout": log, "stderr": ""}, source, limit=3)
        self.assertIn("problem number 3", digest)
        self.assertIn("problem number 9", digest)
        self.assertNotIn("problem number 12", digest)
        self.assertNotIn("problem number 39", digest)
        self.assertIn(">>   3 | line3", digest)
        self.assertIn("further error(s) omitted", digest)

    def test_checker_messages_without_positions_are_kept(self) -> None:
        digest = lean_verify._error_digest(
            {"stdout": "", "stderr": "Lean broker timed out (the fixed server limit is about 90 seconds)"},
            "theorem main : True := trivial",
        )
        self.assertIn("timed out", digest)

    def test_numbered_source_is_one_based(self) -> None:
        numbered = lean_verify._numbered_source("a\nb")
        self.assertEqual(numbered.splitlines()[1], "  2 | b")

    def test_goal_probe_marks_only_tactic_sorry(self) -> None:
        source = (
            "theorem a : True := by sorry\n"
            "theorem b : True :=\n"
            "  sorry\n"
            "theorem c (n : Nat) : n = n := by\n"
            "  induction n with\n"
            "  | zero => rfl\n"
            "  | succ k ih =>\n"
            "    sorry\n"
        )
        probe = lean_verify._sorry_goal_probe_source(source)
        self.assertIn('by trace "AMGOAL 1"; trace_state; sorry', probe)
        self.assertIn("\n  sorry\n", probe)  # term-mode `:= sorry` left alone
        self.assertIn('    trace "AMGOAL 8"; trace_state; sorry', probe)


class CheatRuleTests(unittest.TestCase):
    def test_metaprogramming_and_trust_escapes_are_major(self) -> None:
        cases = {
            "metaprogram": "run_cmd Lean.logInfo \"x\"\ntheorem main : 1 = 1 := rfl",
            "custom_syntax": "macro \"cheat\" : tactic => `(tactic| sorry)\ntheorem main : 1 = 2 := by cheat",
            "initialize": "initialize pure ()\ntheorem main : 1 = 1 := rfl",
            "hidden_admit": "theorem main : 1 = 2 := by run_tac Lean.Elab.admitGoal (← Lean.Elab.Tactic.getMainGoal)",
            "axiom": "axiom cheat : False\ntheorem main : 1 = 2 := cheat.elim",
            "opaque": "opaque secret : Nat\ntheorem main : secret = secret := rfl",
            "implemented_by": "@[implemented_by foo] def bar : Nat := 0\ntheorem main : 1 = 1 := rfl",
            "extern": "@[extern \"c_fn\"] def bar : Nat := 0\ntheorem main : 1 = 1 := rfl",
            "unsafe": "unsafe def bar : Nat := 0\ntheorem main : 1 = 1 := rfl",
            "skip_kernel_tc": "set_option debug.skipKernelTC true\ntheorem main : 1 = 1 := rfl",
            "native_decide": "theorem main : 2 + 2 = 4 := by native_decide",
            "core_instance": "instance : LE ℕ := ⟨fun _ _ => True⟩\ntheorem main : 5 ≤ 3 := trivial",
        }
        for flag, source in cases.items():
            with self.subTest(flag=flag):
                flags = {f["id"]: f["severity"] for f in lean_verify._fidelity_flags(source)}
                self.assertEqual(flags.get(flag), "major", flags)
                self.assertEqual(lean_verify._fidelity_severity(lean_verify._fidelity_flags(source), None), "major")

    def test_comments_and_strings_do_not_trigger_rules(self) -> None:
        source = (
            "/-- We never use run_cmd, macro, axiom or native_decide here. -/\n"
            "theorem main (n : ℕ) : n + 0 = n := by\n"
            "  -- admitGoal would be cheating\n"
            "  simp\n"
        )
        self.assertEqual(
            [f for f in lean_verify._fidelity_flags(source) if f["severity"] == "major"], []
        )

    def test_incomplete_with_major_fidelity_is_downgraded(self) -> None:
        flags = [{"id": "x", "severity": "major"}]
        self.assertEqual(lean_verify._apply_fidelity("incomplete", flags, None), "unfaithful")
        self.assertEqual(lean_verify._apply_fidelity("incomplete", [], AUDIT_OK), "incomplete")
        self.assertEqual(lean_verify._apply_fidelity("failed", flags, None), "failed")


TELESCOPE_PROBLEM = (
    "Prove that for every natural number n, the sum over k = 1 to n of 1/(k(k+1)) "
    "equals n/(n+1), where all arithmetic is in the rational numbers."
)
TELESCOPE_MAIN = """import Mathlib

/-- For every positive natural number `n` (the proof takes ℕ = {1, 2, 3, …}).
Cites: self-derived -/
theorem main (n : ℕ) (hn : 1 ≤ n) :
    ∑ k ∈ Finset.Icc 1 n, (1 : ℚ) / ((k : ℚ) * ((k : ℚ) + 1)) =
      (n : ℚ) / ((n : ℚ) + 1) := by
  sorry
"""
NARROWED = SKELETON.replace("theorem main (n : ℕ) :", "theorem main (n : ℕ) (hn : 1 ≤ n) :")
EVERY_NATURAL = "Prove that for every natural number n, n + 0 = n."


def _narrowed(source: str, problem: str) -> list[dict]:
    return [
        flag for flag in lean_verify._fidelity_flags(source, problem=problem)
        if flag["id"] == lean_verify.NARROWED_DOMAIN_FLAG
    ]


class NarrowedDomainTests(unittest.TestCase):
    """The PROBLEM's quantifier domain wins over the informal proof's convention."""

    def test_telescope_lower_bound_the_problem_never_states_is_minor(self) -> None:
        flags = lean_verify._fidelity_flags(TELESCOPE_MAIN, problem=TELESCOPE_PROBLEM)
        narrowed = [f for f in flags if f["id"] == lean_verify.NARROWED_DOMAIN_FLAG]
        self.assertEqual(len(narrowed), 1)
        self.assertEqual(narrowed[0]["severity"], "minor")
        self.assertIn("`1 ≤ n`", narrowed[0]["message"])
        self.assertEqual(narrowed[0]["lines"], [5])
        # Never major on its own: it cannot downgrade a verified result.
        self.assertEqual(lean_verify._fidelity_severity(flags, AUDIT_OK), "minor")
        self.assertEqual(lean_verify._apply_fidelity("verified", flags, AUDIT_OK), "verified")
        self.assertTrue(lean_verify._statement_fix_wanted(flags, AUDIT_OK))
        # Without the problem text (source-only callers) nothing is guessed.
        self.assertEqual(_narrowed(TELESCOPE_MAIN, ""), [])
        self.assertEqual(
            [f["id"] for f in lean_verify._fidelity_flags(TELESCOPE_MAIN)], ["sorry"]
        )
        # The faithful statement is clean.
        faithful = TELESCOPE_MAIN.replace(" (hn : 1 ≤ n)", "")
        self.assertEqual(_narrowed(faithful, TELESCOPE_PROBLEM), [])
        self.assertFalse(
            lean_verify._statement_fix_wanted(
                lean_verify._fidelity_flags(faithful, problem=TELESCOPE_PROBLEM), AUDIT_OK
            )
        )

    def test_bounds_the_problem_states_or_implies_are_not_flagged(self) -> None:
        fact = "import Mathlib\nopen Nat\ntheorem main (n : ℕ) (hn : 1 ≤ n) : n ! ≥ 2 ^ (n - 1) := by\n  sorry\n"
        cases = [
            (fact, "Prove that for every natural number n >= 1, n! >= 2^(n-1)."),
            (fact, "Prove that for n ≥ 1, n! ≥ 2^(n-1)."),
            (fact, r"Show $n! \geq 2^{n-1}$ for all integers $n\ge1$."),
            (fact, "Prove that for every positive integer n, n! >= 2^(n-1)."),
            (fact, "Let n be a natural number with n at least 1. Show n! is at least 2^(n-1)."),
            (fact, "For n = 1, 2, 3, ... prove n! >= 2^(n-1)."),
            (fact, "Here ℕ = {1, 2, 3, …}. Prove that for every n ∈ ℕ, n! ≥ 2^(n-1)."),
            (
                "theorem main (n : ℕ) (hn : 1 < n) : 2 ≤ n := by\n  omega\n",
                "Prove that for every natural number n > 1, n ≥ 2.",
            ),
            (
                "theorem main (a b : ℝ) (ha : 0 ≤ a) (hb : 0 ≤ b) : 0 ≤ a * b := by\n  positivity\n",
                "Prove that the product of two nonnegative reals is nonnegative.",
            ),
            # Implied by another hypothesis on the same variable: not narrower.
            (
                "theorem main (p : ℕ) (hp : p.Prime) (h2 : 2 ≤ p) : p ∣ p := by\n  simp\n",
                "Prove that every prime number p divides itself.",
            ),
            (
                "theorem main (p : ℕ) (hp : Nat.Prime p) (h1 : 1 < p) : p ∣ p := by\n  simp\n",
                "Prove that every prime number p divides itself.",
            ),
            # `0 ≤ n` on ℕ restricts nothing.
            (
                "theorem main (n : ℕ) (h : 0 ≤ n) : n + 0 = n := by\n  simp\n",
                EVERY_NATURAL,
            ),
            # Bounds inside the conclusion are the claim, not a hypothesis.
            (
                "theorem main (N : ℕ) : ∃ p, Nat.Prime p ∧ p > N ∧ 2 < p := by\n  sorry\n",
                "Prove that for every natural number N there is a prime p exceeding N.",
            ),
            (
                "theorem main (N : ℕ) : ∃ p : ℕ, 1 ≤ p ∧ N < p := by\n  sorry\n",
                "Prove that for every natural number N there is a natural p with N < p.",
            ),
            # A bound on a term, not on a quantified variable.
            (
                "theorem main (n : ℕ) (h : 1 ≤ n + 1) : n + 0 = n := by\n  simp\n",
                EVERY_NATURAL,
            ),
        ]
        # Restricted-set notation and wording that implies the bound.
        positive_n = "theorem main (n : ℕ) (hn : 0 < n) : n ∣ n.factorial := by\n  sorry\n"
        positive_x = "theorem main (x : ℝ) (hx : 0 < x) : 0 < x ^ 2 := by\n  positivity\n"
        for problem in (
            r"Show $n \mid n!$ for every $n \in \mathbb{N} \setminus \{0\}$.",
            r"Show $n \mid n!$ for every $n \in \mathbb{N}\setminus\{0\}$.",
            "For every n ∈ ℕ \\ {0} show n ∣ n!.",
            "For every n ∈ ℕ∖{0} show n ∣ n!.",
            "For every n ∈ ℕ_+ show n ∣ n!.",
            r"For every $n \in \mathbb{N}^*$ show $n \mid n!$.",
            "For every n in N* show that n divides n!.",
            r"Show $n \mid n!$ for all integers $n \geqslant 1$.",
            "Show n divides n! for every natural number n not less than 1.",
            "Show n divides n! for every natural number n not equal to 0.",
        ):
            cases.append((positive_n, problem))
        for problem in (
            r"Prove that $x^2 > 0$ for every $x \in \mathbb{R}^+$.",
            r"Prove that $x^2 > 0$ for every $x \in \mathbb{R}_{>0}$.",
            "Prove that x^2 > 0 for every x ∈ ℝ⁺.",
            "Prove that x^2 > 0 for every x in the open interval (0, 1).",
            "Prove that the square of a side length x of a triangle is positive.",
        ):
            cases.append((positive_x, problem))
        cases.append(
            (
                "theorem main (p : ℕ) (hp : p.Prime) (hp2 : 2 < p) : 8 ∣ p ^ 2 - 1 := by\n  sorry\n",
                "Let p be an odd prime. Prove that p^2 - 1 is divisible by 8.",
            )
        )
        cases.append(
            (
                "theorem main (n : ℕ) (h1 : 1 < n) (hc : ¬ Nat.Prime n) :\n"
                "    ∃ p, p.Prime ∧ p ∣ n ∧ p * p ≤ n := by\n  sorry\n",
                "Prove that every composite number n has a prime divisor p with p*p <= n.",
            )
        )
        for source, problem in cases:
            with self.subTest(problem=problem, source=source.splitlines()[-2]):
                self.assertEqual(_narrowed(source, problem), [])

    def test_arithmetic_and_plain_domains_are_not_read_as_bounds(self) -> None:
        # `N*(N+1)`, `N + 1`, `n ∈ ℕ`, `\mathbb{N}` and `ℤ × ℤ` state no bound,
        # so an unstated `1 ≤ n` in `main` is still flagged.
        for problem in (
            "Prove that for every natural number N, N*(N+1) is even.",
            "Prove that for every natural number N, N * (N + 1) is even.",
            "Show that for every n ∈ ℕ, 6 divides n^3 - n.",
            r"Prove that for all $n \in \mathbb{N}$, $\sum_{k=0}^{n} k = n(n+1)/2$.",
            "Show that every (x, y) ∈ ℤ × ℤ satisfies x + y = y + x.",
            TELESCOPE_PROBLEM,
        ):
            with self.subTest(problem=problem):
                self.assertFalse(lean_verify._problem_states_a_bound(problem))
                self.assertEqual(len(_narrowed(NARROWED, problem)), 1)

    def test_unstated_bounds_are_read_from_binders_foralls_and_arrows(self) -> None:
        cases = [
            ("theorem main (n : ℕ) (hn : 0 < n) : n + 0 = n := by\n  simp\n", "`0 < n`"),
            ("theorem main (n : ℕ) (hn : n ≠ 0) : n + 0 = n := by\n  simp\n", "`n ≠ 0`"),
            ("theorem main {n : ℕ} (hn : n ≥ 2) : n + 0 = n := by\n  simp\n", "`n ≥ 2`"),
            ("theorem main : ∀ n : ℕ, 1 ≤ n → n + 0 = n := by\n  simp\n", "`1 ≤ n`"),
            ("theorem main : ∀ n ≥ 1, n + 0 = n := by\n  simp\n", "`n ≥ 1`"),
            ("theorem main (n : ℕ) (h : 0 < n ∧ n < 100) : n + 0 = n := by\n  simp\n", "`0 < n`"),
            (
                "theorem main (p : ℕ) (hp : p.Prime) (h5 : 5 ≤ p) : p ∣ p := by\n  simp\n",
                "`5 ≤ p`",
            ),
        ]
        for source, expected in cases:
            with self.subTest(source=source.splitlines()[0]):
                narrowed = _narrowed(source, EVERY_NATURAL)
                self.assertEqual(len(narrowed), 1)
                self.assertIn(expected, narrowed[0]["message"])
                self.assertEqual(narrowed[0]["severity"], "minor")
        real = "theorem main (x : ℝ) (hx : (0 : ℝ) < x) : x + 0 = x := by\n  simp\n"
        real_flags = _narrowed(real, "Prove that x + 0 = x for every real number x.")
        self.assertEqual(len(real_flags), 1)
        # The ℕ-includes-0 reminder is only given for a natural-number bound.
        self.assertNotIn("ℕ includes 0", real_flags[0]["message"])
        self.assertIn(
            "Lean's ℕ includes 0", _narrowed(NARROWED, EVERY_NATURAL)[0]["message"]
        )
        # Only the main theorem is read; helpers may carry their own bounds.
        helper = (
            "theorem aux (k : ℕ) (hk : 1 ≤ k) : k + 0 = k := by\n  simp\n\n"
            "theorem main (n : ℕ) : n + 0 = n := by\n  simp\n"
        )
        self.assertEqual(_narrowed(helper, EVERY_NATURAL), [])
        self.assertEqual(
            len(lean_verify._narrowed_domain_flags(helper, EVERY_NATURAL, "aux")), 1
        )

    def test_statement_fix_prompt_carries_the_minor_flag(self) -> None:
        flags = lean_verify._fidelity_flags(NARROWED, problem=EVERY_NATURAL)
        prompts: list[str] = []

        def chat(env, model, prompt, timeout=None):
            prompts.append(prompt)
            return "gpt-test", _fenced(SKELETON)

        with patch.object(lean_verify, "_chat", side_effect=chat):
            fixed = lean_verify._repair_statement(
                {}, "gpt-test", problem=EVERY_NATURAL, lean=NARROWED,
                audit=dict(AUDIT_OK), flags=flags, proof="Let ℕ = {1, 2, 3, …}.",
            )
        self.assertIsNotNone(fixed)
        self.assertIn("a restriction the PROBLEM never states", prompts[0])
        self.assertIn("mechanical fidelity check", prompts[0])
        self.assertIn("conventions in the informal proof", prompts[0])
        # A plain `sorry` flag alone asks for no statement fix.
        with patch.object(lean_verify, "_chat", side_effect=chat):
            self.assertIsNone(
                lean_verify._repair_statement(
                    {}, "gpt-test", problem=EVERY_NATURAL, lean=SKELETON,
                    audit=dict(AUDIT_OK),
                    flags=lean_verify._fidelity_flags(SKELETON, problem=EVERY_NATURAL),
                )
            )

    def test_domain_rule_reaches_translate_audit_and_fix_prompts(self) -> None:
        for prompt in (
            lean_verify.TRANSLATE_PROMPT,
            lean_verify.STATEMENT_FIX_PROMPT,
        ):
            self.assertIn("conventions in the informal proof", prompt)
            self.assertIn("Lean's ℕ includes 0", prompt)
        self.assertIn("never from the informal proof's conventions", lean_verify.AUDIT_PROMPT)
        self.assertIn("Lean's ℕ includes 0", lean_verify.AUDIT_PROMPT)

    def test_flag_alone_triggers_one_statement_fix(self) -> None:
        # The auditor misses the narrowed domain; the mechanical flag still
        # asks for one fix, which restores `∀ n` and is audited again.
        run = _GenerateHarness(
            self,
            [_fenced(NARROWED), _fenced(SKELETON), _fenced(PROVED)],
            ["ok", "ok", "ok"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            result, info = run.run(Path(tmp), problem=EVERY_NATURAL)
        self.assertEqual(
            [a["action"] for a in result["attempts"]], ["statement", "statement-fix", "prove"]
        )
        self.assertEqual(info["audit_calls"], 2)
        self.assertIn("An independent audit or a mechanical fidelity check", run.prompts[1])
        self.assertIn("`1 ≤ n`", run.prompts[1])
        self.assertEqual(result["status"], "verified")
        self.assertNotIn("hn", result["lean"])
        self.assertEqual(result["fidelity"]["severity"], "ok")
        self.assertNotIn(
            lean_verify.NARROWED_DOMAIN_FLAG,
            {f["id"] for f in result["fidelity"]["flags"]},
        )

    def test_a_fix_the_audit_rejects_keeps_the_original_statement(self) -> None:
        wrong = "import Mathlib\n\ntheorem main : (0 : ℕ) + 0 = 0 := by\n  sorry\n"
        narrowed_proved = NARROWED.replace("  sorry", "  simp")
        run = _GenerateHarness(
            self,
            [_fenced(NARROWED), _fenced(wrong), _fenced(narrowed_proved)],
            ["ok", "ok", "ok"],
            audits=[
                AUDIT_OK,
                {"ok": True, "faithful": False, "severity": "major", "issues": ["fixed n"]},
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            result, _info = run.run(workspace, problem=EVERY_NATURAL)
            on_disk = (workspace / "lean" / "Proof.lean").read_text(encoding="utf-8")
        attempts = result["attempts"]
        self.assertEqual(
            [a["action"] for a in attempts],
            ["statement", "statement-fix", "statement-fix", "prove"],
        )
        self.assertEqual(attempts[2]["check"]["status"], "rejected")
        self.assertEqual(result["status"], "verified")
        self.assertIn("(hn : 1 ≤ n)", result["lean"])
        self.assertEqual(on_disk, result["lean"])
        # The original audit stands; the flag stays visible as a minor caveat.
        self.assertEqual(result["statement_audit"]["severity"], "ok")
        self.assertEqual(result["fidelity"]["severity"], "minor")
        self.assertIn(
            lean_verify.NARROWED_DOMAIN_FLAG,
            {f["id"] for f in result["fidelity"]["flags"]},
        )
        self.assertIn("a restriction the PROBLEM never states", result["notes"])


class LeanIdiomPromptTests(unittest.TestCase):
    def test_idiom_hints_reach_the_prove_and_repair_prompts_only(self) -> None:
        for prompt in (lean_verify.PROVE_PROMPT, lean_verify.REPAIR_PROMPT):
            self.assertIn("LEAN 4.14 / MATHLIB IDIOMS", prompt)
            self.assertIn("`n !` with a space", prompt)
            self.assertIn("no goals to be solved", prompt)
            self.assertNotIn("{idioms}", prompt)
        self.assertNotIn("MATHLIB IDIOMS", lean_verify.TRANSLATE_PROMPT)
        self.assertNotIn("MATHLIB IDIOMS", lean_verify.STATEMENT_FIX_PROMPT)
        # Each name below was confirmed with `#check` against the pinned Lean
        # 4.14.0 + Mathlib v4.14.0 mirror (`Finset.sum_Icc_succ_top` is the
        # `to_additive` form of `Finset.prod_Icc_succ_top`). Check any new name
        # the same way before adding it here.
        names = set(re.findall(r"(?:Nat|Finset)\.[A-Za-z_]+(?:\.[A-Za-z_]+)*", lean_verify.LEAN_IDIOMS))
        self.assertEqual(
            names,
            {
                "Nat.factorial",
                "Nat.cast_succ",
                "Nat.add_mul_mod_self_right",
                "Finset.range",
                "Finset.Icc",
                "Finset.sum_range_succ",
                "Finset.sum_Icc_succ_top",
            },
        )


class StatementLockTests(unittest.TestCase):
    def test_lock_covers_signature_and_non_theorem_commands_above_main(self) -> None:
        lock = lean_verify._statement_lock(SKELETON)
        self.assertEqual(lock["main_decl"], "main")
        self.assertEqual(lean_verify._lock_violation(lock, PROVED), "")
        helper = PROVED.replace("/-- The claim.", "lemma helper : True := trivial\n\n/-- The claim.")
        self.assertEqual(lean_verify._lock_violation(lock, helper), "")
        self.assertIn("statement", lean_verify._lock_violation(lock, DRIFTED))
        new_def = PROVED.replace("/-- The claim.", "def f := 3\n\n/-- The claim.")
        self.assertIn("definition", lean_verify._lock_violation(lock, new_def))
        renamed = PROVED.replace("theorem main", "theorem main2")
        self.assertIn("missing", lean_verify._lock_violation(lock, renamed))
        # Whitespace-only reformatting of the signature is not drift.
        spaced = PROVED.replace("(n : ℕ) : n + 0 = n", "(n : ℕ) :\n    n + 0 = n")
        self.assertEqual(lean_verify._lock_violation(lock, spaced), "")

    def test_absolute_value_lines_stay_inside_the_locked_signature(self) -> None:
        source = "theorem main (x y : ℝ) :\n    |x + y| ≤ |x| + |y| := by\n  sorry\n"
        lock = lean_verify._statement_lock(source)
        self.assertIn("|x + y| ≤ |x| + |y|", lock["signature"])
        weaker = source.replace("|x + y| ≤", "|x + y| <")
        self.assertTrue(lean_verify._lock_violation(lock, weaker))

    def test_probe_target_is_namespace_qualified(self) -> None:
        source = "namespace Foo\ntheorem main : True := trivial\nend Foo\n"
        self.assertEqual(lean_verify._probe_target(source), "Foo.main")
        self.assertEqual(lean_verify._lean_name_literal("Foo.main"), "«Foo».«main»")


class GenerateLoopTests(unittest.TestCase):
    def test_happy_path_is_statement_audit_lock_then_prove(self) -> None:
        run = _GenerateHarness(self, [_fenced(SKELETON), _fenced(PROVED)], ["ok", "ok"])
        with tempfile.TemporaryDirectory() as tmp:
            result, info = run.run(Path(tmp))
        self.assertEqual(result["status"], "verified")
        self.assertEqual([a["action"] for a in result["attempts"]], ["statement", "prove"])
        # One independent statement audit; the lock lets it stand for the proof.
        self.assertEqual(info["audit_calls"], 1)
        self.assertEqual(result["fidelity"]["audit"]["statement_lock"], result["statement_lock"]["sha256"])
        # The skeleton prompt states the target and asks for sorry proofs only.
        self.assertIn("PHASE 1 OF 2", run.prompts[0])
        self.assertIn("Lean 4.14.0", run.prompts[0])
        self.assertIn("LOCKED SKELETON", run.prompts[1])

    def test_statement_lock_rejects_drift_and_tells_the_model(self) -> None:
        run = _GenerateHarness(
            self,
            [_fenced(SKELETON), _fenced(DRIFTED), _fenced(PROVED)],
            ["ok", "ok"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            result, _info = run.run(Path(tmp), max_repairs=2)
        actions = [a["action"] for a in result["attempts"]]
        self.assertEqual(actions, ["statement", "lock-violation", "sorry-repair"])
        self.assertEqual(result["status"], "verified")
        self.assertIn("theorem main (n : ℕ) : n + 0 = n", result["lean"])
        repair_prompt = run.prompts[2]
        self.assertIn("YOUR PREVIOUS REPLY WAS REJECTED", repair_prompt)
        self.assertIn("theorem main (n : ℕ) : n + 0 = n", repair_prompt)
        self.assertIn("GOALS AT REMAINING `sorry`", repair_prompt)

    def test_unfaithful_draft_is_never_restored_after_the_statement_fix(self) -> None:
        # The first draft compiles WITHOUT sorry but states the wrong claim.
        # After the audited fix is locked, only lock-respecting candidates may
        # become the final artifact, even though the draft "verified".
        wrong = "import Mathlib\n\ntheorem main : (0 : ℕ) + 0 = 0 := by\n  simp\n"
        fixed = {"lean": SKELETON, "notes": "restated", "citations": [], "main_decl": "main"}
        broken = PROVED.replace("  simp", "  foo")
        run = _GenerateHarness(
            self,
            [_fenced(wrong), _fenced(broken), _fenced(broken)],
            ["ok", "ok", "fail", "fail"],
            audits=[
                {"ok": True, "faithful": False, "severity": "major", "issues": ["fixed n"]},
                AUDIT_OK,
            ],
        )
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(lean_verify, "_repair_statement", return_value=fixed),
        ):
            workspace = Path(tmp)
            result, _info = run.run(workspace, max_repairs=1)
            on_disk = (workspace / "lean" / "Proof.lean").read_text(encoding="utf-8")
        self.assertEqual(
            [a["action"] for a in result["attempts"]],
            ["statement", "statement-fix", "prove", "repair"],
        )
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("theorem main (n : ℕ) : n + 0 = n", result["lean"])
        self.assertEqual(on_disk, result["lean"])

    def test_placeholders_inside_inserted_text_are_not_re_expanded(self) -> None:
        text = lean_verify._fill_template("A {problem} B {proof}", problem="x {proof} y", proof="P")
        self.assertEqual(text, "A x {proof} y B P")

    def test_sorry_elimination_continues_after_a_compiling_file_with_sorry(self) -> None:
        partial = PROVED.replace("  simp", "  sorry")
        run = _GenerateHarness(
            self,
            [_fenced(SKELETON), _fenced(partial), _fenced(PROVED)],
            ["ok", "ok", "ok"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            result, _info = run.run(Path(tmp), max_repairs=3)
        self.assertEqual([a["action"] for a in result["attempts"]], ["statement", "prove", "sorry-repair"])
        self.assertEqual(result["status"], "verified")
        self.assertIn("line 6:", run.prompts[2])  # goals from the checker

    def test_best_checked_candidate_survives_a_worse_final_round(self) -> None:
        partial = PROVED.replace("  simp", "  sorry")
        broken = PROVED.replace("  simp", "  foo")
        run = _GenerateHarness(
            self,
            [_fenced(SKELETON), _fenced(partial), _fenced(broken)],
            ["ok", "ok", "fail"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            result, _info = run.run(workspace, max_repairs=1)
            on_disk = (workspace / "lean" / "Proof.lean").read_text(encoding="utf-8")
        self.assertEqual(result["status"], "incomplete")
        self.assertNotIn("  foo", result["lean"])
        self.assertEqual(on_disk, result["lean"])
        self.assertIsNotNone(result["kernel_receipt"])

    def test_repair_exception_persists_partial_progress(self) -> None:
        partial = PROVED.replace("  simp", "  sorry")
        run = _GenerateHarness(
            self,
            [
                _fenced(SKELETON),
                _fenced(partial),
                ValueError("provider exploded"),
                ValueError("provider exploded again"),
            ],
            ["ok", "ok"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            result, _info = run.run(workspace, max_repairs=4)
            cached = lean_verify.load_cached(workspace)
        # One failed round is recorded and retried; two in a row stop the
        # loop. The checked partial proof is persisted either way.
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(
            [a["action"] for a in result["attempts"]][-2:], ["sorry-repair", "sorry-repair"]
        )
        self.assertEqual(result["attempts"][-1]["check"]["status"], "error")
        self.assertIn("provider exploded again", result["notes"])
        self.assertEqual(len(run.prompts), 4)
        self.assertEqual(cached["status"], "incomplete")
        self.assertIn("  sorry", cached["lean"])

    def test_one_unparseable_reply_does_not_end_the_run(self) -> None:
        run = _GenerateHarness(
            self,
            [_fenced(SKELETON), "I will now think about it...", _fenced(PROVED)],
            ["ok", "ok"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            result, _info = run.run(Path(tmp), max_repairs=2)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(
            [a["action"] for a in result["attempts"]], ["statement", "prove", "sorry-repair"]
        )
        self.assertEqual(result["attempts"][1]["check"]["status"], "error")

    def test_broker_unavailable_short_circuits_without_repairs_or_audits(self) -> None:
        run = _GenerateHarness(self, [_fenced(SKELETON)], ["missing"], audits=[])
        with tempfile.TemporaryDirectory() as tmp:
            result, info = run.run(Path(tmp), max_repairs=4)
        self.assertEqual(result["status"], "toolchain_missing")
        self.assertEqual(len(run.prompts), 1)
        self.assertEqual(info["audit_calls"], 0)
        self.assertIn("unavailable", result["notes"])
        self.assertIsNone(result["kernel_receipt"])

    def test_guest_budget_is_clamped(self) -> None:
        replies = [_fenced(SKELETON)] + [_fenced(PROVED.replace("  simp", "  foo"))] * 6
        run = _GenerateHarness(self, replies, ["ok"] + ["fail"] * 6)
        env = {"AGENT_MONITOR_MODEL": "kimi-k3"}
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(lean_verify, "_llm_environment", return_value=env),
        ):
            result, _info = run.run(
                Path(tmp), user={"id": 7, "guest": True}, max_repairs=9, audit=False
            )
        # statement + prove + GUEST_MAX_REPAIRS repairs.
        self.assertEqual(len(run.prompts), 2 + lean_verify.GUEST_MAX_REPAIRS)
        self.assertEqual(result["status"], "incomplete")

    def test_blueprint_is_a_labelled_section_before_the_proof(self) -> None:
        run = _GenerateHarness(
            self,
            [_fenced(SKELETON), _fenced(PROVED)],
            ["ok", "ok"],
            guidance="INFORMAL FACT DAG BLUEPRINT\n[1] fact_id: abcd",
        )
        with tempfile.TemporaryDirectory() as tmp:
            run.run(Path(tmp))
        prompt = run.prompts[0]
        self.assertLess(prompt.index("BLUEPRINT (advisory"), prompt.index("INFORMAL PROOF"))
        self.assertIn("[1] fact_id: abcd", prompt)
        self.assertFalse(prompt.rstrip().endswith("fact_id: abcd"))

    def test_audit_model_override_is_independent_but_never_for_guests(self) -> None:
        env = {lean_verify.AUDIT_MODEL_ENV: "other-model"}
        self.assertEqual(lean_verify._audit_model(env, "author"), "other-model")
        self.assertEqual(lean_verify._audit_model(env, "kimi-k3", guest=True), "kimi-k3")
        with patch.dict(os.environ, {lean_verify.AUDIT_MODEL_ENV: ""}):
            self.assertEqual(lean_verify._audit_model({}, "author"), "author")


class HarnessScaffoldTests(unittest.TestCase):
    def test_scaffold_never_requires_mathlib_from_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ldir = Path(tmp) / "lean"
            lean_verify._write_scaffold(ldir, uses_mathlib=True)
            lakefile = (ldir / "lakefile.lean").read_text(encoding="utf-8")
            toolchain = (ldir / "lean-toolchain").read_text(encoding="utf-8").strip()
        self.assertNotIn("require", lakefile)
        self.assertNotIn("git", lakefile)
        self.assertEqual(toolchain, lean_broker.EXPECTED_LEAN_TOOLCHAIN)

    def test_check_wrapper_uses_the_mirror_and_replaces_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mirror = root / "mirror"
            (mirror / ".lake" / "packages" / "mathlib").mkdir(parents=True)
            (mirror / "lake-manifest.json").write_text("{}", encoding="utf-8")
            ldir = root / "lean"
            ldir.mkdir()
            victim = root / "victim.txt"
            victim.write_text("keep", encoding="utf-8")
            (ldir / "check.sh").symlink_to(victim)
            with patch.dict(os.environ, {"AGENT_MONITOR_MATHLIB_PROJECT": str(mirror)}):
                script = lean_verify._write_harness_checker(ldir)
            text = script.read_text(encoding="utf-8")
            self.assertFalse(script.is_symlink())
            self.assertEqual(victim.read_text(encoding="utf-8"), "keep")
            self.assertTrue(stat.S_IMODE(script.stat().st_mode) & 0o100)
        self.assertIn("lake env lean", text)
        self.assertIn(str(mirror.resolve()), text)

    def test_harness_env_puts_elan_shims_on_path(self) -> None:
        with (
            patch("agent_monitor.settings.resolved_user_env", return_value={"OPENAI_API_KEY": "k"}),
            patch.object(lean_verify, "toolchain_status", return_value={}),
            patch.object(lean_verify, "_harness_lean_bin_dirs", return_value=["/opt/elan-test/bin"]),
        ):
            env = lean_verify._harness_env("codex", {"id": 7}, model="gpt-test", auth_route="api_key")
        self.assertIn("/opt/elan-test/bin", env["PATH"].split(os.pathsep))


FACTS = [
    {"fact_id": "t0000000000000000", "statement": "The target claim.", "proof": "By a and b.",
     "predecessors": ["b000000000000000", "a000000000000000"], "is_target": True, "attested": True},
    {"fact_id": "b000000000000000", "statement": "Fact B uses A.", "proof": "From A.",
     "predecessors": ["a000000000000000"], "is_target": False, "attested": True},
    {"fact_id": "a000000000000000", "statement": "Base fact A.", "proof": "Direct.",
     "predecessors": [], "is_target": False, "attested": True},
]


class BlueprintAndCoverageTests(unittest.TestCase):
    def test_fact_dag_blueprint_is_topological_with_informal_ids(self) -> None:
        text = proof_bridge.guidance_from_facts(FACTS)
        a, b, t = (text.index(f"fact_id: {f}") for f in ("a000000000000000", "b000000000000000", "t0000000000000000"))
        self.assertLess(a, b)
        self.assertLess(b, t)
        self.assertIn("Informal: <fact_id>", text)
        self.assertIn("TARGET → `main`", text)
        self.assertEqual(proof_bridge.guidance_from_facts([]), "")

    def test_auto_pipeline_prefers_the_fact_dag_contract(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(engine_graph, "load_fact_dag", create=True, return_value=FACTS) as loader,
        ):
            text, source = auto_pipeline._lean_guidance(Path(tmp), {"informal_graph": {"nodes": [{"id": "n1"}]}})
        loader.assert_called_once()
        self.assertEqual(source, "fact_dag")
        self.assertIn("INFORMAL FACT DAG BLUEPRINT", text)

    def test_parallel_lean_waits_for_the_semantic_informal_dag(self) -> None:
        ready = threading.Event()
        shared = {
            "informal_ready": ready,
            "informal_graph": proof_graph.graph_from_text("First step here.\n\nTherefore done."),
        }

        def finish_informal() -> None:
            shared["informal_graph"] = {
                "nodes": [{"id": "i1", "kind": "conclusion", "label": "Goal", "statement": "The claim"}],
                "edges": [],
            }
            ready.set()

        timer = threading.Timer(0.05, finish_informal)
        timer.start()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(engine_graph, "load_fact_dag", create=True, return_value=None),
        ):
            text, source = auto_pipeline._lean_guidance(Path(tmp), shared)
        timer.join()
        self.assertEqual(source, "informal_dag")
        self.assertIn('"id": "i1"', text)
        self.assertIn("Informal: <node id>", text)

    def test_coverage_maps_explicit_informal_ids_before_lexical_overlap(self) -> None:
        lean = (
            "/-- Informal: n2 -/\n"
            "lemma zzq_helper : 1 = 1 := rfl\n\n"
            "/-- Informal: n3 -/\n"
            "theorem main : 2 = 2 := by\n  exact zzq_helper ▸ rfl\n"
        )
        informal = {
            "graph": {
                "nodes": [
                    {"id": "n2", "kind": "step", "label": "Parity of squares", "statement": "Squares mod four"},
                    {"id": "n3", "kind": "conclusion", "label": "Conclusion", "statement": "Done"},
                ],
                "edges": [{"from": "n2", "to": "n3"}],
            }
        }
        formal = {"graph": proof_graph.graph_from_lean(lean)}
        verified = {
            "status": "verified",
            "sorry_count": 0,
            "lean": lean,
            "fidelity": {"severity": "ok", "audit": {"ok": True, "faithful": True}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "proof_graph.json").write_text(json.dumps(informal), encoding="utf-8")
            (workspace / "lean_proof_graph.json").write_text(json.dumps(formal), encoding="utf-8")
            (workspace / "lean_verify.json").write_text(json.dumps(verified), encoding="utf-8")
            result = proof_bridge.build(workspace)
        items = {item["informal_id"]: item for item in result["items"]}
        self.assertEqual(items["n2"]["match"], "explicit")
        self.assertEqual(items["n2"]["lean_declarations"], ["zzq_helper"])
        self.assertEqual(items["n2"]["status"], "verified")
        self.assertEqual(items["n3"]["lean_declarations"], ["main"])
        self.assertEqual(result["summary"]["verified"], 2)

    def test_informal_links_ignore_undocumented_declarations(self) -> None:
        links = proof_bridge._informal_links(
            "/-- Informal: a1, B2 -/\nlemma x : True := trivial\nlemma y : True := trivial\n"
        )
        self.assertEqual(links, {"x": {"a1", "b2"}})


class FormalDagSelectionTests(unittest.TestCase):
    def test_informative_declaration_graph_needs_no_model(self) -> None:
        lean = (
            "lemma first_step : 1 = 1 := rfl\n\n"
            "lemma second_step : 2 = 2 := by\n  have := first_step\n  rfl\n\n"
            "theorem main : 3 = 3 := by\n  have := first_step\n  have := second_step\n  rfl\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "lean").mkdir()
            (workspace / "lean" / "Proof.lean").write_text(lean, encoding="utf-8")
            with (
                patch("agent_monitor.settings.resolved_user_env", return_value={}),
                patch.object(proof_graph, "_call_llm") as direct_api,
            ):
                result = proof_graph.generate(
                    workspace=workspace, run_record={"auth_route": "api"}, user={"id": 7},
                    kind="formal", allow_codex_fallback=False,
                )
        direct_api.assert_not_called()
        self.assertEqual(result["model"], "lean-parse")
        self.assertEqual({n["label"] for n in result["graph"]["nodes"]}, {"first_step", "second_step", "main"})

    def test_single_theorem_file_still_asks_the_model_for_steps(self) -> None:
        graph = json.dumps(
            {"title": "main", "nodes": [{"id": "n1", "kind": "conclusion", "label": "main", "statement": "theorem main : True"}], "edges": []}
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "lean").mkdir()
            (workspace / "lean" / "Proof.lean").write_text("theorem main : True := by\n  trivial\n", encoding="utf-8")
            with (
                patch("agent_monitor.settings.resolved_user_env", return_value={}),
                patch.object(proof_graph, "_call_llm", return_value=("api-model", graph, "openai")) as direct_api,
            ):
                result = proof_graph.generate(
                    workspace=workspace, run_record={"auth_route": "api"}, user={"id": 7},
                    kind="formal", allow_codex_fallback=False,
                )
        direct_api.assert_called_once()
        self.assertEqual(result["model"], "api-model")


if __name__ == "__main__":
    unittest.main()


class TextParseFormalDagTests(unittest.TestCase):
    SOURCE = """import Mathlib

/-- First fact.
Informal: a1
Cites: self-derived -/
lemma alpha_identity (n : ℤ) : n + 0 = n := by
  simp

/-- Second fact.
Informal: a2
Cites: self-derived -/
lemma beta_bound (n : ℤ) (hn : 2 ≤ n) : 0 ≤ n := by
  -- alpha_identity is not needed here
  linarith

/-- Main: the doc names both helpers.
Informal: t
Cites: alpha_identity — rewrites `n + 0`;
beta_bound — the bound -/
theorem main (n : ℤ) (hn : 2 ≤ n) : 0 ≤ n + 0 := by
  rw [alpha_identity]
  exact beta_bound n hn
"""

    def test_next_declarations_doc_and_comments_are_not_uses(self) -> None:
        # Regression: beta_bound's text slice used to run up to `theorem main`
        # and so included main's `Cites:` doc line, which produced a false
        # alpha_identity → beta_bound edge (as did the `--` remark).
        graph = proof_graph.graph_from_lean(self.SOURCE)
        edges = {(edge["from"], edge["to"]) for edge in graph["edges"]}
        self.assertEqual(edges, {("alpha_identity", "main"), ("beta_bound", "main")})
        nodes = {node["id"]: node for node in graph["nodes"]}
        self.assertNotIn("Cites", nodes["beta_bound"]["statement"])
        self.assertEqual(nodes["main"]["citations"][0].split(" — ")[0], "alpha_identity")
        # Uses written in code still count wherever they appear.
        uses_both = self.SOURCE.replace(
            "  -- alpha_identity is not needed here\n  linarith",
            "  have := alpha_identity n\n  linarith",
        )
        edges = {
            (edge["from"], edge["to"])
            for edge in proof_graph.graph_from_lean(uses_both)["edges"]
        }
        self.assertIn(("alpha_identity", "beta_bound"), edges)


class SorryAwareCompilerFactsTests(unittest.TestCase):
    SOURCE = (
        "theorem base : True := trivial\n\n"
        "theorem gap : 1 = 2 := by\n  sorry\n\n"
        "theorem main : 1 = 2 := gap\n"
    )

    def _rows(self) -> list[dict]:
        def row(name, proof):
            return {
                "name": name, "kind": "theorem", "statementDeps": [],
                "proofDeps": proof, "statementTruncated": False, "proofTruncated": False,
            }

        return [row("base", ["True.intro"]), row("gap", ["sorryAx"]), row("main", ["gap"])]

    def test_sorry_dependence_propagates_through_local_uses(self) -> None:
        from agent_monitor import lean_kg

        declarations = {d["qualified_name"]: d for d in lean_kg._normalize(self.SOURCE, self._rows())}
        self.assertFalse(declarations["base"]["uses_sorry"])
        self.assertTrue(declarations["gap"]["uses_sorry"])
        self.assertTrue(declarations["main"]["uses_sorry"])

    def test_incomplete_but_checked_source_is_eligible_and_still_screened(self) -> None:
        from agent_monitor import lean_kg

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "lean").mkdir()
            (workspace / "lean" / "Proof.lean").write_text(self.SOURCE, encoding="utf-8")
            digest = hashlib.sha256(self.SOURCE.encode()).hexdigest()
            cache = {"status": "incomplete", "lean": self.SOURCE, "checked_source_sha256": digest}
            (workspace / lean_verify.RESULT_FILENAME).write_text(json.dumps(cache), encoding="utf-8")
            self.assertEqual(lean_kg._verified_source_reason(workspace, self.SOURCE), "")
            # Kernel-trust escapes stay blocked even for incomplete files.
            bad = self.SOURCE.replace("theorem base : True := trivial", "theorem base : True := by native_decide")
            (workspace / "lean" / "Proof.lean").write_text(bad, encoding="utf-8")
            cache.update({"lean": bad, "checked_source_sha256": hashlib.sha256(bad.encode()).hexdigest()})
            (workspace / lean_verify.RESULT_FILENAME).write_text(json.dumps(cache), encoding="utf-8")
            self.assertIn("native_decide", lean_kg._verified_source_reason(workspace, bad))
            # A failed compile never qualifies.
            cache["status"] = "failed"
            (workspace / lean_verify.RESULT_FILENAME).write_text(json.dumps(cache), encoding="utf-8")
            self.assertIn("kernel-checked", lean_kg._verified_source_reason(workspace, bad))

    def test_compiler_graph_marks_sorry_dependent_nodes(self) -> None:
        from agent_monitor import lean_kg

        declarations = lean_kg._normalize(self.SOURCE, self._rows())
        snapshot = {
            "schema_version": lean_kg.SCHEMA_VERSION,
            "producer": lean_kg.PRODUCER,
            "status": "ok",
            "sandbox": lean_broker.SANDBOX_MARKER,
            "source_sha256": hashlib.sha256(self.SOURCE.encode()).hexdigest(),
            "normalized_source_sha256": lean_kg.normalized_source_sha256(self.SOURCE),
            "declarations": declarations,
        }
        graph = proof_graph.graph_from_lean(self.SOURCE, kg_snapshot=snapshot)
        cites = {node["label"]: node["citations"] for node in graph["nodes"]}
        self.assertIn("depends on sorry (unproved)", cites["gap"])
        self.assertIn("depends on sorry (unproved)", cites["main"])
        self.assertNotIn("depends on sorry (unproved)", cites["base"])
        self.assertEqual(graph["provenance"]["sorry_dependent_declarations"], 2)

    def test_persist_binds_an_incomplete_checked_source_separately(self) -> None:
        source = "theorem main : 1 = 2 := by\n  sorry\n"
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "lean").mkdir()
            (workspace / "lean" / "Proof.lean").write_text(source, encoding="utf-8")
            check = _bound_check(workspace / "lean", False)
            with patch.object(lean_verify, "toolchain_status", return_value={"available": True}):
                result = lean_verify._persist(
                    workspace, lean_src=source, status="incomplete", uses_mathlib=False,
                    attempts=[], authoritative_check=check,
                )
        digest = hashlib.sha256(source.encode()).hexdigest()
        self.assertIsNone(result["verified_source_sha256"])
        self.assertEqual(result["checked_source_sha256"], digest)
        self.assertIsNotNone(result["kernel_receipt"])


class AutoPipelineBudgetTests(unittest.TestCase):
    def _run_worker(self, user: dict) -> dict:
        captured: dict = {}

        def fake_generate(**kwargs):
            captured.update(kwargs)
            lean_dir = Path(kwargs["workspace"]) / "lean"
            lean_dir.mkdir(exist_ok=True)
            (lean_dir / "Proof.lean").write_text("theorem main : True := trivial\n", encoding="utf-8")
            return {"status": "verified", "model": "m"}

        graph = {"model": "m", "source": "Proof.lean", "graph": {"nodes": [{"id": "main"}], "edges": []}}
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(lean_verify, "generate", side_effect=fake_generate),
            patch.object(proof_graph, "generate", return_value=graph),
            patch.object(engine_graph, "load_fact_dag", create=True, return_value=None),
        ):
            workspace = Path(tmp)
            auto_pipeline._write_state(workspace, {"status": "running", "stages": {}})
            auto_pipeline._lean_and_formal_worker(workspace, {}, user, "m", {}, "hermes")
            state = auto_pipeline.load_state(workspace)
        captured["state"] = state
        return captured

    def test_signed_in_and_guest_repair_budgets(self) -> None:
        signed_in = self._run_worker({"id": 7})
        self.assertEqual(signed_in["max_repairs"], lean_verify.AUTO_MAX_REPAIRS)
        self.assertEqual(lean_verify.AUTO_MAX_REPAIRS, 4)
        guest = self._run_worker({"id": 8, "guest": True})
        self.assertEqual(guest["max_repairs"], lean_verify.GUEST_MAX_REPAIRS)
        self.assertEqual(lean_verify.GUEST_MAX_REPAIRS, 4)
        self.assertEqual(signed_in["state"]["stages"]["lean"]["result"]["blueprint_source"], "none")


# ── Adversarial-review regressions: lock bypass, probe decoys, fingerprint ──


def _axioms_broker(reports: dict[str, str] | None = None, *, preamble: str = ""):
    """Broker double answering each ``#print axioms _root_.X`` line in order.

    ``reports`` maps a probed (unquoted) name to its axiom list text; names
    not listed report no axioms. ``preamble`` is output the checked file
    itself printed before the probe lines.
    """
    calls: list[str] = []

    def check(source: str, **_kwargs) -> dict:
        calls.append(source)
        result = {
            "status": "completed",
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "error": "",
            "duration": 0.01,
            "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "toolchain": lean_broker.EXPECTED_TOOLCHAIN,
            "sandbox": lean_broker.SANDBOX_MARKER,
            "stdout_truncated": False,
            "stderr_truncated": False,
            **_release_fields(),
        }
        lines = [preamble] if preamble else []
        # Like Lean, echo the probe's `#print "…"` marker lines around its reports.
        markers = re.findall(r'(?m)^#print "(AGENT_MONITOR_AXIOMS::[0-9a-f]+::(?:begin|end))"$', source)
        lines += markers[:1]
        for literal in re.findall(r"(?m)^#print axioms _root_\.(\S+)$", source):
            name = ".".join(lean_verify._split_lean_name(literal))
            axioms = (reports or {}).get(name)
            lines.append(
                f"'{name}' depends on axioms: [{axioms}]"
                if axioms
                else f"'{name}' does not depend on any axioms"
            )
        lines += markers[1:]
        result["stdout"] = "\n".join(lines) + "\n"
        return result

    return check, calls


class LockBypassRegressionTests(unittest.TestCase):
    """Commands at any column, `… in` prefixes, and binders in statements."""

    SKELETON = (
        "/-- helper -/\ntheorem helper : 1 = 1 := rfl\n\n"
        "/-- Informal: t -/\ntheorem main : (2 : Nat) + 2 = 5 := by\n  sorry\n"
    )

    def test_indented_or_in_prefixed_commands_above_main_break_the_lock(self) -> None:
        lock = lean_verify._statement_lock(self.SKELETON)
        proved = self.SKELETON.replace("  sorry", "  decide")
        hidden = [
            # Indented under the helper, behind an `open … in` prefix.
            "theorem helper : 1 = 1 := rfl\n  open Nat in instance weird : Add Nat := ⟨fun a b => a * b⟩",
            # Same line as the helper's proof.
            "theorem helper : 1 = 1 := rfl def f := 3",
            "theorem helper : 1 = 1 := rfl\n  attribute [local instance] Nat.decEq",
            "theorem helper : 1 = 1 := rfl\n  set_option autoImplicit true",
        ]
        for replacement in hidden:
            with self.subTest(replacement=replacement):
                source = proved.replace("theorem helper : 1 = 1 := rfl", replacement)
                self.assertIn("above `main`", lean_verify._lock_violation(lock, source))
        # A scoped prefix on main itself is part of main's statement.
        prefixed = proved.replace(
            "theorem main", "attribute [local instance] Nat.decEq in\ntheorem main"
        )
        self.assertIn("statement", lean_verify._lock_violation(lock, prefixed))
        # A proof-only option prefix on main is not drift.
        heartbeats = proved.replace(
            "theorem main", "set_option maxHeartbeats 400000 in\ntheorem main"
        )
        self.assertEqual(lean_verify._lock_violation(lock, heartbeats), "")

    def test_tactic_level_open_and_set_option_stay_inside_their_proof(self) -> None:
        lock = lean_verify._statement_lock(self.SKELETON)
        source = self.SKELETON.replace(
            "theorem helper : 1 = 1 := rfl",
            "theorem helper : 1 = 1 := by\n  open Nat in rfl",
        ).replace("  sorry", "  set_option maxRecDepth 2000 in decide")
        self.assertEqual(lean_verify._lock_violation(lock, source), "")
        keywords = [c["keyword"] for c in lean_verify._lean_commands(source)]
        self.assertEqual(keywords, ["theorem", "theorem"])

    def test_main_inside_an_indented_namespace_locks_its_own_signature(self) -> None:
        source = (
            "import Mathlib\n\nnamespace Problem\n\n"
            "  /-- helper -/\n  lemma helper (n : ℕ) : n ≤ n ^ 2 := by\n    sorry\n\n"
            "  theorem main (n : ℕ) (h : 0 < n) : n ^ 2 ≥ 2 * n - 1 := by\n    sorry\n\n"
            "end Problem\n"
        )
        lock = lean_verify._statement_lock(source)
        self.assertEqual(lock["main_decl"], "Problem.main")
        self.assertTrue(lock["signature"].startswith("theorem main (n : ℕ)"))
        weakened = source.replace("n ^ 2 ≥ 2 * n - 1", "n ^ 2 ≥ 0")
        self.assertIn("statement", lean_verify._lock_violation(lock, weakened))
        # The helper is still free to change.
        helper = source.replace("n ≤ n ^ 2 := by\n    sorry", "n ≤ n ^ 2 := by\n    nlinarith")
        self.assertEqual(lean_verify._lock_violation(lock, helper), "")

    def test_let_and_match_statements_are_locked_in_full(self) -> None:
        let_source = "theorem main : let S := Finset.range 10; S.card = 10 := by\n  sorry\n"
        lock = lean_verify._statement_lock(let_source)
        self.assertIn("S.card = 10", lock["signature"])
        self.assertTrue(
            lean_verify._lock_violation(lock, let_source.replace("S.card = 10", "S.card ≤ 10"))
        )
        match_source = (
            "theorem main (n : ℕ) : match n with\n  | 0 => True\n  | _ => 0 < n := by\n  sorry\n"
        )
        lock = lean_verify._statement_lock(match_source)
        self.assertIn("0 < n", lock["signature"])
        # Equation-compiler proofs still end the statement at their first case.
        cases = "theorem main : ∀ n : ℕ, n + 0 = n\n  | 0 => rfl\n  | n+1 => rfl\n"
        self.assertEqual(
            lean_verify._statement_lock(cases)["signature"], "theorem main : ∀ n : ℕ, n + 0 = n"
        )

    def test_mathlib_only_keywords_are_identifiers_in_core_files(self) -> None:
        # `lemma`/`alias` are reserved only once Mathlib is imported; in a
        # core file they may name variables and must not cut the statement.
        core = "theorem main (lemma alias : Nat) (h : lemma = alias) : alias = lemma := by\n  sorry\n"
        lock = lean_verify._statement_lock(core)
        self.assertTrue(lock["signature"].endswith("alias = lemma"))
        self.assertTrue(lean_verify._lock_violation(lock, core.replace(": alias = lemma", ": True")))
        with_mathlib = "import Mathlib\nlemma helper : True := trivial\ntheorem main : True := trivial\n"
        self.assertEqual(
            [d["kind"] for d in lean_verify._qualified_declarations(with_mathlib)], ["lemma", "theorem"]
        )

    def test_declarations_on_one_line_and_class_inductive_are_split(self) -> None:
        source = (
            "def a := 1 def b := 2\n"
            "theorem t1 : a = 1 := rfl theorem main : b = 2 := rfl\n"
            "class inductive Foo | x\nderiving instance Repr for Foo\n"
        )
        names = [(d["kind"], d["qualified"]) for d in lean_verify._qualified_declarations(source)]
        self.assertEqual(
            names,
            [("def", "a"), ("def", "b"), ("theorem", "t1"), ("theorem", "main"), ("inductive", "Foo")],
        )


class CheatRulePositionTests(unittest.TestCase):
    def test_rules_fire_after_in_prefixes_indented_and_mid_line(self) -> None:
        cases = {
            "axiom": "theorem h : 1 = 1 := rfl\n  open Nat in axiom cheat : False\ntheorem main : 1 = 2 := cheat.elim",
            "core_instance": "theorem h : 1 = 1 := rfl\n  open Nat in instance weird : Add Nat := ⟨fun a b => a * b⟩",
            "custom_syntax": "theorem h : 1 = 1 := rfl open X in macro \"c\" : tactic => `(tactic| sorry)",
            "opaque": "theorem h : 1 = 1 := rfl\n    opaque secret : Nat",
            "meta_api": "def f : Lean.Elab.Command.CommandElabM Unit := pure ()\ntheorem main : 1 = 1 := rfl",
        }
        for flag, source in cases.items():
            with self.subTest(flag=flag):
                flags = {f["id"]: f["severity"] for f in lean_verify._fidelity_flags(source)}
                self.assertEqual(flags.get(flag), "major", flags)
        registered = "@[instance] def weird : Add ℕ := ⟨fun a b => a * b⟩\ntheorem main : 1 = 1 := rfl"
        self.assertEqual(
            {f["id"]: f["severity"] for f in lean_verify._fidelity_flags(registered)}["core_instance"],
            "major",
        )


class AxiomsProbeDecoyTests(unittest.TestCase):
    DECOY = (
        "theorem helper : 1 = 1 := rfl\n"
        "  open Nat in axiom cheat : False\n"
        "theorem main : 1 = 2 := cheat.elim\n\n"
        "namespace A\ntheorem main : 2 = 2 := rfl\n"
    )

    def test_probe_is_root_anchored_and_covers_every_declaration(self) -> None:
        check, calls = _axioms_broker({"main": "cheat", "cheat": "cheat"})
        with patch.object(lean_broker, "check_source", side_effect=check):
            probe = lean_verify._axioms_probe(
                self.DECOY, uses_mathlib=False, timeout=5, main_decl="main"
            )
        lines = re.findall(r"(?m)^#print axioms (\S+)$", calls[0])
        self.assertEqual(lines[0], "_root_.«main»")
        self.assertEqual(
            sorted(lines), sorted(["_root_.«main»", "_root_.«helper»", "_root_.«cheat»", "_root_.«A».«main»"])
        )
        self.assertFalse(probe["ok"])
        self.assertEqual(probe["decl"], "main")
        self.assertEqual(probe["unexpected"], ["cheat"])
        self.assertIn("main", probe["offenders"])

    def test_a_same_named_report_cannot_stand_in_for_the_probed_name(self) -> None:
        # The file leaves `namespace A` open; if Lean answered for `A.main`
        # where `main` was probed, the reports no longer line up.
        def decoy_broker(source: str, **_kwargs) -> dict:
            check, _calls = _axioms_broker()
            result = check(source)
            result["stdout"] = result["stdout"].replace("'main' does", "'A.main' does", 1)
            return result

        with patch.object(lean_broker, "check_source", side_effect=decoy_broker):
            probe = lean_verify._axioms_probe(
                "theorem main : 1 = 1 := rfl\n", uses_mathlib=False, timeout=5
            )
        self.assertFalse(probe["ok"])
        self.assertIn("where `main` was probed", probe["error"])

    def test_output_printed_by_the_file_itself_cannot_forge_a_report(self) -> None:
        check, _calls = _axioms_broker(
            {"main": "cheat"}, preamble="'main' does not depend on any axioms"
        )
        with patch.object(lean_broker, "check_source", side_effect=check):
            probe = lean_verify._axioms_probe(
                "theorem main : 1 = 1 := rfl\n", uses_mathlib=False, timeout=5
            )
        self.assertFalse(probe["ok"])
        self.assertEqual(probe["axioms"], ["cheat"])

    def test_locked_main_must_exist_under_that_name(self) -> None:
        check, calls = _axioms_broker()
        with patch.object(lean_broker, "check_source", side_effect=check):
            probe = lean_verify._axioms_probe(
                "theorem aux : 2 = 2 := rfl\n", uses_mathlib=False, timeout=5, main_decl="main"
            )
        self.assertFalse(probe["ok"])
        self.assertIn("not found", probe["error"])
        self.assertEqual(calls, [])
        # Without a lock (manual checks) every declaration is still probed.
        with patch.object(lean_broker, "check_source", side_effect=check):
            unlocked = lean_verify._axioms_probe(
                "theorem aux : 2 = 2 := rfl\n", uses_mathlib=False, timeout=5
            )
        self.assertTrue(unlocked["ok"])
        self.assertEqual(unlocked["decl"], "aux")

    def test_private_and_primed_names_are_matched_exactly(self) -> None:
        def private_broker(source: str, **_kwargs) -> dict:
            check, _calls = _axioms_broker()
            result = check(source)
            result["stdout"] = result["stdout"].replace("'main''", "'_private.Proof.0.main''")
            return result

        with patch.object(lean_broker, "check_source", side_effect=private_broker):
            probe = lean_verify._axioms_probe(
                "private theorem main' : 1 = 1 := rfl\n", uses_mathlib=False, timeout=5, main_decl="main'"
            )
        self.assertTrue(probe["ok"], probe["error"])
        self.assertEqual(probe["decl"], "main'")

    def test_write_and_run_threads_the_locked_main_to_the_checker(self) -> None:
        seen: list = []

        def checker(ldir: Path, uses_mathlib: bool, main_decl=None) -> dict:
            seen.append(main_decl)
            return _bound_check(ldir, uses_mathlib)

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            lean_verify, "_run_lean_check", side_effect=checker
        ):
            lean_verify._write_and_run(Path(tmp), "theorem main : True := trivial\n", False, main_decl="Foo.main")
            lean_verify._write_and_run(Path(tmp), "theorem main : True := trivial\n", False)
        self.assertEqual(seen, ["Foo.main", None])


class StatementFingerprintTests(unittest.TestCase):
    SOURCE = "theorem helper : 1 = 1 := rfl\n\ntheorem main : 1 = 1 := by\n  sorry\n"

    def _broker(self, rows, *, status: str = "ok", mutate=None):
        programs: list[str] = []

        def check(program: str, **_kwargs) -> dict:
            programs.append(program)
            marker = re.search(r"agent-monitor statement fingerprint ([0-9a-f]+)", program).group(1)
            payload = json.dumps({"status": status, "rows": rows})
            result = {
                "status": "completed",
                "exit_code": 0,
                "stdout": f"Proof.lean:3:8: warning: declaration uses 'sorry'\nAGENT_MONITOR_STATEMENT::{marker}::{payload}\n",
                "stderr": "",
                "source_sha256": hashlib.sha256(program.encode()).hexdigest(),
                "sandbox": lean_broker.SANDBOX_MARKER,
                "stdout_truncated": False,
                "stderr_truncated": False,
            }
            if mutate:
                mutate(result)
            return result

        return check, programs

    def test_program_and_row_order_independent_fingerprint(self) -> None:
        rows = [
            {"n": "main", "k": "theorem", "th": "1", "ts": "2", "u": ""},
            {"n": "myset", "k": "def", "th": "3", "ts": "4", "u": "", "vh": "5", "vs": "6"},
        ]
        check, programs = self._broker(rows)
        with patch.object(lean_broker, "check_source", side_effect=check):
            first = lean_verify._statement_fingerprint(self.SOURCE, uses_mathlib=False, main_decl="main")
        check, _ = self._broker(list(reversed(rows)))
        with patch.object(lean_broker, "check_source", side_effect=check):
            second = lean_verify._statement_fingerprint(self.SOURCE, uses_mathlib=False, main_decl="main")
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        program = programs[0]
        self.assertTrue(program.startswith("import Lean\n" + self.SOURCE.rstrip()))
        self.assertIn('(_root_.Lean.Name.mkStr _root_.Lean.Name.anonymous "main")', program)
        changed = [dict(rows[0]), dict(rows[1], vh="99")]
        check, _ = self._broker(changed)
        with patch.object(lean_broker, "check_source", side_effect=check):
            third = lean_verify._statement_fingerprint(self.SOURCE, uses_mathlib=False, main_decl="main")
        self.assertNotEqual(first["fingerprint"], third["fingerprint"])

    def test_unconfirmed_fingerprints_never_match(self) -> None:
        main_row = {"n": "main", "k": "theorem", "th": "1", "ts": "2", "u": ""}
        broken = {
            "truncated closure": self._broker([main_row], status="truncated")[0],
            "missing constant": self._broker([main_row, {"n": "x", "k": "missing"}])[0],
            "no main row": self._broker([{"n": "helper", "k": "theorem"}])[0],
            "untrusted sandbox": self._broker([main_row], mutate=lambda r: r.update(sandbox="local"))[0],
            "compile error": self._broker([main_row], mutate=lambda r: r.update(exit_code=1))[0],
            "no report": self._broker([main_row], mutate=lambda r: r.update(stdout=""))[0],
        }
        for label, check in broken.items():
            with self.subTest(label=label), patch.object(lean_broker, "check_source", side_effect=check):
                self.assertFalse(
                    lean_verify._statement_fingerprint(self.SOURCE, uses_mathlib=False, main_decl="main")["ok"]
                )
        with patch.object(lean_broker, "check_source", side_effect=lean_broker.LeanBrokerUnavailable("down")):
            self.assertFalse(
                lean_verify._statement_fingerprint(self.SOURCE, uses_mathlib=False, main_decl="main")["ok"]
            )

    def test_lean_detected_drift_rejects_a_compiled_candidate_the_text_lock_passed(self) -> None:
        # The helper's statement changes what `main` means (e.g. `main` names
        # `Classical.choose helper`); the text lock lets helpers change, but
        # Lean's fingerprint of main's statement does not match.
        sneaky = PROVED.replace("/-- The claim.", "lemma helper : 2 = 2 := rfl\n\n/-- The claim.")

        def fingerprint(source: str, main_decl: str) -> dict:
            return {"ok": True, "fingerprint": "drifted" if "helper : 2 = 2" in source else "locked"}

        run = _GenerateHarness(
            self,
            [_fenced(SKELETON), _fenced(sneaky), _fenced(PROVED)],
            ["ok", "ok", "ok"],
            fingerprint=fingerprint,
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            result, info = run.run(workspace, max_repairs=2)
            on_disk = (workspace / "lean" / "Proof.lean").read_text(encoding="utf-8")
        actions = [a["action"] for a in result["attempts"]]
        self.assertEqual(actions, ["statement", "prove", "lock-violation", "sorry-repair"])
        self.assertEqual(result["status"], "verified")
        self.assertNotIn("helper : 2 = 2", result["lean"])
        self.assertEqual(on_disk, result["lean"])
        self.assertIn("Lean elaborates the statement", run.prompts[2])
        self.assertEqual(info["audit_calls"], 1)
        self.assertEqual(result["fidelity"]["audit"]["statement_fingerprint"], "locked")
        self.assertEqual(run.main_decls, ["main", "main", "main"])

    def test_drifted_candidate_is_never_the_final_artifact(self) -> None:
        sneaky = PROVED.replace("/-- The claim.", "lemma helper : 2 = 2 := rfl\n\n/-- The claim.")
        run = _GenerateHarness(
            self,
            [_fenced(SKELETON), _fenced(sneaky)],
            ["ok", "ok"],
            fingerprint=lambda source, _main: {
                "ok": True, "fingerprint": "drifted" if "helper : 2 = 2" in source else "locked"
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            result, _info = run.run(workspace, max_repairs=0)
            on_disk = (workspace / "lean" / "Proof.lean").read_text(encoding="utf-8")
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("  sorry", result["lean"])
        self.assertEqual(on_disk, result["lean"])
        self.assertIsNone(result["verified_source_sha256"])

    def test_unconfirmed_statement_gets_a_fresh_final_audit(self) -> None:
        run = _GenerateHarness(
            self,
            [_fenced(SKELETON), _fenced(PROVED)],
            ["ok", "ok"],
            fingerprint=lambda _source, _main: {"ok": False, "error": "probe failed"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            result, info = run.run(Path(tmp))
        self.assertEqual(result["status"], "verified")
        self.assertEqual(info["audit_calls"], 2)
        self.assertEqual(result["fidelity"]["audit"]["scope"], "final")
        self.assertIsNone(result["statement_lock"]["fingerprint"])
        self.assertIn("audited again", result["notes"])


class PartialProgressRegressionTests(unittest.TestCase):
    def test_statement_audit_outage_keeps_the_checked_skeleton(self) -> None:
        run = _GenerateHarness(
            self,
            [_fenced(SKELETON), _fenced(PROVED)],
            ["ok", "ok"],
            audits=[AUDIT_OK],
        )
        outcomes = [TimeoutError("The read operation timed out"), dict(AUDIT_OK)]

        def audit(*_args, **_kwargs):
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(lean_verify, "_audit_faithfulness", side_effect=audit),
        ):
            workspace = Path(tmp)
            (workspace / "problem.txt").write_text("Prove n + 0 = n.", encoding="utf-8")
            with (
                patch("agent_monitor.settings.resolved_user_env", return_value={}),
                patch.object(lean_verify, "_proof_source", return_value=("proof.md", "By simp.")),
                patch.object(lean_verify, "_chat", side_effect=run.chat),
                patch.object(lean_verify, "_run_lean_check", side_effect=run.check),
                patch.object(lean_verify, "_statement_fingerprint", side_effect=run.fake_fingerprint),
                patch.object(lean_verify, "toolchain_status", return_value={"available": True}),
            ):
                result = lean_verify.generate(
                    workspace=workspace, run_record={}, user={"id": 7}, model="gpt-test"
                )
            cached = lean_verify.load_cached(workspace)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(cached["status"], "verified")
        self.assertIn("statement-audit", [a["action"] for a in result["attempts"]])
        # The failed pre-proof audit is not reused: the final file is audited.
        self.assertEqual(result["fidelity"]["audit"]["scope"], "final")
        self.assertTrue(result["fidelity"]["audit"]["faithful"])

    def test_broker_lost_mid_run_restores_the_best_checked_candidate(self) -> None:
        run = _GenerateHarness(self, [_fenced(SKELETON), _fenced(PROVED)], ["ok", "missing"])
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            result, _info = run.run(workspace, max_repairs=3)
            on_disk = (workspace / "lean" / "Proof.lean").read_text(encoding="utf-8")
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("  sorry", result["lean"])
        self.assertEqual(on_disk, result["lean"])
        self.assertIsNotNone(result["checked_source_sha256"])
        self.assertIsNotNone(result["kernel_receipt"])
        self.assertIn("became unavailable during the run", result["notes"])
        self.assertNotIn("nothing was compiled", result["notes"])
        self.assertEqual(len(run.prompts), 2)  # no repair spent on the outage

    def test_unfaithful_file_with_sorry_is_bound_as_checked_not_verified(self) -> None:
        source = "theorem main : (0 : Nat) = 0 := by\n  sorry\n"
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "lean").mkdir()
            (workspace / "lean" / "Proof.lean").write_text(source, encoding="utf-8")
            check = _bound_check(workspace / "lean", False)
            with patch.object(lean_verify, "toolchain_status", return_value={"available": True}):
                result = lean_verify._persist(
                    workspace, lean_src=source, status="unfaithful", uses_mathlib=False,
                    attempts=[], authoritative_check=check,
                )
        self.assertEqual(result["status"], "unfaithful")
        self.assertIsNone(result["verified_source_sha256"])
        self.assertEqual(result["checked_source_sha256"], hashlib.sha256(source.encode()).hexdigest())
        summary = lean_verify._check_summary(source, check, "unfaithful")
        self.assertIn("1 sorry/admit remaining", summary)
        self.assertNotIn("Lean accepted the file", summary)

    def test_audit_provider_errors_are_reported_not_raised(self) -> None:
        for error in (TimeoutError("The read operation timed out"), ConnectionResetError("reset")):
            with self.subTest(error=type(error).__name__), patch.object(
                lean_verify, "_chat", side_effect=error
            ):
                audit = lean_verify._audit_faithfulness(
                    {}, "gpt-test", problem="p", proof="q", lean="theorem main : True := trivial"
                )
            self.assertEqual(audit["ok"], False)
            self.assertTrue(audit["error"])


class LeanFenceDocstringTests(unittest.TestCase):
    def test_markdown_example_inside_a_doc_comment_does_not_truncate_the_file(self) -> None:
        reply = (
            "```lean\nimport Mathlib\n\n/-- Example usage:\n```\n"
            "example : double 2 = 4 := rfl\n```\nMore docs. -/\n"
            "def double (n : ℕ) : ℕ := 2 * n\n\n"
            "theorem main : double 2 = 4 := by\n  sorry\n```\n"
            "```json\n{\"main_decl\": \"main\", \"notes\": \"doc fence\"}\n```"
        )
        payload = lean_verify._parse_model_payload(reply)
        self.assertIn("def double", payload["lean"])
        self.assertTrue(payload["lean"].rstrip().endswith("sorry"))
        self.assertEqual(payload["notes"], "doc fence")
        self.assertEqual(payload["main_decl"], "main")

    def test_unterminated_comment_falls_back_to_the_first_fence(self) -> None:
        reply = "```lean\ntheorem main : True := trivial\n/- oops\n```\n```json\n{\"notes\": \"x\"}\n```"
        payload = lean_verify._parse_model_payload(reply)
        self.assertEqual(payload["lean"].strip(), "theorem main : True := trivial\n/- oops")
        self.assertEqual(payload["notes"], "x")


class SorryTaintTruncationTests(unittest.TestCase):
    SOURCE = "theorem big : 1 = 1 := by\n  sorry\n\ntheorem main : 1 = 1 := big\n"

    def test_truncated_dependencies_fail_closed_for_sorry_sources(self) -> None:
        from agent_monitor import lean_kg

        many = [f"Zeta.lemma{i:04d}" for i in range(lean_kg.MAX_DEPENDENCIES + 5)]
        rows = [
            {
                "name": "big", "kind": "theorem", "statementDeps": [],
                "proofDeps": many + ["sorryAx"], "statementTruncated": False, "proofTruncated": False,
            },
            {
                "name": "main", "kind": "theorem", "statementDeps": [],
                "proofDeps": ["Other.x"], "statementTruncated": False, "proofTruncated": True,
            },
        ]
        declarations = {d["qualified_name"]: d for d in lean_kg._normalize(self.SOURCE, rows)}
        # `sorryAx` survives the dependency cap.
        self.assertIn("sorryAx", declarations["big"]["proof_dependencies"])
        self.assertTrue(declarations["big"]["uses_sorry"])
        # Lean-side truncation with no visible sorryAx: unknown, so marked.
        self.assertTrue(declarations["main"]["uses_sorry"])
        clean = {
            d["qualified_name"]: d
            for d in lean_kg._normalize(self.SOURCE, rows[1:], may_use_sorry=False)
        }
        self.assertFalse(clean["main"]["uses_sorry"])
