"""Engine-stage cost is priced at the model that served each call.

Regressions for the 2026-10-26 accounting check:

* The CGM runners' Codex-style usage events named no model, so every engine
  stage (math_harness, kimi, Analyze, Readability) was priced at
  ``_estimate_cost``'s gpt-4o fallback while derived DAG/Lean calls on the
  same model used its real price (gpt-6-astra about 4-5x higher).
* The run totals had no ``reasoning_tokens``, so the usage ledger's engine
  rows always booked 0 reasoning.
* ``kimi-k3`` has no bare LiteLLM key and was priced by the lookup's family
  match: the first ``*kimi-k3`` key in the table's order.

Prices come from a fake LiteLLM table: nothing here touches the network.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_monitor import cgm_max, jobs, usage_ledger
from agent_monitor.cli_events import CLIEventParser
from agent_monitor.paths import ensure_import_paths
from agent_monitor.runners import math_harness_runner as runner
from tests.test_math_harness_runner import PROBLEM, _accepted_outputs, _in_directory

ensure_import_paths()

from llm_cost_tracker import pricing  # noqa: E402  (monitor_core is on the path now)


def _entry(inp: float, out: float, cache_read: float, cache_write: float | None = None) -> dict:
    """A LiteLLM-shaped entry from per-million prices."""
    entry = {
        "input_cost_per_token": inp / 1e6,
        "output_cost_per_token": out / 1e6,
        "cache_read_input_token_cost": cache_read / 1e6,
    }
    if cache_write is not None:
        entry["cache_creation_input_token_cost"] = cache_write / 1e6
    return entry


# Decoys first: the lookup's family match returns the first key, in the
# table's order, that shares a name's family.
_PRICES = {
    "anthropic.claude-3-opus-20240229-v1:0": _entry(15, 75, 1.5),
    "azure/gpt-5.1-codex-max": _entry(1.25, 10, 0.125),
    "databricks/databricks-kimi-k3": _entry(9, 99, 0.9),
    "databricks/system.ai.kimi-k3": {"input_cost_per_token": None, "output_cost_per_token": None},
    "moonshot/kimi-k3": _entry(3, 15, 0.3),
    "gpt-4o": _entry(2.5, 10, 1.25),
    "gpt-6-astra": _entry(10, 50, 1, 12.5),
    "claude-opus-5-5": _entry(4, 20, 0.2, 5),
    "claude-fable-5-1": _entry(10, 50, 0.25, 12.5),
}
# One API call's usage (Codex semantics: the cached part is inside the input).
_CALL_USAGE = {
    "input_tokens": 1200,
    "cached_input_tokens": 1000,
    "output_tokens": 300,
    "reasoning_output_tokens": 80,
}
_PROOF = "# Proof\n\nA sufficiently long bounded proof artifact for this runner.\n"


@pytest.fixture()
def prices(monkeypatch):
    monkeypatch.setattr(pricing, "_pricing_cache", dict(_PRICES))
    return pricing


def _cost(model: str, *, uncached: int = 0, cached: int = 0, written: int = 0, output: int = 0) -> float:
    entry = _PRICES[model]
    inp = entry["input_cost_per_token"]
    read = entry.get("cache_read_input_token_cost") or inp
    write = entry.get("cache_creation_input_token_cost") or inp
    return uncached * inp + cached * read + written * write + output * entry["output_cost_per_token"]


def _event_cost(event: dict, model: str | None = None) -> float:
    """A usage event priced as the server's parser reads it."""
    usage = event["usage"]
    cached = int(usage.get("cached_input_tokens") or usage.get("cache_read_tokens") or 0)
    written = int(usage.get("cache_write_tokens") or 0)
    total = int(usage.get("input_tokens") or 0)
    if usage.get("input_excludes_cache") is True:
        total += cached + written
    return _cost(
        model or event["model"],
        uncached=max(0, total - cached - written),
        cached=cached,
        written=written,
        output=int(usage.get("output_tokens") or 0),
    )


def _node_cost(node: dict, model: str | None = None) -> float:
    cached = int(node.get("cache_read_tokens") or 0)
    written = int(node.get("cache_write_tokens") or 0)
    return _cost(
        model or node["model"],
        uncached=max(0, int(node.get("input_tokens") or 0) - cached - written),
        cached=cached,
        written=written,
        output=int(node.get("output_tokens") or 0),
    )


def _usage_events(events: list[dict]) -> list[dict]:
    return [event for event in events if event.get("type") == "turn.completed"]


def _priced_nodes(run: dict) -> list[dict]:
    return [a for a in run["agents"] if a.get("input_tokens") or a.get("output_tokens")]


