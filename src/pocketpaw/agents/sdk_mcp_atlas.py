# agents/sdk_mcp_atlas.py — in-process SDK MCP server exposing the atlas
# OS self-model to backends (AT-1). Created: 2026-07-02 (feat/atlas-core).
#
# atlas is the runtime OS self-model: hand-authored capability cards for
# the OS's own primitives (Pocket, Instinct, Fabric, Connector, Ripple,
# Soul, Branch, workspace-jobs, Sites, Belt) in PAW meanings, not
# LLM-default meanings. The ``atlas_search`` / ``atlas_describe`` tools
# let an agent look up what the OS is and can do BEFORE guessing a
# capability from its priors. Pure core: the seed ships as packaged data
# (``pocketpaw.atlas``), no cloud dependency.
#
# Mirrors ``sdk_mcp_widgets.py`` — same server/tool registration shape,
# ``mcp__<server>__<tool>`` id convention, text-block result envelope
# with ``is_error`` on failures, and the same wiring points in
# ``claude_sdk.py`` (``_get_mcp_servers`` + ``_collect_mcp_tool_ids`` +
# the mode-scope grant).
#
# Updated: 2026-07-02 (feat/atlas-overlay, AT-5) — the server takes an
# optional per-run ``EntitlementProvider`` (``atlas/overlay.py``) so
# answers reflect the CALLING workspace: connector cards carry
# ``available`` (connected in this context), unavailable connectors rank
# below available ones at equal relevance, describe points unavailable
# connectors at the integrations surface, and non-granted entries are
# ABSENT everywhere (search, describe, known-ids error — fail-closed, no
# leakage). ``provider=None`` keeps the pre-AT-5 global behavior.
#
# Updated: 2026-07-02 (feat/atlas-fabric, AT-7) — the server also takes an
# optional per-run ``FabricIntrospector`` (``atlas/fabric.py``): live
# workspace-ontology (EE Fabric) introspection. With one, atlas_search
# APPENDS synthetic ``fabric:<entity-type>`` cards (tool-layer kind
# ``fabric``) after compiled-entry results — never displacing them — and
# atlas_describe answers ``fabric:<type>`` ids with the live schema
# (properties + links). Absent (OSS default) or erroring introspector →
# fabric ids are unknown ids and no fabric cards appear (fail-closed);
# workspace ontology is per-tenant and NEVER enters the compiled artifact.

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pocketpaw.atlas.fabric import FabricIntrospector
    from pocketpaw.atlas.overlay import EntitlementProvider

logger = logging.getLogger(__name__)

# Seed id of the surface entry whose route is the integrations page —
# looked up from the store at answer time (the route itself is never
# hard-coded here).
_INTEGRATIONS_SURFACE_ID = "surface:integrations"

SERVER_NAME = "pocketpaw_atlas"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
# Allowlist entries must use this exact form.
ATLAS_SEARCH_TOOL_ID = f"mcp__{SERVER_NAME}__atlas_search"
ATLAS_DESCRIBE_TOOL_ID = f"mcp__{SERVER_NAME}__atlas_describe"

ATLAS_TOOL_IDS = (
    ATLAS_SEARCH_TOOL_ID,
    ATLAS_DESCRIBE_TOOL_ID,
)


def _text_result(text: str, *, is_error: bool = False) -> dict:
    """Shape an SDK MCP tool result from a single text block."""
    out: dict = {"content": [{"type": "text", "text": text}]}
    if is_error:
        out["is_error"] = True
    return out


