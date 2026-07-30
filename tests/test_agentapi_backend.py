"""Tests for the AgentAPI backend (coder/agentapi).

Created 2026-07-30.

Everything here is hermetic — no AgentAPI server and no network. The SSE stream
is scripted, because the interesting behaviour is how this backend reacts to
frames a real terminal UI produces: full-message re-sends, redraws that make a
frame diverge from the last one, and padding to the terminal height.

The regression that motivated most of these: the first live run leaked a startup
tip banner and ~50 trailing newlines into the answer. The fix was in
``_clean_frame`` — a mutation probe showed the append-only delta logic makes no
difference to the final text, so the cleaner is what these tests pin.
"""

from __future__ import annotations

import asyncio
import json

from pocketpaw.agents.agentapi import AgentAPIBackend, _clean_frame
from pocketpaw.config import Settings


def _settings(**overrides) -> Settings:
    base = {"agentapi_base_url": "http://localhost:3284"}
    base.update(overrides)
    return Settings(**base)


# --------------------------------------------------------------------------
# fake AgentAPI server
# --------------------------------------------------------------------------


class _FakeStream:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            await asyncio.sleep(0)  # let the caller interleave, like a real stream
            yield line


class _Resp:
    def __init__(self, payload, status=200, text=""):
        self._payload = payload
        self.status_code = status
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    """Stands in for ``httpx.AsyncClient`` against an AgentAPI server."""

    def __init__(self, *, messages=None, events=(), post_status=200, get_raises=False):
        self._messages = messages if messages is not None else []
        self._events = list(events)
        self._post_status = post_status
        self._get_raises = get_raises
        self.posted: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, path):
        if self._get_raises:
            raise ConnectionError("connection refused")
        if path == "/messages":
            return _Resp({"messages": self._messages})
        if path == "/status":
            return _Resp({"status": "stable", "agent_type": "claude", "transport": "pty"})
        raise AssertionError(path)

    async def post(self, path, json=None):  # noqa: A002
        assert path == "/message"
        self.posted.append(json or {})
        return _Resp({"ok": True}, status=self._post_status, text="rejected")

    def stream(self, method, path):
        assert (method, path) == ("GET", "/events")
        return _FakeStream(self._events)


def _sse(kind: str, payload: dict) -> list[str]:
    return [f"event: {kind}", f"data: {json.dumps(payload)}", ""]


def _frames(*texts, mid=2, role="agent"):
    out: list[str] = []
    for t in texts:
        out += _sse("message_update", {"id": mid, "role": role, "message": t})
    return out


def _no_sleep(monkeypatch):
    """Make retry backoff instant. Captures the REAL sleep first — patching with
    a lambda that calls asyncio.sleep recurses into the patch."""
    real = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: real(0))


def _install(monkeypatch, client):
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)
    return client


async def _run(backend, message="hi"):
    return [ev async for ev in backend.run(message)]


# --------------------------------------------------------------------------
# registry / protocol
# --------------------------------------------------------------------------


def test_backend_is_registered():
    from pocketpaw.agents.registry import get_backend_class, list_backends

    assert "agentapi" in list_backends()
    assert get_backend_class("agentapi") is AgentAPIBackend


def test_satisfies_agent_backend_protocol():
    from pocketpaw.agents.backend import AgentBackend

    assert isinstance(AgentAPIBackend(_settings()), AgentBackend)


def test_declares_no_tool_capability():
    """The wrapped agent brings its own tools; ToolPolicy cannot govern them.

    Advertising TOOLS/MCP here would imply PocketPaw's policy applies to what
    the CLI can do, which is false and would be a misleading security claim.
    """
    caps = AgentAPIBackend.info().capabilities
    from pocketpaw.agents.backend import Capability

    assert Capability.STREAMING in caps
    assert Capability.TOOLS not in caps
    assert Capability.MCP not in caps
    assert AgentAPIBackend.info().builtin_tools == []


# --------------------------------------------------------------------------
# frame cleaning
# --------------------------------------------------------------------------


def test_clean_frame_strips_bullet_status_and_padding():
    raw = (
        "● CLEAN-OK                              \n"
        "                                        \n"
        "✻ Crunched for 2s                       "
    )
    assert _clean_frame(raw) == "CLEAN-OK"


def test_clean_frame_drops_tip_and_tool_continuations():
    raw = "  ⎿ \xa0Tip: you can do a thing\n● real answer\n✻ Cooked for 1s"
    assert _clean_frame(raw) == "real answer"


def test_clean_frame_collapses_terminal_height_padding():
    """A two-word answer must not arrive followed by fifty newlines."""
    raw = "● hi\n" + "\n" * 50
    assert _clean_frame(raw) == "hi"


def test_clean_frame_keeps_paragraph_breaks():
    assert _clean_frame("● one\n\ntwo") == "one\n\ntwo"


# --------------------------------------------------------------------------
# streaming
# --------------------------------------------------------------------------


