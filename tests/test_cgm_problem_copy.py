"""A THEOREM's Statement copies the problem verbatim; the problem's own lines
are never read as the reply's markers.

A user may send a theorem with a proof to complete or check (a "Proof." line),
or text containing a block marker ("=== END ===").  The worker contract asks
for the problem verbatim in the THEOREM's Statement, so the parser skips that
copy before it looks for the proof and for the block's end.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from agent_monitor.cgm import parsing as cgm_parsing
from agent_monitor.runners import math_harness_runner as runner

from tests.test_math_harness_runner import _api_run, _curator, _ok, _receipt, _strategist, _wrong

PROOF_TO_COMPLETE = (
    "Theorem. For every integer n, n^2 + n is even.\n"
    "Proof. Write n^2 + n = n(n + 1).\n"
    "Complete the proof above."
)
WORKER_PROOF = (
    "One of the consecutive integers n and n + 1 is even, so their product "
    "n(n + 1) = n^2 + n is even."
)
STATUS = "\n\n=== STATUS ===\nsolved\n=== END ==="
# Problems whose THEOREM statement the parser keeps trimmed, with a worker
# reply's Statement part for each ("{problem}" is the problem verbatim).
TRIMMED_STATEMENTS = {
    "trailing proof line": (
        "Theorem A. Every integer is an integer.\nProof. Trivial.\n"
        "Theorem B. n^2 + n is even for every integer n.\nProof. Write n^2 + n = n(n + 1).",
        "Statement:\n{problem}\nOne of n and n + 1 is even, so the product n(n + 1) is even.",
    ),
    "bold first line": (
        "**Theorem.** Every even integer squared is even.\nProof. Left to the reader.",
        "Statement: **Theorem.** Every even integer squared is even.\nProof. Left to the reader.\n"
        "Proof:\nIf n = 2k for an integer k, then n^2 = 2(2k^2) is even.",
    ),
}


def _theorem(statement_lines: str, proof: str = WORKER_PROOF, *, marker: str | None = "Proof:") -> str:
    proof_part = f"{marker}\n{proof}" if marker else proof
    return f"=== THEOREM ===\nUses: none\nStatement:\n{statement_lines}\n{proof_part}\n=== END ==={STATUS}"


class ProblemCopyTests(unittest.TestCase):
    def test_a_proof_line_of_the_problem_is_not_the_reply_marker(self) -> None:
        text = _theorem(PROOF_TO_COMPLETE)
        candidate = cgm_parsing.parse_worker(text, problem=PROOF_TO_COMPLETE)
        self.assertEqual(candidate.theorem.proof, WORKER_PROOF)
        self.assertEqual(candidate.theorem.statement, PROOF_TO_COMPLETE)
        # Read without the problem, the problem's own line opened the proof.
        before = cgm_parsing.parse_worker(text)
        self.assertTrue(before.theorem.proof.startswith("Write n^2 + n = n(n + 1).\nComplete the proof above."))

    def test_an_end_marker_in_the_problem_does_not_end_the_block(self) -> None:
        problem = "Consider the text below.\n=== END ===\nShow that 1 + 1 = 2 in the integers."
        proof = "By the definition 2 = 1 + 1 of the integer 2, the sum 1 + 1 equals 2."
        text = _theorem(problem, proof)
        with self.assertRaises(cgm_parsing.ParseError):
            cgm_parsing.parse_worker(text)
        candidate = cgm_parsing.parse_worker(text, problem=problem)
        self.assertEqual(candidate.theorem.proof, proof)
        self.assertEqual(candidate.theorem.statement, problem)
        self.assertEqual(candidate.status, "solved")

    def test_a_block_header_in_the_problem_is_not_a_new_block(self) -> None:
        problem = "=== LEMMA L9 ===\nShow that 2 + 2 = 4 in the integers."
        proof = "Since 2 + 2 = (1 + 1) + (1 + 1) = 4 by associativity, the claim holds."
        candidate = cgm_parsing.parse_worker(_theorem(problem, proof), problem=problem)
        self.assertEqual(candidate.lemmas, ())
        self.assertEqual(candidate.theorem.proof, proof)

    def test_a_problem_ending_with_proof_whose_line_the_reply_reused(self) -> None:
        # The reply wrote the proof right after the copied "Proof:", which is
        # its marker too; a "Proof." inside the proof must not become it.
        problem = "Prove that n^2 + n is even for every integer n.\nProof:"
        proof = (
            "Claim. n(n + 1) is even.\nProof. One of n and n + 1 is even, so the product is even.\n"
            "Hence n^2 + n = n(n + 1) is even."
        )
        text = _theorem(problem, proof, marker=None)
        candidate = cgm_parsing.parse_worker(text, problem=problem)
        self.assertEqual(candidate.theorem.proof, proof)
        self.assertEqual(candidate.theorem.proof, cgm_parsing.parse_worker(text).theorem.proof)

    def test_a_problem_ending_with_proof_and_the_reply_marker_after_it(self) -> None:
        problem = "Prove that n^2 + n is even for every integer n.\nProof:"
        text = _theorem(problem)
        candidate = cgm_parsing.parse_worker(text, problem=problem)
        self.assertEqual(candidate.theorem.proof, WORKER_PROOF)
        self.assertEqual(candidate.theorem.statement, problem)
        # Before, the reply's own marker line was left at the top of the proof.
        self.assertEqual(cgm_parsing.parse_worker(text).theorem.proof, f"Proof:\n{WORKER_PROOF}")

    def test_the_copy_may_start_on_the_statement_line_and_differ_in_spacing(self) -> None:
        text = (
            "=== THEOREM ===\nUses: none\n"
            "Statement: Theorem.  For every integer n,\tn^2 + n is even.\n\n"
            "   Proof. Write   n^2 + n = n(n + 1).   \n"
            "Complete the proof above.\n\n"
            f"**Proof.**\n{WORKER_PROOF}\n=== END ==={STATUS}"
        )
        candidate = cgm_parsing.parse_worker(text, problem=PROOF_TO_COMPLETE)
        self.assertEqual(candidate.theorem.proof, WORKER_PROOF)

    def test_a_bold_first_line_is_still_a_copy(self) -> None:
        problem = "**Theorem.** Every even integer squared is even.\nProof. Left to the reader."
        proof = "If n = 2k for an integer k, then n^2 = 2(2k^2) is even."
        text = (
            "=== THEOREM ===\nUses: none\n"
            "Statement: **Theorem.** Every even integer squared is even.\nProof. Left to the reader.\n"
            f"Proof:\n{proof}\n=== END ==={STATUS}"
        )
        self.assertEqual(cgm_parsing.parse_worker(text, problem=problem).theorem.proof, proof)

    def test_a_reply_that_does_not_copy_the_problem_is_read_as_before(self) -> None:
        paraphrase = "For all integers n, n^2 + n is even.\nProof. Write n^2 + n = n(n + 1)."
        text = _theorem(paraphrase)
        self.assertEqual(
            cgm_parsing.parse_worker(text, problem=PROOF_TO_COMPLETE),
            cgm_parsing.parse_worker(text),
        )

    def test_lemmas_are_read_as_before(self) -> None:
        text = (
            "=== LEMMA L1 ===\nUses: none\nStatement:\nTheorem. For every integer n, n^2 + n is even.\n"
            f"Proof:\n{WORKER_PROOF}\n=== END ===\n\n" + _theorem(PROOF_TO_COMPLETE).replace("Uses: none", "Uses: L1")
        )
        candidate = cgm_parsing.parse_worker(text, problem=PROOF_TO_COMPLETE)
        self.assertEqual([lemma.proof for lemma in candidate.lemmas], [WORKER_PROOF])
        self.assertEqual(candidate.theorem.proof, WORKER_PROOF)

    def test_a_long_problem_shown_to_a_repair_as_an_excerpt(self) -> None:
        filler = "\n".join(f"Remark {index}: the integers are closed under addition." for index in range(700))
        problem = f"{PROOF_TO_COMPLETE}\n{filler}\nThis is the end of the problem."
        size = len(problem.encode())
        self.assertTrue(runner._REPAIR_PROBLEM_BYTES < size <= runner.MAX_PROBLEM_BYTES, size)
        for shown in (problem, runner._clip_utf8(problem, runner._REPAIR_PROBLEM_BYTES)):
            with self.subTest(excerpt=shown != problem):
                candidate = cgm_parsing.parse_worker(_theorem(shown), problem=runner._problem_forms(problem))
                self.assertEqual(candidate.theorem.proof, WORKER_PROOF)

    def test_lines_between_the_copy_and_the_marker_read_as_before(self) -> None:
        # The reply's own marker must follow the copy; otherwise (here a reply
        # with no top-level marker, whose claim has an inner "Proof.") the
        # reply is read exactly as before.
        text = _theorem(
            PROOF_TO_COMPLETE,
            "We show that n(n + 1) is even.\nClaim. Among any two consecutive integers one is even.\n"
            "Proof. If n is odd then n + 1 is even; otherwise n is even.\nBy the claim the product is even.",
            marker=None,
        )
        self.assertEqual(
            cgm_parsing.parse_worker(text, problem=PROOF_TO_COMPLETE), cgm_parsing.parse_worker(text),
        )

    def test_a_copy_that_is_not_used_keeps_the_block_boundaries(self) -> None:
        # The problem is itself a finished block; the reply wrote no proof of
        # its own after the copy, so the copy is not used, nor shielded.
        proof = "By the definition 2 = 1 + 1 of the integer 2, the sum 1 + 1 equals 2."
        problem = f"Show that 1 + 1 = 2 in the integers.\nProof:\n{proof}\n=== END ==="
        text = f"=== THEOREM ===\nUses: none\nStatement:\n{problem}\n{STATUS}"
        candidate = cgm_parsing.parse_worker(text, problem=problem)
        self.assertEqual(candidate.theorem.proof, proof)
        self.assertEqual(candidate, cgm_parsing.parse_worker(text))

    def test_a_code_fence_first_line_on_the_statement_line(self) -> None:
        problem = "```\ntheorem t : 1 + 1 = 2 := by\n  sorry\n```\nProof. Replace sorry with a proof."
        proof = "The goal 1 + 1 = 2 holds by norm_num, which closes the arithmetic goal."
        text = (
            "=== THEOREM ===\nUses: none\nStatement: ```\ntheorem t : 1 + 1 = 2 := by\n  sorry\n```\n"
            f"Proof. Replace sorry with a proof.\nProof:\n{proof}\n=== END ==={STATUS}"
        )
        self.assertEqual(cgm_parsing.parse_worker(text, problem=problem).theorem.proof, proof)

    def test_one_problem_of_a_session_notebook(self) -> None:
        notebook = f"## Problem 1\nProve that the sum of two odd integers is even.\n\n## Problem 2\n{PROOF_TO_COMPLETE}\n"
        forms = runner._problem_forms(notebook)
        for copied in (PROOF_TO_COMPLETE, f"## Problem 2\n{PROOF_TO_COMPLETE}", notebook.strip()):
            with self.subTest(copied=copied[:14]):
                candidate = cgm_parsing.parse_worker(_theorem(copied), problem=forms)
                self.assertEqual(candidate.theorem.proof, WORKER_PROOF)

    def test_a_problem_that_begins_with_an_earlier_one(self) -> None:
        # Problem 2 re-sends problem 1 with a proof to complete: the one-line
        # form of problem 1 is a prefix of the copy, and must not win.
        first = "Theorem. For every integer n, n^2 + n is even."
        notebook = f"## Problem 1\n{first}\n\n## Problem 2\n{PROOF_TO_COMPLETE}\n"
        forms = runner._problem_forms(notebook)
        for copied in (PROOF_TO_COMPLETE, f"## Problem 2\n{PROOF_TO_COMPLETE}", notebook.strip(), first):
            with self.subTest(copied=copied[:14]):
                candidate = cgm_parsing.parse_worker(_theorem(copied), problem=forms)
                self.assertEqual(candidate.theorem.proof, WORKER_PROOF)

    def test_many_forms_or_a_long_reply_stay_fast(self) -> None:
        sections, number = [], 1
        while len("\n\n".join(sections).encode()) < 43_000:  # a crafted notebook: thousands of forms
            sections.append(f"## Problem {number}\nx{number}")
            number += 1
        crafted = runner._problem_forms("\n\n".join(sections))
        realistic = runner._problem_forms("\n\n".join(
            f"## Problem {k}\n" + "Let G be a finite group of order n and p a prime dividing n. " * 14
            for k in range(1, 31)
        ))
        tail = f"Some text.\nProof:\n{WORKER_PROOF}\n=== END ==={STATUS}"
        replies = (
            "=== THEOREM ===\nUses: none\nStatement:\n" + "\n" * 150_000 + tail,
            "=== THEOREM ===\nUses: none\nStatement:\n" + "word " * 30_000 + "\n" + tail,
        )
        for forms in (crafted, realistic):
            for reply in replies:
                started = time.monotonic()
                candidate = cgm_parsing.parse_worker(reply, problem=forms)
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertEqual(candidate.theorem.proof, WORKER_PROOF)

    def test_a_repair_that_copies_the_shown_candidate(self) -> None:
        # The parser can trim the problem's own last "Proof." line, or a first
        # line's leading "**", from the statement it keeps, and the repair
        # prompt shows the candidate as parsed: a repair that copies that
        # statement is read after it too.
        new_proof = "Since n and n + 1 are consecutive, one of them is even; hence their product is even."
        for name, (problem, body) in TRIMMED_STATEMENTS.items():
            with self.subTest(name):
                worker = f"=== THEOREM ===\nUses: none\n{body.format(problem=problem)}\n=== END ==={STATUS}"
                candidate = cgm_parsing.parse_worker(worker, problem=problem)
                self.assertNotEqual(candidate.theorem.statement, problem)  # what the parser keeps
                repaired = cgm_parsing.render_candidate(candidate).replace(candidate.theorem.proof, new_proof)
                forms = runner._repair_forms(problem, candidate)
                self.assertEqual(cgm_parsing.parse_worker(repaired, problem=forms).theorem.proof, new_proof)
                # Only the problem's own forms: the old reading, from the problem's first "Proof.".
                old = cgm_parsing.parse_worker(repaired, problem=runner._problem_forms(problem))
                self.assertNotEqual(old.theorem.proof, new_proof)

    def test_a_self_similar_problem_stays_fast(self) -> None:
        problem = "=== THEOREM ===\nStatement:\n" * 1625 + "Z"
        reply = "=== THEOREM ===\nStatement:\n" * 5900
        started = time.monotonic()
        with self.assertRaises(cgm_parsing.ParseError):
            cgm_parsing.parse_worker(reply, problem=runner._problem_forms(problem))
        self.assertLess(time.monotonic() - started, 2.0)


class RunnerProblemCopyTests(unittest.TestCase):
    def test_a_run_on_a_proof_to_complete_publishes_the_worker_proof(self) -> None:
        outputs = [_strategist(1), _theorem(PROOF_TO_COMPLETE), _ok(), _curator()]
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            code, calls, _events = _api_run(
                workspace, outputs, task=PROOF_TO_COMPLETE,
                env={"AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1"},
            )
            self.assertEqual(code, 0)
            self.assertEqual(_receipt(workspace)["verdict"], "accepted")
            proof_md = (workspace / "proof.md").read_text(encoding="utf-8")
        # The verifier judged the worker's proof, and the published proof
        # states the problem once (before, its tail opened the proof).
        verify = calls[2]["user"]
        self.assertIn(WORKER_PROOF, verify)
        self.assertEqual(verify.count("Complete the proof above."), 1)
        self.assertEqual(proof_md.count("Complete the proof above."), 1)
        self.assertIn(WORKER_PROOF, proof_md)

    def test_a_repair_of_a_proof_to_complete(self) -> None:
        repaired = "Among n and n + 1 one is even, hence n(n + 1) = n^2 + n is even, as required."
        outputs = [
            _strategist(1), _theorem(PROOF_TO_COMPLETE), _wrong(),
            _theorem(PROOF_TO_COMPLETE, repaired), _ok(), _curator(),
        ]
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            code, calls, _events = _api_run(
                workspace, outputs, task=PROOF_TO_COMPLETE,
                env={"AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1"},
            )
            self.assertEqual(code, 0)
            self.assertEqual(_receipt(workspace)["verdict"], "accepted")
            proof_md = (workspace / "proof.md").read_text(encoding="utf-8")
        # The repair is shown the problem as the THEOREM's statement, and its
        # reply is read after the copy: the second verifier sees its proof only.
        self.assertIn(f"Statement:\n{PROOF_TO_COMPLETE}\nProof:\n{WORKER_PROOF}", calls[3]["user"])
        verify = calls[4]["user"]
        self.assertIn(repaired, verify)
        self.assertEqual(verify.count("Complete the proof above."), 1)
        self.assertIn(repaired, proof_md)
        self.assertEqual(proof_md.count("Complete the proof above."), 1)

    def test_a_repair_that_copies_a_trimmed_statement(self) -> None:
        problem, body = TRIMMED_STATEMENTS["trailing proof line"]
        worker = f"=== THEOREM ===\nUses: none\n{body.format(problem=problem)}\n=== END ==={STATUS}"
        candidate = cgm_parsing.parse_worker(worker, problem=problem)
        new_proof = "Among n and n + 1 one is even, hence n(n + 1) = n^2 + n is even, as required."
        repair = cgm_parsing.render_candidate(candidate).replace(candidate.theorem.proof, new_proof)
        outputs = [_strategist(1), worker, _wrong(), repair, _ok(), _curator()]
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            code, calls, _events = _api_run(
                workspace, outputs, task=problem, env={"AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1"},
            )
            self.assertEqual(code, 0)
            self.assertEqual(_receipt(workspace)["verdict"], "accepted")
        # The repair prompt showed the candidate as parsed; the second
        # verifier sees the repair's proof, not "Trivial." and the rest.
        self.assertIn(cgm_parsing.render_candidate(candidate), calls[3]["user"])
        self.assertIn(new_proof, calls[4]["user"])
        self.assertEqual(calls[4]["user"].count("Trivial."), 1)

    def test_a_run_on_a_problem_with_an_end_marker_is_accepted(self) -> None:
        problem = "Consider the text below.\n=== END ===\nShow that 1 + 1 = 2 in the integers."
        proof = "By the definition 2 = 1 + 1 of the integer 2, the sum 1 + 1 equals 2."
        outputs = [_strategist(1), _theorem(problem, proof), _ok(), _curator()]
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            code, _calls, _events = _api_run(
                workspace, outputs, task=problem, env={"AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1"},
            )
            self.assertEqual(code, 0)
            self.assertEqual(_receipt(workspace)["verdict"], "accepted")


if __name__ == "__main__":
    unittest.main()