async def _atlas_search_handler(
    args: dict,
    provider: EntitlementProvider | None = None,
    introspector: FabricIntrospector | None = None,
) -> dict:
    """Rank atlas entries for an intent and return capability cards.

    Backs the ``atlas_search`` MCP tool. Cards are intentionally thin
    (id, kind, name, summary, surface when set) — the agent follows up
    with ``atlas_describe`` on the id it picks. With a *provider* (AT-5),
    results are the calling context's view: non-granted entries are
    absent, connector cards carry ``available``, and available
    connectors rank above unavailable ones at equal relevance. With an
    *introspector* (AT-7), live workspace entity types matching the
    intent are APPENDED as synthetic ``fabric:*`` cards after the
    compiled-entry results — never displacing them; an erroring
    introspector contributes nothing (fail-closed).
    """
    from pocketpaw.atlas.store import get_atlas_store

    intent = args.get("intent")
    if not isinstance(intent, str) or not intent.strip():
        return _text_result(
            "Error: pass `intent` as a non-empty string describing what you "
            "are trying to do (e.g. 'approve agent actions').",
            is_error=True,
        )

    store = get_atlas_store()
    if provider is None:
        overlaid = [(entry, None) for entry in store.search(intent, limit=5)]
    else:
        from pocketpaw.atlas.overlay import AtlasOverlay

        overlaid = [
            (o.entry, o.available) for o in AtlasOverlay.search(store, intent, provider, limit=5)
        ]

    fabric_cards: list[dict[str, Any]] = []
    if introspector is not None:
        # Fail-closed inside: any introspector error yields [] (DEBUG log).
        from pocketpaw.atlas.fabric import search_entity_types

        fabric_cards = search_entity_types(introspector, intent)

    if not overlaid and not fabric_cards:
        return _text_result(f"No atlas entries matched intent: {intent!r}. Try broader wording.")

    cards: list[dict[str, Any]] = []
    for entry, available in overlaid:
        card: dict[str, Any] = {
            "id": entry.id,
            "kind": entry.kind,
            "name": entry.name,
            "summary": entry.summary,
        }
        if entry.surface:
            card["surface"] = entry.surface
        if available is not None:
            card["available"] = available
        cards.append(card)
    # Fabric cards ride AFTER every compiled-entry card (AT-7): live
    # ontology hits are additive context, never displacing OS answers.
    cards.extend(fabric_cards)
    return _text_result(json.dumps({"results": cards}, ensure_ascii=False))


async def _atlas_describe_handler(
    args: dict,
    provider: EntitlementProvider | None = None,
    introspector: FabricIntrospector | None = None,
) -> dict:
    """Return the full atlas entry for a stable id.

    Backs the ``atlas_describe`` MCP tool. Unknown ids return an error
    envelope listing the known ids so the agent can self-correct. With a
    *provider* (AT-5), a non-granted entry answers exactly like an
    unknown id (and the known-ids listing carries only visible ids), so
    describe can never confirm a filtered entry exists; an unavailable
    connector's card carries ``available: false`` plus a pointer to the
    integrations surface (route looked up from the store). With an
    *introspector* (AT-7), ``fabric:<type>`` ids answer with the live
    workspace schema (properties + links); absent or erroring
    introspector, fabric ids fall through to the unknown-id path —
    indistinguishable from ids that never existed (fail-closed).
    """
    from pocketpaw.atlas.store import get_atlas_store

    entry_id = args.get("id")
    if not isinstance(entry_id, str) or not entry_id.strip():
        return _text_result(
            "Error: pass `id` as a non-empty string (e.g. 'primitive:instinct').",
            is_error=True,
        )

    if introspector is not None:
        # Fail-closed inside: a miss OR a raising introspector returns None,
        # and the id falls through to the normal unknown-id error below.
        from pocketpaw.atlas.fabric import describe_fabric_id

        fabric_payload = describe_fabric_id(introspector, entry_id.strip())
        if fabric_payload is not None:
            return _text_result(json.dumps(fabric_payload, ensure_ascii=False))

    store = get_atlas_store()
    if provider is None:
        entry = store.describe(entry_id.strip())
        if entry is None:
            known = ", ".join(sorted(e.id for e in store.entries))
            return _text_result(
                f"Error: unknown atlas id {entry_id!r}. Known ids: {known}",
                is_error=True,
            )
        return _text_result(entry.model_dump_json(by_alias=True))

    from pocketpaw.atlas.overlay import AtlasOverlay

    overlaid = AtlasOverlay.describe(store, entry_id.strip(), provider)
    if overlaid is None:
        # Unknown AND filtered ids land here — same envelope, no leakage.
        known = ", ".join(AtlasOverlay.visible_ids(store, provider))
        return _text_result(
            f"Error: unknown atlas id {entry_id!r}. Known ids: {known}",
            is_error=True,
        )

    payload = overlaid.entry.model_dump(mode="json", by_alias=True)
    if overlaid.available is not None:
        payload["available"] = overlaid.available
        if overlaid.available is False:
            # Route the lookup through the overlay: a provider that filters
            # the integrations surface must not leak its route via the hint.
            integrations = AtlasOverlay.describe(store, _INTEGRATIONS_SURFACE_ID, provider)
            route = integrations.entry.surface if integrations is not None else ""
            if route:
                payload["connect_hint"] = (
                    f"Not connected in this workspace yet — connect it at {route}."
                )
    return _text_result(json.dumps(payload, ensure_ascii=False))


