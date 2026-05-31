"""Storage-key generation for uploads.

Keys are opaque to external callers and namespaced by "kind" + a yyyymm bucket.
The UUID4 hex tail guarantees uniqueness.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

_EXT_RE = re.compile(r"[^a-z0-9]")
_MAX_EXT_LEN = 8


def sanitize_ext(ext: str) -> str:
    """Normalize a file extension to ``.{alnum,<=8}`` or empty."""
    if not ext:
        return ""
    tail = ext.lstrip(".").lower()
    tail = _EXT_RE.sub("", tail)[:_MAX_EXT_LEN]
    return f".{tail}" if tail else ""


def new_storage_key(kind: str = "chat", ext: str = "") -> str:
    """Return a fresh unique storage key ``{kind}/{yyyymm}/{uuid32}{ext}``."""
    yyyymm = datetime.now(UTC).strftime("%Y%m")
    safe_ext = sanitize_ext(ext)
    return f"{kind}/{yyyymm}/{uuid.uuid4().hex}{safe_ext}"
