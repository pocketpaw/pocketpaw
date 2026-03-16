# File browser router — directory listing + file content serving.
# Created: 2026-02-20

from __future__ import annotations

import io
import logging
import mimetypes
import urllib.parse
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

from pocketpaw.api.v1.schemas.files import (
    BrowseResponse,
    FileEntry,
    OpenPathRequest,
    OpenPathResponse,
    RecentFilesResponse,
    WriteFileRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Files"])


@router.get("/files/browse", response_model=BrowseResponse)
async def browse_files(path: str = "~"):
    """List files in a directory. Defaults to home directory."""
    from pocketpaw.config import get_settings
    from pocketpaw.tools.fetch import is_safe_path

    settings = get_settings()

    # Resolve path
    if path in ("~", ""):
        resolved_path = Path.home()
    elif not path.startswith("/"):
        resolved_path = Path.home() / path
    else:
        resolved_path = Path(path)

    resolved_path = resolved_path.resolve()
    jail = settings.file_jail_path.resolve()

    # Security check
    if not is_safe_path(resolved_path, jail):
        return BrowseResponse(path=path, error="Access denied: path outside allowed directory")

    if not resolved_path.exists():
        return BrowseResponse(path=path, error="Path does not exist")

    if not resolved_path.is_dir():
        return BrowseResponse(path=path, error="Not a directory")

    # Build file list
    files: list[FileEntry] = []
    try:
        items = sorted(resolved_path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        visible_items = [item for item in items if not item.name.startswith(".")]

        for item in visible_items[:50]:
            entry = FileEntry(name=item.name, isDir=item.is_dir())
            if not item.is_dir():
                try:
                    size = item.stat().st_size
                    if size < 1024:
                        entry.size = f"{size} B"
                    elif size < 1024 * 1024:
                        entry.size = f"{size / 1024:.1f} KB"
                    else:
                        entry.size = f"{size / (1024 * 1024):.1f} MB"
                except Exception:
                    entry.size = "?"
            files.append(entry)

    except PermissionError:
        return BrowseResponse(path=path, error="Permission denied")

    # Display path relative to home
    try:
        rel_path = resolved_path.relative_to(Path.home())
        display_path = str(rel_path) if str(rel_path) != "." else "~"
    except ValueError:
        display_path = str(resolved_path)

    return BrowseResponse(path=display_path, files=files)


@router.post("/files/open", response_model=OpenPathResponse)
async def open_path(req: OpenPathRequest):
    """Push an open_path event to all connected WebSocket clients.

    Validates the path exists and is within the file jail, then broadcasts
    an ``open_path`` WebSocket event so the client navigates to it.
    """
    from pocketpaw.config import get_settings
    from pocketpaw.dashboard_lifecycle import push_open_path
    from pocketpaw.tools.fetch import is_safe_path

    settings = get_settings()
    resolved = Path(req.path).resolve()
    jail = settings.file_jail_path.resolve()

    if not is_safe_path(resolved, jail):
        return OpenPathResponse(ok=False, error="Access denied: path outside allowed directory")

    if not resolved.exists():
        return OpenPathResponse(ok=False, error="Path does not exist")

    action = req.action if req.action in ("navigate", "view") else "navigate"
    await push_open_path(str(resolved), action)
    return OpenPathResponse(ok=True)


_MAX_VIEWABLE_BYTES = 50 * 1024 * 1024  # 50 MB


@router.get("/files/content")
async def get_file_content(path: str):
    """Serve a file's raw content with appropriate MIME type."""
    from pocketpaw.config import get_settings
    from pocketpaw.tools.fetch import is_safe_path

    settings = get_settings()

    if path in ("~", ""):
        raise HTTPException(status_code=400, detail="Cannot serve a directory")

    resolved = _resolve_path(path)
    jail = settings.file_jail_path.resolve()

    if not is_safe_path(resolved, jail):
        raise HTTPException(status_code=403, detail="Access denied: path outside allowed directory")

    if not resolved.exists():
        raise HTTPException(status_code=404, detail="File not found")

    if resolved.is_dir():
        raise HTTPException(status_code=400, detail="Cannot serve a directory")

    if resolved.stat().st_size > _MAX_VIEWABLE_BYTES:
        raise HTTPException(status_code=413, detail="File too large to view (max 50 MB)")

    mime, _ = mimetypes.guess_type(str(resolved))
    if mime is None:
        mime = "application/octet-stream"

    # For text files requested with ?mode=text, return plain text
    # (allows JS to fetch content for the code viewer)
    return FileResponse(str(resolved), media_type=mime)


@router.get("/files/recent", response_model=RecentFilesResponse)
async def get_recent_files(limit: int = 20):
    """Return recently accessed files from agent tool usage."""
    from pocketpaw.recent_files import get_recent_files_tracker

    tracker = get_recent_files_tracker()
    entries = tracker.get_recent(limit=min(limit, 50))
    return RecentFilesResponse(files=entries)


def _resolve_path(path: str) -> Path:
    """Resolve a path string to an absolute Path."""
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate.resolve()
    return (Path.home() / path).resolve()


def _content_disposition(filename: str) -> str:
    """Build an RFC 5987 Content-Disposition header value.

    Uses an ASCII ``filename`` fallback (non-ASCII chars replaced by
    underscores) plus a ``filename*=UTF-8''...`` parameter so that
    filenames with quotes, non-ASCII, or other special characters are
    handled correctly by all modern browsers.
    """
    ascii_name = filename.encode("ascii", "replace").decode("ascii")
    ascii_name = ascii_name.replace('"', "_")
    utf8_name = urllib.parse.quote(filename, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}"


_ZIP_MAX_FILES = 10_000
_ZIP_MAX_BYTES = 500 * 1024 * 1024  # 500 MB


@router.get("/files/download")
async def download_file(path: str):
    """Download a single file as an attachment."""
    from pocketpaw.config import get_settings
    from pocketpaw.tools.fetch import is_safe_path

    settings = get_settings()
    resolved = _resolve_path(path)
    jail = settings.file_jail_path.resolve()

    if not is_safe_path(resolved, jail):
        raise HTTPException(status_code=403, detail="Access denied: path outside allowed directory")
    if not resolved.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if resolved.is_dir():
        raise HTTPException(status_code=400, detail="Use /files/download-zip for directories")

    mime, _ = mimetypes.guess_type(str(resolved))
    if mime is None:
        mime = "application/octet-stream"

    return FileResponse(
        str(resolved),
        media_type=mime,
        headers={"Content-Disposition": _content_disposition(resolved.name)},
    )


@router.get("/files/download-zip")
async def download_dir_as_zip(path: str):
    """Download a directory (recursively) as a zip archive."""
    from pocketpaw.config import get_settings
    from pocketpaw.tools.fetch import is_safe_path

    settings = get_settings()
    resolved = _resolve_path(path)
    jail = settings.file_jail_path.resolve()

    if not is_safe_path(resolved, jail):
        raise HTTPException(status_code=403, detail="Access denied: path outside allowed directory")
    if not resolved.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    if not resolved.is_dir():
        raise HTTPException(
            status_code=400, detail="Not a directory — use /files/download for files"
        )  # noqa: E501

    buf = io.BytesIO()
    file_count = 0
    cumulative_size = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(resolved.rglob("*")):
            if file_path.is_file() and not file_path.name.startswith("."):
                try:
                    fsize = file_path.stat().st_size
                except (PermissionError, OSError):
                    logger.warning(
                        "Skipping unreadable file during zip: %s",
                        file_path,
                    )
                    continue
                file_count += 1
                cumulative_size += fsize
                if file_count > _ZIP_MAX_FILES:
                    raise HTTPException(
                        status_code=413,
                        detail=(f"Too many files (>{_ZIP_MAX_FILES}). Narrow the directory scope."),
                    )
                if cumulative_size > _ZIP_MAX_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"Cumulative file size exceeds {_ZIP_MAX_BYTES // (1024 * 1024)} MB."
                        ),
                    )
                try:
                    zf.write(
                        file_path,
                        file_path.relative_to(resolved),
                    )
                except PermissionError:
                    logger.warning(
                        "Skipping unreadable file during zip: %s",
                        file_path,
                    )
    zip_bytes = buf.getvalue()
    zip_name = f"{resolved.name}.zip"
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": _content_disposition(zip_name)},
    )


@router.post("/files/write")
async def write_file(req: WriteFileRequest):
    """Overwrite a file's content. Only text files within the jail are permitted."""
    from pocketpaw.config import get_settings
    from pocketpaw.tools.fetch import is_safe_path

    settings = get_settings()
    resolved = _resolve_path(req.path)
    jail = settings.file_jail_path.resolve()

    if not is_safe_path(resolved, jail):
        raise HTTPException(
            status_code=403,
            detail="Access denied: path outside allowed directory",
        )
    if not resolved.exists():
        raise HTTPException(
            status_code=404,
            detail="File not found — this endpoint only edits existing files",
        )
    if resolved.is_dir():
        raise HTTPException(status_code=400, detail="Cannot write to a directory")
    if resolved.stat().st_size > _MAX_VIEWABLE_BYTES:
        raise HTTPException(status_code=413, detail="File too large to edit via the browser")

    try:
        resolved.write_text(req.content, encoding="utf-8")
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Write failed: {exc}") from exc

    return {"ok": True}
