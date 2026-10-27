"""Parse structured (JSON) event streams from external CLI harnesses.

Instead of scraping the human TTY output, we run each CLI in its
machine-readable mode and accumulate token usage, cost, and a readable
activity log:

- Codex CLI:     ``codex exec --json``            -> JSONL events
- OpenClaude:    ``--output-format stream-json``   -> JSONL events
- OpenHands:     TTY output + ``~/.openhands/conversations/<id>/base_state.json``
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


# Engines whose trusted runner announces CGM pipeline stages as
# ``{"type": "pipeline.stage", "stage": <id>, "label": ...}`` events. The ids
# match ``jobs._pipeline_for`` for these engines.  The Analyze runner reuses
# the same five ids (Ingest, Segment, Check, Roll-up, Report), and so does the
# Readability runner (Ingest, Guide, Rewrite, Check, Publish).
PIPELINE_STAGE_ENGINES = frozenset({"math_harness", "kimi", "analyze", "readability"})
PIPELINE_STAGE_IDS = frozenset({"scaffold", "work", "verify", "curate", "finalize"})


class CLIEventParser:
    """Feed raw stdout lines; accumulates usage + readable log lines."""

    def __init__(self, engine: str, *, source_engine: str | None = None):
        # ``engine`` selects the stream protocol (parser style); the source
        # engine is the product engine, which decides whether runner-emitted
        # pipeline stage events are trusted.
        self.engine = engine
        # Codex (OpenAI) usage counts cached input inside ``input_tokens``;
        # Claude-style streams (claude, openclaude, openclaw) report cache
        # reads and writes separately from ``input_tokens``.
        self.input_includes_cache = engine == "codex"
        self.source_engine = str(source_engine or engine or "").strip().lower()
        self.pipeline_stage: str | None = None
        self.pipeline_stage_label: str | None = None
        self.lines: list[str] = []
        self.usage: dict[str, Any] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "cost_usd": None,
            "model": None,
        }
        self._raw_tail: list[str] = []
        # Closed per-turn records -> one Monitor agent node each.
        self.turns: list[dict[str, Any]] = []
        self._cur: dict[str, Any] = self._new_turn()
        # Tool telemetry for the console's live panel.
        self.tool_events: list[dict[str, str]] = []
        self.tool_calls = 0
        # Structured CLIs can report a useful terminal subtype even when the
        # process itself only exposes a generic non-zero exit code.
        self.terminal_error: str | None = None
        self.terminal_status: str | None = None

    def _new_turn(self) -> dict[str, Any]:
        return {"items": [], "kinds": set(), "detail": [], "thinking": [], "tools": []}

    def live_state(self) -> dict[str, Any]:
        """Model + recent tool activity, mirrored into the run record."""
        return {
            "engine": self.engine,
            "model": self.usage.get("model"),
            "tools_active": [self.tool_events[-1]["name"]] if self.tool_events else [],
            "tools_recent": self.tool_events[-10:],
            "tool_calls": self.tool_calls,
            "iteration": len(self.turns) + 1,
        }

    # ── feeding ──────────────────────────────────────────────────────────

    def feed(self, chunk: str) -> None:
        # Newlines only: str.splitlines() also splits at U+2028, U+2029 and
        # U+0085, which a JSON event may carry raw inside a string.
        for line in chunk.split("\n"):
            self._feed_line(line)

    def _feed_line(self, line: str) -> None:
        s = line.strip()
        if not s:
            return
        self._raw_tail.append(line)
        if len(self._raw_tail) > 2000:
            del self._raw_tail[: len(self._raw_tail) - 2000]
        if s.startswith("{"):
            try:
                ev = json.loads(s)
            except json.JSONDecodeError:
                self.lines.append(line.rstrip())
                return
            if self.engine == "codex":
                self._codex_event(ev)
            elif self.engine in {"claude", "openclaude"}:
                self._openclaude_event(ev)
            elif self.engine == "openclaw":
                self._openclaw_event(ev)
            else:
                self.lines.append(line.rstrip())
        else:
            self.lines.append(line.rstrip())

    # ── codex ────────────────────────────────────────────────────────────

    def _codex_event(self, ev: dict) -> None:
        t = ev.get("type") or ""
        if t == "pipeline.stage":
            self._pipeline_stage_event(ev)
        elif t == "thread.started":
            self._log(f"◦ session {ev.get('thread_id', '')[:18]}…")
        elif t == "item.completed":
            item = ev.get("item") or {}
            it = item.get("type") or ""
            # One Monitor node per item (reasoning / exec / edit / message).
            if it == "agent_message":
                self._log(f"assistant · {_clip(item.get('text'))}")
                self._item("message", str(item.get("text") or ""))
                observed = item.get("model") if item.get("model_source") == "response" else None
                self._close_turn(
                    model=observed,
                    model_source="response" if isinstance(observed, str) and observed.strip() else None,
                )
            elif it == "reasoning":
                self._log(f"thinking · {_clip(item.get('text'))}")
                self._item("reasoning", str(item.get("text") or ""))
                self._cur["thinking"].append(str(item.get("text") or ""))
                self._close_turn()
            elif it == "command_execution":
                self._log(f"exec · {_clip(item.get('command'), 120)}")
                self._item("exec", f"$ {item.get('command')}\n{item.get('aggregated_output') or ''}", tool="shell")
                self._close_turn()
            elif it == "file_change":
                changes = item.get("changes") or []
                paths = ", ".join(c.get("path", "?") for c in changes[:3])
                self._log(f"edit · {paths}")
                self._item("edit", f"edited: {paths}", tool="edit")
                self._close_turn()
            elif it in {"mcp_tool_call", "web_search", "todo_list"}:
                self._log(f"{it} · {_clip(item.get('text') or item.get('query') or '', 100)}")
                self._item("tool", f"{it}: {item.get('text') or item.get('query') or ''}", tool=str(it))
                self._close_turn()
            elif it == "error":
                message = str(item.get("message") or "")
                self._log(f"error · {_clip(message)}")
                self._item("error", message)
                # Trusted wrappers mark their final, already-sanitized error.
                # Ordinary model/CLI item text must not silently become a
                # top-level status diagnostic.
                if item.get("terminal") is True and message.strip():
                    self.terminal_error = message.strip()[-1200:]
                self._close_turn()
        elif t == "turn.completed":
            self.terminal_status = "completed"
            self.terminal_error = None
            u = ev.get("usage") or {}
            # Codex semantics: both cached parts are inside input_tokens.
            # cache_write_tokens only comes from our own projections of
            # Claude usage (Codex has no cache writes); those projections send
            # input without the cache, flagged, so fold it in here.
            cached = int(u.get("cached_input_tokens") or u.get("cache_read_tokens") or 0)
            written = int(u.get("cache_write_tokens") or 0)
            input_tokens = int(u.get("input_tokens") or 0)
            if u.get("input_excludes_cache") is True:
                input_tokens += cached + written
            # Reasoning is part of output_tokens, never priced on top. Codex
            # names it reasoning_output_tokens; Kimi usage (api_backend, the
            # CGM harness) and OpenHands usage name it reasoning_tokens. One
            # name is read, in usage_ledger.cli_usage's order.
            reasoning = int(
                u.get("reasoning_tokens") or u.get("reasoning_output_tokens") or 0
            )
            self.usage["input_tokens"] += input_tokens
            self.usage["cache_read_tokens"] += cached
            self.usage["cache_write_tokens"] += written
            self.usage["output_tokens"] += int(u.get("output_tokens") or 0)
            self.usage["reasoning_tokens"] += reasoning
            self._log(
                f"· usage in {input_tokens} / out {u.get('output_tokens', 0)}"
            )
            # The CGM runners name the model each call's usage is priced at
            # (Codex's own events never do), so the node is priced at that
            # model's rates.  The run-level model stays unset: a CGM run's
            # calls can use different models.
            model = ev.get("model")
            model = model.strip() if isinstance(model, str) else ""
            self._close_turn(
                input_tokens=input_tokens,
                output_tokens=int(u.get("output_tokens") or 0),
                cache_read=cached,
                cache_write=written,
                reasoning=reasoning,
                model=model or None,
            )
        elif t in {"turn.failed", "turn.cancelled"}:
            detail = ev.get("error") or ev.get("message") or t
            if isinstance(detail, dict):
                detail = detail.get("message") or str(detail)
            self.terminal_status = "failed"
            self.terminal_error = str(detail)[-1200:]
            self._log(f"error · {_clip(self.terminal_error)}")
        elif t == "error":
            self._log(f"error · {_clip(ev.get('message'))}")

    def _pipeline_stage_event(self, ev: dict) -> None:
        """Remember the CGM stage so later turn nodes land in its column."""
        if self.source_engine not in PIPELINE_STAGE_ENGINES:
            return
        stage = str(ev.get("stage") or "").strip().lower()
        if stage not in PIPELINE_STAGE_IDS:
            return
        # A stage boundary closes the previous stage's pending items.
        self._close_turn()
        self.pipeline_stage = stage
        label = " ".join(str(ev.get("label") or stage).split())[:60]
        self.pipeline_stage_label = label
        detail = " ".join(str(ev.get("detail") or "").split())[:120]
        self._log(f"stage · {label}" + (f" · {detail}" if detail else ""))

    # ── openclaude (claude-code protocol) ────────────────────────────────

    def _openclaude_event(self, ev: dict) -> None:
        t = ev.get("type") or ""
        if t == "system" and ev.get("subtype") == "init":
            self.usage["model"] = ev.get("model")
            self._log(f"◦ session started · model {ev.get('model')}")
        elif t == "assistant" and _claude_api_error_message(ev):
            # Claude Code reports its own API errors (not logged in, usage
            # limit, outage) as a synthetic assistant message.  It is no model
            # output: show it as an error, never as the assistant's answer.
            msg = ev.get("message") or {}
            text = " ".join(
                str(block.get("text") or "").strip()
                for block in msg.get("content") or []
                if isinstance(block, dict)
            ).strip() or str(ev.get("error") or "Claude Code API error")
            self._log(f"error · {_clip(text)}")
            self._item("error", text)
            self._close_turn()
        elif t == "assistant":
            msg = ev.get("message") or {}
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                bt = block.get("type")
                if bt in {"thinking", "redacted_thinking"} and (block.get("thinking") or "").strip():
                    self._log(f"thinking · {_clip(block['thinking'])}")
                    self._cur["thinking"].append(block["thinking"])
                elif bt == "text" and (block.get("text") or "").strip():
                    self._log(f"assistant · {_clip(block['text'])}")
                    self._item("message", block["text"])
                elif bt == "tool_use":
                    name = block.get("name", "?")
                    inp = block.get("input") or {}
                    hint = inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
                    self._log(f"tool · {name} {_clip(str(hint), 90)}")
                    self._item("tool", f"{name} · {hint}", tool=str(name))
            u = msg.get("usage") or {}
            # Each assistant message is one model call -> one turn/node.
            self._close_turn(
                input_tokens=int(u.get("input_tokens") or 0),
                output_tokens=int(u.get("output_tokens") or 0),
                cache_read=int(u.get("cache_read_input_tokens") or 0),
                cache_write=int(u.get("cache_creation_input_tokens") or 0),
                model=msg.get("model"),
                model_source="response" if isinstance(msg.get("model"), str) and msg["model"].strip() else None,
            )
        elif t == "result":
            u = ev.get("usage") or {}
            self.usage["input_tokens"] = int(u.get("input_tokens") or 0)
            self.usage["cache_read_tokens"] = int(u.get("cache_read_input_tokens") or 0)
            self.usage["cache_write_tokens"] = int(u.get("cache_creation_input_tokens") or 0)
            self.usage["output_tokens"] = int(u.get("output_tokens") or 0)
            if ev.get("total_cost_usd") is not None:
                self.usage["cost_usd"] = float(ev["total_cost_usd"])
            result_text = str(ev.get("result") or "").strip()
            subtype = str(ev.get("subtype") or "").strip()
            if ev.get("is_error") or subtype != "success":
                errors = ev.get("errors")
                listed = (
                    "; ".join(str(item).strip() for item in errors if str(item).strip())
                    if isinstance(errors, list)
                    else ""
                )
                self.terminal_status = "failed"
                # Claude Code puts the reason of error_max_turns and friends in
                # ``errors``, not ``result``. OpenClaude keeps the subtype last:
                # its one-shot recovery keys on "…: error_max_turns".
                if listed and self.source_engine == "openclaude" and subtype not in ("", "success"):
                    listed = f"{listed}: {subtype}"
                self.terminal_error = (
                    result_text or listed or subtype or "OpenClaude reported an error"
                )
                self._log(
                    f"✗ result · {ev.get('num_turns', '?')} turns · "
                    f"{_clip(self.terminal_error, 160)}"
                )
            else:
                self.terminal_status = "completed"
                self.terminal_error = None
                self._log(
                    f"✓ result · {ev.get('num_turns', '?')} turns · "
                    f"${ev.get('total_cost_usd', 0):.4f} · {_clip(result_text, 120)}"
                )

    # ── openclaw (agent --local --json + session JSONL) ──────────────────

    def _openclaw_event(self, ev: dict) -> None:
        """Handle any single-line JSON openclaw emits (rare; result is pretty-printed)."""
        payloads = ev.get("payloads")
        if isinstance(payloads, list):
            for p in payloads:
                text = (p or {}).get("text") or ""
                if text.strip():
                    self._log(f"assistant · {_clip(text)}")

    def finalize_openclaw(self) -> None:
        """Parse the openclaw session JSONL (per-message usage, cost, tool calls)."""
        text = "\n".join(self._raw_tail)
        m = re.search(r'"sessionFile":\s*"([^"]+\.jsonl)"', text)
        if not m:
            return
        path = Path(m.group(1))
        if not path.exists():
            return
        total_cost = 0.0
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") != "message":
                continue
            msg = ev.get("message") or {}
            role = msg.get("role") or ""
            if role == "assistant":
                for block in msg.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    bt = block.get("type")
                    if bt == "text" and (block.get("text") or "").strip():
                        self._log(f"assistant · {_clip(block['text'])}")
                        self._item("message", block["text"])
                    elif bt in {"thinking", "reasoning"}:
                        body = block.get("thinking") or block.get("text") or ""
                        if body.strip():
                            self._cur["thinking"].append(body)
                    elif bt in {"toolCall", "tool_use", "toolUse"}:
                        name = block.get("name") or block.get("toolName") or "tool"
                        args = json.dumps(
                            block.get("arguments") or block.get("input") or {},
                            ensure_ascii=False,
                        )[:400]
                        self._log(f"tool · {name}")
                        self._item("tool", f"{name}({args})", tool=str(name))
                u = msg.get("usage") or {}
                cost = (u.get("cost") or {}).get("total") or 0
                total_cost += float(cost)
                self._close_turn(
                    input_tokens=int(u.get("input") or 0),
                    output_tokens=int(u.get("output") or 0),
                    cache_read=int(u.get("cacheRead") or 0),
                    cache_write=int(u.get("cacheWrite") or 0),
                    reasoning=int(u.get("reasoningTokens") or 0),
                    model=self.usage.get("model"),
                )
                self.usage["input_tokens"] += int(u.get("input") or 0)
                self.usage["output_tokens"] += int(u.get("output") or 0)
                self.usage["cache_read_tokens"] += int(u.get("cacheRead") or 0)
                self.usage["cache_write_tokens"] += int(u.get("cacheWrite") or 0)
            elif role in {"tool", "toolResult", "tool_result"}:
                for block in msg.get("content") or []:
                    if isinstance(block, dict) and (block.get("text") or "").strip() and self.turns:
                        prev = self.turns[-1]
                        prev["detail"] = (prev.get("detail") or "") + "\n\n" + block["text"][:2000]
        mm = re.search(r'"model":\s*"([^"]+)"', text)
        if mm:
            self.usage["model"] = mm.group(1)
        if total_cost:
            self.usage["cost_usd"] = total_cost
        self._log(
            f"✓ usage · in {self.usage['input_tokens']} / out {self.usage['output_tokens']}"
            f" · ${total_cost:.4f}"
        )

    # ── openhands (post-run state file) ──────────────────────────────────

    def finalize_openhands(self) -> None:
        """Extract usage + per-action agent nodes from ~/.openhands conversation state."""
        text = "\n".join(self._raw_tail)
        m = re.search(r"Conversation ID:\s*([0-9a-f-]+)", text)
        if not m:
            return
        conv_id = m.group(1).replace("-", "")
        conv_dir = Path.home() / ".openhands" / "conversations" / conv_id
        state = conv_dir / "base_state.json"
        if state.exists():
            try:
                data = json.loads(state.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
            total_cost = 0.0
            for metrics in (data.get("stats", {}).get("usage_to_metrics") or {}).values():
                tu = metrics.get("accumulated_token_usage") or {}
                self.usage["input_tokens"] += int(tu.get("prompt_tokens") or 0)
                self.usage["output_tokens"] += int(tu.get("completion_tokens") or 0)
                self.usage["cache_read_tokens"] += int(tu.get("cache_read_tokens") or 0)
                self.usage["cache_write_tokens"] += int(tu.get("cache_write_tokens") or 0)
                self.usage["reasoning_tokens"] += int(tu.get("reasoning_tokens") or 0)
                total_cost += float(metrics.get("accumulated_cost") or 0)
                self.usage["model"] = tu.get("model") or self.usage["model"]
            if total_cost:
                self.usage["cost_usd"] = total_cost
            self._log(
                f"✓ usage · in {self.usage['input_tokens']} / out {self.usage['output_tokens']}"
                f" · ${total_cost:.4f}"
            )
        # Turn OpenHands events into Monitor agent nodes (Action/Message).
        events_dir = conv_dir / "events"
        if not events_dir.is_dir():
            return
        for path in sorted(events_dir.glob("event-*.json")):
            try:
                ev = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            kind = ev.get("kind") or ""
            if kind == "ActionEvent":
                thought_parts = []
                for t in ev.get("thought") or []:
                    if isinstance(t, dict) and t.get("text"):
                        thought_parts.append(t["text"])
                for t in ev.get("thinking_blocks") or []:
                    if isinstance(t, dict) and (t.get("thinking") or t.get("text")):
                        thought_parts.append(t.get("thinking") or t.get("text"))
                if thought_parts:
                    self._cur["thinking"].extend(thought_parts)
                tool = ev.get("tool_name") or (ev.get("action") or {}).get("kind") or "action"
                summary = ev.get("summary") or ""
                action = ev.get("action") or {}
                detail = summary or json.dumps(action, ensure_ascii=False)[:600]
                self._item("tool", f"{tool} · {detail}", tool=str(tool))
                self._log(f"tool · {tool} {_clip(detail, 90)}")
                self._close_turn()
            elif kind == "MessageEvent" and (ev.get("source") or "") == "agent":
                msg = ev.get("llm_message") or {}
                texts = []
                for block in msg.get("content") or []:
                    if isinstance(block, dict) and block.get("text"):
                        texts.append(block["text"])
                if texts:
                    body = "\n\n".join(texts)
                    self._item("message", body)
                    self._log(f"assistant · {_clip(body)}")
                    self._close_turn()
            elif kind == "ObservationEvent":
                obs = ev.get("observation") or {}
                contents = []
                for block in obs.get("content") or []:
                    if isinstance(block, dict) and block.get("text"):
                        contents.append(block["text"])
                # Fold observation into the previous tool node if possible.
                if contents and self.turns:
                    prev = self.turns[-1]
                    prev["detail"] = (prev.get("detail") or "") + "\n\n" + "\n".join(contents)[:3000]

    # ── output ───────────────────────────────────────────────────────────

    def _log(self, s: str) -> None:
        self.lines.append(s)

    def output(self) -> str:
        return "\n".join(self.lines)

    def input_total(self, input_tokens: Any, cache_read: Any = 0, cache_write: Any = 0) -> int:
        """Every input token the model read, cached or not, for this stream."""
        total = int(input_tokens or 0)
        if not self.input_includes_cache:
            total += int(cache_read or 0) + int(cache_write or 0)
        return total

    def uncached_input(self, input_tokens: Any, cache_read: Any = 0, cache_write: Any = 0) -> int:
        """Input billed at the plain input rate (cache reads/writes excluded).

        Cost estimates price cache reads and writes at their own rates, so
        Codex-semantics input (which already contains them) must drop them
        first or they are charged twice.
        """
        total = int(input_tokens or 0)
        if self.input_includes_cache:
            total -= int(cache_read or 0) + int(cache_write or 0)
        return max(0, total)

    def total_input_tokens(self) -> int:
        """Run-level input including cache reads/writes (see ``input_total``)."""
        u = self.usage
        return self.input_total(
            u.get("input_tokens"), u.get("cache_read_tokens"), u.get("cache_write_tokens")
        )

    def final_message(self) -> str:
        """Return the latest normalized assistant message, if one was parsed."""
        for turn in reversed(self.turns):
            if "message" in turn.get("kinds", set()) and str(turn.get("detail") or "").strip():
                return str(turn["detail"]).strip()
        return ""

    # ── per-turn agent nodes ─────────────────────────────────────────────

    def _item(self, kind: str, text: str, tool: str | None = None) -> None:
        self._cur["kinds"].add(kind)
        self._cur["detail"].append(text.strip())
        if tool:
            name = str(tool).strip()[:60]
            self._cur.setdefault("tools", []).append(name)
            self.tool_calls += 1
            self.tool_events.append({"name": name, "args": text.strip()[:160], "state": "done"})
            del self.tool_events[:-20]

    def _close_turn(self, *, input_tokens: int = 0, output_tokens: int = 0,
                    cache_read: int = 0, cache_write: int = 0,
                    reasoning: int = 0, model: str | None = None,
                    model_source: str | None = None) -> None:
        cur = self._cur
        if not cur["detail"] and not cur["thinking"]:
            # Usage-only close (codex reports usage once per turn, after the
            # per-item nodes were already closed) -> fold into the last node.
            if (input_tokens or output_tokens) and self.turns:
                last = self.turns[-1]
                last["input_tokens"] += input_tokens
                last["output_tokens"] += output_tokens
                last["cache_read_tokens"] += cache_read
                last["cache_write_tokens"] += cache_write
                last["reasoning_tokens"] += reasoning
                # The usage's model, unless the node already names one.  Only
                # the model: it is not evidence of who wrote the node's text,
                # so model_source stays as it was.
                if model and not last.get("model"):
                    last["model"] = model
            return
        self.turns.append(
            {
                "kinds": set(cur["kinds"]),
                "tools": list(cur.get("tools") or []),
                "detail": "\n\n".join(cur["detail"])[:8000],
                "thinking": "\n\n".join(cur["thinking"])[:8000],
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_tokens": cache_read,
                "cache_write_tokens": cache_write,
                "reasoning_tokens": reasoning,
                "model": model or self.usage.get("model"),
                "model_source": model_source,
                "pipeline_stage": self.pipeline_stage,
            }
        )
        self._cur = self._new_turn()

    def agent_nodes(self, run_id: str, *, prompt: str = "", status: str = "running") -> list[dict[str, Any]]:
        """Turn accumulated per-turn records into Monitor agent dicts."""
        nodes: list[dict[str, Any]] = []
        for i, t in enumerate(self.turns, 1):
            kinds = t["kinds"]
            if kinds & {"exec", "edit", "tool"}:
                stage, role = "act", "tools"
            elif "message" in kinds:
                stage, role = "write", "assistant"
            else:
                stage, role = "plan", "reasoning"
            if t.get("pipeline_stage"):
                stage = t["pipeline_stage"]
            elif self.source_engine in PIPELINE_STAGE_ENGINES:
                # Output before the first stage event is the pipeline header.
                stage = "scaffold"
            label = "+".join(sorted(kinds)) or "call"
            nodes.append(
                {
                    "trace_id": f"{run_id}::turn-{i}",
                    "stage_name": f"turn-{i} ({label})",
                    "role": role,
                    "pipeline_stage": stage,
                    "call_seq": i,
                    "round_id": i,
                    "prompt": prompt[:4000] if i == 1
                    else "(conversation continues — model sees the original task, all prior turns and tool results)",
                    "prompt_source": "task prompt" if i == 1 else "session context",
                    "thinking": t.get("thinking") or "",
                    "thinking_source": f"{self.engine} stream" if t.get("thinking") else None,
                    "output": t["detail"] or f"(model call · {label})",
                    "output_source": f"{self.engine} stream-json",
                    "status": "finished",
                    "tools": t.get("tools") or [],
                    "model": t.get("model"),
                    "model_source": t.get("model_source"),
                    "input_tokens": t["input_tokens"] or None,
                    "total_input_tokens": self.input_total(
                        t["input_tokens"], t["cache_read_tokens"], t["cache_write_tokens"]
                    ),
                    "output_tokens": t["output_tokens"] or None,
                    "cache_read_tokens": t["cache_read_tokens"] or 0,
                    "cache_write_tokens": t["cache_write_tokens"] or 0,
                    "reasoning_tokens": t["reasoning_tokens"] or None,
                }
            )
        # Open (in-flight) turn -> live node so the graph moves in real time.
        if status == "running" and self._cur["detail"]:
            i = len(self.turns) + 1
            nodes.append(
                {
                    "trace_id": f"{run_id}::turn-{i}",
                    "stage_name": f"turn-{i} (running)",
                    "role": "agent",
                    "pipeline_stage": self.pipeline_stage or "act",
                    "call_seq": i,
                    "round_id": i,
                    "output": "\n\n".join(self._cur["detail"])[:8000],
                    "status": "running",
                }
            )
        return nodes


def _claude_api_error_message(ev: dict) -> bool:
    """A Claude Code (2.1.x) synthetic API-error assistant event."""
    if ev.get("is_api_error_message") is True:
        return True
    msg = ev.get("message") or {}
    if not isinstance(msg, dict) or str(msg.get("model") or "").strip() != "<synthetic>":
        return False
    usage = msg.get("usage") or {}
    try:
        return not any(
            int(usage.get(name) or 0) > 0
            for name in ("input_tokens", "output_tokens")
        )
    except (TypeError, ValueError, AttributeError):
        return False


def _clip(s: Any, n: int = 160) -> str:
    s = str(s or "").replace("\n", " ").strip()
    return s[: n - 1] + "…" if len(s) > n else s
