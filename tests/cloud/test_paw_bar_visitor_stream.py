# tests/cloud/test_paw_bar_visitor_stream.py — the public concierge stream only
# carries what a website visitor is allowed to see.
# Created 2026-09-26 (fix/pawbar-visitor-stream-allowlist): POST /paw-bar/chat is
# a PUBLIC endpoint, and it used to relay every run-engine frame verbatim, so an
# anonymous visitor could read the model's reasoning (``thinking``), tool names,
# arguments and outputs (``tool_start`` / ``tool_result``), per-turn model and
# cost (``token_usage``, ``stream_end.usage``) and raw exception text on
# ``error``. These tests drive the real route with a fake executor that writes
# all of those frames to the in-memory transport and assert the SSE body holds
# none of them, while the text reply, sources and a terminal frame still arrive.
# Fixtures and helpers are reused from test_paw_bar_reply_sources.

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.cloud.test_paw_bar_reply_sources import (  # noqa: F401 — fixture import
    _chat,
    _site,
    _stub_kb_search,
    _widget,
    concierge_client,
)

_SECRETS = (
    "SECRET_REASONING",
    "SECRET_TOOL_NAME",
    "SECRET_TOOL_ARG",
    "SECRET_TOOL_OUTPUT",
    "SECRET_MODEL",
    "SECRET_BACKEND",
    "SECRET_EXC_TEXT",
    "SECRET_RIPPLE",
    "SECRET_ARTIFACT",
    "SECRET_FUTURE",
    "SECRET_THINKING_CHUNK",
    "SECRET_RUN_ID_REASON",
)

_INTERNAL_EVENTS = (
    "thinking",
    "tool_start",
    "tool_result",
    "token_usage",
    "ripple",
    "artifact",
    "some_future_event",
)


class _ScriptedExecutor:
    """Writes a scripted list of (event, data) frames to the transport."""

    def __init__(self, transport, frames) -> None:
        self.transport = transport
        self.frames = frames

    async def submit(self, spec) -> None:
        for name, data in self.frames:
            await self.transport.append_event(spec.run_id, name, data)


def _stub(monkeypatch, frames) -> None:
    from pocketpaw_ee.cloud.chat.runs.memory_stream import InMemoryStreamTransport

    transport = InMemoryStreamTransport()
    fake_exec = _ScriptedExecutor(transport, frames)
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.transport.get_stream_transport", lambda: transport
    )
    monkeypatch.setattr("pocketpaw_ee.cloud.chat.runs.executor.get_executor", lambda: fake_exec)

    async def _fake_create_run(spec):
        return SimpleNamespace(run_id=spec.run_id)

    monkeypatch.setattr("pocketpaw_ee.cloud.chat.runs.service.create_run", _fake_create_run)


async def _raw_chat(client, widget_id: str) -> str:
    from tests.cloud.test_paw_bar_reply_sources import _ORIGIN, _payload

    res = await client.post("/paw-bar/chat", json=_payload(widget_id), headers={"Origin": _ORIGIN})
    assert res.status_code == 200, res.text
    return res.text


def _assert_nothing_internal(body: str) -> None:
    for secret in _SECRETS:
        assert secret not in body, f"{secret} leaked to the visitor stream"
    for name in _INTERNAL_EVENTS:
        assert f"event: {name}\n" not in body, f"internal event {name!r} relayed"
    # Usage/cost/model fields never ride any frame.
    for key in ('"usage"', '"cost', '"model"', '"backend"', '"input_tokens"'):
        assert key not in body, f"{key} leaked to the visitor stream"


