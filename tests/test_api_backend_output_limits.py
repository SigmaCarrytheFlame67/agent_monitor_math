"""OpenAI output limits and failure wording in ``api_backend``.

In production (2026-10-26) CGM runs on gpt-6-astra at maximum effort failed:
each worker spent its whole 16,384-token output limit reasoning and returned
nothing.  An OpenAI call at extra-high or maximum effort now gets the
64,000-token limit Max (API) uses (unless one is set), a reply cut at its
limit says so, and OpenAI's "no credits remaining" reads as out of credit.
"""
from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from agent_monitor import run_tuning
from agent_monitor.runners import api_backend

_KEY = {"OPENAI_API_KEY": "run-openai-key"}


def _call(env: dict[str, str], *, body: dict[str, Any] | None = None, **kwargs: Any) -> tuple[Any, dict]:
    sent: dict[str, Any] = {}

    def fake_http(_url: str, **request: Any) -> tuple[int, dict[str, Any]]:
        sent.update(request["payload"])
        return 200, body or {"output_text": "done", "usage": {"input_tokens": 3, "output_tokens": 2}}

    with patch("agent_monitor.settings._http_json", side_effect=fake_http):
        try:
            return api_backend.api_chat("s", "u", model="gpt-6-astra", env=env, **kwargs), sent
        except api_backend.APIBackendError as exc:
            return exc, sent


