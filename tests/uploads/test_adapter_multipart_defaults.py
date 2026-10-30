"""StorageAdapter protocol defaults for the multipart surface.

Created 2026-09-14 (feat/uploads-multipart-adapter). New file.

Pins the one asymmetry in the surface: five methods raise, and
``supports_presigned_parts`` returns False. The failure this catches is an
adapter that inherits a silent no-op — a 5 GB upload that reports success and
lands as nothing. A raise is the only safe default for a write path.
"""

from __future__ import annotations

import pytest

from pocketpaw.uploads.adapter import StorageAdapter


class _BareAdapter(StorageAdapter):
    """An adapter that implements nothing beyond the protocol defaults."""


async def test_create_multipart_raises_by_default():
    with pytest.raises(NotImplementedError):
        await _BareAdapter().create_multipart("k", "image/png")


async def test_sign_part_raises_by_default():
    with pytest.raises(NotImplementedError):
        await _BareAdapter().sign_part("k", "u", 1, 300)


async def test_put_part_raises_by_default():
    with pytest.raises(NotImplementedError):
        await _BareAdapter().put_part("k", "u", 1, b"x")


async def test_complete_multipart_raises_by_default():
    with pytest.raises(NotImplementedError):
        await _BareAdapter().complete_multipart("k", "u", [(1, "etag")])


async def test_abort_multipart_raises_by_default():
    with pytest.raises(NotImplementedError):
        await _BareAdapter().abort_multipart("k", "u")


def test_supports_presigned_parts_defaults_false_rather_than_raising():
    """Callers ask this BEFORE they know the rest exists, so it must answer."""
    assert _BareAdapter().supports_presigned_parts() is False
