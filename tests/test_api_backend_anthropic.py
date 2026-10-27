"""Anthropic calls in ``api_backend`` go through the official SDK (1.13.0).

A call to the Claude API streams; one to a custom base URL (a gateway) is a
plain request, as before the SDK.  Every call follows no redirect (the key
travels in X-Api-Key), ends at its deadline with any retries inside it, only
retries a request refused before processing (429, 529), opts into
server-side refusal fallbacks on the Claude API, and reports usage in the
Codex semantics of the CGM event stream, split by model after a fallback.
Effort is validated per provider.  The CGM event parser prices each call,
and each model's share of a fallback call, at that model's rates.

No test reaches a model API: most patch the SDK seam
(``api_backend._anthropic_message``); the transport tests run the real SDK
against a scripted endpoint on the loopback interface.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest.mock import patch

import anthropic
import httpx2
from anthropic.types.beta import BetaMessage

from agent_monitor.runners import api_backend

KEY = "sk-ant-test-secret-0123456789"
_ENV = {"ANTHROPIC_API_KEY": KEY}
_BETA = "server-side-fallback-2026-07-01"


def _message(**fields: Any) -> BetaMessage:
    """A final message as the SDK returns it."""
    data: dict[str, Any] = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-fable-5-1",
        "content": [{"type": "text", "text": "Proof text"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 11, "output_tokens": 5},
    }
    data.update(fields)
    return BetaMessage.model_validate(data)


def _call(
    model: str = "claude-fable-5-1",
    *,
    env: dict[str, str] | None = None,
    reply: Any = None,
    **kwargs: Any,
) -> tuple[Any, dict[str, Any]]:
    """``api_chat`` with the SDK seam faked; returns (result, captured)."""
    captured: dict[str, Any] = {}

    def seam(options: Any, request: Any) -> Any:
        captured.update(options=dict(options), request=dict(request))
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            return reply()
        return reply if reply is not None else _message(model=model)

    with (
        patch.object(api_backend, "_anthropic_message", side_effect=seam),
        patch("agent_monitor.settings._http_json") as http,
    ):
        result = api_backend.api_chat(
            "System contract", "User theorem", model=model, env=dict(env or _ENV), **kwargs
        )
    http.assert_not_called()
    return result, captured


def _status_error(code: int, body: object) -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages?beta=true")
    response = httpx2.Response(code, json=body, request=request)
    return anthropic.APIStatusError(f"Error code: {code} - {body}", response=response, body=body)


class AnthropicRequestTests(unittest.TestCase):
    def test_request_carries_only_the_prompt_limit_and_given_effort(self) -> None:
        _result, captured = _call("claude-opus-5-5", max_tokens=64000)
        self.assertEqual(
            captured["options"],
            {"api_key": KEY, "base_url": "https://api.anthropic.com", "timeout": 240, "stream": True},
        )
        self.assertEqual(
            captured["request"],
            {
                "model": "claude-opus-5-5",
                "max_tokens": 64000,
                "system": "System contract",
                "messages": [{"role": "user", "content": "User theorem"}],
                "betas": [_BETA],
                "fallbacks": "default",
            },
        )
        # Effort only when given; never thinking, sampling or tool parameters.
        for effort in ("low", "medium", "high", "xhigh", "max"):
            with self.subTest(effort=effort):
                _result, captured = _call("claude-opus-5-5", effort=effort, timeout=1800)
                request = captured["request"]
                self.assertEqual(request["output_config"], {"effort": effort})
                self.assertEqual(captured["options"]["timeout"], 1800)
                for name in ("thinking", "temperature", "top_p", "top_k", "tools", "tool_choice"):
                    self.assertNotIn(name, request)

    def test_job_output_ceiling_still_caps_max_tokens(self) -> None:
        _result, captured = _call(
            env={**_ENV, "AGENT_MONITOR_MAX_OUTPUT_TOKENS": "8192"}, max_tokens=64000
        )
        self.assertEqual(captured["request"]["max_tokens"], 8192)

    def test_refusal_fallbacks_only_for_the_listed_models_on_the_claude_api(self) -> None:
        listed = ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")
        for model in listed:
            for base in (None, "https://api.anthropic.com/", "https://api.anthropic.com/v1"):
                with self.subTest(model=model, base=base):
                    env = {**_ENV, **({"ANTHROPIC_BASE_URL": base} if base else {})}
                    _result, captured = _call(model, env=env)
                    self.assertEqual(captured["request"]["betas"], [_BETA])
                    self.assertEqual(captured["request"]["fallbacks"], "default")
        others = (
            ("claude-sonnet-5", None),
            ("claude-haiku-4-5", None),
            ("claude-opus-4-8", None),
            ("claude-fable-5", None),
            ("claude-fable-5-1", "https://gateway.example.test"),
            ("claude-opus-5-5", "https://gateway.example.test/v1"),
        )
        for model, base in others:
            with self.subTest(model=model, base=base):
                env = {**_ENV, **({"ANTHROPIC_BASE_URL": base} if base else {})}
                _result, captured = _call(model, env=env)
                self.assertNotIn("betas", captured["request"])
                self.assertNotIn("fallbacks", captured["request"])

    def test_sdk_base_url_drops_a_trailing_v1(self) -> None:
        cases = {
            "https://gateway.example.test": "https://gateway.example.test",
            "https://gateway.example.test/": "https://gateway.example.test",
            "https://gateway.example.test/v1": "https://gateway.example.test",
            "https://gateway.example.test/v1/": "https://gateway.example.test",
            "https://gateway.example.test/anthropic/v1": "https://gateway.example.test/anthropic",
        }
        for configured, expected in cases.items():
            with self.subTest(base=configured):
                _result, captured = _call(env={**_ENV, "ANTHROPIC_BASE_URL": configured})
                self.assertEqual(captured["options"]["base_url"], expected)
                # A gateway need not stream: it gets plain requests.
                self.assertFalse(captured["options"]["stream"])
        for configured in ("https://api.anthropic.com", "https://API.anthropic.com/v1/"):
            with self.subTest(base=configured):
                _result, captured = _call(env={**_ENV, "ANTHROPIC_BASE_URL": configured})
                self.assertTrue(captured["options"]["stream"])

    def test_client_follows_no_redirect(self) -> None:
        # anthropic 1.13.0 follows redirects unless told not to.
        default, pinned = anthropic.DefaultHttpxClient(), anthropic.DefaultHttpxClient(follow_redirects=False)
        try:
            self.assertTrue(default.follow_redirects)
            self.assertFalse(pinned.follow_redirects)
        finally:
            default.close()
            pinned.close()

        built: dict[str, Any] = {}

        class FakeClient:
            def __init__(self, **kwargs: Any) -> None:
                built.update(kwargs)
                self.beta = self
                self.messages = self

            def __enter__(self) -> FakeClient:
                return self

            def __exit__(self, *_exc: object) -> None:
                built["closed"] = True

            def stream(self, **request: Any) -> _FakeStream:
                built["request"] = request
                return _FakeStream([], _message())

        with patch.object(anthropic, "Anthropic", FakeClient):
            message = api_backend._anthropic_message(
                {"api_key": KEY, "base_url": "https://api.anthropic.com", "timeout": 30, "stream": True},
                {"model": "claude-fable-5-1", "max_tokens": 1024, "messages": []},
            )
        self.assertEqual(message.stop_reason, "end_turn")
        client = built.pop("http_client")
        self.assertIsInstance(client, httpx2.Client)
        self.assertFalse(client.follow_redirects)
        # The SDK never retries: _anthropic_message decides (see the transport tests).
        self.assertEqual(
            {name: built[name] for name in ("api_key", "base_url", "max_retries", "timeout")},
            {"api_key": KEY, "base_url": "https://api.anthropic.com", "max_retries": 0, "timeout": 30},
        )
        # Each attempt's timeout is what is left of the call.
        timeout = built["request"].pop("timeout")
        self.assertIsInstance(timeout, httpx2.Timeout)
        self.assertLessEqual(timeout.read, 30)
        self.assertEqual(timeout.connect, 10)
        self.assertEqual(built["request"], {"model": "claude-fable-5-1", "max_tokens": 1024, "messages": []})
        self.assertTrue(built["closed"])


class _FakeStream:
    """A ``client.messages.stream(...)`` context with scripted events: its
    message snapshot has no stop reason until the events run out."""

    def __init__(self, events: Any, final: Any, pause: float = 0.0) -> None:
        self.events, self.final, self.pause = events, final, pause
        self.closed = self.started = self.done = False
        self.response = None  # no connection for the watchdog to end

    def __enter__(self) -> _FakeStream:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.closed = True

    def __iter__(self) -> Any:
        for event in self.events:
            time.sleep(self.pause)
            self.started = True
            yield event
        self.done = True

    @property
    def current_message_snapshot(self) -> Any:
        if self.done:
            return self.final
        if self.started:
            return _message(stop_reason=None)
        raise AssertionError  # as the SDK does before the first event


class AnthropicReplyTests(unittest.TestCase):
    def test_text_skips_thinking_and_fallback_blocks(self) -> None:
        # After a mid-reply fallback the next model continues the text, and
        # the final message names the model that answered.
        reply = _message(
            model="claude-opus-4-8",
            content=[
                {"type": "thinking", "thinking": "", "signature": "sig"},
                {"type": "text", "text": "Lemma 1 holds "},
                {
                    "type": "fallback",
                    "from": {"model": "claude-fable-5-1"},
                    "to": {"model": "claude-opus-4-8"},
                    "trigger": {"type": "refusal", "category": "cyber"},
                },
                {"type": "text", "text": "by induction."},
            ],
        )
        result, _captured = _call("claude-fable-5-1", reply=reply)
        self.assertEqual(result.text, "Lemma 1 holds by induction.")
        self.assertEqual((result.provider, result.model), ("anthropic", "claude-fable-5-1"))
        self.assertEqual(result.observed_model, "claude-opus-4-8")

    def test_usage_counts_the_cache_inside_input_and_thinking_inside_output(self) -> None:
        reply = _message(
            usage={
                "input_tokens": 100,
                "cache_read_input_tokens": 400,
                "cache_creation_input_tokens": 50,
                "output_tokens": 300,
                "output_tokens_details": {"thinking_tokens": 200},
            }
        )
        result, _captured = _call(reply=reply)
        self.assertEqual(
            result.usage,
            {
                "input_tokens": 550,
                "cached_input_tokens": 400,
                "cache_write_tokens": 50,
                "output_tokens": 300,
                "reasoning_output_tokens": 200,
            },
        )

    def test_usage_sums_every_attempt_of_a_fallback_and_reaches_the_run(self) -> None:
        from agent_monitor.cli_events import CLIEventParser
        from agent_monitor.runners import math_harness_runner

        reply = _message(
            model="claude-opus-4-8",
            usage={
                # The top level covers only the attempt that answered.
                "input_tokens": 1200,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 0,
                "output_tokens": 500,
                "output_tokens_details": {"thinking_tokens": 350},
                "iterations": [
                    {"type": "message", "model": "claude-fable-5-1", "input_tokens": 1000,
                     "cache_read_input_tokens": 0, "cache_creation_input_tokens": 300, "output_tokens": 40},
                    {"type": "fallback_message", "model": "claude-opus-4-8", "input_tokens": 1200,
                     "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0, "output_tokens": 500},
                ],
            },
        )
        result, _captured = _call(reply=reply)
        self.assertEqual(
            result.usage,
            {
                "input_tokens": 2600,
                "cached_input_tokens": 100,
                "cache_write_tokens": 300,
                "output_tokens": 540,
                "reasoning_output_tokens": 350,
            },
        )
        # Each model's share, in order; the thinking count is the last one's.
        self.assertEqual(
            result.usage_by_model,
            (
                ("claude-fable-5-1", {"input_tokens": 1300, "cached_input_tokens": 0, "cache_write_tokens": 300,
                                      "output_tokens": 40, "reasoning_output_tokens": 0}),
                ("claude-opus-4-8", {"input_tokens": 1300, "cached_input_tokens": 100, "cache_write_tokens": 0,
                                     "output_tokens": 500, "reasoning_output_tokens": 350}),
            ),
        )
        # A CGM run books it from its event stream without counting the cache twice.
        events: list[dict[str, Any]] = []
        math_harness_runner._emit_stage_usage(
            "verify-1", math_harness_runner._clean_usage(result.usage), out=events.append
        )
        parser = CLIEventParser("codex", source_engine="math_harness")
        parser.feed("".join(json.dumps(event) + "\n" for event in events))
        self.assertEqual(
            {name: parser.usage[name] for name in (
                "input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens")},
            {"input_tokens": 2600, "cache_read_tokens": 100, "cache_write_tokens": 300,
             "output_tokens": 540, "reasoning_tokens": 350},
        )
        self.assertEqual(parser.uncached_input(2600, 100, 300), 2200)

    def test_a_refusal_is_an_error_that_keeps_its_usage(self) -> None:
        cases = (
            ({"type": "refusal", "category": "cyber"}, "cyber"),
            ({"type": "refusal", "category": None}, "refusal"),
            (None, "refusal"),
        )
        for details, label in cases:
            with self.subTest(label=label, details=details):
                reply = _message(
                    stop_reason="refusal",
                    stop_details=details,
                    content=[{"type": "text", "text": "Partial answer that must not be parsed"}],
                    usage={"input_tokens": 900, "output_tokens": 12},
                )
                with self.assertRaises(api_backend.APIBackendError) as raised:
                    _call("claude-fable-5-1", reply=reply)
                self.assertEqual(
                    str(raised.exception), f"Anthropic declined the request for claude-fable-5-1 ({label})"
                )
                # Unusable output, as an empty reply's is (the harness degrades on it).
                self.assertTrue(raised.exception.refusal)
                self.assertIsNone(raised.exception.kind)
                self.assertEqual(raised.exception.usage["input_tokens"], 900)
                self.assertEqual(raised.exception.usage["output_tokens"], 12)

    def test_a_reply_cut_at_max_tokens_without_text_says_so(self) -> None:
        reply = _message(
            stop_reason="max_tokens",
            content=[{"type": "thinking", "thinking": "", "signature": "sig"}],
            usage={"input_tokens": 40, "output_tokens": 64000,
                   "output_tokens_details": {"thinking_tokens": 64000}},
        )
        with self.assertRaises(api_backend.APIBackendError) as raised:
            _call(reply=reply, max_tokens=64000)
        self.assertEqual(
            str(raised.exception),
            "Anthropic returned an empty response for claude-fable-5-1 "
            "(stop_reason=max_tokens, output_tokens=64000)",
        )
        self.assertEqual(raised.exception.usage["output_tokens"], 64000)
        self.assertEqual(raised.exception.usage["reasoning_output_tokens"], 64000)
        # Text that did arrive before the limit is returned as before.
        result, _captured = _call(
            reply=_message(stop_reason="max_tokens", content=[{"type": "text", "text": "Partial proof"}])
        )
        self.assertEqual(result.text, "Partial proof")

    def test_a_reply_that_never_ended_is_not_returned(self) -> None:
        reply = _message(stop_reason=None, content=[{"type": "text", "text": '{"verdict": "acc'}])
        with self.assertRaisesRegex(api_backend.APIBackendError, "ended before it was complete"):
            _call(reply=reply)

    def test_usage_iterations_in_a_plain_message_are_counted(self) -> None:
        # A gateway's (or a non-beta) reply is a GA Message, which keeps the
        # beta-only usage.iterations as plain dicts.
        from anthropic.types import Message

        reply = Message.model_validate({
            "id": "msg_gw", "type": "message", "role": "assistant", "model": "claude-opus-4-8",
            "content": [{"type": "text", "text": "Proof text"}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1000, "output_tokens": 70, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0,
                      "output_tokens_details": {"thinking_tokens": 50},
                      "iterations": [{"type": "message", "model": "claude-opus-4-8", "input_tokens": 1000,
                                      "output_tokens": 70, "cache_read_input_tokens": 0,
                                      "cache_creation_input_tokens": 0}]},
        })
        self.assertIsInstance(reply.usage.iterations[0], dict)
        result, _captured = _call("claude-opus-4-8", reply=reply)
        self.assertEqual(
            result.usage,
            {"input_tokens": 1000, "cached_input_tokens": 0, "cache_write_tokens": 0,
             "output_tokens": 70, "reasoning_output_tokens": 50},
        )
        self.assertEqual(result.usage_by_model, ())

    def test_usage_is_split_by_model_only_after_a_fallback(self) -> None:
        def usage(*iterations: dict[str, Any]) -> dict[str, Any]:
            return {"input_tokens": 10, "output_tokens": 5, "iterations": list(iterations)}

        def attempt(kind: str, model: str | None, tokens: int) -> dict[str, Any]:
            entry = {"type": kind, "input_tokens": tokens, "cache_read_input_tokens": 0,
                     "cache_creation_input_tokens": 0, "output_tokens": tokens}
            return entry if model is None else {**entry, "model": model}

        single = (
            usage(),
            usage(attempt("message", "claude-fable-5-1", 10)),
            usage(attempt("message", None, 10), attempt("message", "claude-fable-5-1", 20)),
        )
        for case in single:
            with self.subTest(case=case):
                result, _captured = _call(reply=_message(usage=case))
                self.assertEqual(result.usage_by_model, ())
        # An attempt that names no model is the requested model's.
        result, _captured = _call(
            reply=_message(model="claude-opus-4-8", usage=usage(
                attempt("message", None, 10), attempt("fallback_message", "claude-opus-4-8", 20)))
        )
        self.assertEqual([model for model, _share in result.usage_by_model], ["claude-fable-5-1", "claude-opus-4-8"])
        self.assertEqual([share["output_tokens"] for _model, share in result.usage_by_model], [10, 20])
        # A failed call carries its split too (here a refusal by the whole chain).
        refused = _message(
            stop_reason="refusal", content=[],
            usage=usage(attempt("message", "claude-fable-5-1", 10), attempt("message", "claude-opus-4-8", 20)),
        )
        with self.assertRaises(api_backend.APIBackendError) as raised:
            _call(reply=refused)
        self.assertEqual(
            [model for model, _share in raised.exception.usage_by_model], ["claude-fable-5-1", "claude-opus-4-8"]
        )
        self.assertEqual(raised.exception.usage["output_tokens"], 30)


class AnthropicErrorTests(unittest.TestCase):
    def _error(self, reply: BaseException) -> api_backend.APIBackendError:
        with self.assertRaises(api_backend.APIBackendError) as raised:
            _call("claude-opus-5-5", reply=reply)
        exc = raised.exception
        rendered = "".join(traceback.format_exception(exc)) + repr(exc)
        self.assertNotIn(KEY, rendered)
        # The SDK error holds the request, key header included: never chained.
        self.assertIsNone(exc.__cause__)
        self.assertTrue(exc.__suppress_context__ or exc.__context__ is None)
        return exc

    def test_http_errors_keep_their_kind_and_never_echo_the_key(self) -> None:
        cases = (
            (401, {"type": "error", "error": {"type": "authentication_error", "message": f"invalid x-api-key {KEY}"}},
             "auth", "HTTP 401"),
            (403, {"type": "error", "error": {"type": "permission_error", "message": "denied"}}, "auth", "HTTP 403"),
            (400, {"type": "error", "error": {"type": "invalid_request_error",
                                              "message": "Your credit balance is too low to access the Anthropic API."}},
             "usage_limit", "HTTP 400"),
            (404, {"type": "error", "error": {"type": "not_found_error", "message": "model: claude-opus-5-5"}},
             "model", "HTTP 404"),
            (429, {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}, None, "HTTP 429"),
            (529, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}, None, "HTTP 529"),
            (500, {"type": "error", "error": {"type": "api_error", "message": "Internal"}}, None, "HTTP 500"),
            # An error event inside the stream comes with the stream's 200.
            (200, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}, None,
             "stream error"),
        )
        for code, body, kind, where in cases:
            with self.subTest(code=code):
                exc = self._error(_status_error(code, body))
                self.assertTrue(
                    str(exc).startswith(f"Anthropic model call failed for claude-opus-5-5 ({where}): "), str(exc)
                )
                self.assertEqual(exc.kind, kind)
                self.assertEqual(exc.usage, {})
        self.assertIn("[redacted]", str(self._error(_status_error(401, cases[0][1]))))

    def test_timeouts_and_transport_failures(self) -> None:
        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages?beta=true",
                                 headers={"X-Api-Key": KEY})
        exc = self._error(anthropic.APITimeoutError(request=request))
        self.assertEqual(str(exc), "Anthropic model call timed out for claude-opus-5-5 after 240 s")
        self.assertIsNone(exc.kind)

        def refused() -> None:
            try:
                raise httpx2.ConnectError(f"[Errno 111] Connection refused (key {KEY})")
            except httpx2.ConnectError as cause:
                raise anthropic.APIConnectionError(request=request) from cause

        exc = self._error(refused)
        self.assertEqual(
            str(exc),
            "Anthropic model transport failed for claude-opus-5-5: "
            "[Errno 111] Connection refused (key [redacted])",
        )

    def test_a_connection_that_never_opens_is_named(self) -> None:
        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")

        def unreachable() -> None:
            try:
                raise httpx2.ConnectTimeout("timed out")
            except httpx2.ConnectTimeout as cause:
                raise anthropic.APITimeoutError(request=request) from cause

        exc = self._error(unreachable)
        self.assertEqual(
            str(exc), "Anthropic model call failed for claude-opus-5-5: could not connect (connection timed out)"
        )

    def test_the_seams_own_deadline_error_passes_through(self) -> None:
        exc = self._error(
            api_backend.APIBackendError("Anthropic model call timed out for claude-opus-5-5 after 1800 s")
        )
        self.assertEqual(str(exc), "Anthropic model call timed out for claude-opus-5-5 after 1800 s")


class EffortValidationTests(unittest.TestCase):
    """Effort is validated once the provider is known, before any request."""

    def test_kimi_keeps_its_own_efforts(self) -> None:
        env = {"KIMI_API_KEY": "run-kimi-key", "KIMI_API_BASE": "https://run.kimi.invalid/v1"}
        with patch("agent_monitor.settings._http_json") as http:
            for effort in ("xhigh", "medium"):
                with (
                    self.subTest(effort=effort),
                    self.assertRaisesRegex(
                        api_backend.APIBackendError, "^reasoning effort must be one of low, high or max$"
                    ),
                ):
                    api_backend.api_chat("s", "u", model="kimi-k3", effort=effort, env=env)
        http.assert_not_called()

    def test_anthropic_accepts_xhigh_and_rejects_unknown_efforts(self) -> None:
        _result, captured = _call("claude-sonnet-5-5", effort="XHigh ")
        self.assertEqual(captured["request"]["output_config"], {"effort": "xhigh"})
        with (
            patch.object(api_backend, "_anthropic_message") as seam,
            self.assertRaisesRegex(api_backend.APIBackendError, "low, medium, high, xhigh or max"),
        ):
            api_backend.api_chat("s", "u", model="claude-sonnet-5-5", effort="ultra", env=_ENV)
        seam.assert_not_called()

    def test_openai_effort_overrides_the_run_profile(self) -> None:
        from agent_monitor import run_tuning

        captured: list[dict[str, Any]] = []

        def fake_http(_url: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
            captured.append(kwargs["payload"])
            return 200, {"output_text": "done", "usage": {"input_tokens": 3, "output_tokens": 2}}

        env = {
            "OPENAI_API_KEY": "run-openai-key",
            run_tuning.REASONING_ENV: "high",
            run_tuning.SPEED_ENV: "fast",
        }
        with patch("agent_monitor.settings._http_json", side_effect=fake_http):
            api_backend.api_chat("s", "u", model="gpt-6-astra", env=env)
            api_backend.api_chat("s", "u", model="gpt-6-astra", effort="max", env=env)
            api_backend.api_chat("s", "u", model="gpt-6-astra", effort="xhigh",
                                 env={"OPENAI_API_KEY": "run-openai-key"})
            with self.assertRaisesRegex(api_backend.APIBackendError, "low, medium, high, xhigh or max"):
                api_backend.api_chat("s", "u", model="gpt-6-astra", effort="none", env=env)
        self.assertEqual(captured[0]["reasoning"], {"effort": "high"})
        self.assertEqual(captured[1]["reasoning"], {"effort": "max"})
        # The rest of the profile stays.
        self.assertEqual(captured[1]["service_tier"], "fast")
        self.assertEqual(captured[2]["reasoning"], {"effort": "xhigh"})
        self.assertNotIn("service_tier", captured[2])
        self.assertEqual(len(captured), 3)

    def test_other_providers_check_kimi_efforts_and_send_none(self) -> None:
        # A Kimi model served by OpenRouter, Groq or Together gets the CGM's
        # Kimi stage effort, as before: checked, then not sent.
        cases = (
            ("moonshotai/kimi-k2", {"OPENROUTER_API_KEY": "run-router-key"}),
            ("groq:moonshotai/kimi-k2-instruct", {"GROQ_API_KEY": "run-groq-key"}),
            ("together:moonshotai/Kimi-K2-Instruct", {"TOGETHERAI_API_KEY": "run-together-key"}),
            ("gemini-3.6-flash", {"GEMINI_API_KEY": "run-google-key"}),
            ("deepseek-v4-flash", {"DEEPSEEK_API_KEY": "run-deepseek-key"}),
        )
        payloads: list[dict[str, Any]] = []

        def fake_http(_url: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
            payloads.append(kwargs["payload"])
            return 200, {
                "choices": [{"message": {"content": "done"}}],
                "candidates": [{"content": {"parts": [{"text": "done"}]}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            }

        with patch("agent_monitor.settings._http_json", side_effect=fake_http):
            for model, env in cases:
                with self.subTest(model=model):
                    for effort in ("low", "high", "max"):
                        result = api_backend.api_chat("s", "u", model=model, effort=effort, env=env)
                        self.assertEqual(result.text, "done")
                    with self.assertRaisesRegex(
                        api_backend.APIBackendError, "^reasoning effort must be one of low, high or max$"
                    ):
                        api_backend.api_chat("s", "u", model=model, effort="xhigh", env=env)
        self.assertEqual(len(payloads), 3 * len(cases))
        for payload in payloads:
            self.assertFalse({"reasoning", "reasoning_effort", "output_config", "effort"} & set(payload), payload)


# ── the real SDK against a scripted loopback endpoint ─────────────────────


class _Endpoint:
    """A local Messages endpoint; ``respond(handler)`` writes each reply."""

    def __init__(self, respond: Any) -> None:
        self.requests: list[dict[str, Any]] = []
        self.disconnected = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length") or 0)
                outer.requests.append(
                    {
                        "path": self.path,
                        "headers": {name.lower(): value for name, value in self.headers.items()},
                        "body": json.loads(self.rfile.read(length) or b"{}"),
                    }
                )
                try:
                    respond(self)
                except (BrokenPipeError, ConnectionResetError):
                    outer.disconnected.set()

            def log_message(self, *_args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> _Endpoint:
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


def _sse(handler: BaseHTTPRequestHandler, events: list[dict[str, Any]], *, pause: float = 0.0) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()
    for event in events:
        chunk = f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
        handler.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
        handler.wfile.flush()
        time.sleep(pause)
    handler.wfile.write(b"0\r\n\r\n")
    handler.wfile.flush()


def _start(model: str) -> dict[str, Any]:
    return {
        "type": "message_start",
        "message": {
            "id": "msg_wire", "type": "message", "role": "assistant", "model": model, "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 1000, "output_tokens": 1,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        },
    }


def _block(index: int, block: dict[str, Any], *deltas: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"type": "content_block_start", "index": index, "content_block": block},
        *({"type": "content_block_delta", "index": index, "delta": delta} for delta in deltas),
        {"type": "content_block_stop", "index": index},
    ]


@contextmanager
def _no_proxy() -> Any:
    # The SDK honours proxy variables; the loopback endpoint is never proxied.
    with patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*"}):
        yield


@contextmanager
def _claude_api(endpoint: _Endpoint) -> Any:
    """Stand ``endpoint`` in for the Claude API itself (calls to it stream)."""
    with patch.object(api_backend, "_ANTHROPIC_DEFAULT_BASE", endpoint.base):
        yield {**_ENV, "ANTHROPIC_BASE_URL": endpoint.base}


def _reply_events(model: str = "claude-opus-4-8", text: str = "Proof text") -> list[dict[str, Any]]:
    return [
        _start(model),
        *_block(0, {"type": "text", "text": ""}, {"type": "text_delta", "text": text}),
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 70}},
        {"type": "message_stop"},
    ]


def _refuse(code: int, headers: dict[str, str] | None = None) -> Any:
    """A reply refusing the request with HTTP ``code`` (JSON error body)."""
    kind = {429: "rate_limit_error", 529: "overloaded_error"}.get(code, "api_error")

    def respond(handler: BaseHTTPRequestHandler) -> None:
        body = json.dumps({"type": "error", "error": {"type": kind, "message": f"refused {code}"}}).encode()
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            handler.send_header(name, value)
        handler.end_headers()
        handler.wfile.write(body)

    return respond


def _script(*replies: Any) -> Any:
    """Answer the n-th request with the n-th reply (the last one repeats)."""
    queue = list(replies)

    def respond(handler: BaseHTTPRequestHandler) -> None:
        (queue.pop(0) if len(queue) > 1 else queue[0])(handler)

    return respond


class AnthropicTransportTests(unittest.TestCase):
    def test_request_on_the_wire_and_a_streamed_reply(self) -> None:
        events = [
            _start("claude-opus-4-8"),
            *_block(0, {"type": "thinking", "thinking": "", "signature": ""},
                    {"type": "signature_delta", "signature": "sig"}),
            *_block(1, {"type": "text", "text": ""}, {"type": "text_delta", "text": "Proof "},
                    {"type": "text_delta", "text": "text"}),
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
             "usage": {"output_tokens": 700, "output_tokens_details": {"thinking_tokens": 450}}},
            {"type": "message_stop"},
        ]
        with _no_proxy(), _Endpoint(lambda handler: _sse(handler, events)) as endpoint, _claude_api(endpoint) as env:
            result = api_backend.api_chat(
                "System contract", "User theorem", model="claude-opus-4-8", effort="max", max_tokens=64000,
                env=env,
            )
        [sent] = endpoint.requests
        # A model without refusal fallbacks uses the plain endpoint, as before.
        self.assertEqual(sent["path"], "/v1/messages")
        self.assertEqual(sent["headers"]["x-api-key"], KEY)
        self.assertEqual(sent["headers"]["anthropic-version"], "2023-06-01")
        self.assertNotIn("authorization", sent["headers"])
        self.assertNotIn("anthropic-beta", sent["headers"])
        self.assertEqual(sent["headers"]["x-stainless-retry-count"], "0")
        self.assertEqual(
            sent["body"],
            {
                "model": "claude-opus-4-8",
                "max_tokens": 64000,
                "system": "System contract",
                "messages": [{"role": "user", "content": "User theorem"}],
                "output_config": {"effort": "max"},
                "stream": True,
            },
        )
        self.assertEqual(result.text, "Proof text")
        self.assertEqual(result.observed_model, "claude-opus-4-8")
        self.assertEqual(
            result.usage,
            {"input_tokens": 1000, "cached_input_tokens": 0, "cache_write_tokens": 0,
             "output_tokens": 700, "reasoning_output_tokens": 450},
        )
        self.assertEqual(result.usage_by_model, ())

    def test_a_gateway_gets_one_plain_request(self) -> None:
        # A custom base URL need not stream (or know the fallback beta).
        def plain(handler: BaseHTTPRequestHandler) -> None:
            body = json.dumps({
                "id": "msg_gw", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": "Gateway proof"}], "stop_reason": "end_turn",
                "stop_sequence": None, "usage": {"input_tokens": 12, "output_tokens": 3},
            }).encode()
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)

        with _no_proxy(), _Endpoint(plain) as endpoint:
            result = api_backend.api_chat(
                "System contract", "User theorem", model="claude-opus-5-5", effort="max", max_tokens=64000,
                timeout=1800, env={**_ENV, "ANTHROPIC_BASE_URL": endpoint.base + "/v1/"},
            )
        [sent] = endpoint.requests
        self.assertEqual(sent["path"], "/v1/messages")
        self.assertNotIn("anthropic-beta", sent["headers"])
        self.assertEqual(sent["headers"]["x-api-key"], KEY)
        self.assertEqual(
            sent["body"],
            {
                "model": "claude-opus-5-5",
                "max_tokens": 64000,
                "system": "System contract",
                "messages": [{"role": "user", "content": "User theorem"}],
                "output_config": {"effort": "max"},
            },
        )
        self.assertEqual((result.text, result.observed_model), ("Gateway proof", "claude-opus-5-5"))
        self.assertEqual((result.usage["input_tokens"], result.usage["output_tokens"]), (12, 3))

    def test_a_refusal_fallback_on_the_wire(self) -> None:
        events = [
            _start("claude-fable-5-1"),
            *_block(0, {"type": "text", "text": ""}, {"type": "text_delta", "text": "Lemma 1 holds "}),
            *_block(1, {"type": "fallback", "from": {"model": "claude-fable-5-1"},
                        "to": {"model": "claude-opus-4-8"}, "trigger": {"type": "refusal", "category": "cyber"}}),
            *_block(2, {"type": "text", "text": ""}, {"type": "text_delta", "text": "by induction."}),
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
             "usage": {"input_tokens": 1100, "output_tokens": 60, "iterations": [
                 {"type": "message", "model": "claude-fable-5-1", "input_tokens": 1000,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 20},
                 {"type": "fallback_message", "model": "claude-opus-4-8", "input_tokens": 1100,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 60}]}},
            {"type": "message_stop"},
        ]
        with _no_proxy(), _Endpoint(lambda handler: _sse(handler, events)) as endpoint, _claude_api(endpoint) as env:
            result = api_backend.api_chat("System contract", "User theorem", model="claude-fable-5-1", env=env)
        [sent] = endpoint.requests
        self.assertEqual(sent["path"], "/v1/messages?beta=true")
        self.assertEqual(sent["headers"]["anthropic-beta"], _BETA)
        self.assertEqual(sent["body"]["fallbacks"], "default")
        self.assertIs(sent["body"]["stream"], True)
        self.assertEqual(result.text, "Lemma 1 holds by induction.")
        self.assertEqual((result.model, result.observed_model), ("claude-fable-5-1", "claude-opus-4-8"))
        self.assertEqual((result.usage["input_tokens"], result.usage["output_tokens"]), (2100, 80))
        self.assertEqual(
            [(model, share["input_tokens"], share["output_tokens"]) for model, share in result.usage_by_model],
            [("claude-fable-5-1", 1000, 20), ("claude-opus-4-8", 1100, 60)],
        )

    def test_redirects_are_never_followed(self) -> None:
        def elsewhere(handler: BaseHTTPRequestHandler) -> None:
            handler.send_response(200)
            handler.send_header("Content-Length", "0")
            handler.end_headers()

        with _no_proxy(), _Endpoint(elsewhere) as target:
            def moved(handler: BaseHTTPRequestHandler) -> None:
                handler.send_response(307)
                handler.send_header("Location", target.base + "/v1/messages?beta=true")
                handler.send_header("Content-Length", "0")
                handler.end_headers()

            with _Endpoint(moved) as endpoint:
                with self.assertRaises(api_backend.APIBackendError) as raised:
                    api_backend.api_chat(
                        "s", "u", model="claude-opus-5-5", env={**_ENV, "ANTHROPIC_BASE_URL": endpoint.base}
                    )
        self.assertEqual(
            str(raised.exception),
            "Anthropic model call failed for claude-opus-5-5 (HTTP 307): redirects are not followed",
        )
        self.assertEqual(len(endpoint.requests), 1)
        self.assertEqual(target.requests, [])

    def test_a_call_ends_at_its_deadline_while_only_pings_arrive(self) -> None:
        # Long thinking can stream nothing the SDK yields for minutes.
        def thinking(handler: BaseHTTPRequestHandler) -> None:
            _sse(handler, [_start("claude-fable-5-1")] + [{"type": "ping"}] * 160, pause=0.05)

        started = time.monotonic()
        with _no_proxy(), _Endpoint(thinking) as endpoint, _claude_api(endpoint) as env:
            with self.assertRaises(api_backend.APIBackendError) as raised:
                api_backend.api_chat("s", "u", model="claude-fable-5-1", timeout=1, env=env)
            elapsed = time.monotonic() - started
            self.assertTrue(endpoint.disconnected.wait(5), "the connection was not closed")
        self.assertEqual(str(raised.exception), "Anthropic model call timed out for claude-fable-5-1 after 1 s")
        # The transport error that ended the read (it holds the request) is not chained.
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)
        self.assertGreaterEqual(elapsed, 0.9)
        self.assertLess(elapsed, 4.0)

    def test_the_event_loop_also_enforces_the_deadline(self) -> None:
        # Events that keep coming past the deadline, with no socket to shut down.
        stream = _FakeStream(iter(lambda: {"type": "content_block_delta"}, None), _message(), pause=0.02)

        class FakeClient:
            def __init__(self, **_kwargs: Any) -> None:
                self.beta = self
                self.messages = self

            def __enter__(self) -> FakeClient:
                return self

            def __exit__(self, *_exc: object) -> None:
                pass

            def stream(self, **_request: Any) -> _FakeStream:
                return stream

        started = time.monotonic()
        with (
            patch.object(anthropic, "Anthropic", FakeClient),
            self.assertRaisesRegex(api_backend.APIBackendError, r"timed out for claude-opus-5-5 after 0.3 s"),
        ):
            api_backend._anthropic_message(
                {"api_key": KEY, "base_url": "https://api.anthropic.com", "timeout": 0.3, "stream": True},
                {"model": "claude-opus-5-5", "max_tokens": 1024, "messages": []},
            )
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(stream.closed)


def _bounded(call: Any, limit: float = 20.0) -> Any:
    """``call()``'s result or exception, failing at once if it is still
    running after ``limit`` seconds (a regression must not hang the suite)."""
    outcome: list[Any] = []

    def run() -> None:
        try:
            outcome.append(call())
        except BaseException as exc:  # noqa: BLE001 - handed back to the test
            outcome.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(limit)
    if thread.is_alive():
        raise AssertionError(f"the call was still running after {limit:g} s")
    return outcome[0]


class AnthropicRetryTests(unittest.TestCase):
    """Only a request refused before processing is sent again, within the deadline."""

    def _call(self, respond: Any, *, timeout: int = 30) -> tuple[Any, _Endpoint, float]:
        started = time.monotonic()
        with _no_proxy(), _Endpoint(respond) as endpoint, _claude_api(endpoint) as env:
            result = _bounded(
                lambda: api_backend.api_chat("s", "u", model="claude-opus-4-8", timeout=timeout, env=env)
            )
        if isinstance(result, BaseException) and not isinstance(result, api_backend.APIBackendError):
            raise result
        return result, endpoint, time.monotonic() - started

    def test_a_rate_limited_request_is_sent_again_after_the_wait_it_names(self) -> None:
        ok = lambda handler: _sse(handler, _reply_events())  # noqa: E731
        for headers in ({"retry-after-ms": "200"}, {"retry-after": "0.2"}):
            with self.subTest(headers=headers):
                result, endpoint, elapsed = self._call(_script(_refuse(429, headers), ok))
                self.assertEqual(result.text, "Proof text")
                self.assertEqual(len(endpoint.requests), 2)
                self.assertGreaterEqual(elapsed, 0.2)
                self.assertEqual(endpoint.requests[0]["body"], endpoint.requests[1]["body"])

    def test_an_overloaded_request_is_sent_again_at_most_twice(self) -> None:
        ok = lambda handler: _sse(handler, _reply_events())  # noqa: E731
        with patch.object(api_backend, "_ANTHROPIC_RETRY_WAITS", (0.05, 0.1)):
            result, endpoint, _elapsed = self._call(_script(_refuse(529), _refuse(529), ok))
            self.assertEqual(result.text, "Proof text")
            self.assertEqual(len(endpoint.requests), 3)
            result, endpoint, _elapsed = self._call(_refuse(529))
        self.assertIsInstance(result, api_backend.APIBackendError)
        self.assertTrue(str(result).startswith("Anthropic model call failed for claude-opus-4-8 (HTTP 529): "))
        self.assertEqual(len(endpoint.requests), 3)

    def test_a_wait_that_does_not_fit_the_deadline_is_not_taken(self) -> None:
        for headers in ({"retry-after": "3600"}, {"retry-after": "26"}):
            with self.subTest(headers=headers):
                result, endpoint, elapsed = self._call(_refuse(429, headers), timeout=30)
                self.assertIsInstance(result, api_backend.APIBackendError)
                self.assertIn("(HTTP 429)", str(result))
                self.assertEqual(len(endpoint.requests), 1)
                self.assertLess(elapsed, 5.0)

    def test_nothing_that_may_have_been_processed_is_sent_again(self) -> None:
        for code in (408, 409, 500, 502, 503, 504):
            with self.subTest(code=code):
                result, endpoint, _elapsed = self._call(_refuse(code))
                self.assertIn(f"(HTTP {code})", str(result))
                self.assertEqual(len(endpoint.requests), 1)

        # Headers that never come: one request, ended at the deadline.
        def silent(handler: BaseHTTPRequestHandler) -> None:
            time.sleep(4)

        result, endpoint, elapsed = self._call(silent, timeout=1)
        self.assertEqual(str(result), "Anthropic model call timed out for claude-opus-4-8 after 1 s")
        self.assertEqual(len(endpoint.requests), 1)
        self.assertLess(elapsed, 3.5)

        # An error event inside the stream (HTTP 200) is not sent again either.
        def broken(handler: BaseHTTPRequestHandler) -> None:
            error = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
            _sse(handler, [_start("claude-opus-4-8"), error])

        result, endpoint, _elapsed = self._call(broken)
        self.assertEqual(str(result), "Anthropic model call failed for claude-opus-4-8 (stream error): Overloaded")
        self.assertEqual(len(endpoint.requests), 1)

    def test_a_gateway_call_keeps_the_same_rules(self) -> None:
        def plain_ok(handler: BaseHTTPRequestHandler) -> None:
            body = json.dumps({
                "id": "msg_gw", "type": "message", "role": "assistant", "model": "claude-opus-4-8",
                "content": [{"type": "text", "text": "Gateway proof"}], "stop_reason": "end_turn",
                "stop_sequence": None, "usage": {"input_tokens": 12, "output_tokens": 3},
            }).encode()
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)

        def gateway(respond: Any, *, timeout: int = 30) -> tuple[Any, _Endpoint, float]:
            started = time.monotonic()
            with _no_proxy(), _Endpoint(respond) as endpoint:
                result = _bounded(lambda: api_backend.api_chat(
                    "s", "u", model="claude-opus-4-8", timeout=timeout,
                    env={**_ENV, "ANTHROPIC_BASE_URL": endpoint.base}))
            return result, endpoint, time.monotonic() - started

        # A 429 is sent again after its wait, as on the Claude API.
        result, endpoint, _elapsed = gateway(_script(_refuse(429, {"retry-after-ms": "100"}), plain_ok))
        self.assertEqual((result.text, len(endpoint.requests)), ("Gateway proof", 2))
        self.assertNotIn("stream", endpoint.requests[1]["body"])

        # A body still arriving at the deadline is ended there (keep-alive bytes
        # reset each read's timeout, not the call's).
        def trickle(handler: BaseHTTPRequestHandler) -> None:
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Transfer-Encoding", "chunked")
            handler.end_headers()
            for _ in range(16):
                handler.wfile.write(b"1\r\n \r\n")
                handler.wfile.flush()
                time.sleep(0.25)
            handler.wfile.write(b"0\r\n\r\n")

        result, endpoint, elapsed = gateway(trickle, timeout=1)
        self.assertEqual(str(result), "Anthropic model call timed out for claude-opus-4-8 after 1 s")
        self.assertEqual(len(endpoint.requests), 1)
        self.assertLess(elapsed, 3.0)

        # A 200 in another API's shape is named as such, not as a cut reply.
        def other_api(handler: BaseHTTPRequestHandler) -> None:
            body = json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode()
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)

        result, _endpoint, _elapsed = gateway(other_api)
        self.assertEqual(str(result), "The reply for claude-opus-4-8 is not an Anthropic Messages response")


class AnthropicStreamEndTests(unittest.TestCase):
    """A reply whose final event arrived is kept; one without it is not."""

    def _call(self, respond: Any, *, timeout: int = 30) -> Any:
        with _no_proxy(), _Endpoint(respond) as endpoint, _claude_api(endpoint) as env:
            result = _bounded(
                lambda: api_backend.api_chat("s", "u", model="claude-opus-4-8", timeout=timeout, env=env)
            )
        if isinstance(result, BaseException) and not isinstance(result, api_backend.APIBackendError):
            raise result
        return result

    def test_a_reply_whose_body_ends_after_the_deadline_is_kept(self) -> None:
        def late_end(handler: BaseHTTPRequestHandler) -> None:
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Transfer-Encoding", "chunked")
            handler.end_headers()
            for event in _reply_events():
                chunk = f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
                handler.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            handler.wfile.flush()
            time.sleep(2.5)  # the watchdog shuts the socket down at 1 s
            handler.wfile.write(b"0\r\n\r\n")

        started = time.monotonic()
        result = self._call(late_end, timeout=1)
        self.assertEqual(getattr(result, "text", result), "Proof text")
        self.assertLess(time.monotonic() - started, 2.4)

    def test_a_connection_closed_after_the_final_event_keeps_the_reply(self) -> None:
        def cut(handler: BaseHTTPRequestHandler) -> None:
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Transfer-Encoding", "chunked")
            handler.end_headers()
            for event in _reply_events():
                chunk = f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
                handler.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            handler.wfile.flush()
            handler.close_connection = True  # no last chunk

        result = self._call(cut)
        self.assertEqual(getattr(result, "text", result), "Proof text")

    def test_a_reply_cut_before_its_final_event_is_not_kept(self) -> None:
        def cut(handler: BaseHTTPRequestHandler) -> None:
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Transfer-Encoding", "chunked")
            handler.end_headers()
            for event in _reply_events()[:-2]:
                chunk = f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
                handler.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            handler.wfile.flush()
            handler.close_connection = True

        result = self._call(cut)
        self.assertIsInstance(result, api_backend.APIBackendError)
        self.assertTrue(str(result).startswith("Anthropic model transport failed for claude-opus-4-8"), str(result))

        # A body that ends cleanly but early is reported as incomplete.
        result = self._call(lambda handler: _sse(handler, _reply_events()[:-2]))
        self.assertEqual(str(result), "Anthropic's reply for claude-opus-4-8 ended before it was complete")

    def test_a_body_without_events_says_so(self) -> None:
        result = self._call(lambda handler: _sse(handler, []))
        self.assertEqual(str(result), "Anthropic's reply for claude-opus-4-8 carried no message")


# ── the CGM event stream prices each call at the model that answered ──────


@contextmanager
def _prices(table: dict[str, dict[str, float]]) -> Any:
    """A fixed LiteLLM-style price table; nothing is downloaded."""
    import monitor_core.llm_cost_tracker as tracker
    from monitor_core.llm_cost_tracker import pricing

    with (
        patch.dict(sys.modules, {"llm_cost_tracker": tracker, "llm_cost_tracker.pricing": pricing}),
        patch.object(pricing, "_pricing_cache", table),
    ):
        yield


_TABLE = {
    "claude-fable-5-1": {"input_cost_per_token": 10e-6, "output_cost_per_token": 50e-6,
                         "cache_read_input_token_cost": 0.25e-6, "cache_creation_input_token_cost": 12.5e-6},
    "gpt-6-astra": {"input_cost_per_token": 10e-6, "output_cost_per_token": 50e-6,
                    "cache_read_input_token_cost": 1e-6},
    "gpt-4o": {"input_cost_per_token": 2.5e-6, "output_cost_per_token": 10e-6,
               "cache_read_input_token_cost": 1.25e-6},
    "claude-opus-4-8": {"input_cost_per_token": 5e-6, "output_cost_per_token": 25e-6,
                        "cache_read_input_token_cost": 0.5e-6, "cache_creation_input_token_cost": 6.25e-6},
}
_USAGE = {"input_tokens": 1500, "cached_input_tokens": 400, "cache_write_tokens": 100, "output_tokens": 200}


def _turns(models: tuple[str | None, str | None]) -> list[dict[str, Any]]:
    """Two harness calls as the CGM runner emits them."""
    events: list[dict[str, Any]] = []
    for stage, model in zip(("strategist", "worker-1"), models):
        usage_event: dict[str, Any] = {"type": "turn.completed", "stage": stage, "usage": dict(_USAGE)}
        if model is not None:
            usage_event.update(model=model, provider="anthropic" if model.startswith("claude") else "openai")
        events += [
            {"type": "item.completed", "item": {"type": "reasoning", "text": f"Math Harness · {stage}"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": f"{stage} reply"}},
            usage_event,
        ]
    return events


def _priced(events: list[dict[str, Any]]) -> tuple[Any, list[dict[str, Any]]]:
    """Parse ``events`` and price each node as jobs' CLI-engine flush does."""
    from agent_monitor import jobs
    from agent_monitor.cli_events import CLIEventParser

    parser = CLIEventParser("codex", source_engine="math_harness")
    parser.feed("".join(json.dumps(event) + "\n" for event in events))
    nodes = parser.agent_nodes("run-1")
    with _prices(_TABLE):
        for node in nodes:
            if node.get("input_tokens") or node.get("output_tokens"):
                node["cost_usd"] = jobs._estimate_cost(
                    parser.usage["model"] or node.get("model"),
                    input_tokens=parser.uncached_input(
                        node["input_tokens"], node["cache_read_tokens"], node["cache_write_tokens"]
                    ),
                    output_tokens=int(node["output_tokens"] or 0),
                    cache_read=int(node["cache_read_tokens"] or 0),
                    cache_write=int(node["cache_write_tokens"] or 0),
                )
    return parser, nodes