def _cgm_api_events(served: str, *, observed: str | None = None) -> list[dict]:
    """The event stream of an accepted CGM run whose API calls ``served`` answers."""
    replies = _accepted_outputs()
    events: list[dict] = []

    def fake_api(system: str, user: str, *, model: str, **_kwargs):
        return SimpleNamespace(
            text=replies.pop(0),
            usage=dict(_CALL_USAGE),
            model=model,
            provider="kimi" if model.startswith("kimi") else "openai",
            observed_model=observed,
        )

    env = {
        "AGENT_MONITOR_SELECTED_MODEL": served,
        "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "2",
        "AGENT_MONITOR_MAX_ITERATIONS": "20",
        "AGENT_MONITOR_PROBLEM_ID": "cost-case",
        "AGENT_MONITOR_CGM_CONCURRENCY": "1",
    }
    with tempfile.TemporaryDirectory() as raw:
        with (
            _in_directory(Path(raw)),
            patch.dict(runner.os.environ, env, clear=True),
            patch.object(runner.sys, "argv", ["math_harness_runner.py", PROBLEM]),
            patch.object(runner, "codex_subscription_enabled", return_value=False),
            patch.object(runner, "claude_subscription_enabled", return_value=False),
            patch.object(runner, "api_chat", side_effect=fake_api),
            patch.object(runner, "emit", side_effect=events.append),
        ):
            assert runner.main() == 0
    assert not replies
    return events


