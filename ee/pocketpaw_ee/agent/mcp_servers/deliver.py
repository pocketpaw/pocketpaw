# deliver.py — in-process MCP server exposing ``deliver_artifact`` to the cloud
# chat agent (claude_agent_sdk). Created: 2026-06-26 (ART-4).
#
# This is the payoff of the cloud-artifacts stack: a cloud agent builds a file
# (or a whole directory) inside its per-tenant jail (ART-2), then calls this tool
# to LAND that artifact in the tenant's blob storage and get back a real,
# short-lived download URL to hand the user — instead of printing a container
# path or spinning a 127.0.0.1 preview server the user can never reach.
#
# Mirrors the sibling mcp_servers (sites.py / media.py): a single
# ``create_sdk_mcp_server`` behind an SDK import-guard, ``SERVER_NAME`` /
# ``*_TOOL_ID`` allowlist constants, and ContextVar-sourced identity (the same
# ``current_workspace_id`` / ``current_user_id`` accessors in
# ``ee.cloud.chat.agent_service`` the sites + tasks servers read). The tool id
# namespaces as ``mcp__pocketpaw_deliver__deliver_artifact`` so the Claude Code
# allowlist machinery matches it.
#
# Routing: a single file is uploaded directly (mime guessed from the filename); a
# directory is zipped (``application/zip``) and the zip is uploaded. Both go
# through ``EEUploadService.upload`` — the DOWNLOAD path that gives arbitrary
# mime + a presigned URL + ``FileReady``→KB + visibility in the tenant's /files
# (NOT file_versions.write_file, which is the editor-document path, Slice-D).
#
# Security: the path MUST resolve to inside the caller's own jail
# (``workspace_jail_root()/<workspace_id>/...``). ``..``, absolute paths, and
# symlinks pointing out are all rejected (resolve() then check it's under the
# workspace root), reusing ART-2's path-segment guard so the agent can never
# deliver ``/etc/passwd`` or another tenant's jail. Because the upload relaxes
# the mime allowlist, a delivered non-inline type (HTML/SVG/JS) is served as a
# DOWNLOAD, not inline — EEUploadService.presigned_get forces
# ``Content-Disposition: attachment`` for anything outside INLINE_MIMES, so a
# delivered .html can't render active content on the storage origin. The whole
# server is gated on is_multi_tenant_cloud() (cloud-only, like the ART-4 boot
# guard and ART-2 jail).
"""Agent-side MCP surface for delivering a built artifact to tenant blob storage.

Tool registered:

  - ``deliver_artifact(path)`` — persist the file or directory at ``path``
    (inside the caller's workspace jail) to the tenant's blob storage and return
    ``{ok, filename, url, file_id, size, mime, expires_in_seconds}``. ``url`` is a
    short-TTL download link the agent shows the user. ``is_error`` is set (with a
    plain reason) when identity is missing, the path escapes the jail, the file
    is missing / too large, or the upload fails — the agent then surfaces the
    reason instead of fabricating a working link.

Workspace / user identity comes from the per-stream ``ContextVar``s in
``ee.cloud.chat.agent_service`` (same chokepoint the sites + tasks MCP servers
use). Run outside an SSE chat stream, the tool returns a clear error rather than
silently mis-tenanting the delivered file.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_deliver"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
# Allowlist entries must use this exact form.
DELIVER_ARTIFACT_TOOL_ID = f"mcp__{SERVER_NAME}__deliver_artifact"

DELIVER_TOOL_IDS = (DELIVER_ARTIFACT_TOOL_ID,)

# Default per-artifact size cap. Build artifacts (a zipped site, a generated
# bundle) routinely exceed the 25 MiB browser-upload default, so the deliver
# path carries its own, larger, configurable cap.
_DEFAULT_DELIVER_MAX_MB = 100.0
_TOOL_DESCRIPTION = (
    "Deliver a built artifact to the user as a downloadable file. Pass `path` — "
    "a file OR a directory inside your workspace. A single file is uploaded as-is; "
    "a directory is zipped first. Returns {ok, filename, url, file_id, size, mime, "
    "expires_in_seconds} — show the user the `url` (a short-lived download link) "
    "and the filename. USE THIS whenever you produce something the user should be "
    "able to download (a report, an export, a generated file, a built site bundle): "
    "do NOT just print the path you wrote to, and do NOT start a local preview / web "
    "server — the user cannot reach your container's filesystem or 127.0.0.1. "
    "ok=false with an error means the path was missing, escaped your workspace, was "
    "too large, or the upload failed; relay the error, do NOT report a fake link."
)


class _JailEscape(Exception):
    """The requested path resolved outside the caller's workspace jail."""


