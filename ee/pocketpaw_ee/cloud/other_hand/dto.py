# ee/pocketpaw_ee/cloud/other_hand/dto.py — the Otherhand wire contract.
#
# Created 2026-09-15 (feat/otherhand-page-store). Two jobs:
#
#   1. The NEW page-store request/response models (``UpsertPageRequest``,
#      ``PageResponse``, ``PageSummaryResponse``, ``PageListResponse``).
#   2. The touch-time migration CLAUDE.md asks for: ``SnapshotRequest`` and
#      ``IllustrateRequest`` moved here out of ``router.py``, where they had been
#      declared inline since 2026-08-25. The router re-exports them so nothing
#      that imported them from there breaks.
#
# Request and response are DISTINCT classes throughout (cloud rule 4). The
# temptation here is real — a page round-trips almost unchanged — but the shapes
# genuinely differ: the request names the ``rev`` it is based on and never sets
# one, the response reports the ``rev`` the caller must send next, plus
# ``updated_at`` and ``created_at`` the client cannot author.
#
# ``strokes`` is ``list[dict[str, Any]]`` ON PURPOSE. Typing it as a real
# ``Stroke`` model would reject the next field the pen grows — ``text``,
# ``img``, ``icon`` and ``kind`` already exist and ``color`` landed the same day
# as this file — and a page that round-trips lossily is worse than one that
# round-trips a field the server does not understand. Same for ``book``: it is a
# reference to a ``/files`` upload, stored verbatim and NOT validated against the
# files collection in this PR.
#
# The envelope is snake_case (``page_id``, ``free_y``), matching the existing
# ``png_base64`` / ``free_y`` endpoints. Keys INSIDE ``strokes`` and ``book`` are
# the frontend's own camelCase and are never touched.

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# Longest a page id may be. Matches the snapshot endpoint's ``page_id`` bound,
# because it is the same id: a page and its snapshot must be addressable by one
# value or they drift into two different notions of "this page".
PAGE_ID_MAX = 128


class SnapshotRequest(BaseModel):
    """Body for ``POST /other-hand/pages/{page_id}/snapshot``.

    * ``png_base64`` — the full page rendered to a PNG, base64-encoded. A
      ``data:image/png;base64,`` prefix is tolerated. Validated (size, base64,
      PNG magic) by the service before anything touches the disk.
    * ``free_y`` — the y coordinate, in the page's 1240x1754 logical space, below
      which the page is empty. Echoed back; the backend stores nothing. Bounded
      to the page so a nonsense value is rejected at the wire rather than
      reaching the agent as a coordinate it would dutifully draw at.
    """

    png_base64: str = Field(min_length=1)
    #: Upper bound lifted 2026-08-26: the paper GROWS downward (whole
    #: half-sheets as ink approaches the bottom), so free_y can exceed one
    #: A4 sheet. 30 sheets is far past any real page and still rejects a
    #: nonsense coordinate at the wire.
    free_y: int = Field(ge=0, le=52620)
    #: Which image this is. ``page`` (the default, and the only v1 value) is
    #: the notebook the agent draws on. ``book`` is the read-only source page
    #: shown beside it in book mode — the agent reads it and never draws on it.
    #: Defaulted so an older client that knows nothing about book mode keeps
    #: working unchanged.
    kind: Literal["page", "book", "mark"] = "page"


class IllustrateRequest(BaseModel):
    """Body for ``POST /other-hand/illustrate``.

    The endpoint IS the opt-in. It exists only because a person pressed a
    button, so reaching it is the authorisation — there is no path by which an
    ordinary turn arrives here. That matters because each call costs real money
    (a Recraft v4 pro generation) and, unlike LLM tokens, a user's own BYOK key
    does NOT cover it.

    ``x/y/w/h`` is where the drawing lands, in the page's 1240-wide logical
    space. The caller picks it because only the client knows where the page is
    empty; the box is bounded here so a nonsense rectangle is refused at the
    wire rather than becoming coordinates nobody can see.
    """

    prompt: str = Field(min_length=2, max_length=500)
    x: float = Field(ge=0, le=1240)
    #: The paper grows downward, so y follows the same 30-sheet bound the
    #: snapshot's free_y uses.
    y: float = Field(ge=0, le=52620)
    w: float = Field(gt=0, le=1240)
    h: float = Field(gt=0, le=1754)


class UpsertPageRequest(BaseModel):
    """Body for ``PUT /other-hand/pages/{page_id}``.

    ``base_rev`` is the compare-and-set token and the whole multi-tab story. It
    is the ``rev`` the client last saw from the server for this page; ``0`` (the
    default) means "I have never seen a server copy" — a first save, or the
    one-time push of a page that until now lived only in localStorage. The
    service refuses the write when it does not match what is stored, so a stale
    second tab whose 2s debounce fires after the first tab saved gets a 409 with
    the current page rather than silently erasing it.

    ``strokes`` and ``book`` are stored verbatim. Clearing a page is
    ``strokes: []``; there is no DELETE.
    """

    strokes: list[dict[str, Any]] = Field(default_factory=list)
    book: dict[str, Any] | None = None
    #: Which ``/other-hand/[[sessionId]]`` session this page belongs to. ``None``
    #: for the unaddressed draft sheet. Grouping only — never part of the key.
    session_id: str | None = Field(default=None, max_length=PAGE_ID_MAX)
    base_rev: int = Field(default=0, ge=0)


class PageResponse(BaseModel):
    """A whole page, ink included. What ``GET /other-hand/pages/{page_id}``
    answers and what a 409 conflict carries so the loser of a race can reload
    without a second round-trip."""

    page_id: str
    session_id: str | None = None
    strokes: list[dict[str, Any]] = Field(default_factory=list)
    book: dict[str, Any] | None = None
    #: Send this back as the next write's ``base_rev``.
    rev: int
    updated_at: datetime
    created_at: datetime


class PageSummaryResponse(BaseModel):
    """One row of the page list — everything EXCEPT the ink.

    A workspace's pages are hundreds of KB each; a list endpoint that echoed
    every stroke would be the most expensive call in the product and nothing
    renders from it. ``stroke_count`` is what a picker actually needs (to tell a
    blank sheet from a full one).
    """

    page_id: str
    session_id: str | None = None
    stroke_count: int
    has_book: bool
    rev: int
    updated_at: datetime
    created_at: datetime


class PageListResponse(BaseModel):
    """``GET /other-hand/pages``. An envelope, not a bare array, so the list can
    grow a cursor without breaking every client."""

    pages: list[PageSummaryResponse] = Field(default_factory=list)


class PageSavedResponse(BaseModel):
    """``PUT /other-hand/pages/{page_id}``. Meta only — echoing the strokes the
    caller just sent would double the cost of the most frequent call on the
    surface (a debounced save every ~2s of drawing)."""

    page_id: str
    rev: int
    updated_at: datetime


__all__ = [
    "PAGE_ID_MAX",
    "IllustrateRequest",
    "PageListResponse",
    "PageResponse",
    "PageSavedResponse",
    "PageSummaryResponse",
    "SnapshotRequest",
    "UpsertPageRequest",
]
