# Unit tests for the plan normalizer + per-run PlanTracker (HTN-5).
# Created: 2026-08-15 — covers the canonical shape, the four-state enum
# round-trip, the change-hash coalescing that keeps the panel from flickering,
# and the monotonic ``seq``.
#
# The ``write_plan`` fixtures use the shape the tool ACTUALLY receives — the
# argument dict pydantic-ai puts on ``AgentEvent.metadata["input"]``, i.e.
# ``{"items": [{"content": ..., "status": ...}]}`` with status as the raw JSON
# string the model emits. See ``pydantic_ai_harness/planning/_toolset.py``.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.shared.plan_normalizer import (
    MAX_CONTENT_CHARS,
    MAX_ITEMS,
    PlanTracker,
    is_plan_tool,
    normalize_plan,
    plan_progress,
    plan_tools,
)


def _write_plan_args(*pairs: tuple[str, str]) -> dict:
    return {"items": [{"content": content, "status": status} for content, status in pairs]}


THREE_STEP = _write_plan_args(
    ("Add the database migration", "completed"),
    ("Wire the endpoint", "in_progress"),
    ("Backfill the existing rows", "pending"),
)


# --- registry ---------------------------------------------------------------


def test_only_write_plan_is_registered():
    """HTN-6 owns the other two. A guessed ``TodoWrite`` normalizer would give
    the default backend's users a panel that is silently blank."""
    assert plan_tools() == frozenset({"write_plan"})
    assert is_plan_tool("write_plan")
    assert not is_plan_tool("TodoWrite")
    assert not is_plan_tool("write_todos")
    assert not is_plan_tool("")


def test_unregistered_tool_normalizes_to_nothing():
    assert normalize_plan("web_search", {"query": "x"}) == []


# --- canonical shape --------------------------------------------------------


def test_write_plan_normalizes_to_the_canonical_items():
    items = normalize_plan("write_plan", THREE_STEP)

    assert [item.as_dict() for item in items] == [
        {"id": "1", "content": "Add the database migration", "status": "completed"},
        {"id": "2", "content": "Wire the endpoint", "status": "in_progress"},
        {"id": "3", "content": "Backfill the existing rows", "status": "pending"},
    ]


def test_progress_counts_completed_against_the_whole_list():
    items = normalize_plan("write_plan", THREE_STEP)
    assert plan_progress(items) == {"completed": 1, "total": 3}


def test_the_four_state_enum_round_trips_including_cancelled():
    """``cancelled`` is the state only ``write_plan`` has; it must survive
    unmangled and must NOT count as completed."""
    items = normalize_plan(
        "write_plan",
        _write_plan_args(
            ("one", "pending"),
            ("two", "in_progress"),
            ("three", "completed"),
            ("four", "cancelled"),
        ),
    )

    assert [item.status for item in items] == [
        "pending",
        "in_progress",
        "completed",
        "cancelled",
    ]
    # cancelled sits in the denominator but not the numerator, matching the
    # harness's own render_plan summary.
    assert plan_progress(items) == {"completed": 1, "total": 4}


def test_enum_valued_status_is_accepted():
    """A backend that validated the args before announcing hands us the real
    ``TaskStatus`` member rather than its string."""
    from enum import Enum

    # Declared exactly as the harness declares it — ``StrEnum`` would be the
    # modern spelling but would stop mirroring the class we actually receive.
    class TaskStatus(str, Enum):  # noqa: UP042
        in_progress = "in_progress"

    items = normalize_plan(
        "write_plan", {"items": [{"content": "x", "status": TaskStatus.in_progress}]}
    )
    assert items[0].status == "in_progress"


# --- defensive shaping ------------------------------------------------------


def test_missing_status_defaults_to_pending():
    items = normalize_plan("write_plan", {"items": [{"content": "no status here"}]})
    assert items[0].status == "pending"


def test_unknown_status_falls_back_to_pending():
    """The frontend switches exhaustively on the four states, so an unknown
    value must never reach the wire."""
    items = normalize_plan("write_plan", {"items": [{"content": "x", "status": "blocked"}]})
    assert items[0].status == "pending"


def test_newlines_are_collapsed_into_one_renderable_line():
    items = normalize_plan("write_plan", {"items": [{"content": "step one\n\n  and more"}]})
    assert items[0].content == "step one and more"


