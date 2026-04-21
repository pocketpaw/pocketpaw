# tests/ee/test_pocket_journal_stream.py — FastAPI TestClient coverage for the
# pocket journal SSE router shipped in feat/cluster-b-ripple-journal-stream.
# Created: 2026-04-19 — Cluster B / Wave 3 §11 — RippleGraphWidget. Pins the
# route's contract with the frontend widget: (1) filters the shared org journal
# down to events that carry ``payload.pocket_id`` equal to the path pocket, or
# a scope entry ``pocket:<id>``; (2) emits one ``event: journal`` SSE frame per
# match, JSON-encoded with the shape the widget reader consumes; (3) respects
# the ``since_seq`` cursor so reconnects do not replay the whole backlog; (4)
# leaks no events from other pockets.
#
# Auth + pocket access are stubbed by overriding ``require_pocket_edit`` — the
# real dep reads from beanie/Mongo. These are route-contract tests, not end-to-
# end scope tests; Cluster B security-auditor covers the access-control matrix.

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from soul_protocol.engine.journal import open_journal
from soul_protocol.spec.journal import Actor, EventEntry

from ee.cloud.license import require_license
from ee.cloud.pockets.journal_stream_router import (
    router,
    _entry_matches_pocket,
)
from ee.cloud.shared.deps import require_pocket_edit
from ee.journal_dep import get_journal, reset_journal_cache


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_journal_cache() -> None:
    """Clear the module-level journal cache between tests.

    Tests override ``get_journal`` via ``dependency_overrides`` but a stale
    cached instance from a sibling test could leak through if the override
    resolves early. The fixture is belt-and-braces.
    """

    reset_journal_cache()
    yield
    reset_journal_cache()


@pytest.fixture
def journal_path(tmp_path: Path) -> Path:
    """A disposable SQLite file the override points at."""

    return tmp_path / "pocket_journal_stream.db"


@pytest.fixture
def app(journal_path: Path) -> FastAPI:
    """FastAPI app with the stream router + deps overridden.

    ``require_pocket_edit`` is replaced with a no-op that accepts every
    caller, and ``require_license`` short-circuits the same way. Tests that
    want to exercise the 403 path can override these back in-line.
    """

    a = FastAPI()
    a.include_router(router, prefix="/api/v1")
    a.dependency_overrides[get_journal] = lambda: open_journal(journal_path)
    a.dependency_overrides[require_pocket_edit] = lambda pocket_id: None
    a.dependency_overrides[require_license] = lambda: None
    return a


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _append(
    journal, *, action: str, pocket_id: str | None, scope: list[str] | None = None
) -> int:
    """Append one event to the test journal and return its seq.

    Takes a kwargs-only shape so tests read more like specs than scratch
    snippets — the shape mirrors how ``ee/widget/store.py`` builds entries.
    The installed soul-protocol (0.3.1) ``Journal.append`` returns None, so
    we read seq from the backend tail after the write. That matches how
    :func:`_drain_since` in the router resolves seqs.
    """

    entry = EventEntry(
        id=uuid4(),
        ts=datetime.now(UTC),
        actor=Actor(kind="system", id="system:test", scope_context=[]),
        action=action,
        scope=scope or ["org:test"],
        payload={"pocket_id": pocket_id} if pocket_id else {},
    )
    journal.append(entry)
    tail = journal._backend.last_entry()
    assert tail is not None
    _, seq = tail
    return seq


def _read_sse_frames(response_text: str) -> list[dict]:
    """Parse the SSE body into a list of ``{event, data}`` dicts.

    The route emits one ``event: <name>\\ndata: <json>\\n\\n`` block per
    entry. Keepalive comments (``: keepalive\\n\\n``) are discarded so
    assertions can focus on business events.
    """

    frames: list[dict] = []
    for block in response_text.split("\n\n"):
        if not block.strip() or block.strip().startswith(":"):
            continue
        event = None
        data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = line[len("data:"):].strip()
        if event is not None and data is not None:
            frames.append({"event": event, "data": json.loads(data)})
    return frames


