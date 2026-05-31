"""Domain value objects for the sessions module.

Pure-Python frozen dataclass mirroring the persistence ``Session``
document. The repository converts between this and the Beanie doc.

Recent change: added the optional ``surface`` field so callers can
distinguish chat / files / pocket-creation origin without re-fetching the
Beanie doc (see "session bleed" fix).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Session:
    """A chat session — pocket / group / session-scope.

    The ``context_type`` discriminates how the session is bound:
    - ``pocket``: anchored to a Pocket document; ``pocket`` set
    - ``group``: a group chat session; ``group`` set
    - ``session``: free-floating single-user agent session; no anchor

    Field names follow the persistence layer (``sessionId`` camelCase
    because the unique-indexed Mongo column uses that alias).

    ``surface`` tags the originating UI surface (``chat`` / ``files`` /
    ``pocket_creation``). Optional — pre-fix rows have ``surface=None``.
    """

    id: str  # Mongo _id as string
    sessionId: str  # noqa: N815 - camelCase wire/persistence key
    context_type: str  # pocket | group | session
    workspace: str
    owner: str  # user_id
    title: str
    pocket: str | None
    group: str | None
    agent: str | None  # agent_id when applicable
    message_count: int
    last_activity: datetime
    created_at: datetime
    deleted_at: datetime | None = None
    surface: str | None = None  # chat | files | pocket_creation


__all__ = ["Session"]
