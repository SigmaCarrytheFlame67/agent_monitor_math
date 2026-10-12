from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import jobs, kimi_k3, lean_verify, proof_graph
from agent_monitor.runners import api_backend


_KIMI_OK = {"choices": [{"message": {"content": "KIMI_OK"}}]}


class KimiRetryTests(unittest.TestCase):
    def test_retry_helper_retries_only_transient_statuses(self) -> None:
        for status in (0, 408, 429, 500, 503, 599):
            with self.subTest(status=status):
                responses = iter([(status, {"error": "temporary"}), (200, _KIMI_OK)])
                calls: list[int] = []

                def request() -> tuple[int, object]:
                    calls.append(1)
                    return next(responses)

                code, body = kimi_k3.request_with_retry(request, sleep=lambda _: None)
                self.assertEqual((code, body), (200, _KIMI_OK))
                self.assertEqual(len(calls), 2)

        for status in (400, 401, 403, 404, 422):
            with self.subTest(status=status):
                calls: list[int] = []

                def request() -> tuple[int, object]:
                    calls.append(1)
                    return status, {"error": "permanent"}

                code, _body = kimi_k3.request_with_retry(request, sleep=lambda _: None)
                self.assertEqual(code, status)
                self.assertEqual(len(calls), 1)

    def test_retry_helper_retries_empty_success_and_caps_attempts(self) -> None:
        responses = iter([(200, {"choices": []}), (200, _KIMI_OK)])
        delays: list[float] = []
        code, body = kimi_k3.request_with_retry(
            lambda: next(responses), sleep=delays.append
        )
        self.assertEqual((code, body), (200, _KIMI_OK))
        self.assertEqual(delays, [0.25])

        calls: list[int] = []

        def always_busy() -> tuple[int, object]:
            calls.append(1)
            return 503, {"error": "busy"}

        kimi_k3.request_with_retry(always_busy, attempts=99, sleep=lambda _: None)
        self.assertEqual(len(calls), 3)

    def test_api_backend_recovers_from_empty_kimi_response(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "KIMI_API_KEY": "test-only-key",
                    "KIMI_API_BASE": "https://kimi.invalid/v1",
                },
                clear=True,
            ),
            patch(
                "agent_monitor.settings._http_json",
                side_effect=[(200, {"choices": []}), (200, _KIMI_OK)],
            ) as request,
            patch("agent_monitor.kimi_k3.time.sleep"),
        ):
            result = api_backend.api_chat("system", "problem", model="kimi-k3")

        self.assertEqual(result.text, "KIMI_OK")
        self.assertEqual(request.call_count, 2)

    def test_api_backend_does_not_retry_kimi_auth_failure(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "KIMI_API_KEY": "test-only-key",
                    "KIMI_API_BASE": "https://kimi.invalid/v1",
                },
                clear=True,
            ),
            patch(
                "agent_monitor.settings._http_json",
                return_value=(401, {"error": {"message": "invalid key"}}),
            ) as request,
            patch("agent_monitor.kimi_k3.time.sleep"),
        ):
            with self.assertRaisesRegex(api_backend.APIBackendError, "HTTP 401"):
                api_backend.api_chat("system", "problem", model="kimi-k3")

        self.assertEqual(request.call_count, 1)

    def test_proof_graph_recovers_after_connection_and_server_failures(self) -> None:
        with (
            patch(
                "agent_monitor.settings._http_json",
                side_effect=[
                    (0, "connection reset"),
                    (503, {"error": {"message": "busy"}}),
                    (200, _KIMI_OK),
                ],
            ) as request,
            patch("agent_monitor.kimi_k3.time.sleep"),
        ):
            chosen, content, provider = proof_graph._call_llm(
                {
                    "KIMI_API_KEY": "test-only-key",
                    "KIMI_API_BASE": "https://kimi.invalid/v1",
                },
                "kimi-k3",
                "problem",
            )

        self.assertEqual((chosen, content, provider), ("kimi-k3", "KIMI_OK", "kimi"))
        self.assertEqual(request.call_count, 3)


_KIMI_EMPTY_LENGTH = {
    "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
    "usage": {
        "prompt_tokens": 9000,
        "completion_tokens": 8192,
        "completion_tokens_details": {"reasoning_tokens": 8192},
    },
}


