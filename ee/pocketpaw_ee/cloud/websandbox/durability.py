# durability.py — Web Cursor workspace durability: S3 snapshot + restore.
# Created 2026-07-15 (WC-S3, feat/websandbox-s3-durability).
#
# Changed 2026-07-16 (CM-2a′ write-through, feat/code-mode): added the incremental
# per-file tier. ``mirror_file`` write-throughs a single editor-saved file to blob
# storage on every ``file.write`` and records ``relpath -> FileRecord id`` in the
# row's ``overlay`` map; ``restore_workspace`` now applies BOTH tiers in order —
# the full snapshot tar (baseline), then the overlay replayed on top (freshest
# edits). The overlay is cleared by ``service.set_snapshot`` (a full snapshot
# supersedes it), which keeps replay from resurrecting a since-deleted file.
#
# Changed 2026-07-16 (WC-4c file verbs, feat/code-mode): added the delete/rename
# overlay siblings ``drop_overlay`` (removes an entry so a deleted file is not
# resurrected on restore) and ``move_overlay`` (re-keys an entry so a renamed file
# replays at its new path). ws.py binds them as the FileRpc on_delete / on_move
# hooks, the delete/rename counterparts of the on_write mirror.
#
# The Daytona VM is pure scratch and gets reaped (WC-2). Code durability is git
# (push); this module makes a user's UNCOMMITTED work + workspace state durable
# by snapshotting the in-VM workspace dir to the tenant's blob storage (S3,
# indexed in Mongo) and restoring it into a fresh VM. Nothing is file-based —
# the durable tier is S3, the pointer is a FileRecord id on the WebSandbox row.
#
# This is the service-layer orchestration ABOVE ``websandbox/service.py`` (the
# registry + auth oracle). Per ee/cloud Rule 2 it NEVER touches the WebSandbox
# Beanie doc directly — the snapshot pointer write goes through
# ``service.set_snapshot`` and every read through ``service.get_sandbox`` /
# ``service.authorize_sandbox``. Importing DaytonaClient + EEUploadService here
# (not in router/dto/domain) is the correct layer.
#
# The S3 vehicle is ART-4's ``EEUploadService`` — the same "read a file out of a
# VM/jail → upload to the tenant's S3 → get a durable pointer" path
# ``deliver.py`` uses. We reuse it verbatim: wrap the tarball bytes in a
# Starlette ``UploadFile`` and call ``upload(...)`` (workspace-scoped), then
# stash the returned ``FileRecord.id`` on the row. Restore reads the bytes back
# through the same service's owner-scoped ``stream(...)`` and untars them into
# the VM.
#
# Two flows:
#   1. ``snapshot_workspace`` — authorize (owner-scoped get + fail-closed
#      authorize_sandbox) → ``tar -czf`` the workspace dir in the VM →
#      ``download_file`` the tarball → size-cap → ``EEUploadService.upload`` to
#      tenant S3 (folder ``/websandbox-snapshots``) → ``set_snapshot`` → return
#      the FileRecord id.
#   2. ``restore_workspace`` — authorize → read the row's ``snapshot_file_id``
#      (clean 409 if none) → fetch the tarball bytes from S3 → ``upload_bytes``
#      into the VM → ``tar -xzf`` into the workspace dir.
#
# Both take DI seams (``client=None`` → get_daytona_client, ``uploads=None`` →
# a per-call EEUploadService) so tests inject fakes and never hit real Daytona
# or S3.
#
# Changed 2026-07-24 (F, feat/code-durable-project-store): added the PROJECT-KEYED
# sibling path — ``snapshot_project`` / ``restore_project`` / ``mirror_file_to_project``
# / ``drop_project_overlay`` / ``move_project_overlay``. Same S3 vehicle, same VM
# tar/untar mechanics, same DI seams; only the POINTER anchor differs — these
# read/write the snapshot + overlay via ``codeproject/service`` (on the durable
# CodeProject row) instead of ``websandbox/service`` (on the ephemeral WebSandbox
# row), so a project's files round-trip through blob storage independent of any
# runtime and independent of the WebSandbox row. The VM to snapshot/restore is
# passed as an explicit Daytona ``sandbox_id`` (not resolved from a WebSandbox
# row), the runtime-agnostic seam B2 will wire up. ADDITIVE: the sandbox-keyed
# functions above are untouched. The project-keyed durable WRITE path
# (``snapshot_project`` / ``mirror_file_to_project``) hard-requires S3 in cloud
# via ``_require_s3_for_project_store`` — silent non-persistence to local disk is
# worse than a loud 503.
#
# Changed 2026-07-25 (B1, feat/code-project-file-sync): added the BROWSER-runtime
# siblings — ``put_project_file`` / ``drop_project_file`` / ``read_project_overlay``.
# Every project-keyed function above tars or untars a Daytona VM, i.e. the bytes
# live in a VM the backend can reach. On the in-tab (WebContainer) runtime there is
# no VM: the filesystem is in the user's browser, so the bytes arrive FROM the
# client on a write and must be handed BACK to it on a reopen. These three close
# that loop over the same two-tier model, minus the snapshot tier — the baseline is
# the starter scaffold, deterministically re-materializable client-side from the
# starter id and therefore never uploaded, so the overlay alone is the whole
# durable delta. Restore = re-materialize the scaffold, then replay the overlay.
# Same S3 vehicle, same ``_upload_project_blob``, same owner-scoped ``stream(...)``
# read path ``restore_project`` uses, same ``_require_s3_for_project_store``
# fail-closed guard on the write, same ``uploads=`` DI seam. ADDITIVE: nothing
# above is touched.
#
# Changed 2026-07-25 (B4, feat/code-cross-runtime-restore): ``read_project_overlay``
# is now TWO-TIER, closing a cross-runtime data-visibility bug. WHY: a project can
# open in either runtime, but the two tiers were read asymmetrically — work done in
# a Daytona VM ends up in ``snapshot_file_id`` (and ``set_project_snapshot`` CLEARS
# the overlay, correctly, because the tar supersedes it), while the in-tab read-back
# looked ONLY at ``overlay``. So Daytona → disconnect → reopen IN-TAB returned ``{}``
# and the browser re-materialized the bare starter scaffold: the user's work looked
# deleted while it sat safe in S3 inside a tarball no browser can read. The read-back
# now composes the SAME two tiers ``restore_project`` replays into a VM — snapshot
# tar expanded in memory as the baseline, overlay applied on top (freshest wins) —
# so whatever either runtime saved is retrievable from either runtime. It is a READ
# -side fix only: no write path and no snapshot semantics changed. Two concerns the
# VM path doesn't have are handled here because the payload crosses a JSON wire into
# a browser: regenerable trees (``node_modules``/``.git``/build output, which the tar
# carries with no excludes) are filtered out, and tar members are treated as
# UNTRUSTED — absolute/traversing names and non-regular entries (symlinks, hardlinks,
# devices) are skipped rather than materialized.
from __future__ import annotations

