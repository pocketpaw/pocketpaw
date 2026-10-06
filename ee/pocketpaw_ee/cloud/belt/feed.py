# ee/pocketpaw_ee/cloud/belt/feed.py — a Belt run's step feed, live and stored.
#
# The station runs ``claude -p --output-format stream-json --verbose`` through
# its own ``Runner`` (env scrub, argv lists, process-group kill), so the SDK
# client's event stream is not available; this module reads the CLI's stdout.
#
#   * ``stream_events`` — stdout -> ``AgentEvent`` (``agents/protocol.py``):
#     assistant ``text`` -> message, non-empty ``thinking`` -> thinking,
#     ``tool_use`` -> tool_use (block id as ``call_id``), user ``tool_result`` ->
#     tool_result (its ``tool_use_id`` as ``call_id``, the name looked up from
#     it, so parallel same-name calls pair by id). Each carries the line's ISO
#     ``timestamp``; system, rate-limit, result and unparseable lines are skipped.
#   * ``FrameReader`` — the same, one line at a time, into chat frames
#     (``steps.agent_event_frame``): the developer's prose has no chat-step kind,
#     so it is a ``thinking`` frame (back-to-back blocks a blank line apart), and
#     a tool frame's narration is its verb and subject (``label``).
#   * ``fold_frames`` / ``fold_feed`` — frames -> a finalized ``StepRecorder``
#     (same caps, scrub/redact and wire mapper as chat steps), capped at
#     ``FEED_MAX_STEPS`` / ``FEED_MAX_BYTES``.
#   * ``RunFeed`` — one station call. ``start`` empties every ``STAGES`` row and
#     opens the attempt on the run's stream (``stream_id``: the chat-runs
#     ``RunStreamTransport``, so Redis in production); ``stage`` opens a step
#     (and marks the run ``running`` there: ``service.mark_run_stage``, which
#     emits ``belt_run_updated``); ``add`` keeps a frame for its stage and
#     publishes it scrubbed (``steps.scrub_frame``) and stage-tagged, and after
#     an Edit/Write/MultiEdit call ``file_touched`` (the file and the component
#     the base blueprint's ``paths`` give it, ``orient.component_for``); ``save``
#     folds the stage's frames of this call into its stored row; ``end`` closes
#     the attempt with ``stream_end``. Frames reach it already path-relative
#     (the station strips worktree/repo paths and the host account first).
#     Caps per call: frames past ``FEED_MAX_STEPS`` / ``FEED_MAX_BYTES`` are not
#     published (``stream_end.omitted`` counts them); stored rows share the
#     same budget across stages. Everything here is best-effort and never fails
#     the run: a save failure is logged, a publish failure is logged once and
#     ends publishing for the call. A run with no ``action_id`` does nothing.

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.security.redact import redact_output
from pocketpaw.tools.narration import Narration, render
from pocketpaw_ee.cloud.belt.orient import PathIndex, component_for, repo_path
from pocketpaw_ee.cloud.chat.runs.steps import StepRecorder, agent_event_frame, scrub_frame

logger = logging.getLogger(__name__)

FEED_MAX_STEPS = 2_000
FEED_MAX_BYTES = 2_000_000
# The station's steps, in run order; each has its own stored row.
STAGES = ("orient", "develop", "check", "fix", "review")
# Live stream retention: long enough for the longest station call (develop,
# two fixes, three reviews, the checks between), then an hour after it ends.
_LIVE_TTL = 6 * 3600
_ENDED_TTL = 3600
# The tools that change a file: each call also publishes ``file_touched``.
EDIT_TOOLS = frozenset({"Edit", "MultiEdit", "Write"})
# A tool row's narration: its verb and the input arg it reads.
_LABELS = {
    "Read": ("Read", "file_path"),
    "Edit": ("Edit", "file_path"),
    "MultiEdit": ("Edit", "file_path"),
    "Write": ("Write", "file_path"),
    "Bash": ("Run", "command"),
    "Grep": ("Grep", "pattern"),
    "Glob": ("Find", "pattern"),
}

