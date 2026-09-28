# tests/cloud/runs/test_run_step_recorder.py
# Created 2026-09-28 (feat/persist-tool-steps). Unit coverage around the step
# recorder that test_run_tool_steps.py drives end to end: the ``input_pending``
# plumbing in ``_drive_agent_loop`` (production's only source of the flag), the
# caps the end-to-end tests don't reach (byte budget, input size, thinking
# redaction), and the wire keys on the two group-message mappers.
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind
from pocketpaw_ee.cloud.chat.domain import Message as DomainMessage
from pocketpaw_ee.cloud.chat.domain import MessageStep as DomainStep
from pocketpaw_ee.cloud.chat.dto import message_to_wire_dict
from pocketpaw_ee.cloud.chat.runs import run_core
from pocketpaw_ee.cloud.chat.runs.steps import (
    MAX_INPUT_CHARS,
    MAX_TOTAL_BYTES,
    StepRecorder,
    steps_wire_fields,
)

_SECRET = "sk-" + "a1B2c3D4e5F6g7H8i9J0" * 3


# ---------------------------------------------------------------------------
# _drive_agent_loop forwards the backend's provisional flag
# ---------------------------------------------------------------------------


async def _tool_start_frames(monkeypatch, events: list[Any]) -> list[dict]:
    class _FakePool:
        async def get(self, _agent_id):
            return SimpleNamespace(config={}, agent_name="A", backend=None)

        def run(self, *_a, **_k):
            async def _gen():
                for ev in events:
                    yield ev

            return _gen()

    async def _empty(*_a, **_k):
        return ""

    async def _never_cancelled():
        return False

    monkeypatch.setattr(run_core, "get_agent_pool", lambda: _FakePool())
    monkeypatch.setattr(run_core, "build_knowledge_context", _empty)
    monkeypatch.setattr(run_core, "build_behavior_instructions", lambda ctx, backend_name=None: "")
    monkeypatch.setattr(run_core, "attach_sse_event_sink", lambda q: None)
    monkeypatch.setattr(run_core, "attach_agent_identity", lambda **k: None)
    monkeypatch.setattr(run_core, "detach_sse_event_sink", lambda t: None)
    monkeypatch.setattr(run_core, "detach_agent_identity", lambda t: None)

    ctx = ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
    )
    frames = []
    async for name, data in run_core._drive_agent_loop(
        ctx,
        user_content="go",
        attachments_in=None,
        mentions_in=None,
        history=None,
        is_cancelled=_never_cancelled,
        emit_stream_start=False,
    ):
        if name == "tool_start":
            frames.append(data)
    return frames


@pytest.mark.asyncio
async def test_tool_start_carries_input_pending_only_when_the_backend_flags_it(monkeypatch):
    def use(meta):
        return SimpleNamespace(type="tool_use", content="Using read_file...", metadata=meta)

    frames = await _tool_start_frames(
        monkeypatch,
        [
            use({"name": "read_file", "input": {}, "input_pending": True}),
            use({"name": "read_file", "input": {"path": "a"}, "input_pending": False}),
            use({"name": "read_file", "input": {"path": "b"}}),
            SimpleNamespace(type="done", content=""),
        ],
    )
    assert [f.get("input_pending") for f in frames] == [True, None, None]
    assert "input_pending" not in frames[1] and "input_pending" not in frames[2]


# ---------------------------------------------------------------------------
# Recorder caps
# ---------------------------------------------------------------------------


def test_thinking_is_redacted_and_closed_by_text():
    rec = StepRecorder()
    rec.observe("thinking", {"content": f"key is {_SECRET}"})
    rec.observe("chunk", {"content": "answer"})
    rec.observe("thinking", {"content": "second thought"})
    rec.finalize()
    assert [s["kind"] for s in rec.steps] == ["thinking", "thinking"]
    assert _SECRET not in rec.steps[0]["text"]
    assert all(s["ended_at"] is not None for s in rec.steps)


def test_a_huge_input_is_stored_as_a_truncated_string():
    rec = StepRecorder()
    rec.observe("tool_start", {"tool": "write_file", "input": {"body": "x" * 10_000}})
    rec.finalize()
    stored = rec.steps[0]["input"]
    assert isinstance(stored, str) and len(stored) <= MAX_INPUT_CHARS + 1


def test_a_result_with_no_call_is_kept_as_a_finished_step():
    rec = StepRecorder()
    rec.observe("tool_result", {"tool": "grep", "output": {"hits": 3}})
    rec.finalize()
    (step,) = rec.steps
    assert step["status"] == "complete" and step["output"] == '{"hits": 3}'


def test_the_byte_budget_omits_the_tail():
    rec = StepRecorder()
    for i in range(40):
        rec.observe("tool_start", {"tool": "cat", "input": {"i": i}})
        rec.observe("tool_result", {"tool": "cat", "output": "y" * 4000})
    rec.finalize()
    assert 0 < len(rec.steps) < 40
    assert rec.steps_omitted == 40 - len(rec.steps)
    assert sum(len(str(s)) for s in rec.steps) < MAX_TOTAL_BYTES * 1.2


def test_nothing_recorded_means_no_persist_kwargs():
    rec = StepRecorder()
    rec.observe("chunk", {"content": "hi"})
    rec.observe("plan_updated", {"steps": []})
    assert rec.persist_kwargs() == {}


# ---------------------------------------------------------------------------
# Group-message wire mappers
# ---------------------------------------------------------------------------


def _domain_message(**kw) -> DomainMessage:
    return DomainMessage(
        id="m1",
        context_type="group",
        workspace_id="w1",
        group="g1",
        sender=None,
        sender_type="agent",
        sender_name=None,
        agent="a1",
        content="done",
        **kw,
    )


def test_message_to_wire_dict_emits_steps_only_when_present():
    assert "steps" not in message_to_wire_dict(_domain_message())
    at = datetime(2026, 9, 28, tzinfo=UTC)
    step = DomainStep(id="s1", kind="tool", tool="grep", status="complete", started_at=at)
    wire = message_to_wire_dict(_domain_message(steps=(step,), steps_omitted=2))
    assert wire["steps"][0]["tool"] == "grep"
    assert wire["steps"][0]["startedAt"] == at.isoformat()
    assert wire["steps"][0]["outputTruncated"] is False
    assert wire["stepsOmitted"] == 2


def test_steps_wire_fields_is_empty_for_a_plain_message():
    assert steps_wire_fields([], 0) == {}
    assert steps_wire_fields(None) == {}
