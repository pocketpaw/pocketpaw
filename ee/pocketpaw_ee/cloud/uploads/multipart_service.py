"""EEMultipartService — resumable uploads, from init to a ``FileUpload`` row.

Created 2026-09-14 (feat/uploads-multipart-endpoints). Implements the server
half of ``docs/design/drafts/2026-09-14-multipart-upload-contract.md``.

**What this file is really for.** ``EEUploadService.upload_many`` cannot be
reused here and it is worth being exact about why, because "call the existing
service" is the obvious move and it does not work: ``upload_many`` takes
``UploadFile`` objects and performs the write itself, whereas by the time
``complete`` runs the bytes are already in the bucket and there is no file
object to hand it. So ``complete`` REPLICATES its post-write sequence instead —
the storage-plan check, the daily-budget claim, ``save_scoped``, and the
``FileReady`` emit with the same payload keys. That replication is the contract
this module owes the rest of the product: a multipart upload has to be
indistinguishable from a simple one everywhere downstream. Miss the emit and the
file never reaches the KB indexer. Miss ``save_scoped`` and it never appears in
the library.

**Where the two ceilings are checked, and why it is both ends.**
``upload_many`` checks them AFTER the write and rolls back by deleting the blob.
Copying that here would reproduce the exact bug this sprint exists to kill:
transfer 5 GB, get refused at the end. So the storage cap and the per-file
ceiling are checked at INIT against the size the client declares — before a
single part moves — and checked AGAIN at complete against the size storage
actually reports. Both halves are load-bearing. The init check is the one users
feel; the complete check is the one that matters for correctness, because the
declared size is client-supplied and a client that under-declares to slip past
init must still be caught and its object deleted.

**The budget claim is reserved at init**, for the same reason, and released on
abort and on expiry. It is keyed to the UTC day it was claimed on and refunded
against that day — see ``upload_budget.release``.

**``complete`` is server-authoritative** (revised 2026-09-14, from PR3's
findings). The manifest comes from the storage provider's own ``list_parts``,
not from the request body. The first shape required an etag per part from the
client, which is unsatisfiable after precisely the event resume exists for: a
reloaded client has no etags for its previous session's parts, and on the
presigned path this side never saw them either. It also quietly depended on the
bucket setting ``Access-Control-Expose-Headers: ETag``, without which the
browser cannot read a presigned PUT's etag at all and every presigned upload
would have failed at the last step after succeeding part by part. Reading the
manifest from the provider removes that dependency instead of documenting it,
which is why no CORS change accompanies this. ``_resolve_parts`` has the detail.

**The head_object recovery.** ``S3StorageAdapter.complete_multipart`` HEADs the
object after completing, because ``complete_multipart_upload`` returns neither
``ContentLength`` nor ``ContentType``. That HEAD can fail on its own, which is
the nastiest state in this file: the object completed fine and cannot be
described. Surfacing it as a 500 would tell the user a successful 5 GB upload
failed, and a retry would 404 on the already-consumed upload id — the upload
would be unrecoverable despite having worked. The adapter reports it as
``ObjectNotDescribed``, a ``StorageFailure`` subclass, and this file catches
that TYPE and recovers the size and mime from the session document.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pocketpaw.uploads.adapter import StorageAdapter, StoredObject
from pocketpaw.uploads.config import (
    UploadSettings,
    extension_for,
    is_generic_mime,
    mime_allowed,
    mime_for_filename,
    normalize_mime,
    part_count_for,
    part_size_for,
    validate_part_number,
)
from pocketpaw.uploads.errors import InvalidPart, ObjectNotDescribed
from pocketpaw.uploads.errors import NotFound as UploadNotFound
from pocketpaw.uploads.file_store import FileRecord
from pocketpaw.uploads.keys import new_storage_key
from pocketpaw_ee.cloud._core.errors import (
    BadRequest,
    ConflictError,
    NotFound,
    PayloadTooLarge,
)
from pocketpaw_ee.cloud.realtime.emit import emit
from pocketpaw_ee.cloud.realtime.events import FileReady
from pocketpaw_ee.cloud.shared.time import iso_utc
from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore
from pocketpaw_ee.cloud.uploads.multipart_models import (
    MODE_PRESIGNED,
    MODE_RELAY,
    STATE_ABORTED,
    STATE_OPEN,
    MultipartUpload,
)
from pocketpaw_ee.cloud.uploads.multipart_store import MultipartSessionStore

logger = logging.getLogger(__name__)

#: Most presigned part URLs handed out in one response. The contract's number.
#: A 5 GiB file is 640 parts at the 8 MiB default, so a client gets the rest by
#: asking the status route again as it makes progress — which it has to call on
#: resume anyway, and which keeps every URL's TTL counted from when it is
#: actually about to be used rather than from init.
MAX_PARTS_PER_RESPONSE = 256

#: TTL on a presigned part URL. Long enough for one part on a bad connection —
#: 8 MiB at 100 kbit/s is ~11 minutes — and short enough that a URL leaked from
#: a client log is not a write capability for the rest of the day.
PART_URL_TTL_SECONDS = 3600


@dataclass(frozen=True)
class _Ceilings:
    """What the two checks found, so the caller decides what to raise."""

    over_file_limit: bool
    storage_exceeded: bool
    storage_limit: int | None


class EEMultipartService:
    """Resumable uploads for one workspace's storage adapter.

    Constructed with the same collaborators as :class:`EEUploadService` so the
    router can build both from its single module-level adapter/store/config
    triple, and so a test can substitute all three at once.
    """

    def __init__(
        self,
        adapter: StorageAdapter,
        meta: MongoFileStore,
        cfg: UploadSettings,
        store: MultipartSessionStore | None = None,
    ) -> None:
        self._adapter = adapter
        self._meta = meta
        self._cfg = cfg
        self._store = store or MultipartSessionStore()

    # -- init ---------------------------------------------------------------

    async def init(
        self,
        *,
        workspace: str,
        owner_id: str,
        filename: object,
        size: object,
        mime: object,
        chat_id: str | None = None,
        pocket_id: str | None = None,
        folder_path: str = "/",
    ) -> dict:
        """Open a session and return the client's instructions.

        Every refusal in here happens before a byte of the file exists on our
        side. That ordering IS the feature.

        ``filename`` / ``size`` / ``mime`` are typed ``object`` because that is
        what they are: fields off an untrusted JSON body, which the router
        hands over unparsed. Declaring them ``str``/``int`` would be a claim
        this code is not in a position to make, and would push the validation
        this function performs out to every caller.
        """
        filename = _clean_filename(filename)
        size = _validated_size(size)
        mime = _resolve_mime(mime, filename)
        if not mime_allowed(mime, self._cfg.allowed_mimes):
            raise BadRequest("multipart.invalid", f"mime not allowed: {mime}")

        # Ceiling 1 of 2, against the DECLARED size. Cheap, and the whole point
        # of the feature: a file that can never land is refused now, not after
        # the user has waited out a 5 GB transfer.
        ceilings = await self._check_ceilings(workspace=workspace, size=size)
        _raise_for_ceilings(ceilings, size=size, limit=self._cfg.max_large_file_bytes)

        # Reserve the daily claim. Also pre-transfer: a workspace that is out of
        # budget should be told before it starts, and holding the claim stops
        # ten parallel 5 GB inits from each seeing headroom that only one of
        # them can actually use.
        from pocketpaw_ee.cloud.uploads import upload_budget

        budget_day = upload_budget.today()
        allowed, over = await upload_budget.try_spend(workspace, 1, size)
        if not allowed:
            from pocketpaw_ee.cloud._core.errors import DailyUploadLimitError

            raise DailyUploadLimitError(over)

        storage_key = new_storage_key("chat", extension_for(mime, filename))
        try:
            provider_upload_id = await self._adapter.create_multipart(storage_key, mime)
        except Exception:
            # Nothing was stored, so the claim buys nothing — give it back
            # rather than charging a workspace for a session that never opened.
            await upload_budget.release(workspace, budget_day, 1, size)
            raise

        part_size = part_size_for(size, base=self._cfg.multipart_part_bytes)
        part_count = part_count_for(size, part_size)
        presigned = bool(self._adapter.supports_presigned_parts())
        mode = MODE_PRESIGNED if presigned else MODE_RELAY

        try:
            doc = await self._store.create(
                upload_id=f"mpu_{uuid.uuid4().hex}",
                workspace=workspace,
                owner=owner_id,
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
                folder_path=folder_path,
                ttl_hours=self._cfg.multipart_ttl_hours,
                budget_files=1,
                budget_bytes=size,
                budget_day=budget_day,
            )
        except Exception:
            # The provider has an open upload with no session row pointing at
            # it — abandon it now rather than leaving billed parts nobody can
            # find. (The S3 lifecycle rule from PR1 is the backstop if this
            # abort also fails.)
            with contextlib.suppress(Exception):
                await self._adapter.abort_multipart(storage_key, provider_upload_id)
            await upload_budget.release(workspace, budget_day, 1, size)
            raise

        return {
            "upload_id": doc.upload_id,
            "mode": mode,
            "key": doc.storage_key,
            "part_size": part_size,
            "part_count": part_count,
            "parts": await self._mint_part_urls(doc, missing=list(range(1, part_count + 1))),
            "expires_at": iso_utc(doc.expires_at),
        }

    # -- relay part ---------------------------------------------------------

    async def put_part(
        self,
        *,
        upload_id: str,
        workspace: str,
        part_number: int,
        declared_length: int | None,
        read_body: Callable[[], Awaitable[bytes]],
    ) -> dict:
        """Store one relayed part.

        Takes the body as a CALLABLE rather than as bytes so the session — and
        therefore this session's real ``part_size`` — is loaded before anything
        is read. The contract says an over-sized part is rejected before the
        body is read, and a signature taking ``bytes`` cannot honour that: by
        the time the service is called the caller has already buffered it.

        ``declared_length`` is the client's own ``Content-Length`` and is
        checked first because it costs nothing. The measured length is checked
        after the read as well, because a chunked request declares nothing.
        """
        doc = await self._live_session(upload_id, workspace=workspace)
        if doc.mode != MODE_RELAY:
            raise BadRequest(
                "multipart.invalid",
                "this session uploads parts directly to storage; use the signed part URLs",
            )
        number = _validated_part_number(part_number, doc.part_count)
        if declared_length is not None and declared_length > doc.part_size:
            raise PayloadTooLarge(
                "multipart.too_large",
                f"part {number} declares {declared_length} bytes, over this "
                f"session's part size of {doc.part_size}",
            )

        body = await read_body()
        if len(body) > doc.part_size:
            raise PayloadTooLarge(
                "multipart.too_large",
                f"part {number} is {len(body)} bytes, over this session's "
                f"part size of {doc.part_size}",
            )

        etag = await self._adapter.put_part(doc.storage_key, doc.provider_upload_id, number, body)
        await self._store.record_part(doc, part_number=number, etag=etag, size=len(body))
        return {"part_number": number, "etag": etag}

    # -- status / resume ----------------------------------------------------

    async def _received_parts(self, doc) -> list[int]:
        """Part numbers storage actually holds.

        ``doc.parts`` only records parts that arrived through the relay route,
        so on a presigned deploy it is always empty and a resuming client would
        re-upload the whole file. Storage is the only source that knows.
        """
        try:
            provider_parts = await self._adapter.list_parts(doc.storage_key, doc.provider_upload_id)
        except UploadNotFound as exc:
            raise NotFound("multipart", doc.upload_id) from exc
        return sorted({n for n, _ in provider_parts if 1 <= n <= doc.part_count})

    async def status(self, *, upload_id: str, workspace: str) -> dict:
        """What a client that reloaded mid-upload needs to carry on.

        Re-mints URLs only for what is MISSING. A client that has 600 of 640
        parts gets 40 URLs, not 640, and each one's TTL starts now rather than
        having expired hours ago at init.
        """
        doc = await self._live_session(upload_id, workspace=workspace)
        received = await self._received_parts(doc)
        missing = [n for n in range(1, doc.part_count + 1) if n not in set(received)]
        return {
            "upload_id": doc.upload_id,
            "mode": doc.mode,
            "key": doc.storage_key,
            "part_size": doc.part_size,
            "part_count": doc.part_count,
            "received": received,
            "parts": await self._mint_part_urls(doc, missing=missing),
            "expires_at": iso_utc(doc.expires_at),
        }

    # -- complete -----------------------------------------------------------

    async def complete(
        self,
        *,
        upload_id: str,
        workspace: str,
        parts: object,
        pocket_guard: Callable[[str | None], Awaitable[None]] | None = None,
    ) -> dict:
        """Assemble the object, then do everything ``upload_many`` does after a write."""
        doc = await self._live_session(upload_id, workspace=workspace)
        # Re-run the pocket ABAC against the session's target, here rather than
        # in the route, so the session is read once. A session lives seven days
        # and access can be revoked inside that window.
        if pocket_guard is not None:
            await pocket_guard(doc.pocket_id)

        ordered = await self._resolve_parts(doc, claimed=parts)
        obj = await self._complete_in_storage(doc, ordered)

        # Ceiling 2 of 2, against the size STORAGE reports. The declared size
        # got the client this far; this is the check that a client cannot lie
        # its way past. Anything over, and the object goes.
        from pocketpaw_ee.cloud.uploads import upload_budget

        actual = int(obj.size)
        ceilings = await self._check_ceilings(workspace=workspace, size=actual)
        if ceilings.over_file_limit or ceilings.storage_exceeded:
            await self._roll_back(doc, reason="over a ceiling at complete")
            _raise_for_ceilings(ceilings, size=actual, limit=self._cfg.max_large_file_bytes)

        # Reconcile the claim with reality. An upload that came in under its
        # declared size refunds the difference; one that came in over claims it,
        # and a workspace that cannot cover the extra loses the object — the
        # same answer ``upload_many`` gives, for the same reason.
        delta = actual - doc.budget_bytes
        if delta > 0:
            allowed, over = await upload_budget.try_spend(workspace, 0, delta)
            if not allowed:
                await self._roll_back(doc, reason="over the daily budget at complete")
                from pocketpaw_ee.cloud._core.errors import DailyUploadLimitError

                raise DailyUploadLimitError(over)
        elif delta < 0:
            await upload_budget.release(workspace, doc.budget_day, 0, -delta)

        rec = FileRecord(
            id=uuid.uuid4().hex,
            storage_key=obj.key,
            filename=doc.filename,
            mime=obj.mime or doc.mime,
            size=actual,
            owner_id=doc.owner,
            chat_id=doc.chat_id,
            created=datetime.now(UTC),
            folder_path=doc.folder_path or "/",
        )
        await self._meta.save_scoped(
            rec,
            workspace=workspace,
            folder_path=doc.folder_path or "/",
            pocket_id=doc.pocket_id,
        )

        # Same payload, same conditional keys, as ``EEUploadService.upload_many``.
        # ``group_id`` only when chat-scoped and ``pocket_id`` only when
        # pocket-scoped, because the KB listener routes on exactly those.
        data: dict = {
            "workspace_id": workspace,
            "file_id": rec.id,
            "filename": rec.filename,
            "mime": rec.mime,
            "size": rec.size,
            "storage_key": rec.storage_key,
            "url": f"/api/v1/uploads/{rec.id}",
        }
        if rec.chat_id:
            data["group_id"] = rec.chat_id
        if doc.pocket_id:
            data["pocket_id"] = doc.pocket_id
        await emit(FileReady(data=data))

        await self._store.mark_completed(doc)

        # Exactly the shape ``POST /uploads`` puts in ``uploaded[]`` so the
        # frontend normalisers work on both without a branch.
        return {
            "id": rec.id,
            "filename": rec.filename,
            "mime": rec.mime,
            "size": rec.size,
            "url": f"/api/v1/uploads/{rec.id}",
            "created": iso_utc(rec.created),
        }

    # -- abort --------------------------------------------------------------

    async def abort(self, *, upload_id: str, workspace: str) -> None:
        """Drop the upload and give back the claim. Idempotent.

        An already-aborted session succeeds silently — a client cancelling
        twice, or cancelling a session an expiry check already reaped, is not
        an error. A COMPLETED session is a 404: those bytes are a real file
        now, and ``DELETE /uploads/{file_id}`` is how you remove it.

        A retried cancel still settles the claim rather than returning early.
        ``_discard`` releases the budget and then marks the session dead, so a
        process that dies between those two steps leaves an aborted session
        still holding its bytes for the rest of the day; the retry is the only
        thing that will ever notice. ``_refund``'s ``budget_held`` flag is what
        makes the retry safe — without it, a second cancel would refund bytes
        that were already given back and mint free quota.
        """
        doc = await self._store.get(upload_id, workspace=workspace)
        if doc is None or doc.state not in (STATE_OPEN, STATE_ABORTED):
            raise NotFound("multipart", upload_id)
        if doc.state == STATE_ABORTED:
            await self._refund(doc)
            return
        await self._discard(doc)

    # -- internals ----------------------------------------------------------

    async def _live_session(self, upload_id: str, *, workspace: str) -> MultipartUpload:
        """Load an OPEN, unexpired session, or raise the contract's error.

        The workspace filter is the tenant boundary and it is not optional:
        a session in another workspace reads back ``None`` here and leaves as a
        404, never a 403 that would confirm the id exists.
        """
        doc = await self._store.get(upload_id, workspace=workspace)
        if doc is None or doc.state != STATE_OPEN:
            raise NotFound("multipart", upload_id)
        if _is_expired(doc):
            # Observing an expired session is the moment its claim is given
            # back. There is no sweeper: the counter is per UTC day and the
            # refund is charged to the day the claim was made, so a session
            # nobody ever touches again costs at most the rest of that day.
            await self._discard(doc)
            raise ConflictError(
                "multipart.expired",
                "this upload session has expired — start a new one",
            )
        return doc

    async def _discard(self, doc: MultipartUpload) -> None:
        """Abort at the provider, release the claim, mark the session dead.

        Ordered so a failure anywhere still leaves the workspace's budget
        correct: the provider abort is best-effort (PR1's lifecycle rule reaps
        what it misses), the refund runs regardless, and the state flip is last
        so a crash mid-way leaves a session that will be discarded again rather
        than one that looks finished.
        """
        with contextlib.suppress(Exception):
            await self._adapter.abort_multipart(doc.storage_key, doc.provider_upload_id)
        await self._refund(doc)
        await self._store.mark_aborted(doc)

    async def _refund(self, doc: MultipartUpload) -> None:
        """Give back the init claim, at most once per session."""
        if not doc.budget_held:
            return
        from pocketpaw_ee.cloud.uploads import upload_budget

        await upload_budget.release(
            doc.workspace, doc.budget_day, doc.budget_files, doc.budget_bytes
        )
        await self._store.clear_budget_hold(doc)

    async def _roll_back(self, doc: MultipartUpload, *, reason: str) -> None:
        """Delete a just-completed object that turned out not to be allowed.

        The mirror of ``upload_many``'s rollback, and the reason the complete-
        time ceiling check is worth having: an under-declared file gets all the
        way into the bucket, so the only way to refuse it is to remove it.
        """
        logger.warning(
            "multipart upload %s rolled back (%s) — deleting %s",
            doc.upload_id,
            reason,
            doc.storage_key,
        )
        with contextlib.suppress(Exception):
            await self._adapter.delete(doc.storage_key)
        await self._refund(doc)
        await self._store.mark_aborted(doc)

    async def _resolve_parts(
        self, doc: MultipartUpload, *, claimed: object
    ) -> list[tuple[int, str]]:
        """The manifest to complete with — from STORAGE, not from the client.

        The client's list cannot be the source of truth, and the case that
        proves it is the one resumable uploads exist for. A client that reloads
        mid-upload has no etags for the parts its previous session wrote; on the
        presigned path nobody on this side saw them either, because those PUTs
        went browser→bucket. A complete that required a client-supplied etag per
        part was therefore unsatisfiable after exactly the event resume is for.

        It has a second, quieter payoff. The browser cannot read a presigned
        PUT's ``ETag`` response header at all unless the bucket sets
        ``Access-Control-Expose-Headers: ETag``, so the old shape would have had
        every presigned upload succeed part by part and then fail at the last
        step, on a bucket setting nobody would think to check. Reading the
        manifest from the provider removes that dependency rather than
        documenting it.

        ``claimed`` stays useful as an ASSERTION. When the client does know an
        etag, a disagreement means the two sides are looking at different bytes,
        and completing anyway would assemble an object the client never
        intended. An incomplete claim is fine — that is the resume case.
        """
        try:
            provider_parts = await self._adapter.list_parts(doc.storage_key, doc.provider_upload_id)
        except UploadNotFound as exc:
            # Storage has no such upload: expired at the bucket, or already
            # completed/aborted out from under this session.
            raise NotFound("multipart", doc.upload_id) from exc

        present = {number: etag for number, etag in provider_parts}
        missing = [n for n in range(1, doc.part_count + 1) if n not in present]
        if missing:
            # Not a truncated object and not a 400: the request is well-formed,
            # the upload simply is not finished. S3 would happily assemble what
            # it has, which is how a short file lands in a library looking
            # plausible.
            shown = ", ".join(str(n) for n in missing[:10])
            suffix = "…" if len(missing) > 10 else ""
            raise ConflictError(
                "multipart.invalid",
                f"storage is missing {len(missing)} of {doc.part_count} parts "
                f"({shown}{suffix}) — upload them and complete again",
            )

        for number, etag in _validated_claim(claimed, part_count=doc.part_count).items():
            stored = present.get(number)
            if stored is not None and _norm_etag(stored) != _norm_etag(etag):
                raise ConflictError(
                    "multipart.invalid",
                    f"part {number} does not match what storage holds — "
                    "re-upload it and complete again",
                )

        extra = [n for n in present if n > doc.part_count]
        if extra:
            # Parts past the declared geometry. Dropping them silently would
            # assemble a different object than the client believes it sent.
            raise ConflictError(
                "multipart.invalid",
                f"storage holds {len(extra)} parts past this session's "
                f"{doc.part_count} — abort and start again",
            )

        return [(n, present[n]) for n in range(1, doc.part_count + 1)]

    async def _complete_in_storage(
        self, doc: MultipartUpload, ordered: list[tuple[int, str]]
    ) -> StoredObject:
        """Complete at the provider, recovering from a post-complete HEAD failure.

        ``ObjectNotDescribed`` is the ONE recoverable failure here: the object
        assembled and only the read-back of its size and type failed. Every
        other ``StorageFailure`` propagates, because papering over a genuine
        completion failure with a plausible-looking size would write a library
        row for an object that does not exist.

        Caught by TYPE. This used to match a substring of the adapter's error
        message, which put a decision worth a phantom library row at the mercy
        of anyone rewording a log line.
        """
        try:
            return await self._adapter.complete_multipart(
                doc.storage_key, doc.provider_upload_id, ordered
            )
        except ObjectNotDescribed as exc:
            size = _size_from_session(doc)
            logger.warning(
                "multipart upload %s completed but could not be described (%s); "
                "recovering size=%d mime=%s from the session",
                doc.upload_id,
                exc,
                size,
                doc.mime,
            )
            return StoredObject(key=doc.storage_key, size=size, mime=doc.mime)

    async def _check_ceilings(self, *, workspace: str, size: int) -> _Ceilings:
        """Both ceilings, evaluated together so the caller can order the errors.

        Plan cap before daily budget, and file ceiling before both, matching
        ``upload_many``: a billed workspace should get the 402 that names an
        upgrade path rather than a 429 that says "wait", and a file that is
        simply too big should hear that instead of either.
        """
        over_file = size > self._cfg.max_large_file_bytes
        from pocketpaw_ee.cloud.storage import service as storage_service

        exceeded, _used, limit = await storage_service.storage_cap_exceeded(workspace, size)
        return _Ceilings(over_file_limit=over_file, storage_exceeded=exceeded, storage_limit=limit)

    async def _mint_part_urls(self, doc: MultipartUpload, *, missing: list[int]) -> list[dict]:
        """Presigned URLs for up to ``MAX_PARTS_PER_RESPONSE`` of ``missing``.

        Empty on the relay path — there is no URL to sign, and the client is
        told so by ``mode`` rather than by an empty list it has to interpret.
        A part whose signing fails is simply omitted: ``sign_part`` returns
        ``None`` on failure by contract, and the client re-asks the status
        route for what it still needs.
        """
        if doc.mode != MODE_PRESIGNED:
            return []
        expires_at = iso_utc(datetime.now(UTC) + timedelta(seconds=PART_URL_TTL_SECONDS))
        out: list[dict] = []
        for number in missing[:MAX_PARTS_PER_RESPONSE]:
            url = await self._adapter.sign_part(
                doc.storage_key, doc.provider_upload_id, number, PART_URL_TTL_SECONDS
            )
            if url:
                out.append({"part_number": number, "url": url, "expires_at": expires_at})
        return out


# ---------------------------------------------------------------------------
# Validation — every one of these refuses with the contract's 400 code
# ---------------------------------------------------------------------------


def _clean_filename(raw: object) -> str:
    """A filename that cannot escape a folder or arrive empty."""
    if not isinstance(raw, str) or not raw.strip():
        raise BadRequest("multipart.invalid", "filename is required")
    name = raw.strip().replace("\\", "/").rsplit("/", 1)[-1]
    if not name or name in (".", ".."):
        raise BadRequest("multipart.invalid", "filename is required")
    return name


def _validated_size(raw: object) -> int:
    """A non-negative int. ``bool`` is rejected — it passes ``isinstance(int)``
    and would make ``True`` a one-byte file."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise BadRequest("multipart.invalid", "size must be an integer number of bytes")
    if raw < 0:
        raise BadRequest("multipart.invalid", "size must not be negative")
    return raw