def _engine_run(
    events: list[dict],
    *,
    engine: str = "math_harness",
    requested_model: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict:
    """Run ``engine`` through ``jobs._run_cli_engine`` on a recorded stream."""
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        workspace = root / "workspace"
        workspace.mkdir()
        stream = root / "stream.jsonl"
        stream.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        command = [
            sys.executable,
            "-c",
            (
                "import sys; from pathlib import Path; "
                f"Path('proof.md').write_text({_PROOF!r}, encoding='utf-8'); "
                "sys.stdout.write(Path(sys.argv[1]).read_text(encoding='utf-8')); sys.stdout.flush()"
            ),
            str(stream),
        ]
        with (
            patch("agent_monitor.engines_registry.build_cli_command", return_value=command),
            patch("agent_monitor.agent_config.persona_preamble", return_value=""),
            patch.object(jobs, "_write_run"),
        ):
            return jobs._run_cli_engine(
                run_id=f"{engine}_cost",
                engine=engine,
                problem_id="cost",
                problem_text=PROBLEM,
                started=time.time(),
                workspace=workspace,
                requested_model=requested_model,
                extra_env=extra_env,
            )


def _without_models(events: list[dict]) -> list[dict]:
    """The same stream as a runner before this fix emitted it."""
    stripped = []
    for event in events:
        event = json.loads(json.dumps(event))
        event.pop("model", None)
        item = event.get("item")
        if isinstance(item, dict):
            item.pop("model", None)
            item.pop("model_source", None)
        stripped.append(event)
    return stripped


# ── pricing lookup ───────────────────────────────────────────────────────


def test_exact_pricing_counts_only_aliases_and_exact_keys(prices):
    for model in ("gpt-6-astra", "gpt-4o", "claude-opus-5-5", "kimi-k3"):
        assert prices.has_exact_pricing(model), model
    # The family match prices this route alias as an unrelated model; a family shorter than
    # four characters ("max", once priced as a Codex-max model) is never matched.
    assert prices.get_token_price("opus") == (15.0, 75.0)
    assert prices.get_token_price("max") == (0.0, 0.0)
    for alias in ("opus", "sonnet", "fable", "max", "", "gpt-6"):
        assert not prices.has_exact_pricing(alias), alias
    assert not prices.has_exact_pricing("databricks/system.ai.kimi-k3")  # listed without prices
    assert not prices.has_exact_pricing(None)  # type: ignore[arg-type]


# The lookup's own strategies over a table shaped like LiteLLM's (decoys first:
# the family match returns the first key, in the table's order, that matches).
_LOOKUP = {
    "claude-opus-5-5": _entry(4, 20, 0.2, 5),
    "anthropic.claude-haiku-4-5-20251001-v1:0": _entry(1, 5, 0.1),
    "azure/gpt-5.1-codex-max": _entry(1.25, 10, 0.125),
    "us.anthropic.claude-3-5-haiku-20241022-v1:0": _entry(0.8, 4, 0.08),
    "claude-3-5-haiku-20241022": _entry(0.9, 4.5, 0.09),
    "anthropic.claude-3-opus-20240229-v1:0": _entry(15, 75, 1.5),
    "azure/gpt-5.2": _entry(1.75, 14, 0.175),
    "openai/gpt-5.5": _entry(5, 30, 0.5),
    "gpt-6.1-sol": _entry(2, 10, 0.2),
    "xai/grok-4.5": _entry(3, 15, 0.75),
    "gpt-4o": _entry(2.5, 10, 1.25),
    # A dated key whose date a letterless family ("2024") would match.
    "gpt-4o-2024-08-06": _entry(7, 21, 0.7),
}


def test_a_dotted_name_is_priced_at_its_own_entry_never_an_unrelated_familys(monkeypatch):
    """The lookup used to split any name at its first dot as if a Bedrock
    provider prefix came before it: "kimi-k2.5" became the slug "5", whose
    family match took the first key with a "5" in it (claude-opus-5-5 here).
    A provider prefix is a plain word ("anthropic.", "meta.", after a region
    such as "us.") before a model whose name starts with a letter, so a
    model family before its version's dot ("qwen3" of "qwen3.5-flash",
    "flux" of "flux.2-pro") is none, and a family shorter than four
    characters or without a letter is never matched.  Every name that
    resolved correctly resolves to the same entry."""
    monkeypatch.setattr(pricing, "_pricing_cache", dict(_LOOKUP))
    expected = {
        # Bedrock names: exact, region and provider prefixes stripped, the bare id, the family, an alias.
        "us.anthropic.claude-3-5-haiku-20241022-v1:0": (0.8, 4.0),
        "eu.anthropic.claude-3-5-haiku-20241022-v1:0": (0.9, 4.5),
        "anthropic.claude-3-5-haiku-20241022-v1:0": (0.9, 4.5),
        "anthropic.claude-3-opus-20240229-v1:0": (15.0, 75.0),
        "anthropic.claude-haiku-4-5-20251001-v2:0": (1.0, 5.0),
        "claude-3-5-haiku-20241022-v1": (0.8, 4.0),
        # Aliases and exact dotted names.
        "gpt-5.2": (1.75, 14.0),
        "gpt-5.5": (5.0, 30.0),
        "gpt-6.1-sol": (2.0, 10.0),
        # A dotted name whose own family is listed.
        "grok-4.5": (3.0, 15.0),
        # Dotted names with no entry of their own, families too short to match, and one without a
        # letter (a provider prefix, then only a number).
        "kimi-k2.5": (0.0, 0.0),
        "kimi:kimi-k2.5": (0.0, 0.0),
        "gemini-3.6-flash": (0.0, 0.0),
        "2.5": (0.0, 0.0),
        "max": (0.0, 0.0),
        "anthropic.2024": (0.0, 0.0),
    }
    assert {name: pricing.get_token_price(name) for name in expected} == expected
    for name in ("kimi-k2.5", "kimi:kimi-k2.5", "gemini-3.6-flash", "2.5", "max", "anthropic.2024"):
        assert pricing._lookup_model(name) is None and not pricing.has_real_pricing(name), name
    # An unpriced model is estimated at gpt-4o's rates, as designed, never at Claude's.
    assert jobs._estimate_cost("kimi-k2.5", input_tokens=1_000_000, output_tokens=1_000_000) == 12.5
    # With an entry of its own family, that one.
    monkeypatch.setitem(pricing._pricing_cache, "moonshot/kimi-k2.5", _entry(0.6, 3, 0.1))
    assert pricing.get_token_price("kimi-k2.5") == (0.6, 3.0)
    # A model family that ends in a digit, or a bare one, before its version's dot was taken
    # for a provider prefix: the number after it ("5-flash", "5-mini", "5-large", "2-pro")
    # found unrelated models (these decoys are keys of LiteLLM's table, listed first).  Each
    # name is priced at its own family's entry, or not at all, and the names above as before.
    monkeypatch.setattr(pricing, "_pricing_cache", {
        "databricks/databricks-gemini-2-5-flash": _entry(0.3, 2.5, 0.03),
        "azure/eu/gpt-5-mini-2025-08-07": _entry(0.275, 2.2, 0.0275),
        "jamba-1.5-large": _entry(2, 8, 0.2),
        "azure/gpt-5.2-pro": _entry(21, 168, 2.1),
        "stability/sd3.5-large": _entry(6.5, 6.5, 0.65),
        "azure_ai/flux.2-pro": _entry(4, 4, 0.4),
        **_LOOKUP,
    })
    families = {"qwen3.5-flash": (0.0, 0.0), "phi3.5-mini": (0.0, 0.0), "sd3.5-large": (6.5, 6.5),
                "flux.2-pro": (4.0, 4.0)}
    assert {name: pricing.get_token_price(name) for name in {**expected, **families}} == {**expected, **families}


def test_an_openrouter_vendor_model_id_is_priced_at_its_model(monkeypatch):
    """A custom OpenRouter id ("vendor/model") that no key contains whole is
    looked up by the model after its last "/", under the same family rules
    (at least four characters, with a letter).  Before, such ids were found
    only by accident of their first dot ("x-ai/grok-4.5" at Claude's rates),
    and then not at all ("meta-llama/llama-3.1-70b-instruct" at gpt-4o's)."""
    monkeypatch.setattr(pricing, "_pricing_cache", {
        **_LOOKUP,
        "oci/meta.llama-3.1-70b-instruct": _entry(0.72, 0.72, 0.07),
        "replicate/anthropic/claude-3.5-sonnet": _entry(3.75, 18.75, 0.375),
    })
    expected = {
        "meta-llama/llama-3.1-70b-instruct": (0.72, 0.72),
        "x-ai/grok-4.5": (3.0, 15.0),
        # A key that contains the whole id still comes first.
        "anthropic/claude-3.5-sonnet": (3.75, 18.75),
        # Too short, without a letter (the dated gpt-4o key), or with no entry of its family.
        "openai/max": (0.0, 0.0),
        "vendor/2024": (0.0, 0.0),
        "moonshotai/kimi-k2.5": (0.0, 0.0),
    }
    assert {name: pricing.get_token_price(name) for name in expected} == expected


def test_the_suite_prices_from_a_fixed_table_and_never_downloads_one(capsys):
    """tests/conftest.py gives the pricing module a fixed table for every
    test, never LiteLLM's live one, and an emptied cache never reaches
    GitHub either: the conftest refuses the download itself (nothing is
    priced), whether or not the network could be reached."""
    import conftest

    assert pricing._fetch_pricing() == conftest.PRICE_TABLE
    assert pricing.get_token_price("claude-opus-5-5") == (4.0, 20.0)
    assert pricing.get_token_price("kimi-k3") == (3.0, 15.0)
    assert pricing.urllib.request.urlopen is conftest._refuse_download
    capsys.readouterr()
    pricing._pricing_cache = None  # the conftest's patch restores it after this test
    assert pricing._fetch_pricing() == {} and pricing.get_token_price("gpt-4o") == (0.0, 0.0)
    assert "(tests never download the live price table (tests/conftest.py))" in capsys.readouterr().out


def test_exact_pricing_is_false_without_a_pricing_table(monkeypatch):
    # A failed LiteLLM fetch caches an empty table for the process.
    monkeypatch.setattr(pricing, "_pricing_cache", {})
    assert not pricing.has_exact_pricing("gpt-6-astra")
    assert jobs._exact_price_model("gpt-6-astra") is None


def test_exact_pricing_follows_the_lookup_when_an_alias_target_is_missing(prices, monkeypatch):
    # As in _lookup_model, an alias whose target the table no longer lists
    # falls through to the name's own exact key.  Without one the name is not
    # exact, even though the family match still prices it (the decoy).
    monkeypatch.setitem(prices._MODEL_ALIASES, "gpt-6-astra", "azure/gpt-6-astra-unlisted")
    assert prices.has_exact_pricing("gpt-6-astra")
    monkeypatch.setitem(prices._MODEL_ALIASES, "kimi-k3", "moonshot/kimi-k3-unlisted")
    assert not prices.has_exact_pricing("kimi-k3")
    assert prices.get_token_price("kimi-k3") == (9.0, 99.0)
    assert jobs._exact_price_model("kimi-k3") is None


def test_kimi_k3_is_priced_at_moonshots_entry_not_the_first_family_key(prices):
    assert prices.get_token_price("kimi-k3") == (3.0, 15.0)
    assert prices.get_cache_pricing("kimi-k3") == (0.3, 3.0)
    assert jobs._estimate_cost("kimi-k3", input_tokens=1_000_000, output_tokens=1_000_000) == 18.0
    # Without the alias the family match took the decoy listed first.
    with patch.dict(prices._MODEL_ALIASES, clear=True):
        assert prices.get_token_price("kimi-k3") == (9.0, 99.0)


def test_exact_price_model_trims_and_refuses_aliases(prices):
    assert jobs._exact_price_model(" gpt-6-astra ") == "gpt-6-astra"
    for model in (None, "", "  ", "opus", "fable", "max", cgm_max.MODEL_ID):
        assert jobs._exact_price_model(model) is None, model


def test_the_launcher_offers_max_apis_claude_models_and_prices_them_at_their_own_entries(prices, monkeypatch):
    """The Anthropic models the console lists for an Anthropic key include
    the two Max (API) runs on (an Analyze or Readability run can name them
    without the custom box), and a run on either is priced at its own
    entry, never a family match or the gpt-4o fallback."""
    from agent_monitor import settings

    anthropic = next(spec for spec in settings.PROVIDERS if spec["id"] == "anthropic")["models"]
    assert anthropic[:2] == ["claude-fable-5-1", "claude-opus-5-5"]
    assert set(cgm_max.API_ADVISOR_MODELS[:2]) <= set(anthropic) and len(set(anthropic)) == len(anthropic)
    # An account with an Anthropic key is offered them.
    monkeypatch.setattr(settings, "resolved_user_env", lambda _user: {"ANTHROPIC_API_KEY": "sk-ant-member-123456789"})
    offered = settings.get_settings({"id": 7, "email": "m@example.test", "is_admin": False})["api_models"]
    assert {"claude-fable-5-1", "claude-opus-5-5"} <= set(offered)
    for model, (inp, out) in (("claude-opus-5-5", (4.0, 20.0)), ("claude-fable-5-1", (10.0, 50.0))):
        assert jobs._exact_price_model(model) == model and jobs._exact_price_model("anthropic:" + model) == model
        assert prices.get_token_price(model) == (inp, out)
        assert jobs._estimate_cost(model, input_tokens=1_000_000, output_tokens=1_000_000) == inp + out


# ── runner: every usage event names the model that served it ─────────────


def test_stage_usage_names_its_model_only_when_given():
    events: list[dict] = []
    runner._emit_stage_usage("worker-1", {"input_tokens": 3}, out=events.append, model=" gpt-6-astra ")
    runner._emit_stage_usage("worker-2", {"input_tokens": 3}, out=events.append)
    runner._emit_stage_usage("worker-3", {"input_tokens": 3}, out=events.append, model="  ")
    assert events[0] == {
        "type": "turn.completed",
        "stage": "worker-1",
        "model": "gpt-6-astra",
        "usage": {"input_tokens": 3, "cached_input_tokens": 0},
    }
    # Without a model the event keeps its earlier shape.
    assert events[1] == {"type": "turn.completed", "stage": "worker-2",
                         "usage": {"input_tokens": 3, "cached_input_tokens": 0}}
    assert "model" not in events[2]


def test_served_model_never_stands_in_a_claude_route_alias():
    api = runner.Route("api", "gpt-6-astra", "api_chat")
    codex = runner.Route("codex_subscription", "gpt-6-astra", "codex_exec")
    claude = runner.Route("claude_subscription", "fable", "claude_code")
    assert runner._served_model(api, "gpt-6-astra-2026-09-03") == "gpt-6-astra-2026-09-03"
    assert runner._served_model(api) == "gpt-6-astra"
    assert runner._served_model(codex) == "gpt-6-astra"
    assert runner._served_model(claude, "claude-fable-5-1") == "claude-fable-5-1"
    assert runner._served_model(claude) is None
    assert runner._served_model(claude, "  ") is None
    assert runner._served_model(api, 7) == "gpt-6-astra"


def test_api_stage_usage_names_the_resolved_model():
    events = _cgm_api_events("gpt-6-astra")
    usage = _usage_events(events)
    assert len(usage) == 6
    assert {event["model"] for event in usage} == {"gpt-6-astra"}
    # No response model was reported: the messages claim no authorship.
    messages = [e["item"] for e in events if (e.get("item") or {}).get("type") == "agent_message"]
    assert not any(item.get("model_source") for item in messages)


def test_api_stage_usage_names_the_requested_id_not_the_replys_snapshot():
    """An OpenAI reply names a dated snapshot of the requested id, which the
    price table may lack: API usage is priced at the requested id (as the
    run's derived DAG and Lean calls are), and no stage message names the
    snapshot, since a node's own model would price its usage instead."""
    events = _cgm_api_events("gpt-6-astra", observed="gpt-6-astra-2026-09-03")
    assert {event["model"] for event in _usage_events(events)} == {"gpt-6-astra"}
    messages = [e["item"] for e in events if (e.get("item") or {}).get("type") == "agent_message"]
    stage_messages = messages[1:-1]  # the pipeline banner and the final report are not calls
    assert len(stage_messages) == 6
    for item in stage_messages:
        assert "model" not in item and "model_source" not in item


def test_codex_exec_stage_usage_names_the_cli_model():
    outputs = _accepted_outputs()
    events: list[dict] = []

    def fake_codex(system: str, user: str, **kwargs):
        # Codex's own events never name a model.
        kwargs["emit_event"]({"type": "item.completed", "item": {"type": "agent_message", "text": "native"}})
        kwargs["emit_event"]({"type": "turn.completed", "usage": dict(_CALL_USAGE)})
        return SimpleNamespace(text=outputs.pop(0), usage=dict(_CALL_USAGE))

    env = {
        "AGENT_MONITOR_CODEX_SUBSCRIPTION": "1",
        "AGENT_MONITOR_CODEX_MODEL": "gpt-6-astra",
        "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "2",
    }
    with tempfile.TemporaryDirectory() as raw:
        with (
            _in_directory(Path(raw)),
            patch.dict(runner.os.environ, env, clear=True),
            patch.object(runner.sys, "argv", ["runner", PROBLEM]),
            patch.object(runner, "codex_subscription_enabled", return_value=True),
            patch.object(runner, "claude_subscription_enabled", return_value=False),
            patch.object(runner, "codex_exec", side_effect=fake_codex),
            patch.object(runner, "emit", side_effect=events.append),
        ):
            assert runner.main() == 0
    usage = _usage_events(events)
    # The CLI's own turn.completed is dropped; each stage reports once.
    assert len(usage) == 6
    assert {event["model"] for event in usage} == {"gpt-6-astra"}


@pytest.mark.parametrize("reported", ["claude-opus-5-5", None])
def test_claude_stage_usage_names_only_what_claude_code_reported(reported):
    outputs = _accepted_outputs()
    events: list[dict] = []

    def fake_claude(system: str, user: str, **_kwargs):
        return SimpleNamespace(
            text=outputs.pop(0),
            usage={"input_tokens": 9, "output_tokens": 5, "cache_read_tokens": 4},
            model=reported,
            events=(),
        )

    env = {
        "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
        "AGENT_MONITOR_CLAUDE_MODEL": "opus",
        "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "2",
    }
    with tempfile.TemporaryDirectory() as raw:
        with (
            _in_directory(Path(raw)),
            patch.dict(runner.os.environ, env, clear=True),
            patch.object(runner.sys, "argv", ["runner", PROBLEM]),
            patch.object(runner, "codex_subscription_enabled", return_value=False),
            patch.object(runner, "claude_subscription_enabled", return_value=True),
            patch.object(runner, "claude_code_exec", side_effect=fake_claude),
            patch.object(runner, "emit", side_effect=events.append),
        ):
            assert runner.main() == 0
    usage = _usage_events(events)
    assert len(usage) == 6
    # The route's "opus" never stands in for a model Claude Code did not report.
    assert {event.get("model") for event in usage} == {reported}


def test_failed_but_billed_api_call_names_the_route_model():
    from tests.test_math_harness_runner import _strategist

    events: list[dict] = []
    # A worker call that failed after the provider billed it.
    error = runner.APIBackendError(
        "Kimi model call failed for kimi-k3 (HTTP 500): upstream reset",
        usage={"input_tokens": 50, "output_tokens": 8192},
    )
    replies: list[object] = [_strategist(1), error]

    def fake_api(system: str, user: str, *, model: str, **_kwargs):
        value = replies.pop(0) if replies else AssertionError("no more replies")
        if isinstance(value, BaseException):
            raise value
        return SimpleNamespace(text=value, usage=dict(_CALL_USAGE), model=model, provider="kimi")

    env = {
        "AGENT_MONITOR_SELECTED_MODEL": "kimi-k3",
        "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1",
        "AGENT_MONITOR_CGM_CONCURRENCY": "1",
    }
    with tempfile.TemporaryDirectory() as raw:
        with (
            _in_directory(Path(raw)),
            patch.dict(runner.os.environ, env, clear=True),
            patch.object(runner.sys, "argv", ["runner", PROBLEM]),
            patch.object(runner, "codex_subscription_enabled", return_value=False),
            patch.object(runner, "claude_subscription_enabled", return_value=False),
            patch.object(runner, "api_chat", side_effect=fake_api),
            patch.object(runner, "emit", side_effect=events.append),
        ):
            assert runner.main() == 1  # no worker candidate is left
    billed = [event for event in _usage_events(events) if event["usage"].get("output_tokens") == 8192]
    assert [(event["stage"], event["model"]) for event in billed] == [("worker-1", "kimi-k3")]


def test_analyze_check_usage_names_the_model_that_served_it(tmp_path):
    pytest.importorskip("pylatexenc")
    import threading
    from http.server import ThreadingHTTPServer

    from tests import test_analyze_runner as analyze

    server = ThreadingHTTPServer(("127.0.0.1", 0), analyze.Provider)
    server.daemon_threads = True
    server.seen, server.lock, server.script = [], threading.Lock(), analyze.default_script()
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        events: list[dict] = []
        analyze.run(tmp_path, analyze.write_request(tmp_path), analyze.own_key_env(server), events)
    finally:
        server.shutdown()
        server.server_close()
    usage = _usage_events(events)
    assert len(usage) == 4  # the paper map and three checks
    # The fake provider answers as the requested model.
    assert {event["model"] for event in usage} == {"deepseek-test"}


# ── parser: the usage's model reaches the node it is folded into ────────


def _parse(*events: dict) -> list[dict]:
    parser = CLIEventParser("codex", source_engine="math_harness")
    parser.feed("".join(json.dumps(event) + "\n" for event in events))
    return parser.agent_nodes("r")


def test_usage_model_is_stamped_on_a_node_that_names_none():
    nodes = _parse(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "lemma"}},
        {"type": "turn.completed", "model": "gpt-6-astra", "usage": {"input_tokens": 9, "output_tokens": 4}},
    )
    assert (nodes[0]["model"], nodes[0]["input_tokens"]) == ("gpt-6-astra", 9)
    # Not evidence of who wrote the text.
    assert nodes[0]["model_source"] is None


