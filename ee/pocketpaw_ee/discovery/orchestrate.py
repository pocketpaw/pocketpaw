# pocketpaw_ee/discovery/orchestrate.py — wire DiscoveryRun → the two gated proposals.
#
# Created: 2026-06-19 (SZD-6 / feat/szd-6-integration) — the integration slice
# that connects the pieces the earlier SZD slices built in isolation. A discovery
# run yields an ``OntologyDraft`` (SZD-3/4); this module turns that draft into the
# two STAGED Instinct proposals a human reviews through the gate:
#
#   1. a ``_fabric_objects`` proposal (SZD-5a) — the discovered ontology
#      (object_types + objects + links) staged for materialisation into Fabric;
#   2. a ``_pocket_create`` proposal (SZD-5b) — a starter dashboard Pocket whose
#      rippleSpec binds one widget per HIGH-CONFIDENCE discovered type to the
#      ``fabric.objects`` ripple source (SZD-1), so when both proposals are
#      approved the Pocket renders the freshly-materialised Fabric data.
#
# Nothing here writes Fabric or creates a Pocket — both halves are GATED. This
# module only builds the two proposals and returns their Action ids; a human
# approves them in The Tray and the apply-on-approve executors do the writes.
#
# KEY-CONFIDENCE GATE (a design-review must): the digester emits a per-type
# ``key_confidence`` and a ``source_id_field`` that is empty when no stable key
# was inferred. The Fabric ingest path DEDUPES on ``(source_connector,
# source_id)`` — so staging objects of a keyless / low-confidence type would make
# every record collapse onto a single blank ``source_id`` (ingest drops all but
# one). We therefore GATE: only types whose ``key_confidence >=
# KEY_CONFIDENCE_FLOOR`` AND that carry a non-empty ``source_id_field`` contribute
# objects + links + a starter-Pocket widget. Low-confidence keyless types are
# SKIPPED from materialisation and FLAGGED in the returned summary so a human can
# see what was held back (rather than silently dropping records).
#
# SUPERSEDE-ON-RERUN: a discovery run is re-runnable. A second run for the same
# workspace SUPERSEDES the prior STILL-OPEN (PENDING) discovery proposal pair
# rather than stacking a duplicate. Both proposals are tagged with a
# ``discovery_run`` marker (a shared ``run_id`` + the proposal ``role``) on their
# blob so the supersede sweep can find the prior open pair for this workspace and
# reject it (status REJECTED, reason "superseded by discovery run <id>") before
# filing the new pair. Approved / rejected / executed proposals are left alone —
# only the still-open pair is collapsed.
#
# Async orchestration; depends on the SZD-4 DiscoveryRun + the SZD-5a/5b propose
# helpers + the OSS InstinctStore (for the supersede sweep). No direct Fabric /
# Pocket writes.

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from pocketpaw_ee.discovery.models import DraftObjectType, OntologyDraft
from pocketpaw_ee.discovery.run import DiscoveryRun, DiscoveryRunOptions

logger = logging.getLogger(__name__)

# Minimum per-type ``key_confidence`` for a discovered type to be MATERIALISED.
# Below this floor (or with no inferred ``source_id_field``) a type has no stable
# idempotency key, so staging its objects would collapse every record onto one
# blank source_id at ingest time. Such types are skipped + flagged, never staged.
KEY_CONFIDENCE_FLOOR = 0.5

# The blob key (set on BOTH proposals' parameters blob) tagging a proposal as part
# of a discovery run, so the supersede sweep can find a workspace's prior open
# pair. Carries ``{run_id, role, workspace_id}``. ``role`` is "fabric_objects" or
# "pocket_create".
DISCOVERY_MARKER_KEY = "discovery_run"

# The synthetic ``source_connector`` namespace stamped on every staged object so
# the Fabric ingest dedup key ``(source_connector, source_id)`` is stable across
# re-runs. Namespaced per type so two types can reuse the same source_id without
# colliding, and so a discovered object never collides with a real connector's
# ingested object.
_DISCOVERY_CONNECTOR_PREFIX = "discovery"


def _discovery_connector(type_name: str) -> str:
    """The synthetic source_connector for a discovered type's objects."""
    return f"{_DISCOVERY_CONNECTOR_PREFIX}:{type_name}"