# ---------------------------------------------------------------------------
# Unit-level — filter predicate
# ---------------------------------------------------------------------------


class TestEntryMatchesPocket:
    def test_matches_by_payload_pocket_id(self) -> None:
        """Events whose ``payload.pocket_id`` equals the path arg are
        included. This is the dominant convention used by the widget and
        retrieval stores today.
        """

        entry = EventEntry(
            id=uuid4(),
            ts=datetime.now(UTC),
            actor=Actor(kind="system", id="system:t", scope_context=[]),
            action="widget.interaction.recorded",
            scope=["org:test"],
            payload={"pocket_id": "p-target"},
        )
        assert _entry_matches_pocket(entry, "p-target") is True
        assert _entry_matches_pocket(entry, "p-other") is False

    def test_matches_by_scope_pocket_prefix(self) -> None:
        """A scope entry of the form ``pocket:<id>`` also counts as a
        match so future writers can tag scope instead of payload without
        a stream-route change.
        """

        entry = EventEntry(
            id=uuid4(),
            ts=datetime.now(UTC),
            actor=Actor(kind="system", id="system:t", scope_context=[]),
            action="retrieval.query",
            scope=["org:test", "pocket:p-target"],
            payload={},
        )
        assert _entry_matches_pocket(entry, "p-target") is True

    def test_does_not_match_dataref_payload(self) -> None:
        """Zero-Copy DataRef payloads carry no ``pocket_id`` — matching
        should rely on scope alone, not crash on the non-dict payload.
        """

        from soul_protocol.spec.journal import DataRef

        entry = EventEntry(
            id=uuid4(),
            ts=datetime.now(UTC),
            actor=Actor(kind="system", id="system:t", scope_context=[]),
            action="dataref.resolved",
            scope=["org:test"],
            payload=DataRef(
                source="salesforce",
                query="SELECT Id FROM Opp",
                point_in_time=datetime.now(UTC),
            ),
        )
        assert _entry_matches_pocket(entry, "p-target") is False


# ---------------------------------------------------------------------------
# Route-level — SSE frame shape + filtering
# ---------------------------------------------------------------------------


