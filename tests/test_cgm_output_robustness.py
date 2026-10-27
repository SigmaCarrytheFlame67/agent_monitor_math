"""Ansatz CGM reads unusual model output without failing the run.

Found by fuzzing the engine end to end (2026-10-27): reply shapes the parser
did not foresee raised TypeError/ValueError and ended the run with a Python
traceback; a lone UTF-16 surrogate failed the run when its text was encoded;
a duplicated key in a verifier's JSON hid the errors it listed; a long run of
spaces in a worker line froze the runner (Stop included) in regex
backtracking; "Subgoals:" and "Proof (sketch):" lines made a whole worker
reply unparsable (3 of 54 field-test runs); a curator citing more than 64
nodes broke the Explore graph; U+2028 inside a reply dropped its Monitor
event.  Offline.
"""
from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import engine_graph
from agent_monitor.cgm import curation as cgm_curation
from agent_monitor.cgm import parsing as cgm_parsing
from agent_monitor.cgm.parsing import ParseError
from agent_monitor.runners import math_harness_runner as runner


def _within(seconds: float, function, *args, **kwargs):
    """Run ``function`` and fail if it takes longer than ``seconds``."""
    outcome: dict = {}

    def target() -> None:
        try:
            outcome["value"] = function(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            outcome["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    started = time.monotonic()
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise AssertionError(f"still running after {seconds} s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value"), time.monotonic() - started


_PROOF = "A complete argument that checks every case of the statement in turn."


class WorkerParsingTests(unittest.TestCase):
    def test_plural_subgoals_and_qualified_proof_lines_parse(self) -> None:
        reply = (
            "=== LEMMA L1 ===\nSubgoals: S1, S2\nUses: none\nStatement (informal):\nAn auxiliary claim.\n"
            "Proof (self-derived):\n" + _PROOF + "\n=== END ===\n\n"
            "=== THEOREM ===\nUses: L1\nStatement:\nThe claim.\n**Proof (sketch).** " + _PROOF + "\n=== END ===\n\n"
            "=== STATUS ===\nsolved\n=== END ==="
        )
        candidate = cgm_parsing.parse_worker(reply)
        blocks = [*candidate.lemmas, candidate.theorem]
        self.assertEqual([block.label for block in blocks], ["L1", "THEOREM"])
        self.assertTrue(all(_PROOF in block.proof for block in blocks))
        self.assertEqual(candidate.lemmas[0].subgoal, "S1")
        # A long qualifier on one line (a field-test reply's ran past 120 characters).
        long_one = reply.replace("**Proof (sketch).**", "Proof (" + "structure of the computation " * 10 + "):")
        self.assertIn(_PROOF, cgm_parsing.parse_worker(long_one).theorem.proof)

    def test_a_proof_header_whose_qualifier_wraps_starts_the_proof(self) -> None:
        reply = ("=== THEOREM ===\nUses: none\nStatement:\nThe claim.\n"
                 "Proof (structure of the computation,\nstep by step): " + _PROOF + "\n=== END ===\n\n"
                 "=== STATUS ===\nsolved\n=== END ===")
        theorem = cgm_parsing.parse_worker(reply).theorem
        self.assertEqual(theorem.statement, "The claim.")
        self.assertTrue(theorem.proof.startswith("(structure of the computation,"))

    def test_long_whitespace_runs_parse_in_linear_time(self) -> None:
        # Each took minutes (cubic backtracking).  The regex engine holds the
        # GIL, so no thread can time it out: a child process with a hard
        # timeout runs them.
        program = (
            "from agent_monitor.cgm import parsing\n"
            "spaces = ' ' * 150_000\n"
            f"proof = {_PROOF!r}\n"
            "for line in (f'=== THEOREM{spaces}(continued)', f'Proof{spaces}x', f'Statement{spaces}x',\n"
            "             f'Evidence{spaces}x', f'Uses{spaces}x', f'Claim{spaces}x', f'Subgoals{spaces}x'):\n"
            "    reply = '=== THEOREM ===\\nUses: none\\nStatement:\\nThe claim.\\nProof:\\n' + proof + '\\n' + line\n"
            "    try:\n"
            "        parsing.parse_worker(reply)\n"
            "    except parsing.ParseError:\n"
            "        pass\n"
            "print('done')\n"
        )
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, timeout=60,
                                env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(root)}, cwd=root, check=False)
        self.assertEqual(result.stdout.strip(), "done", result.stderr[-1000:])


