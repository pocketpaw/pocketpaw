"""Tests for :meth:`EEUploadService._assert_can_read` — the gate used by
``stream`` and ``presigned_get`` (and therefore the /download-url + /grant
router endpoints).

The gate must allow:

1. the file owner,
2. a non-owner who is a member of the file's chat, and
3. a non-owner workspace admin/owner,

while still raising :class:`NotFound` for everyone else (existence-hiding).
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi import UploadFile

from pocketpaw.uploads.adapter import StorageAdapter, StoredObject
from pocketpaw.uploads.config import UploadSettings
from pocketpaw.uploads.errors import NotFound

pytestmark = pytest.mark.asyncio

PNG = b"\x89PNG\r\n\x1a\n" + b"rest"


class _MemAdapter(StorageAdapter):
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    async def put(self, key, stream, mime):
        buf = b""
        async for c in stream:
            buf += c
        self.blobs[key] = buf
        return StoredObject(key=key, size=len(buf), mime=mime)

    async def open(self, key):
        if key not in self.blobs:
            raise NotFound()
        yield self.blobs[key]

    async def delete(self, key):
        self.blobs.pop(key, None)

    async def exists(self, key):
        return key in self.blobs


def _upload(content: bytes, filename: str, mime: str) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(content),
        filename=filename,
        headers={"content-type": mime},  # type: ignore[arg-type]
    )


def _make_checkers(*, chat_members: set[str], admins: set[str]):
    async def is_chat_member(chat_id: str, user_id: str, workspace: str) -> bool:
        return user_id in chat_members

    async def is_workspace_admin(user_id: str, workspace: str) -> bool:
        return user_id in admins

    return is_chat_member, is_workspace_admin


@pytest.mark.parametrize(
    ("requester", "chat_members", "admins", "expect_ok"),
    [
        ("owner", set(), set(), True),             # 1. owner always allowed
        ("peer", {"peer"}, set(), True),           # 2. chat member
        ("boss", set(), {"boss"}, True),           # 3. workspace admin
        ("stranger", set(), set(), False),         # 4. none -> NotFound
    ],
    ids=["owner", "chat_member", "workspace_admin", "stranger_denied"],
)
async def test_read_gate_allows_owner_member_admin_denies_others(
    store,
    tmp_path: Path,
    requester: str,
    chat_members: set[str],
    admins: set[str],
    expect_ok: bool,
):
    from ee.cloud.uploads.service import EEUploadService

    is_chat_member, is_workspace_admin = _make_checkers(
        chat_members=chat_members, admins=admins
    )
    svc = EEUploadService(
        adapter=_MemAdapter(),
        meta=store,
        cfg=UploadSettings(local_root=tmp_path),
        is_chat_member=is_chat_member,
        is_workspace_admin=is_workspace_admin,
    )
    rec = await svc.upload(
        _upload(PNG, "cat.png", "image/png"),
        owner_id="owner",
        chat_id="c1",
        workspace="w1",
    )

    if expect_ok:
        got, it = await svc.stream(rec.id, requester_id=requester, workspace="w1")
        assert got.id == rec.id
        # Drain the iterator so the adapter context closes cleanly.
        _ = [c async for c in it]
        got2, _url = await svc.presigned_get(
            rec.id, requester_id=requester, workspace="w1", ttl_seconds=60
        )
        assert got2.id == rec.id
    else:
        with pytest.raises(NotFound):
            await svc.stream(rec.id, requester_id=requester, workspace="w1")
        with pytest.raises(NotFound):
            await svc.presigned_get(
                rec.id, requester_id=requester, workspace="w1", ttl_seconds=60
            )


async def test_read_gate_without_checkers_is_owner_only(store, tmp_path: Path):
    """When no collaborator checkers are wired, behaviour is unchanged —
    only the owner can read. Preserves the pre-fix contract that existing
    tests (and any callers that don't wire checkers) depend on."""
    from ee.cloud.uploads.service import EEUploadService

    svc = EEUploadService(
        adapter=_MemAdapter(),
        meta=store,
        cfg=UploadSettings(local_root=tmp_path),
    )
    rec = await svc.upload(
        _upload(PNG, "cat.png", "image/png"),
        owner_id="owner",
        chat_id="c1",
        workspace="w1",
    )
    with pytest.raises(NotFound):
        await svc.stream(rec.id, requester_id="peer", workspace="w1")
    with pytest.raises(NotFound):
        await svc.presigned_get(
            rec.id, requester_id="peer", workspace="w1", ttl_seconds=60
        )


async def test_chat_member_branch_skipped_when_no_chat_id(store, tmp_path: Path):
    """A file with no ``chat_id`` (e.g. avatar / KB upload) must not fall
    through the chat-member branch — otherwise a shared chat would leak
    access to private uploads. Only owner/admin should pass."""
    from ee.cloud.uploads.service import EEUploadService

    # Chat-member check would return True, but chat_id is None so the
    # branch must be skipped entirely.
    async def always_member(chat_id: str, user_id: str, workspace: str) -> bool:
        return True

    async def never_admin(user_id: str, workspace: str) -> bool:
        return False

    svc = EEUploadService(
        adapter=_MemAdapter(),
        meta=store,
        cfg=UploadSettings(local_root=tmp_path),
        is_chat_member=always_member,
        is_workspace_admin=never_admin,
    )
    rec = await svc.upload(
        _upload(PNG, "avatar.png", "image/png"),
        owner_id="owner",
        chat_id=None,
        workspace="w1",
    )
    with pytest.raises(NotFound):
        await svc.stream(rec.id, requester_id="peer", workspace="w1")
