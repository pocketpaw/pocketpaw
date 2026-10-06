# ee/pocketpaw_ee/cloud/belt/feed.py — the develop station's step feed.
#
# The station runs ``claude -p --output-format stream-json --verbose`` through
# its own ``Runner`` (env scrub, argv lists, process-group kill), so the SDK
# client's event stream is not available; this module reads the CLI's stdout
# instead. Nothing else in the codebase parses stream-json, hence the module.
#
#   * ``stream_events`` — stdout lines -> ``AgentEvent`` (the one event schema,
#     ``agents/protocol.py``): assistant ``text`` -> message, non-empty
#     ``thinking`` -> thinking, ``tool_use`` -> tool_use (name + input), and a
#     user ``tool_result`` -> tool_result named from its ``tool_use_id`` (the
#     CLI puts only the id on a result; the recorder pairs by name). Each event
#     carries the line's ISO ``timestamp`` in ``metadata``. Other lines (system,
#     rate limits, the final result envelope) and unparseable ones are skipped.
#   * ``fold_feed`` — events -> ``StepRecorder`` through
#     ``steps.record_agent_event``, so a feed is the chat steps shape: same
#     per-field caps, same scrub/redact (inputs and outputs), same wire mapper.
#     The developer's prose between tool calls has no chat-step kind; it is
#     recorded as a ``thinking`` step so the run page shows the why with the what.
#   * Caps: ``FEED_MAX_STEPS`` steps and ``FEED_MAX_BYTES`` per stored feed; the
#     overflow is counted in ``steps_omitted``. BF-3 stores one stage (develop),
#     so the per-stage cap is the per-run cap.

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from pocketpaw.agents.protocol import AgentEvent
from pocketpaw_ee.cloud.chat.runs.steps import StepRecorder, record_agent_event

FEED_MAX_STEPS = 2_000
FEED_MAX_BYTES = 2_000_000


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


def stream_events(stdout: str) -> Iterator[AgentEvent]:
    """Yield the ``AgentEvent``s a ``stream-json`` stdout carries, in order."""
    names: dict[str, str] = {}  # tool_use id -> tool name
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type") if isinstance(event, dict) else None
        if kind not in ("assistant", "user"):
            continue
        message = event.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            continue
        meta = {"timestamp": event.get("timestamp")}
        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if kind == "assistant" and btype == "text" and block.get("text"):
                yield AgentEvent("message", str(block["text"]), dict(meta))
            elif kind == "assistant" and btype == "thinking" and block.get("thinking"):
                yield AgentEvent("thinking", str(block["thinking"]), dict(meta))
            elif kind == "assistant" and btype == "tool_use":
                name = str(block.get("name") or "")
                names[str(block.get("id") or "")] = name
                raw_input = block.get("input")
                tool_input = raw_input if isinstance(raw_input, dict) else {}
                yield AgentEvent("tool_use", name, {**meta, "name": name, "input": tool_input})
            elif kind == "user" and btype == "tool_result":
                name = names.get(str(block.get("tool_use_id") or ""), "")
                yield AgentEvent(
                    "tool_result",
                    _result_text(block.get("content")),
                    {**meta, "name": name},
                )


def fold_feed(events: Iterator[AgentEvent] | list[AgentEvent]) -> StepRecorder:
    """Fold events into a finalized, capped ``StepRecorder``."""
    recorder = StepRecorder(max_steps=FEED_MAX_STEPS, max_total_bytes=FEED_MAX_BYTES)
    for event in events:
        at = _parse_time(event.metadata.get("timestamp"))
        if event.type == "message":
            # The developer's prose: kept as a thought (see the header).
            recorder.observe("thinking", {"content": event.content}, at)
            continue
        if event.type == "tool_use":
            record_agent_event(
                recorder, event, event.metadata.get("name", ""), event.metadata.get("input"), now=at
            )
            continue
        record_agent_event(recorder, event, now=at)
    recorder.finalize()
    return recorder


__all__ = ["FEED_MAX_BYTES", "FEED_MAX_STEPS", "fold_feed", "stream_events"]