@dataclass(frozen=True)
class DiscoveryProposalResult:
    """The outcome of :func:`run_discovery_and_propose`.

    * ``fabric_objects_action_id`` / ``pocket_action_id`` — the two gated Action
      ids a human reviews. ``None`` for either when there was nothing to stage
      (an empty draft → no fabric proposal → no pocket proposal).
    * ``run_id`` — the discovery-run marker shared by both proposals (used to
      supersede this pair on the next run).
    * ``materialised_types`` — the high-confidence type names that were staged.
    * ``skipped_types`` — ``{type_name: reason}`` for types held back (keyless /
      low-confidence) so a human can see what was NOT staged.
    * ``superseded_action_ids`` — the prior open pair this run collapsed (empty on
      a first run).
    """

    run_id: str
    fabric_objects_action_id: str | None
    pocket_action_id: str | None
    materialised_types: list[str]
    skipped_types: dict[str, str]
    superseded_action_ids: list[str]


def _is_materialisable(ot: DraftObjectType) -> tuple[bool, str]:
    """Decide whether a draft type is safe to materialise.

    Returns ``(ok, reason)``. A type is materialisable only when it carries a
    non-empty ``source_id_field`` AND ``key_confidence >= KEY_CONFIDENCE_FLOOR``.
    ``reason`` explains a skip (for the flagged summary) and is empty when ok.
    """
    if not ot.source_id_field:
        return False, "no inferred primary key (keyless)"
    if ot.key_confidence < KEY_CONFIDENCE_FLOOR:
        return (
            False,
            f"key confidence {ot.key_confidence:.2f} below floor {KEY_CONFIDENCE_FLOOR:.2f}",
        )
    return True, ""


def _draft_to_fabric_proposal_kwargs(
    draft: OntologyDraft,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[str],
    dict[str, str],
]:
    """Map an ``OntologyDraft`` → ``propose_fabric_objects`` kwargs, GATED on key
    confidence.

    Returns ``(object_types, objects, links, materialised_type_names,
    skipped)``:
      * only HIGH-CONFIDENCE keyed types contribute object_types + objects;
      * each object carries a synthetic ``(source_connector, source_id)`` so the
        ingest dedup key is stable (``source_connector =
        "discovery:<type>"``, ``source_id`` = the digester's inferred source_id);
      * links are kept only when BOTH endpoints are materialisable types AND both
        endpoints have a source_id (the link's natural-key endpoints reference the
        SAME synthetic connector namespace the objects use);
      * ``skipped`` maps each held-back type name → a human reason.
    """
    materialisable: dict[str, DraftObjectType] = {}
    skipped: dict[str, str] = {}
    for ot in draft.object_types:
        ok, reason = _is_materialisable(ot)
        if ok:
            materialisable[ot.name] = ot
        else:
            skipped[ot.name] = reason

    # object_types — PropertyDef list serialised to the proposal's dict shape.
    object_types: list[dict[str, Any]] = []
    for name, ot in materialisable.items():
        object_types.append(
            {
                "type_name": name,
                "description": f"Discovered from connector data ({ot.record_count} sampled).",
                "properties": [p.model_dump(mode="json") for p in ot.properties],
            }
        )

    # objects — only for materialisable types, only rows that carry a source_id.
    objects: list[dict[str, Any]] = []
    for obj in draft.objects:
        if obj.type_name not in materialisable:
            continue
        if not obj.source_id:
            # A materialisable type whose individual row lacks the key value —
            # skip the row (can't dedup it) rather than collapse it onto blank.
            continue
        objects.append(
            {
                "type_name": obj.type_name,
                "properties": dict(obj.properties),
                "source_connector": _discovery_connector(obj.type_name),
                "source_id": obj.source_id,
            }
        )

    # links — both endpoints must be materialisable + carry a source_id.
    links: list[dict[str, Any]] = []
    for link in draft.links:
        if link.from_type not in materialisable or link.to_type not in materialisable:
            continue
        if not link.from_source_id or not link.to_source_id:
            continue
        links.append(
            {
                "from": {
                    "source_connector": _discovery_connector(link.from_type),
                    "source_id": link.from_source_id,
                },
                "to": {
                    "source_connector": _discovery_connector(link.to_type),
                    "source_id": link.to_source_id,
                },
                "link_type": link.link_type,
            }
        )

    return object_types, objects, links, list(materialisable.keys()), skipped


