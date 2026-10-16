from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import jobs, lean_verify, proof_graph, settings


_PLAIN = {
    "id": "plain",
    "label": "Plain",
    "available": True,
    "auth_modes": ["api_key", "codex_subscription", "claude_subscription"],
}


class RuntimeRouteFailClosedTests(unittest.TestCase):
    def tearDown(self) -> None:
        with jobs._LOCK:
            jobs._JOBS.clear()

    # Subscription runtimes belong to the site administrator; a member's
    # settings always report the API route (tests/test_member_accounts.py).
    def test_claude_runtime_catalog_excludes_configured_api_models(self) -> None:
        env = {
            "AGENT_MONITOR_ACCOUNT_RUNTIME": "claude",
            "AGENT_MONITOR_MODEL": "kimi-k3",
            "KIMI_API_KEY": "configured-kimi",
            "ANTHROPIC_API_KEY": "configured-anthropic",
        }
        with (
            patch.object(settings, "resolved_user_env", return_value=env),
            patch("agent_monitor.codex_login.account_login_ready", return_value=True),
            patch("agent_monitor.codex_login.available_models", return_value=["gpt-account"]),
            patch("agent_monitor.claude_login.account_login_ready", return_value=True),
        ):
            result = settings.get_settings({"id": 17, "is_admin": True})

        self.assertEqual(result["account_runtime"], "claude")
        self.assertEqual(result["model_presets"], result["claude_models"])
        self.assertEqual(result["active_models"], result["claude_models"])
        self.assertEqual(result["default_model"], "sonnet")
        self.assertIn("kimi-k3", result["models_by_runtime"]["api"])
        self.assertNotIn("kimi-k3", result["model_presets"])

    def test_disconnected_claude_runtime_is_not_reported_as_api(self) -> None:
        env = {
            "AGENT_MONITOR_ACCOUNT_RUNTIME": "claude",
            "OPENAI_API_KEY": "configured-but-dormant",
        }
        with (
            patch.object(settings, "resolved_user_env", return_value=env),
            patch("agent_monitor.codex_login.account_login_ready", return_value=False),
            patch("agent_monitor.claude_login.account_login_ready", return_value=False),
        ):
            result = settings.get_settings({"id": 17, "is_admin": True})

        self.assertEqual(result["auth_mode"], "claude_subscription")
        self.assertFalse(result["runtime_ready"])
        self.assertEqual(result["active_models"], [])
        self.assertFalse(result["api_providers_active"])

    def test_subscription_environment_scrubs_all_provider_routes(self) -> None:
        polluted = {name: f"secret-{index}" for index, name in enumerate(jobs._PROVIDER_ROUTE_ENV_NAMES)}
        polluted["PATH"] = "/bin"
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("agent_monitor.claude_login.account_home", return_value=Path(tmp)),
            patch("agent_monitor.claude_login.account_login_ready", return_value=True),
            patch("agent_monitor.auth.is_operator_account", return_value=True),
        ):
            configured = jobs._configure_auth_route(
                polluted, route="claude_subscription", user_id=17
            )

        for name in jobs._PROVIDER_ROUTE_ENV_NAMES:
            self.assertNotIn(name, configured)
        self.assertEqual(configured["AGENT_MONITOR_AUTH_MODE"], "claude_subscription")

    def test_stale_kimi_model_cannot_override_claude_runtime(self) -> None:
        with (
            patch("agent_monitor.engines_registry.all_engine_ids", return_value={"plain"}),
            patch("agent_monitor.engines_registry.list_engines", return_value=[dict(_PLAIN)]),
            patch("agent_monitor.engines_registry.supported_models", return_value=[]),
            patch.object(
                jobs,
                "_user_extra_env",
                return_value={
                    "AGENT_MONITOR_ACCOUNT_RUNTIME": "claude",
                    "KIMI_API_KEY": "configured-but-dormant",
                },
            ),
            patch.object(jobs, "_configure_auth_route") as configure,
            patch.object(jobs.threading, "Thread") as thread,
        ):
            with self.assertRaisesRegex(ValueError, "Kimi K3.*cannot use"):
                jobs.start_job(
                    engine="plain",
                    problem_text="Prove one equals one.",
                    model="kimi-k3",
                    user={"id": 17, "is_admin": True},
                )
        configure.assert_not_called()
        thread.assert_not_called()

    def test_stale_api_subagent_model_is_rejected_under_claude(self) -> None:
        with (
            patch("agent_monitor.engines_registry.all_engine_ids", return_value={"plain"}),
            patch("agent_monitor.engines_registry.list_engines", return_value=[dict(_PLAIN)]),
            patch("agent_monitor.engines_registry.supported_models", return_value=[]),
            patch.object(
                jobs,
                "_user_extra_env",
                return_value={"AGENT_MONITOR_ACCOUNT_RUNTIME": "claude"},
            ),
            patch.object(jobs, "_configure_auth_route") as configure,
            patch.object(jobs.threading, "Thread") as thread,
        ):
            with self.assertRaisesRegex(ValueError, "Unsupported Claude subagent model"):
                jobs.start_job(
                    engine="plain",
                    problem_text="Prove one equals one.",
                    model="sonnet",
                    use_subagents=True,
                    subagent_model="kimi-k3",
                    user={"id": 17, "is_admin": True},
                )
        configure.assert_not_called()
        thread.assert_not_called()

    def test_stale_api_main_model_is_rejected_under_codex(self) -> None:
        with (
            patch("agent_monitor.engines_registry.all_engine_ids", return_value={"plain"}),
            patch("agent_monitor.engines_registry.list_engines", return_value=[dict(_PLAIN)]),
            patch("agent_monitor.engines_registry.supported_models", return_value=[]),
            patch.object(
                jobs,
                "_user_extra_env",
                return_value={"AGENT_MONITOR_ACCOUNT_RUNTIME": "codex"},
            ),
            patch("agent_monitor.codex_login.available_models", return_value=["gpt-account"]),
            patch.object(jobs, "_configure_auth_route") as configure,
            patch.object(jobs.threading, "Thread") as thread,
        ):
            with self.assertRaisesRegex(ValueError, "Unsupported Codex account model"):
                jobs.start_job(
                    engine="plain",
                    problem_text="Prove one equals one.",
                    model="claude-api-model",
                    user={"id": 17, "is_admin": True},
                )
        configure.assert_not_called()
        thread.assert_not_called()

    def test_incompatible_engine_does_not_fall_back_to_api(self) -> None:
        api_only = {
            "id": "ucla",
            "label": "UCLA",
            "available": True,
            "auth_modes": ["api_key"],
        }
        with (
            patch("agent_monitor.engines_registry.all_engine_ids", return_value={"ucla"}),
            patch("agent_monitor.engines_registry.list_engines", return_value=[api_only]),
            patch.object(
                jobs,
                "_user_extra_env",
                return_value={
                    "AGENT_MONITOR_ACCOUNT_RUNTIME": "claude",
                    "OPENAI_API_KEY": "configured-but-dormant",
                },
            ),
            patch.object(jobs, "_configure_auth_route") as configure,
        ):
            with self.assertRaisesRegex(ValueError, "selected Claude Code subscription"):
                jobs.start_job(
                    engine="ucla",
                    problem_text="Prove one equals one.",
                    user={"id": 17, "is_admin": True},
                )
        configure.assert_not_called()

    def test_stale_explicit_route_conflicting_with_settings_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "conflicts with the selected claude"):
            jobs._selected_auth_route(
                requested="api_key",
                engine="plain",
                model="sonnet",
                auth_modes=set(_PLAIN["auth_modes"]),
                user={"id": 17, "is_admin": True},
                env={"AGENT_MONITOR_ACCOUNT_RUNTIME": "claude"},
            )

    def test_stale_explicit_route_conflicts_with_legacy_claude_flag(self) -> None:
        with self.assertRaisesRegex(ValueError, "conflicts with the selected claude"):
            jobs._selected_auth_route(
                requested="api_key",
                engine="plain",
                model="sonnet",
                auth_modes=set(_PLAIN["auth_modes"]),
                user={"id": 17, "is_admin": True},
                env={
                    "AGENT_MONITOR_USE_CLAUDE": "1",
                    "AGENT_MONITOR_USE_CODEX": "0",
                },
            )

    def test_recorded_codex_dag_never_calls_direct_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "problem.txt").write_text("Prove one equals one.", encoding="utf-8")
            (workspace / "proof.md").write_text("Assume equality. Therefore one equals one.", encoding="utf-8")
            graph_json = '{"title":"demo","nodes":[{"id":"n1","kind":"conclusion","label":"done","statement":"1=1","citations":[]}],"edges":[]}'
            with (
                patch.object(settings, "resolved_user_env", return_value={"OPENAI_API_KEY": "must-not-run"}),
                patch.object(proof_graph, "_run_codex_prompt", return_value=graph_json) as codex,
                patch.object(proof_graph, "_call_llm") as direct_api,
            ):
                result = proof_graph.generate(
                    workspace=workspace,
                    run_record={"auth_route": "codex_subscription", "owner_id": 17},
                    user={"id": 17, "is_admin": True},
                    model=None,
                    allow_codex_fallback=False,
                )

        self.assertEqual(result["model"], "codex")
        codex.assert_called_once()
        direct_api.assert_not_called()

    def _rebuild_dag(self, run_record: dict, call_llm) -> tuple[dict | None, list, list]:
        """A console Rebuild ({kind} only) on an account defaulting to another
        provider's model, with a connected Codex runtime."""
        graph_json = '{"title":"demo","nodes":[{"id":"n1","kind":"conclusion","label":"done","statement":"1=1","citations":[]}],"edges":[]}'
        account = {
            "OPENAI_API_KEY": "openai-key", "ANTHROPIC_API_KEY": "anthropic-key",
            "AGENT_MONITOR_MODEL": "gpt-6-astra", "AGENT_MONITOR_ACCOUNT_RUNTIME": "codex",
        }
        models: list = []
        codex_calls: list = []

        def direct(env, model, _prompt, **_kwargs):
            models.append(model)
            return call_llm(graph_json)

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "proof.md").write_text("Assume equality. Therefore one equals one.", encoding="utf-8")
            with (
                patch.object(settings, "resolved_user_env", return_value=account),
                patch.object(proof_graph, "_call_llm", side_effect=direct),
                patch.object(proof_graph, "_codex_account_ready", return_value=True),
                patch.object(proof_graph, "_claude_account_ready", return_value=False),
                patch.object(
                    proof_graph, "_run_codex_prompt",
                    side_effect=lambda *_a, **kw: codex_calls.append(kw.get("model")) or graph_json,
                ),
            ):
                result = proof_graph.generate(
                    workspace=workspace, run_record=run_record,
                    user={"id": 17, "is_admin": True}, model=None,
                )
        return result, models, codex_calls

    def test_recorded_api_dag_rebuild_keeps_the_runs_model_and_route(self) -> None:
        record = {"auth_route": "api_key", "owner_id": 17,
                  "requested_model": "claude-opus-4-8", "model": "claude-opus-4-8"}
        result, models, codex_calls = self._rebuild_dag(
            record, lambda graph: ("claude-opus-4-8", graph, "anthropic")
        )
        # The run's model, not the account's current default (gpt-6-astra).
        self.assertEqual(models, ["claude-opus-4-8"])
        self.assertEqual(result["model"], "claude-opus-4-8")
        self.assertEqual(codex_calls, [])

        def rate_limited(_graph):
            raise ValueError("Anthropic model call failed for claude-opus-4-8 (HTTP 429)")

        # A failed API call is reported; it never moves to the Codex subscription.
        with self.assertRaisesRegex(ValueError, "HTTP 429"):
            self._rebuild_dag(record, rate_limited)

    def test_legacy_dag_without_a_recorded_route_keeps_the_codex_fallback(self) -> None:
        def no_key(_graph):
            raise ValueError("Model gpt-6-astra requires OPENAI_API_KEY")

        result, models, codex_calls = self._rebuild_dag({"owner_id": 17, "model": "gpt-6-astra"}, no_key)
        self.assertEqual(models, [None])
        self.assertEqual(codex_calls, ["gpt-6-astra"])
        self.assertEqual(result["model"], "gpt-6-astra")

    def test_an_explicit_codex_dag_model_says_why_a_recorded_run_keeps_its_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "proof.md").write_text("Assume equality. Therefore one equals one.", encoding="utf-8")
            with (
                patch.object(settings, "resolved_user_env", return_value={"OPENAI_API_KEY": "openai-key"}),
                patch.object(proof_graph, "_call_llm", side_effect=AssertionError("API call")) as direct_api,
                patch.object(proof_graph, "_run_codex_prompt", side_effect=AssertionError("Codex")) as codex,
                patch.object(proof_graph, "_run_claude_prompt", side_effect=AssertionError("Claude")) as claude,
            ):
                for route, name in (("api_key", "an API key"), ("claude_subscription", "a Claude subscription")):
                    with self.assertRaisesRegex(
                        ValueError, f"^This run was recorded on {name}, so its DAG is rebuilt on that route"
                    ):
                        proof_graph.generate(
                            workspace=workspace,
                            run_record={"auth_route": route, "owner_id": 17, "model": "claude-opus-4-8"},
                            user={"id": 17, "is_admin": True},
                            model="codex",
                        )

        direct_api.assert_not_called()
        codex.assert_not_called()
        claude.assert_not_called()

    def test_recorded_codex_lean_chat_never_calls_direct_api(self) -> None:
        env = {"AGENT_MONITOR_CODEX_SUBSCRIPTION": "1", "CODEX_HOME": "/private/codex"}
        with (
            patch("agent_monitor.proof_graph._run_codex_prompt_env", return_value='{"lean":"theorem main : True := by trivial","uses_mathlib":false}') as codex,
            patch.object(lean_verify, "_call_llm") as direct_api,
        ):
            selected, content = lean_verify._chat(env, None, "formalize", timeout=5)

        self.assertEqual(selected, "codex")
        self.assertIn("theorem main", content)
        codex.assert_called_once()
        direct_api.assert_not_called()


