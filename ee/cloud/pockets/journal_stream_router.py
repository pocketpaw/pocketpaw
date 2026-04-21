# ee/cloud/pockets/journal_stream_router.py — SSE feed for pocket-scoped journal events.
# Created: 2026-04-19 (Cluster B / Wave 3 §11 — RippleGraphWidget)
# Serves a live stream of org journal events filtered to a single pocket so the
# frontend RippleGraphWidget can render the pocket's decision + retrieval + tool
# trace as a live causation graph. The filter matches on either
# ``payload.pocket_id == pocket_id`` (the dominant convention used by ee.widget
# and ee.retrieval stores) or a scope entry of the form ``pocket:<id>`` (so
# callers who tag scope rather than payload still show up without a catalog
# rewrite). The stream replays events from ``since_seq`` for reconnects and
# polls every ``POLL_INTERVAL_SEC`` for new entries.

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from soul_protocol.engine.journal import Journal
from soul_protocol.spec.journal import DataRef, EventEntry

from ee.cloud.license import require_license
from ee.cloud.shared.deps import require_pocket_edit
from ee.journal_dep import get_journal

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/pockets",
    tags=["Pockets", "Journal"],
    dependencies=[Depends(require_license)],
)

# Cadence balances responsiveness (RippleGraphWidget spec asks for <2s node
# visibility) against SQLite read churn. 500ms keeps perceived latency below
# the ask while adding ≤2 QPS/connection on the WAL reader. The heartbeat
# matches the chat SSE convention (events.py) so intermediaries do not close
# the stream on idle.
POLL_INTERVAL_SEC = 0.5
HEARTBEAT_SEC = 30.0
INITIAL_BACKLOG_LIMIT = 200  # safety cap on first-connect replay


def _entry_matches_pocket(entry: EventEntry, pocket_id: str) -> bool:
    """Return True when an event belongs to the given pocket.

    Two sources of truth are in play today:

    * ``payload.pocket_id`` — how ``ee/widget/store.py`` and
      ``ee/retrieval/store.py`` tag the pocket on every event.
    * ``scope`` entry equal to ``pocket:<id>`` — proposed namespace for
      callers that prefer scope tagging over payload fields.

    Accept either so future writers do not have to coordinate with the
    stream route. The payload check short-circuits when the payload is a
    :class:`DataRef` (Zero-Copy external data); those events carry scope
    only.
    """
    scope_match = f"pocket:{pocket_id}"
    if scope_match in entry.scope:
        return True
    payload = entry.payload
    if isinstance(payload, DataRef):
        return False
    if not isinstance(payload, dict):
        return False
    value = payload.get("pocket_id")
    return isinstance(value, str) and value == pocket_id


def _encode_entry(entry: EventEntry, *, seq: int) -> str:
    """Shape one :class:`EventEntry` into the SSE frame the RippleGraphWidget
    reader consumes. Kept deterministic so contract tests can diff against a
    frozen snapshot.
    """
    payload_obj: Any
    payload = entry.payload
    if isinstance(payload, DataRef):
        payload_obj = {"__dataref__": True, **payload.model_dump(mode="json")}
    else:
        payload_obj = dict(payload)

    data = {
        "id": str(entry.id),
        "seq": seq,
        "ts": entry.ts.isoformat(),
        "action": entry.action,
        "actor": {
            "kind": entry.actor.kind,
            "id": entry.actor.id,
        },
        "scope": list(entry.scope),
        "causation_id": str(entry.causation_id) if entry.causation_id else None,
        "correlation_id": str(entry.correlation_id) if entry.correlation_id else None,
        "payload": payload_obj,
    }
    return f"event: journal\ndata: {json.dumps(data)}\n\n"


def _drain_since(
    journal: Journal, pocket_id: str, since_seq: int
) -> tuple[list[tuple[int, EventEntry]], int]:
    """Pull every pocket-matched entry strictly greater than ``since_seq``.

    Returns a pair of ``(matched, last_observed_seq)`` so the caller can
    advance its cursor even when all new entries were filtered out — that
    keeps the next poll from re-walking the same prefix. Replay is bounded
    by :data:`INITIAL_BACKLOG_LIMIT` so a very old cursor does not produce
    an unbounded burst on first connect.

    ``since_seq == -1`` is the cold-start sentinel — it asks for the entire
    backlog (up to :data:`INITIAL_BACKLOG_LIMIT`) without skipping seq 0.
    The backend assigns seqs starting at 0, so a naive ``since_seq + 1``
    start would drop the very first event of a freshly created pocket.

    The function reaches into ``journal._backend`` deliberately: the public
    ``Journal.query`` API flattens seq away, and the installed soul-protocol
    version (0.3.1) does not populate ``EventEntry.seq`` on the round-tripped
    entry. Keeping the seq cursor hand-in-hand with the row iterator is the
    only way today to stream with a resumable cursor.
    """
    matched: list[tuple[int, EventEntry]] = []
    last_seq = since_seq

    backend = journal._backend  # type: ignore[attr-defined]
    tail = backend.last_entry()
    if tail is None:
        return matched, since_seq
    _, tail_seq = tail
    if tail_seq <= since_seq:
        return matched, tail_seq

    # Cold start (since_seq == -1): read from the top of the available
    # window so seq=0 is included. Warm start: skip what we already saw.
    if since_seq < 0:
        start = max(0, tail_seq - INITIAL_BACKLOG_LIMIT + 1)
    else:
        start = since_seq + 1
    cur = backend._conn.execute(  # type: ignore[attr-defined]
        "SELECT * FROM events WHERE seq >= ? ORDER BY seq ASC", (start,)
    )
    for row in cur:
        entry, seq = backend._row_to_entry(row)  # type: ignore[attr-defined]
        last_seq = seq
        if _entry_matches_pocket(entry, pocket_id):
            matched.append((seq, entry))

    return matched, last_seq


