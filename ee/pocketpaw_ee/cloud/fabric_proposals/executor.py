# ee/cloud/fabric_proposals/executor.py — apply an approved Fabric-ontology write.
# Created: 2026-06-19 (SZD-5a — _fabric_objects Instinct proposal type).
#
# What this module does (the apply-on-approve half of the Fabric-objects gate):
# the propose helper (``fabric_proposals.propose.propose_fabric_objects``) files
# an Instinct Action carrying a ``_fabric_objects`` blob THROUGH Instinct (the
# human approve/reject layer). After a human approves the Action, the ee instinct
# router's ``approve_action`` fires ``execute_approved_fabric_objects`` here —
# exactly mirroring how ``external_actions.executor.
# execute_approved_external_action`` is fired for a gated connector call. This
# function:
#
#   1. Reads the ``_fabric_objects`` blob from ``action.parameters``. A missing
#      blob → return (no chain was opened, nothing to close). A schema-mismatched
#      blob → mark_failed + close the chain, return.
#   2. Idempotency guard — if the Action is already terminal (executed / failed)
#      or the blob already carries an ``outcome``, the write is NOT re-run.
#      Re-approve / retry never double-applies. The ingest loop itself is ALSO
#      idempotent (dedup by ``(source_connector, source_id)``), so even a
#      double-fire that slips past this guard creates N stable objects, not N*records.
#   3. Materialises the ontology via the canonical
#      ``connectors.fabric_ingest.ingest_records`` upsert loop, WORKSPACE-SCOPED
#      by the blob's ``workspace_id`` (SZD-2 per-tenant types). Objects are grouped
#      by type so each group rides one FabricMapping. Links are created with
#      ``(from, to, link_type)`` dedup against the existing links.
#   4. Back-writes the outcome ``{status, created, updated, links_created, executed_at}``
#      onto the persisted blob via the direct-SQL pattern.
#   5. Records the result on the Action (``mark_executed`` / ``mark_failed``) and
#      CLOSES the Decision-Graph chain exactly once.
#
# EXACTLY-ONE-TERMINAL discipline (critical): on APPROVE the EXECUTOR owns the
# ``decision.completed`` chain close — the router does NOT emit it (mirrors the
# external-action executor). On REJECT the ROUTER owns the close and the executor
# never runs. Every terminal path here goes through the single ``_fail``
# chokepoint (failure) or the one success emit at the end — never both.
#
# Never raises — a failure here must not break the approve response. The router
# wraps the call too; this is belt-and-braces. A Fabric write error is captured
# as ``status=failed`` with a ``failed`` terminal, NOT re-raised.
#
# Security (this code writes typed objects into a tenant's Fabric on approval):
#   * tenancy: every Fabric write is scoped by the blob's ``workspace_id``; an
#     empty workspace_id is refused (a tenancy hole). The router's
#     ``_assert_fabric_objects_workspace`` is the primary gate; this is
#     belt-and-braces.
#   * the write primitive is the SHIPPED ``ingest_records`` — this module does
#     NOT call raw ``define_type`` / ``create_object`` / ``link``, so it inherits
#     the proven workspace-scoped, provenance-stamped, idempotent upsert.

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pocketpaw_ee.cloud.fabric_proposals.propose import (
    FABRIC_OBJECTS_PARAM_KEY,
    FABRIC_OBJECTS_SCHEMA,
)

logger = logging.getLogger(__name__)


def _coerce_uuid(raw: Any) -> UUID | None:
    """Coerce a value to a ``UUID``, or ``None`` if it can't be."""
    if isinstance(raw, UUID):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return UUID(raw)
        except ValueError:
            return None
    return None