class KimiEmptyCompletionTests(unittest.TestCase):
    """Live 2026-09-27: every Lean prove/repair call came back empty."""

    def test_each_retry_after_an_empty_completion_lowers_reasoning_effort(self) -> None:
        payload = {"reasoning_effort": "max"}
        seen: list[str] = []
        replies = iter([(200, _KIMI_EMPTY_LENGTH), (200, _KIMI_EMPTY_LENGTH), (200, _KIMI_OK)])

        def request() -> tuple[int, object]:
            seen.append(payload["reasoning_effort"])
            return next(replies)

        code, body = kimi_k3.request_with_retry(
            request,
            sleep=lambda _: None,
            before_retry=lambda c, b: kimi_k3.lower_effort_after_empty(payload, c, b),
        )
        self.assertEqual((code, body), (200, _KIMI_OK))
        self.assertEqual(seen, ["max", "high", "low"])

    def test_transient_errors_do_not_lower_effort(self) -> None:
        payload = {"reasoning_effort": "max"}
        self.assertFalse(kimi_k3.lower_effort_after_empty(payload, 503, {"error": "busy"}))
        self.assertFalse(kimi_k3.lower_effort_after_empty(payload, 200, _KIMI_OK))
        self.assertEqual(payload["reasoning_effort"], "max")
        low = {"reasoning_effort": "low"}
        self.assertFalse(kimi_k3.lower_effort_after_empty(low, 200, _KIMI_EMPTY_LENGTH))

    def test_empty_completion_summary_is_content_free(self) -> None:
        summary = kimi_k3.empty_completion_summary(_KIMI_EMPTY_LENGTH)
        self.assertEqual(
            summary, "finish_reason=length, output_tokens=8192, reasoning_tokens=8192"
        )
        self.assertEqual(kimi_k3.empty_completion_summary("oops"), "no JSON body")

    def test_proof_graph_steps_effort_down_and_reports_finish_reason(self) -> None:
        sent: list[str] = []

        def fake_http(url, *, method, headers, payload, timeout):
            sent.append(payload["reasoning_effort"])
            return 200, _KIMI_EMPTY_LENGTH

        with (
            patch("agent_monitor.settings._http_json", side_effect=fake_http),
            patch("agent_monitor.kimi_k3.time.sleep"),
            self.assertLogs("agent_monitor.proof_graph", level="WARNING") as logs,
        ):
            with self.assertRaises(ValueError) as raised:
                proof_graph._call_llm(
                    {
                        "KIMI_API_KEY": "test-only-key",
                        "KIMI_API_BASE": "https://kimi.invalid/v1",
                    },
                    "kimi-k3",
                    "problem",
                )
        self.assertEqual(sent, ["max", "high", "low"])
        message = str(raised.exception)
        self.assertIn("empty response for kimi-k3", message)
        self.assertIn("finish_reason=length", message)
        self.assertNotIn("test-only-key", message)
        self.assertIn("finish_reason=length", "\n".join(logs.output))
        self.assertNotIn("test-only-key", "\n".join(logs.output))

    def test_api_backend_empty_error_names_the_finish_reason(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "KIMI_API_KEY": "test-only-key",
                    "KIMI_API_BASE": "https://kimi.invalid/v1",
                    "AGENT_MONITOR_SPONSORED_KIMI": "1",
                },
                clear=True,
            ),
            patch("agent_monitor.settings._http_json", return_value=(200, _KIMI_EMPTY_LENGTH)),
            patch("agent_monitor.kimi_k3.time.sleep"),
        ):
            with self.assertRaisesRegex(
                api_backend.APIBackendError,
                r"empty response for kimi-k3 \(finish_reason=length",
            ):
                api_backend.api_chat("system", "problem", model="kimi-k3")


class KimiRoutingTests(unittest.TestCase):
    def test_formal_question_never_falls_back_from_explicit_kimi_to_codex(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("agent_monitor.settings.resolved_user_env", return_value={}),
            patch.object(lean_verify, "_chat", side_effect=ValueError("Kimi HTTP 503")),
            patch.object(lean_verify, "_codex_subscription_interaction") as codex,
        ):
            with self.assertRaisesRegex(ValueError, "Kimi HTTP 503"):
                lean_verify._answer_lean_question(
                    workspace=Path(tmp),
                    user={"id": 7},
                    question="Which lemma should replace this step?",
                    model="kimi-k3",
                    lean_src="theorem main (p : Prop) (h : p) : p := h",
                    cached={},
                )

        codex.assert_not_called()

    def test_formal_edit_never_falls_back_from_explicit_kimi_to_codex(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("agent_monitor.settings.resolved_user_env", return_value={}),
            patch.object(lean_verify, "_chat", side_effect=ValueError("Kimi HTTP 503")),
            patch.object(lean_verify, "_codex_subscription_interaction") as codex,
        ):
            with self.assertRaisesRegex(ValueError, "Kimi HTTP 503"):
                lean_verify._revise_with_feedback_impl(
                    workspace=Path(tmp),
                    user={"id": 7},
                    message="Replace the proof term with an explicit exact step",
                    model="kimi-k3",
                    lean="theorem main (p : Prop) (h : p) : p := h",
                )

        codex.assert_not_called()

    def test_openhands_kimi_selection_overwrites_inherited_llm_model(self) -> None:
        env = {
            "KIMI_API_KEY": "test-only-key",
            "LLM_MODEL": "anthropic/old-model",
        }
        jobs._apply_selected_model_env(env, engine="openhands", model="kimi-k3")
        self.assertEqual(env["LLM_MODEL"], "openai/kimi-k3")
        self.assertEqual(env["AGENT_MONITOR_OPENHANDS_MODEL"], "openai/kimi-k3")


if __name__ == "__main__":
    unittest.main()
