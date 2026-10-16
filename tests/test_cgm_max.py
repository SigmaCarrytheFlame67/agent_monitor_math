"""Ansatz CGM "Max": Claude Code leads (strategist, verifier, curator), Codex works."""
from __future__ import annotations

import base64
import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_monitor import cgm_max, engine_graph
from agent_monitor.cgm import schema as cgm_schema
from agent_monitor.runners import math_harness_runner as runner

from tests.test_math_harness_runner import (
    PROBLEM,
    _curator,
    _in_directory,
    _ok,
    _receipt,
    _strategist,
    _worker,
    _wrong,
)

MAX_ENV = {
    "AGENT_MONITOR_CGM_ROUTE_MODE": "max",
    "AGENT_MONITOR_CODEX_SUBSCRIPTION": "1",
    "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
    "AGENT_MONITOR_CODEX_MODEL": "gpt-6-astra",
    "AGENT_MONITOR_CLAUDE_MODEL": "fable",
    "AGENT_MONITOR_RUN_REASONING_EFFORT": "max",
    "AGENT_MONITOR_CLAUDE_EFFORT": "max",
    "CODEX_HOME": "/accounts/codex/user_1",
    "CLAUDE_CONFIG_DIR": "/accounts/claude/user_1",
    "HOME": "/accounts/claude/user_1",
    "AGENT_MONITOR_CODEX_CALL_HOME": "/srv/console-home",
    "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "2",
    "AGENT_MONITOR_MAX_ITERATIONS": "20",
    "AGENT_MONITOR_PROBLEM_ID": "max-case",
}


class _Providers:
    """Scripted Claude (advisor) and Codex (worker) backends."""

    def __init__(self, claude_replies: list[str], codex_replies: list[str]):
        self.claude_replies = list(claude_replies)
        self.codex_replies = list(codex_replies)
        self.claude_calls: list[dict] = []
        self.codex_calls: list[dict] = []

    def claude(self, system: str, user: str, **kwargs):
        self.claude_calls.append({"system": system, "user": user, **kwargs})
        if not self.claude_replies:
            raise AssertionError("unexpected Claude call")
        text = self.claude_replies.pop(0)
        return SimpleNamespace(
            text=text,
            usage={"input_tokens": 5, "output_tokens": 9, "cache_read_tokens": 2},
            model="claude-fable-5-1",
            events=[{"type": "assistant", "message": {"model": "claude-fable-5-1"}}],
        )

    def codex(self, system: str, user: str, **kwargs):
        self.codex_calls.append({"system": system, "user": user, **kwargs})
        if not self.codex_replies:
            raise AssertionError("unexpected Codex call")
        return SimpleNamespace(
            text=self.codex_replies.pop(0),
            usage={"input_tokens": 7, "output_tokens": 30, "reasoning_output_tokens": 12},
        )


