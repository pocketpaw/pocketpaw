# ee/cloud/fabric_proposals/propose.py — propose a gated Fabric-ontology write.
# Created: 2026-06-19 (SZD-5a — _fabric_objects Instinct proposal type).
#
# What this module does (the propose half of the Fabric-objects gate): "sovereign
# zero-setup discovery" stages a PROPOSED Fabric ontology — a set of object types,
# objects, and links it inferred from a tenant's connector data — for a human to
# review through the Instinct gate before any of it is written to Fabric. This
# files an Instinct ``Action`` carrying a ``_fabric_objects`` blob (schema 1)
# under ``Action.parameters``. A human approves it in The Tray; the
# apply-on-approve executor (``fabric_proposals.executor.
# execute_approved_fabric_objects``) then materialises the ontology in Fabric via
# the canonical ``connectors.fabric_ingest.ingest_records`` upsert loop. This
# module does NOT write Fabric — it only gates.
#
# The blob is the gated Fabric-ontology kind, sitting alongside the pocket-write
# bridge's ``_pocket_write``, the Belt develop-station's ``_code_change``, the
# external-action gate's ``_external_action``, the mandate foreman's
# ``_belt_plan``, and the Branch-primitive merge gate's ``_artifact_change``. The
# router + executor dispatch on the presence of the ``_fabric_objects``
# parameters key (the Action model has no literal ``kind`` column; the blob also
# carries ``kind="fabric_objects"`` for readers that introspect it).
#
# Schema 1 (this is the first version — the schema-version pattern is replicated
# from ``external_actions.propose.EXTERNAL_ACTION_SCHEMA`` /
# ``instinct_bridge._POCKET_WRITE_SCHEMA`` / belt's ``CODE_CHANGE_SCHEMA``,
# starting at 1). The blob carries:
#   * ``schema`` / ``kind`` — version + discriminator;
#   * ``workspace_id`` — the originating tenant (the executor's tenancy gate
#     reads it HERE; a Fabric ontology isn't bound to a pocket the way a parked
#     write is, so tenancy lives entirely on the blob — and the Fabric writes are
#     workspace-scoped via this id, building on SZD-2's per-tenant type catalog);
#   * ``object_types`` — the ObjectTypes to ensure-exist: each
#     ``{type_name, description?, icon?, color?, properties[]}``;
#   * ``objects`` — the objects to upsert: each
#     ``{type_name, properties, source_connector, source_id}``. ``source_id`` is
#     the idempotency key — re-approving (or two proposals asserting the same
#     ``(source_connector, source_id)``) updates rather than duplicates;
#   * ``links`` — the links to create: each ``{from, to, link_type}``, deduped by
#     ``(from, to, link_type)`` at execution;
#   * ``summary`` — a human-readable one-liner for the gate UI (The Tray);
#   * ``correlation_id`` / ``proposed_event_id`` — the Decision-Graph chain ids,
#     minted here at propose time (``agent.proposed`` opens the chain). The
#     router's approve / reject paths and the executor close the SAME chain.
#
# Security:
#   * NO Fabric mutation happens here — the ontology is staged as DATA on the
#     blob and only materialised after a human approves.
#   * Tenancy is bound on the router's approve / reject paths via
#     ``_assert_fabric_objects_workspace`` and re-validated at execution (the
#     Fabric writes are scoped by the blob's ``workspace_id``). A
#     cross-workspace approve OR reject is refused — asymmetric tenant scope is
#     no tenant scope (pocketpaw#1183 / #1250).

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)

# The Instinct Action kind discriminator for a Fabric-objects proposal. The
# router + executor dispatch on the presence of the parameters key below; the
# blob also carries ``kind="fabric_objects"`` for readers that introspect it.
FABRIC_OBJECTS_KIND = "fabric_objects"

# The parameters key under which the Fabric-objects blob rides — peer of the
# pocket-write bridge's ``_pocket_write``, belt's ``_code_change``, and the
# external-action gate's ``_external_action``. The router + executor dispatch on
# this key being present.
FABRIC_OBJECTS_PARAM_KEY = "_fabric_objects"

# Schema version stamped on the ``_fabric_objects`` blob. Bump when the blob
# shape changes so a stale pending Action approved after a deploy fails loud
# instead of writing a misinterpreted ontology (same discipline as the
# external-action gate's ``EXTERNAL_ACTION_SCHEMA``). Starts at 1 — first version.
FABRIC_OBJECTS_SCHEMA = 1


