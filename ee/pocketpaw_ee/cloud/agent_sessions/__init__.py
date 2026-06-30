# ee/pocketpaw_ee/cloud/agent_sessions/__init__.py — the agent-session entity.
#
# Holds the Mongo-backed ``SessionStore`` (feat/session-supervisor SS-2) that
# durably mirrors Claude Agent SDK session transcripts per tenant, so an agent
# conversation survives a backend restart by resuming from Mongo instead of the
# subprocess's local disk.
#
# Created 2026-06-30 (feat/session-supervisor SS-2).

from __future__ import annotations

from pocketpaw_ee.cloud.agent_sessions.store import MongoSessionStore

__all__ = ["MongoSessionStore"]
