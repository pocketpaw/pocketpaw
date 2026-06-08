"""Sessions domain — Pydantic request/response schemas + domain → wire mapper.

Recent change: added ``foresight`` to the ``surface`` literal so Foresight
chats are isolated server-side instead of piggybacking on ``chat`` + a
client-side manifest. The ``surface`` field on ``CreateSessionRequest`` and
``SessionResponse`` (and the wire dict) lets the frontend stamp / read the
originating chat surface (``chat`` / ``files`` / ``pocket_creation`` /
``foresight``). Field is optional everywhere so legacy callers and pre-fix
rows (``surface=None``) still validate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from pocketpaw_ee.cloud._core.time import iso_utc
from pocketpaw_ee.cloud.sessions.domain import Session

# Surface tag values — kept in lockstep with ``models.session.SurfaceType``.
Surface = Literal["chat", "files", "pocket_creation", "foresight"]


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    title: str = "New Chat"
    pocket_id: str | None = None  # Link to pocket on creation
    group_id: str | None = None
    agent_id: str | None = None
    session_id: str | None = None  # Link to existing runtime session (e.g. "websocket_abc123")
    # Originating UI surface — frontend stamps this so /chat sidebar can
    # filter out pocket-creation / files-panel sessions. Optional; legacy
    # callers can omit it.
    surface: Surface | None = None


class UpdateSessionRequest(BaseModel):
    title: str | None = None
    pocket_id: str | None = None  # Can link/unlink pocket


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class SessionResponse(BaseModel):
    id: str
    session_id: str  # The unique sessionId
    workspace: str
    owner: str
    title: str
    pocket: str | None
    group: str | None
    agent: str | None
    message_count: int
    last_activity: datetime
    created_at: datetime
    deleted_at: datetime | None = None
    surface: Surface | None = None


class SessionPage(BaseModel):
    """One keyset-paginated page of sessions for a single mode/surface.

    ``sessions`` are legacy wire dicts (``session_to_wire_dict`` shape) for
    drop-in compatibility with the existing list endpoints. ``nextCursor`` is
    the opaque cursor for the following page, or ``None`` on the last page.
    Backs ``GET /sessions/{chat,files,foresight,pocket-creation}``.
    """

    sessions: list[dict[str, Any]]
    nextCursor: str | None = None


def session_to_wire_dict(s: Session) -> dict[str, Any]:
    """Map a domain ``Session`` to the legacy wire-format dict.

    Byte-equivalent to the existing ``_session_response`` in
    ``service.py`` so callers migrating to the repository abstraction
    don't shift the API contract.
    """
    return {
        "_id": s.id,
        "sessionId": s.sessionId,
        "workspace": s.workspace,
        "owner": s.owner,
        "title": s.title,
        "pocket": s.pocket,
        "group": s.group,
        "agent": s.agent,
        "surface": s.surface,
        "messageCount": s.message_count,
        "lastActivity": iso_utc(s.last_activity),
        "createdAt": iso_utc(s.created_at),
        "deletedAt": iso_utc(s.deleted_at),
    }


__all__ = [
    "CreateSessionRequest",
    "SessionPage",
    "SessionResponse",
    "UpdateSessionRequest",
    "session_to_wire_dict",
]
