"""Record the tool calls and thinking an agent run streams, as ordered steps.

Tool calls, tool results and thinking a chat run streams reach the Redis run
stream (1h TTL) live; ``StepRecorder`` folds the same ``(event_name, event_data)``
frames into the ordered ``steps`` list stored on the assistant ``Message``, so a
refresh still shows the work behind a reply. ``record_agent_event`` adapts one
``AgentEvent`` to those frames: the group/DM bridge and the Belt develop
station's feed (``belt/feed.py``) both feed the recorder through it, so every
surface records a call the same way.

Why a recorder and not the raw frames: the frames are a live-UI protocol, not a
record. A claude_sdk call is announced twice (a provisional ``input_pending``
frame, then the real one), thinking arrives as many small deltas, and results
carry no call id. Storing them verbatim would show phantom calls and hundreds of
one-word thinking rows. Everything stored is bounded (per-field caps, plus a
per-recorder step and byte budget: the chat defaults below, larger for a develop
feed) and passed through the same scrub/redact the audit log uses, string values
of tool inputs included, because tool I/O is where secrets leak.

``steps_wire_fields`` is the one step -> wire conversion, shared by every UI
history mapper and the belt feed route. Steps are display data only: the LLM
history reader never reads them.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from pocketpaw.security.redact import redact_output
from pocketpaw.security.scrub import scrub_params
from pocketpaw_ee.cloud._core.time import iso_utc

MAX_STEPS = 50
MAX_TOTAL_BYTES = 64_000
MAX_INPUT_CHARS = 2048
MAX_OUTPUT_CHARS = 4096
MAX_THINKING_CHARS = 8192
# Redaction is regex work over the whole string; bound what it scans. Anything
# past this is cut by the output cap anyway.
_REDACT_SCAN_CHARS = 65_536


def _now() -> datetime:
    return datetime.now(UTC)


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, default=str)
        except (TypeError, ValueError):
            return str(value)
    return "" if value is None else str(value)


def _redact_leaves(value: Any) -> Any:
    """Redact every string inside ``value``. An Edit's ``new_string`` or a Bash
    command carries a key under a harmless name, which ``scrub_params`` (names
    only) never sees. Leaves, not the encoded JSON: there a key at the start of
    a line follows a literal ``\\n`` and the patterns' word boundary misses it."""
    if isinstance(value, str):
        return redact_output(value[:_REDACT_SCAN_CHARS])
    if isinstance(value, dict):
        return {k: _redact_leaves(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_leaves(v) for v in value]
    return value


def _cap_input(value: Any) -> Any:
    """Scrub secret-named args and redact secret-shaped values, then keep the
    dict if it is small.

    Round-tripped through JSON so what lands in Mongo is plain JSON types. An
    over-size input is stored as a truncated JSON string: the step still shows
    what was asked, without one huge argument blowing the message budget.
    """
    scrubbed = _redact_leaves(scrub_params(value))
    try:
        encoded = json.dumps(scrubbed, default=str)
    except (TypeError, ValueError):
        encoded = json.dumps(str(scrubbed))
    if len(encoded) > MAX_INPUT_CHARS:
        return redact_output(encoded[:_REDACT_SCAN_CHARS])[:MAX_INPUT_CHARS] + "…"
    return json.loads(encoded)


def _cap_output(value: Any) -> tuple[str, bool]:
    text = _stringify(value)
    over_scan = len(text) > _REDACT_SCAN_CHARS
    text = redact_output(text[:_REDACT_SCAN_CHARS])
    if over_scan or len(text) > MAX_OUTPUT_CHARS:
        return text[:MAX_OUTPUT_CHARS], True
    return text, False


class StepRecorder:
    """Fold a run's streamed frames into an ordered, bounded list of steps.

    Feed every frame to :meth:`observe`; call :meth:`finalize` once the run has
    ended, however it ended. Only ``thinking``, ``tool_start`` and
    ``tool_result`` are recorded; ``chunk`` closes an open thinking block (text
    between two thinking phases makes them two blocks) and everything else is
    ignored.
    """

    def __init__(
        self, *, max_steps: int = MAX_STEPS, max_total_bytes: int = MAX_TOTAL_BYTES
    ) -> None:
        self._max_steps = max_steps
        self._max_total_bytes = max_total_bytes
        self._steps: list[dict[str, Any]] = []
        self._omitted = 0
        # Open tool calls, oldest first: (step, provisional). No call id reaches
        # this layer, so a result pairs with the OLDEST open call of its tool.
        self._open: list[tuple[dict[str, Any], bool]] = []
        # Calls dropped by the step cap that are still "open", per tool, so
        # their results and resolving frames don't count as a second drop.
        self._dropped_open: dict[str, int] = {}
        self._dropped_provisional: dict[str, int] = {}
        self._thinking: dict[str, Any] | None = None
        self._thinking_raw = ""
        self._finalized = False

    # -- feeding -------------------------------------------------------------

    def observe(self, event_name: str, event_data: Any, now: datetime | None = None) -> None:
        if self._finalized:
            return
        data = event_data if isinstance(event_data, dict) else {}
        at = now or _now()
        if event_name == "thinking":
            content = data.get("content")
            if isinstance(content, str) and content:
                self._on_thinking(content, at)
        elif event_name == "tool_start":
            self._close_thinking(at)
            self._on_tool_start(data, at)
        elif event_name == "tool_result":
            self._close_thinking(at)
            self._on_tool_result(data, at)
        elif event_name == "chunk":
            self._close_thinking(at)

    def _append(self, step: dict[str, Any]) -> bool:
        if len(self._steps) >= self._max_steps:
            self._omitted += 1
            return False
        self._steps.append(step)
        return True

    def _new_step(self, kind: str, at: datetime, **fields: Any) -> dict[str, Any]:
        step: dict[str, Any] = {
            "id": uuid.uuid4().hex[:12],
            "kind": kind,
            "tool": "",
            "narration": "",
            "text": "",
            "input": None,
            "output": "",
            "output_truncated": False,
            "status": "running",
            "started_at": at,
            "ended_at": None,
        }
        step.update(fields)
        return step

    def _on_thinking(self, content: str, at: datetime) -> None:
        if self._thinking is None:
            step = self._new_step("thinking", at)
            self._thinking_raw = ""
            self._thinking = step if self._append(step) else {}
        # Keep some headroom over the cap so redaction sees whole tokens.
        if len(self._thinking_raw) < MAX_THINKING_CHARS * 2:
            self._thinking_raw += content

    def _close_thinking(self, at: datetime) -> None:
        step = self._thinking
        self._thinking = None
        if not step:  # none open, or it was dropped by the cap
            return
        step["text"] = redact_output(self._thinking_raw)[:MAX_THINKING_CHARS]
        step["status"] = "complete"
        step["ended_at"] = at
        self._thinking_raw = ""

    def _on_tool_start(self, data: dict[str, Any], at: datetime) -> None:
        tool = str(data.get("tool") or "")
        narration = data.get("narration")
        narration = narration if isinstance(narration, str) else ""
        pending = data.get("input_pending") is True
        if not pending:
            # The resolved frame for a call announced provisionally: upgrade
            # that step instead of recording a phantom second call.
            for index, (step, provisional) in enumerate(self._open):
                if provisional and step["tool"] == tool:
                    step["input"] = _cap_input(data.get("input"))
                    if narration:
                        step["narration"] = narration
                    self._open[index] = (step, False)
                    return
            if self._dropped_provisional.get(tool):
                self._dropped_provisional[tool] -= 1
                return
        step = self._new_step(
            "tool",
            at,
            tool=tool,
            narration=narration,
            input=None if pending else _cap_input(data.get("input")),
        )
        if self._append(step):
            self._open.append((step, pending))
        else:
            self._dropped_open[tool] = self._dropped_open.get(tool, 0) + 1
            if pending:
                self._dropped_provisional[tool] = self._dropped_provisional.get(tool, 0) + 1

    def _on_tool_result(self, data: dict[str, Any], at: datetime) -> None:
        tool = str(data.get("tool") or "")
        output, truncated = _cap_output(data.get("output"))
        for index, (step, _provisional) in enumerate(self._open):
            if step["tool"] == tool:
                del self._open[index]
                break
        else:
            if self._dropped_open.get(tool):
                # Its call was dropped by the cap and already counted.
                self._dropped_open[tool] -= 1
                return
            # A result with no recorded call: keep it as a finished step.
            step = self._new_step("tool", at, tool=tool)
            if not self._append(step):
                return
        step["output"] = output
        step["output_truncated"] = truncated
        step["status"] = "complete"
        step["ended_at"] = at

    # -- reading -------------------------------------------------------------

    def finalize(self, now: datetime | None = None) -> None:
        """Close the run's record. Idempotent.

        A call still open never got its result (the run failed, was cancelled
        or was killed mid-tool): it becomes ``missing_result`` so the UI can say
        so instead of spinning forever.
        """
        if self._finalized:
            return
        at = now or _now()
        self._close_thinking(at)
        for step, _provisional in self._open:
            step["status"] = "missing_result"
            step["ended_at"] = at
        self._open = []
        # The byte budget. Checked here, not per event, because a
        # step's size is only known once its result has landed.
        total = 0
        for index, step in enumerate(self._steps):
            total += len(json.dumps(step, default=str))
            if total > self._max_total_bytes:
                self._omitted += len(self._steps) - index
                del self._steps[index:]
                break
        self._finalized = True

    @property
    def steps(self) -> list[dict[str, Any]]:
        return self._steps

    @property
    def steps_omitted(self) -> int:
        return self._omitted

    def persist_kwargs(self) -> dict[str, Any]:
        """``steps`` / ``steps_omitted`` for the Message write, or ``{}``.

        Withheld when there is nothing to store, so a plain-text run's writes
        (and every existing fake of them) are unchanged.
        """
        self.finalize()
        if not self._steps and not self._omitted:
            return {}
        return {"steps": list(self._steps), "steps_omitted": self._omitted}


def record_agent_event(
    recorder: StepRecorder,
    event: Any,
    tool_name: str = "",
    tool_input: Any = None,
    narration: str | None = None,
    now: datetime | None = None,
) -> None:
    """Adapt one ``AgentEvent`` to the run_core frame the recorder takes.

    ``tool_use`` arrives with its name, input and narration already resolved by
    the caller (the bridge resolves them for its WS chip too). ``tool_result``
    resolves its own name from ``metadata`` or a dict ``content``. ``now`` is the
    event's own time when the source carries one (the belt feed parses it off
    each CLI line); otherwise the recorder stamps the moment it observes.
    """
    etype = getattr(event, "type", "")
    content = getattr(event, "content", None)
    meta = getattr(event, "metadata", None)
    meta = meta if isinstance(meta, dict) else {}
    if etype == "thinking":
        text = content if isinstance(content, str) else ""
        recorder.observe("thinking", {"content": text}, now)
    elif etype in {"message", "text"}:
        recorder.observe("chunk", {}, now)
    elif etype == "tool_use":
        frame: dict[str, Any] = {"tool": tool_name, "input": tool_input}
        if narration:
            frame["narration"] = narration
        if meta.get("input_pending") is True:
            frame["input_pending"] = True
        recorder.observe("tool_start", frame, now)
    elif etype == "tool_result":
        name = meta.get("name") or meta.get("tool") or ""
        output: Any = content
        if isinstance(content, dict):
            name = name or content.get("tool") or content.get("name") or ""
            output = content.get("result", content)
        recorder.observe("tool_result", {"tool": name, "output": output}, now)


def steps_wire_fields(steps: Any, steps_omitted: int = 0) -> dict[str, Any]:
    """The ``steps`` / ``stepsOmitted`` wire keys for a message, or ``{}``.

    ``steps`` may hold the persistence model, the domain dataclass or plain
    dicts. Keys are emitted only when non-empty, so a message with no steps
    keeps its existing payload exactly.
    """
    out: dict[str, Any] = {}
    if steps:
        out["steps"] = [_step_to_wire(s) for s in steps]
    if steps_omitted:
        out["stepsOmitted"] = steps_omitted
    return out


def _step_to_wire(step: Any) -> dict[str, Any]:
    def get(name: str, default: Any = None) -> Any:
        if isinstance(step, dict):
            return step.get(name, default)
        return getattr(step, name, default)

    return {
        "id": get("id", ""),
        "kind": get("kind", "tool"),
        "tool": get("tool", ""),
        "narration": get("narration", ""),
        "text": get("text", ""),
        "input": get("input"),
        "output": get("output", ""),
        "outputTruncated": bool(get("output_truncated", False)),
        "status": get("status", "complete"),
        "startedAt": iso_utc(get("started_at")),
        "endedAt": iso_utc(get("ended_at")),
    }


__all__ = ["StepRecorder", "record_agent_event", "steps_wire_fields"]