async def _persist_outcome(
    *,
    store: Any,
    action_id: str,
    status: str,
    summary: dict[str, Any],
    executed_at: str,
) -> None:
    """Back-write the write outcome onto the persisted ``_fabric_objects`` blob.

    Direct SQL update — the same pattern the external-action executor's
    ``_persist_outcome`` uses. The blob's ``outcome`` carries the counts +
    timestamp so a reader (audit, a re-invocation idempotency check) sees the
    result structurally. Best-effort: a write failure leaves the blob without the
    structured outcome but the free-text ``mark_executed`` / ``mark_failed``
    outcome still records it.
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
        blob["outcome"] = {"status": status, "executed_at": executed_at, **summary}
        params[FABRIC_OBJECTS_PARAM_KEY] = blob

        async with aiosqlite.connect(store._db_path) as db:
            await db.execute(
                "UPDATE instinct_actions SET parameters = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (_json.dumps(params), action_id),
            )
            await db.commit()
    except Exception:  # noqa: BLE001 — back-write is best-effort
        logger.warning(
            "fabric_objects: failed to persist outcome onto action %s — the "
            "structured outcome is unavailable (free-text outcome still recorded)",
            action_id,
            exc_info=True,
        )


def _emit_chain_close(
    *,
    passed: bool,
    action_outcome: str,
    error_class: str | None,
    reason: str | None,
    correlation_id: UUID | None,
    workspace_id: str,
    user_id: str,
    causation_id: UUID | None,
    summary: str | None = None,
) -> None:
    """Emit the ``decision.completed`` chain-close for a Fabric-objects write.

    Mirrors ``external_actions.executor._emit_chain_close`` — the executor owns
    the chain close on the approve path. ``correlation_id`` is read off the blob;
    ``causation_id`` is the ``human.corrected`` event the router emitted just
    before approval so the terminal chains back to the human approval.

    Returns early when ``correlation_id`` is None (a blob with a malformed /
    missing id): there is no chain to close. Best-effort: a Decision-Graph wiring
    failure must never break the approve response — the journal write is the
    source of truth; the Slice 4 reconciler is the safety net.
    """
    if correlation_id is None:
        return

    from soul_protocol.spec.journal import Actor

    from pocketpaw_ee.cloud.decisions.journal_writer import record_decision_completed

    actor = Actor(
        kind="agent",
        id=f"user:{user_id or 'unknown'}",
        scope_context=[f"workspace:{workspace_id}"],
    )
    payload: dict[str, Any] = {
        "passed": passed,
        "action_outcome": action_outcome,
    }
    if error_class:
        payload["error_class"] = error_class
    if reason:
        payload["reason"] = reason
    if summary:
        payload["summary"] = summary

    try:
        record_decision_completed(
            correlation_id=correlation_id,
            actor=actor,
            scope=[f"workspace:{workspace_id}"],
            payload=payload,
            causation_id=causation_id,
        )
    except Exception:  # noqa: BLE001 — chain close is best-effort
        logger.warning(
            "fabric_objects decision.completed emit failed for correlation_id=%s "
            "(action_outcome=%s) — Slice 4 reconciler will catch up",
            correlation_id,
            action_outcome,
            exc_info=True,
        )


def _already_executed(action: Any, blob: dict[str, Any]) -> bool:
    """Idempotency guard — True when this Fabric write already ran.

    Two signals, either is sufficient:
      * the blob already carries an ``outcome`` (a prior run back-wrote it), or
      * the Action is already in a terminal state (executed / failed).

    Re-invocation (bulk re-approve, retry) must never re-run the write. The
    Action's ``status`` is the authoritative gate, and the back-written outcome
    is the belt-and-braces signal in case status reads are stale. (The ingest
    loop is itself idempotent, so even a slip is harmless — this guard avoids the
    redundant work + the duplicate terminal.)
    """
    if isinstance(blob.get("outcome"), dict):
        return True
    status = getattr(action, "status", None)
    status_value = getattr(status, "value", status)
    return str(status_value) in ("executed", "failed")


# The reserved record key carrying the object's source id. It is prefixed +
# suffixed so it can't collide with a real property name, and it is excluded from
# the projected field_map so it never lands in the object's properties.
_SOURCE_ID_KEY = "__fabric_proposal_source_id__"


def _build_ingest_batches(
    object_types: list[dict[str, Any]],
    objects: list[dict[str, Any]],
) -> list[tuple[Any, str, list[dict[str, Any]]]]:
    """Group objects by (type_name, source_connector) into ingest batches.

    Returns a list of ``(FabricMapping, source_connector, [records])`` — one
    batch per (type, connector) so each ``ingest_records`` call stamps the right
    ``source_connector`` provenance. Each record is a FLAT dict: the object's
    properties at the top level, plus the object's ``source_id`` under the
    reserved key. The mapping's ``field_map`` is an IDENTITY projection over the
    union of property keys in the batch (so ``ingest_records`` stores each
    property verbatim), and ``source_id_field`` reads the reserved key. The type's
    declared properties (from ``object_types``) ride on the mapping so
    ``ensure_type`` defines the type with the proposed schema; a type referenced
    by an object but absent from ``object_types`` is ensured with empty
    properties (the upsert still stamps provenance).
    """
    from pocketpaw.connectors.fabric_ingest import FabricMapping
    from pocketpaw.fabric.models import PropertyDef

    # Index the declared type schemas by name (case-insensitive) for lookup.
    type_schemas: dict[str, dict[str, Any]] = {}
    for t in object_types:
        name = str(t.get("type_name") or "").strip()
        if name:
            type_schemas[name.lower()] = t

    # Group objects by (type_name, source_connector), preserving insertion order.
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for obj in objects:
        type_name = str(obj.get("type_name") or "").strip()
        connector = str(obj.get("source_connector") or "").strip()
        if not type_name or not connector:
            continue
        grouped.setdefault((type_name, connector), []).append(obj)

    batches: list[tuple[Any, str, list[dict[str, Any]]]] = []
    for (type_name, connector), objs in grouped.items():
        decl = type_schemas.get(type_name.lower(), {})
        prop_defs = [
            PropertyDef(**p)
            for p in (decl.get("properties") or [])
            if isinstance(p, dict) and p.get("name")
        ]
        records: list[dict[str, Any]] = []
        prop_keys: set[str] = set()
        for obj in objs:
            props = dict(obj.get("properties") or {})
            prop_keys.update(props.keys())
            record = dict(props)
            record[_SOURCE_ID_KEY] = obj["source_id"]
            records.append(record)
        field_map: dict[str, Any] = {k: k for k in prop_keys}
        mapping = FabricMapping(
            type_name=type_name,
            source_id_field=_SOURCE_ID_KEY,
            field_map=field_map,
            properties=prop_defs,
            type_description=str(decl.get("description") or ""),
            type_icon=str(decl.get("icon") or "box"),
            type_color=str(decl.get("color") or "#0A84FF"),
        )
        batches.append((mapping, connector, records))
    return batches


async def _create_links_deduped(
    *,
    store: Any,
    links: list[dict[str, Any]],
    workspace_id: str,
) -> int:
    """Create the proposed links, WORKSPACE-SCOPED, deduped by (from, to, link_type).

    Each link references its endpoints by ``(source_connector, source_id)`` — the
    natural key the objects were upserted under — so we resolve them to the
    materialised object ids via ``get_object_by_source`` (the same workspace-scoped
    read the ingest loop uses). A link whose endpoint can't be resolved (the object
    wasn't in this proposal and doesn't already exist) is skipped. An EXISTING link
    with the same ``(from_id, to_id, link_type)`` is NOT recreated — re-approve (or
    two proposals asserting the same link) does not duplicate. Returns the count of
    NEW links created.

    ``store.link`` is not idempotent on its own, so the dedup lives here: we check
    ``list_links(from_id, to_id, link_type, workspace_id)`` before each create.
    """
    created = 0
    for link in links:
        from_ref = link.get("from") or {}
        to_ref = link.get("to") or {}
        link_type = str(link.get("link_type") or "").strip()
        if not link_type:
            continue

        from_obj = await store.get_object_by_source(
            source_connector=str(from_ref.get("source_connector") or ""),
            source_id=str(from_ref.get("source_id") or ""),
            workspace_id=workspace_id,
        )
        to_obj = await store.get_object_by_source(
            source_connector=str(to_ref.get("source_connector") or ""),
            source_id=str(to_ref.get("source_id") or ""),
            workspace_id=workspace_id,
        )
        if from_obj is None or to_obj is None:
            logger.info(
                "fabric_objects: skipping link %s — endpoint not resolvable in "
                "workspace %s",
                link_type,
                workspace_id,
            )
            continue

        # Dedup by (from, to, link_type) within the tenant — skip if it exists.
        existing, total = await store.list_links(
            from_id=from_obj.id,
            to_id=to_obj.id,
            link_type=link_type,
            workspace_id=workspace_id,
        )
        if total > 0:
            continue

        await store.link(
            from_id=from_obj.id,
            to_id=to_obj.id,
            link_type=link_type,
            workspace_id=workspace_id,
        )
        created += 1
    return created


async def execute_approved_fabric_objects(
    action: Any,
    *,
    human_event_id: Any | None = None,
) -> None:
    """Materialise the Fabric ontology carried by a freshly-approved Action.

    Called best-effort from the instinct router's ``approve_action`` /
    ``bulk_approve_actions`` after ``store.approve()`` succeeds — the same hook
    shape the external-action executor uses. ``action`` is the approved Action.

    ``human_event_id`` is the id of the ``human.corrected`` event the router
    emitted just before calling this — threaded through so the terminal
    ``decision.completed`` event can chain its ``causation_id`` back to the
    approval, completing the causal walk ``agent.proposed → human.corrected →
    decision.completed``. ``None`` is tolerated (the chain still folds via the
    shared ``correlation_id``).

    Never raises — a failure here must not break the approve response. The router
    wraps the call too; this is belt-and-braces. The Action is marked executed on
    a successful write or failed with a clear outcome on any error, and the
    Decision-Graph chain is closed exactly once (success → landed, failure →
    failed).
    """
    from pocketpaw.stores import get_fabric_store, get_instinct_store

    store = get_instinct_store()
    params = getattr(action, "parameters", None) or {}
    blob = params.get(FABRIC_OBJECTS_PARAM_KEY)
    if not isinstance(blob, dict):
        # Not a fabric-objects Action at all — no chain was opened for it, so
        # there is nothing to close. Return without a terminal emit.
        logger.warning("approved action %s carries no _fabric_objects blob", action.id)
        return

    # Read the chain ids off the blob up front so EVERY terminal path can close
    # the chain it opened. A malformed / missing id → None and the close no-ops
    # (the Slice 4 abandon-sweeper closes any chain left open).
    correlation_id = _coerce_uuid(blob.get("correlation_id"))
    workspace_id = str(blob.get("workspace_id") or "")
    requested_by = str(blob.get("requested_by") or "")
    approver = str(getattr(action, "approved_by", "") or "") or requested_by or "system"
    causation = _coerce_uuid(human_event_id)

    async def _fail(reason: str, *, error_class: str) -> None:
        """Mark the Action failed AND close the chain with one terminal — the
        single failure-path chokepoint so a path can never both fail and
        double-fire the terminal."""
        await store.mark_failed(action.id, reason)
        await _persist_outcome(
            store=store,
            action_id=str(action.id),
            status="failed",
            summary={"reason": reason},
            executed_at=datetime.now(UTC).isoformat(),
        )
        _emit_chain_close(
            passed=False,
            action_outcome="failed",
            error_class=error_class,
            reason=reason,
            correlation_id=correlation_id,
            workspace_id=workspace_id,
            user_id=approver,
            causation_id=causation,
            summary=reason,
        )

    # Schema guard — a stale blob from an incompatible build fails loud rather
    # than writing a misinterpreted ontology.
    if blob.get("schema") != FABRIC_OBJECTS_SCHEMA:
        await _fail(
            "fabric-objects schema mismatch — the blob is from an incompatible "
            "build and cannot be executed",
            error_class="SchemaMismatch",
        )
        return

    # Tenancy — a Fabric write with no workspace is a tenancy hole. The Fabric
    # writes are scoped by workspace_id; fail loud so a malformed blob is
    # recorded, not silently written unscoped.
    if not workspace_id:
        await _fail(
            "fabric-objects blob carries no workspace_id — cannot scope the write",
            error_class="MissingWorkspace",
        )
        return

    # Idempotency — never re-run the write on a re-invocation.
    if _already_executed(action, blob):
        logger.info(
            "fabric_objects: action %s already executed (idempotency guard) — "
            "skipping the Fabric write",
            action.id,
        )
        return

    objects = blob.get("objects")
    if not isinstance(objects, list) or not objects:
        await _fail(
            "fabric-objects blob carries no objects to materialise",
            error_class="MalformedBlob",
        )
        return
    _ot = blob.get("object_types")
    object_types: list[dict[str, Any]] = _ot if isinstance(_ot, list) else []
    _lk = blob.get("links")
    links: list[dict[str, Any]] = _lk if isinstance(_lk, list) else []

    # Materialise the ontology. Any failure (bad property, store error) is
    # captured as a failed outcome — NEVER re-raised into the router.
    try:
        from pocketpaw.connectors.fabric_ingest import ingest_records

        fabric = get_fabric_store()
        batches = _build_ingest_batches(object_types, objects)

        total_created = 0
        total_updated = 0
        for mapping, connector, records in batches:
            # REUSE the shipped, workspace-scoped, idempotent upsert loop — one
            # call per (type, connector) batch so the right provenance is stamped.
            ingest_result = await ingest_records(
                fabric,
                connector,
                records,
                mapping,
                workspace_id=workspace_id,
            )
            total_created += ingest_result.created
            total_updated += ingest_result.updated

        links_created = await _create_links_deduped(
            store=fabric,
            links=links,
            workspace_id=workspace_id,
        )
    except Exception as exc:  # noqa: BLE001 — never let a Fabric write break approve
        logger.warning(
            "fabric_objects: write crashed for action %s (workspace=%s)",
            action.id,
            workspace_id,
            exc_info=True,
        )
        await _fail(
            f"fabric write failed: {exc}",
            error_class=type(exc).__name__,
        )
        return

    summary = {
        "created": total_created,
        "updated": total_updated,
        "links_created": links_created,
    }

    # Success — mark executed, back-write the structured outcome, close the chain.
    await store.mark_executed(
        action.id,
        f"fabric ontology materialised: {total_created} created, "
        f"{total_updated} updated, {links_created} link(s) created",
    )
    await _persist_outcome(
        store=store,
        action_id=str(action.id),
        status="executed",
        summary=summary,
        executed_at=datetime.now(UTC).isoformat(),
    )
    # Close the chain on the SUCCESS path. This is the ONLY terminal on the happy
    # path (every failure path above closed via ``_fail`` and returned), so
    # exactly one ``decision.completed`` lands per write.
    _emit_chain_close(
        passed=True,
        action_outcome="landed",
        error_class=None,
        reason=None,
        correlation_id=correlation_id,
        workspace_id=workspace_id,
        user_id=approver,
        causation_id=causation,
        summary=(
            f"{total_created} created, {total_updated} updated, "
            f"{links_created} link(s) created"
        ),
    )
    logger.info(
        "fabric_objects: executed action %s → %d created / %d updated / %d links (ok)",
        action.id,
        total_created,
        total_updated,
        links_created,
    )


__all__ = ["execute_approved_fabric_objects"]
