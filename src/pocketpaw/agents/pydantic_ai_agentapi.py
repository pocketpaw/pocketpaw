"""AgentAPI as a pydantic-ai *model*, for development without a provider key.

Created 2026-07-30.

`AgentAPI <https://github.com/coder/agentapi>`_ runs a terminal coding agent
(Claude Code, Codex, Goose, …) under a PTY and exposes it over HTTP. This module
adapts that to pydantic-ai's ``Model`` interface, so the ``pydantic_ai`` backend
can drive it with ``POCKETPAW_PYDANTIC_AI_MODEL=agentapi:claude``.

**Why a model and not a backend.** The point of this dev path is to exercise the
REAL ``pydantic_ai`` code — its event mapping, its run lifecycle, its
cancellation, its capabilities — with no provider credential. A standalone
backend that talks to AgentAPI directly would duplicate all of that and test
none of it. Plugging in at the model seam means the only thing swapped is where
tokens come from; everything above it is the code we actually ship.

**The limitation, stated plainly: no tool calling.** AgentAPI wraps a complete
agent that does its own planning and tool use and hands back rendered prose. It
never emits structured tool calls, so pydantic-ai's tool loop cannot fire
through this model. Bridged PocketPaw tools, MCP tools and any harness
capability that depends on a tool call are inert here. That makes this a
text-only development model — good for exercising the plumbing, wrong for
testing tool behaviour, and not a serving path.

**One server is one conversation.** There is no session concept in the API, so
requests are serialised behind a lock; concurrent runs would interleave into a
single terminal. Message history is owned by the AgentAPI server, so only the
newest user prompt is sent — replaying our copy would duplicate every turn in
the wrapped agent's own context.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.settings import ModelSettings

DEFAULT_BASE_URL = "http://localhost:3284"

# Terminal chrome that is not the assistant talking. The spinner line rotates
# both glyph and verb between frames ("✻ Cooked for 2s", "✻ Crunched for 2s"),
# so it is matched by shape rather than text.
_STATUS_LINE = re.compile(r"^[✻✳✽✶✢*·]\s")
# Tool-result / startup-tip continuations.
_CONTINUATION = re.compile(r"^\s*⎿")
# The bullet the CLI puts in front of an assistant paragraph.
_BULLET = re.compile(r"^[●•]\s?")


def clean_frame(text: str) -> str:
    """Strip terminal chrome from one scraped agent frame.

    Best-effort and conservative. The startup banner is excluded by message id
    upstream rather than by matching box-drawing glyphs here — ids survive CLI
    restyling, glyph patterns do not.
    """
    out: list[str] = []
    # A ⎿ block WRAPS: only its first line carries the glyph, the rest are just
    # indented. Dropping the glyph line alone leaks the tail of every startup
    # tip into the answer — observed live, mid-sentence, immediately before the
    # real reply ("…workflow directly.391").
    in_continuation = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if _CONTINUATION.match(line):
            in_continuation = True
            continue
        if in_continuation:
            if line and line[0].isspace():
                continue
            in_continuation = False
        if _STATUS_LINE.match(line):
            continue
        if not line:
            # Collapse runs of blanks — the PTY pads every frame to the terminal
            # height, so otherwise a two-word answer arrives with fifty newlines.
            if out and not out[-1]:
                continue
            out.append("")
            continue
        out.append(_BULLET.sub("", line, count=1))
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def _latest_user_text(messages: list[ModelMessage]) -> str:
    """The newest user prompt. History belongs to the AgentAPI server."""
    for message in reversed(messages):
        for part in getattr(message, "parts", []):
            if type(part).__name__ == "UserPromptPart":
                content = part.content
                return content if isinstance(content, str) else str(content)
    return ""


class AgentAPIError(RuntimeError):
    """Raised when the AgentAPI server cannot accept or complete a turn."""


@dataclass
class AgentAPIStreamedResponse(StreamedResponse):
    """Streams one AgentAPI turn as pydantic-ai text deltas."""

    _model_name: str = ""
    _base_url: str = DEFAULT_BASE_URL
    _prompt: str = ""
    _timeout: float = 600.0
    _timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def provider_name(self) -> str:
        return "agentapi"

    @property
    def provider_url(self) -> str:
        return self._base_url

    @property
    def timestamp(self) -> datetime:
        return self._timestamp

    async def _get_event_iterator(self) -> AsyncIterator[Any]:
        async for delta in _stream_turn(self._base_url, self._prompt, self._timeout):
            for event in self._parts_manager.handle_text_delta(
                vendor_part_id="agentapi", content=delta
            ):
                yield event


@dataclass(init=False)
class AgentAPIModel(Model):
    """A pydantic-ai model backed by a coder/agentapi server.

    Text only — see the module docstring. ``model_name`` is informational (it
    labels which CLI the server wraps); the server decides what actually runs.
    """

    def __init__(
        self,
        model_name: str = "claude",
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 600.0,
    ) -> None:
        super().__init__()
        self._model_name = model_name or "claude"
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._timeout = timeout
        # One server is one terminal. This is a correctness lock, not a
        # throughput knob — overlapping turns interleave into one PTY.
        self._lock = asyncio.Lock()

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def system(self) -> str:
        return "agentapi"

    @property
    def base_url(self) -> str:
        return self._base_url

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None = None,  # noqa: ARG002
        model_request_parameters: ModelRequestParameters | None = None,  # noqa: ARG002
    ) -> ModelResponse:
        prompt = _latest_user_text(messages)
        chunks: list[str] = []
        async with self._lock:
            async for delta in _stream_turn(self._base_url, prompt, self._timeout):
                chunks.append(delta)
        return ModelResponse(parts=[TextPart(content="".join(chunks))], model_name=self._model_name)

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None = None,  # noqa: ARG002
        model_request_parameters: ModelRequestParameters | None = None,
        run_context: Any = None,  # noqa: ARG002
    ) -> AsyncIterator[StreamedResponse]:
        async with self._lock:
            yield AgentAPIStreamedResponse(
                model_request_parameters=model_request_parameters or ModelRequestParameters(),
                _model_name=self._model_name,
                _base_url=self._base_url,
                _prompt=_latest_user_text(messages),
                _timeout=self._timeout,
            )


async def _post_when_ready(client: Any, prompt: str, attempts: int = 12) -> Any:
    """POST the prompt, retrying while the wrapped agent refuses input.

    AgentAPI answers HTTP 500 with ``message can only be sent when the agent is
    waiting for user input`` whenever the CLI is mid-task or sitting on a
    permission prompt. ``GET /status`` is NOT a sufficient readiness check — it
    reported ``stable`` live while the agent was in exactly that state — so the
    reliable move is to retry the POST rather than poll and race.
    """
    delay = 0.25
    for _ in range(attempts):
        resp = await client.post("/message", json={"content": prompt, "type": "user"})
        if resp.status_code < 400:
            return resp
        if "waiting for user input" not in (resp.text or ""):
            raise AgentAPIError(f"AgentAPI rejected the message: {resp.text[:300]}")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 5.0)
    raise AgentAPIError(
        "The wrapped agent is busy and never returned to its input prompt. Check the "
        "terminal running `agentapi server` — the CLI may be mid-task or sitting on a "
        "permission prompt that needs an answer."
    )


async def _stream_turn(base_url: str, prompt: str, timeout: float) -> AsyncIterator[str]:
    """Submit one turn and yield cleaned text deltas until it completes."""
    import httpx

    async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
        try:
            resp = await client.get("/messages")
            resp.raise_for_status()
            baseline = max(
                (int(m.get("id", -1)) for m in (resp.json().get("messages") or [])), default=-1
            )
        except AgentAPIError:
            raise
        except Exception as exc:
            raise AgentAPIError(
                f"Cannot reach the AgentAPI server at {base_url} ({exc}). "
                "Start one with: agentapi server -- claude"
            ) from exc

        emitted = ""
        latest = ""
        started = False

        # Subscribe BEFORE posting: POST /message returns once the agent has
        # STARTED, so a stream opened afterwards can miss the first frames.
        async with client.stream("GET", "/events") as stream:
            await _post_when_ready(client, prompt)

            kind: str | None = None
            async for line in stream.aiter_lines():
                if line.startswith("event:"):
                    kind = line.split(":", 1)[1].strip()
                    continue
                if not line.startswith("data:") or kind is None:
                    continue
                try:
                    data = json.loads(line.split(":", 1)[1].strip())
                except (ValueError, IndexError):
                    continue

                if kind == "message_update":
                    if data.get("role") != "agent" or int(data.get("id", -1)) <= baseline:
                        continue  # pre-existing message, incl. the startup banner
                    started = True
                    latest = clean_frame(data.get("message") or "")
                    # message_update re-sends the WHOLE message every time, so
                    # the delta is ours. Only a strict append is streamed; a
                    # redraw is reconciled at the end.
                    if latest.startswith(emitted) and latest != emitted:
                        delta, emitted = latest[len(emitted) :], latest
                        yield delta

                elif kind == "status_change":
                    status = data.get("status")
                    if status == "running":
                        started = True
                    elif status == "stable" and started:
                        # No end-of-message marker exists; flush whatever a
                        # redraw left unsent so the caller always gets the final
                        # text even when the stream was not append-only.
                        if latest != emitted:
                            yield latest[len(emitted) :] if latest.startswith(emitted) else latest
                        return