async def test_streams_appending_frames_as_deltas(monkeypatch):
    client = _install(
        monkeypatch,
        _FakeClient(
            messages=[{"id": 0, "role": "agent", "content": "banner"}],
            events=[
                *_sse("status_change", {"status": "running"}),
                *_frames("● Hel", "● Hello", "● Hello world"),
                *_sse("status_change", {"status": "stable"}),
            ],
        ),
    )
    events = await _run(AgentAPIBackend(_settings()))

    assert [e.type for e in events] == ["message", "message", "message", "done"]
    assert "".join(e.content for e in events if e.type == "message") == "Hello world"
    assert client.posted == [{"content": "hi", "type": "user"}]


async def test_tip_banner_never_reaches_the_answer(monkeypatch):
    """The bug from the first live run, and what actually fixed it.

    A live turn leaked a startup tip banner and ~50 trailing newlines into the
    answer. The fix was ``_clean_frame`` dropping ⎿ continuations and collapsing
    the PTY's height padding — NOT the append-only delta logic, which a mutation
    probe showed makes no difference to the final text.
    """
    _install(
        monkeypatch,
        _FakeClient(
            events=[
                *_sse("status_change", {"status": "running"}),
                *_frames("● Hello", "⎿ Tip: something\n● Hello world"),
                *_sse("status_change", {"status": "stable"}),
            ],
        ),
    )
    text = "".join(
        e.content for e in await _run(AgentAPIBackend(_settings())) if e.type == "message"
    )
    assert text == "Hello world", text
    assert "Tip:" not in text
    assert text.count("Hello") == 1


async def test_final_text_survives_a_divergent_frame(monkeypatch):
    """A redraw must not lose the answer.

    Divergent frames are held back mid-stream, so the end-of-turn flush is what
    guarantees the caller still receives the final text.
    """
    _install(
        monkeypatch,
        _FakeClient(
            events=[
                *_sse("status_change", {"status": "running"}),
                *_frames("● first draft", "● completely different answer"),
                *_sse("status_change", {"status": "stable"}),
            ],
        ),
    )
    text = "".join(
        e.content for e in await _run(AgentAPIBackend(_settings())) if e.type == "message"
    )
    assert "completely different answer" in text


async def test_pre_existing_messages_are_not_replayed(monkeypatch):
    """The startup banner must not be reported as this turn's answer.

    Excluded by message id rather than by pattern-matching box-drawing glyphs,
    which change between CLI releases.
    """
    _install(
        monkeypatch,
        _FakeClient(
            messages=[
                {"id": 0, "role": "agent", "content": "╭── Claude Code v2 ──╮"},
                {"id": 1, "role": "user", "content": "earlier"},
                {"id": 2, "role": "agent", "content": "● earlier answer"},
            ],
            events=[
                *_sse("status_change", {"status": "running"}),
                # id 2 is at the baseline — a redraw of it must be ignored.
                *_frames("● earlier answer redrawn", mid=2),
                *_frames("● new answer", mid=4),
                *_sse("status_change", {"status": "stable"}),
            ],
        ),
    )
    text = "".join(
        e.content for e in await _run(AgentAPIBackend(_settings())) if e.type == "message"
    )
    assert text == "new answer", text
    assert "earlier" not in text


async def test_user_echo_frames_are_ignored(monkeypatch):
    _install(
        monkeypatch,
        _FakeClient(
            events=[
                *_sse("status_change", {"status": "running"}),
                *_frames("my own prompt", mid=3, role="user"),
                *_frames("● the answer", mid=4),
                *_sse("status_change", {"status": "stable"}),
            ],
        ),
    )
    text = "".join(
        e.content for e in await _run(AgentAPIBackend(_settings())) if e.type == "message"
    )
    assert text == "the answer"


# --------------------------------------------------------------------------
# failure modes
# --------------------------------------------------------------------------


async def test_unreachable_server_explains_how_to_start_one(monkeypatch):
    _install(monkeypatch, _FakeClient(get_raises=True))
    events = await _run(AgentAPIBackend(_settings()))

    assert [e.type for e in events] == ["error", "done"]
    assert "agentapi server -- claude" in events[0].content


async def test_rejected_message_surfaces_as_error(monkeypatch):
    _install(monkeypatch, _FakeClient(post_status=500))
    events = await _run(AgentAPIBackend(_settings()))
    assert events[0].type == "error"
    assert events[-1].type == "done"


async def test_system_prompt_only_prefixes_a_fresh_conversation(monkeypatch):
    """The wrapped CLI has no system-prompt channel, so it rides the first message.

    Re-sending it every turn would pollute the agent's own context, which the
    AgentAPI server owns.
    """
    fresh = _install(monkeypatch, _FakeClient(messages=[]))
    backend = AgentAPIBackend(_settings())
    [ev async for ev in backend.run("hello", system_prompt="You are Paw.")]
    assert fresh.posted[0]["content"].startswith("You are Paw.")

    ongoing = _install(
        monkeypatch,
        _FakeClient(messages=[{"id": i, "role": "agent", "content": "x"} for i in range(3)]),
    )
    backend2 = AgentAPIBackend(_settings())
    [ev async for ev in backend2.run("hello", system_prompt="You are Paw.")]
    assert ongoing.posted[0]["content"] == "hello"


