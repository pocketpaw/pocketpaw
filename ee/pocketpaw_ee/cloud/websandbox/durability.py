# durability.py — Web Cursor workspace durability: S3 snapshot + restore.
# Created 2026-07-15 (WC-S3, feat/websandbox-s3-durability).
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
from __future__ import annotations

import io
import logging
import os
from pathlib import Path
from typing import Any

from pocketpaw_ee.cloud._core.errors import CloudError, ConflictError, with_cause
from pocketpaw_ee.cloud.daytona.client import DaytonaClient, get_daytona_client
from pocketpaw_ee.cloud.websandbox import service as websandbox_service
from pocketpaw_ee.cloud.websandbox.constants import WEBSANDBOX_WORKDIR

logger = logging.getLogger(__name__)

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


def _build_uploads() -> Any:
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
        _rec, chunks = await uploads.stream(
            file_id, requester_id=user_id, workspace=workspace_id
        )
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
        logger.warning(
            "websandbox.snapshot: tar/download failed for row=%s", row_id, exc_info=True
        )
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
    up = uploads if uploads is not None else _build_uploads()
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
    """Restore a row's latest blob-storage snapshot into its (fresh) VM.

    Resolves the row tenant + owner scoped, then fail-closed authorizes on the
    bound Daytona id, reads the row's ``snapshot_file_id`` (a clean 409 when the
    sandbox has never been snapshotted), fetches the tarball bytes back from S3,
    ``upload_bytes`` them into the VM, and ``tar -xzf`` into the workspace dir.
    """
    daytona = _require_client(client)

    row = await websandbox_service.get_sandbox(workspace_id, user_id, row_id)
    if not row.snapshot_file_id:
        raise ConflictError(
            "websandbox.no_snapshot", "This sandbox has no snapshot to restore"
        )
    if not row.sandbox_id:
        raise ConflictError("websandbox.not_ready", "Sandbox is not provisioned yet")

    # Fail-closed authorization on the Daytona id BEFORE touching the runtime.
    await websandbox_service.authorize_sandbox(workspace_id, user_id, row.sandbox_id)

    up = uploads if uploads is not None else _build_uploads()
    data = await _download_snapshot(
        up, row.snapshot_file_id, workspace_id=workspace_id, user_id=user_id
    )

    try:
        await daytona.upload_bytes(row.sandbox_id, data, _SNAPSHOT_TMP)
        await daytona.execute_command(
            row.sandbox_id,
            f"mkdir -p {WEBSANDBOX_WORKDIR} && tar -xzf {_SNAPSHOT_TMP} -C {WEBSANDBOX_WORKDIR}",
        )
    except Exception as exc:  # noqa: BLE001 — any VM-side failure is uniform
        logger.warning(
            "websandbox.restore: upload/untar failed for row=%s", row_id, exc_info=True
        )
        raise with_cause(
            CloudError(502, "websandbox.restore_failed", "Failed to restore the workspace"),
            exc,
        ) from exc

    logger.info(
        "websandbox.restore: row=%s daytona=%s <- file=%s (%d bytes)",
        row_id,
        row.sandbox_id,
        row.snapshot_file_id,
        len(data),
    )


__all__ = [
    "restore_workspace",
    "snapshot_workspace",
]
