from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_monitor import jobs, lean_verify, proof_graph, run_tuning
from agent_monitor.runners import api_backend


HTML = (
    Path(__file__).parents[1] / "agent_monitor" / "web" / "console.html"
).read_text(encoding="utf-8")
SCRIPT = HTML.rsplit("<script>", 1)[1].split("</script>", 1)[0]
NODE = shutil.which("node") or "/home/ubuntu/.local/node24/bin/node"


class RunTuningContractTests(unittest.TestCase):
    def test_codex_sol_exposes_real_fast_and_ultra_values(self) -> None:
        caps = run_tuning.capabilities(
            "gpt-5.6-sol", "codex_subscription", "codex"
        )
        self.assertEqual(
            [item["id"] for item in caps["reasoning"]],
            ["default", "low", "medium", "high", "xhigh", "max"],
        )
        self.assertEqual(
            [item["id"] for item in caps["speed"]], ["standard", "fast"]
        )
        self.assertEqual(caps["reasoning"][-1]["label"], "Ultra")

    def test_claude_code_and_openai_api_expose_only_real_controls(self) -> None:
        claude = run_tuning.capabilities("sonnet", "claude_subscription", "plain")
        openai = run_tuning.capabilities("gpt-5.6-sol", "api_key", "plain")
        self.assertEqual(
            [item["id"] for item in claude["reasoning"]],
            ["default", "low", "medium", "high", "xhigh", "max"],
        )
        self.assertEqual([item["id"] for item in claude["speed"]], ["standard"])
        self.assertIn("deepagents", claude["supported_engines"])
        self.assertEqual(
            [item["id"] for item in openai["speed"]], ["standard", "fast"]
        )

    def test_capabilities_fail_closed_for_unattested_harness_and_sponsor(self) -> None:
        unsupported = run_tuning.capabilities(
            "gpt-5.6-sol", "api_key", "openclaude"
        )
        sponsored = run_tuning.capabilities("kimi-k3", "sponsored_kimi", "plain")
        self.assertEqual([item["id"] for item in unsupported["reasoning"]], ["default"])
        self.assertEqual([item["id"] for item in unsupported["speed"]], ["standard"])
        self.assertEqual([item["id"] for item in sponsored["reasoning"]], ["default"])

    def test_invalid_explicit_profile_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "reasoning effort"):
            run_tuning.normalize(
                "kimi-k3", "api_key", reasoning_effort="xhigh", engine="plain"
            )
        with self.assertRaisesRegex(ValueError, "speed mode"):
            run_tuning.normalize(
                "kimi-k3", "api_key", speed_mode="fast", engine="plain"
            )

    def test_environment_projection_is_provider_specific(self) -> None:
        kimi_env: dict[str, str] = {}
        run_tuning.apply_environment(
            kimi_env,
            model="kimi-k3",
            auth_route="api_key",
            reasoning_effort="max",
            speed_mode="standard",
        )
        self.assertEqual(kimi_env["KIMI_REASONING_EFFORT"], "max")
        self.assertNotIn("service_tier", kimi_env)

        claude_env: dict[str, str] = {}
        run_tuning.apply_environment(
            claude_env,
            model="sonnet",
            auth_route="claude_subscription",
            reasoning_effort="high",
            speed_mode="standard",
        )
        self.assertEqual(claude_env["AGENT_MONITOR_CLAUDE_EFFORT"], "high")

    def test_openai_api_payload_receives_reasoning_and_fast(self) -> None:
        captured: dict = {}

        def fake_http(_url, **kwargs):
            captured.update(kwargs["payload"])
            return 200, {"output_text": "done", "usage": {}}

        env = {
            "OPENAI_API_KEY": "test-only-key",
            run_tuning.REASONING_ENV: "max",
            run_tuning.SPEED_ENV: "fast",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch("agent_monitor.settings._http_json", side_effect=fake_http),
        ):
            result = api_backend.api_chat("system", "user", model="gpt-5.6-sol")
        self.assertEqual(result.text, "done")
        self.assertEqual(captured["reasoning"], {"effort": "max"})
        self.assertEqual(captured["service_tier"], "fast")

    def test_dag_openai_payload_receives_same_profile(self) -> None:
        captured: dict = {}

        def fake_http(_url, **kwargs):
            captured.update(kwargs["payload"])
            return 200, {"output_text": "{}"}

        env = {
            "OPENAI_API_KEY": "test-only-key",
            run_tuning.REASONING_ENV: "high",
            run_tuning.SPEED_ENV: "standard",
        }
        with patch("agent_monitor.settings._http_json", side_effect=fake_http):
            model, content, provider = proof_graph._call_llm(
                env, "gpt-5.6-sol", "graph"
            )
        self.assertEqual((model, content, provider), ("gpt-5.6-sol", "{}", "openai"))
        self.assertEqual(captured["reasoning"], {"effort": "high"})
        self.assertEqual(captured["service_tier"], "default")

    def test_codex_cli_config_is_exact_and_kimi_remains_proxy_owned(self) -> None:
        env = {
            run_tuning.REASONING_ENV: "max",
            run_tuning.SPEED_ENV: "fast",
        }
        self.assertEqual(
            run_tuning.codex_config_args(env, "gpt-5.6-sol"),
            [
                "-c",
                'model_reasoning_effort="max"',
                "-c",
                'service_tier="fast"',
            ],
        )
        self.assertEqual(run_tuning.codex_config_args(env, "kimi-k3"), [])

    def test_unsupported_harness_rejects_before_problem_or_workspace(self) -> None:
        engine = {
            "id": "openclaude",
            "label": "OpenClaude",
            "available": True,
            "auth_modes": ["api_key"],
        }
        with (
            patch("agent_monitor.engines_registry.all_engine_ids", return_value={"openclaude"}),
            patch("agent_monitor.engines_registry.list_engines", return_value=[engine]),
            patch("agent_monitor.engines_registry.supported_models", return_value=[]),
            patch.object(jobs, "_user_extra_env", return_value={"OPENAI_API_KEY": "test"}),
            patch.object(jobs, "_selected_auth_route", return_value=("api_key", True)),
            patch.object(jobs, "resolve_problem") as resolve,
            patch.object(jobs, "workspace_dir") as workspace,
        ):
            with self.assertRaisesRegex(ValueError, "speed mode"):
                jobs.start_job(
                    engine="openclaude",
                    problem_text="prove True",
                    model="gpt-5.6-sol",
                    auth_route="api_key",
                    speed_mode="fast",
                )
        resolve.assert_not_called()
        workspace.assert_not_called()

    def test_new_run_persists_and_projects_validated_profile(self) -> None:
        captured_runs: list[dict] = []
        captured_threads: list[dict] = []

        class FakeThread:
            def __init__(self, *, kwargs, **_other):
                captured_threads.append(kwargs)

            def start(self) -> None:
                return None

        engine = {
            "id": "plain",
            "label": "Plain",
            "available": True,
            "auth_modes": ["api_key"],
        }
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("agent_monitor.engines_registry.all_engine_ids", return_value={"plain"}),
                patch("agent_monitor.engines_registry.list_engines", return_value=[engine]),
                patch("agent_monitor.engines_registry.supported_models", return_value=[]),
                patch.object(jobs, "_user_extra_env", return_value={"OPENAI_API_KEY": "test"}),
                patch.object(jobs, "_selected_auth_route", return_value=("api_key", True)),
                patch.object(jobs, "resolve_problem", return_value=("case", "prove True", None)),
                patch.object(jobs, "workspace_dir", return_value=Path(tmp)),
                patch.object(jobs, "_write_run", side_effect=lambda run: captured_runs.append(dict(run))),
                patch.object(jobs, "_JOBS", {}),
                patch.object(jobs.threading, "Thread", FakeThread),
            ):
                job = jobs.start_job(
                    engine="plain",
                    problem_text="prove True",
                    model="gpt-5.6-sol",
                    auth_route="api_key",
                    reasoning_effort="max",
                    speed_mode="fast",
                )
        self.assertEqual(job["reasoning_effort"], "max")
        self.assertEqual(job["speed_mode"], "fast")
        self.assertEqual(captured_runs[0]["reasoning_effort"], "max")
        worker = captured_threads[0]
        self.assertEqual(worker["reasoning_effort"], "max")
        self.assertEqual(worker["speed_mode"], "fast")
        self.assertEqual(
            worker["extra_env"][run_tuning.REASONING_ENV], "max"
        )

    def test_manual_formal_codex_argv_carries_profile(self) -> None:
        env = {
            run_tuning.REASONING_ENV: "xhigh",
            run_tuning.SPEED_ENV: "fast",
        }
        argv = lean_verify._selected_harness_argv(
            engine="codex",
            model="gpt-5.6-sol",
            prompt="formalize",
            default=["codex", "exec", "formalize"],
            env=env,
        )
        prompt_index = argv.index("formalize")
        self.assertEqual(argv[argv.index("--model") + 1], "gpt-5.6-sol")
        self.assertLess(argv.index('model_reasoning_effort="xhigh"'), prompt_index)
        self.assertLess(argv.index('service_tier="fast"'), prompt_index)

    def test_continuation_and_formal_environment_keep_recorded_profile(self) -> None:
        record = {
            "run_id": "plain_case",
            "owner_id": 7,
            "auth_route": "api_key",
            "model": "kimi-k3",
            "credential_source": "user",
            "reasoning_effort": "max",
            "speed_mode": "standard",
        }
        engine = {"id": "plain", "auth_modes": ["api_key"]}
        with patch("agent_monitor.engines_registry.list_engines", return_value=[engine]):
            continued, route = jobs._continuation_auth_environment(
                run_data=record,
                engine="plain",
                model="kimi-k3",
                user=None,
                env={"KIMI_API_KEY": "test"},
            )
        self.assertEqual(route, "api_key")
        self.assertEqual(continued["KIMI_REASONING_EFFORT"], "max")

        with patch("agent_monitor.settings.resolved_user_env", return_value={"KIMI_API_KEY": "test"}):
            formal = lean_verify._llm_environment(None, record)
        self.assertEqual(formal["KIMI_REASONING_EFFORT"], "max")
        self.assertEqual(formal[run_tuning.REASONING_ENV], "max")


