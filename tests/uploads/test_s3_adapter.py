"""Unit tests for S3StorageAdapter.

Mocks the boto3 client directly rather than spinning up moto — the adapter's
job is to translate the Protocol methods into the right boto calls, plus
exception mapping. Integration coverage against real S3 is out of scope.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("boto3")

from pocketpaw.uploads.errors import NotFound, StorageFailure  # noqa: E402
from pocketpaw.uploads.s3 import S3StorageAdapter  # noqa: E402


def _make_adapter(client: MagicMock) -> S3StorageAdapter:
    adapter = S3StorageAdapter.__new__(S3StorageAdapter)
    adapter._bucket = "test-bucket"  # type: ignore[attr-defined]
    adapter._client = client  # type: ignore[attr-defined]
    return adapter


async def _collect(gen):
    return b"".join([chunk async for chunk in gen])


async def _aiter(chunks):
    for c in chunks:
        yield c


async def test_put_uploads_full_body_and_returns_metadata():
    client = MagicMock()
    adapter = _make_adapter(client)

    obj = await adapter.put("chat/2026-04/abcd.png", _aiter([b"hello", b" world"]), "image/png")

    assert obj.key == "chat/2026-04/abcd.png"
    assert obj.size == 11
    assert obj.mime == "image/png"
    client.put_object.assert_called_once()
    kwargs = client.put_object.call_args.kwargs
    assert kwargs["Bucket"] == "test-bucket"
    assert kwargs["Key"] == "chat/2026-04/abcd.png"
    assert kwargs["ContentType"] == "image/png"
    # Body is the buffered BytesIO; read it back to verify bytes.
    assert kwargs["Body"].getvalue() == b"hello world"


async def test_put_wraps_client_errors_as_storage_failure():
    client = MagicMock()
    client.put_object.side_effect = RuntimeError("boom")
    adapter = _make_adapter(client)

    with pytest.raises(StorageFailure, match="boom"):
        await adapter.put("k", _aiter([b"x"]), "text/plain")


async def test_open_streams_chunks():
    client = MagicMock()
    body = MagicMock()
    body.read.side_effect = [b"abc", b"def", b""]
    client.get_object.return_value = {"Body": body}
    adapter = _make_adapter(client)

    data = await _collect(adapter.open("k"))

    assert data == b"abcdef"
    body.close.assert_called_once()


async def test_open_missing_key_raises_not_found():
    class _ClientError(Exception):
        def __init__(self):
            super().__init__("nope")
            self.response = {"Error": {"Code": "NoSuchKey"}}

    client = MagicMock()
    client.get_object.side_effect = _ClientError()
    adapter = _make_adapter(client)

    with pytest.raises(NotFound):
        await _collect(adapter.open("missing"))


async def test_delete_is_idempotent_on_missing():
    class _ClientError(Exception):
        def __init__(self):
            super().__init__("nope")
            self.response = {"Error": {"Code": "NoSuchKey"}}

    client = MagicMock()
    client.delete_object.side_effect = _ClientError()
    adapter = _make_adapter(client)

    # Should not raise.
    await adapter.delete("missing")


async def test_exists_true_and_false():
    client = MagicMock()
    client.head_object.return_value = {}
    adapter = _make_adapter(client)
    assert await adapter.exists("k") is True

    client.head_object.side_effect = RuntimeError("404")
    assert await adapter.exists("k") is False


async def test_presigned_get_returns_url():
    client = MagicMock()
    client.generate_presigned_url.return_value = "https://s3.example.com/signed"
    adapter = _make_adapter(client)

    url = await adapter.presigned_get("k", 300)

    assert url == "https://s3.example.com/signed"
    client.generate_presigned_url.assert_called_once_with(
        "get_object",
        Params={"Bucket": "test-bucket", "Key": "k"},
        ExpiresIn=300,
    )


async def test_presigned_get_swallows_errors():
    client = MagicMock()
    client.generate_presigned_url.side_effect = RuntimeError("boom")
    adapter = _make_adapter(client)

    assert await adapter.presigned_get("k", 300) is None


def test_local_path_returns_none():
    adapter = _make_adapter(MagicMock())
    assert adapter.local_path("any") is None