class CallModelPricingTests(unittest.TestCase):
    def test_each_call_is_priced_at_the_model_that_answered(self) -> None:
        parser, nodes = _priced(_turns(("claude-fable-5-1", "gpt-6-astra")))
        self.assertIsNone(parser.usage["model"])
        replies = [node for node in nodes if node.get("input_tokens")]
        self.assertEqual([node["model"] for node in replies], ["claude-fable-5-1", "gpt-6-astra"])
        self.assertEqual([node["model"] for node in nodes if not node.get("input_tokens")], [None, None])
        # 1000 plain input, 400 cache reads, 100 cache writes, 200 output.
        fable = 1000 * 10e-6 + 400 * 0.25e-6 + 100 * 12.5e-6 + 200 * 50e-6
        astra = 1000 * 10e-6 + 400 * 1e-6 + 100 * 10e-6 + 200 * 50e-6
        self.assertAlmostEqual(replies[0]["cost_usd"], round(fable, 6))
        self.assertAlmostEqual(replies[1]["cost_usd"], round(astra, 6))

    def test_usage_without_a_model_is_booked_as_before(self) -> None:
        parser, nodes = _priced(_turns((None, None)))
        self.assertIsNone(parser.usage["model"])
        self.assertTrue(all(node["model"] is None for node in nodes))
        gpt_4o = 1000 * 2.5e-6 + 400 * 1.25e-6 + 100 * 2.5e-6 + 200 * 10e-6
        self.assertEqual(
            [node.get("cost_usd") for node in nodes if node.get("input_tokens")], [round(gpt_4o, 6)] * 2
        )
        self.assertEqual(
            {name: parser.usage[name] for name in ("input_tokens", "cache_read_tokens", "output_tokens")},
            {"input_tokens": 3000, "cache_read_tokens": 800, "output_tokens": 400},
        )

    def test_a_node_keeps_the_model_it_already_names(self) -> None:
        from agent_monitor.cli_events import CLIEventParser

        parser = CLIEventParser("codex", source_engine="math_harness")
        events = [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "reply",
                                                "model": "claude-opus-5-5", "model_source": "response"}},
            {"type": "turn.completed", "stage": "verify-1", "usage": dict(_USAGE), "model": "claude-fable-5-1"},
            # A failed call's billed usage lands on its own announcement node.
            {"type": "item.completed", "item": {"type": "reasoning", "text": "Math Harness · worker-2"}},
            {"type": "turn.completed", "stage": "worker-2", "usage": dict(_USAGE), "model": " gpt-6-astra "},
        ]
        parser.feed("".join(json.dumps(event) + "\n" for event in events))
        self.assertEqual(
            [(turn["model"], turn["model_source"]) for turn in parser.turns],
            [("claude-opus-5-5", "response"), ("gpt-6-astra", None)],
        )
        self.assertIsNone(parser.usage["model"])

    def test_each_model_of_a_fallback_call_is_priced_at_its_own_rates(self) -> None:
        from types import SimpleNamespace

        from agent_monitor.runners import math_harness_runner as harness

        shares = (
            ("claude-fable-5-1", {"input_tokens": 1000, "output_tokens": 20}),
            ("claude-opus-4-8", {"input_tokens": 1100, "output_tokens": 60, "reasoning_output_tokens": 15}),
        )
        route = harness.Route("anthropic", "claude-fable-5-1", "api_chat", effort="max", max_tokens=64_000)
        total = {"input_tokens": 2100, "output_tokens": 80, "reasoning_output_tokens": 15}

        def call(api_max: bool, by_model: Any) -> list[dict[str, Any]]:
            events: list[dict[str, Any]] = [
                {"type": "item.completed", "item": {"type": "reasoning", "text": "Math Harness · verify-1"}},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "verify-1 reply"}},
            ]
            harness._emit_call_usage(
                SimpleNamespace(api_max=api_max, label="Math Harness"), "verify-1", total, route=route,
                model="claude-opus-4-8", provider="anthropic", by_model=by_model, out=events.append,
            )
            return events

        events = call(True, shares)
        usage = [event for event in events if event["type"] == "turn.completed"]
        # The model that answered first, so the reply's node names it; then
        # the model that declined, on a node of its own.
        self.assertEqual(
            [(event["stage"], event["model"], event["provider"]) for event in usage],
            [("verify-1", "claude-opus-4-8", "anthropic"), ("verify-1", "claude-fable-5-1", "anthropic")],
        )
        self.assertEqual(events[3]["item"]["text"], "Math Harness · verify-1 · declined first by claude-fable-5-1")
        parser, nodes = _priced(events)
        priced = [(node["model"], node["output"][:30], node["cost_usd"]) for node in nodes if node.get("input_tokens")]
        self.assertEqual(
            priced,
            [("claude-opus-4-8", "verify-1 reply", round(1100 * 5e-6 + 60 * 25e-6, 6)),
             ("claude-fable-5-1", "Math Harness · verify-1 · decl", round(1000 * 10e-6 + 20 * 50e-6, 6))],
        )
        # The run's totals are the call's.
        self.assertEqual((parser.usage["input_tokens"], parser.usage["output_tokens"]), (2100, 80))
        self.assertEqual(parser.usage["reasoning_tokens"], 15)

        # A run that is not Max (API) splits the call the same way, and its
        # usage names no provider.
        with self.subTest(api_max=False, by_model=shares):
            events = call(False, shares)
            usage = [event for event in events if event["type"] == "turn.completed"]
            self.assertEqual(
                [(event["stage"], event["model"], event.get("provider")) for event in usage],
                [("verify-1", "claude-opus-4-8", None), ("verify-1", "claude-fable-5-1", None)],
            )
            self.assertEqual(events[3]["item"]["text"], "Math Harness · verify-1 · declined first by claude-fable-5-1")
            _parser, nodes = _priced(events)
            self.assertEqual(
                [(node["model"], node["output"][:30], node["cost_usd"]) for node in nodes if node.get("input_tokens")],
                priced,
            )

        # One model or a malformed split: one event, as before.
        for api_max, by_model in ((True, ()), (True, shares[:1]), (True, "junk"), (True, ((None, {}),) * 2),
                                  (False, ())):
            with self.subTest(api_max=api_max, by_model=by_model):
                usage = [event for event in call(api_max, by_model) if event["type"] == "turn.completed"]
                self.assertEqual(len(usage), 1)
                self.assertEqual(usage[0]["usage"]["input_tokens"], 2100)
                # Every run's usage names the model it is priced at; only Max
                # (API), whose two sides bill apart, names the provider too.
                self.assertEqual(usage[0].get("model"), "claude-opus-4-8")
                self.assertEqual(usage[0].get("provider"), "anthropic" if api_max else None)
                if not api_max:
                    self.assertEqual(set(usage[0]), {"type", "stage", "usage", "model"})


if __name__ == "__main__":
    unittest.main()
