# files.py — /files surface preamble.
#
# Created: 2026-05-24 — Lists the workspace's most-recent files via
# ``UnifiedFilesService`` so the agent can answer "what files do I
# have?" with real names rather than handwaving. Tenancy enforced by
# the service.
#
# Changes: 2026-09-14 (fix/attachment-not-on-disk) — renders through
# ``entity_line``, WITH the id. THE TRIPWIRE FIRED, exactly as designed.
#
# The note that stood here said "NO TOOL TAKES A FILE ID", checked against every
# MCP schema, and pointed at ``tests/cloud/surface/test_entity_id_contract.py``
# as the guard that would fail "the day a tool takes a ``file_id``". That day is
# this commit: ``pocketpaw_files.read_upload`` takes a REQUIRED ``file_id``, so
# the contract test stopped passing and this handler had to be looked at rather
# than quietly under-informing the agent. It worked; leave the mechanism alone.
#
# Why the id has to be here now: uploads live in object storage, and an
# attachment is inlined into the prompt only for the turn it arrived on. From
# the next turn, ``read_upload`` is the only way back to a document — and a row
# that names a file without its id tells the agent a thing exists while
# withholding the one value the tool needs.
#
# ``source`` rides along as a fact because this listing MERGES sources (uploads,
# drive, local, kb) and only ``uploads`` ids resolve in ``read_upload``. Without
# it the agent cannot tell which rows it can actually open, and a drive row
# looks exactly like an upload.
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
        page = await svc.list_unified(workspace_id, source=None, limit=LIST_LIMIT)
        files = page.files
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
                    source=getattr(getattr(f, "source", None), "value", None)
                    or getattr(f, "source", None),
                )
            )
        parts.append("<files-list>\n" + "\n".join(rows) + "\n</files-list>")
    text = truncate_preamble("\n".join(parts))
    return SurfacePreamble(text=text, cache_key=content_key("files", text))


__all__ = ["build_preamble"]