def _run_max(workspace: Path, providers: _Providers, env: dict[str, str] | None = None):
    events: list[dict] = []
    run_env = {**MAX_ENV, **(env or {})}
    with (
        _in_directory(workspace),
        patch.dict(runner.os.environ, run_env, clear=True),
        patch.object(runner.sys, "argv", ["runner", PROBLEM]),
        patch.object(runner, "codex_subscription_enabled", side_effect=lambda env: env.get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1"),
        patch.object(runner, "claude_subscription_enabled", side_effect=lambda env: env.get("AGENT_MONITOR_CLAUDE_SUBSCRIPTION") == "1"),
        patch.object(runner, "codex_exec", side_effect=providers.codex),
        patch.object(runner, "claude_code_exec", side_effect=providers.claude),
        patch.object(runner, "api_chat") as api,
        patch.object(runner, "emit", side_effect=events.append),
    ):
        code = runner.main()
    api.assert_not_called()
    return code, events


class MaxRunnerTests(unittest.TestCase):
    def test_roles_split_between_claude_and_codex_with_their_own_accounts(self) -> None:
        providers = _Providers(
            claude_replies=[_strategist(2), _ok(), _ok(), _curator()],
            codex_replies=[_worker("first"), _worker("second")],
        )
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            code, events = _run_max(workspace, providers)
            self.assertEqual(code, 0)
            # Claude answered the strategist, both verifications and the curator.
            self.assertEqual(len(providers.claude_calls), 4)
            self.assertEqual(len(providers.codex_calls), 2)
            for call in providers.claude_calls:
                self.assertEqual(call["model"], "fable")
                self.assertIs(call["enable_tools"], False)
                self.assertEqual(call["max_turns"], 1)
                env = call["source_env"]
                self.assertEqual(env["AGENT_MONITOR_CLAUDE_EFFORT"], "max")
                self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/accounts/claude/user_1")
                self.assertEqual(env["HOME"], "/accounts/claude/user_1")
                self.assertNotIn("CODEX_HOME", env)
                self.assertNotIn("AGENT_MONITOR_CODEX_SUBSCRIPTION", env)
            for call in providers.codex_calls:
                self.assertEqual(call["model"], "gpt-6-astra")
                self.assertEqual(call["sandbox"], "read-only")
                self.assertIs(call["enable_tools"], False)
                env = call["source_env"]
                self.assertEqual(env["AGENT_MONITOR_RUN_REASONING_EFFORT"], "max")
                self.assertEqual(env["CODEX_HOME"], "/accounts/codex/user_1")
                self.assertEqual(env["HOME"], "/srv/console-home")
                self.assertNotIn("CLAUDE_CONFIG_DIR", env)
                self.assertNotIn("AGENT_MONITOR_CLAUDE_SUBSCRIPTION", env)
                self.assertNotIn("AGENT_MONITOR_CODEX_CALL_HOME", env)
            # Workers see the strategist's plans; verifiers see the workers' proofs.
            self.assertIn("Route 1", providers.codex_calls[0]["user"])
            self.assertIn("first", providers.claude_calls[1]["user"] + providers.claude_calls[2]["user"])

            receipt = _receipt(workspace)
            self.assertEqual(receipt["schema_version"], cgm_schema.HYBRID_RECEIPT_SCHEMA_VERSION)
            self.assertEqual(receipt["verdict"], "accepted")
            self.assertEqual(receipt["transport"], "hybrid")
            self.assertEqual(receipt["provider"], "claude_subscription")
            self.assertEqual(receipt["requested_model"], "max")
            self.assertEqual(
                receipt["workflow"]["routes"],
                {
                    "advisor": {
                        "families": ["curator", "strategist", "verify"],
                        "provider": "claude_subscription",
                        "model": "fable",
                        "transport": "claude_code",
                        "isolation_version": 1,
                        "effort": "max",
                    },
                    "worker": {
                        "families": ["repair", "worker"],
                        "provider": "codex_subscription",
                        "model": "gpt-6-astra",
                        "transport": "codex_exec",
                        "isolation_version": 5,
                        "effort": "max",
                    },
                },
            )
            by_stage = {call["stage"]: call["provider"] for call in receipt["calls"]}
            self.assertEqual(by_stage["strategist"], "claude_subscription")
            self.assertEqual(by_stage["worker-1"], "codex_subscription")
            self.assertEqual(by_stage["worker-2"], "codex_subscription")
            self.assertEqual(by_stage["curator"], "claude_subscription")
            self.assertTrue(all(by_stage[s] == "claude_subscription" for s in by_stage if s.startswith("verify-")))

            # The published graph attests the run and names the hybrid contract.
            graph = engine_graph.build(workspace, {"engine": "math_harness"})
            provenance = graph["provenance"]
            self.assertEqual(provenance["receipt_status"], "verifier-correct")
            self.assertEqual(provenance["response_only_transport"], "hybrid")
            self.assertEqual(provenance["verifier_model"], "claude-fable-5-1")
            banner = next(e for e in events if (e.get("item") or {}).get("type") == "agent_message")
            self.assertIn("Max: Claude Code fable plans, verifies and curates; Codex gpt-6-astra writes and repairs", banner["item"]["text"])
            usage = [e for e in events if e.get("type") == "turn.completed"]
            self.assertTrue(all(e["usage"].get("input_excludes_cache") for e in usage if e["stage"] in {"strategist", "curator"}))
            self.assertTrue(all(not e["usage"].get("input_excludes_cache") for e in usage if e["stage"].startswith("worker")))

    def test_repairs_run_on_codex_and_their_checks_on_claude(self) -> None:
        providers = _Providers(
            claude_replies=[_strategist(1), _wrong(), _ok(), _curator()],
            codex_replies=[_worker("only", lemma=False), _worker("repaired", lemma=False)],
        )
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            code, _events = _run_max(workspace, providers, {"AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1"})
            self.assertEqual(code, 0)
            receipt = _receipt(workspace)
            stages = [(call["stage"], call["provider"]) for call in receipt["calls"]]
            self.assertIn(("repair-1", "codex_subscription"), stages)
            self.assertEqual(receipt["verdict"], "accepted")
            self.assertEqual(len(providers.codex_calls), 2)

    def test_max_needs_both_account_routes(self) -> None:
        providers = _Providers([], [])
        with tempfile.TemporaryDirectory() as raw:
            code, events = _run_max(Path(raw), providers, {"AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "0"})
        self.assertEqual(code, 1)
        self.assertIn("Max needs both the Claude Code and the Codex account routes", json.dumps(events))
        self.assertEqual(providers.claude_calls + providers.codex_calls, [])

    def test_single_route_runs_still_refuse_two_active_accounts(self) -> None:
        env = {k: v for k, v in MAX_ENV.items() if k != "AGENT_MONITOR_CGM_ROUTE_MODE"}
        with (
            patch.object(runner, "codex_subscription_enabled", return_value=True),
            patch.object(runner, "claude_subscription_enabled", return_value=True),
        ):
            with self.assertRaisesRegex(runner.MathHarnessError, "cannot both be active"):
                runner._select_routes(env)

    def test_tampered_max_receipts_are_rejected(self) -> None:
        providers = _Providers(
            claude_replies=[_strategist(2), _ok(), _ok(), _curator()],
            codex_replies=[_worker("first"), _worker("second")],
        )
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            self.assertEqual(_run_max(workspace, providers)[0], 0)
            path = workspace / "math_harness" / "project" / "engine_receipt.json"
            original = json.loads(path.read_text())

            def valid(receipt: dict) -> bool:
                receipt["binding_sha256"] = cgm_schema.receipt_binding_digest(receipt)
                return engine_graph._cgm_receipt(json.dumps(receipt).encode()) is not None

            self.assertTrue(valid(copy.deepcopy(original)))
            swapped = copy.deepcopy(original)  # a worker call claiming the Claude account
            next(c for c in swapped["calls"] if c["stage"] == "worker-1")["provider"] = "claude_subscription"
            self.assertFalse(valid(swapped))
            sides = copy.deepcopy(original)  # the sides exchanged
            routes = sides["workflow"]["routes"]
            routes["advisor"], routes["worker"] = routes["worker"], routes["advisor"]
            self.assertFalse(valid(sides))
            v3 = copy.deepcopy(original)  # a hybrid transport on schema 3
            v3["schema_version"] = 3
            del v3["workflow"]["routes"]
            self.assertFalse(valid(v3))
            no_routes = copy.deepcopy(original)
            del no_routes["workflow"]["routes"]
            self.assertFalse(valid(no_routes))


class MaxPlanTests(unittest.TestCase):
    def test_formal_view_runs_derived_work_on_the_worker_route(self) -> None:
        record = {
            "run_id": "math_harness_x_1",
            "auth_route": "dual_subscription",
            "model": "max",
            "requested_model": "max",
            "reasoning_effort": "max",
            "speed_mode": "standard",
            "live": {"auth_mode": "dual_subscription", "model": "max"},
            "route_plan": {"advisor": {"model": "fable"}, "worker": {"model": "gpt-6-astra"}},
        }
        view = cgm_max.formal_view(record)
        self.assertEqual(view["auth_route"], "codex_subscription")
        self.assertEqual(view["model"], "gpt-6-astra")
        self.assertEqual(view["reasoning_effort"], "high")
        self.assertEqual(view["live"]["auth_mode"], "chatgpt_subscription")
        self.assertEqual(record["auth_route"], "dual_subscription")  # unchanged
        plain = {"auth_route": "codex_subscription", "model": "gpt-6.1-sol"}
        self.assertIs(cgm_max.formal_view(plain), plain)
        self.assertEqual(cgm_max.formal_model(record, "max"), "gpt-6-astra")
        self.assertEqual(cgm_max.formal_model(plain, "gpt-6.1-sol"), "gpt-6.1-sol")
        # Unknown stored models fall back to the defaults rather than passing through.
        odd = dict(record, route_plan={"worker": {"model": "rm -rf"}})
        self.assertEqual(cgm_max.record_plan(odd)["worker"]["model"], "gpt-6-astra")


if __name__ == "__main__":
    unittest.main()



class MaxContinuationTests(unittest.TestCase):
    """A follow-up message continues a Max run in the same workspace."""

    @staticmethod
    def _first_run(workspace: Path) -> None:
        providers = _Providers(
            claude_replies=[_strategist(2), _ok(), _ok(), _curator()],
            codex_replies=[_worker("first"), _worker("second")],
        )
        code, _events = _run_max(workspace, providers)
        assert code == 0

    def test_follow_up_replaces_the_prior_max_generation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            self._first_run(workspace)
            facts = workspace / "math_harness" / "project" / "fact_graph" / "facts"
            old = set(facts.glob("*.md"))
            self.assertEqual(len(old), 2)
            providers = _Providers(
                claude_replies=[_strategist(2), _ok(), _ok(), _curator()],
                codex_replies=[_worker("renewed"), _worker("other")],
            )
            code, _events = _run_max(workspace, providers)
            self.assertEqual(code, 0)
            self.assertEqual((len(providers.claude_calls), len(providers.codex_calls)), (4, 2))
            new = set(facts.glob("*.md"))
            self.assertEqual(len(new), 2)
            self.assertTrue(all(not path.exists() for path in old))
            receipt = _receipt(workspace)
            self.assertEqual(receipt["schema_version"], cgm_schema.HYBRID_RECEIPT_SCHEMA_VERSION)
            self.assertEqual(
                sorted(f"{item['fact_id']}.md" for item in receipt["facts"]),
                sorted(path.name for path in new),
            )
            graph = engine_graph.build(workspace, {"engine": "math_harness"})
            self.assertEqual(graph["provenance"]["attestation"], "harness-gate-receipt")

    def test_the_max_generation_owns_its_facts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            self._first_run(workspace)
            owned = runner._prior_generated_fact_ids(workspace)
            facts = workspace / "math_harness" / "project" / "fact_graph" / "facts"
            self.assertEqual(sorted(owned), sorted(path.stem for path in facts.glob("*.md")))
            self.assertEqual(len(owned), 2)

    def test_tampered_max_route_tables_are_not_owned(self) -> None:
        tamperings = {
            "worker on the Claude account": lambda r: r["workflow"]["routes"]["worker"].update(provider="claude_subscription"),
            "families swapped": lambda r: r["workflow"]["routes"]["advisor"].update(families=["repair", "worker"]),
            "wrong isolation": lambda r: r["workflow"]["routes"]["worker"].update(isolation_version=4),
            "boolean isolation": lambda r: r["workflow"]["routes"]["advisor"].update(isolation_version=True),
            "unknown effort": lambda r: r["workflow"]["routes"]["worker"].update(effort="ultra"),
            "extra field": lambda r: r["workflow"]["routes"]["worker"].update(note="x"),
            "missing side": lambda r: r["workflow"]["routes"].pop("worker"),
            "single-route contract": lambda r: r["workflow"].update(
                response_only_contract={"transport": "claude_code", "isolation_version": 1}
            ),
            "schema 3 with hybrid transport": lambda r: r.update(schema_version=3),
            "provider is the worker": lambda r: r.update(provider="codex_subscription"),
            "tools enabled": lambda r: r["workflow"].update(tools_enabled=True),
        }
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            self._first_run(workspace)
            receipt_path = workspace / "math_harness" / "project" / "engine_receipt.json"
            pristine = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertTrue(runner._valid_contract(pristine))
            for name, tamper in tamperings.items():
                with self.subTest(name):
                    receipt = copy.deepcopy(pristine)
                    tamper(receipt)
                    receipt["binding_sha256"] = cgm_schema.receipt_binding_digest(receipt)
                    self.assertFalse(runner._valid_contract(receipt))
                    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
                    providers = _Providers(claude_replies=[], codex_replies=[])
                    code, _events = _run_max(workspace, providers)
                    self.assertEqual(code, 1)
                    self.assertEqual((providers.claude_calls, providers.codex_calls), ([], []))
            receipt_path.write_text(json.dumps(pristine), encoding="utf-8")


# ── jobs: starting, recording and continuing a Max run ─────────────────────

from agent_monitor import jobs, run_tuning, usage_ledger  # noqa: E402

_HARNESS = {
    "id": "math_harness",
    "label": "Ansatz CGM harness",
    "available": True,
    "auth_modes": ["codex_subscription", "claude_subscription", "api_key"],
}
_ADMIN = {"id": 17, "email": "owner@example.com", "is_admin": True}


class _FakeThread:
    captured: dict = {}

    def __init__(self, *, kwargs=None, **_unused):
        type(self).captured = dict(kwargs or {})

    def start(self) -> None:
        pass

    def is_alive(self) -> bool:  # start_job launches derived work only for real threads
        return False


def _accounts(tmp: Path, *, codex: bool = True, claude: bool = True, codex_models=("gpt-6.1-sol", "gpt-6-astra")):
    codex_home, claude_home = tmp / "codex-17", tmp / "claude-17"
    codex_home.mkdir(exist_ok=True)
    claude_home.mkdir(exist_ok=True)
    return (
        patch("agent_monitor.codex_login.account_home", return_value=codex_home),
        patch("agent_monitor.codex_login.account_login_ready", return_value=codex),
        patch("agent_monitor.codex_login.available_models", return_value=list(codex_models)),
        patch("agent_monitor.claude_login.account_home", return_value=claude_home),
        patch("agent_monitor.claude_login.account_login_ready", return_value=claude),
        patch(
            "agent_monitor.claude_login.available_models",
            return_value=["sonnet", "opus", "fable", "claude-haiku-4-5"] if claude else [],
        ),
    ), codex_home, claude_home


class MaxJobTests(unittest.TestCase):
    def setUp(self) -> None:
        # Max binds only an administrator's accounts; _ADMIN has no database row.
        operator = patch("agent_monitor.auth.is_operator_account", return_value=True)
        operator.start()
        self.addCleanup(operator.stop)

    def _start(self, tmp: Path, *, user=_ADMIN, engine="kimi", model="max", accounts=None, **kwargs):
        written: list[dict] = []
        workspace = tmp / "workspace"
        workspace.mkdir(exist_ok=True)
        account_patches, codex_home, claude_home = accounts or _accounts(tmp)
        from contextlib import ExitStack

        with ExitStack() as stack:
            for item in (
                patch("agent_monitor.engines_registry.all_engine_ids", return_value={"kimi", "math_harness", "plain"}),
                patch("agent_monitor.engines_registry.list_engines", return_value=[dict(_HARNESS)]),
                patch.object(jobs, "_user_extra_env", return_value={"HOME": "/srv/home", "OPENAI_API_KEY": "sk-should-vanish", "ANTHROPIC_API_KEY": "ak-should-vanish"}),
                patch.object(jobs, "workspace_dir", return_value=workspace),
                patch.object(jobs, "_write_run", side_effect=lambda run: written.append(dict(run))),
                patch("agent_monitor.jobs.threading.Thread", _FakeThread),
                *account_patches,
            ):
                stack.enter_context(item)
            pipeline = stack.enter_context(patch("agent_monitor.auto_pipeline.start"))
            stack.enter_context(patch.dict("os.environ", {"AGENT_MONITOR_DISABLE_AUTO_PIPELINE": ""}))
            job = jobs.start_job(
                engine=engine,
                problem_text="Prove that there are infinitely many primes.",
                model=model,
                user=user,
                **kwargs,
            )
        return job, written, _FakeThread.captured, pipeline, codex_home, claude_home

    def test_max_starts_on_both_accounts_with_the_strongest_models_at_max_effort(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            job, written, captured, pipeline, codex_home, claude_home = self._start(
                Path(raw), auth_route="api_key", reasoning_effort="low", speed_mode="fast"
            )
        # The launcher's runtime and run mode never weaken Max.
        self.assertEqual(job["engine"], "math_harness")
        record = written[0]
        self.assertEqual(record["model"], "max")
        self.assertEqual(record["auth_route"], "dual_subscription")
        self.assertEqual(record["live"]["auth_mode"], "dual_subscription")
        self.assertEqual((record["reasoning_effort"], record["speed_mode"]), ("max", "standard"))
        self.assertEqual(record["route_plan"]["advisor"]["model"], "fable")
        self.assertEqual(record["route_plan"]["worker"]["model"], "gpt-6-astra")
        env = captured["extra_env"]
        self.assertEqual(env["AGENT_MONITOR_CGM_ROUTE_MODE"], "max")
        self.assertEqual(env["AGENT_MONITOR_CLAUDE_MODEL"], "fable")
        self.assertEqual(env["AGENT_MONITOR_CODEX_MODEL"], "gpt-6-astra")
        self.assertEqual(env["AGENT_MONITOR_CLAUDE_EFFORT"], "max")
        self.assertEqual(env["AGENT_MONITOR_RUN_REASONING_EFFORT"], "max")
        self.assertEqual(env["CODEX_HOME"], str(codex_home))
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(claude_home))
        self.assertEqual(env["HOME"], str(claude_home))
        self.assertEqual(env["AGENT_MONITOR_CODEX_CALL_HOME"], "/srv/home")
        self.assertEqual(env["AGENT_MONITOR_CODEX_SUBSCRIPTION"], "1")
        self.assertEqual(env["AGENT_MONITOR_CLAUDE_SUBSCRIPTION"], "1")
        self.assertEqual(env["AGENT_MONITOR_CODEX_TIMEOUT"], "1800")
        self.assertEqual(env["AGENT_MONITOR_CGM_MAX_SECONDS"], "10800")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        # The outer watchdog leaves room for the 3-hour pipeline.
        self.assertGreaterEqual(jobs._cli_timeout_seconds("math_harness", env), 10_800 + 300)
        # Lean and graph work after the run use the worker model.
        self.assertEqual(pipeline.call_args.kwargs["model"], "gpt-6-astra")
        # The model env projection keeps both models.
        projected = dict(env)
        jobs._apply_selected_model_env(projected, engine="math_harness", model="max")
        self.assertEqual((projected["AGENT_MONITOR_CLAUDE_MODEL"], projected["AGENT_MONITOR_CODEX_MODEL"]), ("fable", "gpt-6-astra"))

    def test_operator_cli_timeouts_never_cut_a_max_run_short(self) -> None:
        from agent_monitor.subprocess_env import child_process_env

        with tempfile.TemporaryDirectory() as raw:
            _job, _written, captured, *_ = self._start(Path(raw))
        extra = captured["extra_env"]

        def watchdog(operator: dict[str, str], extra_env: dict[str, str] = extra) -> int:
            # The runner thread copies the service environment, then the run's.
            with patch.dict("os.environ", operator):
                return jobs._cli_timeout_seconds("math_harness", child_process_env(extra=extra_env))

        # A Max run stopped by the watchdog publishes nothing, so the generic
        # limit (.env.example) and the engine's own may only raise it.
        for operator in (
            {"AGENT_MONITOR_CLI_TIMEOUT": "3600"},
            {"AGENT_MONITOR_CLI_TIMEOUT": "7200"},
            {"AGENT_MONITOR_MATH_HARNESS_TIMEOUT": "3600"},
        ):
            self.assertGreaterEqual(watchdog(operator), cgm_max.RUN_SECONDS + 300, operator)
        self.assertEqual(watchdog({"AGENT_MONITOR_CLI_TIMEOUT": "20000"}), 20_000)
        # A single Claude-subscription CGM run keeps the operator's lower limit.
        claude_only = {key: value for key, value in extra.items() if key != cgm_max.ROUTE_MODE_ENV}
        self.assertEqual(watchdog({"AGENT_MONITOR_CLI_TIMEOUT": "3600"}, claude_only), 3_600)

    def test_worker_falls_back_to_the_best_codex_model_the_account_offers(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            _job, written, captured, *_ = self._start(tmp, accounts=_accounts(tmp, codex_models=("gpt-6-luna", "gpt-6.1-sol")))
        self.assertEqual(written[0]["route_plan"]["worker"]["model"], "gpt-6.1-sol")
        self.assertEqual(captured["extra_env"]["AGENT_MONITOR_CODEX_MODEL"], "gpt-6.1-sol")

    def test_max_is_refused_without_both_accounts_or_for_other_engines_and_users(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            with self.assertRaisesRegex(ValueError, "connect Codex in Settings"):
                self._start(tmp, accounts=_accounts(tmp, codex=False))
            with self.assertRaisesRegex(ValueError, "connect Claude Code and Codex in Settings"):
                self._start(tmp, accounts=_accounts(tmp, codex=False, claude=False))
            with self.assertRaisesRegex(ValueError, "only for Ansatz CGM"):
                self._start(tmp, engine="plain")
            member = {"id": 18, "email": "student@ucla.edu"}
            with self.assertRaisesRegex(ValueError, "limited to the site administrator"):
                self._start(tmp, user=member)
            guest = {"id": 19, "email": "guest+x@public.local", "guest": True}
            with self.assertRaisesRegex(ValueError, "site administrator's linked Claude Code and Codex"):
                self._start(tmp, user=guest)
            with self.assertRaisesRegex(ValueError, "only for Ansatz CGM"):
                self._start(tmp, engine="plain", model="gpt-6-astra", auth_route="dual_subscription")

    def test_continuation_keeps_max_on_both_accounts(self) -> None:
        record = {
            "run_id": "math_harness_demo_1",
            "engine": "math_harness",
            "owner_id": 17,
            "auth_route": "dual_subscription",
            "model": "max",
            "requested_model": "max",
            "reasoning_effort": "max",
            "speed_mode": "standard",
            "live": {"auth_mode": "dual_subscription", "model": "max"},
            "route_plan": {"advisor": {"model": "fable"}, "worker": {"model": "gpt-6-astra"}},
        }
        self.assertEqual(jobs._continuation_model(record, "gpt-6.1-sol"), "max")
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            account_patches, codex_home, claude_home = _accounts(tmp)
            with (
                patch("agent_monitor.engines_registry.list_engines", return_value=[dict(_HARNESS)]),
                account_patches[0], account_patches[1], account_patches[2],
                account_patches[3], account_patches[4], account_patches[5],
            ):
                configured, route = jobs._continuation_auth_environment(
                    run_data=record,
                    engine="math_harness",
                    model="max",
                    user=_ADMIN,
                    env={"HOME": "/srv/home", "AGENT_MONITOR_ACCOUNT_RUNTIME": "api", "OPENAI_API_KEY": "sk-x"},
                )
        self.assertEqual(route, "dual_subscription")
        self.assertEqual(configured["AGENT_MONITOR_CGM_ROUTE_MODE"], "max")
        self.assertEqual(configured["AGENT_MONITOR_CLAUDE_MODEL"], "fable")
        self.assertEqual(configured["AGENT_MONITOR_CODEX_MODEL"], "gpt-6-astra")
        self.assertEqual(configured["AGENT_MONITOR_CLAUDE_EFFORT"], "max")
        self.assertEqual(configured["AGENT_MONITOR_RUN_REASONING_EFFORT"], "max")
        self.assertEqual(configured["CODEX_HOME"], str(codex_home))
        self.assertEqual(configured["CLAUDE_CONFIG_DIR"], str(claude_home))
        self.assertNotIn("OPENAI_API_KEY", configured)
        # A disconnected account stops the continuation instead of switching route.
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            account_patches, *_ = _accounts(tmp, claude=False)
            with (
                patch("agent_monitor.engines_registry.list_engines", return_value=[dict(_HARNESS)]),
                account_patches[0], account_patches[1], account_patches[2],
                account_patches[3], account_patches[4], account_patches[5],
            ):
                with self.assertRaisesRegex(ValueError, "Claude Code is not connected"):
                    jobs._continuation_auth_environment(
                        run_data=record, engine="math_harness", model="max", user=_ADMIN, env={}
                    )

    def _follow_up(self, tmp: Path, run_id: str, message: str, *, claude=True, codex=True):
        account_patches, *_ = _accounts(tmp, claude=claude, codex=codex)
        upload = {"name": "notes.md", "data": base64.b64encode(b"Try induction on n.").decode("ascii")}
        with ExitStack() as stack:
            for item in (
                patch("agent_monitor.engines_registry.list_engines", return_value=[dict(_HARNESS)]),
                patch.object(jobs, "_user_extra_env", return_value={"HOME": "/srv/home"}),
                patch("agent_monitor.jobs.threading.Thread", _FakeThread),
                *account_patches,
            ):
                stack.enter_context(item)
            return jobs.send_human_message(run_id, message, user=_ADMIN, attachments=[upload])

    def test_a_follow_up_refused_on_its_route_is_not_recorded(self) -> None:
        max_record = {
            "run_id": "math_harness_max_follow_up",
            "engine": "math_harness",
            "status": "finished",
            "owner_id": 17,
            "model": "max",
            "auth_route": "dual_subscription",
            "live": {"auth_mode": "dual_subscription", "model": "max"},
            "reasoning_effort": "max",
            "speed_mode": "standard",
            "route_plan": {"advisor": {"model": "fable"}, "worker": {"model": "gpt-6-astra"}},
            "problem_text": "Prove 1+1=2",
            "problems": [{"text": "Prove 1+1=2", "source": "initial"}],
        }
        codex_record = {
            **{key: value for key, value in max_record.items() if key != "route_plan"},
            "run_id": "math_harness_codex_follow_up",
            "model": "gpt-6-astra",
            "auth_route": "codex_subscription",
            "live": {"auth_mode": "chatgpt_subscription", "model": "gpt-6-astra"},
            "reasoning_effort": "high",
        }
        with tempfile.TemporaryDirectory() as raw, ExitStack() as stack:
            tmp = Path(raw)
            stack.enter_context(patch.object(jobs, "RUNS_DIR", tmp / "runs"))
            stack.enter_context(patch.object(jobs, "CACHE_DIR", tmp / "cache"))
            stack.enter_context(patch.object(jobs, "_JOBS", {}))
            stack.enter_context(patch.object(jobs, "_STOP_EVENTS", {}))
            for record, refusal, disconnected in (
                (max_record, "Claude Code is not connected", {"claude": False}),
                (max_record, "Codex is not connected", {"codex": False}),
                (codex_record, "Codex is not connected", {"codex": False}),
            ):
                run_id = record["run_id"]
                workspace = jobs.workspace_dir(run_id)
                jobs._write_run(copy.deepcopy(record))
                with self.assertRaisesRegex(ValueError, refusal):
                    self._follow_up(tmp, run_id, "Now also prove 2+2=4", **disconnected)
                # Nothing shows as sent: no chat entry, session problem or file.
                self.assertEqual(jobs.list_chat(run_id), [], refusal)
                self.assertEqual([item["text"] for item in jobs._load_run_record(run_id)["problems"]], ["Prove 1+1=2"])
                self.assertFalse((workspace / "problems.json").exists())
                self.assertFalse((workspace / "attachments").exists())
                self.assertFalse(jobs.run_is_active(run_id))

            # Once the account is back, the same message is sent exactly once.
            run_id = max_record["run_id"]
            result = self._follow_up(tmp, run_id, "Now also prove 2+2=4")
            self.assertEqual(result["status"], "continuing")
            chat = jobs.list_chat(run_id)
            self.assertEqual([entry["content"] for entry in chat if entry["role"] == "user"], ["Now also prove 2+2=4"])
            self.assertEqual(len(chat[0]["attachments"]), 1)
            problems = [item["text"] for item in jobs._load_run_record(run_id)["problems"]]
            self.assertEqual(len(problems), 2)
            self.assertTrue(problems[1].startswith("Now also prove 2+2=4"))
            # Feedback for a running continuation is still queued; its route
            # is checked when it starts.
            self.assertTrue(jobs.run_is_active(run_id))
            queued = self._follow_up(tmp, run_id, "Then prove 3+3=6", claude=False)
            self.assertEqual(queued["status"], "queued")

    def test_billing_tuning_and_route_records(self) -> None:
        self.assertEqual(usage_ledger.billing_class({"auth_route": "dual_subscription"}), "subscription")
        caps = run_tuning.capabilities("max", "dual_subscription", "math_harness")
        self.assertEqual([item["id"] for item in caps["reasoning"]], ["max"])
        self.assertEqual([item["id"] for item in caps["speed"]], ["standard"])
        with self.assertRaisesRegex(ValueError, "does not support reasoning effort"):
            run_tuning.normalize("max", "dual_subscription", reasoning_effort="low")
        self.assertEqual(jobs._route_from_record({"live": {"auth_mode": "dual_subscription"}}), "dual_subscription")
        self.assertEqual(jobs._live_auth_mode("dual_subscription"), "dual_subscription")
        env: dict[str, str] = {}
        run_tuning.apply_environment(env, model="max", auth_route="dual_subscription", reasoning_effort="max")
        self.assertEqual((env["AGENT_MONITOR_RUN_REASONING_EFFORT"], env["AGENT_MONITOR_CLAUDE_EFFORT"]), ("max", "max"))
        # Codex receives -c model_reasoning_effort="max" for the worker model.
        self.assertIn('model_reasoning_effort="max"', " ".join(run_tuning.codex_config_args(env, "gpt-6-astra")))

    def test_settings_report_max_availability(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            account_patches, *_ = _accounts(tmp)
            with (account_patches[0], account_patches[1], account_patches[2], account_patches[3], account_patches[4], account_patches[5]):
                plan = cgm_max.plan_for(_ADMIN)
                member_plan = cgm_max.plan_for({"id": 18, "email": "student@ucla.edu"})
        self.assertTrue(plan["available"])
        self.assertEqual((plan["advisor"]["model"], plan["worker"]["model"]), ("fable", "gpt-6-astra"))
        self.assertFalse(member_plan["available"])


# ── the real runner and backends against fake codex / claude executables ───

import os  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402

_REPO = Path(__file__).resolve().parents[1]
_CATALOG = _REPO / "tests" / "fixtures" / "codex_0_160_bundled_catalog.json"

_FAKE_CLI = r'''#!/usr/bin/env python3
"""Test double for the codex and claude CLIs (one CGM role per call)."""
import json, os, sys
from pathlib import Path

KIND = __KIND__
REPLIES = __REPLIES__
CATALOG = __CATALOG__


def role(system):
    for marker, name in (("strategy advisor", "strategist"), ("# Proof Verification Agent", "verify"),
                         ("You are the curator", "curator"), ("independent worker", "worker")):
        if marker in system:
            return name
    return "unknown"


def answer(home, name, entry):
    log = Path(home) / "calls.jsonl"
    seen = sum(1 for line in log.read_text().splitlines() if json.loads(line)["role"] == name) if log.exists() else 0
    with log.open("a") as out:
        out.write(json.dumps({**entry, "role": name}) + "\n")
    options = REPLIES[name]
    return options[min(seen, len(options) - 1)]


args = sys.argv[1:]
env = {k: os.environ.get(k) for k in ("HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")}
if KIND == "codex":
    if args == ["--version"]:
        print("codex-cli 0.160.0")
    elif args[:3] == ["debug", "models", "--bundled"]:
        sys.stdout.write(Path(CATALOG).read_text())
    elif args[:1] == ["exec"]:
        prompt = sys.stdin.read()
        reply = answer(os.environ["CODEX_HOME"], role(prompt.split("USER REQUEST:", 1)[0]), {"argv": args, "env": env})
        Path(args[args.index("--output-last-message") + 1]).write_text(reply)
        for event in ({"type": "thread.started", "thread_id": "fake"}, {"type": "turn.started"},
                      {"type": "item.completed", "item": {"type": "agent_message", "text": reply}},
                      {"type": "turn.completed", "usage": {"input_tokens": 120, "cached_input_tokens": 20, "output_tokens": 64}}):
            print(json.dumps(event))
    else:
        sys.exit(2)
else:
    if args[:2] == ["auth", "status"]:
        print(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max"}))
    elif args == ["--version"]:
        print("2.1.288 (Claude Code)")
    elif "-p" in args:
        system = args[args.index("--system-prompt") + 1] if "--system-prompt" in args else ""
        reply = answer(os.environ["CLAUDE_CONFIG_DIR"], role(system), {"argv": [a for a in args if a != system], "env": env})
        model = "claude-" + args[args.index("--model") + 1] + "-5-1"
        for event in ({"type": "system", "subtype": "init", "model": model, "session_id": "fake"},
                      {"type": "assistant", "message": {"model": model, "content": [{"type": "text", "text": reply}]}},
                      {"type": "result", "subtype": "success", "is_error": False, "result": reply,
                       "usage": {"input_tokens": 90, "output_tokens": 40, "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5},
                       "total_cost_usd": 0.01}):
            print(json.dumps(event))
    else:
        sys.exit(2)
'''


def write_fake_clis(bin_dir: Path, replies: dict[str, list[str]]) -> None:
    """Install fake ``codex`` and ``claude`` executables answering ``replies`` by role."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    for kind in ("codex", "claude"):
        script = (
            _FAKE_CLI.replace("__KIND__", repr(kind))
            .replace("__REPLIES__", repr(replies))
            .replace("__CATALOG__", repr(str(_CATALOG)))
        )
        path = bin_dir / kind
        path.write_text(script)
        path.chmod(0o755)


@unittest.skipUnless(shutil.which("python3"), "python3 is unavailable")
class MaxSubprocessIntegrationTests(unittest.TestCase):
    def test_runner_and_backends_drive_both_clis_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            bin_dir, workspace = tmp / "bin", tmp / "workspace"
            codex_home, claude_home, call_home = tmp / "codex-home", tmp / "claude-home", tmp / "service-home"
            for path in (workspace, codex_home, claude_home, call_home):
                path.mkdir()
            write_fake_clis(
                bin_dir,
                {
                    "strategist": [_strategist(2)],
                    "worker": [_worker("first"), _worker("second")],
                    "verify": [_ok()],
                    "curator": [_curator()],
                    "unknown": ["unexpected role"],
                },
            )
            (workspace / "problem.txt").write_text(PROBLEM)
            env = {
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', os.defpath)}",
                "PYTHONPATH": str(_REPO),
                "LANG": "C.UTF-8",
                **{k: v for k, v in MAX_ENV.items() if k not in {"CODEX_HOME", "CLAUDE_CONFIG_DIR", "HOME", "AGENT_MONITOR_CODEX_CALL_HOME"}},
                "CODEX_HOME": str(codex_home),
                "CLAUDE_CONFIG_DIR": str(claude_home),
                "HOME": str(claude_home),
                "AGENT_MONITOR_CODEX_CALL_HOME": str(call_home),
                "OPENAI_API_KEY": "sk-must-not-reach-codex",
            }
            completed = subprocess.run(
                [sys.executable, "-u", str(_REPO / "agent_monitor" / "runners" / "math_harness_runner.py")],
                cwd=workspace, env=env, capture_output=True, text=True, timeout=180, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout[-2000:] + completed.stderr[-2000:])
            events = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")]

            codex_calls = [json.loads(line) for line in (codex_home / "calls.jsonl").read_text().splitlines()]
            claude_calls = [json.loads(line) for line in (claude_home / "calls.jsonl").read_text().splitlines()]
            self.assertEqual([c["role"] for c in codex_calls], ["worker", "worker"])
            self.assertEqual(sorted(c["role"] for c in claude_calls), ["curator", "strategist", "verify", "verify"])
            for call in codex_calls:
                argv = " ".join(call["argv"])
                self.assertIn("--model gpt-6-astra", argv)
                self.assertIn('model_reasoning_effort="max"', argv)
                self.assertIn("model_catalog_json", argv)
                self.assertEqual(call["env"]["CODEX_HOME"], str(codex_home))
                self.assertEqual(call["env"]["HOME"], str(call_home))
                self.assertIsNone(call["env"]["CLAUDE_CONFIG_DIR"])
                self.assertIsNone(call["env"]["OPENAI_API_KEY"])
            for call in claude_calls:
                argv = call["argv"]
                self.assertEqual(argv[argv.index("--model") + 1], "fable")
                self.assertEqual(argv[argv.index("--effort") + 1], "max")
                self.assertEqual(argv[argv.index("--max-turns") + 1], "1")
                self.assertEqual(argv[argv.index("--tools") + 1], "")
                self.assertEqual(call["env"]["CLAUDE_CONFIG_DIR"], str(claude_home.resolve()))
                self.assertEqual(call["env"]["HOME"], str(claude_home))
                self.assertIsNone(call["env"]["CODEX_HOME"])
                self.assertIsNone(call["env"]["ANTHROPIC_API_KEY"])

            receipt = _receipt(workspace)
            self.assertEqual(receipt["schema_version"], 4)
            self.assertEqual(receipt["verdict"], "accepted")
            self.assertEqual(receipt["model"], "claude-fable-5-1")
            graph = engine_graph.build(workspace, {"engine": "math_harness"})
            self.assertEqual(graph["provenance"]["receipt_status"], "verifier-correct")
            self.assertTrue((workspace / "proof.md").read_text().strip())
            usage = {e["stage"]: e["usage"] for e in events if e.get("type") == "turn.completed"}
            self.assertEqual(usage["worker-1"]["input_tokens"], 120)
            self.assertTrue(usage["strategist"].get("input_excludes_cache"))
