"""S3StorageAdapter in PUBLIC mode — the world-readable bucket rail.

Created 2026-08-31 (feat/sites-public-asset-uploads). New file.

Mocks the boto3 client directly, matching ``test_s3_adapter.py`` — the adapter's
job here is to translate "this object must be publicly readable" into the right
boto kwargs, and a mock asserts that far more precisely than a fake S3 would.

THE FAILURE THESE EXIST TO CATCH is quiet and expensive: an object uploaded
without ``ACL=public-read`` still gets a perfectly well-formed URL back, is
recorded as a successful upload, and renders as a broken image for every visitor
to the published site. Nothing on the write path notices. So the ACL kwarg is
asserted directly, and its absence in private mode is asserted too — a private
bucket must never start attaching a public ACL because a shared code path grew a
new flag.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("boto3")

from pocketpaw.uploads.s3 import S3StorageAdapter  # noqa: E402


def _make_adapter(
    client: MagicMock,
    *,
    public_base_url: str | None = None,
    public_read: bool = False,
) -> S3StorageAdapter:
    adapter = S3StorageAdapter.__new__(S3StorageAdapter)
    adapter._bucket = "test-bucket"  # type: ignore[attr-defined]
    adapter._client = client  # type: ignore[attr-defined]
    adapter._public_base_url = public_base_url  # type: ignore[attr-defined]
    adapter._public_read = public_read  # type: ignore[attr-defined]
    return adapter


async def _aiter(chunks):
    for c in chunks:
        yield c


async def test_public_mode_uploads_with_a_public_read_acl():
    client = MagicMock()
    adapter = _make_adapter(client, public_base_url="https://cdn.test", public_read=True)

    await adapter.put("sites-assets/ws/pk/abc.png", _aiter([b"bytes"]), "image/png")

    kwargs = client.put_object.call_args.kwargs
    assert kwargs["ACL"] == "public-read"


async def test_public_mode_marks_content_addressed_objects_immutable():
    """Keys carry a content hash, so the bytes at one key can never change."""
    client = MagicMock()
    adapter = _make_adapter(client, public_base_url="https://cdn.test", public_read=True)

    await adapter.put("sites-assets/ws/pk/abc.png", _aiter([b"bytes"]), "image/png")

    kwargs = client.put_object.call_args.kwargs
    assert kwargs["CacheControl"] == "public, max-age=31536000, immutable"


async def test_private_mode_sends_no_acl_and_no_cache_control():
    """The private bucket's wire call must be byte-identical to before this flag."""
    client = MagicMock()
    adapter = _make_adapter(client)

    await adapter.put("chat/2026-08/abcd.png", _aiter([b"bytes"]), "image/png")

    kwargs = client.put_object.call_args.kwargs
    assert "ACL" not in kwargs
    assert "CacheControl" not in kwargs
    assert set(kwargs) == {"Bucket", "Key", "Body", "ContentType"}


def test_public_url_joins_base_and_key_without_a_double_slash():
    adapter = _make_adapter(MagicMock(), public_base_url="https://cdn.test/bucket")
    assert adapter.public_url("a/b.png") == "https://cdn.test/bucket/a/b.png"
    # A key that arrives already slash-prefixed must not produce "//".
    assert adapter.public_url("/a/b.png") == "https://cdn.test/bucket/a/b.png"


def test_public_url_is_none_without_a_base_url():
    """A private adapter answering "no public address" is what keeps links honest."""
    adapter = _make_adapter(MagicMock())
    assert adapter.public_url("a/b.png") is None