import io
import logging
import os
import tarfile
from pathlib import Path
from typing import Any

from pocketpaw_ee.cloud._core.errors import (
    CloudError,
    ConflictError,
    ValidationError,
    with_cause,
)
from pocketpaw_ee.cloud.codeproject import service as codeproject_service
from pocketpaw_ee.cloud.daytona.client import DaytonaClient, get_daytona_client
from pocketpaw_ee.cloud.shared.db import is_multi_tenant_cloud
from pocketpaw_ee.cloud.websandbox import service as websandbox_service
from pocketpaw_ee.cloud.websandbox.constants import WEBSANDBOX_WORKDIR

logger = logging.getLogger(__name__)

# Blob-storage folders for the project-keyed durable store — grouped separately
# from the sandbox-keyed ``/websandbox-*`` folders so a tenant's project
# snapshots/overlay are distinguishable in their Files.
_PROJECT_SNAPSHOT_FOLDER = "/code-project-snapshots"
_PROJECT_OVERLAY_FOLDER = "/code-project-overlay"

_TRUTHY = {"1", "true", "yes", "on"}

# The in-VM staging path for the tarball (outside the workspace dir so it never
# tars itself and a stale copy never re-enters a future snapshot).
_SNAPSHOT_TMP = "/tmp/ws-snapshot.tgz"  # noqa: S108 — a sandbox VM path, not host

# gzip-compressed tar. ``_sniff_mime`` doesn't recognize gzip magic, so the
# UploadFile's declared content-type is the mime that reaches the allowlist —
# we declare this and include it in the per-call UploadSettings allowlist.
_SNAPSHOT_MIME = "application/gzip"

# Default per-snapshot size cap. The workspace can carry node_modules / build
# output, so this is larger than the 25 MiB browser-upload default but bounded
# so a runaway workspace can't blow up a snapshot.
_DEFAULT_SNAPSHOT_MAX_MB = 500.0


def _snapshot_max_bytes() -> int:
    """Per-snapshot size cap in bytes (``POCKETPAW_WEBSANDBOX_SNAPSHOT_MAX_MB``)."""
    raw = os.environ.get("POCKETPAW_WEBSANDBOX_SNAPSHOT_MAX_MB", "").strip()
    mb = _DEFAULT_SNAPSHOT_MAX_MB
    if raw:
        try:
            mb = float(raw)
        except ValueError:
            logger.warning(
                "ignoring non-numeric POCKETPAW_WEBSANDBOX_SNAPSHOT_MAX_MB=%r; using %s", raw, mb
            )
    return int(mb * 1024 * 1024)


def _require_client(client: DaytonaClient | None) -> DaytonaClient:
    """Resolve the Daytona client, raising a clean CloudError when unconfigured.

    Mirrors ``provision._require_client``: ``get_daytona_client()`` returns
    ``None`` when the Daytona keys are unset — an operational condition, not a
    bug — so it surfaces as a 503 CloudError rather than an AttributeError crash.
    """
    resolved = client if client is not None else get_daytona_client()
    if resolved is None:
        raise CloudError(
            503,
            "websandbox.daytona_unavailable",
            "The sandbox runtime is not configured",
        )
    return resolved


def build_uploads() -> Any:
    """Build a snapshot-scoped ``EEUploadService`` the same way ``deliver.py`` does.

    Per-call (cheap): the workspace-scoped ``StorageAdapter`` (local or S3 via
    ``POCKETPAW_UPLOAD_ADAPTER``), a Mongo metadata store, and an
    ``UploadSettings`` whose ``allowed_mimes`` includes the gzip-tar mime so the
    OSS mime gate never rejects a first-party snapshot, and whose
    ``max_file_bytes`` matches our snapshot cap.
    """
    from pocketpaw.uploads.config import DEFAULT_ALLOWED_MIMES, UploadSettings
    from pocketpaw.uploads.factory import build_adapter
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore
    from pocketpaw_ee.cloud.uploads.service import EEUploadService

    root = Path.home() / ".pocketpaw" / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    adapter = build_adapter(root)
    cfg = UploadSettings(
        max_file_bytes=_snapshot_max_bytes(),
        allowed_mimes=frozenset(
            {_SNAPSHOT_MIME, "application/octet-stream", *DEFAULT_ALLOWED_MIMES}
        ),
        local_root=root,
    )
    return EEUploadService(adapter=adapter, meta=MongoFileStore(), cfg=cfg)


async def _upload_snapshot(
    uploads: Any,
    data: bytes,
    *,
    workspace_id: str,
    user_id: str,
    row_id: str,
) -> str:
    """Land the tarball bytes in the tenant's blob storage; return the FileRecord id.

    Wraps the in-memory bytes in a Starlette ``UploadFile`` exactly like
    ``deliver.py`` (a dict ``headers`` carrying the declared content-type is all
    ``UploadFile.content_type`` reads), then calls the workspace-scoped
    ``upload`` — the ART-4 S3 path. Folder-scoped to ``/websandbox-snapshots``
    so snapshots are grouped in the tenant's Files.
    """
    from fastapi import UploadFile

    upload = UploadFile(
        file=io.BytesIO(data),
        filename=f"ws-snapshot-{row_id}.tgz",
        headers={"content-type": _SNAPSHOT_MIME},  # type: ignore[arg-type]
    )
    rec = await uploads.upload(
        upload,
        owner_id=user_id,
        chat_id=None,
        workspace=workspace_id,
        folder_path="/websandbox-snapshots",
    )
    return rec.id


async def _download_snapshot(
    uploads: Any,
    file_id: str,
    *,
    workspace_id: str,
    user_id: str,
) -> bytes:
    """Fetch the snapshot tarball bytes back from blob storage (owner-scoped).

    Uses ``EEUploadService.stream`` — the workspace + owner-checked read path —
    and drains the async chunk iterator into bytes. A missing / not-owned blob
    surfaces as a clean CloudError rather than an upstream ``NotFound`` leak.
    """
    from pocketpaw.uploads.errors import NotFound as _UploadNotFound

    try:
        _rec, chunks = await uploads.stream(file_id, requester_id=user_id, workspace=workspace_id)
    except _UploadNotFound as exc:
        raise with_cause(
            CloudError(
                404,
                "websandbox.snapshot_missing",
                "The recorded snapshot could not be read from storage",
            ),
            exc,
        ) from exc
    buf = bytearray()
    async for chunk in chunks:
        buf.extend(chunk)
    return bytes(buf)