class JsonReplyTests(unittest.TestCase):
    def test_a_repeated_key_in_a_verdict_never_hides_its_errors(self) -> None:
        reply = json.dumps({"verdict": "correct", "repair_hints": ""})[:-1] + (
            ', "verification_report": {"summary": "The induction step is not justified.", '
            '"critical_errors": [{"location": "THEOREM", "issue": "Step 2 fails."}], "gaps": [], '
            '"critical_errors": []}}'
        )
        with self.assertRaises(ParseError):
            cgm_parsing.parse_verifier(reply)
        # Without the repeated key the same report is read as it is: wrong.
        clean = reply.replace(', "critical_errors": []}}', "}}")
        self.assertEqual(cgm_parsing.parse_verifier(clean).verdict, "wrong")

    def test_a_scalar_where_a_list_belongs_is_ignored(self) -> None:
        for value in (True, 2, 1.5, "none"):
            with self.subTest(value=value):
                strategy = cgm_parsing.parse_strategist(
                    json.dumps({"immediate_conclusions": value, "plans": [{"approach": "Do it directly."}]}),
                    max_plans=2,
                )
                self.assertEqual(strategy.conclusions, ())

    def test_an_overlong_integer_is_an_unusable_object_not_a_crash(self) -> None:
        aside = '{"checked_value": ' + "9" * 4_400 + "}"
        plans = json.dumps({"plans": [{"approach": "Do it directly."}]})
        self.assertEqual(len(cgm_parsing.parse_strategist(aside + "\n" + plans, max_plans=2).plans), 1)
        with self.assertRaises(ParseError):
            cgm_parsing.parse_verifier('{"verdict": "correct", "n": ' + "9" * 4_400 + "}")

    def test_a_lone_surrogate_becomes_a_replacement_character(self) -> None:
        reply = '{"plans": [{"approach": "Use \\ud835 directly."}]}'
        approach = cgm_parsing.parse_strategist(reply, max_plans=2).plans[0].approach
        self.assertEqual(approach, "Use � directly.")
        approach.encode("utf-8")


