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

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
import tempfile
from pathlib import Path

from pocketpaw_ee.cloud.agent_jail import workspace_jail_root

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


def write_snapshot(workspace_id: str, page_id: str, png_base64: str) -> str:
    """Write the page snapshot and return the absolute path the agent can ``Read``.

    Overwrites any previous snapshot for this ``page_id`` — v1 keeps exactly one
    live snapshot per page and no history. Raises ``SnapshotError`` for every
    rejected input; the router maps it to the cloud error envelope.
    """
    if not workspace_id:
        raise SnapshotError(400, "other_hand.no_workspace", "no workspace bound to this request")

    safe_page_id = _safe_page_id(page_id)
    data = _decode_png(png_base64)

    directory = snapshot_dir(workspace_id)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{safe_page_id}.png"

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


__all__ = [
    "MAX_SNAPSHOT_BYTES",
    "SnapshotError",
    "snapshot_dir",
    "write_snapshot",
]
