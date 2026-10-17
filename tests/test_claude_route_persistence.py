from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import jobs, monitor_overview


HTML = (
    Path(__file__).parents[1] / "agent_monitor" / "web" / "console.html"
).read_text(encoding="utf-8")
SCRIPT = HTML.rsplit("<script>", 1)[1].split("</script>", 1)[0]
NODE = shutil.which("node")
if not NODE and Path("/home/ubuntu/.local/node24/bin/node").is_file():
    NODE = "/home/ubuntu/.local/node24/bin/node"


def _js_function(name: str) -> str:
    function_start = SCRIPT.index(f"function {name}(")
    start = function_start - 6 if SCRIPT[function_start - 6 : function_start] == "async " else function_start
    opening = SCRIPT.index("{", SCRIPT.index(")", function_start))
    depth = 0
    for index in range(opening, len(SCRIPT)):
        char = SCRIPT[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return SCRIPT[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {name}")


_ENGINE = {
    "id": "plain",
    "label": "Plain",
    "available": True,
    "auth_modes": ["api_key", "codex_subscription", "claude_subscription"],
}


class ClaudeRoutePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        # Subscriptions are the administrator's; account 17 stands for it here.
        operator = patch("agent_monitor.auth.is_operator_account", return_value=True)
        operator.start()
        self.addCleanup(operator.stop)

    def tearDown(self) -> None:
        with jobs._LOCK:
            jobs._JOBS.clear()

    def test_claude_route_uses_private_home_and_scrubs_provider_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            account_home = Path(tmp) / "user_17"
            polluted = {
                "HOME": "/home/server-account",
                "CLAUDE_CONFIG_DIR": "/home/other-claude-account",
                "CODEX_HOME": "/home/other-codex-account",
                "ANTHROPIC_API_KEY": "metered-key",
                "ANTHROPIC_AUTH_TOKEN": "metered-token",
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "AWS_ACCESS_KEY_ID": "cloud-account",
                "AGENT_MONITOR_CODEX_SUBSCRIPTION": "1",
            }
            with (
                patch("agent_monitor.claude_login.account_home", return_value=account_home),
                patch("agent_monitor.claude_login.account_login_ready", return_value=True),
            ):
                configured = jobs._configure_auth_route(
                    polluted, route="claude_subscription", user_id=17
                )

        self.assertEqual(configured["HOME"], str(account_home))
        self.assertEqual(configured["CLAUDE_CONFIG_DIR"], str(account_home))
        self.assertEqual(configured["AGENT_MONITOR_CLAUDE_SUBSCRIPTION"], "1")
        self.assertEqual(configured["AGENT_MONITOR_AUTH_MODE"], "claude_subscription")
        self.assertNotIn("AGENT_MONITOR_CODEX_SUBSCRIPTION", configured)
        self.assertNotIn("CODEX_HOME", configured)
        for name in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_USE_BEDROCK",
            "AWS_ACCESS_KEY_ID",
        ):
            self.assertNotIn(name, configured)

    def test_new_explicit_claude_run_never_falls_back_to_available_api_key(self) -> None:
        with (
            patch("agent_monitor.engines_registry.all_engine_ids", return_value={"plain"}),
            patch("agent_monitor.engines_registry.list_engines", return_value=[dict(_ENGINE)]),
            patch("agent_monitor.engines_registry.supported_models", return_value=["sonnet"]),
            patch.object(jobs, "_user_extra_env", return_value={"ANTHROPIC_API_KEY": "api-key"}),
            patch("agent_monitor.claude_login.account_home", return_value=Path("/tmp/missing-claude")),
            patch("agent_monitor.claude_login.account_login_ready", return_value=False),
        ):
            with self.assertRaisesRegex(ValueError, "Claude Code is not connected"):
                jobs.start_job(
                    engine="plain",
                    problem_text="Prove 1 = 1.",
                    model="sonnet",
                    auth_route="claude_subscription",
                    user={"id": 17, "email": "person@example.com", "is_admin": True},
                )
        self.assertFalse(jobs._JOBS)


    def test_successful_claude_start_records_and_passes_one_route(self) -> None:
        captured: dict = {}
        written: list[dict] = []

        class FakeThread:
            def __init__(self, *, kwargs, **_unused):
                captured.update(kwargs)

            def start(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            account_home = Path(tmp) / "claude-user-17"
            with (
                patch("agent_monitor.engines_registry.all_engine_ids", return_value={"plain"}),
                patch("agent_monitor.engines_registry.list_engines", return_value=[dict(_ENGINE)]),
                patch("agent_monitor.engines_registry.supported_models", return_value=["sonnet"]),
                patch.object(
                    jobs,
                    "_user_extra_env",
                    return_value={"HOME": "/server", "ANTHROPIC_API_KEY": "api-key"},
                ),
                patch.object(jobs, "workspace_dir", return_value=workspace),
                patch.object(jobs, "_write_run", side_effect=lambda run: written.append(dict(run))),
                patch("agent_monitor.claude_login.account_home", return_value=account_home),
                patch("agent_monitor.claude_login.account_login_ready", return_value=True),
                patch("agent_monitor.jobs.threading.Thread", FakeThread),
            ):
                job = jobs.start_job(
                    engine="plain",
                    problem_text="Prove 1 = 1.",
                    model="sonnet",
                    auth_route="claude_subscription",
                    user={"id": 17, "email": "person@example.com", "is_admin": True},
                )

        self.assertEqual(job["auth_route"], "claude_subscription")
        self.assertEqual(written[0]["auth_route"], "claude_subscription")
        self.assertEqual(written[0]["requested_model"], "sonnet")
        self.assertEqual(written[0]["live"]["requested_model"], "sonnet")
        self.assertEqual(captured["extra_env"]["HOME"], str(account_home))
        self.assertEqual(captured["extra_env"]["CLAUDE_CONFIG_DIR"], str(account_home))
        self.assertNotIn("ANTHROPIC_API_KEY", captured["extra_env"])
        self.assertNotIn("AGENT_MONITOR_CODEX_SUBSCRIPTION", captured["extra_env"])
    def test_recorded_api_continuation_ignores_current_account_toggle(self) -> None:
        record = {
            "run_id": "plain_api",
            "engine": "plain",
            "owner_id": 17,
            "auth_route": "api_key",
        }
        polluted = {
            "ANTHROPIC_API_KEY": "api-key",
            "AGENT_MONITOR_ACCOUNT_RUNTIME": "claude",
            "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
            "CLAUDE_CONFIG_DIR": "/home/other-account",
            "CODEX_HOME": "/home/other-account",
        }
        with (
            patch("agent_monitor.engines_registry.list_engines", return_value=[dict(_ENGINE)]),
            patch("agent_monitor.claude_login.account_login_ready") as claude_ready,
            patch("agent_monitor.codex_login.account_login_ready") as codex_ready,
        ):
            configured, route = jobs._continuation_auth_environment(
                run_data=record,
                engine="plain",
                model="claude-sonnet-4-6",
                user={"id": 17, "is_admin": True},
                env=polluted,
            )

        self.assertEqual(route, "api_key")
        self.assertEqual(configured["AGENT_MONITOR_AUTH_MODE"], "api_key")
        self.assertNotIn("AGENT_MONITOR_CLAUDE_SUBSCRIPTION", configured)
        self.assertNotIn("CLAUDE_CONFIG_DIR", configured)
        self.assertNotIn("CODEX_HOME", configured)
        claude_ready.assert_not_called()
        codex_ready.assert_not_called()

    def test_recorded_claude_continuation_fails_closed_when_login_is_gone(self) -> None:
        record = {
            "run_id": "plain_claude",
            "engine": "plain",
            "owner_id": 17,
            "auth_route": "claude_subscription",
        }
        with (
            patch("agent_monitor.engines_registry.list_engines", return_value=[dict(_ENGINE)]),
            patch("agent_monitor.claude_login.account_home", return_value=Path("/tmp/claude-17")),
            patch("agent_monitor.claude_login.account_login_ready", return_value=False),
        ):
            with self.assertRaisesRegex(ValueError, "Claude Code is not connected"):
                jobs._continuation_auth_environment(
                    run_data=record,
                    engine="plain",
                    model="sonnet",
                    user={"id": 17, "is_admin": True},
                    env={"ANTHROPIC_API_KEY": "fallback-must-not-run"},
                )

    def test_continue_run_passes_and_records_original_claude_route(self) -> None:
        captured: dict = {}

        class FakeThread:
            def __init__(self, *, kwargs, **_unused):
                captured.update(kwargs)

            def start(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            (runs / "workspaces" / "plain_claude").mkdir(parents=True)
            account_home = runs / "claude-home"
            record = {
                "run_id": "plain_claude",
                "engine": "plain",
                "owner_id": 17,
                "auth_route": "claude_subscription",
                "problem_id": "demo",
                "problem_text": "Prove 1 = 1.",
                "problems": [{"text": "Prove 1 = 1.", "source": "initial"}],
                "requested_model": "fable",
                "live": {
                    "model": "claude-fable-5",
                    "auth_mode": "claude_subscription",
                },
            }
            with (
                patch.object(jobs, "RUNS_DIR", runs),
                patch.object(jobs, "_run_is_active", return_value=None),
                patch.object(jobs, "_load_run_record", return_value=record),
                patch.object(jobs, "_append_chat"),
                patch.object(
                    jobs,
                    "_user_extra_env",
                    return_value={"HOME": "/server", "ANTHROPIC_API_KEY": "api-key"},
                ),
                patch("agent_monitor.engines_registry.list_engines", return_value=[dict(_ENGINE)]),
                patch("agent_monitor.claude_login.account_home", return_value=account_home),
                patch("agent_monitor.claude_login.account_login_ready", return_value=True),
                patch("agent_monitor.jobs.threading.Thread", FakeThread),
            ):
                result = jobs.continue_run(
                    "plain_claude", message="Check the endpoint.", user={"id": 17, "is_admin": True}
                )

        self.assertEqual(captured["extra_env"]["HOME"], str(account_home))
        self.assertEqual(captured["extra_env"]["CLAUDE_CONFIG_DIR"], str(account_home))
        self.assertEqual(captured["extra_env"]["AGENT_MONITOR_AUTH_MODE"], "claude_subscription")
        self.assertEqual(captured["model"], "fable")
        self.assertNotIn("ANTHROPIC_API_KEY", captured["extra_env"])
        self.assertEqual(jobs._JOBS[result["job_id"]]["auth_route"], "claude_subscription")


    def test_recorded_codex_continuation_cannot_be_redirected_to_claude(self) -> None:
        codex_home = Path("/tmp/codex-user-17")
        record = {
            "run_id": "plain_codex",
            "engine": "plain",
            "owner_id": 17,
            "auth_route": "codex_subscription",
        }
        with (
            patch("agent_monitor.engines_registry.list_engines", return_value=[dict(_ENGINE)]),
            patch("agent_monitor.codex_login.account_home", return_value=codex_home),
            patch("agent_monitor.codex_login.account_login_ready", return_value=True),
            patch("agent_monitor.claude_login.account_login_ready") as claude_ready,
        ):
            configured, route = jobs._continuation_auth_environment(
                run_data=record,
                engine="plain",
                model="gpt-5.6-sol",
                user={"id": 17, "is_admin": True},
                env={
                    "AGENT_MONITOR_ACCOUNT_RUNTIME": "claude",
                    "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
                    "CLAUDE_CONFIG_DIR": "/tmp/other-claude",
                },
            )

        self.assertEqual(route, "codex_subscription")
        self.assertEqual(configured["CODEX_HOME"], str(codex_home))
        self.assertEqual(configured["AGENT_MONITOR_AUTH_MODE"], "chatgpt_subscription")
        self.assertNotIn("AGENT_MONITOR_CLAUDE_SUBSCRIPTION", configured)
        self.assertNotIn("CLAUDE_CONFIG_DIR", configured)
        claude_ready.assert_not_called()

    def test_hitl_resume_uses_the_same_recorded_claude_route(self) -> None:
        captured: dict = {}

        class FakeThread:
            def __init__(self, *, kwargs, **_unused):
                captured.update(kwargs)

            def start(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            (runs / "workspaces" / "plain_hitl").mkdir(parents=True)
            account_home = runs / "claude-home"
            record = {
                "run_id": "plain_hitl",
                "engine": "plain",
                "owner_id": 17,
                "auth_route": "claude_subscription",
                "problem_id": "demo",
                "problem_text": "Prove 1 = 1.",
                "problems": [{"text": "Prove 1 = 1.", "source": "initial"}],
                "live": {"model": "sonnet", "auth_mode": "claude_subscription"},
            }
            with (
                patch.object(jobs, "RUNS_DIR", runs),
                patch.object(jobs, "_run_is_active", return_value=None),
                patch.object(jobs, "_load_run_record", return_value=record),
                patch.object(jobs, "_write_run"),
                patch.object(jobs, "_write_session_problems"),
                patch.object(jobs, "_append_chat"),
                patch.object(
                    jobs,
                    "_user_extra_env",
                    return_value={"HOME": "/server", "ANTHROPIC_API_KEY": "api-key"},
                ),
                patch("agent_monitor.engines_registry.list_engines", return_value=[dict(_ENGINE)]),
                patch("agent_monitor.claude_login.account_home", return_value=account_home),
                patch("agent_monitor.claude_login.account_login_ready", return_value=True),
                patch("agent_monitor.jobs.threading.Thread", FakeThread),
            ):
                result = jobs.send_human_message(
                    "plain_hitl", "Check the endpoint.", user={"id": 17, "is_admin": True}
                )

        self.assertEqual(captured["extra_env"]["HOME"], str(account_home))
        self.assertEqual(captured["extra_env"]["AGENT_MONITOR_AUTH_MODE"], "claude_subscription")
        self.assertNotIn("ANTHROPIC_API_KEY", captured["extra_env"])
        self.assertEqual(jobs._JOBS[result["job_id"]]["auth_route"], "claude_subscription")

    def _follow_up(self, send, route: str, recorded_model: str, sent_model: str) -> tuple[dict, dict]:
        """Continue a finished run with ``sent_model``; return the result and thread kwargs."""
        captured: dict = {}

        class FakeThread:
            def __init__(self, *, kwargs, **_unused):
                captured.update(kwargs)

            def start(self) -> None:
                pass

        record = {
            "run_id": "plain_follow_up",
            "engine": "plain",
            "owner_id": 17,
            "auth_route": route,
            "problem_id": "demo",
            "problem_text": "Prove 1 = 1.",
            "problems": [{"text": "Prove 1 = 1.", "source": "initial"}],
            "model": recorded_model,
            "requested_model": recorded_model,
            "live": {"model": recorded_model, "auth_mode": jobs._live_auth_mode(route)},
        }
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            (runs / "workspaces" / "plain_follow_up").mkdir(parents=True)
            with (
                patch.object(jobs, "RUNS_DIR", runs),
                patch.object(jobs, "_run_is_active", return_value=None),
                patch.object(jobs, "_load_run_record", side_effect=lambda _run_id: json.loads(json.dumps(record))),
                patch.object(jobs, "_write_run"),
                patch.object(jobs, "_write_session_problems"),
                patch.object(jobs, "_append_chat", return_value={}),
                patch.object(jobs, "_user_extra_env", return_value={"HOME": "/server"}),
                patch("agent_monitor.engines_registry.list_engines", return_value=[dict(_ENGINE)]),
                patch("agent_monitor.codex_login.account_home", return_value=runs / "codex-home"),
                patch("agent_monitor.codex_login.account_login_ready", return_value=True),
                patch(
                    "agent_monitor.codex_login.available_models",
                    return_value=["gpt-6-astra", "gpt-5.6-sol"],
                ),
                patch("agent_monitor.claude_login.account_home", return_value=runs / "claude-home"),
                patch("agent_monitor.claude_login.account_login_ready", return_value=True),
                patch("agent_monitor.jobs.threading.Thread", FakeThread),
            ):
                result = send("plain_follow_up", sent_model)
        with jobs._LOCK:
            job = jobs._JOBS.pop(result["job_id"])
        self.assertEqual(job["model"], captured["model"])
        return result, captured

    def test_follow_up_keeps_the_run_model_over_one_its_route_cannot_run(self) -> None:
        # An older console sends the new-run launcher's model with every chat
        # message; a model of another route leaves the run on its own model.
        admin = {"id": 17, "is_admin": True}
        senders = {
            "chat": lambda run_id, model: jobs.send_human_message(
                run_id, "Check the endpoint.", model=model, user=admin
            ),
            "continue": lambda run_id, model: jobs.continue_run(run_id, model=model, user=admin),
        }
        for name, send in senders.items():
            for route, recorded, sent in (
                ("codex_subscription", "gpt-6-astra", "opus"),
                ("codex_subscription", "gpt-6-astra", "max"),
                ("codex_subscription", "gpt-6-astra", "kimi-k3"),
                ("claude_subscription", "sonnet", "gpt-6-astra"),
                ("claude_subscription", "sonnet", "max"),
            ):
                with self.subTest(path=name, route=route, sent=sent):
                    result, captured = self._follow_up(send, route, recorded, sent)
                    self.assertEqual(result["status"], "continuing")
                    self.assertEqual(captured["model"], recorded)

    def test_follow_up_keeps_the_run_model_over_one_its_engine_cannot_run(self) -> None:
        # As at a start: the kimi engine runs only Kimi K3, and OpenClaude's
        # Codex adapter only gpt-5.6-sol, although the account offers more.
        admin = {"id": 17, "is_admin": True}
        kimi_run = {
            "engine": "kimi", "auth_route": "api_key", "credential_source": "sponsored_kimi",
            "model": "kimi-k3", "requested_model": "kimi-k3",
        }
        openclaude_run = {
            "engine": "openclaude", "auth_route": "codex_subscription",
            "model": "gpt-5.6-sol", "requested_model": "gpt-5.6-sol",
        }
        plain_run = {**openclaude_run, "engine": "plain"}
        pick = jobs._member_continuation_model
        with patch("agent_monitor.codex_login.available_models", return_value=["gpt-6-astra", "gpt-5.6-sol"]):
            self.assertEqual(pick(admin, "kimi", kimi_run, "gpt-6-astra"), "kimi-k3")
            self.assertEqual(pick(admin, "openclaude", openclaude_run, "gpt-6-astra"), "gpt-5.6-sol")
            # A model the engine supports is still taken.
            self.assertEqual(pick(admin, "plain", plain_run, "gpt-6-astra"), "gpt-6-astra")

    def test_refused_chat_follow_up_records_nothing(self) -> None:
        record = {
            "run_id": "plain_unknown",
            "engine": "plain",
            "owner_id": 17,
            "auth_route": "claude_subscription",
            "problems": [{"text": "Prove 1 = 1.", "source": "initial"}],
            "live": {"model": "claude-unknown-9", "auth_mode": "claude_subscription"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            (runs / "workspaces" / "plain_unknown").mkdir(parents=True)
            with (
                patch.object(jobs, "RUNS_DIR", runs),
                patch.object(jobs, "_load_run_record", return_value=record),
                patch.object(jobs, "_write_run") as write_run,
                patch.object(jobs, "_append_chat") as append_chat,
            ):
                # A follow-up offers no model choice: the way on is a new run.
                with self.assertRaisesRegex(ValueError, "unsupported Claude account model.*start a new run"):
                    jobs.send_human_message(
                        "plain_unknown", "Check the endpoint.", user={"id": 17, "is_admin": True}
                    )
        append_chat.assert_not_called()
        write_run.assert_not_called()

    def test_chat_composer_does_not_send_the_launcher_model(self) -> None:
        if not NODE:
            self.skipTest("node is unavailable")
        program = f"""
let runId='plain_run', sent=null;
const nodes={{'chat-input':{{value:'Check the endpoint.'}},'chat-send':{{disabled:false}},'chat-msgs':{{innerHTML:''}}}};
const $=id=>nodes[id];
const chatAttachments={{files:[],busy:false,lock(){{}},clear(){{}},async payload(){{return [];}}}};
const currentModel=()=>'opus', renderChat=()=>{{}}, setPill=()=>{{}}, startPolling=()=>{{}}, escapeHtml=s=>s;
globalThis.fetch=async(url,init)=>{{sent={{url,body:JSON.parse(init.body)}};return {{ok:true,json:async()=>({{status:'queued',messages:[]}})}};}};
{_js_function("sendChat")}
sendChat().then(()=>console.log(JSON.stringify(sent)));
"""
        result = subprocess.run([NODE, "-e", program], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        sent = json.loads(result.stdout)
        self.assertEqual(sent["url"], "/api/runs/plain_run/chat")
        # The follow-up keeps the run's model; the launcher's is for new runs.
        self.assertEqual(sent["body"], {"message": "Check the endpoint.", "attachments": []})

    def test_continuation_merge_keeps_previous_route_over_runner_output(self) -> None:
        merged = jobs._merge_continuation_run(
            {
                "run_id": "route-lineage",
                "auth_route": "claude_subscription",
                "live": {"auth_mode": "claude_subscription"},
                "agents": [],
                "edges": [],
                "totals": {},
            },
            {
                "run_id": "route-lineage",
                "auth_route": "api_key",
                "live": {"auth_mode": "api_key"},
                "agents": [],
                "edges": [],
                "totals": {},
            },
            job_id="continue-1",
        )
        self.assertEqual(merged["auth_route"], "claude_subscription")
        self.assertEqual(merged["live"]["auth_mode"], "claude_subscription")


class ClaudeMonitorOverviewTests(unittest.TestCase):
    def test_overview_reports_both_accounts_and_selected_subscription(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "harness"
            cache.mkdir(parents=True)
            (cache / "manifest.json").write_text(json.dumps({"runs": []}), encoding="utf-8")
            with (
                patch.object(monitor_overview, "CACHE_DIR", Path(tmp)),
                patch("agent_monitor.library.get_library", return_value={"items": []}),
                patch(
                    "agent_monitor.agent_config.get_agent_config",
                    return_value={"memory": [], "skills": [], "system_prompt": ""},
                ),
                patch(
                    "agent_monitor.settings.get_settings",
                    return_value={
                        "account_runtime": "claude",
                        "codex_connected": True,
                        "claude_connected": True,
                        "configured_providers": ["anthropic"],
                    },
                ),
                patch("agent_monitor.engines_registry.list_engines", return_value=[]),
            ):
                result = monitor_overview.build_overview({"id": 17, "is_admin": False})

        self.assertEqual(result["auth"]["account_runtime"], "claude")
        self.assertTrue(result["auth"]["codex_connected"])
        self.assertTrue(result["auth"]["claude_connected"])
        self.assertTrue(result["auth"]["subscription_connected"])

    def test_monitor_card_renders_selected_runtime_not_codex_only(self) -> None:
        html = Path("agent_monitor/web/console.html").read_text(encoding="utf-8")
        self.assertIn("auth.account_runtime||'api'", html)
        self.assertIn("runtime==='claude'?'Claude Code'", html)
        self.assertNotIn("MONITOR.auth&&MONITOR.auth.codex_connected", html)


if __name__ == "__main__":
    unittest.main()