_LEAKY_RUN = [
    ("thinking", {"content": "SECRET_REASONING about the owner's config"}),
    ("tool_start", {"tool": "SECRET_TOOL_NAME", "input": {"q": "SECRET_TOOL_ARG"}}),
    ("tool_result", {"tool": "SECRET_TOOL_NAME", "output": "SECRET_TOOL_OUTPUT"}),
    (
        "token_usage",
        {
            "model": "SECRET_MODEL",
            "backend": "SECRET_BACKEND",
            "input_tokens": 1234,
            "output_tokens": 56,
            "cost_usd": 0.42,
        },
    ),
    ("chunk", {"content": "We open at 8am!", "type": "text"}),
    ("chunk", {"content": "SECRET_THINKING_CHUNK", "type": "thinking"}),
    ("ripple", {"spec": {"title": "SECRET_RIPPLE"}}),
    ("artifact", {"file_id": "f1", "name": "SECRET_ARTIFACT"}),
    ("some_future_event", {"anything": "SECRET_FUTURE"}),
    (
        "stream_end",
        {
            "assistant_message_id": "m1",
            "cancelled": False,
            "usage": {
                "model": "SECRET_MODEL",
                "backend": "SECRET_BACKEND",
                "input_tokens": 1234,
                "cost_usd": 0.42,
            },
        },
    ),
]


@pytest.mark.asyncio
async def test_visitor_stream_drops_internal_events_and_usage(concierge_client, monkeypatch):
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget())
    _stub(monkeypatch, _LEAKY_RUN)
    _stub_kb_search(monkeypatch, [{"id": "site-hours", "title": "Opening Hours"}])

    body = await _raw_chat(client, widget.id)
    _assert_nothing_internal(body)

    events = await _chat(client, widget.id)
    names = [n for n, _ in events]
    # Exactly the visitor-safe frames, in order.
    assert names == ["message.persisted", "chunk", "sources", "stream_end"]
    assert events[1][1] == {"content": "We open at 8am!", "type": "text"}
    assert events[3][1] == {"assistant_message_id": "m1", "cancelled": False}
    assert events[2][1]["sources"][0]["title"] == "Opening Hours"


@pytest.mark.asyncio
async def test_visitor_stream_error_is_generic(concierge_client, monkeypatch):
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget())
    _stub(
        monkeypatch,
        [
            ("tool_start", {"tool": "SECRET_TOOL_NAME", "input": {"q": "SECRET_TOOL_ARG"}}),
            ("chunk", {"content": "Let me check", "type": "text"}),
            (
                "error",
                {
                    "code": "agent.run_failed",
                    "message": "SECRET_EXC_TEXT: mongodb://admin:pw@10.0.0.4 refused",
                },
            ),
        ],
    )
    _stub_kb_search(monkeypatch, [])

    body = await _raw_chat(client, widget.id)
    _assert_nothing_internal(body)
    assert "mongodb://" not in body

    events = await _chat(client, widget.id)
    assert [n for n, _ in events] == ["message.persisted", "chunk", "error"]
    err = events[-1][1]
    assert set(err) == {"code", "message"}
    assert err["code"] == "agent.run_failed"
    assert isinstance(err["message"], str) and err["message"]


@pytest.mark.asyncio
async def test_visitor_stream_error_with_unsafe_code_is_replaced(concierge_client, monkeypatch):
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget())
    _stub(monkeypatch, [("error", {"code": "SECRET_EXC_TEXT <b>", "message": "boom"})])
    _stub_kb_search(monkeypatch, [])

    events = await _chat(client, widget.id)
    err = events[-1]
    assert err[0] == "error"
    assert "SECRET_EXC_TEXT" not in err[1]["code"]
    assert err[1]["message"] != "boom"


@pytest.mark.asyncio
async def test_visitor_stream_interrupted_carries_reason_only(concierge_client, monkeypatch):
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget())
    _stub(
        monkeypatch,
        [
            ("chunk", {"content": "Half a", "type": "text"}),
            ("interrupted", {"run_id": "SECRET_RUN_ID_REASON", "reason": "cancelled"}),
        ],
    )
    _stub_kb_search(monkeypatch, [])

    events = await _chat(client, widget.id)
    assert events[-1] == ("interrupted", {"reason": "cancelled"})