Frame = tuple[str, dict[str, Any], datetime | None]
SaveFn = Callable[[str, str, str, list[dict[str, Any]], int], Awaitable[None]]


def _parse_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _result_text(content: Any) -> str:
    """A ``tool_result`` block's content: a string, or a list of blocks whose
    ``text`` parts are joined (images and other blocks are dropped)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(b.get("text") or "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return "" if content is None else str(content)


def label(tool: str, tool_input: Any) -> str | None:
    """A tool row's narration: ``Read <path>``, ``Edit <path>``, ``Write
    <path>``, ``Run <first line of the command>``, ``Grep <pattern>``, ``Find
    <pattern>`` (Glob); the subject is redacted (the recorder never redacts a
    narration) and cut to 80 chars by the narration renderer. ``None`` for
    another tool or a missing arg."""
    verb, arg = _LABELS.get(tool, ("", ""))
    value = tool_input.get(arg) if arg and isinstance(tool_input, dict) else None
    if not isinstance(value, str):
        return None
    lines = value.strip().splitlines()
    first = redact_output(lines[0]) if lines else ""
    return render(Narration(active=f"{verb} {{{arg}}}", bare="", safe_args=(arg,)), {arg: first})


def _line_events(line: str, names: dict[str, str]) -> list[AgentEvent]:
    """One stdout line's ``AgentEvent``s; ``names`` maps tool_use ids to tool
    names across lines."""
    line = line.strip()
    if not line.startswith("{"):
        return []
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return []
    kind = event.get("type") if isinstance(event, dict) else None
    if kind not in ("assistant", "user"):
        return []
    message = event.get("message")
    blocks = message.get("content") if isinstance(message, dict) else None
    if not isinstance(blocks, list):
        return []
    meta = {"timestamp": event.get("timestamp")}
    out: list[AgentEvent] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if kind == "assistant" and btype == "text" and block.get("text"):
            out.append(AgentEvent("message", str(block["text"]), dict(meta)))
        elif kind == "assistant" and btype == "thinking" and block.get("thinking"):
            out.append(AgentEvent("thinking", str(block["thinking"]), dict(meta)))
        elif kind == "assistant" and btype == "tool_use":
            call_id = str(block.get("id") or "")
            name = str(block.get("name") or "")
            names[call_id] = name
            raw_input = block.get("input")
            tool_input = raw_input if isinstance(raw_input, dict) else {}
            meta_use = {**meta, "name": name, "input": tool_input, "call_id": call_id}
            out.append(AgentEvent("tool_use", name, meta_use))
        elif kind == "user" and btype == "tool_result":
            call_id = str(block.get("tool_use_id") or "")
            meta_result = {**meta, "name": names.get(call_id, ""), "call_id": call_id}
            out.append(AgentEvent("tool_result", _result_text(block.get("content")), meta_result))
    return out


def stream_events(stdout: str) -> Iterator[AgentEvent]:
    """Yield the ``AgentEvent``s a ``stream-json`` stdout carries, in order."""
    names: dict[str, str] = {}
    for line in stdout.splitlines():
        yield from _line_events(line, names)


class FrameReader:
    """Chat frames from ``stream-json``, fed one line (or event) at a time; it
    keeps the tool names by id and whether the last frame was prose."""

    def __init__(self) -> None:
        self._names: dict[str, str] = {}
        self._prose_open = False

    def line(self, line: str) -> list[Frame]:
        frames = (self.event(e) for e in _line_events(line, self._names))
        return [f for f in frames if f is not None]

    def event(self, event: AgentEvent) -> Frame | None:
        at = _parse_time(event.metadata.get("timestamp"))
        if event.type in ("message", "thinking"):
            # The developer's prose is kept as a thought (see the header).
            text = event.content if isinstance(event.content, str) else ""
            if not text:
                return None
            sep = "\n\n" if self._prose_open else ""
            self._prose_open = True
            return "thinking", {"content": sep + text}, at
        self._prose_open = False
        name, tool_input, narration = "", None, None
        if event.type == "tool_use":
            name, tool_input = event.metadata.get("name", ""), event.metadata.get("input")
            narration = label(name, tool_input)
        frame = agent_event_frame(event, name, tool_input, narration)
        return (*frame, at) if frame is not None else None


def fold_frames(
    frames: Iterable[Frame], max_steps: int = FEED_MAX_STEPS, max_bytes: int = FEED_MAX_BYTES
) -> StepRecorder:
    """Fold frames into a finalized, capped ``StepRecorder``."""
    recorder = StepRecorder(max_steps=max_steps, max_total_bytes=max_bytes)
    for name, data, at in frames:
        recorder.observe(name, data, at)
    recorder.finalize()
    return recorder


def fold_feed(events: Iterable[AgentEvent]) -> StepRecorder:
    """Fold a seat's events into a finalized, capped ``StepRecorder``.
    Consecutive whole prose/thinking blocks land in one thinking step, a blank
    line apart (the recorder itself concatenates, being built for deltas)."""
    reader = FrameReader()
    return fold_frames(f for e in events if (f := reader.event(e)) is not None)


def stream_id(action_id: str) -> str:
    """The run's id on the ``RunStreamTransport`` (``belt:`` keeps it apart
    from chat run ids)."""
    return f"belt:{action_id}"


async def _publish(action_id: str, event: str, data: dict[str, Any]) -> None:
    from pocketpaw_ee.cloud.chat.runs.transport import get_stream_transport

    transport = get_stream_transport()
    sid = stream_id(action_id)
    await transport.append_event(sid, event, data)
    if event in ("start", "stream_end"):
        await transport.set_ttl(sid, _LIVE_TTL if event == "start" else _ENDED_TTL)


async def _notify(workspace_id: str, action_id: str, stage: str) -> None:
    from pocketpaw_ee.cloud.belt.service import mark_run_stage

    await mark_run_stage(workspace_id, action_id, stage)


def _size(data: Any) -> int:
    return len(json.dumps(data, default=str))


@dataclass
class RunFeed:
    """One station call's feed (see the header)."""

    workspace_id: str
    action_id: str
    save_fn: SaveFn
    stage_name: str = ""
    paths: PathIndex = field(default_factory=list)  # the base blueprint's file join
    _frames: dict[str, list[Frame]] = field(default_factory=dict)
    _rounds: dict[str, int] = field(default_factory=dict)
    _stored: dict[str, tuple[int, int]] = field(default_factory=dict)  # stage -> (steps, bytes)
    _sent: int = 0
    _sent_bytes: int = 0
    _omitted: int = 0
    _calls: int = 0
    _down: bool = False  # the transport failed once: no more publishing this call

    @property
    def live(self) -> bool:
        return bool(self.action_id)

    async def start(self) -> None:
        """A new attempt: no stage shows the previous attempt's steps."""
        if not self.live:
            return
        for stage in STAGES:
            await self._save(stage, [], 0)
        await self._send("start", {}, cap=False)

    async def stage(self, name: str) -> None:
        """Open a station step; a repeat (a second check or fix) is a new round."""
        self.stage_name = name
        self._frames.setdefault(name, [])
        self._rounds[name] = self._rounds.get(name, 0) + 1
        if not self.live:
            return
        await self._send("stage", {"stage": name, "round": self._rounds[name]}, cap=False)
        try:
            await _notify(self.workspace_id, self.action_id, name)
        except Exception:  # noqa: BLE001 — the nudge is a view
            logger.debug("belt: stage nudge failed for run %s", self.action_id, exc_info=True)

    async def add(self, event: str, data: dict[str, Any], at: datetime | None = None) -> None:
        """Keep a (path-relative) frame for this stage and publish it scrubbed."""
        if not self.live:
            return
        self._frames[self.stage_name].append((event, data, at))
        await self._send(event, {**scrub_frame(event, data), "stage": self.stage_name})
        if event == "tool_start" and data.get("tool") in EDIT_TOOLS:
            await self._touched(data)

    async def _touched(self, data: dict[str, Any]) -> None:
        """``file_touched`` for an edit call: the file and the blueprint
        component it maps to (``None``: no glob owns it). Published only; the
        stored rows keep the call itself. A path outside the worktree is not a
        repo file and sends nothing."""
        tool_input = data.get("input")
        raw = tool_input.get("file_path") if isinstance(tool_input, dict) else None
        path = repo_path(raw) if isinstance(raw, str) else None
        if path is None:
            return
        touched = {
            "stage": self.stage_name,
            "path": redact_output(path),
            "component": component_for(path, self.paths),
            "tool": data.get("tool"),
            "call_id": data.get("call_id"),
        }
        await self._send("file_touched", touched)

    async def begin(self, tool: str, tool_input: dict[str, Any], narration: str = "") -> str:
        """A station-run step (a check, the recipe, orient) starts; its call id."""
        self._calls += 1
        call_id = f"{self.stage_name}-{self._calls}"
        narration = narration or label(tool, tool_input) or ""
        frame = {"tool": tool, "input": tool_input, "narration": narration, "call_id": call_id}
        await self.add("tool_start", frame)
        return call_id

    async def finish(self, call_id: str, tool: str, output: str) -> None:
        await self.add("tool_result", {"tool": tool, "output": output, "call_id": call_id})

    async def save(self) -> None:
        """Store every frame the open stage has had this call, folded, inside
        what the other stages' rows left of the per-run budget."""
        if not self.live:
            return
        stage = self.stage_name
        others = [v for k, v in self._stored.items() if k != stage]
        steps_left = max(FEED_MAX_STEPS - sum(n for n, _ in others), 0)
        bytes_left = max(FEED_MAX_BYTES - sum(b for _, b in others), 0)
        frames = list(self._frames.get(stage, []))
        try:
            # Folding and redacting a few MB is CPU work; keep it off the loop.
            recorder = await asyncio.to_thread(fold_frames, frames, steps_left, bytes_left)
        except Exception:  # noqa: BLE001 — a fold failure only loses this stage's row
            logger.warning("belt: could not fold the %s feed", stage, exc_info=True)
            return
        steps = recorder.steps
        self._stored[stage] = (len(steps), _size(steps))
        await self._save(stage, steps, recorder.steps_omitted)

    async def end(self, ok: bool) -> None:
        """Close the attempt; never capped, so a viewer always sees the end."""
        if self.live:
            await self._send("stream_end", {"ok": ok, "omitted": self._omitted}, cap=False)

    async def _save(self, stage: str, steps: list[dict[str, Any]], omitted: int) -> None:
        try:
            await self.save_fn(self.workspace_id, self.action_id, stage, steps, omitted)
        except Exception:  # noqa: BLE001 — the feed is a view; it never fails a run
            logger.warning(
                "belt: could not store the %s feed for run %s", stage, self.action_id, exc_info=True
            )

    async def _send(self, event: str, data: dict[str, Any], *, cap: bool = True) -> None:
        if self._down:
            return
        if cap:
            size = _size(data)
            if self._sent >= FEED_MAX_STEPS or self._sent_bytes + size > FEED_MAX_BYTES:
                self._omitted += 1
                return
            self._sent += 1
            self._sent_bytes += size
        try:
            await _publish(self.action_id, event, data)
        except Exception:  # noqa: BLE001 — the live feed is a view; it never fails a run
            # ponytail: one failure ends live publishing for the call (the stored
            # rows still serve a reload); retry per frame if blips prove common.
            self._down = True
            logger.warning(
                "belt: live feed off for run %s (publish failed)", self.action_id, exc_info=True
            )


__all__ = [
    "EDIT_TOOLS",
    "FEED_MAX_BYTES",
    "FEED_MAX_STEPS",
    "STAGES",
    "FrameReader",
    "RunFeed",
    "fold_feed",
    "fold_frames",
    "label",
    "stream_events",
    "stream_id",
]
