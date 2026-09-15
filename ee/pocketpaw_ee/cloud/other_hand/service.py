# service.py — Otherhand page-snapshot persistence.
#
# Created: 2026-08-25 (feat/other-hand-surface, Otherhand v1) — decodes the
# frontend's base64 PNG of the notebook page and writes it to a workspace-scoped
# scratch path the agent can ``Read``.
#
# This is a FILESYSTEM WRITE DRIVEN BY USER INPUT, so it is treated as hostile
# input end to end. Three guards, in the order they matter:
#
#   1. ``page_id`` is a single safe path SEGMENT or the request is refused. The
#      charset excludes ``/`` and the literals ``.`` / ``..`` are rejected
#      outright, so a crafted id cannot climb out of the workspace's directory.
#      The pattern is anchored with ``\Z`` and not ``$``, because ``$`` also
#      matches just before a trailing newline — ``"..%0a"`` decoded would slip a
#      ``$``-anchored guard. This mirrors ``agent_jail._SAFE_SEGMENT``, which
#      guards the sibling agent-cwd tree for exactly the same reason; it is
#      copied rather than imported because that name is private to that module,
#      and a shared guard that one side is free to relax is worse than two.
#   2. Size is capped BEFORE the decode (on the base64 string) and again after,
#      so a 200MB payload is rejected without first materializing 150MB of bytes.
#   3. The decoded bytes must start with the PNG magic number. The endpoint
#      promises the agent an image; a file that is not one would be handed to
#      ``Read`` as if it were.
#
# The write is atomic-by-rename: the PNG lands in a temp file in the same
# directory and is then ``os.replace``d onto the target. The agent may be reading
# last turn's snapshot at the moment this turn's arrives, and a partially-written
# PNG reads as a corrupt image rather than as an error.
#
# Where it lands: ``<workspace jail root>/<workspace_id>/other_hand/<page_id>.png``
# — a sibling of ``<workspace_id>/agent/<session_id>/``, the per-session agent cwd.
# Reusing ``agent_jail.workspace_jail_root()`` means the snapshots inherit the
# tenant isolation and the deployment's data-volume override
# (``POCKETPAW_WORKSPACE_JAIL_ROOT``) rather than inventing a second root that
# would have to be configured separately. Deliberately NOT inside ``agent/``: the
# jail GC sweeps that subtree on an idle TTL, and a page's snapshot should not be
# evicted out from under a user who left the tab open over lunch. The flip side
# is that snapshots do not count toward the jail quota — bounded in practice by
# one overwritten file per page, which is why that is acceptable rather than
# merely convenient.

# Updated 2026-09-15 (feat/otherhand-page-store): this module gained a SECOND
# concern — the server-side notebook PAGE store (``get_page`` / ``upsert_page``
# / ``list_pages``, below the snapshot section). They share a file because the
# cloud entity rules put every Beanie write for an entity behind exactly one
# ``service.py``, and ``OtherhandPage`` is this entity's document. The two halves
# share nothing but ``_safe_page_id``, which is the point: a page and its
# snapshot are addressed by the SAME client-minted id, so one validator has to
# govern both or the two stores drift into different notions of "this page".
#
# The page half is Mongo, not disk. That is deliberate and is also the reason the
# snapshot half is a known bug: ``snapshot_dir`` writes to LOCAL DISK, which is
# per-replica, and the deployment runs more than one. Out of scope here; tracked
# as a follow-up.

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from pymongo.errors import DuplicateKeyError

from pocketpaw_ee.cloud._core.errors import (
    CloudError,
    NotFound,
    OtherhandPageConflict,
    PayloadTooLarge,
)
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import OtherhandPageSaved
from pocketpaw_ee.cloud.agent_jail import workspace_jail_root
from pocketpaw_ee.cloud.models.other_hand_page import OtherhandPage
from pocketpaw_ee.cloud.other_hand.domain import Page
from pocketpaw_ee.cloud.other_hand.dto import (
    PageListResponse,
    PageResponse,
    PageSavedResponse,
    PageSummaryResponse,
    UpsertPageRequest,
)

