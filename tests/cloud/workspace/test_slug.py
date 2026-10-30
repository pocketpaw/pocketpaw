"""Tests for the workspace slug rules (``workspace/slug.py``).

New file. Covers the pure (no-DB) gates — format regex and the reserved
set — that the create-request validator, the availability service, and the
paw-enterprise client all share. The DB-backed ``taken`` path is exercised
separately in ``test_service_v2.py``.
"""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.workspace.slug import (
    RESERVED_SLUGS,
    SLUG_RE,
    static_slug_reason,
)


@pytest.mark.parametrize("slug", ["acme-corp", "x", "7", "test-workspace-1", "a1b2"])
def test_valid_slugs_match(slug: str) -> None:
    assert SLUG_RE.match(slug) is not None
    assert static_slug_reason(slug) is None


@pytest.mark.parametrize(
    "slug",
    ["Invalid Slug!", "-bad", "bad-", "BadSlug", "", "has space", "under_score", "emoji😀"],
)
def test_malformed_slugs_are_invalid(slug: str) -> None:
    assert static_slug_reason(slug) == "invalid"


@pytest.mark.parametrize("slug", ["admin", "api", "www", "settings", "pocketpaw"])
def test_reserved_slugs_are_reserved(slug: str) -> None:
    # Well-formed but reserved → "reserved", not "invalid".
    assert SLUG_RE.match(slug) is not None
    assert static_slug_reason(slug) == "reserved"


def test_reserved_set_is_all_lowercase_and_well_formed() -> None:
    # Every reserved entry must itself be a legal slug, otherwise it could
    # never collide with a user-supplied (already format-checked) value.
    for handle in RESERVED_SLUGS:
        assert handle == handle.lower()
        assert SLUG_RE.match(handle) is not None