def _resolve_mime(raw: object, filename: str) -> str:
    """The type to record. A generic type asks the filename, as elsewhere.

    There is no magic-byte sniff on this path and there cannot be one: on the
    presigned path the bytes never pass through us. The type is therefore
    client-asserted, which is safe for the same reason the wildcard type policy
    is safe — ``INLINE_MIMES`` and the download rails decide what may render,
    not this string.
    """
    if raw is not None and not isinstance(raw, str):
        raise BadRequest("multipart.invalid", "mime must be a string")
    mime = normalize_mime(raw if isinstance(raw, str) else "")
    if is_generic_mime(mime):
        mime = mime_for_filename(filename) or mime
    return mime


def _validated_part_number(raw: object, part_count: int) -> int:
    """1..``part_count``. Outside that is ``multipart.invalid``.

    ``validate_part_number`` already bounds it to 1..10000 and raises
    ``InvalidPart``, whose ``code`` is ALREADY the contract's wire code — so it
    maps straight across with no translation table. This adds the per-session
    bound on top, because a part number past ``part_count`` is a client bug that
    would otherwise become an orphan part nobody completes.
    """
    try:
        number = validate_part_number(raw)
    except InvalidPart as exc:
        raise BadRequest(exc.code, str(exc)) from exc
    if number > part_count:
        raise BadRequest(
            "multipart.invalid",
            f"part number {number} is past this session's {part_count} parts",
        )
    return number


