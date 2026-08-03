# knowledge.py — /knowledge surface preamble.
#
# Created: 2026-05-24 — KB scope listing for the knowledge surface.
# Updated: 2026-05-24 — Wired through ``ee.cloud.kb.service.list_scopes``
# so the preamble renders the real workspace + pocket + agent scopes
# the kb-go store actually carries, rather than the
# ``[f"workspace:{workspace_id}"]`` synthetic fallback. The
# kb-unreachable path still degrades to "(no scopes detected)" so a
# missing kb binary doesn't break the chat send.
#
# Changes: 2026-08-03 (feat/prompt-entity-suffix) — renders through
# ``unaddressed_line("kb_scope", ...)`` rather than a bare f-string. A KB scope
# is one of the cases where the label genuinely IS the address — ``workspace:w1``
# is what a caller passes — so there is no id to add, and this handler wants the
# no-id renderer rather than an exemption from the entity-row rule. Stating it
# through the renderer means the claim gets checked against the tool schemas on
# every run instead of sitting in an allow-list nobody rereads.
#
# Changes: 2026-08-02 (PA-2, feat/prompt-assembler-seam) — returns a
# ``SurfacePreamble``. Mutable state, read as a LIST (the workspace's real KB
# scopes), so the key is a digest of what was rendered: it moves when a scope
# appears or disappears and holds still otherwise. Note that the kb-unreachable
# fall-back renders the same "(no scopes detected)" text as a genuinely empty
# workspace and therefore keys the same — correct, because it produces the same
# prompt, and the digest exists to track the prompt, not the reason for it.

from __future__ import annotations

import logging

from pocketpaw.prompt.entity import unaddressed_line
from pocketpaw_ee.cloud.kb import service as kb_service
from pocketpaw_ee.cloud.surface.domain import SurfaceMeta, SurfacePreamble
from pocketpaw_ee.cloud.surface.handlers._helpers import content_key, truncate_preamble

logger = logging.getLogger(__name__)


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> SurfacePreamble:
    """Render the knowledge-surface preamble."""
    scopes = await _list_scopes(workspace_id, user_id)
    parts = ['<surface kind="knowledge" route="/knowledge" />']
    if not scopes:
        parts.append("<knowledge-snapshot>(no scopes detected)</knowledge-snapshot>")
    else:
        rows = [unaddressed_line("kb_scope", s) for s in scopes[:10]]
        body = "\n".join(rows)
        parts.append(f'<knowledge-scopes count="{len(scopes)}">\n{body}\n</knowledge-scopes>')
    text = truncate_preamble("\n".join(parts))
    return SurfacePreamble(text=text, cache_key=content_key("knowledge", text))


async def _list_scopes(workspace_id: str, user_id: str) -> list[str]:
    """Resolve the workspace's real KB scopes via the canonical service.

    Failures are isolated — the kb stack is optional in some deploys and
    a probe outage must never crash the preamble. We log and fall back
    to ``[]`` so the handler renders ``(no scopes detected)`` instead.
    """
    if not workspace_id:
        return []
    try:
        return await kb_service.list_scopes(workspace_id, user_id)
    except Exception:
        logger.exception(
            "kb_service.list_scopes failed for workspace=%s; emitting empty list",
            workspace_id,
        )
        return []


__all__ = ["build_preamble"]