class OpenAIOutputLimitTests(unittest.TestCase):
    def test_extra_high_and_maximum_effort_get_room_to_answer(self) -> None:
        for name, env, kwargs, expected in (
            ("run profile max", {**_KEY, run_tuning.REASONING_ENV: "max"}, {}, 64_000),
            ("run profile xhigh", {**_KEY, run_tuning.REASONING_ENV: "xhigh"}, {}, 64_000),
            ("call effort max", dict(_KEY), {"effort": "max"}, 64_000),
            ("run profile high", {**_KEY, run_tuning.REASONING_ENV: "high"}, {}, 16_384),
            ("default", dict(_KEY), {}, 16_384),
            # An explicit limit always wins ...
            ("explicit", {**_KEY, run_tuning.REASONING_ENV: "max"}, {"max_tokens": 20_000}, 20_000),
            ("configured", {**_KEY, run_tuning.REASONING_ENV: "max", "AGENT_MONITOR_API_MAX_TOKENS": "30000"},
             {}, 30_000),
            # ... and a bounded run's ceiling still lowers it.
            ("guest ceiling", {**_KEY, run_tuning.REASONING_ENV: "max", "AGENT_MONITOR_MAX_OUTPUT_TOKENS": "8192"},
             {}, 8_192),
        ):
            with self.subTest(name):
                result, sent = _call(env, **kwargs)
                self.assertEqual(result.text, "done")
                self.assertEqual(sent["max_output_tokens"], expected)

    def test_extra_high_and_maximum_effort_get_time_to_answer(self) -> None:
        seen: list[float] = []

        def fake_http(_url: str, **request: Any) -> tuple[int, dict[str, Any]]:
            seen.append(request["timeout"])
            return 200, {"output_text": "done", "usage": {"input_tokens": 3, "output_tokens": 2}}

        with patch("agent_monitor.settings._http_json", side_effect=fake_http):
            api_backend.api_chat("s", "u", model="gpt-6-astra", env={**_KEY, run_tuning.REASONING_ENV: "max"})
            api_backend.api_chat("s", "u", model="gpt-6-astra", env=dict(_KEY), effort="xhigh")
            api_backend.api_chat("s", "u", model="gpt-6-astra", env=dict(_KEY))
            api_backend.api_chat("s", "u", model="gpt-6-astra", env={**_KEY, run_tuning.REASONING_ENV: "max"}, timeout=90)
        self.assertEqual(seen, [1800, 1800, 240, 90])
        self.assertTrue(api_backend.openai_high_effort({**_KEY, run_tuning.REASONING_ENV: "xhigh"}, "gpt-6-astra"))
        self.assertFalse(api_backend.openai_high_effort({**_KEY, run_tuning.REASONING_ENV: "high"}, "gpt-6-astra"))
        self.assertFalse(api_backend.openai_high_effort({run_tuning.REASONING_ENV: "max"}, "gpt-6-astra"))  # no key
        self.assertFalse(api_backend.openai_high_effort(
            {"ANTHROPIC_API_KEY": "k", run_tuning.REASONING_ENV: "max"}, "claude-opus-5-5"))

    def test_cgm_and_analyze_calls_at_those_efforts_get_30_minutes(self) -> None:
        from pathlib import Path
        import tempfile

        from tests.test_math_harness_runner import _accepted_outputs, _api_run

        for env, expected in (
            ({run_tuning.REASONING_ENV: "max", "AGENT_MONITOR_SELECTED_MODEL": "gpt-6-astra", **_KEY}, 1800),
            ({run_tuning.REASONING_ENV: "high", "AGENT_MONITOR_SELECTED_MODEL": "gpt-6-astra", **_KEY}, 300),
            ({run_tuning.REASONING_ENV: "max", "AGENT_MONITOR_SELECTED_MODEL": "gpt-6-astra",
              "AGENT_MONITOR_CGM_API_TIMEOUT": "600", **_KEY}, 600),
        ):
            with self.subTest(env=env), tempfile.TemporaryDirectory() as raw:
                code, calls, _events = _api_run(Path(raw), _accepted_outputs(), env=env)
                self.assertEqual(code, 0)
                self.assertEqual({call["timeout"] for call in calls}, {expected})

    def test_plain_and_meta_harness_runs_at_those_efforts_get_three_hours(self) -> None:
        # Up to 2 (Plain) or 8 plus the audit's (Meta-Harness) calls of up to
        # 30 minutes each: the one-hour watchdog would cut them short.
        from agent_monitor import jobs

        for engine, env, expected in (
            ("plain", {**_KEY, "PLAIN_MODEL": "gpt-6-astra", run_tuning.REASONING_ENV: "max"}, 10_800),
            ("metaharness", {**_KEY, "METAHARNESS_MODEL": "gpt-6-astra", run_tuning.REASONING_ENV: "xhigh"},
             10_800),
            ("plain", {**_KEY, "PLAIN_MODEL": "gpt-6-astra", run_tuning.REASONING_ENV: "high"}, 3_600),
            # A linked account's route never calls api_chat.
            ("plain", {**_KEY, "PLAIN_MODEL": "gpt-6-astra", run_tuning.REASONING_ENV: "max",
                       "AGENT_MONITOR_CODEX_SUBSCRIPTION": "1"}, 3_600),
            ("plain", {"ANTHROPIC_API_KEY": "k", "PLAIN_MODEL": "claude-opus-5-5",
                       run_tuning.REASONING_ENV: "max"}, 3_600),
            # Other engines keep theirs; an operator timeout still wins.
            ("openclaw", {**_KEY, "AGENT_MONITOR_OPENCLAW_MODEL": "openai/gpt-6-astra",
                          run_tuning.REASONING_ENV: "max"}, 3_600),
            ("plain", {**_KEY, "PLAIN_MODEL": "gpt-6-astra", run_tuning.REASONING_ENV: "max",
                       "AGENT_MONITOR_CLI_TIMEOUT": "5000"}, 5_000),
        ):
            with self.subTest(engine=engine, env=sorted(env)):
                self.assertEqual(jobs._cli_timeout_seconds(engine, env), expected)

    def test_a_reply_cut_at_its_limit_says_so(self) -> None:
        body = {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [{"type": "reasoning", "summary": []}],
            "usage": {"input_tokens": 4592, "output_tokens": 64000,
                      "output_tokens_details": {"reasoning_tokens": 64000}},
        }
        error, _sent = _call({**_KEY, run_tuning.REASONING_ENV: "max"}, body=body)
        self.assertIsInstance(error, api_backend.APIBackendError)
        self.assertEqual(
            str(error),
            "OpenAI returned an empty response for gpt-6-astra "
            "(it stopped at its 64,000-token output limit; reasoning used 64,000 tokens)",
        )
        # Still an empty reply to the CGM harness, and still billed.
        self.assertIn("empty response", str(error))
        self.assertEqual(error.usage["output_tokens"], 64_000)
        # Another unfinished status is named too.
        error, _sent = _call(dict(_KEY), body={"status": "failed", "output": [], "usage": {}})
        self.assertTrue(str(error).endswith("(status failed)"), str(error))

    def test_no_credits_remaining_is_out_of_credit(self) -> None:
        body = {"error": {"message": "You have no credits remaining. Add credits to continue using the API at "
                                     "https://platform.openai.com/settings/organization/billing/."}}
        self.assertEqual(api_backend._failure_kind(429, body), "usage_limit")
        # A rate limit alone is not.
        self.assertIsNone(api_backend._failure_kind(429, {"error": {"message": "Rate limit reached"}}))

    def test_a_moderation_refusal_of_one_input_is_not_a_rejected_key(self) -> None:
        # OpenRouter answers flagged input with HTTP 403; a CGM worker's
        # rejected key stops its route, a refused input must not.
        moderated = {"error": {"code": 403, "message": "Your chosen model requires moderation and your input was "
                                                       "flagged for \"harassment\".",
                               "metadata": {"reasons": ["harassment"], "flagged_input": "…", "provider_name": "x"}}}
        self.assertIsNone(api_backend._failure_kind(403, moderated))
        self.assertEqual(api_backend._failure_kind(403, {"error": {"message": "Key disabled"}}), "auth")
        self.assertEqual(api_backend._failure_kind(401, moderated), "auth")



