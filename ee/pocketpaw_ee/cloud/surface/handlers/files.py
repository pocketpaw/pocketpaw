# files.py — /files surface preamble.
#
# Created: 2026-05-24 — Lists the workspace's most-recent files via
# ``UnifiedFilesService`` so the agent can answer "what files do I
# have?" with real names rather than handwaving. Tenancy enforced by
# the service.
#
# Changes: 2026-08-03 (feat/prompt-entity-ids) — rows render through
# ``pocketpaw.prompt.entity.entity_line`` and carry ``UnifiedFile.id``.
#
# NO TOOL TAKES A FILE ID. Checked, not assumed: enumerating every MCP server's
# schemas on 2026-08-03 found no ``file_id`` parameter anywhere, required or
# optional. So this row is NOT covered by the "a tool requires <kind>_id" rule
# that forced the pocket and widget ids, and the ~30 chars it spends per row buy
# something smaller: two uploads sharing a filename — the same document at two
# revisions, which is the common case, not a corner one — were previously
# indistinguishable in the listing, so the agent could not even TELL THE USER
# there were two, let alone act on one. Disambiguation in the answer, not tool
# addressing.
#
# If that trade stops being worth it, the fix is to drop the id from this call —
# not to hand-roll the row again. The contract test enforces the shape.
#
# Changes: 2026-08-02 (PA-2, feat/prompt-assembler-seam) — returns a
# ``SurfacePreamble``. Mutable state, read as a LIST (the most recent files'
# names and mime types), so the key is a digest of what was rendered: it moves
# the moment an upload lands and holds still across two turns with no new
# files. The unavailable branch reads nothing and gets its own exact key.

from __future__ import annotations

import logging

from pocketpaw.prompt.entity import entity_line
from pocketpaw_ee.cloud.surface.domain import SurfaceMeta, SurfacePreamble
from pocketpaw_ee.cloud.surface.handlers._helpers import (
    content_key,
    meta_key,
    truncate_preamble,
)

logger = logging.getLogger(__name__)

LIST_LIMIT = 10


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> SurfacePreamble:
    """Render the files surface preamble."""
    try:
        from pocketpaw_ee.cloud.files.service import UnifiedFilesService

        svc = UnifiedFilesService()
        files, _warnings = await svc.list_unified(workspace_id, source=None, limit=LIST_LIMIT)
    except Exception:
        logger.debug("files_handler: list_unified failed", exc_info=True)
        return SurfacePreamble(
            text=(
                '<surface kind="files" route="/files" />'
                "<files-snapshot>(unavailable)</files-snapshot>"
            ),
            cache_key=meta_key("files", "unavailable"),
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
            rows.append(
                entity_line(
                    getattr(f, "filename", None),
                    getattr(f, "id", None),
                    mime=getattr(f, "mime", None),
                )
            )
        parts.append("<files-list>\n" + "\n".join(rows) + "\n</files-list>")
    text = truncate_preamble("\n".join(parts))
    return SurfacePreamble(text=text, cache_key=content_key("files", text))


__all__ = ["build_preamble"]