def _error_response(message: str) -> dict[str, Any]:
    """Build an MCP error response in the shape Claude's SDK expects. The agent
    reads ``text`` and surfaces the reason."""
    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "is_error": True,
    }


def _success_response(body: dict[str, Any]) -> dict[str, Any]:
    """Build an MCP success response carrying ``body`` as JSON."""
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(body, separators=(",", ":"), default=str),
            }
        ]
    }


def _identity() -> tuple[str | None, str | None]:
    """Resolve the active workspace + user id from the per-stream ContextVars set
    by the cloud chat agent runtime. Returns ``(workspace_id, user_id)``."""
    try:
        from pocketpaw_ee.cloud.chat.agent_service import current_user_id, current_workspace_id

        return current_workspace_id(), current_user_id()
    except Exception:  # noqa: BLE001
        return None, None


def _deliver_max_bytes() -> int:
    """Per-artifact size cap in bytes (``POCKETPAW_DELIVER_MAX_MB``, default 100)."""
    raw = os.environ.get("POCKETPAW_DELIVER_MAX_MB", "").strip()
    mb = _DEFAULT_DELIVER_MAX_MB
    if raw:
        try:
            mb = float(raw)
        except ValueError:
            logger.warning("ignoring non-numeric POCKETPAW_DELIVER_MAX_MB=%r; using %s", raw, mb)
    return int(mb * 1024 * 1024)


def _too_large_message(size: int, cap: int) -> str:
    return (
        f"artifact is {size / 1024 / 1024:.1f} MB, over the "
        f"{cap / 1024 / 1024:.0f} MB deliver limit. Reduce it (deliver a subset) "
        "or raise POCKETPAW_DELIVER_MAX_MB."
    )


def _resolve_in_jail(path_arg: str, workspace_id: str) -> Path:
    """Resolve ``path_arg`` and assert it lives inside the caller's workspace jail.

    Relative paths resolve against the agent's session cwd (the jail dir ART-2
    set as the agent's working directory); absolute paths are taken as-is. The
    resolved path (symlinks followed) MUST be the workspace jail root
    ``workspace_jail_root()/<workspace_id>`` or live under it — otherwise a
    crafted ``..``, an absolute path, or a symlink pointing out would let the
    agent read another tenant's jail or the host filesystem. Raises
    :class:`_JailEscape` on any escape.
    """
    # ``_safe_segment`` is ART-2's path-segment guard (no separators, no ``.`` /
    # ``..``); reuse it so the workspace root we build can't itself be a traversal.
    from pocketpaw_ee.cloud.agent_jail import (
        _safe_segment,
        resolve_agent_cwd,
        workspace_jail_root,
    )

    try:
        ws_segment = _safe_segment(workspace_id, label="workspace_id")
    except ValueError as exc:
        raise _JailEscape("workspace identity is not a safe path segment") from exc

    ws_root = (workspace_jail_root() / ws_segment).resolve()

    # Relative paths are relative to the agent's cwd == its session jail (ART-2).
    # resolve_agent_cwd() returns that exact dir; fall back to the workspace root
    # if it can't be resolved (e.g. no session bound).
    base: Path
    try:
        cwd = resolve_agent_cwd()
        base = Path(cwd) if cwd else ws_root
    except Exception:  # noqa: BLE001 — never let cwd resolution break delivery
        base = ws_root

    candidate = Path(path_arg)
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise _JailEscape(f"could not resolve path: {path_arg}") from exc

    if resolved != ws_root and ws_root not in resolved.parents:
        raise _JailEscape(
            "path is outside your workspace — deliver_artifact only delivers "
            "files inside your own workspace directory."
        )
    return resolved


