# ee/pocketpaw_ee/cloud/media/storage.py — where generated + uploaded media lives.
#
# The /studio gallery (direct generations, agent-side media MCP output, the
# canvas "save edited image" upload) is backed by the SAME storage swap the
# Files / Knowledge Base uploads use: ``pocketpaw.uploads.build_adapter``.
#
#   * POCKETPAW_UPLOAD_ADAPTER=local (default) — files land under
#     ~/.pocketpaw/generated/<name> (key prefix "generated/", local root
#     ~/.pocketpaw).
#   * POCKETPAW_UPLOAD_ADAPTER=s3 — keys "generated/<name>" in S3_PRIVATE_BUCKET.
#
# The key is flat (``serve_media`` refuses any name with a slash), so a file's
# owning workspace travels IN THE FILENAME as an opaque digest token:
#   * "ws-<token>-…"      gallery file (generation or upload). Listed only to
#                         its workspace; READS stay capability-based so a
#                         published site embedding the URL keeps working.
#   * "browser-<token>-…" /browser capture. Never listed; ``serve_media``
#                         refuses it to any other workspace.
# ``save_generated`` requires ``workspace_id`` so no write path can produce an
# unowned file, and an untagged (legacy) name is listed to NOBODY — see
# ``visible_to``.
#
# Generated names carry a unix-ms timestamp after the owner token
# ("ws-<token>-<ms>-<uuid>.png") so a remote listing (no mtime) can still sort
# newest-first; ``modified_from_name`` reads past the owner prefix.

from __future__ import annotations

import hashlib
import re
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from pocketpaw.uploads.adapter import StorageAdapter
from pocketpaw.uploads.factory import build_adapter

# Local root for the LOCAL adapter. S3 ignores it entirely (keys are the same in
# both modes — "generated/<name>" — so the local path becomes ~/.pocketpaw/<key>).
MEDIA_ROOT = Path.home() / ".pocketpaw"
MEDIA_KEY_PREFIX = "generated"


def media_key(name: str) -> str:
    """The storage key for a gallery file (both adapters)."""
    return f"{MEDIA_KEY_PREFIX}/{name}"


def name_from_key(key: str) -> str:
    """Strip the media prefix back to the filename ('' for a non-media key)."""
    prefix = f"{MEDIA_KEY_PREFIX}/"
    return key[len(prefix) :] if key.startswith(prefix) else ""


def modified_from_name(name: str) -> int:
    """Best-effort ``modified`` (unix ms) from a timestamp-prefixed filename —
    "<ms>-<uuid>.png", optionally behind an owner prefix ("ws-<token>-" or
    "browser-<token>-"). Returns 0 for names without a timestamp (uploads, old
    files) so remote listing still returns a number the frontend expects."""
    if owner_of(name) is not None or capture_owner_of(name) is not None:
        name = name.split("-", 2)[2] if name.count("-") >= 2 else ""
    first = name.split("-", 1)[0]
    return int(first) if first.isdigit() else 0


async def bytes_stream(data: bytes) -> AsyncIterator[bytes]:
    """Adapt an in-memory byte payload into the async byte stream
    ``StorageAdapter.put`` consumes (generated PNGs / uploads are small — no
    chunked file read needed)."""
    yield data


# --- Browser captures (BR-4) --------------------------------------------------
# A /browser screenshot is stored through the SAME adapter and served by the
# SAME ``/api/v1/media/<name>`` route as a studio generation — but it belongs to
# ONE workspace, and the route refuses any name containing a slash
# (``serve_media``'s traversal guard), so a ``generated/<workspace>/<file>`` key
# would have no URL at all. The owner therefore travels in the FILENAME as an
# opaque digest, and ``router.serve_media`` refuses a capture whose token does
# not match the caller's workspace. The digest — not the raw id — keeps the
# tenant id out of a URL that ends up in an image widget.
CAPTURE_PREFIX = "browser-"
# The token shape is checked, not just the prefix: an upload named
# "browser-screenshot.png" is a user's file, not a capture, and treating it as
# one would hide it from the gallery AND 404 it for everybody.
_CAPTURE_TOKEN_RE = re.compile(r"[0-9a-f]{16}\Z")


def capture_owner_token(workspace_id: str) -> str:
    """Opaque, stable per-workspace token baked into a capture's filename."""
    return hashlib.sha256(f"pawbrowser:{workspace_id}".encode()).hexdigest()[:16]


def capture_name_prefix(workspace_id: str) -> str:
    """Filename prefix marking a capture as owned by ``workspace_id``."""
    return f"{CAPTURE_PREFIX}{capture_owner_token(workspace_id)}-"


def capture_owner_of(name: str) -> str | None:
    """The owner token in a capture filename, or None when it is not a capture."""
    if not name.startswith(CAPTURE_PREFIX):
        return None
    token = name[len(CAPTURE_PREFIX) :].split("-", 1)[0]
    return token if _CAPTURE_TOKEN_RE.match(token) else None


