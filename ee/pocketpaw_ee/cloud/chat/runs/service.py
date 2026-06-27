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
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from pymongo.errors import DuplicateKeyError

from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc


def _utcnow() -> datetime:
    return datetime.now(UTC)


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
            {"status": {"$in": ["queued", "running"]}},
        )
        .sort(-ChatRunDoc.createdAt)  # type: ignore[operator]
        .first_or_none()
    )


async def find_stale_running(older_than: datetime) -> list[ChatRunDoc]:
    """Runs left queued/running before a cutoff."""
    return await ChatRunDoc.find(
        {"status": {"$in": ["queued", "running"]}},
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
        {"status": {"$in": ["queued", "running"]}},
    ).to_list()
    return {(d.workspace, d.context_type, d.scope_id) for d in docs}