def assemble_discovery_pocket(
    draft: OntologyDraft,
    materialised_types: list[str] | None = None,
) -> dict[str, Any]:
    """Build a starter-dashboard rippleSpec from a discovery draft.

    A simple dashboard: one widget (a table) per HIGH-CONFIDENCE discovered type,
    each bound to the ``fabric.objects`` ripple source for that type via a
    ``{"$source": "fabric.objects", "type_name": <name>}`` marker placed under the
    spec's ``state`` (the binding the SZD-1 resolver replaces with live rows on
    read). Each table widget reads its rows from the matching state key.

    ``materialised_types`` lets the caller pass the gated high-confidence type
    list directly (the common path — keeps the assembler's gate identical to the
    fabric-proposal gate). When omitted, the assembler applies the same
    :func:`_is_materialisable` gate to the draft so a stand-alone call still skips
    keyless / low-confidence types.

    Pure: no I/O. Returns a rippleSpec dict (``{version, root, state}``) — the
    same shape the pocket-create proposal stages under its ``rippleSpec`` alias.
    """
    if materialised_types is None:
        materialised_types = [ot.name for ot in draft.object_types if _is_materialisable(ot)[0]]

    state: dict[str, Any] = {}
    children: list[dict[str, Any]] = []
    for type_name in materialised_types:
        # State key per type — the resolver replaces the $source marker with the
        # workspace-scoped rows of that Fabric type on read.
        state_key = f"rows_{type_name}"
        state[state_key] = {"$source": "fabric.objects", "type_name": type_name}
        children.append(
            {
                "id": f"table_{type_name}",
                "type": "table",
                "props": {
                    "title": type_name,
                    # The widget binds its rows to the state key the source feeds.
                    "rows": f"$state.{state_key}",
                },
            }
        )

    return {
        "version": "1.0",
        "root": {
            "id": "root",
            "type": "container",
            "props": {"title": "Discovered data"},
            "children": children,
        },
        "state": state,
    }


async def _supersede_prior_open_pair(
    *,
    store: Any,
    workspace_id: str,
    rejector: str,
    new_run_id: str,
) -> list[str]:
    """Reject the workspace's prior STILL-OPEN discovery proposal pair.

    Lists the workspace's PENDING actions, keeps the ones tagged with a
    ``discovery_run`` marker (either proposal kind), and rejects each so the new
    run's pair doesn't stack on top of a stale one. Returns the superseded action
    ids. Approved / rejected / executed proposals are NOT touched (only PENDING).

    Best-effort: a supersede failure logs but does NOT block filing the new pair
    (a duplicate open pair is recoverable; a failed discovery run is worse).
    """
    superseded: list[str] = []
    try:
        from pocketpaw.instinct.models import ActionStatus

        pending = await store.list_actions(
            pocket_id=workspace_id,
            status=ActionStatus.PENDING,
            workspace_id=workspace_id,
            limit=500,
        )
    except Exception:  # noqa: BLE001 — the sweep is best-effort
        logger.warning(
            "discovery: failed to list prior open proposals for workspace %s — "
            "filing the new pair without superseding",
            workspace_id,
            exc_info=True,
        )
        return superseded

    for action in pending:
        params = getattr(action, "parameters", None) or {}
        marker = _find_discovery_marker(params)
        if marker is None:
            continue
        # Never supersede the pair we're about to file (defensive — they don't
        # exist yet at sweep time, but guard against a re-entrant call).
        if marker.get("run_id") == new_run_id:
            continue
        try:
            await store.reject(
                action.id,
                reason=f"superseded by discovery run {new_run_id}",
                rejector=rejector or "discovery",
            )
            superseded.append(action.id)
        except Exception:  # noqa: BLE001 — one bad reject can't block the rest
            logger.warning(
                "discovery: failed to supersede prior open proposal %s — continuing",
                action.id,
                exc_info=True,
            )

    if superseded:
        logger.info(
            "discovery: superseded %d prior open proposal(s) for workspace %s (new run %s)",
            len(superseded),
            workspace_id,
            new_run_id,
        )
    return superseded


