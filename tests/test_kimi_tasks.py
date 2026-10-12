import json

import pytest

from agent_monitor import kimi_tasks
from agent_monitor import sponsored_kimi_client as client
from agent_monitor.kimi_tasks import DailyBudget, KimiTaskError, call_json, exact_quote, extract_json_object


def reply(content, finish="stop"):
    return {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}]}


class Recorder:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.bodies = []

    def __call__(self, body, *, client_id, cache_key, timeout):
        self.bodies.append(json.loads(json.dumps(body)))
        self.kwargs = {"client_id": client_id, "cache_key": cache_key, "timeout": timeout}
        item = self.replies.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def keep_items(value):
    if set(value) != {"items"} or not isinstance(value["items"], list):
        raise ValueError("schema")
    return value


def test_success_returns_validated_object_and_sends_strict_request():
    fake = Recorder(reply('{"items": [1, 2]}'))
    result = call_json("triage", "Rank the items.", {"items": [{"id": "a"}]}, client_id="ansatze-news",
                       cache_key="news", validate=keep_items, request=fake, max_tokens=2048, effort="low", timeout=90)
    assert result == {"items": [1, 2]}
    body = fake.bodies[0]
    assert body["model"] == "kimi-k3" and body["stream"] is False and body["max_tokens"] == 2048
    assert body["reasoning_effort"] == "low" and "tools" not in body
    system, user = body["messages"]
    assert system["role"] == "system" and kimi_tasks.UNTRUSTED_DATA_RULE in system["content"]
    assert json.loads(user["content"]) == {"task": "triage", "data": {"items": [{"id": "a"}]}}
    assert fake.kwargs == {"client_id": "ansatze-news", "cache_key": "news", "timeout": 90.0}


@pytest.mark.parametrize("content", ['```json\n{"items": []}\n```', '```\n{"items": []}\n```'])
def test_fenced_json_is_accepted(content):
    assert call_json("t", "s", {}, client_id="c", cache_key="k", validate=keep_items,
                     request=Recorder(reply(content))) == {"items": []}


@pytest.mark.parametrize("content", ["Sure! {\"items\": []}", "[1, 2]", "", "```python\n{}\n```", "x" * 70_000])
def test_non_json_replies_are_invalid(content):
    with pytest.raises(KimiTaskError) as caught:
        call_json("t", "s", {}, client_id="c", cache_key="k", validate=keep_items, request=Recorder(reply(content)))
    assert caught.value.kind == "invalid"


def test_validator_rejection_is_invalid_and_not_retried():
    fake = Recorder(reply('{"other": 1}'), reply('{"items": []}'))
    with pytest.raises(KimiTaskError) as caught:
        call_json("t", "s", {}, client_id="c", cache_key="k", validate=keep_items, request=fake)
    assert caught.value.kind == "invalid" and len(fake.bodies) == 1


def test_empty_completion_retries_once_at_lower_effort():
    fake = Recorder(reply(""), reply('{"items": []}'))
    assert call_json("t", "s", {}, client_id="c", cache_key="k", validate=keep_items, request=fake,
                     effort="high") == {"items": []}
    assert [b["reasoning_effort"] for b in fake.bodies] == ["high", "low"]


def test_empty_completion_at_low_effort_is_invalid():
    fake = Recorder(reply(""), reply('{"items": []}'))
    with pytest.raises(KimiTaskError) as caught:
        call_json("t", "s", {}, client_id="c", cache_key="k", validate=keep_items, request=fake, effort="low")
    assert caught.value.kind == "invalid" and len(fake.bodies) == 1


def test_truncated_reply_is_invalid():
    with pytest.raises(KimiTaskError) as caught:
        call_json("t", "s", {}, client_id="c", cache_key="k", validate=keep_items,
                  request=Recorder(reply('{"items": []}', finish="length")))
    assert caught.value.kind == "invalid"


def test_budget_is_taken_per_attempt_and_persists_in_state():
    state = {}
    budget = DailyBudget(state, 2, today="2026-10-11")
    fake = Recorder(reply(""), reply('{"items": []}'), reply('{"items": []}'))
    call_json("t", "s", {}, client_id="c", cache_key="k", validate=keep_items, request=fake, effort="high", budget=budget)
    assert state == {"day": "2026-10-11", "calls": 2}
    with pytest.raises(KimiTaskError) as caught:
        call_json("t", "s", {}, client_id="c", cache_key="k", validate=keep_items, request=fake, budget=budget)
    assert caught.value.kind == "budget" and len(fake.bodies) == 2
    assert DailyBudget(state, 2, today="2026-10-12").remaining() == 2 and state["calls"] == 0