# ---------------------------------------------------------------------------
# snapshot flow.
# ---------------------------------------------------------------------------


async def snapshot_workspace(
    workspace_id: str,
    user_id: str,
    row_id: str,
    *,
    client: DaytonaClient | None = None,
    uploads: Any = None,
) -> str:
    """Snapshot a ready sandbox's workspace to the tenant's blob storage.

    Resolves the row tenant + owner scoped (``get_sandbox`` raises ``NotFound``
    for a row the caller doesn't own), runs the fail-closed ``authorize_sandbox``
    on the bound Daytona id BEFORE any VM/S3 op, then: ``tar -czf`` the workspace
    dir → ``download_file`` the tarball → size-cap → upload to S3 →
    ``set_snapshot`` the returned FileRecord id on the row. Returns that id.

    A row that hasn't bound a Daytona id yet (never provisioned / still opening)
    is a clean 409, not a runtime crash. A tarball over the size cap raises a
    clean CloudError before any upload.
    """
    daytona = _require_client(client)

    row = await websandbox_service.get_sandbox(workspace_id, user_id, row_id)
    if not row.sandbox_id:
        raise ConflictError("websandbox.not_ready", "Sandbox is not provisioned yet")

    # Fail-closed authorization on the Daytona id BEFORE touching the runtime.
    await websandbox_service.authorize_sandbox(workspace_id, user_id, row.sandbox_id)

    # 1. Tar the workspace dir inside the VM (everything, incl. .git — simple and
    #    complete). ``-C`` so arcnames are relative to the workspace root.
    try:
        await daytona.execute_command(
            row.sandbox_id,
            f"tar -czf {_SNAPSHOT_TMP} -C {WEBSANDBOX_WORKDIR} .",
        )
        data = await daytona.download_file(row.sandbox_id, _SNAPSHOT_TMP)
    except Exception as exc:  # noqa: BLE001 — any VM-side failure is uniform
        logger.warning("websandbox.snapshot: tar/download failed for row=%s", row_id, exc_info=True)
        raise with_cause(
            CloudError(502, "websandbox.snapshot_failed", "Failed to snapshot the workspace"),
            exc,
        ) from exc

    # 2. Guard the size before spending an S3 upload on a runaway workspace.
    cap = _snapshot_max_bytes()
    if len(data) > cap:
        raise CloudError(
            413,
            "websandbox.snapshot_too_large",
            f"Workspace snapshot is {len(data) / 1024 / 1024:.1f} MB, over the "
            f"{cap / 1024 / 1024:.0f} MB limit",
        )

    # 3. Land it in the tenant's blob storage (the ART-4 S3 path) and record the
    #    durable pointer on the row.
    up = uploads if uploads is not None else build_uploads()
    file_id = await _upload_snapshot(
        up, data, workspace_id=workspace_id, user_id=user_id, row_id=row_id
    )
    await websandbox_service.set_snapshot(workspace_id, user_id, row_id, file_id)
    logger.info(
        "websandbox.snapshot: row=%s daytona=%s -> file=%s (%d bytes)",
        row_id,
        row.sandbox_id,
        file_id,
        len(data),
    )
    return file_id


# ---------------------------------------------------------------------------
# restore flow.
# ---------------------------------------------------------------------------


async def restore_workspace(
    workspace_id: str,
    user_id: str,
    row_id: str,
    *,
    client: DaytonaClient | None = None,
    uploads: Any = None,
) -> None:
    """Restore a row's durable state into its (fresh) VM: snapshot then overlay.

    Two durability tiers land here in order (CM-2a′):
      1. ``snapshot_file_id`` — the coarse full-workspace tarball (on-disconnect).
         Fetched from S3, ``upload_bytes`` into the VM, ``tar -xzf`` over the dir.
      2. ``overlay`` — the write-through per-file tier (each editor save since the
         last snapshot). Each ``relpath -> FileRecord id`` is fetched and written
         over the tree. The overlay is CLEARED whenever a snapshot is taken, so it
         only ever holds edits made AFTER the snapshot — replaying it on top is
         always the freshest source state, never a stale resurrection.

    Resolves the row tenant + owner scoped, fail-closed authorizes on the bound
    Daytona id, then applies whatever tiers exist. A row with NEITHER a snapshot
    nor an overlay is a clean 409 (nothing to restore). A bound-VM requirement:
    an unprovisioned row is a 409, not a crash.
    """
    daytona = _require_client(client)

    row = await websandbox_service.get_sandbox(workspace_id, user_id, row_id)
    overlay = row.overlay or {}
    if not row.snapshot_file_id and not overlay:
        raise ConflictError(
            "websandbox.no_snapshot", "This sandbox has no durable state to restore"
        )
    if not row.sandbox_id:
        raise ConflictError("websandbox.not_ready", "Sandbox is not provisioned yet")

    # Fail-closed authorization on the Daytona id BEFORE touching the runtime.
    await websandbox_service.authorize_sandbox(workspace_id, user_id, row.sandbox_id)

    up = uploads if uploads is not None else build_uploads()

    # Tier 1 — the full-workspace snapshot (baseline).
    if row.snapshot_file_id:
        data = await _download_snapshot(
            up, row.snapshot_file_id, workspace_id=workspace_id, user_id=user_id
        )
        untar = f"mkdir -p {WEBSANDBOX_WORKDIR} && tar -xzf {_SNAPSHOT_TMP} -C {WEBSANDBOX_WORKDIR}"
        try:
            await daytona.upload_bytes(row.sandbox_id, data, _SNAPSHOT_TMP)
            await daytona.execute_command(row.sandbox_id, untar)
        except Exception as exc:  # noqa: BLE001 — any VM-side failure is uniform
            logger.warning(
                "websandbox.restore: snapshot untar failed for row=%s", row_id, exc_info=True
            )
            raise with_cause(
                CloudError(502, "websandbox.restore_failed", "Failed to restore the workspace"),
                exc,
            ) from exc
        logger.info(
            "websandbox.restore: row=%s daytona=%s <- snapshot=%s (%d bytes)",
            row_id,
            row.sandbox_id,
            row.snapshot_file_id,
            len(data),
        )

    # Tier 2 — replay the write-through overlay (freshest edits) over the tree.
    replayed = 0
    for rel_path, file_id in overlay.items():
        abs_path = _overlay_abs_path(rel_path)
        if abs_path is None:
            logger.warning("websandbox.restore: skipping unsafe overlay path %r", rel_path)
            continue
        try:
            file_bytes = await _download_snapshot(
                up, file_id, workspace_id=workspace_id, user_id=user_id
            )
            parent = abs_path.rsplit("/", 1)[0]
            await daytona.execute_command(row.sandbox_id, f"mkdir -p {parent}")
            await daytona.upload_bytes(row.sandbox_id, file_bytes, abs_path)
            replayed += 1
        except Exception:  # noqa: BLE001 — one bad overlay file must not sink the rest
            logger.warning(
                "websandbox.restore: overlay replay failed for row=%s path=%r",
                row_id,
                rel_path,
                exc_info=True,
            )
    if replayed:
        logger.info(
            "websandbox.restore: row=%s daytona=%s replayed %d overlay file(s)",
            row_id,
            row.sandbox_id,
            replayed,
        )


