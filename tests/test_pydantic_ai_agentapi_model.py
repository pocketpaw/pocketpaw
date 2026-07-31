"""Tests for AgentAPI-as-a-pydantic-ai-model (the keyless dev path).

Hermetic — no AgentAPI server, no network. The SSE stream is scripted, because
what matters is how this reacts to frames a real terminal UI emits: full-message
re-sends rather than deltas, wrapped tip blocks, and padding to terminal height.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("pydantic_ai", reason="pocketpaw[pydantic-ai] not installed")

from pocketpaw.agents.pydantic_ai_agentapi import (  # noqa: E402
    AgentAPIError,
    AgentAPIModel,
    clean_frame,
)


class _Resp:
    def __init__(self, payload, status=200, text=""):
        self._payload, self.status_code, self.text = payload, status, text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(str(self.status_code))


class _Stream:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            await asyncio.sleep(0)
            yield line


class _Client:
    def __init__(self, *, messages=None, events=(), post_statuses=None, get_raises=False):
        self._messages = messages or []
        self._events = list(events)
        self._post_statuses = list(post_statuses or [200])
        self._get_raises = get_raises
        self.posts: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False

    async def get(self, path):
        if self._get_raises:
            raise ConnectionError("refused")
        return _Resp({"messages": self._messages})

    async def post(self, path, json=None):  # noqa: A002
        self.posts.append(json or {})
        status = self._post_statuses.pop(0) if self._post_statuses else 200
        text = "waiting for user input" if status >= 400 else ""
        return _Resp({"ok": True}, status=status, text=text)

    def stream(self, method, path):
        return _Stream(self._events)


def _sse(kind, payload):
    return [f"event: {kind}", f"data: {json.dumps(payload)}", ""]


def _frames(*texts, mid=2, role="agent"):
    out = []
    for t in texts:
        out += _sse("message_update", {"id": mid, "role": role, "message": t})
    return out


def _install(monkeypatch, client):
    import httpx

    real_sleep = asyncio.sleep
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)
    # Capture the REAL sleep first — a lambda that calls asyncio.sleep would
    # recurse into its own patch.
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: real_sleep(0))
    return client


async def _text(model, prompt="hi"):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    msgs = [ModelRequest(parts=[UserPromptPart(content=prompt)])]
    resp = await model.request(msgs)
    return "".join(p.content for p in resp.parts if type(p).__name__ == "TextPart")


# -- frame cleaning ---------------------------------------------------------


def test_clean_frame_strips_bullet_status_and_padding():
    raw = "● CLEAN-OK        \n                  \n✻ Crunched for 2s "
    assert clean_frame(raw) == "CLEAN-OK"


def test_clean_frame_consumes_a_wrapped_tip_block():
    """A continuation block WRAPS — only its first line carries the glyph.

    Dropping the glyph line alone leaked a tip's tail into an answer,
    mid-sentence, right before the real reply: "...workflow directly.391".
    """
    raw = (
        "  ⎿ \xa0Tip: control how big a workflow is by prompting. Ask for a\n"
        "     small workflow, or set a default with Dynamic workflow size.\n"
        "● 391\n"
        "✻ Cooked for 2s"
    )
    assert clean_frame(raw) == "391"


def test_clean_frame_collapses_terminal_height_padding():
    assert clean_frame("● hi\n" + "\n" * 50) == "hi"


def test_clean_frame_keeps_paragraph_breaks():
    assert clean_frame("● one\n\ntwo") == "one\n\ntwo"


# -- model behaviour --------------------------------------------------------


async def test_request_returns_the_cleaned_answer(monkeypatch):
    _install(
        monkeypatch,
        _Client(
            events=[
                *_sse("status_change", {"status": "running"}),
                *_frames("● Hel", "● Hello world"),
                *_sse("status_change", {"status": "stable"}),
            ]
        ),
    )
    assert await _text(AgentAPIModel("claude")) == "Hello world"


async def test_only_the_newest_prompt_is_sent(monkeypatch):
    """The AgentAPI server owns history; replaying ours would duplicate turns."""
    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

    client = _install(
        monkeypatch,
        _Client(
            events=[
                *_sse("status_change", {"status": "running"}),
                *_frames("● ok"),
                *_sse("status_change", {"status": "stable"}),
            ]
        ),
    )
    msgs = [
        ModelRequest(parts=[UserPromptPart(content="old question")]),
        ModelResponse(parts=[TextPart(content="old answer")]),
        ModelRequest(parts=[UserPromptPart(content="new question")]),
    ]
    await AgentAPIModel("claude").request(msgs)
    assert client.posts == [{"content": "new question", "type": "user"}]


async def test_startup_banner_is_excluded_by_id(monkeypatch):
    """Excluded by message id, not by matching box glyphs that change per release."""
    _install(
        monkeypatch,
        _Client(
            messages=[{"id": 0, "role": "agent", "content": "banner"}],
            events=[
                *_sse("status_change", {"status": "running"}),
                *_frames("● stale redraw", mid=0),
                *_frames("● fresh answer", mid=2),
                *_sse("status_change", {"status": "stable"}),
            ],
        ),
    )
    out = await _text(AgentAPIModel("claude"))
    assert out == "fresh answer"
    assert "stale" not in out


async def test_busy_agent_is_retried(monkeypatch):
    """AgentAPI 500s while the CLI is mid-task or on a permission prompt.

    GET /status is NOT a sufficient readiness check — it reported "stable" live
    while the agent refused input — so retrying the POST is what works.
    """
    client = _install(
        monkeypatch,
        _Client(
            post_statuses=[500, 500, 200],
            events=[
                *_sse("status_change", {"status": "running"}),
                *_frames("● done"),
                *_sse("status_change", {"status": "stable"}),
            ],
        ),
    )
    assert await _text(AgentAPIModel("claude")) == "done"
    assert len(client.posts) == 3


async def test_permanently_busy_agent_says_where_to_look(monkeypatch):
    _install(monkeypatch, _Client(post_statuses=[500] * 20))
    with pytest.raises(AgentAPIError, match="permission prompt"):
        await _text(AgentAPIModel("claude"))


async def test_unreachable_server_explains_how_to_start_one(monkeypatch):
    _install(monkeypatch, _Client(get_raises=True))
    with pytest.raises(AgentAPIError, match="agentapi server"):
        await _text(AgentAPIModel("claude"))


# -- backend wiring ---------------------------------------------------------


def test_backend_builds_an_agentapi_model():
    """The whole point: the REAL pydantic_ai backend, only the model swapped."""
    from pocketpaw.agents.pydantic_ai import PydanticAIBackend
    from pocketpaw.config import Settings

    backend = PydanticAIBackend(
        Settings(pydantic_ai_model="agentapi:claude", agentapi_base_url="http://localhost:3284")
    )
    model = backend._build_model()
    assert isinstance(model, AgentAPIModel)
    assert model.model_name == "claude"
    assert model.base_url == "http://localhost:3284"


def test_agentapi_needs_no_provider_key():
    """The reason this path exists — no credential of any kind is consulted."""
    from pocketpaw.agents.pydantic_ai import PydanticAIBackend
    from pocketpaw.config import Settings

    s = Settings(pydantic_ai_model="agentapi:claude")
    s.anthropic_api_key = s.openai_api_key = s.litellm_api_key = None
    assert isinstance(PydanticAIBackend(s)._build_model(), AgentAPIModel)


async def test_agentapi_refuses_a_surface_that_gates_tools():
    """The wrapped CLI does its OWN tool use, below this backend.

    Observed live 2026-07-31 on /sites: it wrote a landing page to the SERVER's
    disk and blocked on a permission prompt in the `agentapi server` terminal,
    having never called a sites tool. Nothing above the model seam can prevent
    that — the tools it uses were never ours — so the only honest move is to
    refuse the run instead of letting a wrong-machine write read as success.
    """
    from pocketpaw.agents.pydantic_ai import PydanticAIBackend
    from pocketpaw.config import Settings

    backend = PydanticAIBackend(
        Settings(pydantic_ai_model="agentapi:claude", pydantic_ai_skills_enabled=False)
    )
    backend._mcp_tools = []
    backend._custom_tools = []

    events = [
        e async for e in backend.run("build me a site", deny_mcp_tool_ids=frozenset({"Bash"}))
    ]
    errors = [e for e in events if e.type == "error"]
    assert errors, "a gated surface on agentapi must not run"
    assert "text-only development model" in errors[0].content
    assert "openrouter:" in errors[0].content, "must name the way out"
    assert events[-1].type == "done", "the run still has to terminate cleanly"


async def test_an_ungated_surface_still_runs_on_agentapi(monkeypatch):
    """The dev path stays usable for plain chat — that is the whole point."""
    from pocketpaw.agents.pydantic_ai import PydanticAIBackend
    from pocketpaw.config import Settings

    _install(
        monkeypatch,
        _Client(
            events=[
                *_sse("status_change", {"status": "running"}),
                *_frames("● hello"),
                *_sse("status_change", {"status": "stable"}),
            ]
        ),
    )
    backend = PydanticAIBackend(
        Settings(pydantic_ai_model="agentapi:claude", pydantic_ai_skills_enabled=False)
    )
    backend._mcp_tools = []
    backend._custom_tools = []

    events = [e async for e in backend.run("hi")]
    assert not [e for e in events if e.type == "error"]
    assert "hello" in "".join(e.content for e in events if e.type == "message")
