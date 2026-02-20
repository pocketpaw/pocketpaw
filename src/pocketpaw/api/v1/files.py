# File browser router — directory listing.
# Created: 2026-02-20

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter

from pocketpaw.api.v1.schemas.files import BrowseResponse, FileEntry

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