_BAD_KEYS = (
    ("ANTHROPIC_API_KEY", "claude-opus-4-8", "sk-ant-api03-FAKEFIRSTHALF\nFAKESECONDHALF"),
    ("OPENAI_API_KEY", "gpt-6-astra", "sk-proj-FAKEFIRSTHALF\r\nFAKESECONDHALF"),
    ("OPENAI_API_KEY", "gpt-6-astra", "sk-proj-FAKEFIRSTHALF\x00FAKESECONDHALF"),
    ("OPENAI_API_KEY", "gpt-6-astra", "sk-proj-FAKEFIRSTHALF\tFAKESECONDHALF"),
    # A Gemini key goes into the URL: a space made the URL invalid, quoted.
    ("GEMINI_API_KEY", "gemini-3-pro", "AIzaSyFAKEFIRSTHALF FAKESECONDHALF"),
    # Pasted non-breaking and zero-width spaces.
    ("OPENAI_API_KEY", "gpt-6-astra", "sk-proj-FAKEFIRSTHALF\u00a0FAKESECONDHALF"),
    ("ANTHROPIC_API_KEY", "claude-opus-4-8", "sk-ant-api03-FAKEFIRSTHALF\u200bFAKESECONDHALF"),
)


class CallTimeoutRoundingTests(unittest.TestCase):
    def test_a_call_near_the_deadline_gets_its_whole_remaining_time(self) -> None:
        # int() cut 3.7 s to 3 s, so a call could end before the deadline (and
        # an Analyze check failed as "timed out" instead of "time limit").
        from types import SimpleNamespace

        from agent_monitor.runners import math_harness_runner as runner

        sent: list[int] = []

        def chat(system, user, *, model, timeout, **_kwargs):
            sent.append(timeout)
            return SimpleNamespace(text="reply", usage={}, model=model, provider="openai")

        import time

        ctx = runner._Ctx(route=runner.Route("openai", "gpt-6-astra", "api_chat"),
                          budgets=runner.Budgets(max_calls=4), started=time.monotonic(), env={"OPENAI_API_KEY": "k"})
        turn = ctx.open_turn("worker-1", 1)
        with (
            patch.object(runner, "_deadline_timeout", return_value=3.2),
            patch.object(runner, "api_chat", side_effect=chat),
        ):
            runner._invoke(ctx, turn, stage="worker-1", system="s", user="u", effort=None)
        self.assertEqual(sent, [4])