def test_a_nodes_own_model_keeps_precedence_over_its_usage_model():
    nodes = _parse(
        {"type": "item.completed",
         "item": {"type": "agent_message", "text": "lemma", "model": "claude-opus-5-5", "model_source": "response"}},
        {"type": "turn.completed", "model": "gpt-6-astra", "usage": {"input_tokens": 9, "output_tokens": 4}},
    )
    assert (nodes[0]["model"], nodes[0]["model_source"]) == ("claude-opus-5-5", "response")


def test_codex_usage_without_a_model_leaves_the_node_unchanged():
    for model in (None, "", 7):
        event = {"type": "turn.completed", "usage": {"input_tokens": 9, "output_tokens": 4}}
        if model is not None:
            event["model"] = model
        nodes = _parse({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}, event)
        assert nodes[0]["model"] is None, model


# ── jobs: node costs at the served model; exact-only run fallback ───────


@pytest.mark.parametrize("requested_model", ["gpt-6-astra", None])
def test_gpt_6_astra_cgm_run_is_priced_at_gpt_6_astra_rates(prices, requested_model):
    events = _cgm_api_events("gpt-6-astra")
    run = _engine_run(events, requested_model=requested_model)
    nodes = _priced_nodes(run)
    assert len(nodes) == 6
    for node in nodes:
        assert node["model"] == "gpt-6-astra"
        assert node["cost_usd"] == pytest.approx(_node_cost(node), abs=1e-6)
    usage = _usage_events(events)
    expected = sum(_event_cost(event) for event in usage)
    assert run["totals"]["cost_usd"] == pytest.approx(expected, abs=1e-5)
    # Each call: 200 uncached, 1000 cached, 300 out = $0.018 (was $0.00475).
    assert expected == pytest.approx(6 * 0.018)
    assert run["totals"]["cost_usd"] > 3 * sum(_event_cost(event, "gpt-4o") for event in usage)


