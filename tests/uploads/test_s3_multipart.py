"""S3StorageAdapter multipart surface — the presigned (cloud) path.

Created 2026-09-14 (feat/uploads-multipart-adapter). New file.

Mocks the boto3 client directly, matching ``test_s3_adapter.py`` and
``test_s3_public_mode.py`` — the adapter's job here is translating the Protocol
into the right boto calls, and a mock asserts the kwargs far more precisely than
a fake S3 would.

The failures worth catching are all quiet. A ``ContentType`` omitted from
``create_multipart_upload`` cannot be fixed at complete time, so the finished
object is served as ``application/octet-stream`` forever. An unsorted ``Parts``
list is rejected by S3 only after every byte has moved. And an ``abort`` that
raises on an already-gone upload turns a cancel into a second failure.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("boto3")

from pocketpaw.uploads.errors import InvalidPart, NotFound, StorageFailure  # noqa: E402
from pocketpaw.uploads.s3 import S3StorageAdapter  # noqa: E402


def _make_adapter(client: MagicMock, *, public_read: bool = False) -> S3StorageAdapter:
    adapter = S3StorageAdapter.__new__(S3StorageAdapter)
    adapter._bucket = "test-bucket"  # type: ignore[attr-defined]
    adapter._client = client  # type: ignore[attr-defined]
    adapter._public_base_url = None  # type: ignore[attr-defined]
    adapter._public_read = public_read  # type: ignore[attr-defined]
    return adapter


class _ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class TestCreateMultipart:
    async def test_sends_key_and_content_type(self):
        client = MagicMock()
        client.create_multipart_upload.return_value = {"UploadId": "s3-upload-1"}
        adapter = _make_adapter(client)

        upload_id = await adapter.create_multipart("ws/1/raw.mov", "video/quicktime")

        assert upload_id == "s3-upload-1"
        kwargs = client.create_multipart_upload.call_args.kwargs
        assert kwargs["Bucket"] == "test-bucket"
        assert kwargs["Key"] == "ws/1/raw.mov"
        # Unfixable later: complete_multipart_upload ignores ContentType.
        assert kwargs["ContentType"] == "video/quicktime"

    async def test_private_bucket_sends_no_acl(self):
        client = MagicMock()
        client.create_multipart_upload.return_value = {"UploadId": "u"}
        adapter = _make_adapter(client)

        await adapter.create_multipart("k", "image/png")

        assert "ACL" not in client.create_multipart_upload.call_args.kwargs

    async def test_public_rail_sends_the_acl_and_cache_control(self):
        client = MagicMock()
        client.create_multipart_upload.return_value = {"UploadId": "u"}
        adapter = _make_adapter(client, public_read=True)

        await adapter.create_multipart("k", "image/png")

        kwargs = client.create_multipart_upload.call_args.kwargs
        assert kwargs["ACL"] == "public-read"
        assert "immutable" in kwargs["CacheControl"]

    async def test_missing_upload_id_is_a_failure(self):
        client = MagicMock()
        client.create_multipart_upload.return_value = {}
        adapter = _make_adapter(client)

        with pytest.raises(StorageFailure, match="no UploadId"):
            await adapter.create_multipart("k", "image/png")

    async def test_client_errors_are_wrapped(self):
        client = MagicMock()
        client.create_multipart_upload.side_effect = RuntimeError("boom")
        adapter = _make_adapter(client)

        with pytest.raises(StorageFailure, match="boom"):
            await adapter.create_multipart("k", "image/png")


class TestSignPart:
    def test_supports_presigned_parts(self):
        assert _make_adapter(MagicMock()).supports_presigned_parts() is True

    async def test_signs_upload_part_with_the_right_params(self):
        client = MagicMock()
        client.generate_presigned_url.return_value = "https://s3.example.com/part"
        adapter = _make_adapter(client)

        url = await adapter.sign_part("k", "u-1", 3, 900)

        assert url == "https://s3.example.com/part"
        client.generate_presigned_url.assert_called_once_with(
            "upload_part",
            Params={
                "Bucket": "test-bucket",
                "Key": "k",
                "UploadId": "u-1",
                "PartNumber": 3,
            },
            ExpiresIn=900,
        )

    async def test_swallows_errors_like_presigned_get(self):
        client = MagicMock()
        client.generate_presigned_url.side_effect = RuntimeError("boom")
        adapter = _make_adapter(client)

        assert await adapter.sign_part("k", "u", 1, 300) is None

    async def test_rejects_a_bad_part_number(self):
        adapter = _make_adapter(MagicMock())
        with pytest.raises(InvalidPart):
            await adapter.sign_part("k", "u", 0, 300)


class TestPutPart:
    async def test_uploads_and_returns_the_etag(self):
        client = MagicMock()
        client.upload_part.return_value = {"ETag": '"abc123"'}
        adapter = _make_adapter(client)

        etag = await adapter.put_part("k", "u-1", 2, b"payload")

        assert etag == '"abc123"'
        kwargs = client.upload_part.call_args.kwargs
        assert kwargs["Bucket"] == "test-bucket"
        assert kwargs["Key"] == "k"
        assert kwargs["UploadId"] == "u-1"
        assert kwargs["PartNumber"] == 2
        assert kwargs["Body"] == b"payload"

    async def test_missing_etag_is_a_failure(self):
        client = MagicMock()
        client.upload_part.return_value = {}
        adapter = _make_adapter(client)

        with pytest.raises(StorageFailure, match="no ETag"):
            await adapter.put_part("k", "u", 1, b"x")

    async def test_client_errors_are_wrapped(self):
        client = MagicMock()
        client.upload_part.side_effect = RuntimeError("boom")
        adapter = _make_adapter(client)

        with pytest.raises(StorageFailure, match="boom"):
            await adapter.put_part("k", "u", 1, b"x")

    async def test_rejects_a_bad_part_number_before_calling_boto(self):
        client = MagicMock()
        adapter = _make_adapter(client)

        with pytest.raises(InvalidPart):
            await adapter.put_part("k", "u", 10_001, b"x")

        client.upload_part.assert_not_called()


class TestCompleteMultipart:
    def _client(self, *, size: int = 2048, mime: str = "video/quicktime") -> MagicMock:
        client = MagicMock()
        client.complete_multipart_upload.return_value = {}
        client.head_object.return_value = {"ContentLength": size, "ContentType": mime}
        return client

    async def test_sorts_parts_by_number(self):
        """S3 rejects an unordered Parts list — after every byte has moved."""
        client = self._client()
        adapter = _make_adapter(client)

        await adapter.complete_multipart("k", "u-1", [(3, '"c"'), (1, '"a"'), (2, '"b"')])

        parts = client.complete_multipart_upload.call_args.kwargs["MultipartUpload"]["Parts"]
        assert parts == [
            {"PartNumber": 1, "ETag": '"a"'},
            {"PartNumber": 2, "ETag": '"b"'},
            {"PartNumber": 3, "ETag": '"c"'},
        ]

    async def test_sends_key_and_upload_id(self):
        client = self._client()
        adapter = _make_adapter(client)

        await adapter.complete_multipart("ws/1/raw.mov", "u-1", [(1, '"a"')])

        kwargs = client.complete_multipart_upload.call_args.kwargs
        assert kwargs["Bucket"] == "test-bucket"
        assert kwargs["Key"] == "ws/1/raw.mov"
        assert kwargs["UploadId"] == "u-1"

    async def test_size_and_mime_come_from_head_object(self):
        """complete_multipart_upload returns neither, so a HEAD is the only
        honest source for the StoredObject."""
        client = self._client(size=2_147_483_648, mime="video/quicktime")
        adapter = _make_adapter(client)

        obj = await adapter.complete_multipart("k", "u", [(1, '"a"')])

        assert obj.key == "k"
        assert obj.size == 2_147_483_648
        assert obj.mime == "video/quicktime"
        client.head_object.assert_called_once_with(Bucket="test-bucket", Key="k")

    async def test_empty_parts_list_is_refused(self):
        client = self._client()
        adapter = _make_adapter(client)

        with pytest.raises(StorageFailure, match="no parts"):
            await adapter.complete_multipart("k", "u", [])

        client.complete_multipart_upload.assert_not_called()

    async def test_rejects_a_bad_part_number_before_calling_boto(self):
        client = self._client()
        adapter = _make_adapter(client)

        with pytest.raises(InvalidPart):
            await adapter.complete_multipart("k", "u", [(1, '"a"'), (0, '"b"')])

        client.complete_multipart_upload.assert_not_called()

    async def test_complete_failure_is_wrapped(self):
        client = self._client()
        client.complete_multipart_upload.side_effect = RuntimeError("boom")
        adapter = _make_adapter(client)

        with pytest.raises(StorageFailure, match="complete_multipart_upload failed"):
            await adapter.complete_multipart("k", "u", [(1, '"a"')])

    async def test_head_failure_raises_rather_than_reporting_size_zero(self):
        """An object we cannot describe must not become a metadata row claiming
        size=0 — surfacing the failure is the lesser harm."""
        client = self._client()
        client.head_object.side_effect = RuntimeError("boom")
        adapter = _make_adapter(client)

        with pytest.raises(StorageFailure, match="head_object after complete failed"):
            await adapter.complete_multipart("k", "u", [(1, '"a"')])


class TestAbortMultipart:
    async def test_aborts_with_key_and_upload_id(self):
        client = MagicMock()
        adapter = _make_adapter(client)

        await adapter.abort_multipart("k", "u-1")

        client.abort_multipart_upload.assert_called_once_with(
            Bucket="test-bucket", Key="k", UploadId="u-1"
        )

    async def test_unknown_upload_is_idempotent(self):
        """Abort runs on cancel paths; "already gone" is success, not an error."""
        client = MagicMock()
        client.abort_multipart_upload.side_effect = _ClientError("NoSuchUpload")
        adapter = _make_adapter(client)

        await adapter.abort_multipart("k", "u")  # no raise

    async def test_other_errors_are_wrapped(self):
        client = MagicMock()
        client.abort_multipart_upload.side_effect = _ClientError("AccessDenied")
        adapter = _make_adapter(client)

        with pytest.raises(StorageFailure, match="abort_multipart_upload failed"):
            await adapter.abort_multipart("k", "u")


class TestEnsureMultipartLifecycle:
    async def test_applies_the_abort_rule(self):
        client = MagicMock()
        client.get_bucket_lifecycle_configuration.side_effect = _ClientError(
            "NoSuchLifecycleConfiguration"
        )
        adapter = _make_adapter(client)

        assert await adapter.ensure_multipart_lifecycle() is True

        kwargs = client.put_bucket_lifecycle_configuration.call_args.kwargs
        assert kwargs["Bucket"] == "test-bucket"
        rules = kwargs["LifecycleConfiguration"]["Rules"]
        assert len(rules) == 1
        assert rules[0]["Status"] == "Enabled"
        assert rules[0]["AbortIncompleteMultipartUpload"] == {"DaysAfterInitiation": 7}
        # A Filter is mandatory on the v2 lifecycle API.
        assert rules[0]["Filter"] == {"Prefix": ""}

    async def test_days_override(self):
        client = MagicMock()
        client.get_bucket_lifecycle_configuration.return_value = {"Rules": []}
        adapter = _make_adapter(client)

        await adapter.ensure_multipart_lifecycle(days=2)

        rules = client.put_bucket_lifecycle_configuration.call_args.kwargs[
            "LifecycleConfiguration"
        ]["Rules"]
        assert rules[0]["AbortIncompleteMultipartUpload"] == {"DaysAfterInitiation": 2}

    async def test_preserves_another_services_rules(self):
        """put_bucket_lifecycle_configuration REPLACES the set — dropping a
        shared bucket's other rules would silently disable their cleanup."""
        other = {"ID": "someone-elses", "Status": "Enabled", "Expiration": {"Days": 30}}
        client = MagicMock()
        client.get_bucket_lifecycle_configuration.return_value = {"Rules": [other]}
        adapter = _make_adapter(client)

        await adapter.ensure_multipart_lifecycle()

        rules = client.put_bucket_lifecycle_configuration.call_args.kwargs[
            "LifecycleConfiguration"
        ]["Rules"]
        assert other in rules
        assert len(rules) == 2

    async def test_rerun_does_not_stack_duplicates(self):
        client = MagicMock()
        client.get_bucket_lifecycle_configuration.return_value = {"Rules": []}
        adapter = _make_adapter(client)

        await adapter.ensure_multipart_lifecycle()
        ours = client.put_bucket_lifecycle_configuration.call_args.kwargs["LifecycleConfiguration"][
            "Rules"
        ]
        client.get_bucket_lifecycle_configuration.return_value = {"Rules": list(ours)}

        await adapter.ensure_multipart_lifecycle()

        rules = client.put_bucket_lifecycle_configuration.call_args.kwargs[
            "LifecycleConfiguration"
        ]["Rules"]
        assert rules == ours

    async def test_changed_rule_with_our_id_is_replaced_not_duplicated(self):
        """S3 rejects a configuration carrying duplicate rule IDs."""
        stale = {
            "ID": "pocketpaw-abort-incomplete-multipart",
            "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 30},
        }
        client = MagicMock()
        client.get_bucket_lifecycle_configuration.return_value = {"Rules": [stale]}
        adapter = _make_adapter(client)

        await adapter.ensure_multipart_lifecycle(days=7)

        rules = client.put_bucket_lifecycle_configuration.call_args.kwargs[
            "LifecycleConfiguration"
        ]["Rules"]
        assert len(rules) == 1
        assert rules[0]["AbortIncompleteMultipartUpload"] == {"DaysAfterInitiation": 7}

    @pytest.mark.parametrize("code", ["AccessDenied", "NotImplemented", "MethodNotAllowed"])
    async def test_unconfigurable_bucket_returns_false_instead_of_raising(self, code: str):
        """Runs at boot against buckets we may not own, and against
        S3-compatible endpoints with no lifecycle API. Neither fails the boot."""
        client = MagicMock()
        client.get_bucket_lifecycle_configuration.return_value = {"Rules": []}
        client.put_bucket_lifecycle_configuration.side_effect = _ClientError(code)
        adapter = _make_adapter(client)

        assert await adapter.ensure_multipart_lifecycle() is False

    async def test_unreadable_lifecycle_skips_rather_than_wiping(self):
        """If the current rules can't be read, writing ours would replace a set
        we never saw — skip instead."""
        client = MagicMock()
        client.get_bucket_lifecycle_configuration.side_effect = _ClientError("AccessDenied")
        adapter = _make_adapter(client)

        assert await adapter.ensure_multipart_lifecycle() is False

        client.put_bucket_lifecycle_configuration.assert_not_called()

    async def test_unexpected_write_errors_still_raise(self):
        client = MagicMock()
        client.get_bucket_lifecycle_configuration.return_value = {"Rules": []}
        client.put_bucket_lifecycle_configuration.side_effect = RuntimeError("network")
        adapter = _make_adapter(client)

        with pytest.raises(StorageFailure, match="put_bucket_lifecycle_configuration failed"):
            await adapter.ensure_multipart_lifecycle()

    async def test_explicit_preserve_rules_skips_the_read(self):
        client = MagicMock()
        adapter = _make_adapter(client)

        await adapter.ensure_multipart_lifecycle(preserve_rules=[])

        client.get_bucket_lifecycle_configuration.assert_not_called()


