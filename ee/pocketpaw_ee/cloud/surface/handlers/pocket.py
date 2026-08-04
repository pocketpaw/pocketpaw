# pocket.py — Pocket-surface preamble.
#
# Updated: 2026-08-03 (PA-9, feat/prompt-budget-measurement) — the 12-widget cut
# is no longer a bare literal; it is ``WIDGET_PREVIEW_LIMIT`` in ``_helpers.py``,
# beside ``PREAMBLE_MAX_CHARS``, because the two caps jointly bound this preamble
# and neither could be reasoned about without the other. Measured: a widget line
# is 36.2 chars, so this preamble renders at 609 chars against a 1500 cap. The
# limit stays 12 — not for token cost, which is negligible, but because this
# text is digested into ``cache_key`` below, so each extra widget listed is one
# more edit that invalidates and costs a reconnect. That side is unmeasured.
#
# Updated: 2026-08-02 (PA-2, feat/prompt-assembler-seam) — returns a
# ``SurfacePreamble``: the same text, plus the cache key the ``surface`` prompt
# layer is keyed on. This handler is the reason the key is the HANDLER's answer
# and not something the dispatcher derives from ``(kind, pocket_id, intent)``.
# It reads live pocket data — the widget count, the first 12 widgets, the node
# and backend summaries — so a pocket edited between two turns renders a
# different preamble under an identical kind, id and intent. A key built from
# those three would hold still and a backend caching on it would keep serving a
# prompt describing a pocket that no longer looks like that. Only the handler
# is in a position to notice, so the handler answers.
#
# The key is a digest of the text this handler RENDERED, not of the pocket it
# read, and the distinction decides what a cache invalidation means. The
# preamble carries a SUMMARY — the name, the widget count, the first 12
# widgets, the node and backend summaries — so a pocket edit that touches none
# of those produces a byte-identical preamble. Keying on the pocket would
# invalidate there anyway, and on the Claude SDK backend an invalidation costs
# a ~12s reconnect to hand the agent a prompt it already has. The rendered
# digest moves exactly when the agent's view of the pocket moves, which is the
# only time a cached prompt is actually stale.
#
# Two keys were tried and rejected on the way here, both worth recording:
#
#   * the pocket's ``updatedAt`` — the natural revision, and what
#     ``TimestampedDocument`` plus this service's own comments claim is bumped
#     on every write. It is NOT. Under beanie 2 the timestamp hooks are never
#     registered (``init_actions`` skips ``_``-prefixed attributes; the hooks
#     are ``_set_created`` / ``_set_updated``), so a pocket's ``updatedAt``
#     keeps its creation value for life. A key on it looks correct, reviews
#     clean, and reports every pocket edit as "unchanged".
#   * a fingerprint of every widget read, which fixed that but went too far the
#     other way: it invalidated on edits past the 12-widget cut that the
#     preamble cannot show, buying a reconnect for a prompt that would come
#     back identical.
#
# The two no-pocket paths (no id, unavailable) read nothing mutable, so their
# keys name their inputs exactly instead of digesting anything.
#
# Updated: 2026-05-24 — Added workspace_id tenancy guard. The downstream
# ``pockets_service.get`` gates on owner / shared_with / visibility but
# NOT workspace, so a user who belongs to multiple workspaces could
# stamp a pocket_id from workspace B in a chat from workspace A and the
# preamble would render B's pocket data inside A's chat context. We now
# fetch the pocket, then reject any whose ``workspace`` field doesn't
# match the chat's ``workspace_id``; the request falls back to the
# unavailable-snapshot path that already covers unknown / no-access ids.
#
# Original: When the user is viewing a specific pocket (/pockets/[id])
# the agent should know the pocket's name, widget / node summary, and
# backend wiring state. Bad ``pocket_id`` (missing, deleted, no access,
# or now cross-workspace) returns an empty preamble per the graceful-
# fall-back rule; the chat send keeps going.

from __future__ import annotations

import logging

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta, SurfacePreamble
from pocketpaw_ee.cloud.surface.handlers._helpers import (
    WIDGET_PREVIEW_LIMIT,
    content_key,
    format_widget_line,
    meta_key,
    truncate_preamble,
)

