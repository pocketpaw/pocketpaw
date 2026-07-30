"""AgentAPI backend — drive a coding agent through coder/agentapi over HTTP.

Created 2026-07-30. Backend #10 in ``_BACKEND_REGISTRY``.

`AgentAPI <https://github.com/coder/agentapi>`_ runs a terminal coding agent
(Claude Code, Codex, Goose, Aider, Cursor CLI, …) under a PTY and exposes it as
a small HTTP API. This backend speaks that API, so PocketPaw can drive whichever
agent the operator already has authenticated locally.

**Why this exists: development without a model key.** Every other backend needs
a provider credential — an Anthropic key, an OpenAI key, a working LiteLLM
proxy. AgentAPI borrows the CLI's OWN authentication (a Claude Code
subscription, say), so a developer with no API key can still exercise the whole
PocketPaw stack end to end.

Read this before reaching for it in anger:

* **It is an AGENT, not a model.** AgentAPI wraps a complete agent that does its
  own planning and tool use. It cannot serve as the model endpoint for
  ``pydantic_ai`` or any other backend — those need an OpenAI-compatible
  ``/v1/chat/completions`` surface, which AgentAPI does not provide. Tool
  policy, the tool bridge and MCP config do not apply here either: the wrapped
  agent's own tools are what run.
* **One server is ONE conversation.** There is no session concept in the API —
  a server is a single PTY driving a single agent process with a single message
  history. Concurrent runs against one server would interleave into one
  terminal, so this backend SERIALISES runs behind a lock. That makes it
  unsuitable for multi-tenant serving by construction; it is a development and
  single-user tool. (This is the exact opposite property from
  ``pydantic_ai``, which exists to serve hundreds of concurrent runs.)
* **Output is scraped from a terminal.** Message content arrives as rendered TUI
  text — bullet markers, spinner lines, and right-padding to the terminal width.
  ``_clean_frame`` strips what it can. A future agent-type may render
  differently; the cleaning is best-effort and deliberately conservative.

Protocol notes, read off a live server rather than the README:

* ``POST /message`` with ``{"content": ..., "type": "user"}`` submits a turn and
  returns as soon as the agent STARTS work, not when it finishes.
* ``GET /events`` is an SSE stream carrying ``message_update`` (the FULL current
  text of one message id, re-sent on every change — not a delta) and
  ``status_change`` (``running`` / ``stable``).
* Completion is ``status_change`` back to ``stable`` after the turn began. There
  is no end-of-message marker.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

from pocketpaw.agents.backend import BackendInfo, BaseAgentBackend, Capability
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.config import Settings
from pocketpaw.tools.policy import ToolPolicy

logger = logging.getLogger(__name__)

# Lines the TUI draws that are not assistant output. The spinner line rotates
# its glyph and its verb between frames (``✻ Cooked for 2s``, ``✻ Crunched for
# 2s``), so it is matched by shape rather than by text.
_STATUS_LINE = re.compile(r"^[✻✳✽✶✢*·]\s")
# Tool-result / tip continuations. Claude Code prints startup tips and tool
# output under this glyph; neither is the assistant talking to the user.
_CONTINUATION = re.compile(r"^\s*⎿")
# The bullet Claude Code puts in front of an assistant paragraph.
_BULLET = re.compile(r"^[●•]\s?")


def _clean_frame(text: str) -> str:
    """Strip terminal chrome from a scraped agent frame.

    Best-effort and conservative: it removes the leading bullet, drops spinner /
    status lines, and trims the right-padding the PTY adds to fill the terminal
    width. It does NOT try to parse box-drawing banners — the startup frame is
    excluded by id instead (see ``run``), which is more reliable than pattern
    matching a TUI that changes between releases.
    """
    out: list[str] = []
    # A ⎿ block WRAPS: only its first line carries the glyph, the rest are just
    # indented. Dropping the glyph line alone leaks the tail of every startup
    # tip into the answer — which is exactly what a live turn did, mid-sentence,
    # right before the real reply ("…workflow directly.391").
    in_continuation = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if _CONTINUATION.match(line):
            in_continuation = True
            continue
        if in_continuation:
            # Still inside the block while lines stay indented; a line starting
            # at column 0 (``● answer``) ends it.
            if line and line[0].isspace():
                continue
            in_continuation = False
        if _STATUS_LINE.match(line):
            continue
        if not line:
            # Collapse runs of blank lines. The PTY pads every frame to the
            # terminal height, so without this a two-word answer arrives
            # followed by fifty newlines.
            if out and not out[-1]:
                continue
            out.append("")
            continue
        out.append(_BULLET.sub("", line, count=1))
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


class AgentAPIBackend(BaseAgentBackend):
    """Drive a terminal coding agent through a coder/agentapi server."""

    @staticmethod
    def info() -> BackendInfo:
        return BackendInfo(
            name="agentapi",
            display_name="AgentAPI (coder/agentapi)",
            capabilities=(
                Capability.STREAMING | Capability.MULTI_TURN | Capability.CUSTOM_SYSTEM_PROMPT
            ),
            # No TOOLS / MCP capability on purpose: the wrapped agent brings its
            # own tools and PocketPaw's ToolPolicy cannot govern them.
            builtin_tools=[],
            tool_policy_map={},
            required_keys=[],
            supported_providers=[],
            install_hint={
                "pip_package": "",
                "pip_spec": "",
                "verify_import": "",
                "note": (
                    "Needs a running AgentAPI server: "
                    "`agentapi server -- claude` (defaults to :3284). "
                    "See https://github.com/coder/agentapi"
                ),
            },
            beta=True,
        )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._policy = ToolPolicy(
            profile=settings.tool_profile,
            allow=settings.tools_allow,
            deny=settings.tools_deny,
        )
        # ONE server is ONE terminal, so turns must not overlap. See the module
        # docstring — this is a correctness lock, not a throughput tweak.
        self._turn_lock = asyncio.Lock()
        # Per-run cancellation boxes, held by identity. A list of the actual
        # mutable flags — NOT their ids, which would make ``stop()`` a no-op.
        self._active_stops: list[list[bool]] = []

    # -- policy (accepted so the protocol is satisfied; not enforceable here) --

    def get_tool_policy(self) -> ToolPolicy:
        return self._policy

    def set_tool_policy(self, policy: ToolPolicy) -> None:
        self._policy = policy

    # -- helpers ------------------------------------------------------------

    @property
    def _base_url(self) -> str:
        return (getattr(self.settings, "agentapi_base_url", "") or "http://localhost:3284").rstrip(
            "/"
        )

    async def _highest_message_id(self, client: Any) -> int:
        """Return the id of the newest message, or -1 on an empty conversation.

        Used as a baseline so the run only reports messages produced AFTER the
        prompt is submitted — which is also what keeps the startup banner (the
        box-drawing splash the CLI prints on boot) out of the stream.
        """
        resp = await client.get("/messages")
        resp.raise_for_status()
        messages = resp.json().get("messages") or []
        return max((int(m.get("id", -1)) for m in messages), default=-1)

    # -- run ----------------------------------------------------------------

    async def run(
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list[dict] | None = None,  # noqa: ARG002
        session_key: str | None = None,  # noqa: ARG002
    ) -> AsyncIterator[AgentEvent]:
        """Submit one turn and stream the agent's reply.

        ``history`` is ignored: the AgentAPI server owns the conversation and
        replaying our copy of it would duplicate every turn in the agent's own
        context. ``system_prompt`` is prepended to the FIRST message of a
        conversation only, since the wrapped CLI has no system-prompt channel.
        """
        try:
            import httpx
        except ImportError:  # pragma: no cover - httpx is a core dep
            yield AgentEvent(type="error", content="httpx is required for the AgentAPI backend.")
            return

        stop: list[bool] = [False]
        self._active_stops.append(stop)
        timeout = float(getattr(self.settings, "agentapi_timeout", 0) or 600)

        async with self._turn_lock:
            try:
                async with httpx.AsyncClient(base_url=self._base_url, timeout=timeout) as client:
                    try:
                        baseline = await self._highest_message_id(client)
                    except Exception as exc:
                        yield AgentEvent(
                            type="error",
                            content=(
                                f"Cannot reach the AgentAPI server at {self._base_url} ({exc}).\n\n"
                                "Start one with: agentapi server -- claude"
                            ),
                        )
                        yield AgentEvent(type="done", content="")
                        return

                    prompt = message
                    if system_prompt and baseline < 1:
                        # Only on a fresh conversation — see the docstring.
                        prompt = f"{system_prompt}\n\n{message}"

                    async for event in self._stream_turn(client, prompt, baseline, stop):
                        yield event

            except Exception as exc:
                logger.error("AgentAPI error: %s", exc, exc_info=True)
                yield AgentEvent(type="error", content=f"AgentAPI error: {exc}")
                yield AgentEvent(type="done", content="")
                return
            finally:
                if stop in self._active_stops:
                    self._active_stops.remove(stop)

        yield AgentEvent(type="done", content="")

    async def _post_when_ready(self, client: Any, prompt: str, attempts: int = 12) -> Any:
        """POST the prompt, retrying while the agent refuses to accept input.

        AgentAPI rejects a message with HTTP 500 and ``message can only be sent
        when the agent is waiting for user input`` whenever the wrapped CLI is
        mid-task or sitting on a prompt. Two things make this worth retrying
        rather than failing fast:

        * ``GET /status`` is NOT a sufficient readiness check. It reported
          ``stable`` while the agent was in exactly that state, so polling
          status and then posting still races.
        * the condition is usually transient — the previous turn is finishing.

        Returns the response, or ``None`` if the agent never became ready.
        """
        delay = 0.25
        for _ in range(attempts):
            resp = await client.post("/message", json={"content": prompt, "type": "user"})
            if resp.status_code < 400:
                return resp
            if "waiting for user input" not in (resp.text or ""):
                return resp  # a different failure — let the caller report it
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5.0)
        return None

    async def _stream_turn(
        self, client: Any, prompt: str, baseline: int, stop: list
    ) -> AsyncIterator[AgentEvent]:
        """Post the prompt and translate the SSE stream into ``AgentEvent``s."""
        # Subscribe BEFORE posting. ``POST /message`` returns once the agent has
        # STARTED, so a stream opened afterwards can miss the first frames.
        emitted: dict[int, str] = {}  # what the caller has already been sent
        latest: dict[int, str] = {}  # newest cleaned frame per message id
        started = False

        async with client.stream("GET", "/events") as stream:
            post = await self._post_when_ready(client, prompt)
            if post is None:
                yield AgentEvent(
                    type="error",
                    content=(
                        "The wrapped agent is busy and never returned to its input prompt.\n\n"
                        "AgentAPI only accepts a message while the agent is waiting for user "
                        "input. Check the terminal running `agentapi server` — the CLI may be "
                        "mid-task or sitting on a permission prompt that needs an answer."
                    ),
                )
                return
            if post.status_code >= 400:
                yield AgentEvent(
                    type="error", content=f"AgentAPI rejected the message: {post.text[:300]}"
                )
                return

            kind: str | None = None
            async for line in stream.aiter_lines():
                if stop[0]:
                    break
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
                    if data.get("role") != "agent":
                        continue
                    mid = int(data.get("id", -1))
                    if mid <= baseline:
                        continue  # pre-existing message, incl. the startup banner
                    started = True
                    latest[mid] = _clean_frame(data.get("message") or "")
                    # ``message_update`` re-sends the WHOLE message on every
                    # change, so the delta is ours to compute. Only a strict
                    # APPEND is streamed; a divergent frame (a TUI redraw) is
                    # held back and reconciled when the turn ends.
                    #
                    # Honesty note, because the comment here used to claim more:
                    # this does NOT change the final text. A mutation probe
                    # showed emitting divergent frames immediately produces the
                    # same concatenation — the two shapes converge. What it buys
                    # is event granularity: the caller does not see a
                    # half-rendered frame emitted as if it were new content.
                    # The startup-tip and trailing-newline noise on the first
                    # live run was fixed by ``_clean_frame``, not by this.
                    prev = emitted.get(mid, "")
                    if latest[mid].startswith(prev) and latest[mid] != prev:
                        delta = latest[mid][len(prev) :]
                        emitted[mid] = latest[mid]
                        yield AgentEvent(type="message", content=delta)

                elif kind == "status_change":
                    status = data.get("status")
                    if status == "running":
                        started = True
                    elif status == "stable" and started:
                        # Turn complete — there is no end-of-message marker.
                        # Flush whatever a redraw left unsent, so the caller
                        # always ends up with the final text even if the stream
                        # was not append-only.
                        for mid, final in latest.items():
                            sent = emitted.get(mid, "")
                            if final == sent:
                                continue
                            tail = final[len(sent) :] if final.startswith(sent) else final
                            if tail:
                                yield AgentEvent(type="message", content=tail)
                        return

    async def stop(self) -> None:
        """Signal every in-flight turn to stop consuming the event stream.

        This does NOT interrupt the wrapped agent — AgentAPI has no cancel
        endpoint, and the CLI keeps working in its terminal. It only detaches
        this backend from the stream.
        """
        for flag in list(self._active_stops):
            flag[0] = True

    async def get_status(self) -> dict[str, Any]:
        status: dict[str, Any] = {
            "backend": "agentapi",
            "base_url": self._base_url,
            "available": False,
        }
        try:
            import httpx

            async with httpx.AsyncClient(base_url=self._base_url, timeout=10) as client:
                resp = await client.get("/status")
                resp.raise_for_status()
                body = resp.json()
            status.update(
                available=True,
                running=body.get("status") == "running",
                agent_type=body.get("agent_type"),
                transport=body.get("transport"),
            )
        except Exception as exc:  # noqa: BLE001
            status["error"] = str(exc)
        return status
