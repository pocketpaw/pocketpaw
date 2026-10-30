# ee/pocketpaw_ee/cloud/models/agent_session_runtime.py — the durable,
# tenant-scoped mapping from a chat-session scope to its native CLI session id
# (feat/session-supervisor SS-3).
#
# One ``AgentSessionRuntimeDoc`` row records, for ONE
# (workspace, session_id, agent_id) tuple, the native ``cli_session_id`` the
# Claude Agent SDK minted for that conversation (plus the SDK ``project_key``
# scope it lives under). The executor persists it on turn 1 (when the
# ``claude_sdk`` backend emits the ``session_id`` event) and looks it up on
# every later turn — even a COLD turn after a backend restart — so the agent
# can resume the same native session instead of starting fresh. Without this
# durable mapping the captured native id evaporates between turns and the user
# gets "message with no agent reply".
#
# The UNIQUE compound index on ``(workspace, session_id, agent_id)`` keeps it
# one row per logical key (so the service can upsert: insert on turn 1, update
# on a later resume/backfill) and the leading ``workspace`` component is the
# tenancy boundary — every read in the service filters on ``workspace`` so a
# lookup scoped to tenant B can never observe tenant A's mapping even though
# both live in the same collection.
#
# Created 2026-06-30 (feat/session-supervisor SS-3): new entity. Registered in
# ``cloud.models.__init__`` (``get_all_documents()`` + ``__all__``) so
# ``init_beanie`` wires the ``agent_session_runtimes`` collection. Only
# ``ee.cloud.agent_sessions.runtime_service`` imports this doc class
# (entity-isolation boundary, mirroring the SS-2 transcript / sessions
# entities).

from __future__ import annotations

from beanie import Indexed
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class AgentSessionRuntimeDoc(TimestampedDocument):
    """Durable ``(workspace, session_id, agent_id) -> cli_session_id`` mapping.

    ``session_id`` is the chat-session scope key (the conversation the user is
    in), ``agent_id`` the agent answering it, and ``cli_session_id`` the native
    Claude Agent SDK session id minted on turn 1 — ``None`` until the first
    ``session_id`` event lands. ``project_key`` is the SDK scope the native
    session lives under, carried so a resume can reconstruct the full
    ``SessionHandle``. ``updatedAt`` (from :class:`TimestampedDocument`) records
    the last backfill.
    """

    # Tenancy boundary. Indexed (non-unique) — many runtime rows per workspace.
    workspace: Indexed(str)  # type: ignore[valid-type]
    # The chat-session scope key (the conversation the turn belongs to).
    session_id: str
    # The agent answering this session.
    agent_id: str
    # The native Claude Agent SDK session id; None until turn 1's event lands.
    cli_session_id: str | None = None
    # The SDK ``project_key`` scope the native session lives under (default cwd).
    project_key: str | None = None

    class Settings:
        name = "agent_session_runtimes"
        indexes = [
            # One row per logical key. The service upserts on this key; the
            # leading ``workspace`` makes a guessed/leaked (session_id,
            # agent_id) from another tenant impossible to collide with.
            IndexModel(
                [("workspace", 1), ("session_id", 1), ("agent_id", 1)],
                unique=True,
                name="uq_workspace_session_agent",
            ),
        ]