def _dir_size_skipping_symlinks(src: Path) -> int:
    """Total size of the regular files under ``src``, never following symlinks.

    Symlinks are skipped (not measured, not archived) so a link pointing out of
    the jail can neither inflate the size nor smuggle its target into the zip."""
    total = 0
    for root, dirs, files in os.walk(src, followlinks=False):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for name in files:
            fp = os.path.join(root, name)
            if os.path.islink(fp):
                continue
            try:
                total += os.path.getsize(fp)
            except OSError:
                continue
    return total


def _zip_dir_to_tempfile(src: Path) -> Path:
    """Zip ``src`` into a temp file (OUTSIDE the jail) and return its path.

    Walks without following symlinks and skips symlink entries entirely so a
    link inside the directory can't drag its out-of-jail target into the archive.
    The temp file lives in the system temp dir, not the jail, so it never
    counts against the jail quota or re-enters a future delivery. Caller unlinks
    it after the upload.
    """
    fd, tmp_name = tempfile.mkstemp(suffix=".zip", prefix="paw-deliver-")
    os.close(fd)
    tmp_path = Path(tmp_name)
    parent = src.parent  # arcnames become ``<dirname>/...``
    # The mkstemp file exists NOW, and the caller only binds it for cleanup
    # AFTER this returns — so a failure mid-walk (a vanished file, a perms
    # error, disk-full) would leak it. Clean it up here on any failure.
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(src, followlinks=False):
                dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
                for name in files:
                    fp = os.path.join(root, name)
                    if os.path.islink(fp):
                        continue
                    zf.write(fp, arcname=os.path.relpath(fp, parent))
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path


async def _upload_and_sign(
    upload_path: Path,
    *,
    filename: str,
    mime: str,
    workspace_id: str,
    user_id: str,
    deliver_max: int,
) -> tuple[Any, str]:
    """Upload ``upload_path`` through ``EEUploadService`` and return ``(rec, url)``.

    Builds a delivery-scoped service per call (cheap, like ``write_text_file``):
    the workspace-scoped ``StorageAdapter`` (local or S3 via
    ``POCKETPAW_UPLOAD_ADAPTER``), a Mongo metadata store, and an
    ``UploadSettings`` whose ``allowed_mimes`` includes the computed mime (+ zip)
    so the OSS pipeline's mime gate never rejects a first-party artifact — while
    its magic-byte sniff still upgrades recognized types (png/pdf/…) to their
    canonical mime. ``url`` is the adapter's presigned URL when available (S3),
    else the authenticated cloud download path.
    """
    from fastapi import UploadFile

    from pocketpaw.uploads.config import DEFAULT_ALLOWED_MIMES, UploadSettings
    from pocketpaw.uploads.factory import build_adapter
    from pocketpaw.uploads.signing import DEFAULT_TTL_SECONDS
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore
    from pocketpaw_ee.cloud.uploads.service import EEUploadService

    root = Path.home() / ".pocketpaw" / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    adapter = build_adapter(root)
    cfg = UploadSettings(
        max_file_bytes=deliver_max,
        allowed_mimes=frozenset({mime, "application/zip", *DEFAULT_ALLOWED_MIMES}),
        local_root=root,
    )
    svc = EEUploadService(adapter=adapter, meta=MongoFileStore(), cfg=cfg)

    with open(upload_path, "rb") as fh:
        upload = UploadFile(
            file=fh,
            filename=filename,
            headers={"content-type": mime},  # type: ignore[arg-type]
        )
        rec = await svc.upload(upload, owner_id=user_id, chat_id=None, workspace=workspace_id)

    # Owner-scoped presign (the uploader is the requester, so the read gate
    # passes). Fall back to the authenticated cloud download path when the
    # adapter can't presign (local adapter).
    _rec, url = await svc.presigned_get(rec.id, user_id, workspace_id, DEFAULT_TTL_SECONDS)
    return rec, url or f"/api/v1/uploads/{rec.id}"