def _find_discovery_marker(params: dict[str, Any]) -> dict[str, Any] | None:
    """Pull the ``discovery_run`` marker off either proposal's blob, or None.

    The marker rides on the per-proposal blob (``_fabric_objects`` or
    ``_pocket_create``) under :data:`DISCOVERY_MARKER_KEY`, so a single read of
    the Action's parameters surfaces it regardless of which proposal kind this is.
    """
    for blob_key in ("_fabric_objects", "_pocket_create"):
        blob = params.get(blob_key)
        if isinstance(blob, dict):
            marker = blob.get(DISCOVERY_MARKER_KEY)
            if isinstance(marker, dict):
                return marker
    return None


async def run_discovery_and_propose(
    workspace_id: str,
    user_id: str,
    connector_ids: list[str],
    opts: DiscoveryRunOptions | None = None,
    *,
    discovery_run: DiscoveryRun | None = None,
) -> DiscoveryProposalResult:
    """Run discovery for a workspace and stage the two gated proposals.

    The integration entry point (SZD-6). Validates tenancy at entry, runs a
    discovery pass to an ``OntologyDraft``, GATES the draft on per-type key
    confidence, then:
      1. supersedes the workspace's prior STILL-OPEN discovery proposal pair;
      2. files a ``_fabric_objects`` proposal for the high-confidence ontology;
      3. files a ``_pocket_create`` proposal for a starter dashboard bound to the
         ``fabric.objects`` source for each materialised type.

    Both proposals are GATED — nothing is written to Fabric and no Pocket is
    created here; a human approves them in The Tray and the apply-on-approve
    executors do the writes. Returns a :class:`DiscoveryProposalResult` carrying
    the two Action ids (either ``None`` when there was nothing to stage).

    Args:
        workspace_id: the originating tenant. Required — discovery, the proposals,
            and the supersede sweep are all workspace-scoped by it.
        user_id: the approver/owner. Required — the Pocket is created under it and
            the proposals are assigned to it.
        connector_ids: the workspace's bound connectors to sample.
        opts: discovery-run knobs (sampling cap, explicit read actions, ...).
        discovery_run: an injected ``DiscoveryRun`` (tests pass a mock-registry
            run); defaults to a fresh one driving the local-runtime registry.
    """
    from pocketpaw_ee.cloud.fabric_proposals.propose import propose_fabric_objects
    from pocketpaw_ee.cloud.pocket_proposals.propose import propose_pocket

    workspace_id = str(workspace_id or "")
    if not workspace_id:
        raise ValueError("run_discovery_and_propose requires a non-empty workspace_id")
    user_id = str(user_id or "")
    if not user_id:
        raise ValueError("run_discovery_and_propose requires a non-empty user_id")

    run = discovery_run or DiscoveryRun()
    draft = await run.run(workspace_id, connector_ids, opts)

    object_types, objects, links, materialised_types, skipped = _draft_to_fabric_proposal_kwargs(
        draft
    )

    # The shared discovery-run marker — both proposals carry it so the NEXT run can
    # find + supersede this still-open pair.
    run_id = uuid4().hex

    from pocketpaw.stores import get_instinct_store

    store = get_instinct_store()

    # 1) Supersede the prior open pair BEFORE filing the new one (so the new pair
    #    is never itself collapsed and the queue never shows two open pairs).
    superseded = await _supersede_prior_open_pair(
        store=store,
        workspace_id=workspace_id,
        rejector=user_id,
        new_run_id=run_id,
    )

    if not objects:
        # Nothing high-confidence to materialise — no fabric proposal, so no
        # starter pocket either (a dashboard with no live source is noise). The
        # prior pair is still superseded; the run is a clean no-op otherwise.
        logger.info(
            "discovery: no high-confidence objects to stage for workspace %s "
            "(skipped types: %s) — no proposals filed",
            workspace_id,
            ", ".join(skipped) or "none",
        )
        return DiscoveryProposalResult(
            run_id=run_id,
            fabric_objects_action_id=None,
            pocket_action_id=None,
            materialised_types=materialised_types,
            skipped_types=skipped,
            superseded_action_ids=superseded,
        )

    skipped_note = f" {len(skipped)} low-confidence type(s) held back." if skipped else ""
    fabric_summary = (
        f"Discovered {len(objects)} object(s) across {len(materialised_types)} "
        f"type(s) and {len(links)} link(s) from connector data.{skipped_note}"
    )
    fabric_action_id = await propose_fabric_objects(
        workspace_id=workspace_id,
        objects=objects,
        object_types=object_types,
        links=links,
        requested_by=user_id,
        summary=fabric_summary,
    )
    # The marker rides on the blob, not the per-object dicts — stamp it via a
    # direct blob back-write (the propose helper normalises objects and would drop
    # an unknown per-object key). Done after propose so the Action exists.
    await _stamp_discovery_marker(
        store=store,
        action_id=fabric_action_id,
        blob_key="_fabric_objects",
        marker={
            "run_id": run_id,
            "role": "fabric_objects",
            "workspace_id": workspace_id,
        },
    )

    # 2) The starter Pocket — one widget per materialised type, bound to
    #    fabric.objects so it renders the (to-be-)materialised rows.
    ripple_spec = assemble_discovery_pocket(draft, materialised_types)
    pocket_summary = (
        f"Starter dashboard for {len(materialised_types)} discovered type(s): "
        f"{', '.join(materialised_types)}."
    )
    pocket_action_id = await propose_pocket(
        workspace_id=workspace_id,
        user_id=user_id,
        ripple_spec=ripple_spec,
        name="Discovered data",
        summary=pocket_summary,
    )
    await _stamp_discovery_marker(
        store=store,
        action_id=pocket_action_id,
        blob_key="_pocket_create",
        marker={
            "run_id": run_id,
            "role": "pocket_create",
            "workspace_id": workspace_id,
        },
    )

    logger.info(
        "discovery: filed proposal pair for workspace %s (fabric=%s pocket=%s, "
        "run=%s, %d materialised / %d skipped, %d superseded)",
        workspace_id,
        fabric_action_id,
        pocket_action_id,
        run_id,
        len(materialised_types),
        len(skipped),
        len(superseded),
    )

    return DiscoveryProposalResult(
        run_id=run_id,
        fabric_objects_action_id=fabric_action_id,
        pocket_action_id=pocket_action_id,
        materialised_types=materialised_types,
        skipped_types=skipped,
        superseded_action_ids=superseded,
    )