@router.get("/{pocket_id}/journal/stream")
async def stream_pocket_journal(
    pocket_id: str,
    since_seq: int = Query(
        default=-1,
        ge=-1,
        description=(
            "Resume cursor. ``-1`` (the default) asks for the whole recent "
            "backlog (cold-start). Pass the ``seq`` of the last event the "
            "client already rendered to resume strictly after it — used by "
            "EventSource reconnects so the widget never double-renders a "
            "node."
        ),
    ),
    max_idle_polls: int | None = Query(
        default=None,
        ge=1,
        description=(
            "Debug/test hook — close the stream after this many idle polls "
            "(polls that yielded zero matched events). Production clients "
            "connect via EventSource and never pass this so the stream runs "
            "for the life of the page."
        ),
    ),
    journal: Journal = Depends(get_journal),
    _user=Depends(require_pocket_edit),
) -> StreamingResponse:
    """Subscribe to the pocket's journal as a live SSE stream.

    Emits three event types:

    * ``connected`` — initial handshake with the server-observed ``last_seq``
      so the client can pin its cursor without reading stale data.
    * ``journal`` — one per matched :class:`EventEntry`, newest-to-oldest
      after the initial backlog flushes, then strictly increasing thereafter.
    * ``: keepalive`` comments every :data:`HEARTBEAT_SEC` seconds so proxies
      do not tear down the connection on idle.

    The route is read-only — it does not mutate the journal. Scope enforcement
    runs through ``require_pocket_edit`` (the same guard the PATCH route uses)
    so only authorised pocket members receive events.
    """

    cancel_event = asyncio.Event()

    async def _event_generator():
        nonlocal_since = since_seq
        idle_polls = 0
        try:
            # Seed the client with the current cursor so a disconnect +
            # reconnect can resume without a SQL count.
            backend = journal._backend  # type: ignore[attr-defined]
            tail = backend.last_entry()
            tail_seq = tail[1] if tail else 0
            handshake = {"last_seq": tail_seq, "pocket_id": pocket_id}
            yield f"event: connected\ndata: {json.dumps(handshake)}\n\n"

            # Flush the backlog up front so the widget renders immediately on
            # mount instead of waiting for the next event.
            matched, nonlocal_since = _drain_since(journal, pocket_id, nonlocal_since)
            for seq, entry in matched:
                yield _encode_entry(entry, seq=seq)

            last_heartbeat = datetime.now(UTC)

            while not cancel_event.is_set():
                await asyncio.sleep(POLL_INTERVAL_SEC)

                matched, nonlocal_since = _drain_since(
                    journal, pocket_id, nonlocal_since
                )
                for seq, entry in matched:
                    yield _encode_entry(entry, seq=seq)

                if max_idle_polls is not None:
                    if matched:
                        idle_polls = 0
                    else:
                        idle_polls += 1
                        if idle_polls >= max_idle_polls:
                            # Emit a terminal frame so the client can
                            # distinguish "backlog drained" from "connection
                            # dropped". Matches the Fleet router convention
                            # of always framing terminal state explicitly.
                            yield "event: closed\ndata: {\"reason\": \"idle\"}\n\n"
                            return

                # Keepalive comment keeps intermediaries happy when the
                # pocket is idle.
                now = datetime.now(UTC)
                if (now - last_heartbeat).total_seconds() >= HEARTBEAT_SEC:
                    yield ": keepalive\n\n"
                    last_heartbeat = now

        except asyncio.CancelledError:
            # The client disconnected — propagate so FastAPI can clean up
            # without producing a 500.
            raise
        except Exception:  # pragma: no cover — defensive
            logger.exception(
                "pocket journal stream failed for pocket_id=%s", pocket_id
            )
            raise

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Disable nginx buffering so frames flush in real time.
            "X-Accel-Buffering": "no",
        },
    )
