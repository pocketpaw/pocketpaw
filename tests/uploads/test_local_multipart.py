"""LocalStorageAdapter multipart relay — the desktop/dev path.

Created 2026-09-14 (feat/uploads-multipart-adapter). New file.

Two failures these exist to catch. The first is silent corruption: parts arrive
from parallel workers in whatever order they finish, so a complete that trusts
list order rather than part number produces a file that is the right SIZE and
the wrong BYTES — nothing downstream notices until a user opens it. Every
round-trip here asserts byte-identity, and one shuffles the parts.

The second is path traversal. A part number becomes a filename on this path, so
``../../`` reaching the filesystem would let a caller write outside the storage
root. Rejection is asserted, and so is the absence of any file afterwards.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from pocketpaw.uploads.errors import InvalidPart, NotFound
from pocketpaw.uploads.local import LocalStorageAdapter

MIB = 1024 * 1024


async def _one_chunk(body: bytes):
    yield body


def _parts(body: bytes, part_size: int) -> list[bytes]:
    return [body[i : i + part_size] for i in range(0, len(body), part_size)] or [b""]


async def _upload(
    adapter: LocalStorageAdapter,
    key: str,
    body: bytes,
    mime: str = "application/octet-stream",
    *,
    part_size: int = 1024,
    order: list[int] | None = None,
) -> tuple[str, list[tuple[int, str]]]:
    """init → put every part → return (upload_id, parts) ready for complete."""
    upload_id = await adapter.create_multipart(key, mime)
    chunks = _parts(body, part_size)
    numbers = order or list(range(1, len(chunks) + 1))
    collected: list[tuple[int, str]] = []
    for number in numbers:
        etag = await adapter.put_part(key, upload_id, number, chunks[number - 1])
        collected.append((number, etag))
    return upload_id, collected


class TestRoundTrip:
    async def test_completes_byte_identical(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        body = bytes(range(256)) * 40  # 10 KiB, every byte value

        upload_id, parts = await _upload(adapter, "big/file.bin", body)
        obj = await adapter.complete_multipart("big/file.bin", upload_id, parts)

        assert obj.size == len(body)
        assert (tmp_upload_root / "big" / "file.bin").read_bytes() == body

    async def test_out_of_order_parts_assemble_correctly(self, tmp_upload_root: Path):
        """The bug this catches produces a right-sized, wrong-ordered file."""
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        body = b"".join(bytes([i]) * 1024 for i in range(10))

        upload_id, parts = await _upload(
            adapter, "k/shuffled.bin", body, order=[7, 2, 10, 1, 5, 9, 3, 8, 4, 6]
        )
        # Complete is also handed the list in the order the workers finished.
        obj = await adapter.complete_multipart("k/shuffled.bin", upload_id, parts)

        assert obj.size == len(body)
        assert (tmp_upload_root / "k" / "shuffled.bin").read_bytes() == body

    async def test_mime_survives_to_complete(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        upload_id, parts = await _upload(adapter, "k/clip.mov", b"x" * 10, "video/quicktime")

        obj = await adapter.complete_multipart("k/clip.mov", upload_id, parts)

        assert obj.mime == "video/quicktime"
        assert obj.key == "k/clip.mov"

    async def test_single_part_upload(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        upload_id, parts = await _upload(adapter, "k/small.bin", b"hello", part_size=4096)

        obj = await adapter.complete_multipart("k/small.bin", upload_id, parts)

        assert obj.size == 5
        assert (tmp_upload_root / "k" / "small.bin").read_bytes() == b"hello"

    async def test_parts_land_under_the_contract_path(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        await _upload(adapter, "k/file.bin", b"a" * 3000, part_size=1024)

        part_dir = tmp_upload_root / "k" / "file.bin.part"
        assert {p.name for p in part_dir.iterdir() if not p.name.startswith(".")} == {
            "1",
            "2",
            "3",
        }

    async def test_complete_removes_the_part_directory(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        upload_id, parts = await _upload(adapter, "k/file.bin", b"a" * 3000)

        await adapter.complete_multipart("k/file.bin", upload_id, parts)

        assert not (tmp_upload_root / "k" / "file.bin.part").exists()

    async def test_etag_is_the_content_hash_and_round_trips(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        upload_id = await adapter.create_multipart("k/f.bin", "application/octet-stream")

        etag = await adapter.put_part("k/f.bin", upload_id, 1, b"payload")

        assert etag == hashlib.md5(b"payload", usedforsecurity=False).hexdigest()
        # A re-PUT of identical bytes is verifiably the same part.
        assert await adapter.put_part("k/f.bin", upload_id, 1, b"payload") == etag

    async def test_completed_object_reads_back_through_open(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        body = b"stream me back" * 500
        upload_id, parts = await _upload(adapter, "k/f.bin", body)
        await adapter.complete_multipart("k/f.bin", upload_id, parts)

        read = b"".join([c async for c in adapter.open("k/f.bin")])

        assert read == body
        assert await adapter.exists("k/f.bin") is True


class TestAbort:
    async def test_abort_removes_the_parts(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        upload_id, _ = await _upload(adapter, "k/file.bin", b"a" * 3000)
        assert (tmp_upload_root / "k" / "file.bin.part").exists()

        await adapter.abort_multipart("k/file.bin", upload_id)

        assert not (tmp_upload_root / "k" / "file.bin.part").exists()
        assert not (tmp_upload_root / "k" / "file.bin").exists()

    async def test_abort_is_idempotent(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        upload_id, _ = await _upload(adapter, "k/file.bin", b"a" * 100)

        await adapter.abort_multipart("k/file.bin", upload_id)
        await adapter.abort_multipart("k/file.bin", upload_id)  # no error

    async def test_abort_of_an_unknown_upload_is_a_no_op(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        await adapter.abort_multipart("k/never-started.bin", "nope")

    async def test_abort_with_a_stale_id_leaves_the_live_session_alone(self, tmp_upload_root: Path):
        """A late cancel for a re-created session must not delete its parts."""
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        stale_id, _ = await _upload(adapter, "k/file.bin", b"a" * 100)
        await adapter.abort_multipart("k/file.bin", stale_id)
        live_id, parts = await _upload(adapter, "k/file.bin", b"b" * 100)

        await adapter.abort_multipart("k/file.bin", stale_id)

        obj = await adapter.complete_multipart("k/file.bin", live_id, parts)
        assert obj.size == 100
        assert (tmp_upload_root / "k" / "file.bin").read_bytes() == b"b" * 100

    async def test_completing_after_abort_raises(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        upload_id, parts = await _upload(adapter, "k/file.bin", b"a" * 100)
        await adapter.abort_multipart("k/file.bin", upload_id)

        with pytest.raises(NotFound):
            await adapter.complete_multipart("k/file.bin", upload_id, parts)


class TestPartNumberValidation:
    @pytest.mark.parametrize("number", [0, -1, 10_001])
    async def test_out_of_range_is_rejected(self, tmp_upload_root: Path, number: int):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        upload_id = await adapter.create_multipart("k/f.bin", "application/octet-stream")

        with pytest.raises(InvalidPart):
            await adapter.put_part("k/f.bin", upload_id, number, b"x")

    async def test_traversal_string_never_reaches_the_filesystem(self, tmp_upload_root: Path):
        """A string part number is refused before it can become a path segment."""
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        upload_id = await adapter.create_multipart("k/f.bin", "application/octet-stream")

        with pytest.raises(InvalidPart):
            await adapter.put_part("k/f.bin", upload_id, "../../escaped", b"pwned")  # type: ignore[arg-type]

        assert not (tmp_upload_root.parent / "escaped").exists()
        assert list((tmp_upload_root / "k" / "f.bin.part").glob("*")) == [
            tmp_upload_root / "k" / "f.bin.part" / ".meta.json"
        ]

    async def test_validation_runs_before_any_part_is_written(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        upload_id = await adapter.create_multipart("k/f.bin", "application/octet-stream")

        with pytest.raises(InvalidPart):
            await adapter.put_part("k/f.bin", upload_id, 0, b"x")

        part_dir = tmp_upload_root / "k" / "f.bin.part"
        assert [p.name for p in part_dir.iterdir() if not p.name.startswith(".")] == []

    async def test_complete_rejects_a_bad_part_number(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        upload_id, parts = await _upload(adapter, "k/f.bin", b"x" * 10)

        with pytest.raises(InvalidPart):
            await adapter.complete_multipart("k/f.bin", upload_id, [*parts, (0, "etag")])

    async def test_create_rejects_a_traversal_key(self, tmp_upload_root: Path):
        from pocketpaw.uploads.errors import AccessDenied

        adapter = LocalStorageAdapter(root=tmp_upload_root)
        with pytest.raises(AccessDenied):
            await adapter.create_multipart("../../evil.bin", "application/octet-stream")


class TestSessionIntegrity:
    async def test_put_part_with_a_wrong_upload_id_is_refused(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        await adapter.create_multipart("k/f.bin", "application/octet-stream")

        with pytest.raises(NotFound):
            await adapter.put_part("k/f.bin", "some-other-id", 1, b"x")

    async def test_put_part_without_an_init_is_refused(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        with pytest.raises(NotFound):
            await adapter.put_part("k/never.bin", "made-up", 1, b"x")

    async def test_complete_with_a_wrong_upload_id_is_refused(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        _, parts = await _upload(adapter, "k/f.bin", b"x" * 10)

        with pytest.raises(NotFound):
            await adapter.complete_multipart("k/f.bin", "wrong", parts)

    async def test_complete_with_a_missing_part_raises_and_writes_nothing(
        self, tmp_upload_root: Path
    ):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        upload_id, parts = await _upload(adapter, "k/f.bin", b"x" * 3000, part_size=1024)

        with pytest.raises(NotFound):
            await adapter.complete_multipart("k/f.bin", upload_id, [*parts, (4, "never-sent")])

        assert not (tmp_upload_root / "k" / "f.bin").exists()
        assert not (tmp_upload_root / "k" / "f.bin.tmp").exists()

    async def test_complete_with_no_parts_raises(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        upload_id = await adapter.create_multipart("k/f.bin", "application/octet-stream")

        with pytest.raises(NotFound):
            await adapter.complete_multipart("k/f.bin", upload_id, [])

    async def test_two_keys_do_not_share_a_part_directory(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        id_a, parts_a = await _upload(adapter, "k/a.bin", b"a" * 2000, part_size=1024)
        id_b, parts_b = await _upload(adapter, "k/b.bin", b"b" * 2000, part_size=1024)

        obj_a = await adapter.complete_multipart("k/a.bin", id_a, parts_a)
        obj_b = await adapter.complete_multipart("k/b.bin", id_b, parts_b)

        assert obj_a.size == obj_b.size == 2000
        assert (tmp_upload_root / "k" / "a.bin").read_bytes() == b"a" * 2000
        assert (tmp_upload_root / "k" / "b.bin").read_bytes() == b"b" * 2000


class TestListingsHideScratch:
    async def test_in_progress_parts_are_not_browsable(self, tmp_upload_root: Path):
        """A user browsing mid-upload must not see a `raw.mov.part` folder."""
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        await _upload(adapter, "k/raw.mov", b"a" * 3000)

        names = [item.name for item in await adapter.browse("k")]

        assert names == []
        assert await adapter.list_prefix("k") == []

    async def test_the_completed_file_is_browsable(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        upload_id, parts = await _upload(adapter, "k/raw.mov", b"a" * 3000)
        await adapter.complete_multipart("k/raw.mov", upload_id, parts)

        items = await adapter.browse("k")

        assert [(i.name, i.is_dir, i.size) for i in items] == [("raw.mov", False, 3000)]

    async def test_a_real_file_named_dot_part_still_lists(self, tmp_upload_root: Path):
        """Only directories are hidden — a file a user actually named `.part` is theirs."""
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        await adapter.put("k/notes.part", _one_chunk(b"mine"), "text/plain")

        assert [i.name for i in await adapter.browse("k")] == ["notes.part"]


class TestRelayMode:
    def test_cannot_presign(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        assert adapter.supports_presigned_parts() is False

    async def test_sign_part_returns_none(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        upload_id = await adapter.create_multipart("k/f.bin", "application/octet-stream")

        assert await adapter.sign_part("k/f.bin", upload_id, 1, 3600) is None
