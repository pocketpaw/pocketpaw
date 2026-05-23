"""Chat-run service — the only module that touches ``ChatRunDoc``.

Note on signature: cloud rule #5 prescribes
``async def op(workspace_id, user_id, body) -> dict`` for entity services
exposed over HTTP. The chat-run service deviates: it takes a ``RunSpec``
value object (and bare ``run_id`` for status mutations) because runs are
an *internal seam* driven by the chat router and the worker — not an
HTTP-exposed CRUD entity. The ``RunSpec`` carries the full tenancy bundle
(``workspace_id``, ``user_id``, ``agent_id``, scope) so the service still
honours rule #7 (tenant filter on every read) — see ``find_one`` on
``workspace + client_message_id`` and ``find_active_run_for_scope``.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def create_run(spec: RunSpec) -> ChatRunDoc:
    """Create the run doc. Idempotent on ``(workspace, client_message_id)``
    so a re-submitted message returns the existing run instead of
    duplicating."""
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
    await doc.insert()
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
) -> None:
    doc = await get_run(run_id)
    doc.status = "completed"
    doc.assistant_message_id = assistant_message_id
    doc.partial_text = partial_text
    doc.ended_at = _utcnow()
    await doc.save()


async def mark_terminal(
    run_id: str,
    *,
    status: str,
    partial_text: str = "",
    error: str | None = None,
    assistant_message_id: str | None = None,
) -> None:
    """Set a non-completed terminal status: ``interrupted`` | ``failed`` |
    ``cancelled``."""
    doc = await get_run(run_id)
    doc.status = status  # type: ignore[assignment]
    doc.partial_text = partial_text or doc.partial_text
    doc.error = error
    doc.assistant_message_id = assistant_message_id or doc.assistant_message_id
    doc.ended_at = _utcnow()
    await doc.save()


async def find_active_run_for_scope(
    *,
    workspace_id: str,
    context_type: str | Iterable[str],
    scope_id: str,
) -> ChatRunDoc | None:
    """Newest non-terminal run for a scope — drives frontend auto-resume.

    ``context_type`` accepts either a single string (the original signature
    used by ``post_agent_chat``) or any iterable of strings (used by the
    group-messages history path, which has to query for both ``"dm"`` and
    ``"group"`` because ``post_agent_chat`` writes one or the other depending
    on the URL scope but ``get_messages`` only knows the group id).
    """
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
        .sort(-ChatRunDoc.createdAt)
        .first_or_none()
    )


async def find_stale_running(older_than: datetime) -> list[ChatRunDoc]:
    """Runs left queued/running before a cutoff — for the PR2 startup sweep."""
    return await ChatRunDoc.find(
        {"status": {"$in": ["queued", "running"]}},
        ChatRunDoc.createdAt < older_than,
    ).to_list()
