# ee/pocketpaw_ee/cloud/agent_sessions/runtime_service.py — the SOLE owner of
# the durable ``(workspace, session_id, agent_id) -> cli_session_id`` mapping
# (feat/session-supervisor SS-3).
#
# Module-level ``async def`` API (no class wrapper), mirroring
# ``ee.cloud.sessions.service``. The executor (SS-5) calls:
#   - ``set_cli_session_id`` on turn 1, when the OSS ``claude_sdk`` backend
#     emits its ``session_id`` event, to durably record the native session id;
#     and again on a later resume/backfill (it UPSERTs — one row per key).
#   - ``get_cli_session_id`` on every later turn (including a COLD turn after a
#     backend restart) to recover the native session and rebuild the
#     ``SessionHandle`` so the agent resumes instead of starting fresh.
#
# Tenancy: EVERY read filters on ``workspace == workspace_id`` (the leading
# component of the unique index), so a lookup scoped to tenant B can never
# observe tenant A's mapping. Inputs are validated at entry; a foreign-tenant
# or absent key resolves to ``None`` rather than raising.
#
# This module is the ONLY importer of ``AgentSessionRuntimeDoc`` (the ee
# entity-isolation boundary). The mapping is internal-only (no HTTP surface),
# so there is no DTO/router and no realtime event on write.
#
# no-event: this entity has no HTTP/realtime surface — it is an internal resume
# index written by the executor on the agent turn hot path, never observed by a
# client, so there is nothing to emit. (ee write-event convention.)
#
# Created 2026-06-30 (feat/session-supervisor SS-3): new entity service.
#
# Updated 2026-06-30 (fix/session-supervisor-saas-hardening SH-1b):
# ``set_cli_session_id`` is now a single ATOMIC ``update_one(..., upsert=True)``
# on the unique ``(workspace, session_id, agent_id)`` key instead of
# find-then-insert-or-update. Two concurrent turn-1s for the same key no longer
# race into a DuplicateKeyError — one inserts, the other updates in place.

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.models.agent_session_runtime import AgentSessionRuntimeDoc

logger = logging.getLogger(__name__)


def _require(name: str, value: str) -> str:
    """Validate a required scope-key component at entry; raise on empty."""
    if not value or not isinstance(value, str):
        raise ValidationError(
            "agent_session_runtime.invalid",
            f"{name} must be a non-empty string",
        )
    return value


async def _find_row(
    workspace_id: str, session_id: str, agent_id: str
) -> AgentSessionRuntimeDoc | None:
    """Tenant-filtered lookup of the single row for this logical key.

    The leading ``workspace`` filter is the tenancy boundary — a key from
    another tenant simply does not match.
    """
    return await AgentSessionRuntimeDoc.find_one(
        AgentSessionRuntimeDoc.workspace == workspace_id,
        AgentSessionRuntimeDoc.session_id == session_id,
        AgentSessionRuntimeDoc.agent_id == agent_id,
    )


async def set_cli_session_id(
    workspace_id: str,
    session_id: str,
    agent_id: str,
    cli_session_id: str,
    project_key: str | None = None,
) -> None:
    """UPSERT the native ``cli_session_id`` for ``(ws, session, agent)`` ATOMICALLY.

    A single ``update_one(filter, update, upsert=True)`` on the unique
    ``(workspace, session_id, agent_id)`` key. Mongo's upsert is atomic on that
    key, so two concurrent turn-1s for the same session no longer race into a
    DuplicateKeyError — one wins the insert, the other falls through to an update
    in place. (The old find-then-insert path let both find nothing and both try to
    insert, so the loser hit the unique index unhandled.)

    ``cli_session_id`` is ``$set`` on every call. ``project_key`` is recorded when
    supplied (``$set``) and left unchanged on update when omitted; on a fresh
    insert with no ``project_key`` it is seeded to ``None`` via ``$setOnInsert`` so
    the row shape is stable. ``updatedAt`` is refreshed on every call and
    ``createdAt`` stamped only on insert (the raw update bypasses Beanie's
    timestamp hooks).
    """
    workspace_id = _require("workspace_id", workspace_id)
    session_id = _require("session_id", session_id)
    agent_id = _require("agent_id", agent_id)
    cli_session_id = _require("cli_session_id", cli_session_id)

    now = datetime.now(UTC)
    update: dict[str, Any] = {
        "$set": {"cli_session_id": cli_session_id, "updatedAt": now},
        "$setOnInsert": {"createdAt": now},
    }
    if project_key is not None:
        # Provided → set on both insert and update (only in ``$set`` so it never
        # collides with ``$setOnInsert``).
        update["$set"]["project_key"] = project_key
    else:
        # Omitted → seed ``None`` on a fresh insert only; leave a prior value
        # untouched on update.
        update["$setOnInsert"]["project_key"] = None

    await AgentSessionRuntimeDoc.get_pymongo_collection().update_one(
        {
            "workspace": workspace_id,
            "session_id": session_id,
            "agent_id": agent_id,
        },
        update,
        upsert=True,
    )


async def get_cli_session_id(
    workspace_id: str,
    session_id: str,
    agent_id: str,
) -> str | None:
    """Return the native ``cli_session_id`` for ``(ws, session, agent)``.

    Tenant-filtered: a foreign-tenant or never-written key resolves to ``None``
    (the row either doesn't match the ``workspace`` filter or doesn't exist).
    Also ``None`` when a row exists but its ``cli_session_id`` was never set.
    """
    workspace_id = _require("workspace_id", workspace_id)
    session_id = _require("session_id", session_id)
    agent_id = _require("agent_id", agent_id)

    doc = await _find_row(workspace_id, session_id, agent_id)
    if doc is None:
        return None
    return doc.cli_session_id


__all__ = ["set_cli_session_id", "get_cli_session_id"]