def build_atlas_context_server(
    provider: EntitlementProvider | None = None,
    introspector: FabricIntrospector | None = None,
) -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server, or None if the SDK is unavailable.

    ``provider`` is the per-run entitlement/availability context (AT-5);
    the tool closures capture it so every ``atlas_search`` /
    ``atlas_describe`` call answers for THAT context. ``None`` serves the
    global (pre-overlay) view. ``introspector`` (AT-7) is the per-run live
    Fabric (workspace-ontology) view — EE-wired, constructed with the run's
    workspace id; ``None`` (the OSS default) means nothing about live
    Fabric exists beyond the compiled ``primitive:fabric`` narrative.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_atlas MCP disabled")
        return None

    @tool(
        "atlas_search",
        (
            "Search the OS self-model (atlas) for the capability that matches "
            "an intent. Call this BEFORE guessing whether the OS can do "
            "something or which primitive to reach for — atlas terms carry "
            "paw-specific meanings (Pocket = workspace app container, "
            "Instinct = human approval gate, Fabric = typed knowledge graph, "
            "Belt = code assembly line...) that differ from their everyday "
            "meanings. Pass `intent` as what you're trying to accomplish "
            "(e.g. 'approve agent actions', 'publish a website'). Returns "
            "ranked capability cards (id, kind, name, summary, surface if "
            "set); follow up with atlas_describe on the best id. Cheap, "
            "in-process, single round-trip."
        ),
        {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "description": (
                        "What you are trying to do, in plain words "
                        "(e.g. 'let a human review agent changes')."
                    ),
                }
            },
            "required": ["intent"],
        },
    )
    async def atlas_search(args):  # type: ignore[no-untyped-def]
        return await _atlas_search_handler(args, provider, introspector)

    @tool(
        "atlas_describe",
        (
            "Fetch the full atlas entry for one capability by stable id "
            "(e.g. 'primitive:instinct'). Returns the narrative (WHEN to "
            "reach for it and what it pairs with), `how` (the tool / verb / "
            "API that exercises it), `requires`, and `surface`. Use after "
            "atlas_search picks a candidate, or whenever you're about to "
            "explain or exercise an OS primitive and want ground truth "
            "instead of guessing from the name."
        ),
        {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Stable atlas entry id, e.g. 'primitive:pocket'.",
                }
            },
            "required": ["id"],
        },
    )
    async def atlas_describe(args):  # type: ignore[no-untyped-def]
        return await _atlas_describe_handler(args, provider, introspector)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[atlas_search, atlas_describe],
    )
    return SERVER_NAME, server


__all__ = [
    "ATLAS_DESCRIBE_TOOL_ID",
    "ATLAS_SEARCH_TOOL_ID",
    "ATLAS_TOOL_IDS",
    "SERVER_NAME",
    "build_atlas_context_server",
]
