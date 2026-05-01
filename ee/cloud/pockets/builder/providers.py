# Pockets builder — provider-agnostic structured-output adapter.
#
# Created 2026-05-01.  Public surface is a single ``structured_call`` async
# function that takes a Pydantic schema, a list of messages, and a
# ``provider`` string and returns a validated schema instance.  Dispatches
# to one of four backend impls:
#   - Anthropic (tool_use with forced ``tool_choice``)
#   - OpenAI / OpenAI-compatible (response_format=json_schema)
#   - Ollama native (httpx POST /api/chat with format=schema_json)
#   - Plain-text fallback (codex_cli, copilot_sdk, deep_agents, opencode, other)
#
# Errors surface as ``ProviderError`` so the SSE handler can map them to
# user-visible ``error`` events without exposing raw provider internals.

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from pocketpaw.config import Settings

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    """Builder-side provider failure surfaced to the SSE stream."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


# Fast-model hints per provider.  Used for the classifier call where
# latency matters more than spec quality.
_FAST_MODEL_HINTS: dict[str, str] = {
    "anthropic": "claude-haiku-4-5-20251001",
}


def _fast_model_for(provider: str, fallback: str | None) -> str | None:
    return _FAST_MODEL_HINTS.get(provider, fallback)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def structured_call(
    provider: str,
    schema: type[BaseModel],
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    settings: Settings | None = None,
) -> BaseModel:
    """Call an LLM provider with structured-output constraints and return a
    validated ``schema`` instance.

    Raises ``ProviderError`` on:
      - missing API key (``no_key``)
      - HTTP / API failure (``api_error``)
      - two consecutive Pydantic validation failures (``parse_failed_twice``)

    Retries once with a correction message when the first parse fails.
    """
    if settings is None:
        settings = Settings.load()

    if provider == "anthropic":
        return await _anthropic_call(schema, messages, model, settings)
    if provider in ("openai", "openai_compatible", "openrouter", "litellm"):
        return await _openai_call(schema, messages, model, provider, settings)
    if provider == "ollama":
        return await _ollama_native_call(schema, messages, model, settings)
    # codex_cli, copilot_sdk, deep_agents, opencode, anything else
    return await _plain_text_call(schema, messages, provider, model, settings)


# ---------------------------------------------------------------------------
# Anthropic dispatch
# ---------------------------------------------------------------------------


async def _anthropic_call(
    schema: type[BaseModel],
    messages: list[dict[str, Any]],
    model: str | None,
    settings: Settings,
) -> BaseModel:
    api_key = settings.anthropic_api_key
    if not api_key:
        raise ProviderError(
            "no_key",
            "Pocket creation requires an API key for anthropic. Add one in Settings.",
        )

    try:
        from anthropic import AsyncAnthropic
    except ImportError as exc:  # pragma: no cover - dep is required
        raise ProviderError(
            "no_key",
            f"anthropic SDK not installed: {exc}",
        ) from exc

    used_model = model or _fast_model_for("anthropic", settings.anthropic_model)
    json_schema = schema.model_json_schema()

    # Anthropic tool_use expects a flat ``input_schema`` — the model emits
    # exactly one tool call shaped like the schema.  We force ``tool_choice``.
    system_prompt, anthropic_messages = _split_system(messages)

    tool = {
        "name": "emit_result",
        "description": "Return the structured result.",
        "input_schema": json_schema,
    }

    client = AsyncAnthropic(api_key=api_key)
    return await _call_with_retry(
        schema,
        lambda turn_messages: _anthropic_invoke(
            client, used_model, system_prompt, turn_messages, tool
        ),
        anthropic_messages,
    )


async def _anthropic_invoke(
    client: Any,
    model: str,
    system_prompt: str,
    messages: list[dict[str, Any]],
    tool: dict[str, Any],
) -> str:
    """Single Anthropic round-trip; returns the tool_use input as a JSON string."""
    try:
        resp = await client.messages.create(
            model=model,
            system=system_prompt or "",
            messages=messages,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
            max_tokens=2048,
        )
    except Exception as exc:  # noqa: BLE001
        raise ProviderError("api_error", f"anthropic call failed: {exc}") from exc

    for block in getattr(resp, "content", []) or []:
        btype = getattr(block, "type", None)
        if btype == "tool_use":
            payload = getattr(block, "input", None)
            if payload is None:
                continue
            return json.dumps(payload)
    raise ProviderError(
        "api_error", "anthropic response contained no tool_use block"
    )


# ---------------------------------------------------------------------------
# OpenAI / OpenAI-compatible dispatch
# ---------------------------------------------------------------------------


async def _openai_call(
    schema: type[BaseModel],
    messages: list[dict[str, Any]],
    model: str | None,
    provider: str,
    settings: Settings,
) -> BaseModel:
    api_key, base_url, used_model = _openai_credentials(provider, model, settings)
    if not api_key and provider != "openai_compatible":
        raise ProviderError(
            "no_key",
            f"Pocket creation requires an API key for {provider}. "
            f"Add one in Settings.",
        )

    try:
        from openai import AsyncOpenAI
    except ImportError as exc:  # pragma: no cover
        raise ProviderError("no_key", f"openai SDK not installed: {exc}") from exc

    client_kwargs: dict[str, Any] = {"api_key": api_key or "missing"}
    if base_url:
        client_kwargs["base_url"] = str(base_url)
    client = AsyncOpenAI(**client_kwargs)

    json_schema = schema.model_json_schema()
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "emit_result",
            "strict": False,
            "schema": json_schema,
        },
    }

    return await _call_with_retry(
        schema,
        lambda turn_messages: _openai_invoke(
            client, used_model, turn_messages, response_format
        ),
        messages,
    )


def _openai_credentials(
    provider: str, model: str | None, settings: Settings
) -> tuple[str | None, str | None, str]:
    if provider == "openai":
        return settings.openai_api_key, None, model or settings.openai_model
    if provider == "openrouter":
        return (
            settings.openrouter_api_key,
            "https://openrouter.ai/api/v1",
            model or settings.openrouter_model,
        )
    if provider == "litellm":
        return (
            settings.litellm_api_key,
            str(settings.litellm_api_base) if settings.litellm_api_base else None,
            model or settings.litellm_model,
        )
    # openai_compatible
    return (
        settings.openai_compatible_api_key,
        str(settings.openai_compatible_base_url)
        if settings.openai_compatible_base_url
        else None,
        model or getattr(settings, "openai_compatible_model", None) or settings.openai_model,
    )


async def _openai_invoke(
    client: Any,
    model: str,
    messages: list[dict[str, Any]],
    response_format: dict[str, Any],
) -> str:
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=messages,
            response_format=response_format,
        )
    except Exception as exc:  # noqa: BLE001
        raise ProviderError("api_error", f"openai call failed: {exc}") from exc

    try:
        content = resp.choices[0].message.content
    except (AttributeError, IndexError) as exc:
        raise ProviderError("api_error", f"openai response malformed: {exc}") from exc
    if not content:
        raise ProviderError("api_error", "openai response had empty content")
    return content


# ---------------------------------------------------------------------------
# Ollama native dispatch
# ---------------------------------------------------------------------------


async def _ollama_native_call(
    schema: type[BaseModel],
    messages: list[dict[str, Any]],
    model: str | None,
    settings: Settings,
) -> BaseModel:
    host = settings.ollama_host or "http://localhost:11434"
    used_model = model or settings.ollama_model
    json_schema = schema.model_json_schema()

    return await _call_with_retry(
        schema,
        lambda turn_messages: _ollama_invoke(
            host, used_model, turn_messages, json_schema
        ),
        messages,
    )


async def _ollama_invoke(
    host: str,
    model: str,
    messages: list[dict[str, Any]],
    json_schema: dict[str, Any],
) -> str:
    url = f"{host.rstrip('/')}/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "format": json_schema,
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise ProviderError("api_error", f"ollama call failed: {exc}") from exc

    msg = data.get("message", {})
    content = msg.get("content", "") if isinstance(msg, dict) else ""
    if not content:
        raise ProviderError("api_error", "ollama response had empty content")
    return content


# ---------------------------------------------------------------------------
# Plain-text fallback (codex_cli, copilot_sdk, deep_agents, opencode, other)
# ---------------------------------------------------------------------------


async def _plain_text_call(
    schema: type[BaseModel],
    messages: list[dict[str, Any]],
    provider: str,
    model: str | None,
    settings: Settings,
) -> BaseModel:
    """Fallback for backends without a native structured-output API.

    The provider clients these backends use are diverse and not all expose
    a Python async API; for the structured-output use case we route through
    the cheapest reachable provider that has a key.  In rank order:
      anthropic → openai → openai_compatible → ollama
    """
    candidate_providers = []
    if settings.anthropic_api_key:
        candidate_providers.append("anthropic")
    if settings.openai_api_key:
        candidate_providers.append("openai")
    if settings.openai_compatible_api_key:
        candidate_providers.append("openai_compatible")
    candidate_providers.append("ollama")  # always available locally

    last_err: ProviderError | None = None
    for fallback in candidate_providers:
        try:
            logger.debug(
                "plain-text fallback: provider=%s using=%s", provider, fallback
            )
            return await structured_call(
                fallback, schema, messages, model=model, settings=settings
            )
        except ProviderError as exc:
            last_err = exc
            if exc.code == "no_key":
                continue
            raise
    if last_err is not None:
        raise last_err
    raise ProviderError(
        "no_key",
        f"Pocket creation requires an API key reachable from {provider}.",
    )


# ---------------------------------------------------------------------------
# Plain-text JSON extraction (used by tests and the plain-text path when the
# raw text contains a JSON block surrounded by prose)
# ---------------------------------------------------------------------------


def _extract_json_object(text: str) -> str | None:
    """Return the first balanced ``{...}`` JSON object found in ``text``."""
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start >= 0:
                return text[start : i + 1]
    return None


# ---------------------------------------------------------------------------
# Retry / validation helper
# ---------------------------------------------------------------------------


async def _call_with_retry(
    schema: type[BaseModel],
    invoker: Any,
    messages: list[dict[str, Any]],
) -> BaseModel:
    """Run ``invoker(messages) -> str`` and validate the JSON.  On parse
    failure, append a correction turn and retry once.  Two failures raise
    ``ProviderError(code='parse_failed_twice')``."""
    raw = await invoker(messages)
    parsed_or_err = _try_parse(schema, raw)
    if isinstance(parsed_or_err, BaseModel):
        return parsed_or_err

    # First parse failed; retry once with correction prompt.
    correction_messages = [
        *messages,
        {"role": "assistant", "content": raw},
        {
            "role": "user",
            "content": (
                "Your previous response was not valid JSON matching the "
                "schema. Return only the JSON object, no prose, no code "
                "fences."
            ),
        },
    ]
    raw2 = await invoker(correction_messages)
    parsed_or_err2 = _try_parse(schema, raw2)
    if isinstance(parsed_or_err2, BaseModel):
        return parsed_or_err2

    raise ProviderError(
        "parse_failed_twice",
        "Could not generate a valid spec — the AI response did not match "
        "the expected schema after one retry.",
    )


def _try_parse(schema: type[BaseModel], raw: str) -> BaseModel | Exception:
    """Try to parse ``raw`` as the schema.  Returns the model instance on
    success or the underlying exception on failure (the caller decides
    whether to retry)."""
    candidate = raw.strip()
    # Strip code fences if a model decided to wrap the JSON.
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        first_nl = candidate.find("\n")
        if first_nl > 0:
            candidate = candidate[first_nl + 1 :]
        if candidate.endswith("```"):
            candidate = candidate[:-3]
        candidate = candidate.strip()
    extracted = _extract_json_object(candidate) or candidate
    try:
        data = json.loads(extracted)
    except (json.JSONDecodeError, ValueError) as exc:
        return exc
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        return exc


def _split_system(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Split the leading ``system`` messages from the rest.  Anthropic's
    Messages API takes ``system`` as a top-level kwarg, not in the message
    list."""
    system_parts: list[str] = []
    rest: list[dict[str, Any]] = []
    for m in messages:
        if m.get("role") == "system":
            system_parts.append(str(m.get("content", "")))
        else:
            rest.append(m)
    return ("\n\n".join(system_parts), rest)


__all__ = ["ProviderError", "structured_call"]