def _overlay_abs_path(rel_path: str) -> str | None:
    """Jail an overlay relpath into the workspace dir; ``None`` if it escapes.

    Defense-in-depth: the overlay paths originate from the ``file.write`` jail, but
    a restore writes bytes straight into the VM, so re-reject anything absolute or
    containing a ``..`` traversal segment before it becomes a VM path.
    """
    candidate = (rel_path or "").strip().lstrip("/")
    if not candidate or candidate.startswith("/"):
        return None
    if any(seg in ("..",) for seg in candidate.split("/")):
        return None
    return f"{WEBSANDBOX_WORKDIR}/{candidate}"


async def mirror_file(
    workspace_id: str,
    user_id: str,
    row_id: str,
    rel_path: str,
    data: bytes,
    *,
    uploads: Any = None,
) -> str:
    """Write-through one editor-saved file to blob storage; record the overlay entry.

    The incremental durability tier (CM-2a′): called best-effort right after the
    ``file.write`` byte-for-byte VM write, so the edit is durable in S3 the moment
    it lands — a crash / idle-out before the next full snapshot no longer loses it.
    Uploads the bytes to the tenant's blob storage (folder ``/websandbox-overlay``)
    and records ``rel_path -> FileRecord id`` on the row via ``set_overlay_entry``.
    Returns the FileRecord id. The bytes are declared ``application/octet-stream``
    (in the per-call allowlist) so the OSS mime gate never rejects a source file.
    """
    from fastapi import UploadFile

    up = uploads if uploads is not None else build_uploads()
    basename = (rel_path or "file").rsplit("/", 1)[-1] or "file"
    upload = UploadFile(
        file=io.BytesIO(data),
        filename=f"overlay-{row_id}-{basename}",
        headers={"content-type": "application/octet-stream"},  # type: ignore[arg-type]
    )
    rec = await up.upload(
        upload,
        owner_id=user_id,
        chat_id=None,
        workspace=workspace_id,
        folder_path="/websandbox-overlay",
    )
    await websandbox_service.set_overlay_entry(workspace_id, user_id, row_id, rel_path, rec.id)
    logger.debug(
        "websandbox.mirror: row=%s path=%r -> file=%s (%d bytes)",
        row_id,
        rel_path,
        rec.id,
        len(data),
    )
    return rec.id


async def drop_overlay(
    workspace_id: str,
    user_id: str,
    row_id: str,
    rel_path: str,
) -> None:
    """Drop the overlay entry for a deleted file (WC-4c on_delete hook).

    The delete-side sibling of ``mirror_file``: after ``file.delete`` removes the
    file in the VM, drop its overlay entry so a later restore falls back to the
    snapshot tier instead of resurrecting the deleted file. Dropping is always
    safe (worst case the file reappears from the snapshot, never a stale
    resurrection). Delegates the doc write to the service (Rule 2). A directory
    delete drops every child entry too.
    """
    await websandbox_service.drop_overlay_entry(workspace_id, user_id, row_id, rel_path)


async def move_overlay(
    workspace_id: str,
    user_id: str,
    row_id: str,
    src_rel: str,
    dst_rel: str,
) -> None:
    """Re-key the overlay entry for a renamed file (WC-4c on_move hook).

    The move-side sibling of ``mirror_file``: after ``file.move`` renames the file
    in the VM, re-key its overlay entry from ``src_rel`` to ``dst_rel`` so restore
    replays the same durable blob at the new path. The FileRecord id is unchanged,
    so no blob work is needed — only the key moves. Delegates the doc write to the
    service (Rule 2). A directory move re-keys every child entry too.
    """
    await websandbox_service.move_overlay_entry(workspace_id, user_id, row_id, src_rel, dst_rel)


# ===========================================================================
# Project-keyed durable store (F, feat/code-durable-project-store).
#
# The durable half of the /code build-and-persist loop. Same S3 vehicle and VM
# tar/untar as the sandbox-keyed path above; the ONLY difference is the pointer
# anchor — snapshot + overlay live on the durable ``CodeProject`` row (via
# ``codeproject/service``), so a project's files survive VM reaping and any
# WebSandbox row. The VM is passed as an explicit Daytona ``sandbox_id`` rather
# than resolved from a WebSandbox row, keeping this independent of the ephemeral
# runtime row (B2 will wire the resolution). Tenancy is enforced by the
# owner-scoped ``get_project`` (a project the caller doesn't own reads as
# NotFound).
# ===========================================================================


def _require_s3_for_project_store() -> None:
    """Hard-require S3-backed blob storage for the durable project store in cloud.

    The ART-4 boot guard (``uploads/bootstrap.verify_cloud_storage_backend``) only
    WARNs on a non-s3 adapter unless ``POCKETPAW_REQUIRE_S3_IN_CLOUD`` is set. For
    the durable PROJECT store a warn is not enough: a project-keyed write that
    lands on the box's local disk instead of the tenant's object store looks like
    it persisted but is gone with the container — silent non-persistence, worse
    than a loud failure. So the project-keyed durable WRITE path fails CLOSED —
    a clean 503 ``CloudError`` (not a crash) — when running in cloud
    (``is_multi_tenant_cloud()``, the same signal the jail and ART-4 guard read)
    and ``POCKETPAW_UPLOAD_ADAPTER`` is not ``s3``.

    No-op OFF multi-tenant cloud (OSS / dedicated installs never initialized the
    cloud DB): there is no tenant object store to require, so local-disk uploads
    are correct there and this must not raise.
    """
    if not is_multi_tenant_cloud():
        return
    adapter = os.environ.get("POCKETPAW_UPLOAD_ADAPTER", "local").strip().lower()
    if adapter == "s3":
        return
    raise CloudError(
        503,
        "codeproject.durable_store_requires_s3",
        "The durable project store requires S3-backed blob storage in cloud "
        f"(POCKETPAW_UPLOAD_ADAPTER={adapter!r}, expected 's3'); a local adapter "
        "writes project snapshots to the box's disk, which vanishes with the "
        "container. Set POCKETPAW_UPLOAD_ADAPTER=s3 and the S3_* settings.",
    )