logger = logging.getLogger(__name__)


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> SurfacePreamble:
    """Build the pocket-surface preamble for ``meta.pocket_id``."""
    if not meta.pocket_id:
        # No pocket id supplied — nothing the agent can tell about this
        # surface beyond the route. Emit a minimal preamble so the agent
        # still knows it's on /pockets/[?] without specific context.
        # Reads nothing: the text is a constant, so the key is one too.
        return SurfacePreamble(
            text='<surface kind="pocket" route="/pockets/?" />',
            cache_key=meta_key("pocket", None, "no-id"),
        )

    pocket = await _load_pocket(meta.pocket_id, user_id, workspace_id)
    if pocket is None:
        # Unknown / no-access pocket. Tell the agent we're on a pocket
        # surface but the snapshot is empty — better than a totally bare
        # preamble that hides which surface the user is on.
        # The text is a function of the id alone, and so is the key. Distinct
        # from a LOADED pocket's key by the marker, so "pocket A is gone" and
        # "pocket A is here" can never hash alike.
        return SurfacePreamble(
            text=(
                f'<surface kind="pocket" route="/pockets/{meta.pocket_id}" />'
                "<pocket-snapshot>(pocket unavailable)</pocket-snapshot>"
            ),
            cache_key=meta_key("pocket", meta.pocket_id, "unavailable"),
        )

    name = pocket.get("name") or "(unnamed)"
    pocket_id = pocket.get("_id") or meta.pocket_id
    widgets = pocket.get("widgets", []) or []
    nodes_summary = _summarise_nodes(pocket)
    backend_summary = _summarise_backend(pocket)

    parts = [
        f'<surface kind="pocket" route="/pockets/{pocket_id}" />',
        f'<current-pocket id="{pocket_id}" name="{name}" widgets="{len(widgets)}" />',
    ]
    if widgets:
        rows = [format_widget_line(_AttrDict(w)) for w in widgets[:WIDGET_PREVIEW_LIMIT]]
        if len(widgets) > WIDGET_PREVIEW_LIMIT:
            rows.append(f"... (+{len(widgets) - WIDGET_PREVIEW_LIMIT} more)")
        parts.append(
            f'<pocket-widgets count="{len(widgets)}">\n' + "\n".join(rows) + "\n</pocket-widgets>"
        )
    if nodes_summary:
        parts.append(f"<pocket-nodes>{nodes_summary}</pocket-nodes>")
    if backend_summary:
        parts.append(f"<pocket-backend>{backend_summary}</pocket-backend>")
    text = truncate_preamble("\n".join(parts))

    # The rendered summary IS what the agent is told about this pocket, so it
    # is what the key tracks. An edit the summary does not show leaves the key
    # still — deliberately: re-rendering would produce these same bytes.
    return SurfacePreamble(text=text, cache_key=content_key("pocket", text))


async def _load_pocket(pocket_id: str, user_id: str, workspace_id: str) -> dict | None:
    """Fetch a pocket via the canonical service. Returns ``None`` on any error.

    The catch is broad on purpose — Forbidden, NotFound, validation
    issues all collapse into "no preamble" rather than propagating.

    Tenancy guard: ``pockets_service.get`` gates by owner / shared_with /
    visibility — not workspace. A user in multiple workspaces could stamp
    a pocket from workspace B in a chat from workspace A and the
    preamble would render B's pocket inside A's context. We reject any
    pocket whose ``workspace`` field doesn't match the chat's workspace
    so the cross-workspace bleed-through is impossible.
    """
    try:
        from pocketpaw_ee.cloud.pockets import service as pockets_service

        pocket = await pockets_service.get(pocket_id, user_id)
    except Exception:
        logger.debug("pocket_handler: get(%s) failed", pocket_id, exc_info=True)
        return None
    if pocket.get("workspace") != workspace_id:
        logger.warning(
            "pocket_handler: workspace mismatch for pocket %s (chat=%s, pocket=%s); rejecting",
            pocket_id,
            workspace_id,
            pocket.get("workspace"),
        )
        return None
    return pocket


def _summarise_nodes(pocket: dict) -> str:
    """Count nodes in the pocket's rippleSpec, if any.

    A pocket can carry both a flat ``widgets`` list (the home-grid shape)
    AND a ``rippleSpec.ui`` subtree (the canvas shape). The agent should
    see at least the count; the full subtree blows the budget.
    """
    spec = pocket.get("rippleSpec") or pocket.get("ripple_spec") or {}
    if not isinstance(spec, dict):
        return ""
    ui = spec.get("ui")
    if not isinstance(ui, dict):
        return ""
    children = ui.get("children") or []
    if not isinstance(children, list):
        return ""
    return f"ui_root={ui.get('type', '?')} top_level_nodes={len(children)}"


def _summarise_backend(pocket: dict) -> str:
    """Render the backend config summary tags.

    The pocket wire dict carries ``backend`` (the active config). We
    surface whether one is configured, the base URL (truncated to 60
    chars) and the count of allowed writes — enough for the agent to
    know what tools exist without reading the full spec.
    """
    backend = pocket.get("backend") or {}
    if not isinstance(backend, dict) or not backend:
        return "configured=false"
    base = str(backend.get("base_url") or backend.get("baseUrl") or "")[:60]
    actions = backend.get("allowed_writes") or backend.get("allowedWrites") or []
    parts = ["configured=true"]
    if base:
        parts.append(f"base_url={base}")
    if isinstance(actions, list):
        parts.append(f"allowed_writes={len(actions)}")
    return " · ".join(parts)


class _AttrDict:
    """Attr-access wrapper for ``format_widget_line``. Mirrors the home handler's helper."""

    def __init__(self, source: dict) -> None:
        self._source = source

    def __getattr__(self, key: str):
        return self._source.get(key)


__all__ = ["build_preamble"]