async def test_turns_are_serialised(monkeypatch):
    """One server is ONE terminal — overlapping turns would interleave into it.

    This is a correctness property of the integration, not a throughput knob,
    and it is why this backend is unsuitable for multi-tenant serving.
    """
    order: list[str] = []

    class _Tracking(_FakeClient):
        async def post(self, path, json=None):  # noqa: A002
            order.append("start")
            await asyncio.sleep(0.05)
            order.append("end")
            return await super().post(path, json=json)

    client = _Tracking(
        events=[
            *_sse("status_change", {"status": "running"}),
            *_frames("● ok"),
            *_sse("status_change", {"status": "stable"}),
        ]
    )
    _install(monkeypatch, client)

    backend = AgentAPIBackend(_settings())
    await asyncio.gather(_run(backend, "a"), _run(backend, "b"))

    # Serialised: start/end never nest.
    assert order == ["start", "end", "start", "end"], order


async def test_status_reports_the_wrapped_agent(monkeypatch):
    _install(monkeypatch, _FakeClient())
    status = await AgentAPIBackend(_settings()).get_status()
    assert status["backend"] == "agentapi"
    assert status["available"] is True
    assert status["agent_type"] == "claude"
    assert status["transport"] == "pty"


async def test_status_degrades_when_the_server_is_down(monkeypatch):
    _install(monkeypatch, _FakeClient(get_raises=True))
    status = await AgentAPIBackend(_settings()).get_status()
    assert status["available"] is False
    assert "error" in status


def test_clean_frame_consumes_a_wrapped_continuation_block():
    """A ⎿ block wraps — only its FIRST line carries the glyph.

    Dropping just the glyph line leaks the tail of every startup tip into the
    answer. Seen live, mid-sentence, immediately before the real reply:
    "...workflow directly.391".
    """
    raw = (
        "  ⎿ \xa0Tip: You can control how big a workflow is just by prompting. Ask for a\n"
        '     small workflow, cap it with "use at most 5 agents", or set a default with\n'
        "     Dynamic workflow size in /config.\n"
        "● 391\n"
        "✻ Cooked for 2s"
    )
    assert _clean_frame(raw) == "391"


def test_clean_frame_continuation_block_ends_at_column_zero():
    raw = "  ⎿ tool output\n     more output\n● the answer\n     indented answer line"
    assert _clean_frame(raw) == "the answer\n     indented answer line"


async def test_busy_agent_is_retried_then_succeeds(monkeypatch):
    """AgentAPI 500s while the wrapped CLI is mid-task; that is transient.

    Note GET /status is NOT a sufficient readiness check — it reported "stable"
    live while the agent was refusing input, so polling status and then posting
    still races. Retrying the POST is what actually works.
    """
    calls = {"n": 0}

    class _Busy(_FakeClient):
        async def post(self, path, json=None):  # noqa: A002
            calls["n"] += 1
            if calls["n"] < 3:
                return _Resp(
                    {},
                    status=500,
                    text='{"errors":[{"message":"failed to send message: message can only '
                    'be sent when the agent is waiting for user input"}]}',
                )
            return await super().post(path, json=json)

    _install(
        monkeypatch,
        _Busy(
            events=[
                *_sse("status_change", {"status": "running"}),
                *_frames("● done"),
                *_sse("status_change", {"status": "stable"}),
            ]
        ),
    )
    _no_sleep(monkeypatch)

    events = await _run(AgentAPIBackend(_settings()))
    assert calls["n"] == 3
    assert "".join(e.content for e in events if e.type == "message") == "done"


async def test_permanently_busy_agent_explains_where_to_look(monkeypatch):
    class _AlwaysBusy(_FakeClient):
        async def post(self, path, json=None):  # noqa: A002
            return _Resp({}, status=500, text='{"message":"waiting for user input"}')

    _install(monkeypatch, _AlwaysBusy())
    _no_sleep(monkeypatch)

    events = await _run(AgentAPIBackend(_settings()))
    assert events[0].type == "error"
    assert "permission prompt" in events[0].content
    assert events[-1].type == "done"


async def test_a_different_post_failure_is_not_retried(monkeypatch):
    """Only the busy condition is transient; other errors must surface at once."""
    calls = {"n": 0}

    class _Broken(_FakeClient):
        async def post(self, path, json=None):  # noqa: A002
            calls["n"] += 1
            return _Resp({}, status=400, text="malformed request")

    _install(monkeypatch, _Broken())
    events = await _run(AgentAPIBackend(_settings()))
    assert calls["n"] == 1, "a non-busy failure was retried"
    assert events[0].type == "error"