async def _upload_project_blob(
    uploads: Any,
    data: bytes,
    *,
    workspace_id: str,
    user_id: str,
    filename: str,
    folder: str,
    content_type: str,
) -> str:
    """Land bytes in the tenant's blob storage; return the FileRecord id.

    The project-keyed analog of ``_upload_snapshot`` / ``mirror_file``'s inline
    upload — a single wrapper both project write paths share, so the sandbox-keyed
    helpers stay untouched. Wraps the bytes in a Starlette ``UploadFile`` (a dict
    ``headers`` carrying the declared content-type is all ``UploadFile.content_type``
    reads) and calls the workspace + owner-scoped ``upload`` (the ART-4 S3 path).
    """
    from fastapi import UploadFile

    upload = UploadFile(
        file=io.BytesIO(data),
        filename=filename,
        headers={"content-type": content_type},  # type: ignore[arg-type]
    )
    rec = await uploads.upload(
        upload,
        owner_id=user_id,
        chat_id=None,
        workspace=workspace_id,
        folder_path=folder,
    )
    return rec.id


async def snapshot_project(
    workspace_id: str,
    user_id: str,
    project_id: str,
    sandbox_id: str,
    *,
    client: DaytonaClient | None = None,
    uploads: Any = None,
) -> str:
    """Snapshot a project's live VM workspace to blob storage; return the pointer.

    Mirrors ``snapshot_workspace`` but anchors the pointer on the durable
    ``CodeProject`` row. Resolves the project owner-scoped (``get_project`` raises
    ``NotFound`` for a project the caller doesn't own), then ``tar -czf`` the
    workspace dir in ``sandbox_id`` → ``download_file`` the tarball → size-cap →
    upload to S3 (folder ``/code-project-snapshots``) → ``set_project_snapshot``
    the returned FileRecord id (which also clears the overlay). Returns that id.

    The ``sandbox_id`` is the live Daytona VM to snapshot, passed explicitly (not
    resolved from a WebSandbox row) so this stays independent of the ephemeral
    runtime row. Fails CLOSED on a non-s3 upload adapter in cloud before any VM
    op. A tarball over the size cap raises a clean CloudError before any upload.
    """
    _require_s3_for_project_store()
    daytona = _require_client(client)

    # Owner-scoped tenancy gate: NotFound for a project the caller doesn't own.
    await codeproject_service.get_project(workspace_id, user_id, project_id)

    try:
        await daytona.execute_command(
            sandbox_id,
            f"tar -czf {_SNAPSHOT_TMP} -C {WEBSANDBOX_WORKDIR} .",
        )
        data = await daytona.download_file(sandbox_id, _SNAPSHOT_TMP)
    except Exception as exc:  # noqa: BLE001 — any VM-side failure is uniform
        logger.warning(
            "codeproject.snapshot: tar/download failed for project=%s", project_id, exc_info=True
        )
        raise with_cause(
            CloudError(502, "codeproject.snapshot_failed", "Failed to snapshot the project"),
            exc,
        ) from exc

    cap = _snapshot_max_bytes()
    if len(data) > cap:
        raise CloudError(
            413,
            "codeproject.snapshot_too_large",
            f"Project snapshot is {len(data) / 1024 / 1024:.1f} MB, over the "
            f"{cap / 1024 / 1024:.0f} MB limit",
        )

    up = uploads if uploads is not None else build_uploads()
    file_id = await _upload_project_blob(
        up,
        data,
        workspace_id=workspace_id,
        user_id=user_id,
        filename=f"code-project-snapshot-{project_id}.tgz",
        folder=_PROJECT_SNAPSHOT_FOLDER,
        content_type=_SNAPSHOT_MIME,
    )
    await codeproject_service.set_project_snapshot(workspace_id, user_id, project_id, file_id)
    logger.info(
        "codeproject.snapshot: project=%s daytona=%s -> file=%s (%d bytes)",
        project_id,
        sandbox_id,
        file_id,
        len(data),
    )
    return file_id


async def restore_project(
    workspace_id: str,
    user_id: str,
    project_id: str,
    sandbox_id: str,
    *,
    client: DaytonaClient | None = None,
    uploads: Any = None,
) -> None:
    """Restore a project's durable state into a (fresh) VM: snapshot then overlay.

    Mirrors ``restore_workspace`` but reads the pointer off the durable
    ``CodeProject`` row (via the owner-scoped ``get_project`` view — so it also
    enforces tenancy). Applies the two tiers in order into ``sandbox_id``:
      1. ``snapshot_file_id`` — the full-workspace tarball (baseline): fetched
         from S3, ``upload_bytes`` into the VM, ``tar -xzf`` over the dir.
      2. ``overlay`` — the write-through per-file tier (edits since the last
         snapshot): each ``relpath -> FileRecord id`` is fetched and written over
         the tree. Cleared whenever a snapshot is taken, so replay is always the
         freshest source state, never a stale resurrection.

    A project with NEITHER a snapshot nor an overlay is a clean 409 (nothing to
    restore). No S3 guard here — restore is a READ path; the guard protects the
    WRITE path so a project never silently persists to disk in the first place.
    """
    daytona = _require_client(client)

    project = await codeproject_service.get_project(workspace_id, user_id, project_id)
    overlay = project.overlay or {}
    if not project.snapshot_file_id and not overlay:
        raise ConflictError(
            "codeproject.no_snapshot", "This project has no durable state to restore"
        )

    up = uploads if uploads is not None else build_uploads()

    # Tier 1 — the full-workspace snapshot (baseline).
    if project.snapshot_file_id:
        data = await _download_snapshot(
            up, project.snapshot_file_id, workspace_id=workspace_id, user_id=user_id
        )
        untar = f"mkdir -p {WEBSANDBOX_WORKDIR} && tar -xzf {_SNAPSHOT_TMP} -C {WEBSANDBOX_WORKDIR}"
        try:
            await daytona.upload_bytes(sandbox_id, data, _SNAPSHOT_TMP)
            await daytona.execute_command(sandbox_id, untar)
        except Exception as exc:  # noqa: BLE001 — any VM-side failure is uniform
            logger.warning(
                "codeproject.restore: snapshot untar failed for project=%s",
                project_id,
                exc_info=True,
            )
            raise with_cause(
                CloudError(502, "codeproject.restore_failed", "Failed to restore the project"),
                exc,
            ) from exc
        logger.info(
            "codeproject.restore: project=%s daytona=%s <- snapshot=%s (%d bytes)",
            project_id,
            sandbox_id,
            project.snapshot_file_id,
            len(data),
        )

    # Tier 2 — replay the write-through overlay (freshest edits) over the tree.
    replayed = 0
    for rel_path, file_id in overlay.items():
        abs_path = _overlay_abs_path(rel_path)
        if abs_path is None:
            logger.warning("codeproject.restore: skipping unsafe overlay path %r", rel_path)
            continue
        try:
            file_bytes = await _download_snapshot(
                up, file_id, workspace_id=workspace_id, user_id=user_id
            )
            parent = abs_path.rsplit("/", 1)[0]
            await daytona.execute_command(sandbox_id, f"mkdir -p {parent}")
            await daytona.upload_bytes(sandbox_id, file_bytes, abs_path)
            replayed += 1
        except Exception:  # noqa: BLE001 — one bad overlay file must not sink the rest
            logger.warning(
                "codeproject.restore: overlay replay failed for project=%s path=%r",
                project_id,
                rel_path,
                exc_info=True,
            )
    if replayed:
        logger.info(
            "codeproject.restore: project=%s daytona=%s replayed %d overlay file(s)",
            project_id,
            sandbox_id,
            replayed,
        )