async def _stamp_discovery_marker(
    *,
    store: Any,
    action_id: str,
    blob_key: str,
    marker: dict[str, Any],
) -> None:
    """Write the ``discovery_run`` marker onto a filed proposal's blob.

    Direct-SQL blob back-write — the same pattern the propose helpers use for
    ``_persist_chain_ids``. Best-effort: a stamp failure leaves the proposal
    un-marked (it just won't be auto-superseded by the next run; a human can still
    reject it). A failed stamp must NOT fail the proposal that was already filed.
    """
    import json as _json

    import aiosqlite

    try:
        action = await store.get_action(action_id)
        if action is None:
            return
        params = dict(getattr(action, "parameters", None) or {})
        blob = params.get(blob_key)
        if not isinstance(blob, dict):
            return
        blob = dict(blob)
        blob[DISCOVERY_MARKER_KEY] = dict(marker)
        params[blob_key] = blob

        async with aiosqlite.connect(store._db_path) as db:
            await db.execute(
                "UPDATE instinct_actions SET parameters = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (_json.dumps(params), action_id),
            )
            await db.commit()
    except Exception:  # noqa: BLE001 — marker stamp is best-effort
        logger.warning(
            "discovery: failed to stamp discovery_run marker onto action %s "
            "(blob=%s) — it won't be auto-superseded by the next run",
            action_id,
            blob_key,
            exc_info=True,
        )


__all__ = [
    "KEY_CONFIDENCE_FLOOR",
    "DISCOVERY_MARKER_KEY",
    "DiscoveryProposalResult",
    "assemble_discovery_pocket",
    "run_discovery_and_propose",
]
