"""Ansatz CGM "Max (API)" in the harness: an Anthropic API advisor (strategist,
verifier, curator, write-up checker) and an OpenAI API worker (workers,
repairs, write-up writer), both at maximum effort, each on its own key."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_monitor import cgm_max, engine_graph
from agent_monitor.cgm import prompts as cgm_prompts
from agent_monitor.cgm import schema as cgm_schema
from agent_monitor.cgm import writeup as cgm_writeup
from agent_monitor.proof_graph import _resolve_llm_provider
from agent_monitor.runners import analyze_runner, readability_runner
from agent_monitor.runners import math_harness_runner as runner
from agent_monitor.runners.api_backend import APIBackendError

from tests.test_cgm_max import _Providers, _run_max
from tests.test_math_harness_runner import (
    PROBLEM,
    _accepted_outputs,
    _api_run,
    _curator,
    _in_directory,
    _ok,
    _receipt,
    _strategist,
    _worker,
    _wrong,
)

ADVISOR = cgm_max.API_ADVISOR_MODELS[0]
WORKER = cgm_max.API_WORKER_MODELS[0]
ANTHROPIC_KEY = "sk-ant-advisor-test-key"
OPENAI_KEY = "sk-openai-worker-test-key"
WRITEUP = (
    "=== WRITEUP ===\n**Theorem.** The sum of two even integers is even.\n\n"
    "**Proof.** Write $a = 2m$ and $b = 2n$; then $a + b = 2(m + n)$. $\\blacksquare$\n"
    "=== END ==="
)


def _api_max_env(overrides: dict[str, str | None] | None = None) -> dict[str, str]:
    """A Max (API) child env as jobs builds it: the run owner's two keys, then
    ``cgm_max.apply_models_api``.  An override of ``None`` removes a name."""
    env = {
        "AGENT_MONITOR_AUTH_MODE": "api_key",
        "ANTHROPIC_API_KEY": ANTHROPIC_KEY,
        "ANTHROPIC_BASE_URL": "https://anthropic.invalid",
        "OPENAI_API_KEY": OPENAI_KEY,
        "OPENAI_BASE_URL": "https://openai.invalid/v1",
        "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "2",
        "AGENT_MONITOR_MAX_ITERATIONS": "20",
        "AGENT_MONITOR_PROBLEM_ID": "max-api-case",
        # Queued replies are handed out in call order, defined one call at a time.
        "AGENT_MONITOR_CGM_CONCURRENCY": "1",
    }
    cgm_max.apply_models_api(env, cgm_max.record_plan_api({}))
    env.update(overrides or {})
    return {name: value for name, value in env.items() if value is not None}


class _Apis:
    """Scripted Anthropic (advisor) and OpenAI (worker) API replies.

    Each call resolves its provider and key from the env it was given, as
    api_chat does, and is answered from that provider's queue; a reply that
    is an exception is raised instead.  ``relabel`` makes the n-th call of a
    provider (1-based) report another provider.
    """

    def __init__(self, advisor: list, worker: list, *, relabel: dict | None = None):
        self.queues = {"anthropic": list(advisor), "openai": list(worker)}
        self.relabel = dict(relabel or {})
        self.calls: list[dict] = []

    def __call__(
        self,
        system: str,
        user: str,
        *,
        model: str,
        timeout: int = 240,
        effort: str | None = None,
        max_tokens: int | None = None,
        env=None,
        retry_allowance=None,
    ):
        provider, _base, key, _chosen = _resolve_llm_provider(dict(env or {}), model)
        call = {
            "system": system,
            "user": user,
            "model": model,
            "timeout": timeout,
            "effort": effort,
            "max_tokens": max_tokens,
            "env": dict(env or {}),
            "retry_allowance": retry_allowance,
            "provider": provider,
            "key": key,
        }
        self.calls.append(call)
        queue = self.queues[provider]
        if not queue:
            raise AssertionError(f"unexpected {provider} call")
        reply = queue.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        answered_by = self.relabel.get((provider, len(self.side(provider))), provider)
        return SimpleNamespace(
            text=reply,
            usage=(
                {"input_tokens": 5, "output_tokens": 9}
                if provider == "anthropic"
                else {"input_tokens": 7, "output_tokens": 30, "reasoning_output_tokens": 12}
            ),
            model=model,
            provider=answered_by,
            # OpenAI answers with a dated snapshot of the requested model.
            observed_model=model if provider == "anthropic" else f"{model}-2026-10-01",
        )

    def side(self, provider: str) -> list[dict]:
        return [call for call in self.calls if call["provider"] == provider]


def _run_api_max(workspace: Path, apis: _Apis, env: dict[str, str | None] | None = None):
    events: list[dict] = []
    with (
        _in_directory(workspace),
        patch.dict(runner.os.environ, _api_max_env(env), clear=True),
        patch.object(runner.sys, "argv", ["runner", PROBLEM]),
        patch.object(runner, "api_chat", side_effect=apis),
        patch.object(runner, "codex_exec") as codex,
        patch.object(runner, "claude_code_exec") as claude,
        patch.object(runner, "emit", side_effect=events.append),
    ):
        before = dict(os.environ)
        code = runner.main()
        unchanged = dict(os.environ) == before
    # Never a linked subscription, and the process environment is only read.
    codex.assert_not_called()
    claude.assert_not_called()
    assert unchanged
    return code, events


def _usage_events(events: list[dict]) -> list[dict]:
    return [event for event in events if event.get("type") == "turn.completed"]


def _banner(events: list[dict]) -> str:
    return next(e for e in events if (e.get("item") or {}).get("type") == "agent_message")["item"]["text"]


def _facts(workspace: Path) -> set[Path]:
    return set((workspace / "math_harness" / "project" / "fact_graph" / "facts").glob("*.md"))


def _attestation(workspace: Path) -> str:
    return engine_graph.build(workspace, {"engine": "math_harness"})["provenance"]["attestation"]


def _receipt_attests(workspace: Path) -> bool:
    """Whether engine_graph accepts the published receipt as written."""
    path = workspace / "math_harness" / "project" / "engine_receipt.json"
    return engine_graph._cgm_receipt(path.read_bytes()) is not None


def _accepted_api_max_run(workspace: Path) -> None:
    apis = _Apis([_strategist(2), _ok(), _ok(), _curator()], [_worker("first"), _worker("second")])
    code, _events = _run_api_max(workspace, apis)
    assert code == 0


class ApiMaxRouteSelectionTests(unittest.TestCase):
    def test_routes_are_an_anthropic_advisor_and_an_openai_worker_at_max_effort(self) -> None:
        env = _api_max_env()
        advisor, worker = runner._select_routes(env)
        self.assertEqual(advisor, runner.Route("anthropic", ADVISOR, "api_chat", effort="max", max_tokens=64_000))
        self.assertEqual(worker, runner.Route("openai", WORKER, "api_chat", effort="max", max_tokens=64_000))
        self.assertTrue(runner._max_api_mode(env))
        self.assertFalse(runner._max_mode(env))
        self.assertTrue(runner._is_api_max_pair(advisor, worker))
        # Concurrency keeps the API default unless the env sets it.
        default = runner.Budgets.from_env(_api_max_env({"AGENT_MONITOR_CGM_CONCURRENCY": None}))
        self.assertEqual(default.resolved_concurrency(advisor), 3)
        # Any allowlisted pair the operator pins is used as is.
        for advisor_model in cgm_max.API_ADVISOR_MODELS:
            for worker_model in cgm_max.API_WORKER_MODELS:
                routes = runner._select_routes(
                    _api_max_env(
                        {
                            cgm_max.API_ADVISOR_MODEL_ENV: advisor_model,
                            cgm_max.API_WORKER_MODEL_ENV: worker_model,
                        }
                    )
                )
                self.assertEqual([r.model for r in routes], [advisor_model, worker_model])
        # The output ceiling is clamped as api_chat clamps it; a subscription
        # flag explicitly off is not a subscription.
        clamped = runner._select_routes(
            _api_max_env({"AGENT_MONITOR_API_MAX_TOKENS": "999999", "AGENT_MONITOR_CODEX_SUBSCRIPTION": "0"})
        )
        self.assertEqual([r.max_tokens for r in clamped], [128_000, 128_000])
        self.assertEqual(
            runner._select_routes(_api_max_env({"AGENT_MONITOR_API_MAX_TOKENS": "10"}))[1].max_tokens, 1024
        )

    def test_refusals_come_before_any_call(self) -> None:
        cases = {
            "no Anthropic key": ({"ANTHROPIC_API_KEY": None}, "Max (API) needs an Anthropic API key"),
            "blank OpenAI key": ({"OPENAI_API_KEY": "  "}, "Max (API) needs an OpenAI API key"),
            "an OpenRouter key instead of OpenAI": (
                {"OPENAI_API_KEY": None, "OPENROUTER_API_KEY": "sk-or-test"},
                "Max (API) needs an OpenAI API key",
            ),
            "advisor not allowlisted": (
                {cgm_max.API_ADVISOR_MODEL_ENV: "claude-haiku-4-5"},
                "advisor model must be one of",
            ),
            "advisor as a Claude Code alias": (
                {cgm_max.API_ADVISOR_MODEL_ENV: "fable"},
                "advisor model must be one of",
            ),
            "advisor through OpenRouter": (
                {cgm_max.API_ADVISOR_MODEL_ENV: "anthropic/claude-fable-5-1"},
                "advisor model must be one of",
            ),
            "advisor with a provider prefix": (
                {cgm_max.API_ADVISOR_MODEL_ENV: "anthropic:claude-fable-5-1"},
                "advisor model must be one of",
            ),
            "no worker model": ({cgm_max.API_WORKER_MODEL_ENV: None}, "worker model must be one of"),
            "the advisor model as worker": ({cgm_max.API_WORKER_MODEL_ENV: ADVISOR}, "worker model must be one of"),
            "Claude Code subscription selected": ({"AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1"}, "API keys only"),
            "Codex subscription selected": ({"AGENT_MONITOR_CODEX_SUBSCRIPTION": "1"}, "API keys only"),
            "guest": ({"AGENT_MONITOR_GUEST": "1"}, "not available to guest"),
            "public": ({"AGENT_MONITOR_PUBLIC_KIMI": "1"}, "not available to guest"),
            "sponsored": ({"AGENT_MONITOR_SPONSORED_KIMI": "1"}, "not available to guest"),
            "invalid output ceiling": (
                {"AGENT_MONITOR_API_MAX_TOKENS": "lots"},
                "AGENT_MONITOR_API_MAX_TOKENS must be an integer",
            ),
            "invalid call timeout": (
                {"AGENT_MONITOR_CGM_API_TIMEOUT": "soon"},
                "AGENT_MONITOR_CGM_API_TIMEOUT must be an integer",
            ),
        }
        for name, (overrides, message) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as raw:
                workspace = Path(raw)
                apis = _Apis([_strategist(1)], [_worker("never")])
                code, events = _run_api_max(workspace, apis, overrides)
                self.assertEqual(code, 1)
                errors = [e["item"] for e in events if (e.get("item") or {}).get("type") == "error"]
                self.assertEqual(len(errors), 1)
                self.assertIn(message, errors[0]["message"])
                self.assertTrue(errors[0]["terminal"])
                self.assertEqual(apis.calls, [])
                self.assertFalse((workspace / "math_harness").exists())
                self.assertFalse((workspace / "proof.md").exists())
                # Never a key in the refusal.
                self.assertNotIn(ANTHROPIC_KEY, json.dumps(events))
                self.assertNotIn(OPENAI_KEY, json.dumps(events))

    def test_route_modes_never_fall_through_to_one_route(self) -> None:
        single = {"AGENT_MONITOR_SELECTED_MODEL": "kimi-k3", "KIMI_API_KEY": "k"}
        for mode in ("maximum", "max-api", "api"):
            with self.subTest(mode), self.assertRaisesRegex(runner.MathHarnessError, "unknown CGM route mode"):
                runner._select_routes({**single, "AGENT_MONITOR_CGM_ROUTE_MODE": mode})
        # Unchanged: no mode is one route, and Max still needs both subscriptions.
        self.assertEqual(
            runner._select_routes({**single, "AGENT_MONITOR_CGM_ROUTE_MODE": " "}),
            (runner.Route("api", "kimi-k3", "api_chat"), None),
        )
        with self.assertRaisesRegex(runner.MathHarnessError, "Max needs both the Claude Code and the Codex"):
            runner._select_routes({**_api_max_env(), "AGENT_MONITOR_CGM_ROUTE_MODE": "max"})

    def test_route_tables_name_both_max_modes(self) -> None:
        self.assertEqual(set(cgm_schema.HYBRID_ROUTE_TABLES), {cgm_max.MODEL_ID, cgm_max.API_MODEL_ID})
        table = cgm_schema.HYBRID_ROUTE_TABLES[cgm_max.API_MODEL_ID]
        self.assertEqual({side: provider for side, (provider, _t) in table.items()}, cgm_max.API_PROVIDERS)


class ApiMaxRunnerTests(unittest.TestCase):
    def test_roles_split_between_the_anthropic_and_openai_apis_each_on_its_own_key(self) -> None:
        apis = _Apis([_strategist(2), _ok(), _ok(), _curator()], [_worker("first"), _worker("second")])
        # Neither the run profile nor Kimi's per-stage effort applies to a side.
        env = {
            "AGENT_MONITOR_RUN_REASONING_EFFORT": "high",
            "AGENT_MONITOR_CGM_REASONING_EFFORT": "low",
            "KIMI_REASONING_EFFORT": "low",
            "KIMI_API_KEY": "sk-kimi-never-sent",
            "OPENAI_API_BASE": "https://openai-alt.invalid/v1",
        }
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            code, events = _run_api_max(workspace, apis, env)
            self.assertEqual(code, 0)
            advisor, worker = apis.side("anthropic"), apis.side("openai")
            self.assertEqual(
                [call["system"] for call in advisor],
                [
                    cgm_prompts.STRATEGIST_SYSTEM,
                    cgm_prompts.VERIFIER_SYSTEM,
                    cgm_prompts.VERIFIER_SYSTEM,
                    cgm_prompts.CURATOR_SYSTEM,
                ],
            )
            self.assertEqual(len(worker), 2)
            self.assertTrue(all(call["system"].startswith(cgm_prompts.WORKER_SYSTEM) for call in worker))
            for call in apis.calls:
                # Maximum effort on every stage, the curator included; the
                # output ceiling and the long per-call limit are explicit.
                self.assertEqual((call["effort"], call["max_tokens"], call["timeout"]), ("max", 64_000, 1800))
                self.assertIsNone(call["retry_allowance"])
                for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "AGENT_MONITOR_CODEX_SUBSCRIPTION",
                             "AGENT_MONITOR_CLAUDE_SUBSCRIPTION", "KIMI_API_KEY"):
                    self.assertNotIn(name, call["env"])
            for call in advisor:
                self.assertEqual((call["model"], call["key"]), (ADVISOR, ANTHROPIC_KEY))
                self.assertEqual(call["env"]["ANTHROPIC_BASE_URL"], "https://anthropic.invalid")
                for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE"):
                    self.assertNotIn(name, call["env"])
            for call in worker:
                self.assertEqual((call["model"], call["key"]), (WORKER, OPENAI_KEY))
                self.assertEqual(call["env"]["OPENAI_BASE_URL"], "https://openai.invalid/v1")
                self.assertNotIn("ANTHROPIC_API_KEY", call["env"])
                self.assertNotIn("ANTHROPIC_BASE_URL", call["env"])
            # Workers see the strategist's plans; verifiers see the workers' proofs.
            self.assertIn("Route 1", worker[0]["user"])
            self.assertIn("first", advisor[1]["user"] + advisor[2]["user"])

            receipt = _receipt(workspace)
            self.assertEqual(receipt["schema_version"], cgm_schema.HYBRID_RECEIPT_SCHEMA_VERSION)
            self.assertEqual(receipt["verdict"], "accepted")
            self.assertEqual(receipt["transport"], "hybrid")
            self.assertEqual(receipt["workflow"]["response_only_contract"], cgm_schema.HYBRID_CONTRACT)
            self.assertEqual((receipt["provider"], receipt["model"]), ("anthropic", ADVISOR))
            self.assertEqual(receipt["requested_model"], cgm_max.API_MODEL_ID)
            self.assertEqual(
                receipt["workflow"]["routes"],
                {
                    "advisor": {
                        "families": ["curator", "strategist", "verify"],
                        "provider": "anthropic",
                        "model": ADVISOR,
                        "transport": "api_chat",
                        "isolation_version": 1,
                        "effort": "max",
                    },
                    "worker": {
                        "families": ["repair", "worker"],
                        "provider": "openai",
                        "model": WORKER,
                        "transport": "api_chat",
                        "isolation_version": 1,
                        "effort": "max",
                    },
                },
            )
            self.assertEqual(receipt["workflow"]["budgets"]["concurrency"], 1)
            self.assertEqual(
                [(call["stage"], call["provider"], call["model"], call["effort"]) for call in receipt["calls"]],
                [
                    ("strategist", "anthropic", ADVISOR, "max"),
                    ("worker-1", "openai", WORKER, "max"),
                    ("worker-2", "openai", WORKER, "max"),
                    ("verify-1", "anthropic", ADVISOR, "max"),
                    ("verify-2", "anthropic", ADVISOR, "max"),
                    ("curator", "anthropic", ADVISOR, "max"),
                ],
            )
            # The published graph attests the run and names the hybrid contract.
            provenance = engine_graph.build(workspace, {"engine": "math_harness"})["provenance"]
            self.assertEqual(provenance["attestation"], "harness-gate-receipt")
            self.assertEqual(provenance["receipt_status"], "verifier-correct")
            self.assertEqual(provenance["response_only_transport"], "hybrid")
            self.assertEqual(provenance["verifier_model"], ADVISOR)

            self.assertIn(
                f"1 in flight · Max (API): {ADVISOR} plans, verifies and curates through the "
                f"Anthropic API; {WORKER} writes and repairs through the OpenAI API · no tools",
                _banner(events),
            )
            scaffold = next(e for e in events if e.get("type") == "pipeline.stage" and e["stage"] == "scaffold")
            self.assertEqual(
                scaffold["detail"], f"Max (API): {ADVISOR} (Anthropic API) + {WORKER} (OpenAI API) · no tools"
            )
            # Each call's usage names its billing model and provider: the
            # Anthropic model that answered, and the requested OpenAI id (its
            # replies name a dated snapshot, which may have no price of its own).
            usage = _usage_events(events)
            self.assertEqual(
                [(e["stage"], e["model"], e["provider"]) for e in usage],
                [
                    ("strategist", ADVISOR, "anthropic"),
                    ("worker-1", WORKER, "openai"),
                    ("worker-2", WORKER, "openai"),
                    ("verify-1", ADVISOR, "anthropic"),
                    ("verify-2", ADVISOR, "anthropic"),
                    ("curator", ADVISOR, "anthropic"),
                ],
            )
            self.assertTrue(all(not e["usage"].get("input_excludes_cache") for e in usage))
            self.assertEqual(usage[1]["usage"]["reasoning_output_tokens"], 12)
            for text in (json.dumps(events), json.dumps(receipt), (workspace / "proof.md").read_text()):
                self.assertNotIn(ANTHROPIC_KEY, text)
                self.assertNotIn(OPENAI_KEY, text)

    def test_a_run_at_the_default_concurrency_is_accepted_and_priced_per_call(self) -> None:
        # Real runs keep 3 calls in flight on API routes; every other
        # end-to-end test pins 1 so queued replies come in call order.  Here
        # each call is answered by its role instead.
        class _ByRole(_Apis):
            def __init__(self) -> None:
                super().__init__([], [])
                self.workers = 0

            def __call__(self, system, user, **kwargs):
                if system.startswith(cgm_prompts.STRATEGIST_SYSTEM[:200]):
                    reply = _strategist(3)
                elif system == cgm_prompts.WORKER_SYSTEM or system.startswith(cgm_prompts.WORKER_SYSTEM[:200]):
                    self.workers += 1
                    reply = _worker(f"w{self.workers}")
                elif system.startswith(cgm_prompts.VERIFIER_SYSTEM[:200]):
                    reply = _ok()
                elif system.startswith(cgm_prompts.CURATOR_SYSTEM[:200]):
                    reply = _curator()
                else:
                    raise AssertionError(f"unexpected call: {system[:60]!r}")
                provider = "anthropic" if kwargs["model"].startswith("claude") else "openai"
                self.queues[provider].append(reply)
                return super().__call__(system, user, **kwargs)

        apis = _ByRole()
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            code, events = _run_api_max(
                workspace, apis,
                {"AGENT_MONITOR_CGM_CONCURRENCY": None, "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "3"},
            )
            self.assertEqual(code, 0)
            receipt = _receipt(workspace)
            self.assertEqual(receipt["verdict"], "accepted")
            self.assertEqual(receipt["workflow"]["budgets"]["concurrency"], 3)
            self.assertEqual(_attestation(workspace), "harness-gate-receipt")
            self.assertTrue(_receipt_attests(workspace))
        # Three workers ran on OpenAI; each call is booked once, on its own side.
        self.assertEqual(len(apis.side("openai")), 3)
        usage = _usage_events(events)
        self.assertEqual(len(usage), len(apis.calls))
        for event in usage:
            side = "openai" if event["stage"].startswith(("worker", "repair")) else "anthropic"
            self.assertEqual((event["provider"], event["model"]), (side, WORKER if side == "openai" else ADVISOR))
        # Each usage event follows its own reply directly (one unit per call).
        for index, event in enumerate(events):
            if event.get("type") == "turn.completed":
                self.assertEqual((events[index - 1].get("item") or {}).get("type"), "agent_message", event)

    def test_repairs_and_format_retries_stay_on_their_side(self) -> None:
        apis = _Apis(
            ["Not the strategist's JSON.", _strategist(1), _wrong(), _ok(), _curator()],
            ["No markers at all.", _worker("only", lemma=False), _worker("repaired", lemma=False)],
        )
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            code, events = _run_api_max(workspace, apis, {"AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1"})
            self.assertEqual(code, 0)
            receipt = _receipt(workspace)
            self.assertEqual(receipt["verdict"], "accepted")
            self.assertEqual(
                [(call["stage"], call["provider"]) for call in receipt["calls"]],
                [
                    ("strategist", "anthropic"),
                    ("strategist-retry", "anthropic"),
                    ("worker-1", "openai"),
                    ("worker-1-retry", "openai"),
                    ("verify-1", "anthropic"),
                    ("repair-1", "openai"),
                    ("verify-2", "anthropic"),
                    ("curator", "anthropic"),
                ],
            )
            self.assertEqual(receipt["workflow"]["retries"], {"format": 2, "empty": 0})
            self.assertTrue(all(call["effort"] == "max" for call in apis.calls))
            # The format retry resends the worker's own reply to the worker.
            self.assertIn("<PREVIOUS_REPLY>\nNo markers at all.", apis.side("openai")[1]["user"])
            self.assertEqual(_attestation(workspace), "harness-gate-receipt")

    def test_an_exhausted_openai_key_takes_no_more_repairs_while_anthropic_carries_on(self) -> None:
        quota = APIBackendError(
            "Openai model call failed for gpt-6-astra (HTTP 429): You exceeded your current quota "
            "(insufficient_quota)",
            kind="usage_limit",
        )
        apis = _Apis(
            [_strategist(2), _wrong(), _wrong(), _curator()],
            [_worker("first", lemma=False), _worker("second", lemma=False), quota, quota],
        )
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            code, events = _run_api_max(workspace, apis)
            self.assertEqual(code, 0)
            receipt = _receipt(workspace)
            # The second rejection gets no repair: the OpenAI key would refuse it too.
            self.assertEqual(len(apis.side("openai")), 3)
            self.assertEqual(
                [(call["stage"], call["provider"]) for call in receipt["calls"]],
                [
                    ("strategist", "anthropic"),
                    ("worker-1", "openai"),
                    ("worker-2", "openai"),
                    ("verify-1", "anthropic"),
                    ("repair-1", "openai"),
                    ("verify-2", "anthropic"),
                    ("curator", "anthropic"),
                ],
            )
            # The Anthropic side, on its own key, still verifies and curates.
            self.assertEqual(len(apis.side("anthropic")), 4)
            self.assertIn("repair-provider-failure", receipt["workflow"]["degraded"])
            self.assertNotIn("curator-skipped-by-provider-failure", receipt["workflow"]["degraded"])
            self.assertEqual(receipt["verdict"], "unattested")
            self.assertTrue(_receipt_attests(workspace))

    def test_a_key_exhausted_by_a_worker_takes_no_repair(self) -> None:
        quota = APIBackendError(
            "Openai model call failed for gpt-6-astra (HTTP 429): You have no credits remaining.",
            kind="usage_limit",
        )
        apis = _Apis(
            [_strategist(2), _wrong(), _curator()],
            [_worker("first", lemma=False), quota],
        )
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            code, _events = _run_api_max(workspace, apis)
            self.assertEqual(code, 0)
            receipt = _receipt(workspace)
            # worker-2 found the key out of credit: the rejected candidate gets
            # no repair call, which the same key would refuse.
            self.assertEqual(len(apis.side("openai")), 2)
            self.assertNotIn("repair-1", [call["stage"] for call in receipt["calls"]])
            self.assertIn("worker-provider-failure", receipt["workflow"]["degraded"])
            self.assertNotIn("repair-provider-failure", receipt["workflow"]["degraded"])
            self.assertEqual(len(apis.side("anthropic")), 3)

    def test_a_key_exhausted_by_a_worker_stops_the_later_waves(self) -> None:
        quota = APIBackendError(
            "Openai model call failed for gpt-6-astra (HTTP 429): You have no credits remaining.",
            kind="usage_limit",
        )
        # One worker per wave (concurrency 1): worker-3 never calls the key.
        apis = _Apis([_strategist(3), _ok(), _ok(), _curator()], [_worker("first"), quota])
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            code, _events = _run_api_max(workspace, apis, {"AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "3"})
            self.assertEqual(code, 0)
            receipt = _receipt(workspace)
            self.assertEqual(len(apis.side("openai")), 2)
            self.assertNotIn("worker-3", [call["stage"] for call in receipt["calls"]])
            self.assertIn("worker-route-stopped", receipt["workflow"]["degraded"])
            self.assertEqual(receipt["verdict"], "accepted")

    def test_the_receipt_names_the_verifiers_model(self) -> None:
        # A refusal fallback can answer any one advisor call on another model:
        # the receipt (engine_graph's verifier_model) names the model of the
        # last verify answer, never the curator's.
        for fallback_call, expected in ((4, ADVISOR), (3, "claude-opus-4-8")):
            class _FallbackApis(_Apis):
                def __call__(self, *args, **kwargs):
                    result = super().__call__(*args, **kwargs)
                    if result.provider == "anthropic" and len(self.side("anthropic")) == fallback_call:
                        result.observed_model = "claude-opus-4-8"
                    return result

            apis = _FallbackApis([_strategist(2), _ok(), _ok(), _curator()], [_worker("first"), _worker("second")])
            with self.subTest(fallback_call=fallback_call), tempfile.TemporaryDirectory() as raw:
                workspace = Path(raw)
                code, _events = _run_api_max(workspace, apis)
                self.assertEqual(code, 0)
                receipt = _receipt(workspace)
                self.assertEqual(receipt["model"], expected)
                provenance = engine_graph.build(workspace, {"engine": "math_harness"})["provenance"]
                self.assertEqual(provenance["verifier_model"], expected)
                self.assertEqual(provenance["attestation"], "harness-gate-receipt")

    def test_a_rejected_anthropic_key_ends_verification_and_curation(self) -> None:
        rejected = APIBackendError(
            "Anthropic model call failed for claude-fable-5-1 (HTTP 401): invalid x-api-key", kind="auth"
        )
        apis = _Apis(
            [_strategist(2), _wrong(), rejected, rejected],
            [_worker("first", lemma=False), _worker("second", lemma=False), _worker("repaired", lemma=False)],
        )
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            code, events = _run_api_max(workspace, apis)
            self.assertEqual(code, 0)
            receipt = _receipt(workspace)
            # After the repair's check is refused, neither the second worker's
            # verification nor the curator calls Anthropic again.
            self.assertEqual(
                [(call["stage"], call["provider"]) for call in receipt["calls"]],
                [
                    ("strategist", "anthropic"),
                    ("worker-1", "openai"),
                    ("worker-2", "openai"),
                    ("verify-1", "anthropic"),
                    ("repair-1", "openai"),
                    ("verify-2", "anthropic"),
                ],
            )
            self.assertIn("verifier-provider-failure", receipt["workflow"]["degraded"])
            self.assertIn("curator-skipped-by-provider-failure", receipt["workflow"]["degraded"])
            self.assertEqual(receipt["verdict"], "unattested")
            self.assertTrue(_receipt_attests(workspace))
            self.assertFalse((workspace / "math_harness" / "project" / "TARGET.md").exists())
            self.assertIn("invalid x-api-key", (workspace / "proof.md").read_text())

    def test_a_provider_change_on_either_side_fails_closed(self) -> None:
        for name, relabel in (
            ("an advisor stage answered by OpenAI", {("anthropic", 2): "openai"}),
            ("a worker stage answered by Anthropic", {("openai", 1): "anthropic"}),
        ):
            with self.subTest(name), tempfile.TemporaryDirectory() as raw:
                workspace = Path(raw)
                apis = _Apis(
                    [_strategist(2), _ok(), _ok(), _curator()],
                    [_worker("first"), _worker("second")],
                    relabel=relabel,
                )
                code, events = _run_api_max(workspace, apis)
                self.assertEqual(code, 1)
                self.assertIn("provider changed during the bounded workflow", json.dumps(events))
                self.assertFalse((workspace / "math_harness" / "project" / "engine_receipt.json").exists())

    def test_per_call_timeouts_reach_an_hour_only_in_max_api(self) -> None:
        for setting, expected in (("5000", 3600), (None, 1800), ("30", 60), ("2400", 2400)):
            with self.subTest(setting=setting), tempfile.TemporaryDirectory() as raw:
                apis = _Apis([_strategist(1), _ok(), _curator()], [_worker("only", lemma=False)])
                code, _events = _run_api_max(
                    Path(raw),
                    apis,
                    {"AGENT_MONITOR_CGM_API_TIMEOUT": setting, "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1"},
                )
                self.assertEqual(code, 0)
                self.assertEqual({call["timeout"] for call in apis.calls}, {expected})
        # Every other API run keeps its 900 s ceiling and its own settings.
        with tempfile.TemporaryDirectory() as raw:
            code, calls, events = _api_run(
                Path(raw), _accepted_outputs(), env={"AGENT_MONITOR_CGM_API_TIMEOUT": "5000"}
            )
            self.assertEqual(code, 0)
            self.assertEqual({call["timeout"] for call in calls}, {900})
            self.assertEqual(calls[-1]["effort"], "low")  # the Kimi curator, as before

    def test_usage_events_name_the_billing_model_only_in_max_api(self) -> None:
        empty = APIBackendError(
            "OpenAI returned an empty response for gpt-6-astra",
            usage={"input_tokens": 100, "output_tokens": 64_000, "reasoning_output_tokens": 64_000},
        )
        apis = _Apis([_strategist(2), _ok(), _ok(), _curator()], [empty, _worker("second")])
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            code, events = _run_api_max(workspace, apis)
            self.assertEqual(code, 0)
            # The failed but billed worker call is booked at the model it asked for.
            first = next(e for e in _usage_events(events) if e["stage"] == "worker-1")
            self.assertEqual((first["model"], first["provider"]), (WORKER, "openai"))
            self.assertEqual(first["usage"]["output_tokens"], 64_000)
            receipt = _receipt(workspace)
            failed = next(call for call in receipt["calls"] if call["stage"] == "worker-1")
            self.assertEqual((failed["provider"], failed["response_sha256"]), ("openai", None))
            self.assertEqual(_attestation(workspace), "harness-gate-receipt")
        # A single-route API run and a subscription Max run are unchanged.
        with tempfile.TemporaryDirectory() as raw:
            code, _calls, events = _api_run(Path(raw), _accepted_outputs())
            self.assertEqual(code, 0)
            self.assertTrue(_usage_events(events))
            self.assertTrue(all(set(e) == {"type", "stage", "usage"} for e in _usage_events(events)))
        with tempfile.TemporaryDirectory() as raw:
            providers = _Providers([_strategist(2), _ok(), _ok(), _curator()], [_worker("first"), _worker("second")])
            code, events = _run_max(Path(raw), providers)
            self.assertEqual(code, 0)
            self.assertTrue(all(set(e) == {"type", "stage", "usage"} for e in _usage_events(events)))

    def test_a_refusal_fallback_is_recorded_under_the_model_that_answered(self) -> None:
        class _FallbackApis(_Apis):
            def __call__(self, *args, **kwargs):
                result = super().__call__(*args, **kwargs)
                if result.provider == "anthropic" and len(self.side("anthropic")) == 2:
                    # The first verify call was declined, and Anthropic's
                    # server-side fallback answered it.
                    result.observed_model = "claude-opus-4-8"
                return result

        apis = _FallbackApis(
            [_strategist(2), _ok(), _ok(), _curator()], [_worker("first"), _worker("second")]
        )
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            code, events = _run_api_max(workspace, apis)
            self.assertEqual(code, 0)
            verify = [call for call in _receipt(workspace)["calls"] if call["stage"].startswith("verify")]
            self.assertEqual([call["model"] for call in verify], ["claude-opus-4-8", ADVISOR])
            usage = {event["stage"]: event["model"] for event in _usage_events(events)}
            self.assertEqual(usage[verify[0]["stage"]], "claude-opus-4-8")
            self.assertEqual(_attestation(workspace), "harness-gate-receipt")

    def test_each_model_a_refusal_fallback_billed_gets_its_own_usage_event(self) -> None:
        shares = (
            (ADVISOR, {"input_tokens": 4, "output_tokens": 2}),
            ("claude-opus-4-8", {"input_tokens": 5, "output_tokens": 9}),
        )

        class _FallbackApis(_Apis):
            def __call__(self, *args, **kwargs):
                result = super().__call__(*args, **kwargs)
                if result.provider == "anthropic" and len(self.side("anthropic")) == 2:
                    result.observed_model = "claude-opus-4-8"
                    result.usage_by_model = shares
                return result

        apis = _FallbackApis([_strategist(2), _ok(), _ok(), _curator()], [_worker("first"), _worker("second")])
        units: list[list[str]] = []
        deliver_all = runner._RunEmitter.deliver_all

        def recording(emitter, objs, *, admit=None):
            units.append([obj["type"] + ":" + str((obj.get("item") or {}).get("type") or obj.get("stage")) for obj in objs])
            return deliver_all(emitter, objs, admit=admit)

        with tempfile.TemporaryDirectory() as raw, patch.object(runner._RunEmitter, "deliver_all", recording):
            code, events = _run_api_max(Path(raw), apis)
        self.assertEqual(code, 0)
        # The reply and every share of its usage go as one unit: a concurrent
        # call's events can never come between them.
        self.assertIn(
            ["item.completed:agent_message", "turn.completed:verify-1", "item.completed:reasoning",
             "turn.completed:verify-1"],
            units,
        )
        verify = [(e["model"], e["usage"]) for e in _usage_events(events) if e["stage"] == "verify-1"]
        # The answering model first (its share lands on the reply's node).
        self.assertEqual(
            verify,
            [("claude-opus-4-8", {"input_tokens": 5, "output_tokens": 9, "cached_input_tokens": 0}),
             (ADVISOR, {"input_tokens": 4, "output_tokens": 2, "cached_input_tokens": 0})],
        )
        notes = [e["item"]["text"] for e in events if (e.get("item") or {}).get("type") == "reasoning"]
        self.assertIn(f"Math Harness · verify-1 · declined first by {ADVISOR}", notes)
        # Every other call is one event, as before.
        self.assertEqual(len([e for e in _usage_events(events) if e["stage"] != "verify-1"]), 5)

    def test_a_refusal_is_unusable_output_not_the_end_of_the_run(self) -> None:
        def refusal() -> APIBackendError:
            return APIBackendError(
                f"Anthropic declined the request for {ADVISOR} (cyber)",
                usage={"input_tokens": 900, "output_tokens": 12},
                refusal=True,
            )

        # The strategist declined: the run goes on with one direct plan.
        apis = _Apis([refusal(), _ok(), _curator()], [_worker("direct", lemma=False)])
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            code, events = _run_api_max(workspace, apis)
            self.assertEqual(code, 0)
            receipt = _receipt(workspace)
            self.assertEqual(receipt["verdict"], "accepted")
            self.assertIn("strategist-fallback-plan", receipt["workflow"]["degraded"])
            # The declined call was billed, and is booked.
            strategist = next(e for e in _usage_events(events) if e["stage"] == "strategist")
            self.assertEqual((strategist["model"], strategist["usage"]["input_tokens"]), (ADVISOR, 900))
        # A verifier declined: an "error" verdict for that candidate only, and
        # the other worker's lemma and theorem are still checked.
        apis = _Apis(
            [_strategist(2), refusal(), _ok(), _ok(), _curator()], [_worker("first"), _worker("second")]
        )
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            code, _events = _run_api_max(workspace, apis)
            self.assertEqual(code, 0)
            receipt = _receipt(workspace)
            self.assertEqual(receipt["verdict"], "accepted")
            self.assertIn("verifier-empty", receipt["workflow"]["degraded"])
            self.assertNotIn("verifier-provider-failure", receipt["workflow"]["degraded"])
            self.assertEqual(
                [call["stage"] for call in receipt["calls"] if call["stage"].startswith("verify")],
                ["verify-1", "verify-2", "verify-3"],
            )
            self.assertEqual(_attestation(workspace), "harness-gate-receipt")
        # Any other provider failure still ends verification, as before.
        apis = _Apis(
            [_strategist(2), APIBackendError("Anthropic model call failed for x (HTTP 500): boom"), _curator()],
            [_worker("first"), _worker("second")],
        )
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            _code, _events = _run_api_max(workspace, apis)
            self.assertIn("verifier-provider-failure", _receipt(workspace)["workflow"]["degraded"])

    def test_mixed_pairs_are_refused_before_any_call(self) -> None:
        anthropic = runner.Route("anthropic", ADVISOR, "api_chat", effort="max", max_tokens=64_000)
        openai = runner.Route("openai", WORKER, "api_chat", effort="max", max_tokens=64_000)
        pairs = {
            "API advisor, Codex worker": (anthropic, runner.Route("codex_subscription", WORKER, "codex_exec")),
            "Claude Code advisor, API worker": (runner.Route("claude_subscription", "fable", "claude_code"), openai),
            "sides swapped": (openai, anthropic),
            "Kimi advisor": (runner.Route("api", "kimi-k3", "api_chat"), openai),
            "one provider twice": (anthropic, runner.Route("anthropic", "claude-opus-5-5", "api_chat")),
        }
        for name, (route, worker_route) in pairs.items():
            with (
                self.subTest(name),
                tempfile.TemporaryDirectory() as raw,
                patch.dict(os.environ, _api_max_env(), clear=True),
                patch.object(runner, "api_chat") as api,
                patch.object(runner, "codex_exec") as codex,
                patch.object(runner, "claude_code_exec") as claude,
            ):
                with self.assertRaisesRegex(runner.MathHarnessError, "Max pairs a Claude Code advisor"):
                    runner.run_pipeline(Path(raw), PROBLEM, route, worker_route=worker_route, emit=lambda _e: None)
                api.assert_not_called()
                codex.assert_not_called()
                claude.assert_not_called()
                self.assertEqual(list(Path(raw).iterdir()), [])


def _ctx(route: runner.Route, worker_route: runner.Route | None, env: dict[str, str] | None = None) -> runner._Ctx:
    return runner._Ctx(
        route=route,
        worker_route=worker_route,
        budgets=runner.Budgets(max_calls=8),
        started=time.monotonic(),
        env=dict(env or {}),
        concurrency=1,
        emitter=runner._RunEmitter(lambda _event: None),
    )


class ApiMaxCallTests(unittest.TestCase):
    def test_each_side_sees_only_its_own_provider(self) -> None:
        env = _api_max_env(
            {
                "OPENAI_API_BASE": "https://openai-alt.invalid/v1",
                "OPENAI_API_KEYS": "sk-a,sk-b",
                "CODEX_API_KEY": "sk-codex",
                "openai_api_key": "sk-lower",
                "ANTHROPIC_AUTH_TOKEN": "ant-token",
                "KIMI_API_KEY": "sk-kimi",
                "OPENROUTER_API_KEY": "sk-or",
                "PATH": "/usr/bin",
            }
        )
        snapshot = dict(env)
        advisor, worker = runner._select_routes(env)
        ctx = _ctx(advisor, worker, env)
        self.assertTrue(ctx.api_max)
        advisor_env, worker_env = ctx.env_for(advisor), ctx.env_for(worker)
        self.assertEqual(
            {name for name in advisor_env if "ANTHROPIC" in name.upper() or "OPENAI" in name.upper()
             or name.upper().endswith(("_API_KEY", "_API_KEYS"))},
            {"ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"},
        )
        self.assertEqual(
            {name for name in worker_env if "ANTHROPIC" in name.upper() or "OPENAI" in name.upper()
             or name.upper().endswith(("_API_KEY", "_API_KEYS"))},
            {"OPENAI_API_KEY", "openai_api_key", "OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_API_KEYS",
             "CODEX_API_KEY"},
        )
        for scoped in (advisor_env, worker_env):
            self.assertEqual(scoped["PATH"], "/usr/bin")
            self.assertEqual(scoped[cgm_max.API_ADVISOR_MODEL_ENV], ADVISOR)
        # The run's snapshot is never modified.
        self.assertEqual(ctx.env, snapshot)
        # A single-route API run keeps every key it was given.
        single = _ctx(runner.Route("api", "kimi-k3", "api_chat"), None, env)
        self.assertFalse(single.api_max)
        self.assertIs(single.env_for(single.route), single.env)

    def test_the_answering_route_decides_the_low_effort_retry(self) -> None:
        kimi = runner.Route("api", "kimi-k3", "api_chat")
        anthropic = runner.Route("anthropic", ADVISOR, "api_chat", effort="max", max_tokens=64_000)
        openai = runner.Route("openai", WORKER, "api_chat", effort="max", max_tokens=64_000)
        cases = (
            # A worker stage on a non-Kimi route is never retried at low
            # effort, even when the advisor route is Kimi's ...
            ("Kimi advisor, OpenAI worker", kimi, openai, 1),
            # ... and on a Kimi worker route it is, whatever the advisor is.
            ("Anthropic advisor, Kimi worker", anthropic, kimi, 2),
        )
        for name, route, worker_route, expected in cases:
            with self.subTest(name):
                replies = ["", "=== reply ==="]
                calls: list[dict] = []

                def fake(system, user, *, model, timeout=240, effort=None, env=None, **kwargs):
                    calls.append({"model": model, "effort": effort, **kwargs})
                    return SimpleNamespace(text=replies.pop(0), usage={}, model=model, provider="test")

                ctx = _ctx(route, worker_route)
                turn = ctx.open_turn("worker-1", 2)
                with patch.object(runner, "api_chat", side_effect=fake):
                    if expected == 1:
                        with self.assertRaises(runner.EmptyReplyError):
                            runner._invoke(ctx, turn, stage="worker-1", system="s", user="u", effort="high")
                    else:
                        reply = runner._invoke(ctx, turn, stage="worker-1", system="s", user="u", effort="high")
                        self.assertEqual(reply.text, "=== reply ===")
                self.assertEqual(len(calls), expected)
                self.assertEqual(calls[0]["model"], worker_route.model)
                if expected == 1:
                    # The OpenAI route's own effort and output ceiling.
                    self.assertEqual((calls[0]["effort"], calls[0]["max_tokens"]), ("max", 64_000))
                else:
                    self.assertEqual([c["effort"] for c in calls], ["high", "low"])
                    self.assertTrue(all("max_tokens" not in c for c in calls))
                    self.assertEqual([r["stage"] for r in ctx.calls], ["worker-1", "worker-1-low"])

    def test_a_declined_call_is_an_empty_reply_that_keeps_its_reason(self) -> None:
        declined = APIBackendError(
            f"Anthropic declined the request for {ADVISOR} (cyber)", usage={"input_tokens": 9}, refusal=True
        )
        route = runner.Route("anthropic", ADVISOR, "api_chat", effort="max", max_tokens=64_000)
        ctx = _ctx(route, None, {"ANTHROPIC_API_KEY": ANTHROPIC_KEY})
        turn = ctx.open_turn("verify-1", 2)
        with patch.object(runner, "api_chat", side_effect=declined), self.assertRaises(runner.EmptyReplyError) as raised:
            runner._invoke(ctx, turn, stage="verify-1", system="s", user="u")
        self.assertIs(raised.exception.__cause__, declined)
        self.assertTrue(runner._is_refusal(raised.exception))
        self.assertIn("Anthropic declined the request", str(raised.exception))
        # One call (no low-effort retry off Kimi), billed and recorded.
        self.assertEqual([(call["stage"], call["usage"]) for call in ctx.calls], [("verify-1", {"input_tokens": 9})])
        # Any other failure is not an empty reply.
        ctx = _ctx(route, None, {"ANTHROPIC_API_KEY": ANTHROPIC_KEY})
        turn = ctx.open_turn("verify-1", 2)
        failed = APIBackendError("Anthropic model call failed for x (HTTP 500): boom")
        with patch.object(runner, "api_chat", side_effect=failed), self.assertRaises(runner.ProviderCallError) as raised:
            runner._invoke(ctx, turn, stage="verify-1", system="s", user="u")
        self.assertNotIsInstance(raised.exception, runner.EmptyReplyError)

    def test_a_failed_billed_call_books_its_usage_on_a_node_of_its_own(self) -> None:
        from agent_monitor.cli_events import CLIEventParser

        empty = APIBackendError(
            "OpenAI returned an empty response for gpt-6-astra",
            usage={"input_tokens": 40_000, "output_tokens": 64_000, "reasoning_output_tokens": 64_000},
        )
        advisor = runner.Route("anthropic", ADVISOR, "api_chat", effort="max", max_tokens=64_000)
        worker = runner.Route("openai", WORKER, "api_chat", effort="max", max_tokens=64_000)
        events: list[dict] = []
        ctx = runner._Ctx(
            route=advisor, worker_route=worker, budgets=runner.Budgets(max_calls=8), started=time.monotonic(),
            env=_api_max_env(), concurrency=2, emitter=runner._RunEmitter(events.append),
        )
        self.assertTrue(ctx.api_max)
        turn = ctx.open_turn("worker-2", 1)

        def failing(*_args, **_kwargs):
            # Another call of the wave closes its node meanwhile.
            events.append({"type": "item.completed", "item": {"type": "agent_message", "text": "worker-1 reply"}})
            raise empty

        with patch.object(runner, "api_chat", side_effect=failing), self.assertRaises(runner.ProviderCallError):
            runner._provider_call(ctx, turn, stage="worker-2", system="s", user="u", effort=None)
        # The bare call marker (progress views skip it) opens the usage's node.
        self.assertEqual(
            [(e["type"], (e.get("item") or {}).get("text", e.get("stage"))) for e in events[-2:]],
            [("item.completed", "Math Harness · worker-2"), ("turn.completed", "worker-2")],
        )
        parser = CLIEventParser("codex", source_engine="math_harness")
        parser.feed("".join(json.dumps(event) + "\n" for event in events))
        [reply] = [turn for turn in parser.turns if turn["detail"].startswith("worker-1 reply")]
        self.assertEqual(reply["output_tokens"], 0)
        billed = parser.turns[-1]
        self.assertEqual(billed["detail"], "Math Harness · worker-2")
        self.assertEqual((billed["model"], billed["output_tokens"]), (WORKER, 64_000))

    def test_a_kimi_model_behind_an_anthropic_or_openai_endpoint_gets_no_kimi_effort(self) -> None:
        keys = {name: "test-key" for name in (
            "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "KIMI_API_KEY", "OPENROUTER_API_KEY")}
        cases = (
            # Those APIs would send it as their own effort: none, as before.
            ("anthropic:kimi-k2-turbo-preview", None),
            ("claude:kimi-k2-thinking", None),
            ("openai:kimi-k2", None),
            # Kimi sends it; OpenRouter checks it and drops it (api_chat).
            ("kimi-k3", "high"),
            ("moonshotai/kimi-k2", "high"),
        )
        for model, expected in cases:
            with self.subTest(model=model):
                sent: list = []

                def fake(system, user, *, model, timeout=240, effort=None, env=None, **kwargs):
                    sent.append(effort)
                    return SimpleNamespace(text="=== reply ===", usage={}, model=model, provider="test")

                ctx = _ctx(runner.Route("api", model, "api_chat"), None, keys)
                turn = ctx.open_turn("worker-1", 2)
                with patch.object(runner, "api_chat", side_effect=fake):
                    runner._invoke(ctx, turn, stage="worker-1", system="s", user="u", effort="high")
                self.assertEqual(sent, [expected])
                self.assertEqual(ctx.calls[0]["effort"], expected)


class ApiMaxReceiptTests(unittest.TestCase):
    _CODEX_SIDE = {
        "families": ["repair", "worker"],
        "provider": "codex_subscription",
        "model": WORKER,
        "transport": "codex_exec",
        "isolation_version": 5,
        "effort": "max",
    }
    _CLAUDE_SIDE = {
        "families": ["curator", "strategist", "verify"],
        "provider": "claude_subscription",
        "model": "fable",
        "transport": "claude_code",
        "isolation_version": 1,
        "effort": "max",
    }

    @classmethod
    def _relabel_calls(cls, receipt: dict, side: str, provider: str) -> None:
        for call in receipt["calls"]:
            worker = cgm_schema.call_family(call["stage"]) in cgm_schema.WORKER_CALL_FAMILIES
            if worker == (side == "worker"):
                call["provider"] = provider

    @classmethod
    def _table_tamperings(cls) -> dict:
        def codex_worker(r):
            r["workflow"]["routes"]["worker"] = dict(cls._CODEX_SIDE)
            cls._relabel_calls(r, "worker", "codex_subscription")

        def claude_code_advisor(r):
            r["workflow"]["routes"]["advisor"] = dict(cls._CLAUDE_SIDE)
            r["provider"] = "claude_subscription"
            cls._relabel_calls(r, "advisor", "claude_subscription")

        def subscription_table(r):
            codex_worker(r)
            claude_code_advisor(r)

        def mixed_under_max(r):
            claude_code_advisor(r)
            r["requested_model"] = cgm_max.MODEL_ID

        def sides_swapped(r):
            routes = r["workflow"]["routes"]
            routes["advisor"], routes["worker"] = routes["worker"], routes["advisor"]

        def providers_swapped(r):
            routes = r["workflow"]["routes"]
            routes["advisor"]["provider"], routes["worker"]["provider"] = "openai", "anthropic"
            r["provider"] = "openai"
            cls._relabel_calls(r, "advisor", "openai")
            cls._relabel_calls(r, "worker", "anthropic")

        def one_provider(r):
            r["workflow"]["routes"]["worker"]["provider"] = "anthropic"
            cls._relabel_calls(r, "worker", "anthropic")

        return {
            "a Codex worker under Max (API)": codex_worker,
            "a Claude Code advisor under Max (API)": claude_code_advisor,
            "the subscription table under Max (API)": subscription_table,
            "the API table under subscription Max": lambda r: r.update(requested_model=cgm_max.MODEL_ID),
            "a mixed table under subscription Max": mixed_under_max,
            "an unknown requested model": lambda r: r.update(requested_model="max-api-2"),
            "sides swapped": sides_swapped,
            "providers swapped": providers_swapped,
            "one provider on both sides": one_provider,
            "Codex isolation on api_chat": lambda r: r["workflow"]["routes"]["worker"].update(isolation_version=5),
            "boolean isolation": lambda r: r["workflow"]["routes"]["advisor"].update(isolation_version=True),
            "unknown effort": lambda r: r["workflow"]["routes"]["worker"].update(effort="ultra"),
            "extra field": lambda r: r["workflow"]["routes"]["worker"].update(note="x"),
            "families swapped": lambda r: r["workflow"]["routes"]["advisor"].update(families=["repair", "worker"]),
            "receipt speaks for the worker": lambda r: r.update(provider="openai"),
            "single-route contract": lambda r: r["workflow"].update(
                response_only_contract={"transport": "api_chat", "isolation_version": 1}
            ),
            "schema 3 with hybrid transport": lambda r: r.update(schema_version=3),
            "missing side": lambda r: r["workflow"]["routes"].pop("worker"),
        }

    @staticmethod
    def _checks(receipt: dict) -> tuple[bool, bool]:
        """(engine_graph attests it, the runner's continuation owns it)."""
        receipt["binding_sha256"] = cgm_schema.receipt_binding_digest(receipt)
        return (
            engine_graph._cgm_receipt(json.dumps(receipt).encode()) is not None,
            runner._valid_contract(receipt),
        )

    def test_tampered_api_max_receipts_are_rejected_by_both_validators(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            _accepted_api_max_run(workspace)
            pristine = _receipt(workspace)
        self.assertEqual(self._checks(copy.deepcopy(pristine)), (True, True))
        for name, tamper in self._table_tamperings().items():
            with self.subTest(name):
                receipt = copy.deepcopy(pristine)
                tamper(receipt)
                self.assertEqual(self._checks(receipt), (False, False))
        # Each call must come from its side, at an effort a receipt may record.
        calls = {
            "a worker call answered by Anthropic": ("worker-1", {"provider": "anthropic"}),
            "an advisor call answered by OpenAI": ("strategist", {"provider": "openai"}),
            "a call effort no receipt records": ("verify-1", {"effort": "xhigh"}),
        }
        for name, (stage, change) in calls.items():
            with self.subTest(name):
                receipt = copy.deepcopy(pristine)
                next(call for call in receipt["calls"] if call["stage"] == stage).update(change)
                self.assertFalse(self._checks(receipt)[0])

    def test_subscription_max_receipts_still_attest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            providers = _Providers([_strategist(2), _ok(), _ok(), _curator()], [_worker("first"), _worker("second")])
            self.assertEqual(_run_max(workspace, providers)[0], 0)
            receipt = _receipt(workspace)
        self.assertEqual(receipt["requested_model"], cgm_max.MODEL_ID)
        self.assertEqual(self._checks(copy.deepcopy(receipt)), (True, True))
        # Its table is never valid under the API model.
        relabelled = copy.deepcopy(receipt)
        relabelled["requested_model"] = cgm_max.API_MODEL_ID
        self.assertEqual(self._checks(relabelled), (False, False))

    def test_tampered_api_max_tables_are_not_owned_by_a_follow_up(self) -> None:
        tamperings = self._table_tamperings()
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            _accepted_api_max_run(workspace)
            path = workspace / "math_harness" / "project" / "engine_receipt.json"
            pristine = json.loads(path.read_text(encoding="utf-8"))
            for name in ("a Codex worker under Max (API)", "the API table under subscription Max", "providers swapped"):
                with self.subTest(name):
                    receipt = copy.deepcopy(pristine)
                    tamperings[name](receipt)
                    receipt["binding_sha256"] = cgm_schema.receipt_binding_digest(receipt)
                    path.write_text(json.dumps(receipt), encoding="utf-8")
                    apis = _Apis([], [])
                    code, events = _run_api_max(workspace, apis)
                    self.assertEqual(code, 1)
                    self.assertIn("not owned by this adapter generation", json.dumps(events))
                    self.assertEqual(apis.calls, [])
            path.write_text(json.dumps(pristine), encoding="utf-8")

    def test_follow_ups_retire_prior_generations_across_modes(self) -> None:
        def api_max(workspace: Path) -> None:
            _accepted_api_max_run(workspace)

        def subscription_max(workspace: Path) -> None:
            providers = _Providers([_strategist(2), _ok(), _ok(), _curator()], [_worker("other"), _worker("more")])
            assert _run_max(workspace, providers)[0] == 0

        def single_route(workspace: Path) -> None:
            assert _api_run(workspace, _accepted_outputs())[0] == 0

        for name, first, second in (
            ("Max (API) after Max (API)", api_max, api_max),
            ("Max (API) after Max", subscription_max, api_max),
            ("Max after Max (API)", api_max, subscription_max),
            ("Max (API) after a single-route run", single_route, api_max),
        ):
            with self.subTest(name), tempfile.TemporaryDirectory() as raw:
                workspace = Path(raw)
                first(workspace)
                old = _facts(workspace)
                self.assertTrue(old)
                if first is api_max and second is api_max:
                    # Same replies, same facts: change the second run's proofs.
                    apis = _Apis(
                        [_strategist(2), _ok(), _ok(), _curator()], [_worker("renewed"), _worker("again")]
                    )
                    self.assertEqual(_run_api_max(workspace, apis)[0], 0)
                else:
                    second(workspace)
                new = _facts(workspace)
                self.assertTrue(new)
                self.assertTrue(all(not path.exists() for path in old - new))
                receipt = _receipt(workspace)
                self.assertEqual(
                    sorted(f"{item['fact_id']}.md" for item in receipt["facts"]),
                    sorted(path.name for path in new),
                )
                self.assertEqual(_attestation(workspace), "harness-gate-receipt")


class ApiMaxWriteupTests(unittest.TestCase):
    def test_openai_writes_and_anthropic_checks(self) -> None:
        apis = _Apis(
            [_strategist(2), _ok(), _ok(), _curator(), _ok()],
            [_worker("first"), _worker("second"), WRITEUP],
        )
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            code, events = _run_api_max(workspace, apis, {"AGENT_MONITOR_CGM_WRITEUP": "1"})
            self.assertEqual(code, 0)
            writer, checker = apis.side("openai")[-1], apis.side("anthropic")[-1]
            self.assertEqual(writer["system"], cgm_writeup.WRITEUP_SYSTEM)
            self.assertEqual((writer["model"], writer["key"]), (WORKER, OPENAI_KEY))
            self.assertNotIn("ANTHROPIC_API_KEY", writer["env"])
            self.assertEqual(checker["system"], cgm_prompts.VERIFIER_SYSTEM)
            self.assertTrue(checker["user"].startswith("Verify the candidate fact labelled THEOREM."))
            self.assertEqual((checker["model"], checker["key"]), (ADVISOR, ANTHROPIC_KEY))
            self.assertNotIn("OPENAI_API_KEY", checker["env"])
            for call in (writer, checker):
                self.assertEqual((call["effort"], call["max_tokens"], call["timeout"]), ("max", 64_000, 1800))
            sidecar = json.loads((workspace / "math_harness" / "project" / "writeup.json").read_text())
            self.assertEqual(sidecar["status"], "accepted")
            self.assertEqual(
                sidecar["routes"],
                {
                    "writer": {"provider": "openai", "model": WORKER, "transport": "api_chat"},
                    "checker": {"provider": "anthropic", "model": ADVISOR, "transport": "api_chat"},
                },
            )
            self.assertEqual(
                [(call["stage"], call["provider"], call["effort"]) for call in sidecar["calls"]],
                [("writeup", "openai", "max"), ("writecheck", "anthropic", "max")],
            )
            self.assertEqual(_receipt(workspace)["workflow"]["calls"]["total"], 6)
            self.assertEqual(_attestation(workspace), "harness-gate-receipt")
            usage = {e["stage"]: (e["model"], e["provider"]) for e in _usage_events(events)}
            self.assertEqual(usage["writeup"], (WORKER, "openai"))
            self.assertEqual(usage["writecheck"], (ADVISOR, "anthropic"))

    def test_the_writeup_round_gets_an_hour_as_on_max(self) -> None:
        mode = {"AGENT_MONITOR_CGM_ROUTE_MODE": cgm_max.API_ROUTE_MODE}
        budgets, fixes = runner._writeup_limits(mode)
        self.assertEqual((budgets.max_calls, budgets.max_seconds, fixes), (6, 3600, 1))
        self.assertEqual(runner.writeup_allowance_seconds({**mode, "AGENT_MONITOR_CGM_WRITEUP": "1"}), 3660)
        self.assertEqual(runner._writeup_limits({})[0].max_seconds, 1800)
        self.assertEqual(runner._writeup_limits({"AGENT_MONITOR_CGM_ROUTE_MODE": "max"})[0].max_seconds, 3600)


class ApiMaxManuscriptTests(unittest.TestCase):
    def test_analyze_and_readability_refuse_max_api(self) -> None:
        request = {"role": "admin", "route": "api_key", "model": "kimi-k3", "sponsored": False}
        for rules, mode in ((analyze_runner.ANALYZE_ROUTES, "Analyze"), (readability_runner.ROUTES, "Readability")):
            with self.subTest(mode):
                env = _api_max_env({"KIMI_API_KEY": "sk-kimi"})
                with self.assertRaisesRegex(analyze_runner.ConfigError, rf"Max \(API\) is not available for {mode}"):
                    analyze_runner.configure_route(env, request, rules)
                # Max keeps its own refusal.
                env[cgm_max.ROUTE_MODE_ENV] = "max"
                with self.assertRaisesRegex(analyze_runner.ConfigError, rf"^Max is not available for {mode}"):
                    analyze_runner.configure_route(env, request, rules)
