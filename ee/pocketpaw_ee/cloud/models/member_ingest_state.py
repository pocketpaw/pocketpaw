# MemberIngestState Beanie document — per-member Gmail/Calendar ingest status.
# Created: 2026-06-08 — VIP Onboarding Phase B (per-user ingest worker).
# One row per (workspace, member_id) tracking the private-KB ingest worker's
# state for that member: whether the initial bounded backfill has run
# (``backfill_done`` drives backfill-vs-incremental), the per-source
# high-water cursors used to bound the incremental window, and the last
# run's status/error for operator visibility. Distinct from
# WorkspaceConnector.last_sync_* (which is one row per (workspace, connector
# name) and is the connector's own health, not the per-member ingest
# bookkeeping the worker needs). The member id stored here is the clean
# opaque cloud user_id — the same value used verbatim as the ``user:{id}``
# kb-go scope (see chat.agent_service._member_private_user_scope).

from __future__ import annotations

from datetime import datetime

from beanie import Indexed

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class MemberIngestState(TimestampedDocument):
    """Per-member ingest bookkeeping for the Phase B private-KB worker.

    Tenancy: ``workspace`` is required and indexed; every read filters on it
    (cloud rule §7). ``member_id`` is the opaque cloud user id — paired with
    ``workspace`` it is unique per member, enforced at the service layer
    (upsert-on-write, no Mongo unique index so re-runs are forgiving).

    The ``*_cursor`` fields hold the RFC3339 timestamp of the newest item
    ingested from that source on the last successful run. The incremental
    pass uses ``max(cursor, now - INCREMENTAL_WINDOW)`` as its lower bound so
    a long gap still re-reads a bounded window rather than the whole history.
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    member_id: str
    status: str = "never"  # "never" | "running" | "ok" | "error"
    backfill_done: bool = False
    last_sync_at: datetime | None = None
    last_error: str = ""
    # High-water marks (RFC3339 strings) for the incremental window. Empty
    # until the first successful read of that source.
    gmail_cursor: str = ""
    calendar_cursor: str = ""
    # Running totals — handy for an operator/status endpoint without a
    # separate metrics store. Not load-bearing for correctness.
    documents_ingested: int = 0

    class Settings(TimestampedDocument.Settings):
        name = "member_ingest_state"