class RunTuningUiTests(unittest.TestCase):
    def test_run_mode_replaces_subagents_in_the_prompt_dock(self) -> None:
        # Speed is a two-state toggle (standard/fast) in the redesigned dock;
        # thinking effort keeps its slider.
        for identifier in (
            "run-profile-trigger",
            "run-profile-popover",
            "run-speed-toggle",
            "run-thinking-slider",
            "run-thinking-stage",
        ):
            self.assertEqual(HTML.count(f'id="{identifier}"'), 1)
        self.assertNotIn('id="run-speed-slider"', HTML)
        self.assertIn(
            '<button type="button" class="speed-button" id="run-speed-toggle" aria-pressed="false">',
            HTML,
        )
        self.assertIn("speed.disabled=speeds.length<=1;", HTML)
        self.assertIn("speed.setAttribute('aria-pressed',String(fast));", HTML)
        self.assertIn("if(choices.length<2)return;", HTML)
        dock = HTML.split('<div class="choice-dock">', 1)[1].split(
            '<button class="start"', 1
        )[0]
        # The sidebar gained an id for the mobile drawer; match its opening tag.
        sidebar = HTML.split('<aside class="sidebar"', 1)[1].split('</aside>', 1)[0]
        self.assertIn('<details class="run-profile choice-field"', dock)
        self.assertIn('<summary class="run-profile-trigger"', dock)
        details = dock.split('<details class="run-profile choice-field"', 1)[1]
        self.assertLess(details.index("<summary"), details.index("run-profile-popover"))
        self.assertNotIn('<span class="choice-label">Speed / Thinking</span>', details)
        self.assertNotIn('id="run-profile"', sidebar)
        self.assertNotIn('id="subagents-check"', HTML)
        self.assertNotIn('id="subagent-model-select"', HTML)
        self.assertIn("SETTINGS?.run_tuning_by_runtime?.[runtime]?.[model]", HTML)
        self.assertIn("reasoning_effort:tuning.reasoning_effort", HTML)
        self.assertIn("speed_mode:tuning.speed_mode", HTML)
        self.assertIn("use_subagents:false", HTML)
        self.assertIn("subagent_model:null", HTML)
        self.assertIn(
            "existing runs, Formal Lean, Lean DAG, and continuations keep the profile they started with",
            HTML,
        )

    def test_run_mode_motion_is_helpful_and_respects_reduced_motion(self) -> None:
        self.assertIn("runProfilePulse", HTML)
        self.assertIn("runProfileRevealHalf", HTML)
        self.assertIn("runSliderPulse", HTML)
        self.assertIn("@keyframes controlShimmer", HTML)
        self.assertIn(".slider-burst .run-slider-fill::after { animation:controlShimmer", HTML)
        self.assertIn("@keyframes tuningPop", HTML)
        self.assertIn("height: 13px", HTML)
        self.assertIn("--slider-progress", HTML)
        self.assertNotIn("calc(var(--slider-progress)", HTML)
        self.assertIn("width: 200%", HTML)
        self.assertIn("transform: translateY(0) scale(.5)", HTML)
        self.assertNotIn("animation: runProfileReveal .36s", HTML)
        self.assertIn(".run-profile-field-head b { font-size: 25px", HTML)
        # The redesigned popover is moved to <body> and fixed-positioned, so
        # a clipped or transformed toolbar ancestor can never hide it.
        self.assertIn("document.body.appendChild($('run-profile-popover'))", HTML)
        self.assertIn(".run-profile-popover { position:fixed;", HTML)
        self.assertNotIn('class="run-profile-head"', HTML)
        self.assertNotIn('class="run-profile-hero"', HTML)
        self.assertIn('class="run-slider-fill"', HTML)
        self.assertIn("run-mode-slider thinking-slider", HTML)
        self.assertNotIn("run-mode-slider speed-slider", HTML)
        self.assertNotIn("speed-options", HTML)
        self.assertNotIn("function toggleRunProfile", HTML)
        self.assertIn("$('run-profile').ontoggle", HTML)
        self.assertIn("@media (prefers-reduced-motion: reduce)", HTML)
        reduced = HTML.split("@media(prefers-reduced-motion:reduce) {", 1)[1].split("}", 1)[0]
        for selector in (
            ".run-profile-popover",
            ".run-profile-bolt::after",
            ".run-slider-fill::after",
            ".thinking-thumb",
            ".tuning-pop",
            ".speed-button",
            ".speed-bolt",
        ):
            self.assertIn(selector, reduced)
        self.assertIn("animation:none!important; transition:none!important;", reduced)

    @unittest.skipUnless(Path(NODE).is_file(), "node is unavailable")
    def test_full_inline_javascript_parses(self) -> None:
        result = subprocess.run(
            [NODE, "-e", "new Function(require('fs').readFileSync(0,'utf8'));"],
            input=SCRIPT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_user_secret_was_not_copied_into_touched_sources(self) -> None:
        for path in (
            Path("agent_monitor/run_tuning.py"),
            Path("agent_monitor/web/console.html"),
            Path("tests/test_run_tuning.py"),
        ):
            self.assertNotIn("sk" + "-proj-", path.read_text(encoding="utf-8"))


def _ids(caps: dict, field: str) -> list[str]:
    return [item["id"] for item in caps[field]]


class Gpt6RunTuningTests(unittest.TestCase):
    """GPT-6 controls follow the OpenAI model pages and the vetted Codex catalog."""

    def test_api_models_expose_their_documented_thinking_and_speed(self) -> None:
        full = ["default", "low", "medium", "high", "xhigh", "max"]
        with_none = ["default", "none", "low", "medium", "high", "xhigh", "max"]
        cases = {
            "gpt-6-astra": (full, ["standard", "fast", "ultrafast"]),
            "gpt-6.1-sol": (full, ["standard", "fast"]),
            "gpt-6-sol": (with_none, ["standard", "fast"]),
            "gpt-6-luna": (with_none, ["standard", "fast"]),
        }
        for model, (reasoning, speed) in cases.items():
            for engine in ("plain", "math_harness", "metaharness", "hermes"):
                with self.subTest(model=model, engine=engine):
                    caps = run_tuning.capabilities(model, "api_key", engine)
                    self.assertEqual(_ids(caps, "reasoning"), reasoning)
                    self.assertEqual(_ids(caps, "speed"), speed)

    def test_codex_transport_keeps_the_vetted_cli_catalog(self) -> None:
        for route in ("api_key", "codex_subscription"):
            with self.subTest(route=route):
                astra = run_tuning.capabilities("gpt-6-astra", route, "codex")
                sol = run_tuning.capabilities("gpt-6-sol", route, "codex")
                newer = run_tuning.capabilities("gpt-6.1-sol", route, "codex")
                self.assertEqual(_ids(astra, "speed"), ["standard", "fast"])
                self.assertNotIn("none", _ids(sol, "reasoning"))
                # GPT-6.1 Sol ships in the pinned codex-cli 0.160.0 catalog.
                self.assertEqual(
                    _ids(newer, "reasoning"), ["default", "low", "medium", "high", "xhigh", "max"]
                )
                self.assertEqual(_ids(newer, "speed"), ["standard", "fast"])
        self.assertEqual(
            run_tuning.codex_config_args(
                {run_tuning.REASONING_ENV: "high", run_tuning.SPEED_ENV: "fast"}, "gpt-6.1-sol"
            ),
            ["-c", 'model_reasoning_effort="high"', "-c", 'service_tier="fast"'],
        )
        luna = run_tuning.capabilities("gpt-6-luna", "codex_subscription", "deepagents")
        self.assertEqual(_ids(luna, "speed"), ["standard", "fast"])

    def test_deepagents_api_route_does_not_advertise_openai_controls(self) -> None:
        caps = run_tuning.capabilities("gpt-6-astra", "api_key", "deepagents")
        self.assertEqual(_ids(caps, "reasoning"), ["default"])
        self.assertEqual(_ids(caps, "speed"), ["standard"])
        self.assertNotIn("deepagents", caps["supported_engines"])

    def test_settings_payload_names_the_harnesses_of_partial_options(self) -> None:
        astra = run_tuning.capabilities("gpt-6-astra", "api_key")
        sol = run_tuning.capabilities("gpt-6-sol", "api_key")
        # Ansatz Analyze and Ansatz Readability apply a profile exactly as math_harness does.
        responses = ["analyze", "hermes", "math_harness", "metaharness", "plain", "readability"]
        speeds = {item["id"]: item for item in astra["speed"]}
        none = next(item for item in sol["reasoning"] if item["id"] == "none")
        self.assertEqual(speeds["ultrafast"]["engines"], responses)
        self.assertNotIn("engines", speeds["fast"])
        self.assertEqual(none["engines"], responses)
        self.assertIn("codex", astra["supported_engines"])

    def test_harness_specific_options_are_validated_per_harness(self) -> None:
        self.assertEqual(
            run_tuning.normalize(
                "gpt-6-astra", "api_key",
                reasoning_effort="max", speed_mode="ultrafast", engine="plain",
            ),
            {"reasoning_effort": "max", "speed_mode": "ultrafast"},
        )
        with self.assertRaisesRegex(ValueError, "speed mode 'ultrafast'"):
            run_tuning.normalize("gpt-6-astra", "api_key", speed_mode="ultrafast", engine="codex")
        with self.assertRaisesRegex(ValueError, "reasoning effort 'none'"):
            run_tuning.normalize(
                "gpt-6-sol", "codex_subscription", reasoning_effort="none", engine="plain"
            )

    def test_ultrafast_and_none_reach_the_responses_request(self) -> None:
        captured: dict = {}

        def fake_http(_url, **kwargs):
            captured.update(kwargs["payload"])
            return 200, {"output_text": "done", "usage": {}}

        for model, effort, speed, tier in (
            ("gpt-6-astra", "max", "ultrafast", "ultrafast"),
            ("gpt-6-sol", "none", "fast", "fast"),
            ("gpt-6.1-sol", "xhigh", "standard", "default"),
        ):
            captured.clear()
            env = {
                "OPENAI_API_KEY": "test-only-key",
                run_tuning.REASONING_ENV: effort,
                run_tuning.SPEED_ENV: speed,
            }
            with (
                self.subTest(model=model),
                patch.dict(os.environ, env, clear=True),
                patch("agent_monitor.settings._http_json", side_effect=fake_http),
            ):
                api_backend.api_chat("system", "user", model=model)
                self.assertEqual(captured["reasoning"], {"effort": effort})
                self.assertEqual(captured["service_tier"], tier)

    def test_codex_cli_never_receives_api_only_values(self) -> None:
        api_only = {run_tuning.REASONING_ENV: "none", run_tuning.SPEED_ENV: "ultrafast"}
        self.assertEqual(run_tuning.codex_config_args(api_only, "gpt-6-astra"), [])
        self.assertEqual(
            run_tuning.codex_config_args(
                {run_tuning.REASONING_ENV: "high", run_tuning.SPEED_ENV: "fast"}, "gpt-6-luna"
            ),
            ["-c", 'model_reasoning_effort="high"', "-c", 'service_tier="fast"'],
        )

    def test_hermes_receives_the_selected_service_tier(self) -> None:
        from agent_monitor.runners import hermes as hermes_runner

        class FakeAgent:
            model = "gpt-6-astra"
            session_input_tokens = 1
            session_output_tokens = 1
            session_estimated_cost_usd = 0.0

            def run_conversation(self, prompt):
                return {
                    "completed": True,
                    "final_response": "# Proof\n\nDone.",
                    "messages": [],
                    "input_tokens": 1,
                    "output_tokens": 1,
                }

        captured: dict = {}

        def create_agent(**kwargs):
            captured.update(kwargs)
            return FakeAgent()

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(hermes_runner, "create_agent", side_effect=create_agent),
                patch.object(hermes_runner, "close_agent"),
                patch.object(jobs, "_write_run"),
                patch.object(jobs, "_attach_analysis", side_effect=lambda run, _ws: run),
            ):
                jobs._run_hermes(
                    run_id="hermes-ultrafast",
                    problem_id="tier",
                    problem_text="Prove it.",
                    model="gpt-6-astra",
                    max_iterations=1,
                    started=time.time(),
                    workspace=Path(tmp),
                    extra_env={
                        "OPENAI_API_KEY": "test-only-key",
                        run_tuning.REASONING_ENV: "max",
                        run_tuning.SPEED_ENV: "ultrafast",
                    },
                    use_subagents=False,
                )
        self.assertEqual(captured["service_tier"], "ultrafast")
        self.assertEqual(captured["reasoning_effort"], "max")


