# Site templates — the private-asset detector run before a template goes public.
#
# ``find_private_asset_refs`` walks every string in a snapshot (rippleSpec JSON and
# every source file) and returns the references that point at a workspace's own
# files. A public template is copied into other tenants' workspaces, so a reference
# like that would either leak the owner's file (a storage key or a live presign is
# a bearer token) or break in the copy (an auth-gated API path 401s for everyone
# else). Synchronous, no I/O; reads ``POCKETPAW_PUBLIC_BASE_URL`` per call.
#
# One ``_SHAPES`` row per URL shape the platform mints for workspace files. The
# ``/api/v1/...`` rows match on any host, so the absolute form on the deployment's
# own host is caught by the same row as the relative one. ``/uploads/`` is too
# common a path elsewhere (WordPress) for that, so it matches relative paths and
# the deployment's own host (``POCKETPAW_PUBLIC_BASE_URL``, read per call) only.
# Every row requires an id-like character after the prefix, so a docs page that
# writes ``/api/v1/uploads/{id}`` is not refused.
#
# Deliberately allowed: external URLs, and the public Sites asset rail
# (``sites-assets/{workspace}/{pocket}/...`` on the public bucket), which is the
# only durable public address a site image has.

from __future__ import annotations

import os
import re
from typing import Any

# A URL body: stops at whitespace, quotes, brackets and markup delimiters.
_TAIL = r"[^\s\"'<>()\[\]{}`\\]*"
# An optional ``scheme://host`` in front of a path.
_HOST = r"(?:https?:)?(?://[^\s\"'<>()/]+)?"

_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # GET /api/v1/uploads/{id} — the chat/artifact upload API (auth-gated, or a
    # signed ?t= grant): src/pocketpaw/api/v1/uploads.py.
    ("uploads_api", re.compile(_HOST + r"/api/v1/uploads/[A-Za-z0-9]" + _TAIL)),
    # /api/v1/files — the workspace file library (ee cloud files router and the
    # local files API: content, download, browse, by id).
    ("files_api", re.compile(_HOST + r"/api/v1/files(?:/[A-Za-z0-9]|\?)" + _TAIL)),
    # /api/v1/media/{name} — what studio ``save_generated`` returns
    # (ee/pocketpaw_ee/cloud/media/storage.py), plus browser captures.
    ("media_api", re.compile(_HOST + r"/api/v1/media/[A-Za-z0-9]" + _TAIL)),
    # /api/v1/auth/avatar/{filename} — a user's avatar (auth/router.py).
    ("avatar_api", re.compile(_HOST + r"/api/v1/auth/avatar/[A-Za-z0-9]" + _TAIL)),
    # /uploads/... (relative) — the unauthenticated StaticFiles mount (avatars
    # today, the whole uploads tree historically): ee/pocketpaw_ee/cloud/__init__.py.
    ("uploads_mount", re.compile(r"(?<![\w.~%/-])/uploads/[A-Za-z0-9]" + _TAIL)),
    # Bare storage keys: uploads ``{kind}/{yyyymm}/{uuid32}{ext}``
    # (src/pocketpaw/uploads/keys.py) and media ``generated/{ms}-{hex12}.{ext}``.
    (
        "storage_key",
        re.compile(
            r"(?<![A-Za-z0-9])(?:[a-z][a-z0-9_-]*/\d{6}/[0-9a-f]{32}"
            r"|generated/\d{13}-[0-9a-f]{12})" + _TAIL
        ),
    ),
    # Presigned object-storage reads (S3/R2 SigV4 and SigV2, GCS): expire, and
    # grant whoever holds them the private object until they do.
    (
        "presigned",
        re.compile(
            r"https?://" + _TAIL + r"[?&](?:X-Amz-Signature|X-Goog-Signature|Signature)=" + _TAIL,
            re.IGNORECASE,
        ),
    ),
)


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)


def _own_host_shapes() -> list[re.Pattern[str]]:
    """``/uploads/...`` on the deployment's own host (``POCKETPAW_PUBLIC_BASE_URL``)."""
    # Same default as the OAuth callback builder (auth/social/service.py).
    base = os.environ.get("POCKETPAW_PUBLIC_BASE_URL", "http://localhost:8888").strip().rstrip("/")
    if not base:
        return []
    host = re.escape(base.split("://", 1)[-1])
    return [re.compile(r"(?:https?:)?//" + host + r"/uploads/[A-Za-z0-9]" + _TAIL, re.I)]


def find_private_asset_refs(snapshot: Any) -> list[str]:
    """Every distinct workspace-file reference in ``snapshot``, in walk order."""
    patterns = [pattern for _name, pattern in _SHAPES] + _own_host_shapes()
    found: dict[str, None] = {}
    for text in _strings(snapshot):
        for pattern in patterns:
            for match in pattern.finditer(text):
                found.setdefault(match.group(0), None)
    return list(found)


__all__ = ["find_private_asset_refs"]
