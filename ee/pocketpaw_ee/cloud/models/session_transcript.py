# ee/pocketpaw_ee/cloud/models/session_transcript.py — the durable, tenant-scoped
# transcript rows that back the Mongo ``SessionStore`` (feat/session-supervisor
# SS-2).
#
# One ``SessionTranscriptDoc`` row holds the full JSONL transcript for ONE
# (workspace, project_key, session_id, subpath) tuple. The Claude Agent SDK's
# ``SessionStore`` protocol mirrors transcript lines here via ``append`` and
# reconstructs a conversation on resume via ``load`` — so an agent session
# survives a backend restart by reading from Mongo instead of the subprocess's
# local disk.
#
# ``subpath`` is ``""`` for the main transcript and a non-empty path (e.g.
# ``"subagents/agent-{id}"``) for a subagent transcript. The UNIQUE compound
# index on ``(workspace, project_key, session_id, subpath)`` keeps it one row
# per logical key (so ``append`` can find-and-extend) and the leading
# ``workspace`` component is the tenancy boundary — every read in the adapter
# filters on ``workspace`` so a store bound to tenant B can never observe tenant
# A's rows.
#
# Created 2026-06-30 (feat/session-supervisor SS-2): new entity. Registered in
# ``cloud.models.__init__`` (``get_all_documents()`` + ``__all__``) so
# ``init_beanie`` wires the ``session_transcripts`` collection. Only
# ``ee.cloud.agent_sessions.store`` imports this doc class (entity-isolation
# boundary, mirroring the credit / sessions entities).

from __future__ import annotations

from typing import Any

from beanie import Indexed
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class SessionTranscriptDoc(TimestampedDocument):
    """One agent-session transcript (or subagent transcript) for a tenant.

    Mirrors the SDK ``SessionStore`` key shape. ``entries`` is the ordered list
    of opaque JSONL transcript lines (pass-through blobs — the adapter never
    interprets them). ``updatedAt`` (from :class:`TimestampedDocument`) is the
    ``mtime`` source the adapter surfaces in ``list_sessions``.
    """

    # Tenancy boundary. Indexed (non-unique) — many sessions per workspace.
    workspace: Indexed(str)  # type: ignore[valid-type]
    # The SDK ``project_key`` — caller-defined scope (default: sanitized cwd).
    project_key: str
    # The SDK session id.
    session_id: str
    # "" for the main transcript; a non-empty subpath for a subagent transcript.
    subpath: str = ""
    # Ordered opaque transcript lines. Each is one JSONL entry as a plain dict.
    entries: list[dict[str, Any]] = []  # noqa: RUF012 — Beanie field default, not a shared mutable

    class Settings:
        name = "session_transcripts"
        indexes = [
            # One row per logical key. ``append`` upserts on this key; the
            # leading ``workspace`` makes a guessed/leaked (project_key,
            # session_id) from another tenant impossible to collide with.
            IndexModel(
                [("workspace", 1), ("project_key", 1), ("session_id", 1), ("subpath", 1)],
                unique=True,
                name="uq_workspace_project_session_subpath",
            ),
        ]