class TestStreamPocketJournal:
    def test_emits_connected_handshake_with_last_seq(
        self, client: TestClient, journal_path: Path
    ) -> None:
        """The first SSE frame is the ``connected`` handshake so the
        widget can pin its resume cursor before the first journal frame.
        """

        journal = open_journal(journal_path)
        _append(journal, action="widget.interaction.recorded", pocket_id="p-1")
        journal.close()

        body = _drain_stream(client, "/api/v1/pockets/p-1/journal/stream")
        frames = _read_sse_frames(body)
        assert frames[0]["event"] == "connected"
        assert frames[0]["data"]["pocket_id"] == "p-1"
        # SQLite backend assigns seq starting at 0 for the first event. The
        # handshake just has to echo whatever the backend sees — assert
        # non-negative rather than hard-coding 1 so a future seq-from-1
        # migration would not flake this test.
        assert frames[0]["data"]["last_seq"] >= 0

    def test_emits_backlog_for_matching_events_only(
        self, client: TestClient, journal_path: Path
    ) -> None:
        """Only events that match the path pocket_id are streamed —
        cross-pocket leakage would be a scope bug.
        """

        journal = open_journal(journal_path)
        _append(journal, action="widget.interaction.recorded", pocket_id="p-target")
        _append(journal, action="widget.interaction.recorded", pocket_id="p-other")
        _append(journal, action="retrieval.query", pocket_id="p-target")
        journal.close()

        body = _drain_stream(client, "/api/v1/pockets/p-target/journal/stream")
        frames = _read_sse_frames(body)
        journal_frames = [f for f in frames if f["event"] == "journal"]
        assert len(journal_frames) == 2
        for f in journal_frames:
            assert f["data"]["payload"]["pocket_id"] == "p-target"

    def test_respects_since_seq_cursor(
        self, client: TestClient, journal_path: Path
    ) -> None:
        """``since_seq`` skips events whose seq is ``<=`` the cursor so
        reconnects do not replay the whole backlog.
        """

        journal = open_journal(journal_path)
        seq_a = _append(journal, action="agent.proposed", pocket_id="p-1")
        seq_b = _append(journal, action="human.corrected", pocket_id="p-1")
        journal.close()

        body = _drain_stream(
            client,
            "/api/v1/pockets/p-1/journal/stream",
            params={"since_seq": seq_a},
        )
        frames = _read_sse_frames(body)
        journal_frames = [f for f in frames if f["event"] == "journal"]
        seqs = [f["data"]["seq"] for f in journal_frames]
        assert seq_b in seqs
        assert seq_a not in seqs

    def test_frame_carries_widget_contract_fields(
        self, client: TestClient, journal_path: Path
    ) -> None:
        """The JSON payload of each ``journal`` frame matches the shape
        the RippleGraphWidget reader expects — ``id``, ``seq``, ``ts``,
        ``action``, ``actor``, ``scope``, ``correlation_id``,
        ``causation_id``, ``payload``.
        """

        journal = open_journal(journal_path)
        _append(journal, action="retrieval.query", pocket_id="p-1")
        journal.close()

        body = _drain_stream(client, "/api/v1/pockets/p-1/journal/stream")
        frames = _read_sse_frames(body)
        journal_frames = [f for f in frames if f["event"] == "journal"]
        assert journal_frames, "expected at least one journal frame"
        data = journal_frames[0]["data"]
        assert set(data.keys()) == {
            "id",
            "seq",
            "ts",
            "action",
            "actor",
            "scope",
            "causation_id",
            "correlation_id",
            "payload",
        }
        assert data["actor"] == {"kind": "system", "id": "system:test"}

    def test_empty_pocket_still_emits_connected(
        self, client: TestClient, journal_path: Path
    ) -> None:
        """A pocket with zero journal events still receives the
        ``connected`` handshake so the widget can render the cold-start
        empty state instead of hanging.
        """

        # Write an unrelated event so the journal file exists.
        journal = open_journal(journal_path)
        _append(journal, action="widget.interaction.recorded", pocket_id="p-other")
        journal.close()

        body = _drain_stream(client, "/api/v1/pockets/p-empty/journal/stream")
        frames = _read_sse_frames(body)
        assert any(f["event"] == "connected" for f in frames)
        assert [f for f in frames if f["event"] == "journal"] == []

    def test_stream_closes_on_max_idle_polls(
        self, client: TestClient, journal_path: Path
    ) -> None:
        """The ``max_idle_polls`` debug knob short-circuits the long-poll
        loop so the stream terminates with a ``closed`` frame once the
        backlog has drained. Without it, the connection would stay open
        for the life of the page — useful in prod, catastrophic in a
        synchronous TestClient.
        """

        journal = open_journal(journal_path)
        _append(journal, action="agent.proposed", pocket_id="p-1")
        journal.close()

        body = _drain_stream(client, "/api/v1/pockets/p-1/journal/stream")
        frames = _read_sse_frames(body)
        assert any(f["event"] == "closed" for f in frames)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _drain_stream(
    client: TestClient,
    path: str,
    *,
    params: dict[str, object] | None = None,
    max_idle_polls: int = 1,
) -> str:
    """Drive the SSE endpoint to completion by asking the server to close
    the stream after ``max_idle_polls`` idle polls, then return the full
    response body.

    Without the ``max_idle_polls`` debug knob the route would hold the
    connection open for the life of the page and ``TestClient.stream``
    would hang forever — FastAPI's sync TestClient does not surface the
    same cancellation signals that a real ASGI transport would.
    """

    full_params: dict[str, object] = {"max_idle_polls": max_idle_polls}
    if params:
        full_params.update(params)

    with client.stream("GET", path, params=full_params) as resp:
        assert resp.status_code == 200
        chunks = [c for c in resp.iter_raw()]
    return b"".join(chunks).decode("utf-8")