logger = logging.getLogger(__name__)

# Max DECODED snapshot size. A full-page 1240x1754 PNG of pen strokes is well
# under a megabyte; 12MB is generous enough that a high-DPI or image-heavy page
# never trips it, and small enough that a workspace cannot be used as free
# storage one page at a time.
MAX_SNAPSHOT_BYTES = 12 * 1024 * 1024

# Max length of the base64 STRING, checked before decoding. Base64 inflates by
# 4/3; the small slack absorbs padding, and any whitespace/data-URI prefix the
# client may have left on. Rejecting here means an oversize payload never gets
# decoded into memory.
MAX_SNAPSHOT_B64_CHARS = (MAX_SNAPSHOT_BYTES * 4) // 3 + 1024

# The PNG magic number (the 8-byte signature every PNG file starts with).
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# A page id is one safe path segment. See the ``\Z`` note in the module comment.
_SAFE_PAGE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}\Z")

# Optional data-URI prefix the browser's ``canvas.toDataURL()`` produces. The
# contract asks for bare base64, but accepting the prefix costs one strip and
# removes a whole class of "works in curl, fails in the app" bug.
_DATA_URI_PREFIX = "data:image/png;base64,"

# Subdirectory under the workspace's jail root. A sibling of ``agent/``, so a
# snapshot can never be mistaken for (or evicted as) agent scratch.
_SNAPSHOT_DIRNAME = "other_hand"


class SnapshotError(Exception):
    """A snapshot the service refused to write.

    ``status_code`` is the HTTP status the router maps this to, and ``code`` is
    the machine-readable error code. Raised — never returned — so no caller can
    treat a refusal as a path.
    """

    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _safe_page_id(page_id: str) -> str:
    """Return *page_id* if it is one safe path segment, else raise.

    The whole traversal guard. Rejects anything containing a path separator, the
    ``.``/``..`` specials, an empty string, or a byte outside the safe charset —
    which together mean the value can only ever name a file directly inside the
    workspace's own snapshot directory.

    The value is validated VERBATIM — deliberately not stripped first. Stripping
    would accept ``"page-1\\n"`` and quietly write it as ``page-1``, which is the
    same class of mistake as anchoring the pattern with ``$``: it makes two
    distinct client-supplied ids address one file, and it means the id the
    validator approved is not the id the caller sent. Refusing is both safer and
    easier to debug than silently renaming.
    """
    candidate = page_id or ""
    if candidate in {".", ".."} or not _SAFE_PAGE_ID.match(candidate):
        raise SnapshotError(
            400,
            "other_hand.invalid_page_id",
            "page_id must be a single safe path segment "
            "(letters, digits, '_', '.', '-'; max 128 characters)",
        )
    return candidate


def snapshot_dir(workspace_id: str) -> Path:
    """The workspace's snapshot directory. Not created here — see ``write_snapshot``."""
    ws_segment = _safe_page_id(workspace_id)
    return workspace_jail_root() / ws_segment / _SNAPSHOT_DIRNAME


def _decode_png(png_base64: str) -> bytes:
    """Decode and validate the payload, or raise ``SnapshotError``."""
    raw = (png_base64 or "").strip()
    if raw.startswith(_DATA_URI_PREFIX):
        raw = raw[len(_DATA_URI_PREFIX) :]
    if not raw:
        raise SnapshotError(400, "other_hand.empty_snapshot", "png_base64 is empty")
    if len(raw) > MAX_SNAPSHOT_B64_CHARS:
        raise SnapshotError(
            413,
            "other_hand.snapshot_too_large",
            f"snapshot exceeds the {MAX_SNAPSHOT_BYTES // (1024 * 1024)}MB limit",
        )

    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SnapshotError(
            400, "other_hand.invalid_base64", "png_base64 is not valid base64"
        ) from exc

    if len(data) > MAX_SNAPSHOT_BYTES:
        raise SnapshotError(
            413,
            "other_hand.snapshot_too_large",
            f"snapshot exceeds the {MAX_SNAPSHOT_BYTES // (1024 * 1024)}MB limit",
        )
    if not data.startswith(_PNG_MAGIC):
        raise SnapshotError(
            400,
            "other_hand.not_a_png",
            "snapshot must be a PNG image",
        )
    return data


