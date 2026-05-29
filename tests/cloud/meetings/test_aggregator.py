# Tests for src/pocketpaw/connectors/adapters/meetings_aggregator.py

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.meetings.providers.recall.adapters.meetings_aggregator import (
    MeetingsAggregatorConnector,
)

from pocketpaw.connectors.protocol import ConnectorStatus, ExecutionMode


@pytest.fixture
def aggregator():
    calls: list[tuple[str, tuple, dict]] = []

    async def fake_search(workspace_id, *, query, since, until, limit):
        calls.append(
            (
                "search",
                (workspace_id,),
                {
                    "query": query,
                    "since": since,
                    "until": until,
                    "limit": limit,
                },
            )
        )
        return [{"id": "m1", "title": f"about {query}"}]

    async def fake_list_recent(workspace_id, *, limit):
        calls.append(("list_recent", (workspace_id,), {"limit": limit}))
        return [{"id": "m1"}, {"id": "m2"}]

    async def fake_get_transcript(workspace_id, meeting_id):
        calls.append(("get_transcript", (workspace_id, meeting_id), {}))
        return {"meeting_id": meeting_id, "file_id": "f1", "entry_count": 42}

    agg = MeetingsAggregatorConnector(
        "ws-1",
        search_fn=fake_search,
        list_recent_fn=fake_list_recent,
        get_transcript_fn=fake_get_transcript,
    )
    return agg, calls


def test_metadata(aggregator):
    a, _ = aggregator
    assert a.name == "meetings"
    assert a.display_name == "Meetings"


async def test_actions_surface(aggregator):
    """All read-only, AUTO trust level."""
    from pocketpaw.connectors.protocol import TrustLevel

    a, _ = aggregator
    schemas = await a.actions()
    assert {s.name for s in schemas} == {"search", "list_recent", "get_transcript_by_id"}
    assert all(s.trust_level == TrustLevel.AUTO for s in schemas)
    assert all(s.execution_mode == ExecutionMode.CLOUD for s in schemas)


async def test_search_dispatches_to_callback(aggregator):
    a, calls = aggregator
    result = await a.execute("search", {"query": "Acme", "limit": 5})
    assert result.success
    assert result.records_affected == 1
    # workspace_id was forwarded from constructor, NOT the params.
    assert calls[0][1] == ("ws-1",)
    assert calls[0][2]["query"] == "Acme"
    assert calls[0][2]["limit"] == 5


async def test_search_parses_iso_bounds(aggregator):
    from datetime import UTC, datetime

    a, calls = aggregator
    await a.execute(
        "search",
        {
            "query": "x",
            "since": "2026-01-01T00:00:00Z",
            "until": "2026-12-31T23:59:59Z",
        },
    )
    kw = calls[0][2]
    assert kw["since"] == datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert kw["until"] == datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)


async def test_list_recent(aggregator):
    a, calls = aggregator
    result = await a.execute("list_recent", {"limit": 25})
    assert result.success
    assert result.records_affected == 2
    assert calls[0][2] == {"limit": 25}


async def test_get_transcript_by_id(aggregator):
    a, _ = aggregator
    result = await a.execute("get_transcript_by_id", {"meeting_id": "m1"})
    assert result.success
    assert result.data["meeting_id"] == "m1"


async def test_get_transcript_missing_param(aggregator):
    a, _ = aggregator
    result = await a.execute("get_transcript_by_id", {})
    assert result.success is False
    assert "Missing required param: meeting_id" in result.error


async def test_unknown_action(aggregator):
    a, _ = aggregator
    result = await a.execute("nope", {})
    assert result.success is False
    assert "Unknown action" in result.error


async def test_callback_exception_wrapped(aggregator):
    async def bad_search(*a, **kw):
        raise RuntimeError("mongo unreachable")

    agg = MeetingsAggregatorConnector(
        "ws-1",
        search_fn=bad_search,
        list_recent_fn=bad_search,
        get_transcript_fn=bad_search,
    )
    result = await agg.execute("search", {"query": "x"})
    assert result.success is False
    assert "mongo unreachable" in result.error


async def test_health(aggregator):
    a, _ = aggregator
    h = await a.health()
    assert h.ok is True
    assert h.status == ConnectorStatus.CONNECTED


async def test_widget_recipe(aggregator):
    a, _ = aggregator
    widgets = await a.widgets()
    assert len(widgets) == 1
    assert widgets[0].action == "list_recent"


async def test_connect_is_a_noop(aggregator):
    a, _ = aggregator
    result = await a.connect("pocket-1", {})
    assert result.success
    assert result.connector_name == "meetings"