def test_kimi_run_is_priced_as_kimi(prices):
    events = _cgm_api_events("kimi-k3", observed="kimi-k3")
    run = _engine_run(events, engine="kimi", requested_model="kimi-k3")
    nodes = _priced_nodes(run)
    assert nodes and {node["model"] for node in nodes} == {"kimi-k3"}
    # Moonshot's list price, not the decoy family key listed first.
    expected = sum(_event_cost(event, "moonshot/kimi-k3") for event in _usage_events(events))
    assert expected == pytest.approx(6 * 0.0054)
    assert run["totals"]["cost_usd"] == pytest.approx(expected, abs=1e-5)


def test_max_run_prices_each_stage_at_its_own_accounts_model(prices):
    from tests.test_cgm_max import _Providers, _run_max
    from tests.test_math_harness_runner import _curator, _ok, _strategist, _worker

    providers = _Providers(
        claude_replies=[_strategist(2), _ok(), _ok(), _curator()],
        codex_replies=[_worker("first"), _worker("second")],
    )
    with tempfile.TemporaryDirectory() as raw:
        code, events = _run_max(Path(raw), providers)
    assert code == 0
    usage = _usage_events(events)
    assert {event["model"] for event in usage} == {"claude-fable-5-1", "gpt-6-astra"}
    run = _engine_run(
        events, requested_model=cgm_max.MODEL_ID, extra_env={cgm_max.ROUTE_MODE_ENV: "max"}
    )
    assert {node["model"] for node in _priced_nodes(run)} == {"claude-fable-5-1", "gpt-6-astra"}
    expected = sum(_event_cost(event) for event in usage)
    assert run["totals"]["cost_usd"] == pytest.approx(expected, abs=1e-5)