def _emit_agent_proposed(
    *,
    correlation_id: UUID,
    action_id: str,
    workspace_id: str,
    user_id: str,
    type_count: int,
    object_count: int,
    link_count: int,
) -> UUID | None:
    """Emit the chain-opening ``agent.proposed`` event for a Fabric-objects write.

    Mirrors ``external_actions.propose._emit_agent_proposed``: the proposing
    caller is the actor (``kind="agent"`` with the requesting user on its id, the
    workspace on its scope_context). A Fabric ontology isn't bound to a pocket —
    its tenancy is the workspace — so ``pocket_id`` on the chain carries the
    workspace id (matching how the Action's ``pocket_id`` field carries the
    workspace).

    Returns the emitted event id so the caller can persist it on the blob's
    ``proposed_event_id`` field for the ``human.corrected`` causation chain, or
    ``None`` when the emit raised — best-effort per RFC 09; the Slice 4
    reconciler picks up any orphans.
    """
    from soul_protocol.spec.journal import Actor

    from pocketpaw_ee.cloud.decisions.journal_writer import record_agent_proposed

    actor = Actor(
        kind="agent",
        id=f"user:{user_id or 'unknown'}",
        scope_context=[f"workspace:{workspace_id}"],
    )
    intent = (
        f"create {object_count} Fabric object(s) across {type_count} type(s) "
        f"and {link_count} link(s)"
    )
    payload: dict[str, Any] = {
        # Fields the projection's ``_fold_proposed`` consumes.
        "intent": intent,
        "action": "fabric_objects",
        "pocket_id": workspace_id,
        "inputs": [],
        # Richer fields for the explain narrator.
        "proposal_kind": "fabric_objects",
        "proposal": {
            "type_count": type_count,
            "object_count": object_count,
            "link_count": link_count,
        },
        "action_id": action_id,
    }
    try:
        entry = record_agent_proposed(
            correlation_id=correlation_id,
            actor=actor,
            scope=[f"workspace:{workspace_id}"],
            payload=payload,
        )
        return entry.id
    except Exception:  # noqa: BLE001 — chain emit is best-effort
        logger.warning(
            "fabric_objects agent.proposed emit failed for correlation_id=%s "
            "(action_id=%s) — Slice 4 reconciler will catch up",
            correlation_id,
            action_id,
            exc_info=True,
        )
        return None


async def _persist_chain_ids(
    *,
    store: Any,
    action_id: str,
    correlation_id: str,
    proposed_event_id: str | None,
) -> None:
    """Write ``correlation_id`` + ``proposed_event_id`` onto the persisted
    Action's ``parameters._fabric_objects`` blob after ``agent.proposed`` fired.

    The blob is built with ``correlation_id`` already set (minted before build);
    ``proposed_event_id`` is the field this back-write fills in. Direct SQL
    update — the same pattern the external-action gate's ``_persist_chain_ids``
    uses. Best-effort: a write failure leaves ``proposed_event_id`` None and the
    eventual ``human.corrected`` emits without a causation_id (the chain still
    folds; causation_id is optional on EventEntry).
    """
    import json as _json

    import aiosqlite

    try:
        action = await store.get_action(action_id)
        if action is None:
            return
        params = dict(getattr(action, "parameters", None) or {})
        blob = params.get(FABRIC_OBJECTS_PARAM_KEY)
        if not isinstance(blob, dict):
            return
        blob = dict(blob)
        blob["correlation_id"] = correlation_id
        blob["proposed_event_id"] = proposed_event_id
        params[FABRIC_OBJECTS_PARAM_KEY] = blob

        async with aiosqlite.connect(store._db_path) as db:
            await db.execute(
                "UPDATE instinct_actions SET parameters = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (_json.dumps(params), action_id),
            )
            await db.commit()
    except Exception:  # noqa: BLE001 — write-back is best-effort
        logger.warning(
            "fabric_objects: failed to persist chain ids onto action %s — the "
            "chain's human.corrected will emit without causation_id",
            action_id,
            exc_info=True,
        )