def test_contentless_entries_are_dropped_and_ids_stay_contiguous():
    items = normalize_plan(
        "write_plan",
        {
            "items": [
                {"content": "real"},
                {"status": "pending"},
                {"content": "   "},
                {"content": "also real"},
            ]
        },
    )
    assert [(item.id, item.content) for item in items] == [("1", "real"), ("2", "also real")]


def test_oversized_plans_are_bounded():
    long_content = "x" * (MAX_CONTENT_CHARS + 50)
    items = normalize_plan(
        "write_plan",
        {"items": [{"content": long_content} for _ in range(MAX_ITEMS + 10)]},
    )
    assert len(items) == MAX_ITEMS
    assert len(items[0].content) == MAX_CONTENT_CHARS


def test_a_json_string_items_argument_still_decodes():
    """Belt and braces for a backend that passes tool args through un-decoded."""
    items = normalize_plan("write_plan", {"items": '[{"content": "one", "status": "completed"}]'})
    assert [item.as_dict() for item in items] == [
        {"id": "1", "content": "one", "status": "completed"}
    ]


@pytest.mark.parametrize("args", [{}, {"items": None}, {"items": []}, {"items": "not json"}, None])
def test_unusable_arguments_normalize_to_nothing_without_raising(args):
    assert normalize_plan("write_plan", args) == []


# --- PlanTracker ------------------------------------------------------------


def test_tracker_emits_the_full_contract_on_first_observation():
    tracker = PlanTracker(run_id="run-1")
    observation = tracker.observe("write_plan", THREE_STEP)

    assert observation.recognized
    assert observation.payload == {
        "run_id": "run-1",
        "seq": 1,
        "items": [
            {"id": "1", "content": "Add the database migration", "status": "completed"},
            {"id": "2", "content": "Wire the endpoint", "status": "in_progress"},
            {"id": "3", "content": "Backfill the existing rows", "status": "pending"},
        ],
        "progress": {"completed": 1, "total": 3},
    }


def test_an_identical_repeat_is_recognized_but_emits_nothing():
    """``write_plan`` fires at the start AND the end of every step, resending
    the whole list. Without this the channel floods and the panel flickers."""
    tracker = PlanTracker(run_id="run-1")

    first = tracker.observe("write_plan", THREE_STEP)
    second = tracker.observe("write_plan", THREE_STEP)

    assert first.payload is not None
    assert second.recognized, "still a plan call — the caller must not fall back to a tool chip"
    assert second.payload is None
    assert tracker.seq == 1, "a suppressed repeat must not burn a seq"


def test_seq_increases_across_genuine_updates():
    tracker = PlanTracker(run_id="run-1")

    seqs = []
    for status in ("pending", "in_progress", "completed"):
        observation = tracker.observe("write_plan", _write_plan_args(("Wire the endpoint", status)))
        assert observation.payload is not None
        seqs.append(observation.payload["seq"])

    assert seqs == [1, 2, 3]


def test_a_repeat_between_two_changes_does_not_break_monotonicity():
    tracker = PlanTracker(run_id="run-1")
    plan_a = _write_plan_args(("one", "pending"))
    plan_b = _write_plan_args(("one", "completed"))

    assert tracker.observe("write_plan", plan_a).payload["seq"] == 1
    assert tracker.observe("write_plan", plan_a).payload is None
    assert tracker.observe("write_plan", plan_b).payload["seq"] == 2


def test_a_non_plan_tool_is_unrecognized():
    tracker = PlanTracker(run_id="run-1")
    observation = tracker.observe("web_search", {"query": "quarterly filings"})

    assert not observation.recognized
    assert observation.payload is None


def test_an_unreadable_plan_call_is_unrecognized_so_the_caller_can_fall_back():
    """pydantic-ai announces the call from ``PartStartEvent`` with ``input={}``
    before the arguments finish streaming (HTN-9). Reporting that as
    unrecognized is what keeps the tool chip as the fallback."""
    tracker = PlanTracker(run_id="run-1")
    observation = tracker.observe("write_plan", {})

    assert not observation.recognized
    assert observation.payload is None
    assert tracker.seq == 0


def test_trackers_do_not_share_state_across_runs():
    plan = THREE_STEP
    assert PlanTracker(run_id="run-1").observe("write_plan", plan).payload["seq"] == 1
    assert PlanTracker(run_id="run-2").observe("write_plan", plan).payload["seq"] == 1