def test_a_node_with_its_own_model_is_not_repriced_at_the_run_model(prices):
    events = [
        {"type": "item.completed",
         "item": {"type": "agent_message", "text": "advisor", "model": "claude-opus-5-5", "model_source": "response"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1000, "output_tokens": 100}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "worker"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1000, "output_tokens": 100}},
    ]
    run = _engine_run(events, requested_model="gpt-6-astra")
    own, other = _priced_nodes(run)
    assert own["model"] == "claude-opus-5-5"
    assert own["cost_usd"] == pytest.approx(_cost("claude-opus-5-5", uncached=1000, output=100))
    # A node that names no model is priced at the run's exact model.
    assert other["model"] is None
    assert other["cost_usd"] == pytest.approx(_cost("gpt-6-astra", uncached=1000, output=100))


def test_a_replys_model_outranks_the_streams_session_model(prices):
    # A Claude-style stream names its configured model once (system/init) and
    # the model that answered on each reply.  The reply's model prices its
    # node; the old order priced every node at the init model.
    events = [
        {"type": "system", "subtype": "init", "model": "claude-opus-5-5"},
        {"type": "assistant", "message": {
            "model": "claude-fable-5-1",
            "content": [{"type": "text", "text": "Proof written."}],
            "usage": {"input_tokens": 1000, "output_tokens": 100},
        }},
    ]
    run = _engine_run(
        events, engine="claude", requested_model="opus",
        extra_env={"AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1"},
    )
    (node,) = _priced_nodes(run)
    assert node["model"] == "claude-fable-5-1"
    assert node["cost_usd"] == pytest.approx(_cost("claude-fable-5-1", uncached=1000, output=100))
    assert node["cost_usd"] != pytest.approx(_cost("claude-opus-5-5", uncached=1000, output=100))