async def mirror_file_to_project(
    workspace_id: str,
    user_id: str,
    project_id: str,
    rel_path: str,
    data: bytes,
    *,
    uploads: Any = None,
) -> str:
    """Write-through one editor-saved file to blob storage; record the project overlay.

    The project-keyed analog of ``mirror_file``: uploads the bytes to the tenant's
    blob storage (folder ``/code-project-overlay``) and records ``rel_path ->
    FileRecord id`` on the durable project via ``set_project_overlay_entry``.
    Returns the FileRecord id. Fails CLOSED on a non-s3 upload adapter in cloud.
    """
    _require_s3_for_project_store()
    up = uploads if uploads is not None else build_uploads()
    basename = (rel_path or "file").rsplit("/", 1)[-1] or "file"
    file_id = await _upload_project_blob(
        up,
        data,
        workspace_id=workspace_id,
        user_id=user_id,
        filename=f"overlay-{project_id}-{basename}",
        folder=_PROJECT_OVERLAY_FOLDER,
        content_type="application/octet-stream",
    )
    await codeproject_service.set_project_overlay_entry(
        workspace_id, user_id, project_id, rel_path, file_id
    )
    logger.debug(
        "codeproject.mirror: project=%s path=%r -> file=%s (%d bytes)",
        project_id,
        rel_path,
        file_id,
        len(data),
    )
    return file_id


async def drop_project_overlay(
    workspace_id: str,
    user_id: str,
    project_id: str,
    rel_path: str,
) -> None:
    """Drop the project overlay entry for a deleted file (delete-side sibling).

    Delegates the doc write to the service (Rule 2). Dropping is always safe: the
    file falls back to the snapshot tier on restore instead of being resurrected
    from the overlay. A directory delete drops every child entry too.
    """
    await codeproject_service.drop_project_overlay_entry(
        workspace_id, user_id, project_id, rel_path
    )


async def move_project_overlay(
    workspace_id: str,
    user_id: str,
    project_id: str,
    src_rel: str,
    dst_rel: str,
) -> None:
    """Re-key the project overlay entry for a renamed file (move-side sibling).

    Delegates the doc write to the service (Rule 2). The FileRecord id is
    unchanged — only the overlay key moves — so restore replays the same durable
    blob at the new path. A directory move re-keys every child entry too.
    """
    await codeproject_service.move_project_overlay_entry(
        workspace_id, user_id, project_id, src_rel, dst_rel
    )


# ===========================================================================
# Browser-runtime project file sync (B1, feat/code-project-file-sync).
#
# The in-tab (WebContainer) runtime has no VM, so the three functions here are
# the client-side counterparts of ``mirror_file_to_project`` /
# ``drop_project_overlay`` / ``restore_project``: the bytes arrive from the
# browser on a write and are streamed back to it on a reopen, instead of being
# tarred out of and untarred into a Daytona VM.
#
# Only the OVERLAY tier is involved. The baseline is the starter scaffold, which
# the client re-materializes deterministically from the starter id
# (``codescaffold.compose``), so a fresh project stores nothing and the overlay
# IS the durable delta. That also means these never write ``snapshot_file_id``:
# ``set_project_snapshot`` clears the overlay, which would discard exactly the
# state this path persists.
# ===========================================================================

# Read-back caps for the browser restore payload. The whole overlay is
# materialized in memory as one JSON response, so both a per-file byte ceiling
# and a file-count ceiling are enforced — a source tree is small, and an
# unbounded read is a memory + latency hazard for the tab as much as the box.
# Env-tunable in the same shape as ``POCKETPAW_WEBSANDBOX_SNAPSHOT_MAX_MB``.
_DEFAULT_OVERLAY_MAX_MB = 10.0
_DEFAULT_OVERLAY_MAX_FILES = 2000


def _overlay_max_bytes() -> int:
    """Overlay byte cap (``POCKETPAW_CODEPROJECT_OVERLAY_MAX_MB``).

    Applied twice: as the per-file ceiling on a write (so one runaway file can
    never land) and as the cumulative ceiling on the read-back (so the restore
    payload stays bounded no matter how the overlay grew).
    """
    raw = os.environ.get("POCKETPAW_CODEPROJECT_OVERLAY_MAX_MB", "").strip()
    mb = _DEFAULT_OVERLAY_MAX_MB
    if raw:
        try:
            mb = float(raw)
        except ValueError:
            logger.warning(
                "ignoring non-numeric POCKETPAW_CODEPROJECT_OVERLAY_MAX_MB=%r; using %s", raw, mb
            )
    return int(mb * 1024 * 1024)


def _overlay_max_files() -> int:
    """Overlay file-count cap (``POCKETPAW_CODEPROJECT_OVERLAY_MAX_FILES``)."""
    raw = os.environ.get("POCKETPAW_CODEPROJECT_OVERLAY_MAX_FILES", "").strip()
    count = _DEFAULT_OVERLAY_MAX_FILES
    if raw:
        try:
            count = int(raw)
        except ValueError:
            logger.warning(
                "ignoring non-numeric POCKETPAW_CODEPROJECT_OVERLAY_MAX_FILES=%r; using %s",
                raw,
                count,
            )
    return count


def _normalize_rel_path(rel_path: str) -> str | None:
    """Normalize a path to the overlay's relpath shape; ``None`` if it escapes.

    ONE jail, two callers with different failure modes — the write boundary turns
    ``None`` into a 422 (``_require_safe_rel_path``), the snapshot expander skips
    the entry — so there is a single definition of "a safe project-relative path".
    Leading slashes and ``./`` segments are stripped (a client-side FS spells the
    same file both ways, and the overlay key must be stable or a later delete
    won't match the earlier write); an empty path or any ``..`` traversal escapes.
    """
    candidate = (rel_path or "").strip().lstrip("/")
    segments = [s for s in candidate.split("/") if s not in ("", ".")]
    if not segments or any(s == ".." for s in segments):
        return None
    return "/".join(segments)


