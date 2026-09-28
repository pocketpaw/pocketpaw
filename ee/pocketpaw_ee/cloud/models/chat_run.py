"""Beanie document for one assistant chat turn (``ChatRunDoc``).

Fields a reader should know about:

- ``last_heartbeat_at``: stamped every ``POCKETPAW_CLOUD_RUN_HEARTBEAT_SECONDS``
  by the worker while it drives a ``running`` run. The stale-run sweeper judges
  running runs by it, falling back to ``started_at`` then ``createdAt`` when it
  is ``None``, so older docs need no migration.
- ``usage``: the prompt / completion / cached token counts (plus cost, model,
  backend) the backend reported, captured by ``run_core`` and written via
  ``mark_completed`` / ``mark_terminal``. ``{}`` when the backend reported none.
- ``billed``: flipped True once the metering sweeper
  (``ee.cloud.metering.sweeper``) has charged the run's compute cost to the
  workspace wallet. The ledger's ``run:{run_id}`` idempotency key is the real
  exactly-once guard; this flag is the cheap "already done" filter.
- ``user_text``: the visitor's turn for CONCIERGE runs, which have no Message
  row for the user side. Written only when the site has
  ``concierge_store_transcripts`` on. It is personal data, so it is opt-outable
  per site and length-capped by the writer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from beanie import Document
from pydantic import Field
from pymongo import IndexModel

RunStatus = Literal["queued", "running", "completed", "interrupted", "failed", "cancelled"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ChatRunDoc(Document):
    run_id: str
    workspace: str
    context_type: str  # "dm" | "group" | "pocket" | "session"
    scope_id: str
    session_key: str
    group: str | None = None
    user_id: str
    agent_id: str
    client_message_id: str
    user_message_id: str
    assistant_message_id: str | None = None
    status: RunStatus = "queued"
    partial_text: str = ""
    error: str | None = None
    # Per-run token usage assembled from the backend's ``token_usage`` event
    # (input / output / cached_input token counts + total_cost_usd + model +
    # backend). ``{}`` when the backend reported nothing — keeps legacy /
    # empty-text runs unchanged. This is the durable metering sink that
    # outcome-based pricing reads off.
    usage: dict[str, Any] = Field(default_factory=dict)
    # BC-3 compute-cost metering: True once this run's compute cost has been
    # billed to the workspace wallet. The metering sweeper queries terminal runs
    # where ``billed is False`` and bills each EXACTLY ONCE, then flips this so the
    # run never re-bills. The ledger's ``run:{run_id}`` idempotency key is the real
    # exactly-once guard; this flag is the cheap "skip already-billed" filter that
    # keeps each sweep bounded.
    billed: bool = False
    # The user's OWN message text, stored on the run itself. Empty on every surface
    # that persists the user turn as a real Message document (``user_message_id``
    # points at it there) — this exists for the CONCIERGE surface, whose visitor is
    # anonymous and therefore has no thread and no Message row. Written only when
    # the site's ``concierge_store_transcripts`` toggle is on, and length-capped by
    # the writer. Treat it as PERSONAL DATA: a visitor types free text, so it can
    # carry a name, an email, or an order number.
    user_text: str = ""
    createdAt: datetime = Field(default_factory=_utcnow)
    started_at: datetime | None = None
    # Liveness stamp from the worker driving this run. Refreshed on a timer, not
    # per event, because one tool call can go many minutes without emitting
    # anything. ``None`` until the run is marked running (and on legacy docs).
    last_heartbeat_at: datetime | None = None
    ended_at: datetime | None = None

    class Settings:
        name = "chat_runs"
        # Uniques close the create_run find-then-insert race.
        indexes = [
            IndexModel([("run_id", 1)], unique=True),
            IndexModel([("workspace", 1), ("client_message_id", 1)], unique=True),
            [("workspace", 1), ("context_type", 1), ("scope_id", 1), ("createdAt", -1)],
            # HR-12a. The activity board filters `workspace + createdAt >= since`
            # and sorts `-createdAt`, with NO context_type / scope_id. The
            # compound index above cannot serve that query: its unbounded middle
            # keys mean `createdAt` is neither a usable range bound nor able to
            # supply the sort, so the planner walks every run the workspace has
            # ever had, then filters and sorts in memory. On an endpoint polled
            # every few seconds by every signed-in member, that is a full history
            # scan per tick. This two-key index makes the window a real bound and
            # the sort an index walk.
            IndexModel([("workspace", 1), ("createdAt", -1)]),
            # backend-perf H4. `find_active_run_scopes` — the jail GC's guard,
            # which runs on startup and every five minutes — filters on
            # `status` alone, with no workspace. No index above leads on
            # status, so that query is a COLLSCAN of the whole collection.
            #
            # That would be tolerable against a bounded collection. `chat_runs`
            # has NO TTL and no archival, so it grows forever, and every row
            # carries the run's complete assistant answer in `partial_text`.
            # At 10k runs/day it passes a million documents in three months,
            # and the GC then walks all of them, every five minutes.
            #
            # The index makes it a walk of only the active runs, which is a
            # handful. It does NOT bound the collection — see the note on the
            # retention gap in the audit; a TTL deletes customer data and is
            # not a call to make inside a performance change.
            IndexModel([("status", 1), ("createdAt", -1)]),
        ]