async def _deliver_handler(args: dict) -> dict:
    """MCP handler for ``deliver_artifact`` — resolve, jail-check, route, upload."""
    from pocketpaw.uploads.errors import UploadError
    from pocketpaw.uploads.signing import DEFAULT_TTL_SECONDS

    workspace_id, user_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "deliver_artifact requires workspace and user context (call from a cloud chat session)."
        )

    path_arg = args.get("path")
    if not isinstance(path_arg, str) or not path_arg.strip():
        return _error_response(
            "deliver_artifact requires a `path` — the file or directory inside "
            "your workspace to deliver."
        )
    path_arg = path_arg.strip()

    try:
        resolved = _resolve_in_jail(path_arg, workspace_id)
    except _JailEscape as exc:
        return _error_response(str(exc))

    if not resolved.exists():
        return _error_response(f"no such file or directory: {path_arg}")

    deliver_max = _deliver_max_bytes()
    tmp_to_clean: Path | None = None
    try:
        if resolved.is_dir():
            total = _dir_size_skipping_symlinks(resolved)
            if total > deliver_max:
                return _error_response(_too_large_message(total, deliver_max))
            tmp_to_clean = _zip_dir_to_tempfile(resolved)
            upload_path = tmp_to_clean
            filename = f"{resolved.name}.zip"
            mime = "application/zip"
        else:
            size = resolved.stat().st_size
            if size > deliver_max:
                return _error_response(_too_large_message(size, deliver_max))
            upload_path = resolved
            filename = resolved.name
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"

        rec, url = await _upload_and_sign(
            upload_path,
            filename=filename,
            mime=mime,
            workspace_id=workspace_id,
            user_id=user_id,
            deliver_max=deliver_max,
        )
    except UploadError as exc:
        # TooLarge / UnsupportedMime / EmptyFile / StorageFailure — relay cleanly.
        return _error_response(f"could not deliver artifact: {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("deliver_artifact failed", exc_info=True)
        return _error_response(f"deliver failed: {exc}")
    finally:
        if tmp_to_clean is not None:
            tmp_to_clean.unlink(missing_ok=True)

    return _success_response(
        {
            "ok": True,
            "filename": rec.filename,
            "url": url,
            "file_id": rec.id,
            "size": rec.size,
            "mime": rec.mime,
            "expires_in_seconds": DEFAULT_TTL_SECONDS,
        }
    )


def build_deliver_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for artifact delivery, or ``None`` if
    delivery isn't available (not multi-tenant cloud, or the SDK is missing).

    Matches the shape returned by ``build_sites_manager_server`` /
    ``build_pocket_context_server`` (``(name, server)`` or ``None``) so the
    backend's MCP registration loop in ``claude_sdk.py`` treats it identically.

    Cloud-only gate: deliver lands artifacts in tenant blob storage and resolves
    paths against the per-tenant jail, both of which only exist in multi-tenant
    cloud. Gating on ``is_multi_tenant_cloud()`` makes the cloud-only intent
    explicit — the same signal the ART-4 boot guard and the ART-2 jail read —
    rather than relying solely on the tool's fail-closed-without-identity guard.
    The per-run MCP build (``claude_sdk._get_mcp_servers``) happens after the
    cloud DB is initialized, so this never hides the tool from a real cloud run.
    """
    from pocketpaw_ee.cloud.shared.db import is_multi_tenant_cloud

    if not is_multi_tenant_cloud():
        logger.debug("not multi-tenant cloud; pocketpaw_deliver MCP disabled")
        return None

    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_deliver MCP disabled")
        return None

    @tool(
        "deliver_artifact",
        _TOOL_DESCRIPTION,
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "File or directory inside your workspace to deliver. A "
                        "directory is zipped before upload."
                    ),
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    )
    async def deliver_artifact(args):  # type: ignore[no-untyped-def]
        return await _deliver_handler(args)

    server = create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=[deliver_artifact])
    return SERVER_NAME, server


__all__ = [
    "DELIVER_ARTIFACT_TOOL_ID",
    "DELIVER_TOOL_IDS",
    "SERVER_NAME",
    "build_deliver_server",
]
