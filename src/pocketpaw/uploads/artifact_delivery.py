# artifact_delivery.py — land a locally-generated agent file into the shared OSS
# uploads store and return the frozen artifact meta the chat contract carries.
# Created: 2026-07-11 (ART-OSS): OSS/local-mode parity for the cloud artifacts
# pipeline. In cloud mode the ``deliver_artifact`` MCP server uploads to the
# tenant blob store and ``run_core`` drains a per-run collector into
# ``{type:"artifact", meta}`` attachments + ``artifact`` SSE events. OSS mode has
# no deliver MCP (the default ``claude_agent_sdk`` backend never runs the OSS
# ``DeliverArtifactTool``; agents write files via Bash/Write and the AgentLoop
# detects them through ``media_paths``). This helper is the OSS equivalent of the
# cloud tool's upload half: given a local file path the agent produced, it copies
# the bytes into the SAME store the ``/api/v1/uploads`` router serves from
# (``~/.pocketpaw/uploads`` + ``_idx.jsonl``, owner ``local``) so the resulting
# ``file_id`` resolves through the client's existing ``/uploads/{id}`` +
# ``/grant`` flow, then hands back ``{file_id, name, mime, size}`` — the exact
# meta shape the frozen artifact attachment/event contract is built against.
#
# Mirrors the cloud ``_upload_artifact`` (ee deliver.py): a per-call service with
# a RELAXED mime allowlist (the file's own guessed mime + zip + the defaults) so
# a first-party artifact (a report, an export, a built bundle) is never rejected
# by the browser-upload mime gate, and a larger per-artifact cap
# (``POCKETPAW_DELIVER_MAX_MB``, default 100). Best-effort: any failure returns
# ``None`` and never raises into the AgentLoop — a store hiccup must not break an
# otherwise-successful turn.

from __future__ import annotations

import logging
import mimetypes
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Same store the OSS uploads router (``api/v1/uploads.py``) serves from, so a
# delivered artifact's ``file_id`` is addressable through ``GET
# /api/v1/uploads/{id}`` and its ``/grant`` sibling with no extra plumbing.
_UPLOADS_ROOT = Path.home() / ".pocketpaw" / "uploads"
_UPLOADS_INDEX = _UPLOADS_ROOT / "_idx.jsonl"
# OSS is single-user; the uploads router owns every row as ``local`` and streams
# with ``requester_id="local"``. Match it so the download/grant routes can serve
# the delivered file back.
_OWNER = "local"

_DEFAULT_DELIVER_MAX_MB = 100.0


def _deliver_max_bytes() -> int:
    """Per-artifact size cap in bytes (``POCKETPAW_DELIVER_MAX_MB``, default 100).

    Shares the env knob with the cloud deliver tool so an operator tunes one
    value for both deployment modes."""
    raw = os.environ.get("POCKETPAW_DELIVER_MAX_MB", "").strip()
    mb = _DEFAULT_DELIVER_MAX_MB
    if raw:
        try:
            mb = float(raw)
        except ValueError:
            logger.warning("ignoring non-numeric POCKETPAW_DELIVER_MAX_MB=%r; using %s", raw, mb)
    return int(mb * 1024 * 1024)


async def upload_local_artifact(path: str) -> dict[str, Any] | None:
    """Upload the local file at ``path`` into the shared OSS uploads store.

    Returns ``{file_id, name, mime, size}`` on success (the frozen artifact meta
    the chat attachment/event contract carries), or ``None`` when the path is
    missing / not a regular file / too large / the upload fails. Never raises:
    the AgentLoop calls this per produced file at persist time and a failure for
    one artifact must not abort the turn.
    """
    from fastapi import UploadFile

    from pocketpaw.uploads.config import DEFAULT_ALLOWED_MIMES, UploadSettings
    from pocketpaw.uploads.factory import build_adapter
    from pocketpaw.uploads.file_store import JSONLFileStore
    from pocketpaw.uploads.service import UploadService

    try:
        file_path = Path(path).expanduser()
        if not file_path.is_file():
            return None

        deliver_max = _deliver_max_bytes()
        try:
            if file_path.stat().st_size > deliver_max:
                logger.info(
                    "artifact %s over the %s MB deliver cap; skipping",
                    file_path.name,
                    deliver_max / 1024 / 1024,
                )
                return None
        except OSError:
            return None

        name = file_path.name
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"

        _UPLOADS_ROOT.mkdir(parents=True, exist_ok=True)
        adapter = build_adapter(_UPLOADS_ROOT)
        cfg = UploadSettings(
            max_file_bytes=deliver_max,
            # Relax the gate like the cloud deliver path: a first-party artifact
            # can be any type the agent produced, not just a browser-upload mime.
            allowed_mimes=frozenset({mime, "application/zip", *DEFAULT_ALLOWED_MIMES}),
            local_root=_UPLOADS_ROOT,
        )
        svc = UploadService(
            adapter=adapter,
            meta=JSONLFileStore(path=_UPLOADS_INDEX),
            cfg=cfg,
        )

        with open(file_path, "rb") as fh:
            upload = UploadFile(
                file=fh,
                filename=name,
                headers={"content-type": mime},  # type: ignore[arg-type]
            )
            rec = await svc.upload(upload, owner_id=_OWNER, chat_id=None)
    except Exception:  # noqa: BLE001 — best-effort; a delivery hiccup never fails the turn
        logger.debug("OSS artifact upload failed for %r", path, exc_info=True)
        return None

    return {
        "file_id": rec.id,
        "name": rec.filename,
        "mime": rec.mime,
        "size": rec.size,
    }


__all__ = ["upload_local_artifact"]
