# ee/pocketpaw_ee/cloud/other_hand/domain.py — the Otherhand page value object.
#
# Created 2026-09-15 (feat/otherhand-page-store). Brings ``other_hand`` onto the
# 4-file entity shape (CLAUDE.md's touch-time migration rule) as the page store
# lands: the entity had only ``service.py`` + ``router.py`` before.
#
# Tenancy is enforced AT CONSTRUCTION — ``workspace_id`` and ``page_id`` are
# required with no defaults, so a page value object cannot exist without knowing
# which tenant it belongs to. That is cloud rule 3, and it is what stops a page
# read from being written against "whatever workspace was last in scope".
#
# ``strokes`` and ``book`` stay OPAQUE here for the same reason the Beanie
# document keeps them opaque: the frontend's ``Stroke`` shape grows, and the
# server must round-trip it verbatim rather than re-model it. Frozen dataclasses
# do not freeze the nested JSON; the service never mutates it in place.

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Page:
    """One Otherhand notebook page, as every reader outside the service sees it."""

    workspace_id: str
    page_id: str
    user_id: str
    #: The compare-and-set counter the next write must name. Never 0 for a page
    #: that exists — 0 is the wire value for "no server copy".
    rev: int
    updated_at: datetime
    created_at: datetime
    session_id: str | None = None
    strokes: list[dict[str, Any]] = field(default_factory=list)
    book: dict[str, Any] | None = None


__all__ = ["Page"]