class SavedDefaultModelTests(unittest.TestCase):
    """A blank model (the Projects page's "Default account model") is the
    administrator's saved default model when the run's route can run it."""

    def tearDown(self) -> None:
        with jobs._LOCK:
            jobs._JOBS.clear()

    def _start(self, engine: str, saved: dict, *, model: str | None = None) -> dict:
        captured: dict = {}

        class FakeThread:
            def __init__(self, *, kwargs, **_unused):
                captured.update(kwargs)

            def start(self) -> None:
                pass

        info = {**_PLAIN, "id": engine, "label": engine}
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("agent_monitor.engines_registry.all_engine_ids", return_value={engine}),
                patch("agent_monitor.engines_registry.list_engines", return_value=[info]),
                patch("agent_monitor.engines_registry.supported_models", return_value=[]),
                patch.object(settings, "resolved_user_env", return_value=dict(saved)),
                patch.object(jobs, "workspace_dir", return_value=Path(tmp)),
                patch.object(jobs, "_write_run"),
                patch("agent_monitor.claude_login.account_home", return_value=Path(tmp) / "claude"),
                patch("agent_monitor.claude_login.account_login_ready", return_value=True),
                patch("agent_monitor.codex_login.account_home", return_value=Path(tmp) / "codex"),
                patch("agent_monitor.codex_login.account_login_ready", return_value=True),
                patch(
                    "agent_monitor.codex_login.available_models",
                    return_value=["gpt-6-astra", "gpt-5.6-sol"],
                ),
                patch.object(jobs.threading, "Thread", FakeThread),
            ):
                job = jobs.start_job(
                    engine=engine,
                    problem_text="Prove one equals one.",
                    model=model,
                    user={"id": 17, "is_admin": True},
                    use_subagents=False,
                )
        self.assertEqual(job["model"], captured["model"])
        return captured

    def test_blank_model_runs_the_saved_claude_default_on_every_harness(self) -> None:
        saved = {"AGENT_MONITOR_ACCOUNT_RUNTIME": "claude", "AGENT_MONITOR_MODEL": "opus"}
        for engine in ("claude", "plain", "improof"):
            with self.subTest(engine=engine):
                captured = self._start(engine, saved)
                self.assertEqual(captured["model"], "opus")
                env = dict(captured["extra_env"])
                jobs._apply_selected_model_env(env, engine=engine, model=captured["model"])
                self.assertEqual(env["AGENT_MONITOR_CLAUDE_MODEL"], "opus")

    def test_blank_model_runs_the_saved_codex_default(self) -> None:
        saved = {"AGENT_MONITOR_ACCOUNT_RUNTIME": "codex", "AGENT_MONITOR_MODEL": "gpt-5.6-sol"}
        captured = self._start("plain", saved)
        self.assertEqual(captured["model"], "gpt-5.6-sol")

    def test_blank_model_runs_the_saved_api_default_with_its_key(self) -> None:
        from agent_monitor.runners import math_harness_runner
        from agent_monitor.subprocess_env import child_process_env

        saved = {
            "AGENT_MONITOR_ACCOUNT_RUNTIME": "api",
            "AGENT_MONITOR_MODEL": "claude-opus-4-8",
            "ANTHROPIC_API_KEY": "sk-ant-test",
        }
        captured = self._start("math_harness", saved)
        self.assertEqual(captured["model"], "claude-opus-4-8")
        self.assertEqual(captured["extra_env"]["ANTHROPIC_API_KEY"], "sk-ant-test")
        env = child_process_env(extra=captured["extra_env"])
        jobs._apply_selected_model_env(env, engine="math_harness", model=captured["model"])
        self.assertEqual(math_harness_runner._select_route(env).model, "claude-opus-4-8")

    def test_a_saved_default_the_route_cannot_run_leaves_the_model_blank(self) -> None:
        for runtime, saved_model, keys in (
            # An API model left from another runtime never refuses the run.
            ("claude", "claude-opus-4-8", {"ANTHROPIC_API_KEY": "sk-ant-test"}),
            ("codex", "opus", {}),
            ("api", "opus", {"OPENAI_API_KEY": "sk-test"}),
            # A provider without a key, and Max, which is always chosen.
            ("api", "claude-opus-4-8", {"OPENAI_API_KEY": "sk-test"}),
            ("claude", "max", {}),
        ):
            saved = {"AGENT_MONITOR_ACCOUNT_RUNTIME": runtime, "AGENT_MONITOR_MODEL": saved_model, **keys}
            with self.subTest(runtime=runtime, saved=saved_model):
                self.assertIsNone(self._start("plain", saved)["model"])

    def test_a_requested_model_wins_over_the_saved_default(self) -> None:
        saved = {"AGENT_MONITOR_ACCOUNT_RUNTIME": "claude", "AGENT_MONITOR_MODEL": "opus"}
        self.assertEqual(self._start("plain", saved, model="fable")["model"], "fable")


if __name__ == "__main__":
    unittest.main()