class RunnerTests(unittest.TestCase):
    def test_any_parser_failure_is_an_unparsable_reply(self) -> None:
        reply = runner.Reply(text="{}", usage={}, provider="openai", model="m", effort=None, turn_index=0) \
            if "effort" in runner.Reply.__dataclass_fields__ else None
        if reply is None:
            fields = {name: None for name in runner.Reply.__dataclass_fields__}
            reply = runner.Reply(**{**fields, "text": "{}", "usage": {}})
        for error in (TypeError("x"), ValueError("x"), OverflowError("x"), RecursionError("x")):
            with self.subTest(error=type(error).__name__), self.assertRaises(ParseError):
                runner._parse_reply(reply, limit=1000, parse=lambda _text, error=error: (_ for _ in ()).throw(error))

    def test_a_run_whose_replies_carry_lone_surrogates_finishes(self) -> None:
        from tests.test_math_harness_runner import _accepted_outputs, _api_run

        outputs = [
            text.replace("Route 1", "Route \ud83d 1") if index == 0 else text + "\n\ud83d"
            for index, text in enumerate(_accepted_outputs())
        ]
        with tempfile.TemporaryDirectory() as raw:
            code, _calls, events = _api_run(Path(raw), outputs)
            self.assertEqual(code, 0)
            json.dumps(events, ensure_ascii=False).encode("utf-8")
            (Path(raw) / "proof.md").read_text(encoding="utf-8")

    def test_an_unverified_header_stays_one_literal_line(self) -> None:
        issue = SimpleIssue(location="THEOREM", issue="Line one.\n\n# Heading\n```\nunclosed" + " word" * 200)
        report = SimpleReport(critical_errors=[issue], gaps=[], verdict="wrong")
        attempt = SimpleAttempt(rejected_label="THEOREM", verdicts={"THEOREM": SimpleRecord(report=report)},
                                oversized_label=None)
        reason = runner._unverified_reason(SimpleResult(best_attempt=attempt), None)
        self.assertNotIn("\n", reason)
        self.assertNotIn("bounded excerpt", reason)
        self.assertNotIn("```", reason.replace("\\`", ""))
        self.assertTrue(reason.startswith("block THEOREM was rejected (THEOREM: Line one."))

    def test_an_annotation_carries_at_most_64_evidence_ids(self) -> None:
        from tests.test_math_harness_runner import _accepted_outputs, _api_run

        real = cgm_curation.propagate

        def many(**kwargs):
            rows = list(real(**kwargs))
            index = next(i for i, row in enumerate(rows) if row.evidence)
            rows[index] = dataclasses.replace(rows[index], evidence=tuple((list(rows[index].evidence) * 70)[:70]))
            return rows

        with tempfile.TemporaryDirectory() as raw, patch.object(runner.cgm_curation, "propagate", side_effect=many):
            code, _calls, _events = _api_run(Path(raw), _accepted_outputs())
            self.assertEqual(code, 0)
            graph = engine_graph.build(Path(raw), {"engine": "math_harness"}, view="workflow")
            self.assertTrue(graph)


@dataclasses.dataclass
class SimpleIssue:
    location: str
    issue: str


@dataclasses.dataclass
class SimpleReport:
    critical_errors: list
    gaps: list
    verdict: str


@dataclasses.dataclass
class SimpleRecord:
    report: SimpleReport


@dataclasses.dataclass
class SimpleAttempt:
    rejected_label: str | None
    verdicts: dict
    oversized_label: str | None


@dataclasses.dataclass
class SimpleResult:
    best_attempt: SimpleAttempt