class TestGetLifecycle:
    async def test_returns_rules(self):
        client = MagicMock()
        client.get_bucket_lifecycle_configuration.return_value = {"Rules": [{"ID": "a"}]}
        adapter = _make_adapter(client)

        assert await adapter.get_lifecycle() == [{"ID": "a"}]

    async def test_missing_configuration_normalizes_to_empty(self):
        client = MagicMock()
        client.get_bucket_lifecycle_configuration.side_effect = _ClientError(
            "NoSuchLifecycleConfiguration"
        )
        adapter = _make_adapter(client)

        assert await adapter.get_lifecycle() == []

    async def test_permission_errors_raise_rather_than_reading_as_empty(self):
        """An AccessDenied that read as "no rules" would let the next write
        replace the bucket's real policy with only ours."""
        client = MagicMock()
        client.get_bucket_lifecycle_configuration.side_effect = _ClientError("AccessDenied")
        adapter = _make_adapter(client)

        with pytest.raises(StorageFailure):
            await adapter.get_lifecycle()


class TestListParts:
    """The authoritative manifest (feat/uploads-multipart-endpoints).

    The endpoint layer completes from this rather than from the client's list,
    because a resumed client has no etags and on the presigned path neither do
    we. That makes pagination load-bearing: a short read here does not look like
    a bug, it looks like an unfinished upload, and it only happens on the
    largest file anyone uploads.
    """

    async def test_returns_number_and_etag_sorted(self):
        client = MagicMock()
        client.list_parts.return_value = {
            "Parts": [
                {"PartNumber": 3, "ETag": '"ccc"'},
                {"PartNumber": 1, "ETag": '"aaa"'},
                {"PartNumber": 2, "ETag": '"bbb"'},
            ],
            "IsTruncated": False,
        }
        adapter = _make_adapter(client)

        parts = await adapter.list_parts("ws/1/raw.mov", "s3-upload-1")

        assert parts == [(1, '"aaa"'), (2, '"bbb"'), (3, '"ccc"')]
        kwargs = client.list_parts.call_args.kwargs
        assert kwargs["Bucket"] == "test-bucket"
        assert kwargs["Key"] == "ws/1/raw.mov"
        assert kwargs["UploadId"] == "s3-upload-1"

    async def test_pages_until_the_listing_is_exhausted(self):
        """S3 returns at most 1000 parts a call and an upload may hold 10000.

        Mutation that breaks this: stop after the first page. The adapter then
        reports a complete 2500-part upload as holding 1000, and the endpoint
        refuses it as unfinished.
        """
        client = MagicMock()
        pages = [
            {
                "Parts": [{"PartNumber": n, "ETag": f'"e{n}"'} for n in range(1, 1001)],
                "IsTruncated": True,
                "NextPartNumberMarker": 1000,
            },
            {
                "Parts": [{"PartNumber": n, "ETag": f'"e{n}"'} for n in range(1001, 2001)],
                "IsTruncated": True,
                "NextPartNumberMarker": 2000,
            },
            {
                "Parts": [{"PartNumber": n, "ETag": f'"e{n}"'} for n in range(2001, 2501)],
                "IsTruncated": False,
            },
        ]
        client.list_parts.side_effect = pages
        adapter = _make_adapter(client)

        parts = await adapter.list_parts("ws/1/big.mov", "s3-upload-1")

        assert len(parts) == 2500
        assert parts[0] == (1, '"e1"')
        assert parts[-1] == (2500, '"e2500"')
        assert [c.kwargs["PartNumberMarker"] for c in client.list_parts.call_args_list] == [
            0,
            1000,
            2000,
        ]

    async def test_a_truncated_page_that_does_not_advance_stops(self):
        """A marker that fails to move would spin forever. Trust the loop, not
        the data."""
        client = MagicMock()
        client.list_parts.return_value = {
            "Parts": [{"PartNumber": 1, "ETag": '"a"'}],
            "IsTruncated": True,
            "NextPartNumberMarker": 0,
        }
        adapter = _make_adapter(client)

        parts = await adapter.list_parts("ws/1/raw.mov", "s3-upload-1")

        assert parts == [(1, '"a"')]

    async def test_an_unknown_upload_raises_not_found(self):
        """Distinct from "the upload exists and is empty" — the caller has to be
        able to tell an expired/aborted upload from an unfinished one."""
        client = MagicMock()
        client.list_parts.side_effect = _ClientError("NoSuchUpload")
        adapter = _make_adapter(client)

        with pytest.raises(NotFound):
            await adapter.list_parts("ws/1/raw.mov", "gone")

    async def test_other_errors_are_storage_failures(self):
        client = MagicMock()
        client.list_parts.side_effect = _ClientError("AccessDenied")
        adapter = _make_adapter(client)

        with pytest.raises(StorageFailure):
            await adapter.list_parts("ws/1/raw.mov", "s3-upload-1")

    async def test_entries_missing_a_number_or_etag_are_skipped(self):
        """A malformed entry must not become a `(None, ...)` in the manifest."""
        client = MagicMock()
        client.list_parts.return_value = {
            "Parts": [
                {"PartNumber": 1, "ETag": '"a"'},
                {"PartNumber": 2},
                {"ETag": '"c"'},
            ],
            "IsTruncated": False,
        }
        adapter = _make_adapter(client)

        assert await adapter.list_parts("ws/1/raw.mov", "s3-upload-1") == [(1, '"a"')]
