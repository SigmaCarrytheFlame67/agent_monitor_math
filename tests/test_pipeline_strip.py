"""The Monitor 'Agent Flow' strip counts every agent exactly once."""
from __future__ import annotations

from agent_monitor.paths import ensure_import_paths

ensure_import_paths()

from harness_dashboard.analysis import build_agent_flow  # noqa: E402


def _agent(trace_id: str, stage: str, role: str, cost: float, **extra) -> dict:
    return {
        "trace_id": trace_id,
        "stage_name": trace_id,
        "role": role,
        "pipeline_stage": stage,
        "cost_usd": cost,
        "call_seq": int(trace_id.rsplit("-", 1)[-1]),
        **extra,
    }


def _strip_totals(flow: dict) -> tuple[int, float, dict[str, int]]:
    strip = flow["compact"]["pipeline_strip"]
    by_stage = {card["id"]: card["agent_count"] for card in strip}
    return (
        sum(card["agent_count"] for card in strip),
        round(sum(card["cost_usd"] for card in strip), 2),
        by_stage,
    )


def test_cgm_shaped_run_with_only_isolated_groups_is_not_doubled():
    # CGM runs have no cross-group edges, so every role group is isolated.
    shape = {"1": ("strategist", 2), "2": ("worker", 11), "3": ("verifier", 10),
             "4": ("curator", 3), "5": ("finalizer", 4)}
    agents = []
    seq = 0
    for stage, (role, count) in shape.items():
        for _ in range(count):
            seq += 1
            agents.append(_agent(f"{role}-{seq}", stage, role, 0.25))
    flow = build_agent_flow(agents)

    total, cost, by_stage = _strip_totals(flow)
    assert total == len(flow["nodes"]) == 30
    assert by_stage == {stage: count for stage, (_role, count) in shape.items()}
    assert cost == round(0.25 * 30, 2)
    # The per-role breakdown agrees with the stage total.
    for card in flow["compact"]["pipeline_strip"]:
        assert sum(role["count"] for role in card["roles"]) == card["agent_count"]


def test_mixed_connected_and_isolated_groups_count_each_agent_once():
    agents = [
        _agent("advisor-1", "1", "advisor", 1.0, sent_to=["solver-2"]),
        _agent("solver-2", "1", "solver", 0.5),
        _agent("helper-3", "1", "helper", 0.5),
        _agent("helper-4", "1", "helper", 0.5),
        _agent("verifier-5", "2", "verifier", 0.75),
    ]
    flow = build_agent_flow(agents)
    total, cost, by_stage = _strip_totals(flow)
    assert total == len(flow["nodes"]) == 5
    assert by_stage == {"1": 4, "2": 1}
    assert cost == 3.25
