"""Subscription routes book each call's usage on that call's own Monitor node.

A Codex or Claude Code CLI prints its reply before it exits, and the call's
usage is only known once it has.  With several calls in flight
(AGENT_MONITOR_CGM_CONCURRENCY > 1), another call's reply could come in
between, and the Monitor, which books a usage event on the node just before
it, put the usage on that other call's node.  The reply is now held and sent
together with the usage, as on the API routes.
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_monitor.cli_events import CLIEventParser
from agent_monitor.runners import math_harness_runner as runner
from agent_monitor.runners.codex_backend import CodexBackendError

from tests.test_cgm_max import _run_max
from tests.test_math_harness_runner import _curator, _ok, _receipt, _strategist, _worker

CODEX = runner.Route("codex_subscription", "gpt-6-astra", "codex_exec")
CLAUDE = runner.Route("claude_subscription", "fable", "claude_code")


def _ctx(route: runner.Route, events: list[dict]) -> runner._Ctx:
    return runner._Ctx(
        route=route,
        worker_route=None,
        budgets=runner.Budgets(max_calls=8),
        started=time.monotonic(),
        env={},
        concurrency=2,
        emitter=runner._RunEmitter(events.append),
    )


def _reply(text: str) -> dict:
    return {"type": "item.completed", "item": {"type": "agent_message", "text": text}}


def _nodes(events: list[dict]) -> list[dict]:
    parser = CLIEventParser("codex", source_engine="math_harness")
    parser.feed("".join(json.dumps(event) + "\n" for event in events))
    return parser.turns


class ProviderCallUnitTests(unittest.TestCase):
    def test_a_codex_reply_and_its_usage_go_out_together(self) -> None:
        events: list[dict] = []
        ctx = _ctx(CODEX, events)
        turn = ctx.open_turn("worker-1", 1)

        def codex(system, user, *, emit_event, **_kwargs):
            emit_event({"type": "item.completed", "item": {"type": "reasoning", "text": "thinking"}})
            emit_event(_reply("worker-1 reply"))
            # Another call of the wave closes its node while this CLI exits.
            events.append(_reply("worker-2 reply"))
            emit_event({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
            return SimpleNamespace(text="worker-1 reply", usage={"input_tokens": 70, "output_tokens": 300})

        with patch.object(runner, "codex_exec", side_effect=codex):
            reply = runner._provider_call(ctx, turn, stage="worker-1", system="s", user="u", effort=None)
        self.assertEqual(reply.text, "worker-1 reply")
        self.assertEqual(
            [(e["type"], (e.get("item") or {}).get("text", e.get("stage"))) for e in events[-2:]],
            [("item.completed", "worker-1 reply"), ("turn.completed", "worker-1")],
        )
        nodes = {node["detail"]: node for node in _nodes(events)}
        self.assertEqual(nodes["worker-1 reply"]["output_tokens"], 300)
        self.assertEqual(nodes["worker-2 reply"]["output_tokens"], 0)
        # The CLI's own terminal event is still never forwarded.
        self.assertEqual(sum(e["type"] == "turn.completed" for e in events), 1)

    def test_a_claude_code_reply_and_its_usage_go_out_together(self) -> None:
        events: list[dict] = []
        ctx = _ctx(CLAUDE, events)
        turn = ctx.open_turn("verify-1", 1)
        assistant = {
            "type": "assistant",
            "message": {"model": "claude-fable-5-1", "content": [
                {"type": "thinking", "thinking": "checking"}, {"type": "text", "text": "verify-1 reply"}]},
        }

        def claude(system, user, *, emit_event, **_kwargs):
            emit_event({"type": "system", "subtype": "init", "session_id": "s1"})
            emit_event(assistant)
            events.append(_reply("verify-2 reply"))
            emit_event({"type": "result", "subtype": "success", "usage": {"input_tokens": 1, "output_tokens": 1}})
            return SimpleNamespace(
                text="verify-1 reply", usage={"input_tokens": 50, "output_tokens": 90},
                model="claude-fable-5-1", events=[assistant],
            )

        with patch.object(runner, "claude_code_exec", side_effect=claude):
            reply = runner._provider_call(ctx, turn, stage="verify-1", system="s", user="u", effort=None)
        self.assertEqual(reply.observed_model, "claude-fable-5-1")
        self.assertEqual(
            [(e["type"], (e.get("item") or {}).get("text", e.get("stage"))) for e in events[-2:]],
            [("item.completed", "verify-1 reply"), ("turn.completed", "verify-1")],
        )
        nodes = {node["detail"]: node for node in _nodes(events)}
        self.assertEqual(nodes["verify-1 reply"]["output_tokens"], 90)
        self.assertEqual(nodes["verify-2 reply"]["output_tokens"], 0)

    def test_earlier_messages_keep_their_order_and_the_last_closes_the_call(self) -> None:
        events: list[dict] = []
        ctx = _ctx(CODEX, events)
        turn = ctx.open_turn("worker-1", 1)

        def codex(system, user, *, emit_event, **_kwargs):
            emit_event(_reply("first message"))
            emit_event({"type": "item.completed", "item": {"type": "reasoning", "text": "more thought"}})
            emit_event(_reply("final message"))
            return SimpleNamespace(text="final message", usage={"input_tokens": 7, "output_tokens": 30})

        with patch.object(runner, "codex_exec", side_effect=codex):
            runner._provider_call(ctx, turn, stage="worker-1", system="s", user="u", effort=None)
        self.assertEqual(
            [(e.get("item") or {}).get("text", e.get("stage")) for e in events],
            ["Math Harness · worker-1", "first message", "more thought", "final message", "worker-1"],
        )

    def test_a_reply_printed_before_a_failure_is_still_shown(self) -> None:
        events: list[dict] = []
        ctx = _ctx(CODEX, events)
        turn = ctx.open_turn("worker-1", 1)

        def codex(system, user, *, emit_event, **_kwargs):
            emit_event(_reply("partial reply"))
            raise CodexBackendError("Codex exited with code 1")

        with patch.object(runner, "codex_exec", side_effect=codex), self.assertRaises(runner.ProviderCallError):
            runner._provider_call(ctx, turn, stage="worker-1", system="s", user="u", effort=None)
        self.assertEqual(
            [(e.get("item") or {}).get("text") for e in events], ["Math Harness · worker-1", "partial reply"],
        )

    def test_an_unexpected_failure_still_shows_the_printed_reply(self) -> None:
        events: list[dict] = []
        ctx = _ctx(CODEX, events)
        turn = ctx.open_turn("worker-1", 1)

        def codex(system, user, *, emit_event, **_kwargs):
            emit_event(_reply("partial reply"))
            raise RuntimeError("unexpected")

        with patch.object(runner, "codex_exec", side_effect=codex), self.assertRaises(runner.ProviderCallError):
            runner._provider_call(ctx, turn, stage="worker-1", system="s", user="u", effort=None)
        self.assertIn("partial reply", [(e.get("item") or {}).get("text") for e in events])

    def test_events_that_make_no_node_leave_the_reply_with_its_usage(self) -> None:
        events: list[dict] = []
        ctx = _ctx(CODEX, events)
        turn = ctx.open_turn("worker-1", 1)

        def codex(system, user, *, emit_event, **_kwargs):
            emit_event({"type": "thread.started", "thread_id": "t1"})
            emit_event({"type": "turn.started"})
            emit_event(_reply("worker-1 reply"))
            emit_event({"type": "item.started", "item": {"type": "agent_message"}})
            emit_event({"type": "error", "message": "stream notice"})
            emit_event({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
            return SimpleNamespace(text="worker-1 reply", usage={"input_tokens": 7, "output_tokens": 30})

        with patch.object(runner, "codex_exec", side_effect=codex):
            runner._provider_call(ctx, turn, stage="worker-1", system="s", user="u", effort=None)
        self.assertEqual(
            [e["type"] for e in events],
            ["item.completed", "thread.started", "turn.started", "item.started", "error",
             "item.completed", "turn.completed"],
        )
        # One call marker only, and the usage is on the reply's node.
        self.assertEqual(
            [(e.get("item") or {}).get("text") for e in events if e["type"] == "item.completed"],
            ["Math Harness · worker-1", "worker-1 reply"],
        )
        self.assertEqual({node["detail"]: node for node in _nodes(events)}["worker-1 reply"]["output_tokens"], 30)

    def test_a_call_that_printed_no_reply_books_its_usage_on_its_own_node(self) -> None:
        events: list[dict] = []
        ctx = _ctx(CLAUDE, events)
        turn = ctx.open_turn("verify-1", 1)

        def claude(system, user, *, emit_event, **_kwargs):
            emit_event({"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "hm"}]}})
            events.append(_reply("verify-2 reply"))
            return SimpleNamespace(text="", usage={"input_tokens": 50, "output_tokens": 90},
                                   model="claude-fable-5-1", events=[])

        with patch.object(runner, "claude_code_exec", side_effect=claude):
            runner._provider_call(ctx, turn, stage="verify-1", system="s", user="u", effort=None)
        self.assertEqual(
            [(e["type"], (e.get("item") or {}).get("text", e.get("stage"))) for e in events[-2:]],
            [("item.completed", "Math Harness · verify-1"), ("turn.completed", "verify-1")],
        )
        nodes = _nodes(events)
        self.assertEqual(nodes[-1]["output_tokens"], 90)
        self.assertEqual({node["detail"]: node["output_tokens"] for node in nodes}["verify-2 reply"], 0)

    def test_an_api_call_is_unchanged(self) -> None:
        events: list[dict] = []
        ctx = _ctx(runner.Route("openai", "gpt-6-astra", "api_chat"), events)
        ctx.env = {"OPENAI_API_KEY": "sk-test-not-a-key"}
        turn = ctx.open_turn("worker-1", 1)
        result = SimpleNamespace(text="api reply", usage={"input_tokens": 3, "output_tokens": 4},
                                 model="gpt-6-astra", provider="openai")
        with patch.object(runner, "api_chat", return_value=result):
            runner._provider_call(ctx, turn, stage="worker-1", system="s", user="u", effort=None)
        self.assertEqual(
            [(e.get("item") or {}).get("text", e.get("stage")) for e in events],
            ["Math Harness · worker-1", "api reply", "worker-1"],
        )

    def test_an_abandoned_call_shows_nothing_more(self) -> None:
        events: list[dict] = []
        ctx = _ctx(CODEX, events)
        turn = ctx.open_turn("worker-1", 1)

        def codex(system, user, *, emit_event, **_kwargs):
            emit_event(_reply("late reply"))
            ctx.close_turn(turn, abandon=True)
            return SimpleNamespace(text="late reply", usage={"input_tokens": 7, "output_tokens": 30})

        with patch.object(runner, "codex_exec", side_effect=codex), self.assertRaises(runner._TurnAbandoned):
            runner._provider_call(ctx, turn, stage="worker-1", system="s", user="u", effort=None)
        self.assertEqual([(e.get("item") or {}).get("text") for e in events], ["Math Harness · worker-1"])


class _InterleavingCli:
    """Max (subscription) CLIs that print a reply, then exit only after every
    worker has printed its own: each worker's usage comes after the others'
    replies unless the reply is held for the closing unit."""

    def __init__(self, workers: int) -> None:
        self.workers = workers
        self.printed = threading.Barrier(workers, timeout=10)
        self.lock = threading.Lock()
        self.worker_calls = 0
        self.output_by_tag: dict[str, int] = {}

    def codex(self, system, user, *, emit_event, **_kwargs):
        with self.lock:
            self.worker_calls += 1
            index = self.worker_calls
        tag = f"w{index}"
        text = _worker(tag)
        output = 1000 * index
        self.output_by_tag[tag] = output
        emit_event(_reply(text))
        if index <= self.workers:
            self.printed.wait()
        return SimpleNamespace(text=text, usage={"input_tokens": 7, "output_tokens": output})

    def claude(self, system, user, **_kwargs):
        if system == runner.cgm_prompts.STRATEGIST_SYSTEM:
            text = _strategist(self.workers)
        elif system == runner.cgm_prompts.VERIFIER_SYSTEM:
            text = _ok()
        else:
            text = _curator()
        return SimpleNamespace(text=text, usage={"input_tokens": 5, "output_tokens": 9},
                               model="claude-fable-5-1", events=[])


class MaxSubscriptionConcurrencyTests(unittest.TestCase):
    def test_concurrent_workers_book_their_own_usage(self) -> None:
        cli = _InterleavingCli(3)
        providers = SimpleNamespace(codex=cli.codex, claude=cli.claude)
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            code, events = _run_max(
                workspace, providers,
                {"AGENT_MONITOR_CGM_CONCURRENCY": "3", "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "3"},
            )
            self.assertEqual(code, 0)
            self.assertEqual(_receipt(workspace)["verdict"], "accepted")
        self.assertEqual(cli.worker_calls, 3)
        for index, event in enumerate(events):
            if event.get("type") == "turn.completed" and str(event.get("stage")).startswith("worker-"):
                before = events[index - 1].get("item") or {}
                self.assertEqual(before.get("type"), "agent_message", event)
        # Each worker's reply node carries that worker's own usage (the run's
        # closing summary quotes the accepted proof too, so only replies count).
        booked = {}
        for node in _nodes(events):
            if not node["detail"].startswith("=== LEMMA"):
                continue
            for tag in cli.output_by_tag:
                if f"[{tag}]" in node["detail"]:
                    booked[tag] = node["output_tokens"]
        self.assertEqual(booked, cli.output_by_tag)


if __name__ == "__main__":
    unittest.main()