def test_an_older_runners_stream_is_priced_at_the_runs_exact_model(prices):
    events = _without_models(_cgm_api_events("gpt-6-astra"))
    assert not any("model" in event for event in _usage_events(events))
    with patch.object(jobs, "_exact_price_model", wraps=jobs._exact_price_model) as check:
        run = _engine_run(events, requested_model="gpt-6-astra")
    # Resolved once per run, however many nodes and flushes need it.
    assert check.call_count == 1
    for node in _priced_nodes(run):
        assert node["cost_usd"] == pytest.approx(_node_cost(node, "gpt-6-astra"), abs=1e-6)


@pytest.mark.parametrize(
    ("requested_model", "extra_env"),
    [
        ("opus", {"AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1"}),  # Claude 3 Opus by family match
        ("fable", {"AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1"}),
        (cgm_max.MODEL_ID, {cgm_max.ROUTE_MODE_ENV: "max"}),  # a Codex-max key by family match
        # A Max run has two models: even an exact selection is not the run's.
        ("gpt-6-astra", {cgm_max.ROUTE_MODE_ENV: "max"}),
    ],
)
def test_a_route_alias_is_never_the_fallback_price(prices, requested_model, extra_env):
    events = _without_models(_cgm_api_events("gpt-6-astra"))
    run = _engine_run(events, requested_model=requested_model, extra_env=extra_env)
    nodes = _priced_nodes(run)
    assert nodes
    for node in nodes:
        # _estimate_cost's fallback, as before this fix.
        assert node["cost_usd"] == pytest.approx(_node_cost(node, "gpt-4o"), abs=1e-6)


