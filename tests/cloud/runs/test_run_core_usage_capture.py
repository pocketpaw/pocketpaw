# Tests for the RFC 11 token-usage capture helper in run_core.
# Created: 2026-05-29 — TDD coverage for the EE-side metering seam. The cloud
#   run path emits a `token_usage` AgentEvent from claude_sdk.py that was
#   previously dropped on the floor, which is why the stream_end frame always
#   carried `"usage": {}`. RFC 11 wires it: capture the usage off the event and
#   (when an inference-gateway provider is registered) forward it to
#   provider.record_usage(...).
#
# The full _drive_agent_loop / execute_run path needs Mongo + Redis cloud
# fixtures, so the capture logic is extracted into a pure helper and unit-tested
# here. The end-to-end wiring (loop -> stream_end usage field) is covered by an
# integration gap noted in the PR body.

from __future__ import annotations

from typing import Any

from pocketpaw_ee.cloud.chat.runs.run_core import usage_from_token_event


class _FakeEvent:
    def __init__(self, metadata: dict[str, Any] | None) -> None:
        self.type = "token_usage"
        self.content = ""
        self.metadata = metadata


def test_usage_from_token_event_maps_metadata():
    ev = _FakeEvent(
        {
            "input_tokens": 1200,
            "output_tokens": 340,
            "cached_input_tokens": 800,
            "total_cost_usd": 0.0123,
            "model": "claude-haiku",
            "backend": "claude_agent_sdk",
        }
    )
    usage = usage_from_token_event(ev)
    assert usage["input_tokens"] == 1200
    assert usage["output_tokens"] == 340
    assert usage["cached_input_tokens"] == 800
    assert usage["cost_usd"] == 0.0123
    assert usage["model"] == "claude-haiku"


def test_usage_from_token_event_handles_missing_metadata():
    assert usage_from_token_event(_FakeEvent(None)) == {}
    assert usage_from_token_event(_FakeEvent({})) == {}


def test_usage_from_token_event_defaults_missing_numbers_to_zero():
    usage = usage_from_token_event(_FakeEvent({"model": "claude"}))
    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0
    assert usage["cost_usd"] == 0.0
    assert usage["model"] == "claude"


def test_usage_from_token_event_handles_null_cost():
    # claude_sdk emits total_cost_usd=None when the SDK omits it.
    usage = usage_from_token_event(
        _FakeEvent({"input_tokens": 5, "output_tokens": 3, "total_cost_usd": None})
    )
    assert usage["cost_usd"] == 0.0
    assert usage["input_tokens"] == 5