def _validated_claim(raw: object, *, part_count: int) -> dict[int, str]:
    """What the client CLAIMS it uploaded. Optional, and never the manifest.

    Absent, null or empty is the normal resumed case and yields ``{}`` — the
    client has nothing to assert, and storage is asked instead.

    When present it must be well-formed, and that is still a 400: a list of
    objects, each with an in-range part number and a non-empty etag. Two entries
    for one part number that disagree are refused here rather than at the
    storage comparison, because the client has contradicted itself before we
    even look at the bucket.
    """
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise BadRequest("multipart.invalid", "parts must be a list when supplied")

    claimed: dict[int, str] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            raise BadRequest("multipart.invalid", "each part must be an object")
        number = _validated_part_number(entry.get("part_number"), part_count)
        etag = entry.get("etag")
        if not isinstance(etag, str) or not etag.strip():
            raise BadRequest("multipart.invalid", f"part {number} is missing an etag")
        seen = claimed.get(number)
        if seen is not None and _norm_etag(seen) != _norm_etag(etag):
            raise BadRequest(
                "multipart.invalid",
                f"part {number} was listed twice with different etags",
            )
        claimed[number] = etag.strip()
    return claimed


def _norm_etag(raw: str) -> str:
    """Compare etags on content, not on transport decoration.

    S3 returns them wrapped in literal double quotes and clients variously echo
    them quoted, unquoted, or with a ``W/`` weak-validator prefix picked up from
    a proxy. All three name the same bytes, and refusing an upload over a pair
    of quotation marks would be a bad way to lose a 5 GB transfer.
    """
    value = raw.strip()
    if value.startswith("W/"):
        value = value[2:]
    return value.strip('"').strip().lower()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _is_expired(doc: MultipartUpload) -> bool:
    """Past its TTL. Naive timestamps are read as UTC — mongomock and some
    drivers hand back tz-naive datetimes, and comparing those raises."""
    expires = doc.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return datetime.now(UTC) >= expires


def _size_from_session(doc: MultipartUpload) -> int:
    """Best honest size when storage could not tell us.

    Prefers the bytes we counted ourselves — on the relay path every part was
    weighed on the way through, so this is the real number. Falls back to the
    size the client declared, which is untrusted; the alternative is zero, and
    a zero-byte row for a file that exists is a worse lie than a client's own
    figure. The caller logs when this path is taken.
    """
    measured = [p.size for p in doc.parts if p.size is not None]
    if measured and len(measured) == len(doc.parts):
        return sum(measured)
    return int(doc.size)


def _raise_for_ceilings(ceilings: _Ceilings, *, size: int, limit: int) -> None:
    """Turn a ceiling result into the contract's error, in priority order."""
    if ceilings.over_file_limit:
        raise PayloadTooLarge(
            "multipart.too_large",
            f"file is {size} bytes, over the {limit}-byte limit for large uploads",
        )
    if ceilings.storage_exceeded:
        from pocketpaw_ee.cloud._core.errors import StorageLimitError

        raise StorageLimitError(ceilings.storage_limit)


__all__ = ["EEMultipartService", "MAX_PARTS_PER_RESPONSE", "PART_URL_TTL_SECONDS"]