def _js_function(name: str) -> str:
    start = SCRIPT.index(f"function {name}(")
    opening = SCRIPT.index("{", start)
    depth = 0
    for index in range(opening, len(SCRIPT)):
        if SCRIPT[index] == "{":
            depth += 1
        elif SCRIPT[index] == "}":
            depth -= 1
            if depth == 0:
                return SCRIPT[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {name}")


@unittest.skipUnless(Path(NODE).is_file(), "node is unavailable")
class Gpt6RunModeUiTests(unittest.TestCase):
    def test_run_mode_offers_each_harness_its_own_speeds(self) -> None:
        caps = run_tuning.capabilities("gpt-6-astra", "api_key")
        source = "\n".join(
            _js_function(name)
            for name in (
                "runTuningCapabilities",
                "runTuningStorage",
                "runTuningStorageKey",
                "currentRunTuning",
                "renderRunTuning",
            )
        )
        program = f"""
const RUN_TUNING_KEY='tuning';
let store={{}};
const localStorage={{getItem:()=>JSON.stringify(store),setItem(){{}}}};
const els={{}};
const $=id=>els[id]||(els[id]={{id,textContent:'',innerHTML:'',title:'',attrs:{{}},style:{{setProperty(){{}}}},classList:{{toggle(){{}},add(){{}},remove(){{}}}},setAttribute(name,value){{this.attrs[name]=value;}}}});
const selectedAccountRuntime=()=>'api', engineModelMode=()=>'api_key', currentModel=()=>'gpt-6-astra', animateRunTuningFeedback=()=>{{}};
let maxSelected=false;
const isCgmMax=()=>maxSelected, runAuthRoute=()=>maxSelected?'dual_subscription':'api_key';
let engine='plain';
const SETTINGS={{run_tuning_by_runtime:{{api:{{'gpt-6-astra':{json.dumps(caps)}}}}}}};
{source}
const ids=items=>items.map(item=>item.id), out=[];
out.push(ids(runTuningCapabilities().speed));
engine='codex'; out.push(ids(runTuningCapabilities().speed));
engine='deepagents'; out.push(!!runTuningCapabilities().unsupported_engine);
engine='plain';
store={{'api_key:gpt-6-astra':{{reasoning_effort:'high',speed_mode:'fast'}}}};
renderRunTuning(); out.push($('run-speed-toggle').title,$('run-profile-summary').textContent);
store={{'api_key:gpt-6-astra':{{reasoning_effort:'high',speed_mode:'ultrafast'}}}};
renderRunTuning(); out.push($('run-speed-toggle').title,$('run-profile-summary').textContent,$('run-speed-toggle').attrs['aria-label']);
engine='codex'; renderRunTuning(); out.push($('run-speed-label').textContent);
engine='math_harness'; maxSelected=true; renderRunTuning();
out.push($('run-profile-summary').textContent,$('run-profile-note').textContent,!!$('run-speed-toggle').disabled,!!$('run-thinking-slider').disabled);
console.log(JSON.stringify(out));
"""
        result = subprocess.run([NODE, "-e", program], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            [
                ["standard", "fast", "ultrafast"],
                ["standard", "fast"],
                True,
                "Use ultrafast speed",
                "High · Fast",
                "Use standard speed",
                "High · Ultrafast",
                "Speed: Ultrafast",
                "Standard",
                # Max: fixed at maximum effort, nothing to adjust.
                "Ultra",
                "Max runs Claude Code and Codex at maximum effort. "
                "Formal Lean and Lean DAG run on Codex at High.",
                True,
                True,
            ],
        )

    def test_profile_note_states_the_effort_of_derived_codex_work(self) -> None:
        # cgm_max.formal_view derives a Codex-subscription run's Formal Lean
        # and Lean DAG at no more than high effort (a fixed per-call limit), so
        # the note never promises them an Extra high or Ultra profile.
        from agent_monitor import cgm_max

        caps = {route: run_tuning.capabilities("gpt-6-sol", route) for route in ("codex_subscription", "api_key")}
        source = "\n".join(
            _js_function(name)
            for name in (
                "runTuningCapabilities",
                "runTuningStorage",
                "runTuningStorageKey",
                "currentRunTuning",
                "renderRunTuning",
            )
        )
        cases = [("codex_subscription", effort) for effort in ("high", "xhigh", "max")] + [("api_key", "max")]
        program = f"""
const RUN_TUNING_KEY='tuning';
let store={{}};
const localStorage={{getItem:()=>JSON.stringify(store),setItem(){{}}}};
const els={{}};
const $=id=>els[id]||(els[id]={{id,textContent:'',innerHTML:'',title:'',attrs:{{}},style:{{setProperty(){{}}}},classList:{{toggle(){{}},add(){{}},remove(){{}}}},setAttribute(name,value){{this.attrs[name]=value;}}}});
let route='api_key';
const selectedAccountRuntime=()=>route==='codex_subscription'?'codex':'api', currentModel=()=>'gpt-6-sol', animateRunTuningFeedback=()=>{{}};
const isCgmMax=()=>false, runAuthRoute=()=>route;
const engine='plain';
const SETTINGS={{run_tuning_by_runtime:{{codex:{{'gpt-6-sol':{json.dumps(caps["codex_subscription"])}}},api:{{'gpt-6-sol':{json.dumps(caps["api_key"])}}}}}}};
{source}
const out=[];
for(const [selected,effort] of {json.dumps(cases)}){{
  route=selected; store={{[selected+':gpt-6-sol']:{{reasoning_effort:effort,speed_mode:'fast'}}}};
  renderRunTuning(); out.push([$('run-profile-summary').textContent,$('run-profile-note').textContent]);
}}
console.log(JSON.stringify(out));
"""
        result = subprocess.run([NODE, "-e", program], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        kept = (
            "Saved for this model. Applies to new runs; existing runs, Formal Lean, "
            "Lean DAG, and continuations keep the profile they started with."
        )
        capped = (
            "Saved for this model. Applies to new runs; existing runs and continuations "
            "keep the profile they started with. Formal Lean and Lean DAG run at High, "
            "the most their Codex calls allow."
        )
        for (route, effort), (summary, note) in zip(cases, json.loads(result.stdout), strict=True):
            derived = cgm_max.formal_view({"auth_route": route, "reasoning_effort": effort})
            self.assertIn(" · Fast", summary)
            self.assertEqual(note, kept if derived["reasoning_effort"] == effort else capped, (route, effort))
        self.assertEqual(
            [note for _summary, note in json.loads(result.stdout)], [kept, capped, capped, kept]
        )


if __name__ == "__main__":
    unittest.main()
