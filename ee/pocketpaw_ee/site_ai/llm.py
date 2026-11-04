# ee/pocketpaw_ee/site_ai/llm.py — the one place a site AI call reaches a model.
#
# Talks OpenAI chat-completions to our LiteLLM proxy (``litellm_api_base`` with the
# deployment key) and normalises what comes back into ``LlmEvent``s / ``LlmResult``
# so the rest of the package never touches SDK types. ``stream`` and ``complete``
# are the test seam.
#
# Payloads are built by the caller from validated fields only; ``build_payload``
# adds the server-side pieces: the provider model, the output cap, ``user`` = the
# paying workspace (LiteLLM end-user spend attribution, never the caller's
# ``user``) and ``metadata.tags`` naming the site. No retries here (``max_retries=0``):
# the caller retries once, and only before the first token.
#
# Failures become ``LlmError(transient)`` carrying no provider text: an SDK error
# message can quote the provider model or request, and none of that may reach a
# response or a log line. Embeddings go through ``embed`` on the same client.

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

TIMEOUT_S = 120.0
_CLIENT: Any = None
_CLIENT_KEY: tuple[str, str] | None = None


class LlmError(Exception):
    def __init__(self, transient: bool, kind: str = "error", status: int | None = None) -> None:
        self.transient = transient
        self.kind = kind
        self.status = status
        super().__init__(kind)


@dataclass
class LlmEvent:
    kind: str  # "text" | "tool" | "finish" | "usage"
    text: str = ""
    index: int = 0
    id: str | None = None
    name: str | None = None
    arguments: str | None = None
    reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class LlmResult:
    content: str | None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str = "stop"
    input_tokens: int = 0
    output_tokens: int = 0


def _client() -> Any:
    global _CLIENT, _CLIENT_KEY
    from openai import AsyncOpenAI

    from pocketpaw.config import get_settings

    settings = get_settings()
    base = str(settings.litellm_api_base or "").rstrip("/")
    key = str(settings.litellm_api_key or "") or "sk-none"
    if _CLIENT is None or _CLIENT_KEY != (base, key):
        _CLIENT = AsyncOpenAI(base_url=base, api_key=key, max_retries=0, timeout=TIMEOUT_S)
        _CLIENT_KEY = (base, key)
    return _CLIENT


def build_payload(
    *,
    provider_model: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    workspace_id: str,
    tags: list[str],
    temperature: float | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
    response_format: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": provider_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "user": workspace_id,
        "extra_body": {"metadata": {"tags": tags}},
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if tools:
        payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
    if response_format is not None:
        payload["response_format"] = response_format
    return payload


def _classify(exc: BaseException) -> LlmError:
    import openai

    status = getattr(exc, "status_code", None)
    if isinstance(exc, openai.APITimeoutError | openai.APIConnectionError):
        return LlmError(True, "connection")
    if isinstance(status, int):
        if status in (408, 429) or status >= 500:
            return LlmError(True, "http", status)
        if status == 400 and "content" in type(exc).__name__.lower():
            return LlmError(False, "content_filter", status)
        return LlmError(False, "http", status)
    return LlmError(False, type(exc).__name__)


def _finish(reason: Any) -> str:
    if reason in ("stop", "length", "tool_calls", "content_filter"):
        return reason
    if reason == "function_call":
        return "tool_calls"
    return "stop"


def _usage(obj: Any) -> tuple[int, int]:
    usage = getattr(obj, "usage", None)
    if usage is None:
        return 0, 0
    return int(getattr(usage, "prompt_tokens", 0) or 0), int(
        getattr(usage, "completion_tokens", 0) or 0
    )


async def stream(payload: dict[str, Any]) -> AsyncIterator[LlmEvent]:
    """Stream one completion as ``LlmEvent``s; raises ``LlmError``."""
    try:
        response = await _client().chat.completions.create(
            **payload, stream=True, stream_options={"include_usage": True}
        )
        async for chunk in response:
            for choice in getattr(chunk, "choices", None) or ():
                delta = getattr(choice, "delta", None)
                text = getattr(delta, "content", None) if delta is not None else None
                if text:
                    yield LlmEvent("text", text=text)
                for call in (getattr(delta, "tool_calls", None) or ()) if delta else ():
                    fn = getattr(call, "function", None)
                    yield LlmEvent(
                        "tool",
                        index=int(getattr(call, "index", 0) or 0),
                        id=getattr(call, "id", None),
                        name=getattr(fn, "name", None) if fn else None,
                        arguments=getattr(fn, "arguments", None) if fn else None,
                    )
                if getattr(choice, "finish_reason", None):
                    yield LlmEvent("finish", reason=_finish(choice.finish_reason))
            if getattr(chunk, "usage", None) is not None:
                tokens_in, tokens_out = _usage(chunk)
                yield LlmEvent("usage", input_tokens=tokens_in, output_tokens=tokens_out)
    except LlmError:
        raise
    except Exception as exc:  # noqa: BLE001 - classified, never echoed
        raise _classify(exc) from None


async def complete(payload: dict[str, Any]) -> LlmResult:
    """One non-streamed completion; raises ``LlmError``."""
    try:
        response = await _client().chat.completions.create(**payload)
    except Exception as exc:  # noqa: BLE001 - classified, never echoed
        raise _classify(exc) from None
    choice = (getattr(response, "choices", None) or [None])[0]
    message = getattr(choice, "message", None)
    calls = []
    for call in getattr(message, "tool_calls", None) or ():
        fn = getattr(call, "function", None)
        calls.append(
            {
                "id": str(getattr(call, "id", "") or ""),
                "type": "function",
                "function": {
                    "name": str(getattr(fn, "name", "") or ""),
                    "arguments": str(getattr(fn, "arguments", "") or ""),
                },
            }
        )
    tokens_in, tokens_out = _usage(response)
    return LlmResult(
        content=getattr(message, "content", None),
        tool_calls=calls,
        finish_reason=_finish(getattr(choice, "finish_reason", None)),
        input_tokens=tokens_in,
        output_tokens=tokens_out,
    )


async def embed(*, provider_model: str, inputs: list[str], workspace_id: str, tags: list[str]):
    """Embed ``inputs``; returns (vectors, input_tokens). Raises ``LlmError``."""
    try:
        response = await _client().embeddings.create(
            model=provider_model,
            input=inputs,
            user=workspace_id,
            extra_body={"metadata": {"tags": tags}},
        )
    except Exception as exc:  # noqa: BLE001 - classified, never echoed
        raise _classify(exc) from None
    rows = sorted(getattr(response, "data", None) or [], key=lambda d: getattr(d, "index", 0))
    vectors = [list(getattr(d, "embedding", []) or []) for d in rows]
    usage = getattr(response, "usage", None)
    return vectors, int(getattr(usage, "prompt_tokens", 0) or 0)


__all__ = ["LlmError", "LlmEvent", "LlmResult", "build_payload", "complete", "embed", "stream"]