def test_zero_cap_never_calls():
    fake = Recorder()
    with pytest.raises(KimiTaskError) as caught:
        call_json("t", "s", {}, client_id="c", cache_key="k", validate=keep_items, request=fake,
                  budget=DailyBudget({}, 0))
    assert caught.value.kind == "budget" and fake.bodies == []


@pytest.mark.parametrize(("error", "kind"), [
    (client.SponsoredKimiError("Sponsored Kimi request failed: Sponsored Kimi call quota reached"), "refused"),
    (client.SponsoredKimiError("Sponsored Kimi service is temporarily unavailable"), "unavailable"),
    (RuntimeError("provider said sk-secret-123456789 and the prompt"), "unavailable"),
])
def test_failures_are_public_safe(error, kind):
    with pytest.raises(KimiTaskError) as caught:
        call_json("t", "s", {}, client_id="c", cache_key="k", validate=keep_items, request=Recorder(error))
    assert caught.value.kind == kind
    assert "sk-secret" not in str(caught.value) and "prompt" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_oversized_payload_is_refused_before_any_call():
    fake = Recorder()
    with pytest.raises(KimiTaskError) as caught:
        call_json("t", "s", {"text": "x" * 200_000}, client_id="c", cache_key="k", validate=keep_items, request=fake)
    assert caught.value.kind == "invalid" and fake.bodies == []


def test_unknown_effort_is_refused():
    with pytest.raises(KimiTaskError):
        call_json("t", "s", {}, client_id="c", cache_key="k", validate=keep_items, request=Recorder(), effort="medium")


def test_exact_quote_requires_verbatim_substring():
    source = "The  Kourovka Notebook marks problem 12.20\nwith a sharp sign."
    assert exact_quote("marks problem 12.20 with a sharp", source) == "marks problem 12.20 with a sharp"
    for bad in ("marks problem 12.21", "short", 42, "x" * 300):
        with pytest.raises(ValueError):
            exact_quote(bad, source)


def test_extract_json_object_rejects_arrays_and_trailing_text():
    assert extract_json_object(' {"a": 1} ') == {"a": 1}
    for bad in ('{"a": 1} extra', "[]", None):
        with pytest.raises(KimiTaskError):
            extract_json_object(bad)


def test_default_request_uses_loopback_broker_with_deadline_header(monkeypatch):
    seen = {}
    monkeypatch.setattr(client, "issue_credentials", lambda **kw: seen.setdefault("issue", kw) and {"KIMI_API_KEY": "tok"} or {"KIMI_API_KEY": "tok"})

    def fake_request(path, **kw):
        seen["path"], seen["kw"] = path, kw
        return reply('{"items": []}'), ""

    monkeypatch.setattr(client, "_request", fake_request)
    assert call_json("t", "s", {}, client_id="ansatze-problems", cache_key="problems", validate=keep_items,
                     timeout=100) == {"items": []}
    assert seen["issue"] == {"engine": "plain", "client_id": "ansatze-problems", "cache_key": "problems",
                             "minimum_ttl_seconds": 130}
    assert seen["path"] == "/internal/v1/chat/completions"
    assert seen["kw"]["bearer"] == "tok" and seen["kw"]["timeout"] == 100.0
    assert seen["kw"]["extra_headers"] == {"X-Ansatze-Timeout": "100"}


def test_empty_or_cut_off_replies_are_marked_incomplete_and_schema_breaks_are_not():
    for content, finish in (('{"items": []', "length"), ("", "stop"), ("   ", "stop")):
        with pytest.raises(KimiTaskError) as caught:
            call_json("t", "s", {}, client_id="c", cache_key="k", validate=keep_items, request=Recorder(reply(content, finish)))
        assert caught.value.kind == "invalid" and caught.value.incomplete is True, (content, finish)
    with pytest.raises(KimiTaskError) as caught:
        call_json("t", "s", {}, client_id="c", cache_key="k", validate=keep_items, request=Recorder(reply('{"other": 1}')))
    assert caught.value.kind == "invalid" and caught.value.incomplete is False
