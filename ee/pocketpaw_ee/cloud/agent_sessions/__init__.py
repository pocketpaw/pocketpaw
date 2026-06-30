# ee/pocketpaw_ee/cloud/agent_sessions/__init__.py — the agent-session entity.
#
# Holds the Mongo-backed ``SessionStore`` (feat/session-supervisor SS-2) that
# durably mirrors Claude Agent SDK session transcripts per tenant, so an agent
# conversation survives a backend restart by resuming from Mongo instead of the
# subprocess's local disk, AND the durable
# ``(workspace, session_id, agent_id) -> cli_session_id`` resume mapping
# (SS-3, ``runtime_service``) so any turn — even a cold one after a restart —
# can find the native session to resume.
#
# Created 2026-06-30 (feat/session-supervisor SS-2).
# Updated 2026-06-30 (feat/session-supervisor SS-3): re-export the
# ``runtime_service`` resume-mapping functions.

from __future__ import annotations

from pocketpaw_ee.cloud.agent_sessions.runtime_service import (
    get_cli_session_id,
    set_cli_session_id,
)
from pocketpaw_ee.cloud.agent_sessions.store import MongoSessionStore

__all__ = ["MongoSessionStore", "get_cli_session_id", "set_cli_session_id"]
