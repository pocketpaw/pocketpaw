"""Path helpers for the "My Files" folder tree.

Paths are absolute, forward-slash, normalized, with no trailing slash
(the root is ``"/"``). Names cannot contain slashes, ``..``, null bytes,
or control characters.
"""

from __future__ import annotations

_MAX_SEGMENT_LEN = 255
_MAX_PATH_LEN = 1024


def _validate_name(name: str) -> str:
    """Validate a single path segment; return it unchanged on success."""
    if not name:
        raise ValueError("empty path segment")
    if name in (".", ".."):
        raise ValueError(f"invalid path segment: {name!r}")
    if "/" in name or "\\" in name:
        raise ValueError(f"path segment must not contain slashes: {name!r}")
    if len(name) > _MAX_SEGMENT_LEN:
        raise ValueError(
            f"segment too long ({len(name)} > {_MAX_SEGMENT_LEN})"
        )
    for ch in name:
        # Reject control chars (0x00-0x1F, 0x7F) and null explicitly.
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise ValueError(f"control character in path segment: {name!r}")
    return name


def normalize_path(p: str | None) -> str:
    """Return an absolute normalized path with no trailing slash.

    Collapses repeated slashes, strips ``.`` segments, rejects ``..``,
    rejects control chars and empty strings. The root is ``"/"``.
    """
    if p is None or p == "" or p == "/":
        return "/"
    if not isinstance(p, str):
        raise ValueError("path must be a string")
    if len(p) > _MAX_PATH_LEN:
        raise ValueError(f"path too long ({len(p)} > {_MAX_PATH_LEN})")
    if "\x00" in p:
        raise ValueError("null byte in path")
    if not p.startswith("/"):
        raise ValueError("path must be absolute (start with '/')")

    segments: list[str] = []
    for raw in p.split("/"):
        if raw == "" or raw == ".":
            continue
        if raw == "..":
            raise ValueError("'..' segment not allowed")
        segments.append(_validate_name(raw))

    if not segments:
        return "/"
    return "/" + "/".join(segments)


def is_subpath(ancestor: str, p: str) -> bool:
    """Return True if ``p`` equals ``ancestor`` or is a strict descendant."""
    a = normalize_path(ancestor)
    c = normalize_path(p)
    if a == c:
        return True
    if a == "/":
        return c != "/"
    return c.startswith(a + "/")


def join_path(parent: str, name: str) -> str:
    """Join a normalized ``parent`` path with a raw ``name`` segment."""
    _validate_name(name)
    base = normalize_path(parent)
    if base == "/":
        joined = "/" + name
    else:
        joined = base + "/" + name
    if len(joined) > _MAX_PATH_LEN:
        raise ValueError(f"path too long ({len(joined)} > {_MAX_PATH_LEN})")
    return joined


def parent_of(p: str) -> str:
    """Return the parent path of ``p``. Parent of ``"/"`` is ``"/"``."""
    n = normalize_path(p)
    if n == "/":
        return "/"
    head, _, _ = n.rpartition("/")
    return head or "/"


def basename(p: str) -> str:
    """Return the final segment of ``p`` (empty string for root)."""
    n = normalize_path(p)
    if n == "/":
        return ""
    return n.rsplit("/", 1)[-1]