#: The two images a turn can carry. ``page`` is the notebook the agent draws
#: on; ``book`` is the read-only source page beside it (book mode, added
#: 2026-08-26). A closed set, because it becomes part of a FILENAME — an
#: open-ended ``kind`` would be a second traversal vector past the page_id
#: guard.
SNAPSHOT_KINDS: tuple[str, ...] = ("page", "book", "mark")


def _kind_suffix(kind: str) -> str:
    """Filename suffix for a snapshot kind, or raise on an unknown kind."""
    if kind not in SNAPSHOT_KINDS:
        raise SnapshotError(
            400,
            "other_hand.invalid_kind",
            f"kind must be one of {', '.join(SNAPSHOT_KINDS)}",
        )
    # "page" keeps the original bare filename so v1 pages and their tests are
    # byte-identical; only the new kind takes a suffix.
    return "" if kind == "page" else f".{kind}"


def write_snapshot(workspace_id: str, page_id: str, png_base64: str, kind: str = "page") -> str:
    """Write a snapshot and return the absolute path the agent can ``Read``.

    Overwrites any previous snapshot for this ``page_id`` and ``kind`` — v1
    keeps exactly one live image per page per kind and no history. Raises
    ``SnapshotError`` for every rejected input; the router maps it to the cloud
    error envelope.
    """
    if not workspace_id:
        raise SnapshotError(400, "other_hand.no_workspace", "no workspace bound to this request")

    safe_page_id = _safe_page_id(page_id)
    suffix = _kind_suffix(kind)
    data = _decode_png(png_base64)

    directory = snapshot_dir(workspace_id)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{safe_page_id}{suffix}.png"

    # Belt and braces on the traversal guard: the resolved target must still sit
    # inside the resolved directory. ``_safe_page_id`` already makes this true;
    # this catches a future edit to the charset that quietly makes it false.
    resolved_dir = directory.resolve()
    resolved_target = (resolved_dir / target.name).resolve()
    if resolved_target.parent != resolved_dir:
        raise SnapshotError(400, "other_hand.invalid_page_id", "page_id escapes the page directory")

    # Atomic replace so a concurrent ``Read`` never sees a half-written PNG.
    fd, tmp_name = tempfile.mkstemp(dir=str(resolved_dir), prefix=".snap-", suffix=".png")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_name, resolved_target)
    except Exception:
        # Best-effort cleanup; the write failure is what propagates.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    logger.debug(
        "other_hand: wrote snapshot for page %s (%d bytes)",
        safe_page_id,
        len(data),
    )
    return str(resolved_target)


# ── Page store (feat/otherhand-page-store, 2026-09-15) ──────────────────────
#
# Everything below is the server-side page: the thing that makes a notebook page
# survive a reload and follow the user to another browser. It replaces nothing —
# the snapshot half above is a rendered PNG for the AGENT to read, this half is
# the ink model for the RENDERER to restore. Both keyed by the same page_id.


