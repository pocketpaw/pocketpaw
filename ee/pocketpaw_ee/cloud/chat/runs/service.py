"""Chat-run service — the only module that touches ``ChatRunDoc``.

Internal seam (not an HTTP-exposed CRUD entity): the public functions take a
``RunSpec`` value object rather than the standard ``(workspace_id, user_id, body)``.

Changes:
- 2026-06-10 (sov/w3a-igw — per-run token metering) — ``mark_completed`` and
  ``mark_terminal`` now accept an optional ``usage: dict[str, Any] | None`` and
  persist it onto ``ChatRunDoc.usage`` when provided. ``run_core`` passes the
  token-usage dict it assembles from the backend's ``token_usage`` event, so
  each finished run carries its real prompt / completion / cached token counts.
  ``None`` leaves the stored usage untouched (legacy callers / no-usage runs).
- 2026-06-24 (B3 review fix — metering boundary) — added ``mark_billed(run_id)``.
  The metering service (BC-3) previously wrote ``run_doc.billed=True`` +
  ``run_doc.save()`` directly, a FOREIGN write across the chat.runs entity
  boundary (EE Rule 2 — only this module touches ``ChatRunDoc``). ``bill_run`` now
  calls ``mark_billed`` instead. Reading ``run_doc.usage`` in metering stays fine;
  only the WRITE moves here, the owner of the document.
- 2026-07-28 (HR-12a, feat/cockpit-agent-activity) — the ``queued``/``running``
  pair that three functions each spelled inline is now the named
  ``ACTIVE_RUN_STATUSES`` constant, and two workspace-scoped reads were added
  (``find_active_runs_for_workspace`` / ``find_recent_runs_for_workspace``) for
  the agent-activity board. They return ``RunActivityRow`` value objects, not
  ``ChatRunDoc``, so the caller never sees a Beanie document.
- 2026-07-26 (concierge transcripts) — ``create_run`` copies
  ``RunSpec.persist_user_text`` onto ``ChatRunDoc.user_text``. Only the concierge
  surface ever sets it (its anonymous visitor has no Message row to point
  ``user_message_id`` at), so every other caller writes "" and is unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError

from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.chat.runs.domain import RunActivityRow, RunSpec
from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

logger = logging.getLogger(__name__)

# A run is ACTIVE while it has been accepted but has not reached a terminal
# state. One definition, one place: the scope lookup, the jail guard, and the
# agent-activity board all read the same pair, so "is this agent working right
# now" can never disagree with "is this scope busy".
ACTIVE_RUN_STATUSES = ("queued", "running")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class _RunActivityProjection(BaseModel):
    """Wire-minimal projection for the two activity reads (HR-12a).

    Without this, both reads pull WHOLE run documents just to keep six fields —
    and ``ChatRunDoc.partial_text`` holds the entire assistant answer (see
    ``run_core`` writing ``partial_text=full_text``), plus ``usage``. On a
    board polled every few seconds by every signed-in member, the recent read
    would drag up to ``MAX_RECENT_RUNS_SCANNED`` full answers across the wire
    and discard all of them; it also inflates the top-K sort buffer, which on a
    workspace with long turns can reach Mongo's 32MB in-memory sort limit and
    fail the endpoint outright. Field names mirror the document exactly so
    ``_to_activity_row`` reads either shape.
    """

    run_id: str
    agent_id: str
    status: str
    createdAt: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None


def _to_activity_row(doc: ChatRunDoc | _RunActivityProjection) -> RunActivityRow:
    """Project a run document onto the Beanie-free activity value object."""
    return RunActivityRow(
        run_id=doc.run_id,
        agent_id=doc.agent_id,
        status=doc.status,
        created_at=doc.createdAt,
        started_at=doc.started_at,
        ended_at=doc.ended_at,
    )


async def create_run(spec: RunSpec) -> ChatRunDoc:
    """Idempotent on ``(workspace, client_message_id)`` — unique index is the
    source of truth; find-then-insert is the fast path."""
    existing = await ChatRunDoc.find_one(
        ChatRunDoc.workspace == spec.workspace_id,
        ChatRunDoc.client_message_id == spec.client_message_id,
    )
    if existing is not None:
        return existing
    doc = ChatRunDoc(
        run_id=spec.run_id,
        workspace=spec.workspace_id,
        context_type=spec.context_type,
        scope_id=spec.scope_id,
        session_key=spec.session_key,
        group=spec.group,
        user_id=spec.user_id,
        agent_id=spec.agent_id,
        client_message_id=spec.client_message_id,
        user_message_id=spec.user_message_id,
        user_text=spec.persist_user_text,
    )
    try:
        await doc.insert()
    except DuplicateKeyError:
        winner = await ChatRunDoc.find_one(
            ChatRunDoc.workspace == spec.workspace_id,
            ChatRunDoc.client_message_id == spec.client_message_id,
        )
        if winner is None:
            raise
        return winner
    return doc


async def get_run(run_id: str) -> ChatRunDoc:
    doc = await ChatRunDoc.find_one(ChatRunDoc.run_id == run_id)
    if doc is None:
        raise NotFound("chat_run", run_id)
    return doc


def _trigger_spend_ingest(doc: ChatRunDoc) -> None:
    """Ask for this workspace's proxy spend to be billed shortly (live mode only).

    Called from every terminal transition. In ``live`` the LiteLLM sweep is the
    only meter and it runs every five minutes, so without this a customer's
    balance lags their usage by up to that long — and the run-start balance gate
    can admit a run the previous one had already spent the credits for.

    Fire and forget, and deliberately last: it must never delay or fail a
    transition that has already happened. The trigger schedules the SAME ingest
    the sweep runs, keyed on the same ledger id, so the two racing is harmless
    and the sweep remains the backstop for anything this misses.
    """
    try:
        from pocketpaw_ee.cloud.llm_provisioning import run_end_trigger

        run_end_trigger.schedule_spend_ingest(doc.workspace)
    except Exception:  # noqa: BLE001 — a billing hint must not break the lifecycle
        logger.debug("run spend-ingest trigger failed for %s", doc.run_id, exc_info=True)


async def mark_running(run_id: str) -> None:
    doc = await get_run(run_id)
    doc.status = "running"
    doc.started_at = _utcnow()
    await doc.save()


async def mark_completed(
    run_id: str,
    *,
    assistant_message_id: str | None,
    partial_text: str,
    usage: dict[str, Any] | None = None,
) -> None:
    doc = await get_run(run_id)
    doc.status = "completed"
    doc.assistant_message_id = assistant_message_id
    doc.partial_text = partial_text
    # Per-run token metering: persist the usage the backend reported (None =
    # nothing to record, leave the stored value as-is).
    if usage:
        doc.usage = usage
    doc.ended_at = _utcnow()
    await doc.save()
    _trigger_spend_ingest(doc)


async def mark_terminal(
    run_id: str,
    *,
    status: str,
    partial_text: str = "",
    error: str | None = None,
    assistant_message_id: str | None = None,
    usage: dict[str, Any] | None = None,
) -> None:
    """Set a non-completed terminal status (``interrupted`` | ``failed`` | ``cancelled``)."""
    doc = await get_run(run_id)
    doc.status = status  # type: ignore[assignment]
    doc.partial_text = partial_text or doc.partial_text
    doc.error = error
    doc.assistant_message_id = assistant_message_id or doc.assistant_message_id
    # A cancelled / interrupted run may still have consumed tokens before it
    # stopped — record whatever the backend reported.
    if usage:
        doc.usage = usage
    doc.ended_at = _utcnow()
    await doc.save()
    # A cancelled or failed run consumed tokens before it stopped, and the proxy
    # priced them. Billing only completed runs would serve every interrupted one
    # free — which is why the metering sweeper bills all four terminal states too.
    _trigger_spend_ingest(doc)


async def mark_billed(run_id: str) -> None:
    """Flag a run as billed (BC-3). The owner of ``ChatRunDoc`` makes this write
    so the metering service never writes a foreign entity's document (EE Rule 2).
    Idempotent — re-flagging an already-billed run is harmless; the ledger key is
    the real double-debit guard, this flag just keeps the sweeper bounded."""
    doc = await get_run(run_id)
    doc.billed = True
    await doc.save()


async def find_active_run_for_scope(
    *,
    workspace_id: str,
    context_type: str | Iterable[str],
    scope_id: str,
) -> ChatRunDoc | None:
    """Newest non-terminal run for a scope. ``context_type`` may be a single
    string or an iterable (the group history path queries both ``dm`` and
    ``group`` at once)."""
    if isinstance(context_type, str):
        ctype_filter: dict = {"context_type": context_type}
    else:
        types = list(context_type)
        ctype_filter = {"context_type": {"$in": types}} if types else {"context_type": None}
    return (
        await ChatRunDoc.find(
            ChatRunDoc.workspace == workspace_id,
            ChatRunDoc.scope_id == scope_id,
            ctype_filter,
            {"status": {"$in": list(ACTIVE_RUN_STATUSES)}},
        )
        .sort(-ChatRunDoc.createdAt)  # type: ignore[operator]
        .first_or_none()
    )


async def find_stale_running(older_than: datetime) -> list[ChatRunDoc]:
    """Runs left queued/running before a cutoff."""
    return await ChatRunDoc.find(
        {"status": {"$in": list(ACTIVE_RUN_STATUSES)}},
        ChatRunDoc.createdAt < older_than,
    ).to_list()


async def find_active_run_scopes() -> set[tuple[str, str, str]]:
    """Every ``(workspace, context_type, scope_id)`` with a non-terminal
    (``queued`` / ``running``) run.

    The active-jail guard for the ART-3 jail GC: a per-session agent jail dir is
    named after its run's scope (``session`` runs → ``<scope_id>``; ``dm`` /
    ``group`` / ``pocket`` runs share the workspace ``_shared`` dir), so a jail
    whose scope is in this set is in use and must never be evicted. Uses the
    same ``active = queued | running`` definition as ``find_active_run_for_scope``
    — an ``interrupted`` run that the user retries spawns a NEW queued/running
    run for the same scope, which re-protects the jail, so a retry can't race a
    GC pass into deleting a jail it's about to reuse.
    """
    docs = await ChatRunDoc.find(
        {"status": {"$in": list(ACTIVE_RUN_STATUSES)}},
    ).to_list()
    return {(d.workspace, d.context_type, d.scope_id) for d in docs}


# ---------------------------------------------------------------------------
# Workspace-scoped activity reads (HR-12a)
#
# Both are TENANT-SCOPED by construction: ``workspace_id`` is a required
# keyword and lands in the filter, so there is no call shape that returns
# another workspace's runs. Both are also WINDOWED and CAPPED — an activity
# board must never be able to ask Mongo for a workspace's whole run history.
# The ``(workspace, context_type, scope_id, createdAt)`` index on ChatRunDoc
# leads on ``workspace``, so the workspace + createdAt filter is served by its
# prefix.
# ---------------------------------------------------------------------------


async def find_active_runs_for_workspace(
    *,
    workspace_id: str,
    since: datetime,
    limit: int,
) -> list[RunActivityRow]:
    """A workspace's currently-active (``queued``/``running``) runs, newest first.

    Windowed like its sibling below on purpose: a run still marked ``running``
    from days ago is a leaked run (that is what ``find_stale_running`` reaps),
    not an agent at work, and showing it would pin an agent to ACTIVE forever.
    """
    docs = (
        await ChatRunDoc.find(
            ChatRunDoc.workspace == workspace_id,
            ChatRunDoc.createdAt >= since,
            {"status": {"$in": list(ACTIVE_RUN_STATUSES)}},
        )
        .project(_RunActivityProjection)
        .sort(-ChatRunDoc.createdAt)  # type: ignore[operator]
        .limit(limit)
        .to_list()
    )
    return [_to_activity_row(d) for d in docs]


async def find_recent_runs_for_workspace(
    *,
    workspace_id: str,
    since: datetime,
    limit: int,
) -> list[RunActivityRow]:
    """A workspace's runs of any status since ``since``, newest first.

    Truncation is by newest-first, so a busy workspace loses its least-recently
    active agents from the tail — never its live ones, which the active read
    above surfaces independently of this cap.
    """
    docs = (
        await ChatRunDoc.find(
            ChatRunDoc.workspace == workspace_id,
            ChatRunDoc.createdAt >= since,
        )
        .project(_RunActivityProjection)
        .sort(-ChatRunDoc.createdAt)  # type: ignore[operator]
        .limit(limit)
        .to_list()
    )
    return [_to_activity_row(d) for d in docs]