class KeyWithControlCharacterTests(unittest.TestCase):
    """A key saved with a line break inside never reaches an error message."""

    def test_such_a_key_is_refused_before_any_request(self) -> None:
        for name, model, key in _BAD_KEYS:
            with (
                self.subTest(name=name, key=repr(key)[-20:]),
                patch("agent_monitor.settings._http_json") as http,
                patch.object(api_backend, "_anthropic_message") as anthropic_call,
                self.assertRaises(api_backend.APIBackendError) as raised,
            ):
                api_backend.api_chat("s", "u", model=model, env={name: key})
            http.assert_not_called()
            anthropic_call.assert_not_called()
            self.assertEqual(raised.exception.kind, "auth")
            self.assertIn("contains a space, a line break or another character", str(raised.exception))
            for part in ("FAKEFIRSTHALF", "FAKESECONDHALF"):
                self.assertNotIn(part, str(raised.exception))

    def test_the_graph_and_lean_calls_refuse_it_too(self) -> None:
        # proof_graph._call_llm (DAG, formal DAG and Lean calls) quoted such a
        # key in full: "Invalid header value b'Bearer sk-…'", or an invalid
        # URL carrying a Gemini key.
        from agent_monitor import proof_graph

        for name, model, key in _BAD_KEYS:
            with (
                self.subTest(name=name, key=repr(key)[-20:]),
                patch("agent_monitor.settings._http_json") as http,
                self.assertRaises(ValueError) as raised,
            ):
                proof_graph._call_llm({name: key}, model, "prompt")
            http.assert_not_called()
            self.assertIn("contains a space, a line break or another character", str(raised.exception))
            for part in ("FAKEFIRSTHALF", "FAKESECONDHALF"):
                self.assertNotIn(part, str(raised.exception))
        # A well-formed key is sent as before.
        with patch("agent_monitor.settings._http_json",
                   return_value=(200, {"output_text": "ok", "usage": {}})) as http:
            self.assertEqual(proof_graph._call_llm({"OPENAI_API_KEY": "sk-proj-AbC_123-xyz="}, "gpt-6-astra",
                                                   "prompt")[1], "ok")
        http.assert_called_once()

    def test_the_refusal_names_the_provider_not_the_model(self) -> None:
        # Behind an aggregator the model id names another provider's model.
        for name, model, label in (
            ("OPENROUTER_API_KEY", "openrouter:anthropic/claude-opus-5", "OpenRouter"),
            ("GROQ_API_KEY", "groq:openai/gpt-oss-120b", "Groq"),
            ("OPENAI_API_KEY", "gpt-6-astra", "OpenAI"),
        ):
            with self.subTest(provider=label), self.assertRaises(api_backend.APIBackendError) as raised:
                api_backend.api_chat("s", "u", model=model, env={name: "sk-FAKE FAKE", "ANTHROPIC_API_KEY": "fine"})
            self.assertTrue(str(raised.exception).startswith(f"The {label} API key contains a space"),
                            str(raised.exception))

    def test_a_model_id_with_a_space_never_puts_the_key_in_an_error(self) -> None:
        # A Gemini key travels in the URL: an unquoted "gemini-3 pro" made an
        # invalid URL whose error quoted it, key included (DAG and Lean calls).
        from agent_monitor import proof_graph

        key = "AIzaSyFAKEFIRSTHALFFAKESECONDHALF"
        env = {"GEMINI_API_KEY": key, "GEMINI_API_BASE": "http://127.0.0.1:9"}  # nothing listens there
        for model in ("gemini-3 pro", "gemini-3\tpro", "google:gemini 3"):
            with self.subTest(model=repr(model)), self.assertRaises(Exception) as raised:
                proof_graph._call_llm(dict(env), model, "prompt", timeout=5)
            self.assertNotIn("FAKEFIRSTHALF", str(raised.exception))
        # The id is sent quoted (the request is valid; the provider answers it).
        urls: list[str] = []

        def http(url, **_kwargs):
            urls.append(url)
            return 404, {"error": {"message": "model not found"}}

        with patch("agent_monitor.settings._http_json", side_effect=http), self.assertRaises(ValueError):
            proof_graph._call_llm(dict(env), "gemini-3 pro", "prompt")
        self.assertIn("/models/gemini-3%20pro:generateContent", urls[0])
        # Whatever the HTTP library refuses is redacted too.
        import http.client

        refused = http.client.InvalidURL(f"URL can't contain control characters. '/x?key={key}' (found at least ' ')")
        with patch("agent_monitor.settings._http_json", side_effect=refused), self.assertRaises(ValueError) as raised:
            proof_graph._call_llm(dict(env), "gemini-3-pro", "prompt")
        self.assertNotIn("FAKEFIRSTHALF", str(raised.exception))
        self.assertIn("[redacted]", str(raised.exception))

    def test_every_form_of_a_key_is_redacted(self) -> None:
        key = "sk-ant-api03-FAKEFIRSTHALF\nFAKESECONDHALF-xyz"
        for text in (
            f"Illegal header value b'{repr(key.encode())[2:-1]}'",  # an HTTP library's quote
            f"value {key!r}",
            f"plain {key}",
            "first part sk-ant-api03-FAKEFIRSTHALF only",
        ):
            with self.subTest(text=text[:30]):
                redacted = api_backend._safe_error(text, key)
                self.assertNotIn("FAKEFIRSTHALF", redacted)
                self.assertNotIn("FAKESECONDHALF", redacted)
                self.assertIn("[redacted]", redacted)



class MissingAnthropicSDKTests(unittest.TestCase):
    def test_a_runner_without_the_sdk_gets_a_clear_error(self) -> None:
        # An engine's own environment (DeepAgents') may lack the pinned SDK,
        # or hold one from before 1.0 (httpx, not httpx2; no fallbacks).
        import anthropic

        import types

        for name, context in (
            ("missing", patch.dict("sys.modules", {"anthropic": None})),
            ("0.125.0", patch.object(anthropic, "__version__", "0.125.0")),
            # A leftover anthropic/ directory imports as a namespace package.
            ("no __version__", patch.dict("sys.modules", {"anthropic": types.ModuleType("anthropic")})),
        ):
            with (
                self.subTest(sdk=name),
                context,
                patch.object(api_backend, "_anthropic_message") as call,
                self.assertRaises(api_backend.APIBackendError) as raised,
            ):
                api_backend.api_chat("s", "u", model="claude-opus-4-8", env={"ANTHROPIC_API_KEY": "run-key"})
            call.assert_not_called()
            self.assertIn("is missing or older than 1.0", str(raised.exception))
            self.assertIn("re-run ./setup.sh", str(raised.exception))
            self.assertIsNone(raised.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
