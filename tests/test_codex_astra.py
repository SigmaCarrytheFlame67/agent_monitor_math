"""Regression coverage for selecting hidden Codex catalog models."""

from __future__ import annotations

import sys
from pathlib import Path

from agent_monitor import engines_registry, settings

HERMES_CORE = Path(__file__).resolve().parents[1] / "engines" / "hermes_core"
if str(HERMES_CORE) not in sys.path:
    sys.path.insert(0, str(HERMES_CORE))

from hermes_cli.codex_models import (  # noqa: E402
    _add_forward_compat_models,
    get_codex_model_ids,
)


def test_astra_is_available_in_openai_model_presets() -> None:
    openai = next(provider for provider in settings.PROVIDERS if provider["id"] == "openai")

    assert "gpt-6-astra" in openai["models"]


def test_openai_presets_offer_the_gpt6_family_once_without_dead_ids() -> None:
    openai = next(provider for provider in settings.PROVIDERS if provider["id"] == "openai")
    models = openai["models"]

    assert {"gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna"} <= set(models)
    assert len(models) == len(set(models))
    # OpenAI answers model_not_found for the bare "gpt-5.6" id.
    assert "gpt-5.6" not in models


def test_hermes_model_picker_fallback_includes_astra(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    assert get_codex_model_ids()[0] == "gpt-6-astra"


def test_hidden_astra_is_synthesized_from_visible_codex_models() -> None:
    models = _add_forward_compat_models(["gpt-5.6-sol"])

    assert "gpt-6-astra" in models


def test_codex_command_passes_selected_model_explicitly(
    monkeypatch, tmp_path: Path
) -> None:
    spec = engines_registry.CLI_ENGINES["codex"]
    monkeypatch.setattr(
        engines_registry,
        "_cli_available",
        lambda candidate: (True, candidate["default_cmd"]),
    )

    argv = engines_registry.build_cli_command(
        "codex",
        prompt="prove it",
        workspace=tmp_path,
        problem_file=tmp_path / "problem.txt",
        model="gpt-6-astra",
    )

    assert argv is not None
    model_index = argv.index("--model")
    assert argv[model_index + 1] == "gpt-6-astra"
    # The prompt goes on stdin ("-"), never in argv.
    assert argv[-1] == "-" and "prove it" not in argv
    assert spec["default_cmd"].count("{model_args}") == 1


def test_codex_command_omits_model_flag_without_selection(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        engines_registry,
        "_cli_available",
        lambda candidate: (True, candidate["default_cmd"]),
    )

    argv = engines_registry.build_cli_command(
        "codex",
        prompt="prove it",
        workspace=tmp_path,
        problem_file=tmp_path / "problem.txt",
    )

    assert argv is not None
    assert "--model" not in argv
    assert argv[-1] == "-" and "prove it" not in argv


def test_latest_chat_alias_and_reasoning_models_are_routed_correctly(monkeypatch, tmp_path):
    from agent_monitor.runners import plain_runner
    from agent_monitor.subprocess_env import project_provider_env
    openai = next(p for p in settings.PROVIDERS if p["id"] == "openai")
    assert {"gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "chat-latest"} <= set(openai["models"])
    projected = project_provider_env({"OPENAI_API_KEY": "own-key", "KIMI_API_KEY": "other-key"}, ("chat-latest",))
    assert projected == {"OPENAI_API_KEY": "own-key"}
    # Plain reaches providers through api_backend's HTTP transport, so the
    # routed request is captured at that boundary instead of an SDK client.
    requests = []

    def fake_http_json(url, *, method, headers, payload, timeout):
        requests.append({"url": url, "headers": headers, "payload": payload})
        return 200, {
            "model": payload["model"],
            "output_text": "# Proof\n\nA proof.",
            "usage": {"input_tokens": 7, "output_tokens": 8},
        }

    monkeypatch.setattr(settings, "_http_json", fake_http_json)
    for name in (
        "AGENT_MONITOR_CODEX_SUBSCRIPTION",
        "AGENT_MONITOR_CLAUDE_SUBSCRIPTION",
        "AGENT_MONITOR_MAX_OUTPUT_TOKENS",
        "AGENT_MONITOR_API_MAX_TOKENS",
        "OPENAI_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "own-key")
    monkeypatch.setenv("AGENT_MONITOR_PLAIN_MAX_OUTPUT_TOKENS", "1024")
    monkeypatch.setattr(sys, "argv", ["plain_runner.py", "Prove it."])
    monkeypatch.chdir(tmp_path)
    for model in ("gpt-6-astra", "chat-latest"):
        monkeypatch.setenv("PLAIN_MODEL", model)
        assert plain_runner.main() == 0
        request = requests[-1]
        # Reasoning models and the latest-chat alias both use OpenAI's
        # Responses API with its native output-token ceiling.
        assert request["url"] == "https://api.openai.com/v1/responses"
        assert request["headers"]["Authorization"] == "Bearer own-key"
        assert request["payload"]["model"] == model
        assert request["payload"]["max_output_tokens"] == 1024
        assert "messages" not in request["payload"]
        assert (tmp_path / "proof.md").read_text().startswith("# Proof\n\nA proof.")
