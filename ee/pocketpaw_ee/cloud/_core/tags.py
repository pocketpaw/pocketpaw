# Discover tags — the one tag dialect shared by Discover and its sources.
#
# A tag is 1-32 characters of ``[a-z0-9-]``; a listing holds at most ``MAX_TAGS``.
# ``normalize_tags`` is the write-side rule: trim, lowercase, fold runs of spaces
# and underscores to one ``-``, drop empties and duplicates (first wins), then
# refuse anything still outside the dialect. It lives in ``_core`` because the
# sources (``site_templates``) must validate with the same rule Discover stores
# under, and they never import ``discover``.

from __future__ import annotations

import re
from collections.abc import Iterable

MAX_TAGS = 8
MAX_TAG_LENGTH = 32
TAG_PATTERN = re.compile(r"^[a-z0-9-]{1,32}$")
_SEPARATORS = re.compile(r"[\s_]+")


def normalize_tag(raw: str) -> str:
    """One tag, normalised; ``""`` when nothing is left. Raises ``ValueError``
    when it is not a string or the result is outside ``[a-z0-9-]{1,32}``."""
    if not isinstance(raw, str):
        raise ValueError("A tag must be a string")
    tag = _SEPARATORS.sub("-", raw.strip().lower())
    if tag and not TAG_PATTERN.fullmatch(tag):
        raise ValueError(f"Tag {raw!r} must be 1-{MAX_TAG_LENGTH} characters of a-z, 0-9 and '-'")
    return tag


def normalize_tags(raw: Iterable[str] | None) -> list[str]:
    """A tag list, normalised and de-duplicated in order. Raises ``ValueError``
    for a bad tag, a value that is not a list, or more than ``MAX_TAGS``
    distinct tags."""
    if raw is not None and not isinstance(raw, list | tuple):
        raise ValueError("Tags must be a list of strings")
    tags = list(dict.fromkeys(t for t in (normalize_tag(r) for r in raw or ()) if t))
    if len(tags) > MAX_TAGS:
        raise ValueError(f"At most {MAX_TAGS} tags")
    return tags


__all__ = ["MAX_TAGS", "MAX_TAG_LENGTH", "TAG_PATTERN", "normalize_tag", "normalize_tags"]