def _normalize_object_types(object_types: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Coerce the proposed object types into a stable, JSON-safe blob shape.

    Each entry keeps ``type_name`` + the optional presentation/properties fields
    the executor's FabricMapping consumes. ``properties`` is a list of
    PropertyDef-shaped dicts (``{name, type, required, description, ...}``);
    anything not a dict is dropped (a malformed property can't be materialised).
    """
    out: list[dict[str, Any]] = []
    for entry in object_types or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("type_name") or "").strip()
        if not name:
            continue
        props = [p for p in (entry.get("properties") or []) if isinstance(p, dict)]
        out.append(
            {
                "type_name": name,
                "description": str(entry.get("description") or ""),
                "icon": str(entry.get("icon") or "box"),
                "color": str(entry.get("color") or "#0A84FF"),
                "properties": props,
            }
        )
    return out


def _normalize_objects(objects: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Coerce the proposed objects into a stable, JSON-safe blob shape.

    Each entry MUST carry ``type_name`` + ``source_connector`` + ``source_id``
    (the idempotency key) and a ``properties`` dict. Entries missing any of those
    are dropped — without a stable ``(source_connector, source_id)`` an object
    cannot be deduplicated and would silently duplicate on re-approve.
    """
    out: list[dict[str, Any]] = []
    for entry in objects or []:
        if not isinstance(entry, dict):
            continue
        type_name = str(entry.get("type_name") or "").strip()
        source_connector = str(entry.get("source_connector") or "").strip()
        source_id = str(entry.get("source_id") or "").strip()
        props = entry.get("properties")
        if not type_name or not source_connector or not source_id or not isinstance(props, dict):
            continue
        out.append(
            {
                "type_name": type_name,
                "properties": dict(props),
                "source_connector": source_connector,
                "source_id": source_id,
            }
        )
    return out


def _normalize_links(links: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Coerce the proposed links into a stable, JSON-safe blob shape.

    Each entry MUST carry ``from`` + ``to`` + ``link_type``. ``from`` / ``to``
    reference an object by its ``(source_connector, source_id)`` natural key so
    the executor can resolve them to the materialised object ids AFTER the
    upsert (the Fabric object ids don't exist yet at propose time). Entries
    missing any field are dropped.
    """
    out: list[dict[str, Any]] = []
    for entry in links or []:
        if not isinstance(entry, dict):
            continue
        from_ref = entry.get("from")
        to_ref = entry.get("to")
        link_type = str(entry.get("link_type") or "").strip()
        if not isinstance(from_ref, dict) or not isinstance(to_ref, dict) or not link_type:
            continue
        out.append(
            {
                "from": {
                    "source_connector": str(from_ref.get("source_connector") or "").strip(),
                    "source_id": str(from_ref.get("source_id") or "").strip(),
                },
                "to": {
                    "source_connector": str(to_ref.get("source_connector") or "").strip(),
                    "source_id": str(to_ref.get("source_id") or "").strip(),
                },
                "link_type": link_type,
            }
        )
    return out


async def propose_fabric_objects(
    *,
    workspace_id: str,
    objects: list[dict[str, Any]],
    object_types: list[dict[str, Any]] | None = None,
    links: list[dict[str, Any]] | None = None,
    requested_by: str,
    summary: str | None = None,
    correlation_id: str | None = None,
    assignee: str | None = None,
) -> str:
    """Build + store an Instinct ``Action`` for a gated Fabric-ontology write.

    Files an Action carrying the ``_fabric_objects`` blob (schema 1) and opens
    the Decision-Graph chain (``agent.proposed``). Returns the proposed Action
    id. NO Fabric mutation happens here — the ontology is staged as DATA and only
    materialised after a human approves.

    Args:
        workspace_id: the originating tenant. Bound on the router's approve /
            reject paths and re-validated at execution; the Fabric writes are
            scoped by it. Required — a Fabric write with no tenant to scope it to
            is a tenancy hole.
        objects: the objects to upsert. Each carries
            ``{type_name, properties, source_connector, source_id}``. ``source_id``
            is the idempotency key. Required + non-empty — a proposal with no
            objects has nothing to materialise.
        object_types: the ObjectTypes to ensure-exist. Each carries
            ``{type_name, description?, icon?, color?, properties[]}``. Optional —
            when omitted, the executor ensures each object's type by name with
            empty properties.
        links: the links to create. Each carries ``{from, to, link_type}`` where
            ``from`` / ``to`` reference objects by ``(source_connector,
            source_id)``. Deduped by ``(from, to, link_type)`` at execution.
        requested_by: the user id that proposed the write (chain actor + audit).
        summary: a human-readable one-liner for the gate UI. A sensible default
            is built from the counts when omitted.
        correlation_id: an optional pre-minted chain id. When omitted a fresh one
            is minted here (the common case).
        assignee: the workspace member who should approve. Defaults to
            ``requested_by`` so the proposer's queue carries it.
    """
    from pocketpaw.instinct.models import ActionCategory, ActionPriority, ActionTrigger
    from pocketpaw.stores import get_instinct_store

    workspace_id = str(workspace_id or "")
    if not workspace_id:
        raise ValueError("propose_fabric_objects requires a non-empty workspace_id")

    norm_types = _normalize_object_types(object_types)
    norm_objects = _normalize_objects(objects)
    norm_links = _normalize_links(links)
    if not norm_objects:
        raise ValueError(
            "propose_fabric_objects requires at least one object with a "
            "type_name, source_connector, source_id, and properties"
        )

    # Mint the chain correlation_id BEFORE building the blob so the stored Action
    # carries it from the first write. The same id threads through approve /
    # reject (router) and execute (executor) so the whole write folds into ONE
    # Decision chain.
    corr = correlation_id or str(uuid4())

    human_summary = summary or (
        f"Create {len(norm_objects)} Fabric object(s) across "
        f"{len(norm_types)} type(s) and {len(norm_links)} link(s)."
    )

    blob: dict[str, Any] = {
        "kind": FABRIC_OBJECTS_KIND,
        "schema": FABRIC_OBJECTS_SCHEMA,
        "workspace_id": workspace_id,
        "object_types": norm_types,
        "objects": norm_objects,
        "links": norm_links,
        "requested_by": requested_by,
        "summary": human_summary,
        # RFC 09 chain-correlation fields (schema 1 carries them from the start).
        "correlation_id": corr,
        "proposed_event_id": None,
    }

    title = (
        f"Fabric ontology — {len(norm_objects)} object(s), "
        f"{len(norm_types)} type(s), {len(norm_links)} link(s)"
    )
    recommendation = f"Approve to create the proposed Fabric ontology. {human_summary}"
    trigger = ActionTrigger(
        type="agent",
        source=requested_by or "fabric_objects",
        reason="proposed Fabric ontology requires approval",
    )

    store = get_instinct_store()
    action_obj = await store.propose(
        # ``pocket_id`` carries the workspace for Fabric-objects writes — they
        # aren't bound to a pocket the way Mission Control items are (mirrors the
        # external-action gate). The workspace also rides on the blob (the
        # executor's tenancy gate reads it there); pocket_id mirrors it so the
        # existing per-pocket queries still surface the row.
        pocket_id=workspace_id,
        title=title,
        description=recommendation,
        recommendation=recommendation,
        trigger=trigger,
        category=ActionCategory.WORKFLOW,
        priority=ActionPriority.MEDIUM,
        parameters={FABRIC_OBJECTS_PARAM_KEY: blob},
        assignee=assignee or requested_by or None,
        workspace_id=workspace_id,
    )

    logger.info(
        "fabric_objects: proposed %d object(s) / %d type(s) / %d link(s) → "
        "Instinct action %s (workspace=%s, correlation_id=%s)",
        len(norm_objects),
        len(norm_types),
        len(norm_links),
        action_obj.id,
        workspace_id,
        corr,
    )

    # Open the Decision-Graph chain now that the Action is stored. ``agent.
    # proposed`` is the chain origin; its event id is back-written onto the blob
    # so the router's ``human.corrected`` can cite it as causation. Best-effort:
    # a Decision-Graph wiring failure must NOT fail the propose response.
    proposed_event_id = _emit_agent_proposed(
        correlation_id=UUID(corr),
        action_id=action_obj.id,
        workspace_id=workspace_id,
        user_id=requested_by,
        type_count=len(norm_types),
        object_count=len(norm_objects),
        link_count=len(norm_links),
    )
    if proposed_event_id is not None:
        await _persist_chain_ids(
            store=store,
            action_id=action_obj.id,
            correlation_id=corr,
            proposed_event_id=str(proposed_event_id),
        )

    return action_obj.id


__all__ = [
    "FABRIC_OBJECTS_KIND",
    "FABRIC_OBJECTS_PARAM_KEY",
    "FABRIC_OBJECTS_SCHEMA",
    "propose_fabric_objects",
]
