"""Provider-aware one-shot API backend for non-agentic proof runners."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


_OPENAI_COMPATIBLE_PROVIDERS = frozenset(
    {"openrouter", "deepseek", "groq", "together", "xai"}
)


class APIBackendError(RuntimeError):
    """Raised when a direct provider call cannot produce a response.

    ``failure`` carries the content-free facts of a failed sponsored Kimi call
    (``kimi_k3.CallFailure``; ``None`` otherwise).  ``usage`` totals the 2xx
    replies that came back before the call failed (an empty completion is
    still billed).
    """

    def __init__(
        self,
        message: str,
        *,
        failure: object | None = None,
        usage: dict[str, int] | None = None,
    ) -> None:
        super().__init__(message)
        self.failure = failure
        self.usage = dict(usage or {})


@dataclass(frozen=True)
class APIResult:
    text: str
    usage: dict[str, int]
    model: str
    provider: str
    # The provider response, never the requested/routing model.
    observed_model: str | None = None


def _safe_error(detail: object, secret: str) -> str:
    text = str(detail or "unknown error")
    if secret:
        text = text.replace(secret, "[redacted]")
    return text[-1600:]


def _kimi_usage(body: Mapping[str, object]) -> dict[str, int]:
    from agent_monitor import kimi_k3

    try:
        return kimi_k3.response_usage(body)
    except (TypeError, ValueError):
        return {}


def _total_usage(parts: list[dict[str, int]]) -> dict[str, int]:
    """Token counts summed over every billed reply of one call."""
    total: dict[str, int] = {}
    for usage in parts:
        for name, value in usage.items():
            total[name] = total.get(name, 0) + int(value)
    return total


def _chat_response_text(body: dict[str, object]) -> str:
    """Extract text from OpenAI-compatible Chat Completions responses."""
    choices = body.get("choices") or []
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0]
    if not isinstance(choice, dict):
        return ""
    message = choice.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("text") is not None:
            parts.append(str(block["text"]))
    return "\n".join(parts)


def api_chat(
    system: str,
    user: str,
    *,
    model: str,
    timeout: int = 240,
    effort: str | None = None,
    max_tokens: int | None = None,
    env: Mapping[str, str] | None = None,
    retry_allowance: object | None = None,
) -> APIResult:
    """Call the provider implied by ``model`` using the user's resolved env.

    Every per-call knob can be passed explicitly, so concurrent callers never
    need to mutate the process environment:

    * ``env`` -- the settings mapping to resolve the provider, credentials and
      limits from (default: ``os.environ``, read at call time).  It is only
      read, never modified.
    * ``effort`` -- Kimi reasoning effort (``low``/``high``/``max``) for this
      call; it overrides ``KIMI_REASONING_EFFORT``.  Other providers keep
      their validated run profile.
    * ``max_tokens`` -- requested output limit for this call instead of
      ``AGENT_MONITOR_API_MAX_TOKENS`` (same 1024..128000 clamp).  The job's
      ``AGENT_MONITOR_MAX_OUTPUT_TOKENS`` ceiling still applies and can only
      lower it.
    * ``timeout`` -- the call's total seconds.  A server-sponsored Kimi call
      sends it as ``X-Ansatze-Timeout`` so the broker and gateway answer
      first, and retries only replies ``kimi_k3.sponsored_retryable``
      accepts, within this deadline.
    * ``retry_allowance`` -- a ``kimi_k3.RetryAllowance`` shared by every
      call of one run, capping that run's sponsored transport retries.
    """
    from agent_monitor.proof_graph import (
        _api_error,
        _openai_response_text,
        _resolve_llm_provider,
    )
    from agent_monitor.settings import _http_json

    source: Mapping[str, str] = env if env is not None else os.environ
    if effort is not None:
        from agent_monitor import kimi_k3

        effort = str(effort).strip().lower()
        if effort not in kimi_k3.ALLOWED_REASONING_EFFORTS:
            raise APIBackendError("reasoning effort must be one of low, high or max")

    try:
        provider, base, key, chosen = _resolve_llm_provider(dict(source), model)
    except (TypeError, ValueError) as exc:
        raise APIBackendError(str(exc)) from exc

    if max_tokens is not None and (
        isinstance(max_tokens, bool) or not isinstance(max_tokens, int)
    ):
        raise APIBackendError("max_tokens must be an integer")
    try:
        requested = (
            max_tokens
            if max_tokens is not None
            else int(source.get("AGENT_MONITOR_API_MAX_TOKENS") or 16384)
        )
        max_output = max(1024, min(requested, 128000))
    except ValueError as exc:
        raise APIBackendError("AGENT_MONITOR_API_MAX_TOKENS must be an integer") from exc

    # A bounded run (currently anonymous guest sessions) carries a hard output
    # ceiling. It only ever lowers the limit above, and it is not negotiable by
    # the caller's own AGENT_MONITOR_API_MAX_TOKENS.
    ceiling_raw = str(source.get("AGENT_MONITOR_MAX_OUTPUT_TOKENS") or "").strip()
    if ceiling_raw:
        try:
            ceiling = int(ceiling_raw)
        except ValueError as exc:
            raise APIBackendError(
                "AGENT_MONITOR_MAX_OUTPUT_TOKENS must be an integer"
            ) from exc
        if ceiling > 0:
            max_output = min(max_output, ceiling)

    if provider == "openai":
        url = f"{base}/responses"
        headers = {"Authorization": f"Bearer {key}"}
        payload = {
            "model": chosen,
            "instructions": system,
            "input": user,
            "max_output_tokens": max_output,
            "store": False,
        }
        from agent_monitor.run_tuning import openai_payload_options

        payload.update(openai_payload_options(source, chosen))
    elif provider == "kimi":
        from agent_monitor import kimi_k3

        url = f"{base}/chat/completions"
        headers = {"Authorization": f"Bearer {key}"}
        payload = kimi_k3.chat_payload(
            system=system,
            user=user,
            effort=effort,
            env=source,
            max_tokens=max_output,
        )
    elif provider == "anthropic":
        api_base = base if base.endswith("/v1") else f"{base}/v1"
        url = f"{api_base}/messages"
        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model": chosen,
            "system": system,
            "max_tokens": max_output,
            "messages": [{"role": "user", "content": user}],
        }
    elif provider == "google":
        api_base = base if base.endswith("/v1beta") else f"{base}/v1beta"
        url = f"{api_base}/models/{chosen}:generateContent?key={key}"
        headers = {}
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [
                {"role": "user", "parts": [{"text": user}]},
            ],
            "generationConfig": {"maxOutputTokens": max_output},
        }
    elif provider in _OPENAI_COMPATIBLE_PROVIDERS:
        api_base = base if base.endswith("/v1") else f"{base}/v1"
        url = f"{api_base}/chat/completions"
        headers = {"Authorization": f"Bearer {key}"}
        payload = {
            "model": chosen,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_output,
        }
    else:
        raise APIBackendError(
            f"Direct {provider} calls are not yet supported by this harness; "
            "choose a provider shown in Settings"
        )

    from agent_monitor import kimi_k3

    sponsored = provider == "kimi" and kimi_k3.is_sponsored(source)
    # Every 2xx Kimi reply is billed, including an empty completion that a
    # retry replaced, so the call's usage is the total of all of them.
    billed: list[dict[str, int]] = []

    def request(seconds: float | None = None) -> tuple[int, object]:
        limit = timeout if seconds is None else seconds
        call_headers = headers
        if sponsored:
            call_headers = {
                **headers,
                kimi_k3.TIMEOUT_HEADER: kimi_k3.timeout_header_value(limit),
            }
        code, body = _http_json(
            url,
            method="POST",
            headers=call_headers,
            payload=payload,
            timeout=limit,
        )
        if provider == "kimi" and 200 <= code < 300 and isinstance(body, dict):
            billed.append(_kimi_usage(body))
        return code, body

    failure = None
    try:
        if sponsored:
            # Server-sponsored traffic is admitted and billed per logical model
            # call, whether anonymous or registered.  Only a failure that
            # certainly never reached processing is retried (see
            # kimi_k3.sponsored_retryable); an ambiguous timeout, 5xx or empty
            # response is never replayed, since the upstream may already have
            # processed it.
            code, body, failure = kimi_k3.sponsored_request(
                request,
                timeout=timeout,
                allowance=retry_allowance,  # type: ignore[arg-type]
            )
        elif provider == "kimi":
            # User-key runs retain the existing bounded reliability retries.
            code, body = kimi_k3.request_with_retry(request)
        else:
            code, body = request()
    except Exception as exc:
        # Gemini authenticates in its query string.  Unexpected transport
        # exceptions can include the full URL, so suppress the original cause
        # after projecting a key-redacted diagnostic.
        raise APIBackendError(
            f"{provider.title()} model transport failed for {chosen}: "
            f"{_safe_error(exc, key)}",
            usage=_total_usage(billed),
        ) from None
    if failure is not None:
        raise APIBackendError(
            kimi_k3.failure_message(failure)
            or (
                f"{provider.title()} model call failed for {chosen} (HTTP {code}): "
                f"{_safe_error(_api_error(body), key)}"
            ),
            failure=failure,
            usage=_total_usage(billed),
        )
    if code != 200 or not isinstance(body, dict):
        detail = _api_error(body)
        raise APIBackendError(
            f"{provider.title()} model call failed for {chosen} (HTTP {code}): "
            f"{_safe_error(detail, key)}",
            failure=kimi_k3.call_failure(code, body) if sponsored else None,
            usage=_total_usage(billed),
        )

    try:
        if provider == "openai":
            text = _openai_response_text(body)
            raw_usage = body.get("usage") or {}
            usage = {
                "input_tokens": int(raw_usage.get("input_tokens") or 0),
                "output_tokens": int(raw_usage.get("output_tokens") or 0),
            }
        elif provider == "kimi":
            text = kimi_k3.response_text(body)
            usage = _total_usage(billed)
        elif provider == "anthropic":
            text = "\n".join(
                str(block.get("text") or "")
                for block in body.get("content") or []
                if isinstance(block, dict) and block.get("type") == "text"
            )
            raw_usage = body.get("usage") or {}
            usage = {
                "input_tokens": int(raw_usage.get("input_tokens") or 0),
                "output_tokens": int(raw_usage.get("output_tokens") or 0),
            }
        elif provider == "google":
            text = "\n".join(
                str(part.get("text") or "")
                for candidate in body.get("candidates") or []
                if isinstance(candidate, dict)
                for part in ((candidate.get("content") or {}).get("parts") or [])
                if isinstance(part, dict) and part.get("text")
            )
            raw_usage = body.get("usageMetadata") or {}
            usage = {
                "input_tokens": int(raw_usage.get("promptTokenCount") or 0),
                "output_tokens": int(raw_usage.get("candidatesTokenCount") or 0),
            }
        else:
            text = _chat_response_text(body)
            raw_usage = body.get("usage") or {}
            if not isinstance(raw_usage, dict):
                raw_usage = {}
            usage = {
                "input_tokens": int(raw_usage.get("prompt_tokens") or 0),
                "output_tokens": int(raw_usage.get("completion_tokens") or 0),
            }
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise APIBackendError(
            f"{provider.title()} returned an unexpected response shape for {chosen}"
        ) from exc
    if not text.strip():
        detail = ""
        if provider == "kimi":
            # finish_reason and token counts only, for diagnosing budget
            # exhaustion; never response or prompt text.
            summarize = getattr(kimi_k3, "empty_completion_summary", None)
            detail = f" ({summarize(body)})" if summarize is not None else ""
        raise APIBackendError(
            f"{provider.title()} returned an empty response for {chosen}{detail}",
            usage=usage,
        )
    return APIResult(
        text=text.strip(), usage=usage, model=chosen, provider=provider,
        observed_model=(body["model"].strip() or None) if isinstance(body.get("model"), str) else None
    )
