# files.py — /files surface preamble.
#
# Created: 2026-05-24 — Lists the workspace's most-recent files via
# ``UnifiedFilesService`` so the agent can answer "what files do I
# have?" with real names rather than handwaving. Tenancy enforced by
# the service.

from __future__ import annotations

import logging

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers._helpers import truncate_preamble

logger = logging.getLogger(__name__)

LIST_LIMIT = 10


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str:
    """Render the files surface preamble."""
    try:
        from pocketpaw_ee.cloud.files.service import UnifiedFilesService

        svc = UnifiedFilesService()
        files, _warnings = await svc.list_unified(workspace_id, source=None, limit=LIST_LIMIT)
    except Exception:
        logger.debug("files_handler: list_unified failed", exc_info=True)
        return (
            '<surface kind="files" route="/files" /><files-snapshot>(unavailable)</files-snapshot>'
        )

    parts = [
        '<surface kind="files" route="/files" />',
        f'<files-snapshot count="{len(files)}" />',
    ]
    if not files:
        parts.append("<files-list>(no files yet)</files-list>")
    else:
        rows = []
        for f in files[:LIST_LIMIT]:
            name = getattr(f, "filename", None) or "(unnamed)"
            mime = getattr(f, "mime", None) or "?"
            rows.append(f"- {name} ({mime})")
        parts.append("<files-list>\n" + "\n".join(rows) + "\n</files-list>")
    return truncate_preamble("\n".join(parts))


__all__ = ["build_preamble"]