def _require_safe_rel_path(rel_path: str) -> str:
    """Normalize a client-supplied overlay path; reject anything that escapes.

    The VM paths came out of the ``file.write`` jail; these come straight off the
    wire, so the jail check moves to the write boundary. An empty path or any
    ``..`` traversal is a clean 422 — the overlay key becomes a real path again on
    both restore paths (``_overlay_abs_path`` in a VM, the client's FS in a tab).
    """
    safe = _normalize_rel_path(rel_path)
    if safe is None:
        raise ValidationError(
            "codeproject.invalid_file_path",
            f"{rel_path!r} is not a valid project-relative file path",
        )
    return safe


# Path segments dropped when a snapshot tar is expanded for the BROWSER read-back.
# ``snapshot_project`` tars the whole workspace with no excludes (right for a VM
# restore, which wants a byte-identical tree), but every tree here is regenerable
# or meaningless in a tab: ``npm install`` restores ``node_modules``, a build
# restores ``dist``/``build``/``.next``/``.svelte-kit``, ``.turbo``/``.cache``/
# ``coverage`` are derived artifacts, and ``.git`` is a binary object store the
# in-tab runtime has no client for. They are also the bulk of the bytes — shipping
# them through a JSON string map would be hundreds of MB the browser throws away.
# Not applied to the VM restore path, which legitimately wants the whole tree.
_SNAPSHOT_EXCLUDED_SEGMENTS = frozenset(
    {
        "node_modules",
        ".git",
        "dist",
        "build",
        ".next",
        ".svelte-kit",
        ".turbo",
        ".cache",
        "coverage",
    }
)


def _is_excluded_snapshot_path(rel_path: str) -> bool:
    """True when any segment of ``rel_path`` names a regenerable/irrelevant tree."""
    return any(seg in _SNAPSHOT_EXCLUDED_SEGMENTS for seg in rel_path.split("/"))


def _tar_member_rel_path(name: str) -> str | None:
    """Jail a TAR member name into the overlay's relpath shape; ``None`` to skip.

    A tar is untrusted input: it is expanded into the user's in-tab filesystem, so
    a member that names an absolute path or climbs out of the workspace must never
    become a write. Snapshot tars are built with ``-C <workdir> .``, so ordinary
    members arrive as ``./src/app.ts`` and normalize to the same ``src/app.ts`` key
    the overlay uses — which is what lets the two tiers merge by path at all.
    Backslashes are rejected outright rather than normalized: they are not a path
    separator here, so a ``..\\..\\x`` member would slip past the segment check.
    """
    raw = (name or "").strip()
    if raw.startswith("/") or "\\" in raw:
        return None
    return _normalize_rel_path(raw)


def _expand_snapshot_tar(data: bytes, *, project_id: str, cap: int) -> tuple[dict[str, str], int]:
    """Expand a snapshot tarball in memory into ``({relpath: text}, total bytes)``.

    The BASELINE tier of the browser read-back — the in-memory counterpart of
    ``restore_project``'s ``tar -xzf`` into a VM. Written by ``tar -czf`` so the
    blob is gzipped; ``r:*`` auto-detects anyway.

    Three classes of member never make it into the payload:
      * non-regular entries (directories, symlinks, hardlinks, devices) — a
        directory carries no content and a link is a tar-smuggling vector with no
        meaning in a browser FS;
      * unsafe names (absolute or ``..``-traversing) and regenerable trees
        (``_SNAPSHOT_EXCLUDED_SEGMENTS``);
      * non-UTF-8 blobs — the transport is a JSON string map, so a binary file
        can't be carried. Skipped with a warning rather than failing the whole
        restore, mirroring how the overlay read-back tolerates one bad entry.

    The byte cap is enforced from the member HEADER before anything is read, so a
    huge (or zip-bomb) snapshot bounds memory instead of consuming it. Exceeding it
    raises LOUD (413) — see ``read_project_overlay`` for why a truncated payload is
    the one outcome to avoid.
    """
    files: dict[str, str] = {}
    total = 0
    skipped_unsafe = skipped_excluded = skipped_binary = 0
    # A corrupt or truncated archive raises on open OR mid-iteration; both are the
    # same failure — the baseline is unreadable — and both must be LOUD. Returning
    # the overlay alone would look like a project that lost most of its files.
    # ``EOFError``/``OSError`` join ``TarError`` because the decompressor, not
    # tarfile, is what fails on a truncated gzip stream (``gzip.BadGzipFile`` is an
    # OSError, a half-written blob raises EOFError).
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
        with archive:
            for member in archive:
                if not member.isfile():
                    continue
                rel_path = _tar_member_rel_path(member.name)
                if rel_path is None:
                    skipped_unsafe += 1
                    logger.warning(
                        "codeproject.read_overlay: skipping unsafe snapshot entry for "
                        "project=%s name=%r",
                        project_id,
                        member.name,
                    )
                    continue
                if _is_excluded_snapshot_path(rel_path):
                    skipped_excluded += 1
                    continue
                if total + member.size > cap:
                    raise CloudError(
                        413,
                        "codeproject.overlay_too_large",
                        f"Project files total over the {cap / 1024 / 1024:.0f} MB limit",
                    )
                handle = archive.extractfile(member)
                blob = handle.read() if handle is not None else b""
                try:
                    files[rel_path] = blob.decode("utf-8")
                except UnicodeDecodeError:
                    skipped_binary += 1
                    continue
                total += len(blob)
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise with_cause(
            CloudError(
                502,
                "codeproject.snapshot_unreadable",
                "The project's stored snapshot could not be read",
            ),
            exc,
        ) from exc
    logger.debug(
        "codeproject.read_overlay: snapshot expanded for project=%s -> %d file(s), %d bytes "
        "(skipped %d excluded, %d binary, %d unsafe)",
        project_id,
        len(files),
        total,
        skipped_excluded,
        skipped_binary,
        skipped_unsafe,
    )
    return files, total


