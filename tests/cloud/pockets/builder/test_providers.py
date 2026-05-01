# Tests for ``ee.cloud.pockets.builder.providers``.
#
# Created 2026-05-01.  Four cases per design §12 — anthropic happy / retry /
# two-failure plus the plain-text JSON extractor.  Each test stubs the
# Anthropic SDK rather than reaching the network.

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from ee.cloud.pockets.builder import providers as providers_mod
from ee.cloud.pockets.builder.providers import ProviderError, structured_call


class _Schema(BaseModel):
    answer: str
    score: int = 0


class _FakeContent:
    """Mirror of an Anthropic ``content`` block — only the bits we read."""

    def __init__(self, type_: str, text: str = "", input_payload: Any = None) -> None:
        self.type = type_
        self.text = text
        self.input = input_payload


class _FakeMessage:
    def __init__(self, content: list[_FakeContent]) -> None:
        self.content = content


class _FakeAnthropicMessages:
    def __init__(self, responses: list[_FakeMessage]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _FakeMessage:
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _FakeAnthropicClient:
    def __init__(self, responses: list[_FakeMessage]) -> None:
        self.messages = _FakeAnthropicMessages(responses)


def _install_fake_anthropic(
    monkeypatch: pytest.MonkeyPatch, responses: list[_FakeMessage]
) -> _FakeAnthropicClient:
    client = _FakeAnthropicClient(responses)

    def _factory(*_args: Any, **_kwargs: Any) -> _FakeAnthropicClient:
        return client

    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _factory)
    return client


def _settings_with_anthropic_key() -> Any:
    """Return a lightweight stand-in object with the fields providers reads."""

    class _Settings:
        anthropic_api_key = "sk-ant-test"
        anthropic_model = "claude-haiku-4-5-20251001"

    return _Settings()


@pytest.mark.asyncio
async def test_anthropic_happy_path_returns_validated_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [
        _FakeMessage(
            [_FakeContent("tool_use", input_payload={"answer": "yes", "score": 7})]
        )
    ]
    _install_fake_anthropic(monkeypatch, responses)

    out = await structured_call(
        "anthropic",
        _Schema,
        [{"role": "user", "content": "ping"}],
        settings=_settings_with_anthropic_key(),
    )
    assert isinstance(out, _Schema)
    assert out.answer == "yes"
    assert out.score == 7


@pytest.mark.asyncio
async def test_anthropic_retries_on_first_parse_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # First response fails validation (missing required field); second succeeds.
    responses = [
        _FakeMessage([_FakeContent("tool_use", input_payload={"score": "bad"})]),
        _FakeMessage(
            [_FakeContent("tool_use", input_payload={"answer": "ok", "score": 1})]
        ),
    ]
    client = _install_fake_anthropic(monkeypatch, responses)

    out = await structured_call(
        "anthropic",
        _Schema,
        [{"role": "user", "content": "ping"}],
        settings=_settings_with_anthropic_key(),
    )
    assert isinstance(out, _Schema)
    assert out.answer == "ok"
    # Second call must have included the correction turn.
    assert len(client.messages.calls) == 2
    second_call_messages = client.messages.calls[1]["messages"]
    assert any(
        "not valid JSON" in (m.get("content") or "")
        for m in second_call_messages
        if m.get("role") == "user"
    )


@pytest.mark.asyncio
async def test_anthropic_raises_after_two_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [
        _FakeMessage([_FakeContent("tool_use", input_payload={"score": "x"})]),
        _FakeMessage([_FakeContent("tool_use", input_payload={"unrelated": True})]),
    ]
    _install_fake_anthropic(monkeypatch, responses)

    with pytest.raises(ProviderError) as info:
        await structured_call(
            "anthropic",
            _Schema,
            [{"role": "user", "content": "ping"}],
            settings=_settings_with_anthropic_key(),
        )
    assert info.value.code == "parse_failed_twice"


def test_extract_json_object_pulls_from_prose() -> None:
    text = (
        "Sure, here's the answer:\n\n"
        '```\n{"answer": "hi", "score": 3}\n```\n'
        "Hope that helps!"
    )
    extracted = providers_mod._extract_json_object(text)
    assert extracted is not None
    assert '"answer"' in extracted
    assert '"score"' in extracted