class TransportAndMonitorTests(unittest.TestCase):
    def test_provider_text_with_a_lone_surrogate_is_encodable(self) -> None:
        from agent_monitor.runners import api_backend

        body = {"output_text": "half \ud83d of an emoji", "usage": {"input_tokens": 1, "output_tokens": 1}}
        with patch("agent_monitor.settings._http_json", return_value=(200, body)):
            result = api_backend.api_chat("s", "u", model="gpt-6-astra", env={"OPENAI_API_KEY": "k"})
        self.assertEqual(result.text, "half � of an emoji")

    def test_line_separators_inside_a_reply_keep_its_monitor_event(self) -> None:
        from agent_monitor.cli_events import CLIEventParser

        text = "first second third\u0085fourth"
        events = [{"type": "item.completed", "item": {"type": "agent_message", "text": text}},
                  {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 2}}]
        parser = CLIEventParser("codex", source_engine="math_harness")
        parser.feed("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events))
        self.assertTrue(any(text in str(turn.get("detail") or turn.get("output") or "") for turn in parser.turns),
                        parser.turns)

    def test_guests_get_a_plain_sentence_for_empty_worker_replies(self) -> None:
        from agent_monitor.runners import kimi_runner

        error = runner.MathHarnessError(
            "no worker produced a usable candidate: worker-2 provider call failed: Kimi returned an empty "
            "response for kimi-k3 (finish_reason=length, output_tokens=8192, reasoning_tokens=8192)"
        )
        message = kimi_runner._empty_workers_message(error)
        self.assertTrue(message.startswith("Kimi used its whole answer budget on reasoning and returned no proof text "
                                           "(finish_reason=length, output_tokens=8192, reasoning_tokens=8192)."), message)
        other = runner.MathHarnessError("no worker produced a usable candidate: worker-1 provider call failed: Kimi "
                                        "returned an empty response for kimi-k3 (finish_reason=stop)")
        self.assertTrue(kimi_runner._empty_workers_message(other).startswith("Kimi returned no proof text."))
        self.assertEqual(kimi_runner._empty_workers_message(runner.MathHarnessError("other")), "")
        # ... and the Kimi runner shows it as the run's error.
        environ = {k: v for k, v in os.environ.items() if k != "AGENT_MONITOR_SPONSORED_KIMI"}
        out = io.StringIO()
        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as raw:
            os.chdir(raw)
            try:
                with (
                    patch.dict(os.environ, {**environ, "KIMI_API_KEY": "sk-test-kimi-123456789"}, clear=True),
                    patch.object(runner, "run_pipeline", side_effect=error),
                    patch.object(sys, "argv", ["kimi_runner.py", "Prove it."]),
                    contextlib.redirect_stdout(out),
                ):
                    self.assertEqual(kimi_runner.main(), 1)
            finally:
                os.chdir(previous)
        items = [json.loads(line).get("item") or {} for line in out.getvalue().splitlines() if line.strip()]
        self.assertEqual(items[-1]["message"], "Kimi could not finish this proof: " + message)


if __name__ == "__main__":
    unittest.main()


class StopRequestTests(unittest.TestCase):
    """The run's stop request (set from the SIGTERM handler) never blocks."""

    def test_a_stop_never_waits_for_a_lock_the_interrupted_code_holds(self) -> None:
        # The handler runs on the main thread wherever it interrupted it.
        # threading.Event.set takes the event's own non-reentrant lock, which
        # that code may hold (inside Event.wait): the runner then hung.
        ctx = runner._Ctx(route=runner.Route("openai", "gpt-6-astra", "api_chat"),
                          budgets=runner.Budgets(max_calls=1), started=time.monotonic())
        for cancel in (ctx.cancel, runner._StopRequest()):
            done = threading.Event()

            def interrupted(cancel=cancel, done=done) -> None:
                holding = getattr(cancel, "_cond", None) or contextlib.nullcontext()
                runner._ACTIVE_RUN_CANCELS.append(cancel)
                try:
                    with holding:  # what the interrupted code holds, if anything
                        runner._stop_active_runs(15, None)
                finally:
                    runner._ACTIVE_RUN_CANCELS.remove(cancel)
                done.set()

            thread = threading.Thread(target=interrupted, daemon=True)
            thread.start()
            thread.join(3)
            self.assertTrue(done.is_set(), "the stop request waited for a lock")
            self.assertTrue(cancel.is_set())

    def test_a_cgm_run_registers_a_lock_free_stop_request(self) -> None:
        from tests.test_math_harness_runner import _accepted_outputs, _api_run

        seen: list = []
        real = runner._stop_on_sigterm

        def capture(cancel):
            seen.append(cancel)
            return real(cancel)

        with tempfile.TemporaryDirectory() as raw, patch.object(runner, "_stop_on_sigterm", side_effect=capture):
            code, _calls, _events = _api_run(Path(raw), _accepted_outputs())
        self.assertEqual(code, 0)
        self.assertEqual([type(cancel) for cancel in seen], [runner._StopRequest])

    def test_it_waits_like_an_event(self) -> None:
        request = runner._StopRequest()
        started = time.monotonic()
        self.assertFalse(request.wait(0.2))
        self.assertGreaterEqual(time.monotonic() - started, 0.18)
        threading.Timer(0.1, request.set).start()
        started = time.monotonic()
        self.assertTrue(request.wait(5))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(request.wait(0))
        request.clear()
        self.assertFalse(request.is_set())