# Max serialized size of one page's ``strokes`` + ``book``, in bytes.
#
# The ceiling exists because the frontend will not stop growing the payload: a
# single drawn circle became ~242 points across 2 strokes when rough.js landed,
# and an ``img`` stroke carries its picture as a data URL, so a page with a few
# generated illustrations is megabytes on its own. 8MiB leaves 2x headroom under
# Mongo's 16MB document cap after BSON framing, and sits far under the ASGI body
# ceiling (``src/pocketpaw/security/body_limit.py``, ~1GB by default) so a page
# that trips this gets OUR error and not a generic one from the layer outside.
#
# Over the ceiling we REFUSE THE WHOLE WRITE (413) and leave the stored page
# exactly as it was. Truncating would be worse than losing the save: a page that
# comes back missing its last half-hour reads as corruption, and the user has no
# way to tell which half survived. A loud refusal is recoverable — the ink is
# still in the browser.
MAX_PAGE_BYTES = 8 * 1024 * 1024


def _page_payload_bytes(strokes: list[dict[str, Any]], book: dict[str, Any] | None) -> int:
    """Serialized size of what this write would store.

    Measured on the JSON the client sent, not on point counts: the expensive
    thing on a page is an ``img`` stroke's embedded data URL, which no count of
    points sees at all.
    """
    return len(json.dumps({"strokes": strokes, "book": book}, separators=(",", ":")))


def _page_key(page_id: str) -> str:
    """``_safe_page_id`` for the page store: same guard, cloud error envelope.

    The snapshot half raises ``SnapshotError`` and its router maps it; the page
    endpoints are thin and rely on the central ``CloudError`` handler, so the
    refusal is re-raised as one here rather than surfacing as a 500.
    """
    try:
        return _safe_page_id(page_id)
    except SnapshotError as exc:
        raise CloudError(exc.status_code, exc.code, exc.message) from exc


def _page_to_domain(doc: OtherhandPage) -> Page:
    """Beanie document -> the value object every reader outside this module sees."""
    return Page(
        workspace_id=doc.workspace,
        page_id=doc.page_id,
        user_id=doc.user_id,
        rev=doc.rev,
        updated_at=doc.updatedAt,
        created_at=doc.createdAt,
        session_id=doc.session_id,
        strokes=doc.strokes,
        book=doc.book,
    )


def _page_wire(page: Page) -> dict[str, Any]:
    """The full-page wire dict (``PageResponse``'s shape)."""
    return PageResponse.model_validate(page, from_attributes=True).model_dump(mode="json")


async def get_page(workspace_id: str, user_id: str, page_id: str) -> dict[str, Any]:
    """Return one page, ink included. Raises ``NotFound`` when there is none.

    404 rather than an empty page on purpose: the frontend starts blank on a
    miss, and an empty 200 is indistinguishable from "a page that exists and was
    cleared" — which matters because the latter must NOT be overwritten by a
    tab restoring from localStorage.
    """
    safe_id = _page_key(page_id)
    doc = await OtherhandPage.find_one(
        OtherhandPage.workspace == workspace_id,
        OtherhandPage.page_id == safe_id,
    )
    if doc is None:
        raise NotFound("other_hand.page", safe_id)
    return _page_wire(_page_to_domain(doc))


async def list_pages(workspace_id: str, user_id: str, limit: int = 100) -> dict[str, Any]:
    """The workspace's pages, newest first, WITHOUT their ink.

    Strokes are deliberately not projected: a workspace's pages are hundreds of
    KB each and nothing renders from a list. ``stroke_count`` is what a picker
    needs to tell a blank sheet from a full one.
    """
    docs = (
        await OtherhandPage.find(OtherhandPage.workspace == workspace_id)
        .sort(-OtherhandPage.updatedAt)
        .limit(max(1, min(limit, 500)))
        .to_list()
    )
    rows = [
        PageSummaryResponse(
            page_id=doc.page_id,
            session_id=doc.session_id,
            stroke_count=len(doc.strokes),
            has_book=doc.book is not None,
            rev=doc.rev,
            updated_at=doc.updatedAt,
            created_at=doc.createdAt,
        )
        for doc in docs
    ]
    return PageListResponse(pages=rows).model_dump(mode="json")