def test_a_run_without_usage_never_loads_the_pricing_table(prices):
    events = [{"type": "item.completed", "item": {"type": "agent_message", "text": "no call billed"}}]
    with patch.object(jobs, "_exact_price_model", wraps=jobs._exact_price_model) as check:
        run = _engine_run(events, requested_model="gpt-6-astra")
    check.assert_not_called()
    assert run["totals"]["cost_usd"] is None


# ── reasoning tokens reach the run totals and the ledger ────────────────


def test_reasoning_tokens_reach_the_totals_and_the_engine_ledger_row(prices):
    events = _cgm_api_events("gpt-6-astra")
    run = _engine_run(events, requested_model="gpt-6-astra")
    reasoning = sum(event["usage"]["reasoning_output_tokens"] for event in _usage_events(events))
    assert reasoning == 6 * 80
    assert run["totals"]["reasoning_tokens"] == reasoning
    usage_ledger.record_run(run)
    summary = usage_ledger.usage_summary({})
    engine = next(row for row in summary["by_stage"] if row["key"] == "engine")
    assert engine["reasoning_tokens"] == reasoning
    assert engine["cost_usd"] == pytest.approx(run["totals"]["cost_usd"])


def test_a_stream_without_reasoning_records_none(prices):
    events = [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {"input_tokens": 9, "output_tokens": 4}},
    ]
    run = _engine_run(events, requested_model="gpt-6-astra")
    assert run["totals"]["reasoning_tokens"] is None
    assert run["totals"]["output_tokens"] == 4


def test_continuations_sum_reasoning_and_the_ledger_books_the_new_part():
    def record(totals: dict, **extra) -> dict:
        return {"run_id": "cont_cost", "engine": "math_harness", "status": "finished",
                "agents": [], "totals": totals, **extra}

    # A record written before this fix has no reasoning in its totals.
    first = record({"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.5})
    usage_ledger.record_run(first)
    follow_up = record({"input_tokens": 40, "output_tokens": 20, "cost_usd": 0.2, "reasoning_tokens": 12})
    merged = jobs._merge_continuation_run(first, follow_up, job_id="j2")
    assert merged["totals"]["reasoning_tokens"] == 12
    usage_ledger.record_run(merged)
    again = jobs._merge_continuation_run(
        merged, record({"output_tokens": 5, "cost_usd": 0.1, "reasoning_tokens": 3}), job_id="j3"
    )
    assert again["totals"]["reasoning_tokens"] == 15
    usage_ledger.record_run(again)
    totals = usage_ledger.usage_summary({})["totals"]
    assert totals["reasoning_tokens"] == 15
    assert totals["output_tokens"] == 75