async def put_project_file(
    workspace_id: str,
    user_id: str,
    project_id: str,
    rel_path: str,
    content: str,
    *,
    uploads: Any = None,
) -> tuple[str, str]:
    """Persist one browser-written file; return ``(normalized path, FileRecord id)``.

    The write half of the in-tab loop, and the counterpart of
    ``mirror_file_to_project`` for a runtime whose filesystem the backend can't
    read: the client hands us the text it just wrote to its in-tab FS and we
    write it through to the tenant's blob storage + the project's overlay, so the
    edit outlives the tab. Delegates to ``mirror_file_to_project`` so both
    runtimes share ONE upload + pointer path (and therefore one S3 fail-closed
    guard, one blob folder, one last-write-wins rule per path) — the only things
    added here are the wire-facing concerns: path jailing and a size ceiling.

    Text only, because the transport is a JSON string (the in-tab FS deals in
    source files). Encoded UTF-8 so the read-back is byte-identical.
    """
    _require_s3_for_project_store()  # fail closed before validating or uploading
    safe_path = _require_safe_rel_path(rel_path)
    data = content.encode("utf-8")

    cap = _overlay_max_bytes()
    if len(data) > cap:
        raise CloudError(
            413,
            "codeproject.file_too_large",
            f"{safe_path!r} is {len(data) / 1024 / 1024:.1f} MB, over the "
            f"{cap / 1024 / 1024:.0f} MB per-file limit",
        )

    file_id = await mirror_file_to_project(
        workspace_id, user_id, project_id, safe_path, data, uploads=uploads
    )
    return safe_path, file_id


async def drop_project_file(
    workspace_id: str,
    user_id: str,
    project_id: str,
    rel_path: str,
) -> str:
    """Drop a browser-deleted file from the project overlay; return the dropped path.

    Normalizes through the SAME ``_require_safe_rel_path`` as the write so the
    key actually matches what the write stored — a delete that missed would leave
    the file to reappear on the next restore, which is the exact bug this tier
    exists to prevent. Delegates the pointer write to ``drop_project_overlay``
    (and through it to the service, Rule 2). A directory path drops every child.
    """
    safe_path = _require_safe_rel_path(rel_path)
    await drop_project_overlay(workspace_id, user_id, project_id, safe_path)
    return safe_path


async def read_project_overlay(
    workspace_id: str,
    user_id: str,
    project_id: str,
    *,
    uploads: Any = None,
) -> dict[str, str]:
    """Read a project's whole durable state back as ``{relpath: content}``.

    The restore payload for the in-tab runtime, and the read-side counterpart of
    ``restore_project`` — which is exactly why it composes the SAME two tiers, in
    the same order, instead of writing them into a VM:
      1. ``snapshot_file_id`` — the full-workspace tarball a Daytona session wrote
         on disconnect. Fetched from blob storage and expanded IN MEMORY as the
         baseline, minus the trees a browser can't use (see
         ``_expand_snapshot_tar``).
      2. ``overlay`` — the per-file write-through tier, applied ON TOP so the
         freshest bytes win, exactly as the VM replay orders them.

    Both tiers are read because a project is not bound to one runtime (B4). Reading
    only the overlay meant a project last touched in DAYTONA came back empty here —
    ``set_project_snapshot`` clears the overlay once its contents are inside the tar
    — so reopening it in a tab re-materialized the bare scaffold and the user's work
    looked deleted while it sat safe in S3. A project that never saw a VM has no
    snapshot and this collapses to the overlay-only behaviour it always had.

    Owner-scoped through ``get_project`` (a project the caller doesn't own reads as
    ``NotFound`` before any blob is touched), and every blob — tar and overlay entry
    alike — comes back through the same owner-scoped ``stream(...)`` path
    ``_download_snapshot`` wraps, so a foreign or vanished blob can't be read out of
    this endpoint.

    Caps stay LOUD (413) rather than truncating, INCLUDING for the snapshot-derived
    baseline: filtering already removed everything that isn't user work, so what is
    left over the cap IS the user's project, and a silently partial restore looks
    like their files were deleted. A 413 that says the project outgrew the in-browser
    runtime is recoverable — it still opens in the VM runtime, whose restore has no
    such cap. What IS dropped silently is only the non-user-work classes: excluded
    trees, and individually unreadable/undecodable entries (a reaped blob, a binary
    file the JSON transport can't carry), which are skipped with a warning so one bad
    file doesn't sink the rest — the same rule ``restore_project`` replays under.
    """
    project = await codeproject_service.get_project(workspace_id, user_id, project_id)
    overlay = project.overlay or {}
    if not project.snapshot_file_id and not overlay:
        return {}

    max_files = _overlay_max_files()
    if len(overlay) > max_files:
        raise CloudError(
            413,
            "codeproject.overlay_too_many_files",
            f"Project holds {len(overlay)} persisted files, over the {max_files} file limit",
        )

    up = uploads if uploads is not None else build_uploads()
    cap = _overlay_max_bytes()
    files: dict[str, str] = {}
    total = 0

    # Tier 1 — the snapshot tar (baseline). A download or expansion failure raises:
    # the baseline going missing is not "one bad file", it is most of the project.
    if project.snapshot_file_id:
        snapshot_bytes = await _download_snapshot(
            up, project.snapshot_file_id, workspace_id=workspace_id, user_id=user_id
        )
        files, total = _expand_snapshot_tar(snapshot_bytes, project_id=project_id, cap=cap)

    # Tier 2 — the overlay, applied on top (freshest wins).
    for rel_path in sorted(overlay):
        file_id = overlay[rel_path]
        try:
            data = await _download_snapshot(up, file_id, workspace_id=workspace_id, user_id=user_id)
        except CloudError:
            logger.warning(
                "codeproject.read_overlay: unreadable blob for project=%s path=%r file=%s",
                project_id,
                rel_path,
                file_id,
            )
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning(
                "codeproject.read_overlay: skipping non-UTF-8 entry for project=%s path=%r",
                project_id,
                rel_path,
            )
            continue
        # The baseline copy is superseded, so its bytes leave the budget with it —
        # a file present in both tiers must not be counted twice against the cap.
        superseded = len(files[rel_path].encode("utf-8")) if rel_path in files else 0
        total += len(data) - superseded
        if total > cap:
            raise CloudError(
                413,
                "codeproject.overlay_too_large",
                f"Project files total over the {cap / 1024 / 1024:.0f} MB limit",
            )
        files[rel_path] = text

    if len(files) > max_files:
        raise CloudError(
            413,
            "codeproject.overlay_too_many_files",
            f"Project holds {len(files)} persisted files, over the {max_files} file limit",
        )
    logger.debug(
        "codeproject.read_overlay: project=%s -> %d file(s), %d bytes (snapshot=%s)",
        project_id,
        len(files),
        total,
        project.snapshot_file_id,
    )
    return files


__all__ = [
    "build_uploads",
    "drop_overlay",
    "drop_project_file",
    "drop_project_overlay",
    "mirror_file",
    "mirror_file_to_project",
    "move_overlay",
    "move_project_overlay",
    "put_project_file",
    "read_project_overlay",
    "restore_project",
    "restore_workspace",
    "snapshot_project",
    "snapshot_workspace",
]