async def upsert_page(workspace_id: str, user_id: str, page_id: str, body: Any) -> dict[str, Any]:
    """Create or replace a page's ink. Last-write-wins, guarded by a CAS.

    ``body.base_rev`` is the ``rev`` the caller last saw; ``0`` means "no server
    copy". It must equal what is stored (or the page must not exist) or the write
    is refused with ``OtherhandPageConflict``, which carries the current page so
    the loser reloads without a second round-trip. That is the entire multi-tab
    story — two tabs on one page is the common case, not a rare multi-device
    race, and a plain LWW overwrite there is silent data loss.

    Idempotent for identical content: a re-save of the same strokes and book is
    a no-op that returns the current ``rev`` without touching the document or
    emitting. The client debounces at ~2s and also saves on blur and
    visibilitychange, so the same bytes arrive repeatedly while a hand rests on
    the page.
    """
    body = UpsertPageRequest.model_validate(body)
    safe_id = _page_key(page_id)

    size = _page_payload_bytes(body.strokes, body.book)
    if size > MAX_PAGE_BYTES:
        # Refused BEFORE any write — a rejected save must leave the stored page
        # byte-identical, never half-applied.
        raise PayloadTooLarge(
            "other_hand.page_too_large",
            f"This page is {size // (1024 * 1024)}MB, over the "
            f"{MAX_PAGE_BYTES // (1024 * 1024)}MB limit. Nothing was saved.",
        )

    doc = await OtherhandPage.find_one(
        OtherhandPage.workspace == workspace_id,
        OtherhandPage.page_id == safe_id,
    )

    stored_rev = doc.rev if doc is not None else 0
    if body.base_rev != stored_rev:
        raise OtherhandPageConflict(_page_wire(_page_to_domain(doc)) if doc is not None else None)

    if doc is None:
        doc = OtherhandPage(
            workspace=workspace_id,
            page_id=safe_id,
            user_id=user_id,
            session_id=body.session_id,
            strokes=body.strokes,
            book=body.book,
            rev=1,
        )
        try:
            await doc.insert()
        except DuplicateKeyError:
            # Two tabs racing to CREATE the same page: both read "no doc", both
            # insert, the unique index refuses the second. That is the same
            # lost race as a stale base_rev and gets the same answer — a 409
            # carrying whichever copy won — not a 500.
            winner = await OtherhandPage.find_one(
                OtherhandPage.workspace == workspace_id,
                OtherhandPage.page_id == safe_id,
            )
            raise OtherhandPageConflict(
                _page_wire(_page_to_domain(winner)) if winner is not None else None
            ) from None
    else:
        unchanged = (
            doc.strokes == body.strokes
            and doc.book == body.book
            and doc.session_id == body.session_id
        )
        if unchanged:
            # no-event: nothing changed. Emitting here would put an event on the
            # bus every ~2s for a page nobody is drawing on.
            return PageSavedResponse(
                page_id=doc.page_id, rev=doc.rev, updated_at=doc.updatedAt
            ).model_dump(mode="json")
        doc.strokes = body.strokes
        doc.book = body.book
        doc.session_id = body.session_id
        doc.user_id = user_id
        doc.rev = stored_rev + 1
        await doc.save()

    await emit(
        OtherhandPageSaved(
            data={
                "workspace_id": workspace_id,
                "page_id": doc.page_id,
                "session_id": doc.session_id,
                "user_id": user_id,
                "rev": doc.rev,
                "updated_at": doc.updatedAt.isoformat(),
            }
        )
    )
    return PageSavedResponse(page_id=doc.page_id, rev=doc.rev, updated_at=doc.updatedAt).model_dump(
        mode="json"
    )


__all__ = [
    "MAX_PAGE_BYTES",
    "MAX_SNAPSHOT_BYTES",
    "SnapshotError",
    "get_page",
    "list_pages",
    "snapshot_dir",
    "upsert_page",
    "write_snapshot",
]
