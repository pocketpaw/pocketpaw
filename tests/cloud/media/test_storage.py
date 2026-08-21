# tests/cloud/media/test_storage.py — media storage key/name helpers.
#
# The gallery stores files under the media key prefix ("generated/<name>") on
# whichever adapter the deployment configures. Generated filenames carry a
# unix-ms prefix so a remote listing (no mtime) can sort newest-first and report
# ``modified``. These are the pure helpers both the studio service and the media
# router lean on.
#
# Created 2026-08-17 (studio-media-s3): new storage tests.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.media import storage


def test_media_key_and_name_round_trip() -> None:
    assert storage.media_key("abc.png") == "generated/abc.png"
    assert storage.name_from_key("generated/abc.png") == "abc.png"
    # Non-media keys strip to nothing (defensive for stray S3 prefixes).
    assert storage.name_from_key("projects/ws/x.png") == ""


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("1700000000000-abcdef.png", 1700000000000),  # generated (timestamped)
        ("edited.png", 0),  # upload (no timestamp)
        ("gen-uuid.png", 0),  # old local file (no timestamp)
        ("-abc.png", 0),  # malformed leading dash
    ],
)
def test_modified_from_name(name, expected) -> None:
    assert storage.modified_from_name(name) == expected


async def test_bytes_stream_yields_the_payload() -> None:
    chunks = [c async for c in storage.bytes_stream(b"hello")]
    assert chunks == [b"hello"]
