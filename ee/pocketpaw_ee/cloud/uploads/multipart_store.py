"""Mongo store for multipart upload sessions — and the tenant boundary.

Created 2026-09-14 (feat/uploads-multipart-endpoints).

**Every read here takes ``workspace`` as a required, keyword-only argument.**
An upload session is a tenant boundary in the same way a ``FileUpload`` row is:
it names a storage key, a provider upload id and an owner, and a session read
without the filter would let one workspace append parts to — and then complete —
another workspace's upload. There is deliberately no ``get(upload_id)``
convenience overload for a caller to reach for by mistake; the parameter is
keyword-only so it cannot be dropped positionally, and ``ee/cloud`` rule 7
(tenant filter on every read) is satisfied structurally rather than by review.

A session belonging to another workspace reads back as ``None``, which the
service turns into a 404. Never a 403: a 403 confirms the id exists, which is
itself a cross-tenant disclosure, and the caller has no business knowing the
difference.

Sibling of ``mongo_store.py`` (file metadata) and ``share_store.py`` (public
share links) — the established shape in this package is one store module per
document, so this follows it rather than growing ``mongo_store``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pocketpaw_ee.cloud.uploads.multipart_models import (
    MODE_PRESIGNED,
    STATE_ABORTED,
    STATE_COMPLETED,
    STATE_OPEN,
    MultipartPart,
    MultipartUpload,
)


class MultipartSessionStore:
    """CRUD for ``MultipartUpload``, workspace-scoped on every read."""

    async def create(
        self,
        *,
        upload_id: str,
        workspace: str,
        owner: str,
        storage_key: str,
        provider_upload_id: str,
        filename: str,
        mime: str,
        size: int,
        part_size: int,
        part_count: int,
        mode: str = MODE_PRESIGNED,
        chat_id: str | None = None,
        pocket_id: str | None = None,
        folder_path: str = "/",
        ttl_hours: int,
        budget_files: int = 0,
        budget_bytes: int = 0,
        budget_day: str = "",
    ) -> MultipartUpload:
        doc = MultipartUpload(
            upload_id=upload_id,
            workspace=workspace,
            owner=owner,
            storage_key=storage_key,
            provider_upload_id=provider_upload_id,
            filename=filename,
            mime=mime,
            size=size,
            part_size=part_size,
            part_count=part_count,
            mode=mode,
            chat_id=chat_id,
            pocket_id=pocket_id,
            folder_path=folder_path or "/",
            budget_files=budget_files,
            budget_bytes=budget_bytes,
            budget_day=budget_day,
            expires_at=datetime.now(UTC) + timedelta(hours=ttl_hours),
        )
        await doc.insert()
        return doc

    async def get(self, upload_id: str, *, workspace: str) -> MultipartUpload | None:
        """One session, or ``None``.

        ``workspace`` is keyword-only and required so this cannot be called
        without the tenant filter — see the module docstring. A session in
        another workspace is indistinguishable from a session that never
        existed.
        """
        return await MultipartUpload.find_one(
            MultipartUpload.upload_id == upload_id,
            MultipartUpload.workspace == workspace,
        )

    async def record_part(
        self,
        doc: MultipartUpload,
        *,
        part_number: int,
        etag: str,
        size: int | None = None,
    ) -> MultipartUpload:
        """Record a stored part, REPLACING any prior record of that number.

        A client that retries a part after a timeout uploads it again and gets
        a new etag; keeping both would send storage two entries for one part
        number at complete time. The replacement is by number, so a retry is
        idempotent from the session's point of view even though the bytes moved
        twice.
        """
        kept = [p for p in doc.parts if p.part_number != part_number]
        kept.append(MultipartPart(part_number=part_number, etag=etag, size=size))
        kept.sort(key=lambda p: p.part_number)
        doc.parts = kept
        await doc.save()
        return doc

    async def mark_completed(self, doc: MultipartUpload) -> None:
        doc.state = STATE_COMPLETED
        # A completed session's claim is SPENT, not released — the bytes are in
        # the bucket and on a ``FileUpload`` row. Clearing the flag stops a
        # later expiry sweep from refunding a real upload.
        doc.budget_held = False
        await doc.save()

    async def mark_aborted(self, doc: MultipartUpload) -> None:
        doc.state = STATE_ABORTED
        await doc.save()

    async def clear_budget_hold(self, doc: MultipartUpload) -> None:
        """Mark the daily-budget claim as given back.

        Separate from ``mark_aborted`` because the two happen at different
        moments on the expiry path: the claim is released as soon as anyone
        observes the session is past its TTL, and the state flip records why.
        Calling this twice is harmless; the service only refunds when the flag
        was still set.
        """
        doc.budget_held = False
        await doc.save()

    async def list_open(self, *, workspace: str, owner: str) -> list[MultipartUpload]:
        """Open sessions for one owner — the resume listing."""
        return await MultipartUpload.find(
            MultipartUpload.workspace == workspace,
            MultipartUpload.owner == owner,
            MultipartUpload.state == STATE_OPEN,
        ).to_list()


__all__ = ["MultipartSessionStore"]