# --- Uploaded gallery files -------------------------------------------------
# The SAME problem as a capture, and a deliberately different answer.
#
# ``POST /api/v1/media`` had no auth and ``media_key`` carried no tenant, so one
# gallery was shared by every workspace on the deployment: GET /api/v1/media
# returned every tenant's filenames and URLs, and anyone on the internet could
# add to it. The key cannot grow a workspace segment — ``serve_media`` refuses
# any name containing a slash — so, as with captures, the owner travels in the
# filename.
#
# Where this DIFFERS from a capture, and the difference is the whole design:
# ``serve_media`` does NOT enforce this token. A capture is private to its
# workspace; a gallery image is embedded in ripple specs, and a pocket
# published as a site is served from the edge with no session, so enforcing an
# owner on the read would blank every published page that shows one. The token
# makes a file ATTRIBUTABLE so the LISTING can be scoped. Reads stay
# capability-based on an unguessable name, exactly like /uploads.
#
# LEGACY POLICY: an untagged name is listed to NO workspace. Files written
# before every write path stamped an owner (notably the agent media MCP, which
# saved untagged) cannot be attributed, and showing them to everyone put one
# tenant's generations in every other tenant's gallery. There is no
# single-tenant mode to keep them for: the listing route needs an authenticated
# cloud workspace to run at all. They still SERVE by URL (published sites embed
# them) and direct /studio generations still appear via the generation history.
OWNED_PREFIX = "ws-"
_OWNED_TOKEN_RE = re.compile(r"[0-9a-f]{16}\Z")


def owned_name_prefix(workspace_id: str) -> str:
    """Filename prefix marking a gallery upload as owned by ``workspace_id``."""
    return f"{OWNED_PREFIX}{capture_owner_token(workspace_id)}-"


def owner_of(name: str) -> str | None:
    """The owner token in an uploaded filename, or None when it carries none.

    Shape-checked rather than prefix-checked: a user's own file called
    "ws-holiday.png" is not an owned upload, and treating it as one would hide
    it from every listing including its owner's.
    """
    if not name.startswith(OWNED_PREFIX):
        return None
    token = name[len(OWNED_PREFIX) :].split("-", 1)[0]
    return token if _OWNED_TOKEN_RE.match(token) else None


def visible_to(name: str, workspace_id: str | None) -> bool:
    """Whether ``name`` belongs in ``workspace_id``'s gallery listing.

    Only a file stamped with ``workspace_id``'s token. An untagged (legacy) name
    is visible to nobody: see the LEGACY POLICY note above."""
    owner = owner_of(name)
    if owner is None or not workspace_id:
        return False
    return owner == capture_owner_token(workspace_id)


async def save_generated(
    data: bytes, *, mime: str, workspace_id: str, ext: str = "png", capture: bool = False
) -> str:
    """Persist a freshly generated blob (image / audio / …) through the media
    adapter and return its backend-relative ``/api/v1/media/<name>`` URL.

    ``workspace_id`` is REQUIRED: it is stamped into the name as the owner token
    (``owned_name_prefix``), which is the only thing that scopes the gallery
    listing. ``capture=True`` stamps a /browser capture prefix instead
    (``capture_name_prefix``), private on read and never listed.

    After the owner token the name carries a unix-ms timestamp so a remote
    listing can sort newest-first and report ``modified``."""
    if not workspace_id:
        raise ValueError("save_generated needs the owning workspace_id")
    prefix = capture_name_prefix(workspace_id) if capture else owned_name_prefix(workspace_id)
    name = f"{prefix}{int(time.time() * 1000)}-{uuid.uuid4().hex[:12]}.{ext.lstrip('.')}"
    await get_adapter().put(media_key(name), bytes_stream(data), mime)
    return f"/api/v1/media/{name}"


# Module-level singleton, same pattern as the uploads router — one adapter per
# process. Tests swap ``_ADAPTER`` for a tmp-backed local adapter.
_ADAPTER: StorageAdapter = build_adapter(MEDIA_ROOT)


def get_adapter() -> StorageAdapter:
    """Return the process's media storage adapter."""
    return _ADAPTER


def local_generated_dir() -> Path | None:
    """The on-disk generated directory for the LOCAL adapter, or None when the
    configured adapter is remote (S3). Lets the list/serve routes branch on the
    live backend instead of sniffing env."""
    return get_adapter().local_path(f"{MEDIA_KEY_PREFIX}/")


__all__ = [
    "CAPTURE_PREFIX",
    "OWNED_PREFIX",
    "MEDIA_ROOT",
    "MEDIA_KEY_PREFIX",
    "media_key",
    "name_from_key",
    "modified_from_name",
    "bytes_stream",
    "capture_name_prefix",
    "capture_owner_of",
    "capture_owner_token",
    "owned_name_prefix",
    "owner_of",
    "visible_to",
    "save_generated",
    "get_adapter",
    "local_generated_dir",
]
