"""Pockets domain — business logic service.

Sole owner of writes to the ``Pocket`` Beanie document. Module-level
``async def`` API. The doc → domain mapping helpers (formerly in
``repositories.py``) live alongside the public API as private helpers.

Public API (returns wire dicts for legacy router compatibility):
- ``create``, ``list_pockets``, ``get``, ``update``, ``delete``
- ``ensure_home_pocket`` — resolve-or-provision the user's home pocket
- ``create_from_ripple_spec`` — agent-generated pockets
- ``add_widget``, ``update_widget``, ``remove_widget``, ``reorder_widgets``
- ``generate_share_link``, ``revoke_share_link``, ``update_share_link``,
  ``access_via_share_link``
- ``add_collaborator``, ``remove_collaborator``
- ``add_team_member``, ``remove_team_member``
- ``add_agent``, ``remove_agent``

Agent-facing granular ``rippleSpec.ui`` mutations (called from the
``pocket_specialist`` subagent via the in-process MCP server):
- ``agent_add_node``, ``agent_replace_node``, ``agent_set_node_prop``,
  ``agent_move_node``, ``agent_remove_node``
- ``agent_set_prop_array_item``, ``agent_append_prop_array_item``,
  ``agent_remove_prop_array_item`` — Tier-2 surgical edits on a single
  item inside a widget prop-array (chart.data, table.rows, …)

Changes: 2026-05-14 — added the Tier-2 prop-array item ops (reworked
onto the pocketpaw_ee layout from PR #1106).
Changes: 2026-05-21 (#1172) — ``agent_view`` self-heals node ids via
``_heal_node_ids`` so pockets persisted before node-id stamping became
addressable by granular edit ops on first agent read.
Changes: 2026-05-21 — added ``ensure_home_pocket`` (home-as-pocket
foundation): idempotently resolves-or-provisions a per-user ``type="home"``
pocket and persists its id onto the user's ``home_pocket_id`` setting via
an atomic compare-and-swap, so a first-login provision race resolves to a
single home pocket (the losing call deletes its orphan and adopts the
winner's). Native widgets (``type="native"``) ride the existing
``widgets[]`` paths unchanged — they carry no ``rippleSpec``, so manifest
validation (which only walks ``rippleSpec`` trees) never touches them.
Changes: 2026-05-21 (RFC 04 alpha) — added the per-pocket backend
binding API: ``set_pocket_backend``, ``get_pocket_backend``,
``get_pocket_backend_for_executor``, ``remove_pocket_backend``. The
credential lives in a SEPARATE collection (``PocketBackendCredential``);
this service is the sole Beanie writer for it, same as for ``Pocket``.
Changes: 2026-05-22 (RFC 04 alpha follow-up) — added the agent-facing
``rippleSpec.sources`` ops ``agent_set_source`` / ``agent_remove_source``
so the pocket EDIT specialist (not just the create flow) can author the
top-level read-only data-source block. Bindings are validated through
the ``SourceBinding`` model before persistence.
Changes: 2026-05-22 (RFC 04 alpha follow-up 2) — ``agent_view`` now
attaches a non-secret ``backend`` summary (``{base_url, auth_type,
configured}``) so the edit specialist knows whether a backend is already
configured before it authors a ``sources`` block. The token is NEVER
included — the summary comes from ``get_pocket_backend``.
Changes: 2026-05-22 (RFC 05 M2a) — added the write-action half:
``agent_set_action`` / ``agent_remove_action`` author the top-level
``rippleSpec.actions`` block (twins of the ``sources`` ops, validated
through ``ActionBinding``); ``set_pocket_write_policy`` sets the
per-pocket write allowlist (owner-only, audit-logged, rejects when no
backend is configured); ``has_action_run_access`` gates the action-run
route (owner OR explicit shared_with ONLY — a write is NOT
workspace-visible). ``get_pocket_backend`` /
``get_pocket_backend_for_executor`` / ``_agent_backend_summary`` now
carry ``allowed_writes`` so the executor and the edit specialist can see
the write policy.
Changes: 2026-05-22 (RFC 04 M3) — added the data-source refresh half:
``list_interval_source_pockets`` (the scan the interval scheduler
iterates), ``get_pocket_ripple_spec`` (raw-spec read for an internal,
already-authenticated refresh caller), and the webhook-secret trio
``resolve_webhook_pocket`` (constant-time secret auth — returns ``None``
for every failure so the endpoint is not a pocket-existence oracle),
``get_webhook_secret`` / ``rotate_webhook_secret`` (owner-only). The
secret lives on the ``PocketBackendCredential`` row — never in the spec.
Changes: 2026-05-22 (#1174) — widgets carry an optional ``spec`` field (a
per-tile rippleSpec subtree the home grid renders). ``_build_widget_doc``,
``_widget_to_domain``, the REST ``add_widget``, and ``agent_update_widget``
thread it through; the home agent's ``add_widget`` MCP tool populates it.
Changes: 2026-05-24 (#1208) — ``agent_add_widget`` and ``agent_update_widget``
now run the catalog + action-wiring gates on ``widgets[].spec`` the same way
``agent_replace_node`` runs them on the pocket-level rippleSpec. Closes the
verb-hallucination / unwired-Refresh-button class of regressions on the home
pocket's widget-add surface (sibling to #1196 at the pocket level).
Changes: 2026-05-28 (feat/wave-3e-template-slug) — wired the RFC 03 v2
``template_slug`` field end-to-end: ``create`` / ``update`` load + compile
the bundled template (via OSS ``load_template`` + ``compile_template``)
and **merge** the compile output into ``rippleSpec`` (option B —
user-customized fields survive a re-apply). Added the
``resolve_pocket_template(workspace_id, pocket_id)`` helper the bulk
dispatcher + temporal scheduler call to obtain a typed ``PocketTemplate``
from a pocket. A stale / missing slug never breaks pocket creation: the
loader's ``strict=False`` mode returns ``None`` and the rippleSpec is
left unmodified so a later resolver run can retry.
Changes: 2026-06-08 (feat/sense-template-needs, Sense tier chunk 6a) —
``create`` now reads the template's ``needs:`` Sense ids and, for each,
asks the EE Sense resolver whether an enabled connector fills it for the
tenant (``_check_template_needs``). Senses with no provider are logged as
a warning AND attached as ``missing_senses`` on the ``pocket.created``
event so the UX can prompt-to-connect. This NEVER blocks creation —
``needs`` is template metadata, not rippleSpec, so it is not merged.
Changes: 2026-05-24 — added ``merge_spec`` (MVP entry point for the
new ``POST /api/v1/pockets/{id}/spec/merge`` endpoint). Accepts either a
``{"replace": <full spec>}`` or a ``{"merge": <partial spec>}`` body,
runs the same strict catalog + action-wiring gates as the agent
generation path, persists on success, returns a wire dict + warnings
list. Lives alongside (not replacing) the 17-tool granular-op surface
— the captain greenlights deletion in a follow-up PR after the new
path is proven on real chat traffic.
Changes: 2026-05-25 (PR #1222 R1) — ``merge_spec`` now accepts either
the typed ``MergeSpecRequest`` Pydantic model or a legacy dict and
``model_validate``s at entry. The exactly-one rule (``replace`` xor
``merge``) is enforced by the model's ``model_validator``; the
hand-rolled ``isinstance`` + presence checks the original MVP carried
are gone. Behaviour is unchanged for the happy path; bad bodies now
raise the same ``spec_merge.invalid_body`` ValidationError but via
Pydantic instead of an ad-hoc branch.
Changes: 2026-05-31 (feat/home-agent-source-authoring) — the REFINE half
of the home-pocket live-data fix. ``agent_update_widget`` gains an optional
``sources`` kwarg and authors it onto ``rippleSpec.sources`` via the same
``_merge_authored_sources_logged`` helper ``agent_add_widget`` (PR-4) uses,
so refining a static tile into a live one is one call. That helper now
RETURNS ``(authored_keys, skipped_keys)``; both widget paths stash the
result on the returned view under ``_source_merge`` so the agent_context
wrappers can build an HONEST tool result — the agent can confirm only the
sources that actually persisted and can't claim a binding that was dropped.
Changes: 2026-06-03 (Sites fix A) — ``list_pockets`` gained an optional
``exclude_pocket_ids: set[str] | None`` kwarg. When provided it adds
``{"_id": {"$nin": [...]}}`` to the query (pocket ids are stored as
strings, so no ObjectId cast). The /pockets gallery route passes the ids
of pockets already published as Sites so they don't appear in both the
pocket gallery and the sites list. ``None`` is a no-op, so mission
control / kb / surface / planner callers are unchanged.
Changes: 2026-06-07 (M3 connector→skill auto-authoring) — added
``apply_derived_surface_profile``: the SOLE Beanie-write entry point for a
connector-derived ``PocketSurfaceProfile``. The connectors service derives the
profile (pure) and calls this; this owns the Pocket-doc write (tenant-filtered,
mirroring the :1134 assignment) so the connectors package never imports the
Pocket Beanie model. It OWNS the connector-contributed dims (``skill_names`` /
``allowed_sdk_tools`` / ``deny_mcp_tool_ids``) and PRESERVES the user-owned
``ripple_mode`` / ``system_message_override`` already on the pocket.
Changes: 2026-06-04 (feat/sites-landing-brain) — ``agent_create`` now
accepts an optional ``pattern`` (alongside the existing ``type_``) and
stamps both onto the persisted ``Pocket``. The marketing-site brain
(``pocketpaw-create-paw-site``) creates via the specialist path with
``type="site"`` + ``pattern="landing"``; previously that path defaulted
``type_="custom"`` and never carried ``pattern``, so site intent was
dropped (pockets persisted as type="custom", pattern=None). Both keep
today's defaults when unset — additive, no Mongo migration.
Changes: 2026-06-11 (fix/template-ui-compile) — the template merge now
carries the template's authored canvas through. Found on a live deploy:
pockets created via ``create(template_slug=...)`` rendered an empty
canvas because the compile pass translates YAML metadata only and the
merge treated ``ui`` as user-authored-only, so a fresh create never
adopted the template's ``ui`` tree. ``_merge_compile_into_ripple_spec``
gains a ``template_ripple_spec`` kwarg and an ownership rule: an
empty/absent existing ``ui`` is template-owned (adopt the template's
``ui`` + merge its seed ``state`` / placeholder ``sources`` under the
compiled values); a non-empty ``ui`` is user-owned (machinery-only
merge, exactly the prior recompile semantics). Both ``create`` and
``update`` pass the loader's ``ripple_spec`` sibling through.
Changes: 2026-06-12 (feat/connector-as-pocket-backend) — a pocket backend
can now be an existing CONNECTOR, not only an HTTP base_url.
``set_pocket_backend`` gained ``backend_type`` (``"http"`` default |
``"connector"``) + ``connector_name``: a connector backend validates the
connector is enabled for the workspace (via
``connectors.service.is_connector_enabled_for_workspace``), needs no
``base_url``, and clears any stale http credential. ``get_pocket_backend``
+ ``get_pocket_backend_for_executor`` now carry ``backend_type`` /
``connector_name`` (``getattr`` defaults so a legacy row reads as http) so
the source executor can route a connector backend through
``connectors_service.execute`` instead of an HTTP GET. The executor tuple
grew two trailing elements; the write-path consumers ignore them.
Changes: 2026-06-13 (feat/pocket-template-reconcile, P2.4) — added the
read helper ``get_pocket_spec_and_slug`` (tenant-scoped single-read of
``(rippleSpec, template_slug)``, sibling to ``get_pocket_ripple_spec``). The new
Template Reconcile service (``pockets.reconcile``) resolves a pocket through
this helper and writes the reconciled spec through ``update`` — so reconcile
never imports the Pocket Beanie model itself (the "funnel through service.py"
boundary the import-linter pins). No existing path changed.
Changes: 2026-06-12 (fix/pocket-anchored-chat-context) — ``_agent_view_dict``
now LEADS with a ``_summary`` field (``spec_ops.summarize_ripple_spec`` over
the doc's rippleSpec: ui node count/types, capped state keys, source
summaries, action keys, legacy ``widgets_count``, plus a note that the
top-level ``widgets[]`` array is legacy and the real layout lives in
rippleSpec). Fixes the agent-view misread where ``get_pocket`` returned the
empty legacy ``widgets: []`` alongside the full rippleSpec and agents
concluded a fully composed template pocket was "an empty shell". The
``widgets`` field itself is NOT removed — other consumers may rely on it.

Changes: 2026-06-17 (feat/sites-svelte-component-edit, SE-2) — added
``set_svelte_source_file``: rewrite ONE file in a svelte-engine pocket's
``source`` map (the {path: contents} hand-written SvelteKit files) and persist
it. The Pocket write stays here (entity isolation); the sites service
orchestrates the republish. Returns the prior file contents alongside the wire
dict so the caller can roll the edit back when the downstream smoke gate fails.
Changes: 2026-06-18 (feat/branch-primitive-versions, BP-1) — ``merge_spec``
now ALSO records a draft ArtifactVersion (scope_type="pocket") after the
rippleSpec persist, via the universal Branch-primitive versions service. The
existing destructive overwrite + emit are unchanged; the version write is an
additive, best-effort snapshot (a version-store failure never breaks the merge)
so every content mutation gains a draft/history without changing the merge
contract. TODO(BP-4): revert/history hook onto these rows.
Changes: 2026-06-18 (feat/branch-primitive-instinct-gate, BP-3) —
``set_svelte_source_file`` now ALSO records a draft ArtifactVersion
(scope_type="pocket") after a svelte source edit, via
``_record_pocket_svelte_draft_version`` (the svelte peer of
``_record_pocket_draft_version``). BP-1 only versioned the rippleSpec write
(merge_spec); svelte edits go through ``set_svelte_source_file`` and were not
versioned. Same additive, best-effort snapshot contract — a version-store
failure never breaks the edit. The merge gate itself lives in the Instinct
router (BP-3); these rows are the candidates it reviews.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import secrets
from collections import OrderedDict
from pathlib import Path
from typing import Any

from beanie import PydanticObjectId
from bson.errors import InvalidId
from pydantic import ValidationError as PydanticValidationError

from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import (
    PocketCreated,
    PocketDeleted,
    PocketUpdated,
)
from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc
from pocketpaw_ee.cloud.models.pocket import Widget as _WidgetDoc
from pocketpaw_ee.cloud.models.pocket_backend import AllowedWrite as _AllowedWriteDoc
from pocketpaw_ee.cloud.models.pocket_backend import ApprovalRoute as _ApprovalRouteDoc
from pocketpaw_ee.cloud.models.pocket_backend import (
    PocketBackendCredential as _BackendCredentialDoc,
)
from pocketpaw_ee.cloud.pockets import (
    actions_ops,
    backend_crypto,
    prop_arrays,
    sources_ops,
    spec_ops,
    state_ops,
)
from pocketpaw_ee.cloud.pockets._merge import merge_ripple_spec
from pocketpaw_ee.cloud.pockets.domain import Pocket, Widget, WidgetPosition
from pocketpaw_ee.cloud.pockets.dto import (
    AddCollaboratorRequest,
    AddWidgetRequest,
    CreatePocketRequest,
    MergeSpecRequest,
    UpdatePocketRequest,
    UpdateWidgetRequest,
    pocket_to_wire_dict,
)
from pocketpaw_ee.cloud.ripple_normalizer import normalize_ripple_spec
from pocketpaw_ee.cloud.ripple_validator import (
    ActionWiringViolationError,
    CatalogViolationError,
    MissingRequiredPropError,
    find_unreferenced_state_keys,
    format_action_violations_for_agent,
    format_required_prop_violations_for_agent,
    format_violations_for_agent,
    validate_action_wiring_logged,
    validate_action_wiring_strict,
    validate_against_catalog_logged,
    validate_against_catalog_strict,
    validate_required_props_logged,
    validate_required_props_strict,
    validate_ripple_spec_logged,
)
from pocketpaw_ee.cloud.shared.errors import Forbidden, NotFound, ValidationError
from pocketpaw_ee.cloud.shared.events import event_bus
from pocketpaw_ee.cloud.surface.domain import PocketSurfaceProfile

logger = logging.getLogger(__name__)

# Pocket ``type`` for the per-user pocket that backs the home page. Behaves
# like an ordinary private pocket — the type is a marker the home route and
# the frontend key on, not a behavioural switch.
HOME_POCKET_TYPE = "home"

# Widget ``type`` for "native" widgets — widgets the frontend renders as a
# built-in Svelte component (looked up by ``name``) rather than from a
# ``rippleSpec``. Native widgets have no spec, so manifest validation —
# which only walks ``rippleSpec`` trees — never touches them.
NATIVE_WIDGET_TYPE = "native"


# ---------------------------------------------------------------------------
# Private mapping + access helpers
# ---------------------------------------------------------------------------


def _widget_to_domain(w: _WidgetDoc) -> Widget:
    return Widget(
        id=w.id,
        name=w.name,
        type=w.type,
        icon=w.icon,
        color=w.color,
        span=w.span,
        data_source_type=w.dataSourceType,
        config=tuple(w.config.items()),
        props=tuple(w.props.items()),
        data=w.data,
        assigned_agent=w.assignedAgent,
        position=WidgetPosition(row=w.position.row, col=w.position.col),
        spec=getattr(w, "spec", None),
    )


def _surface_profile_to_dict(sp: Any) -> dict[str, Any] | None:
    """Normalize a pocket's ``surface_profile`` to the JSON wire shape.

    Accepts the Beanie ``PocketSurfaceProfile`` sub-model, a plain dict (an
    already-JSON value), or ``None`` (legacy pockets / no override). Returns a
    plain dict (or ``None``) so the domain + wire layers never carry the
    Pydantic sub-model. Entity-rooms chunk ②.
    """
    if sp is None:
        return None
    if hasattr(sp, "model_dump"):
        return sp.model_dump()
    if isinstance(sp, dict):
        return dict(sp)
    return None


async def apply_derived_surface_profile(
    workspace_id: str,
    pocket_id: str,
    profile: PocketSurfaceProfile | None,
) -> None:
    """Persist a CONNECTOR-DERIVED surface profile onto a pocket.

    M3 connector→skill auto-authoring. This is the SOLE Beanie-write path for a
    connector-derived ``PocketSurfaceProfile`` — the connectors service derives
    the profile (pure) and calls this so the connectors package never imports the
    ``Pocket`` Beanie document (cloud Beanie-write boundary, enforced by
    import-linter).

    Tenancy (cloud rule §7): the doc is loaded by id then verified to belong to
    ``workspace_id``; a mismatch raises ``NotFound`` rather than writing across
    tenants.

    Coexistence rule: the derivation OWNS the connector-contributed dims
    (``skill_names`` / ``allowed_sdk_tools`` / ``deny_mcp_tool_ids``) — it sets
    them to the union from the pocket's enabled connectors, OVERWRITING any prior
    connector-derived (or hand-set) values for those dims. It PRESERVES the
    user-owned ``ripple_mode`` and ``system_message_override`` already on the
    pocket. ``profile=None`` (no connector contributes anything) clears the
    connector dims back to empty while still preserving the user-owned dims.

    No event is emitted: this is an internal re-derive triggered by a
    connector enable/disable that already emitted ``connector.enabled`` /
    ``connector.disabled``. # no-event — covered by the connector lifecycle event.
    """
    doc = await _fetch_pocket(pocket_id)
    if doc.workspace != workspace_id:
        raise NotFound("pocket", pocket_id)

    existing = getattr(doc, "surface_profile", None)
    # Preserve the user-owned dims regardless of whether a connector contributes.
    ripple_mode = getattr(existing, "ripple_mode", None)
    sys_override = getattr(existing, "system_message_override", None)

    if profile is None:
        skill_names: list[str] = []
        allowed_sdk_tools: list[str] | None = None
        deny_mcp_tool_ids: list[str] = []
    else:
        skill_names = list(profile.skill_names)
        allowed_sdk_tools = (
            list(profile.allowed_sdk_tools) if profile.allowed_sdk_tools is not None else None
        )
        deny_mcp_tool_ids = list(profile.deny_mcp_tool_ids)

    # If nothing remains set on any dim, drop the override entirely (legacy
    # ``None`` shape) so a pocket that never had connector skills stays clean.
    if (
        ripple_mode is None
        and sys_override is None
        and not skill_names
        and allowed_sdk_tools is None
        and not deny_mcp_tool_ids
    ):
        doc.surface_profile = None
    else:
        doc.surface_profile = PocketSurfaceProfile(
            ripple_mode=ripple_mode,
            allowed_sdk_tools=allowed_sdk_tools,
            deny_mcp_tool_ids=deny_mcp_tool_ids,
            skill_names=skill_names,
            system_message_override=sys_override,
        )
    await doc.save()


def _pocket_to_domain(doc: _PocketDoc) -> Pocket:
    return Pocket(
        id=str(doc.id),
        workspace_id=doc.workspace,
        name=doc.name,
        description=doc.description,
        type=doc.type,
        icon=doc.icon,
        color=doc.color,
        owner=doc.owner,
        visibility=doc.visibility,
        team=tuple(str(t) for t in doc.team),
        agents=tuple(str(a) for a in doc.agents),
        widgets=tuple(_widget_to_domain(w) for w in doc.widgets),
        ripple_spec=doc.rippleSpec,
        share_link_token=doc.share_link_token,
        share_link_access=doc.share_link_access,
        shared_with=tuple(doc.shared_with),
        tool_specs=tuple(doc.tool_specs),
        project_id=getattr(doc, "project_id", None),
        # Wave 3e — optional. ``getattr`` for legacy docs that pre-date the field.
        template_slug=getattr(doc, "template_slug", None),
        # Sites landing brain — optional layout pattern. ``getattr`` for
        # legacy docs that pre-date the field.
        pattern=getattr(doc, "pattern", None),
        # Sites svelte track — generation engine + svelte source map.
        # ``getattr`` for legacy docs that pre-date the fields (read back
        # as ``engine="ripple"``, ``source=None``).
        engine=getattr(doc, "engine", "ripple"),
        source=getattr(doc, "source", None),
        # Entity-rooms chunk ② — optional per-entity surface-profile override.
        # ``getattr`` for legacy docs that pre-date the field. Dumped to a plain
        # JSON dict so the domain layer carries the wire shape, not the Beanie
        # sub-model. ``None`` for legacy pockets.
        surface_profile=_surface_profile_to_dict(getattr(doc, "surface_profile", None)),
        created_at=getattr(doc, "createdAt", None),
        updated_at=getattr(doc, "updatedAt", None),
    )


async def _fetch_pocket(pocket_id: str) -> _PocketDoc:
    """Fetch a pocket doc by id; raise NotFound if missing."""
    try:
        doc = await _PocketDoc.get(PydanticObjectId(pocket_id))
    except Exception:
        doc = None
    if doc is None:
        raise NotFound("pocket", pocket_id)
    return doc


def _check_domain_owner(domain_pocket: Pocket, user_id: str) -> None:
    if domain_pocket.owner != user_id:
        from pocketpaw_ee.guards.audit import log_denial

        log_denial(
            actor=user_id,
            action="pocket.share",
            code="pocket.not_owner",
            resource_id=domain_pocket.id,
        )
        raise Forbidden("pocket.not_owner", "Only the pocket owner can perform this action")


def _check_domain_edit_access(domain_pocket: Pocket, user_id: str) -> None:
    if domain_pocket.owner == user_id:
        return
    if user_id in domain_pocket.shared_with:
        return
    if domain_pocket.visibility == "workspace":
        return
    from pocketpaw_ee.guards.audit import log_denial

    log_denial(
        actor=user_id,
        action="pocket.edit",
        code="pocket.access_denied",
        resource_id=domain_pocket.id,
    )
    raise Forbidden("pocket.access_denied", "You do not have edit access to this pocket")


def _build_widget_doc(payload: dict) -> _WidgetDoc:
    return _WidgetDoc(
        name=payload.get("name", "Widget"),
        type=payload.get("type", "custom"),
        icon=payload.get("icon", ""),
        color=payload.get("color", ""),
        span=payload.get("span", "col-span-1"),
        dataSourceType=payload.get("dataSourceType", payload.get("data_source_type", "static")),
        config=payload.get("config", {}),
        props=payload.get("props", {}),
        data=payload.get("data"),
        # ``spec`` is the per-tile rippleSpec subtree the home grid renders.
        # Optional — native widgets and legacy widget entries omit it.
        spec=payload.get("spec"),
        assignedAgent=payload.get("assignedAgent", payload.get("assigned_agent")),
    )


async def _resolved_wire_dict(doc: _PocketDoc, viewer_user_id: str) -> dict:
    """Build the wire dict with rippleSpec ``$source`` markers resolved
    against ``viewer_user_id``'s workspace context.

    Used by every boundary that hands a spec to a renderer:
    ``service.get`` (REST), ``service.create``/``update`` return values,
    and ``_pocket_event_payload`` (WebSocket broadcast). Falls back to
    the raw wire dict on resolver failure — never raises.

    Resolution requires a viewer because sources like ``workspace.pockets``
    apply per-user visibility filters. For multi-recipient broadcasts,
    pass the doc's owner; this can over-share owner's private pockets to
    other recipients (metadata only). Tracked for v2: per-recipient
    resolution or frontend refetch on event receipt.
    """
    import dataclasses

    pocket = _pocket_to_domain(doc)
    if pocket.ripple_spec:
        from pocketpaw_ee.cloud import ripple_sources  # noqa: F401  — register sources
        from pocketpaw_ee.cloud.ripple_resolver import ResolveCtx, resolve_ripple_spec

        try:
            resolved = await resolve_ripple_spec(
                pocket.ripple_spec,
                ResolveCtx(
                    workspace_id=doc.workspace,
                    user_id=viewer_user_id,
                    pocket_id=str(doc.id),
                ),
            )
            pocket = dataclasses.replace(pocket, ripple_spec=resolved)
        except Exception:
            logger.warning(
                "ripple_resolver: resolve failed for pocket %s; returning raw spec",
                str(doc.id),
                exc_info=True,
            )
    return pocket_to_wire_dict(pocket)


async def _pocket_event_payload(
    doc: _PocketDoc, *, missing_senses: list[str] | None = None
) -> dict:
    """Build the realtime event payload for a pocket mutation.

    Always includes ``recipient_ids`` (owner + shared_with). For
    workspace-visible pockets, also includes ``workspace_id`` so the
    audience resolver fans out to every workspace member. Mirrors the
    visibility rules used by ``list_pockets`` so a member only ever sees
    a ``pocket.*`` event for a pocket they could read via REST.

    ``shareLinkToken`` and ``sharedWith`` are stripped from the broadcast
    pocket — workspace-visible pockets fan out to every member, and the
    share token is owner-only state. Owners receive the token directly in
    the REST response from ``generate_share_link``.

    rippleSpec ``$source`` markers are resolved using ``doc.owner`` as
    the viewer. See ``_resolved_wire_dict`` for the v2 follow-up note.
    """
    pocket_dict = await _resolved_wire_dict(doc, doc.owner)
    pocket_dict.pop("shareLinkToken", None)
    pocket_dict.pop("sharedWith", None)
    payload: dict = {
        "pocket_id": str(doc.id),
        "pocket": pocket_dict,
        "recipient_ids": [doc.owner, *list(doc.shared_with or [])],
    }
    if doc.visibility == "workspace":
        payload["workspace_id"] = doc.workspace
    # Sense tier chunk 6a — when a template declared ``needs:`` Senses that no
    # enabled connector fills for this tenant, surface them so the UX can
    # prompt-to-connect. Only present on the create path when non-empty; the
    # absence of the key means "nothing missing".
    if missing_senses:
        payload["missing_senses"] = list(missing_senses)
    return payload


async def _mutate_list_field(pocket_id: str, field: str, value: str, action: str) -> Pocket:
    """Append/remove a string value on shared_with / team / agents.
    Idempotent in both directions. Emits ``PocketUpdated`` when the doc
    actually changes (no-op mutations stay silent)."""
    doc = await _fetch_pocket(pocket_id)
    current: list[str] = list(getattr(doc, field))
    changed = False
    if action == "add":
        if value not in current:
            current.append(value)
            setattr(doc, field, current)
            await doc.save()
            changed = True
    else:
        if value in current:
            current.remove(value)
            setattr(doc, field, current)
            await doc.save()
            changed = True
    if changed:
        await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
    return _pocket_to_domain(doc)


# ---------------------------------------------------------------------------
# Catalog-as-allowlist ingest gate (Increment 5)
# ---------------------------------------------------------------------------


async def _catalog_allowed_types() -> list[str] | None:
    """Resolve the widget-catalog allow-list from the Ripple manifest.

    The manifest's ``widgets`` array is the renderer's closed registry.
    Returns the type list, or ``None`` when the manifest is unavailable
    (network failure, dev server down) — the caller skips the catalog
    gate in that case, best-effort like the manifest-drift validator.
    """
    try:
        from pocketpaw.config import get_settings
        from pocketpaw.ripple.manifest import allowed_types_from_manifest, get_manifest

        settings = get_settings()
        manifest = await get_manifest(
            settings.ripple_manifest_url,
            ttl_seconds=settings.ripple_manifest_ttl_seconds,
        )
        if manifest is None:
            return None
        types = allowed_types_from_manifest(manifest)
        return types or None
    except Exception:
        logger.debug("ripple catalog allow-list lookup skipped (non-fatal)", exc_info=True)
        return None


async def _catalog_required_props() -> dict[str, list[str]] | None:
    """Resolve the per-widget required-prop map from the Ripple manifest.

    Returns ``{type: [required_prop, ...]}`` for the constraint-zone HARD
    required-prop gate, or ``None`` when the manifest is unavailable or no
    widget declares a required prop — the caller skips the gate in that case,
    best-effort like :func:`_catalog_allowed_types`.

    ``get_manifest`` is in-process TTL-cached, so this resolves from the same
    cache entry the allow-list lookup populated — no extra network fetch.
    """
    try:
        from pocketpaw.config import get_settings
        from pocketpaw.ripple.manifest import get_manifest, required_props_from_manifest

        settings = get_settings()
        manifest = await get_manifest(
            settings.ripple_manifest_url,
            ttl_seconds=settings.ripple_manifest_ttl_seconds,
        )
        if manifest is None:
            return None
        required = required_props_from_manifest(manifest)
        return required or None
    except Exception:
        logger.debug("ripple required-prop map lookup skipped (non-fatal)", exc_info=True)
        return None


def _embed_allowed_hosts() -> list[str]:
    """Return the configured ``embed`` host allow-list."""
    try:
        from pocketpaw.config import get_settings

        return list(get_settings().ripple_embed_allowed_hosts or [])
    except Exception:
        logger.debug("ripple embed allow-list lookup skipped (non-fatal)", exc_info=True)
        return []


def _audit_embed_ingest(
    spec: dict[str, Any] | None,
    *,
    actor: str,
    workspace_id: str | None,
    pocket_id: str | None,
) -> None:
    """Write an audit-log entry when an ingested spec contains an ``embed``
    node.

    The ``embed`` widget points an iframe at third-party content; an
    audit trail of every embed-bearing spec that is published lets a
    security review reconstruct what external surfaces a pocket exposed.
    Audit failures must never break the ingest flow — the call is wrapped.
    """
    try:
        from pocketpaw.ripple.manifest import find_embed_nodes

        embeds = find_embed_nodes(spec)
        if not embeds:
            return
        from pocketpaw.security.audit import AuditEvent, AuditSeverity, get_audit_logger

        urls: list[str] = []
        for node in embeds:
            props = node.get("props")
            if isinstance(props, dict):
                url = props.get("url")
                if isinstance(url, str) and url:
                    urls.append(url)
        get_audit_logger().log(
            AuditEvent.create(
                severity=AuditSeverity.WARNING,
                actor=actor,
                action="pocket.embed_ingest",
                target=pocket_id or "(new)",
                status="success",
                category="pocket_embed",
                workspace_id=workspace_id,
                pocket_id=pocket_id,
                embed_count=len(embeds),
                embed_urls=urls,
            )
        )
    except Exception:  # noqa: BLE001 — audit must never break the ingest
        logger.warning("pocket embed-ingest audit-log write failed", exc_info=True)


async def _gate_catalog(
    spec: dict[str, Any] | None,
    *,
    strict: bool,
    actor: str,
    workspace_id: str | None = None,
    pocket_id: str | None = None,
) -> None:
    """Run the catalog-as-allowlist gate over a normalized rippleSpec.

    ``strict=True`` (agent-generation path) raises ``CatalogViolationError``
    on the first violation so the caller can retry the LLM. ``strict=False``
    (human / import path) logs a structured warning per violation and does
    not block. Either way, an embed-bearing spec is audit-logged.

    Best-effort: when the widget manifest can't be fetched the gate is
    skipped (same posture as the manifest-drift validator).
    """
    if not isinstance(spec, dict):
        return
    _audit_embed_ingest(spec, actor=actor, workspace_id=workspace_id, pocket_id=pocket_id)
    allowed_types = await _catalog_allowed_types()
    if allowed_types is None:
        return
    embed_hosts = _embed_allowed_hosts()
    if strict:
        validate_against_catalog_strict(
            spec,
            allowed_types,
            embed_allowed_hosts=embed_hosts,
            pocket_id=pocket_id,
            workspace_id=workspace_id,
        )
        # Action-handler wiring runs as a sibling gate — same strict
        # posture as the catalog walk so the agent-generation path
        # gets a single corrective signal per failed attempt rather
        # than chained-and-shadowed errors. Manifest-driven catalog
        # already passed; this catches the verb-level / Refresh-button
        # failure modes the catalog can't see.
        validate_action_wiring_strict(
            spec,
            pocket_id=pocket_id,
            workspace_id=workspace_id,
        )
        # Required-prop "HARD constraint" gate — same strict posture. Runs
        # after the catalog walk so an unknown ``type`` surfaces as the
        # fundamental "no such widget" error first; this catches the
        # known-widget-but-missing-required-prop case (a ``chart`` with no
        # ``data``) the catalog can't see. Best-effort: skipped when the
        # required-prop map can't be resolved.
        required_props = await _catalog_required_props()
        if required_props:
            validate_required_props_strict(
                spec,
                required_props,
                pocket_id=pocket_id,
                workspace_id=workspace_id,
            )
    else:
        validate_against_catalog_logged(
            spec,
            allowed_types,
            embed_allowed_hosts=embed_hosts,
            pocket_id=pocket_id,
            workspace_id=workspace_id,
        )
        validate_action_wiring_logged(
            spec,
            pocket_id=pocket_id,
            workspace_id=workspace_id,
        )
        required_props = await _catalog_required_props()
        if required_props:
            validate_required_props_logged(
                spec,
                required_props,
                pocket_id=pocket_id,
                workspace_id=workspace_id,
            )


# Widget ``type`` whose tiles carry no rippleSpec — the frontend renders them
# as a built-in Svelte component keyed on ``name``. Mirrors the
# ``_NATIVE_WIDGET_TYPE`` constant in the MCP server; duplicated here so the
# service layer can short-circuit the gate without importing from the agent
# package (which would invert the cloud → agent dependency direction).
_NATIVE_WIDGET_TYPE = "native"


async def _gate_widget_spec_for_agent(
    spec: dict[str, Any] | None,
    *,
    widget_type: str | None,
    actor: str,
    workspace_id: str | None,
    pocket_id: str | None,
) -> str | None:
    """Run the catalog + action-wiring gates over a single widget's ``spec``
    subtree on the agent-generation path. Returns an agent-readable error
    string on rejection, or ``None`` when the spec passes (or there's nothing
    to gate).

    Mirrors the strict half of :func:`_gate_catalog` — the same posture used
    by the pocket-level granular ops (``agent_replace_node`` etc.) — but
    scoped to a single ``widgets[].spec`` subtree rather than the
    ``rippleSpec.ui`` tree. ``type="native"`` widgets and missing specs are
    a no-op; the manifest-unavailable best-effort posture is inherited
    from :func:`_gate_catalog`.

    The catalog walker tolerates an unwrapped subtree (its first line is
    ``root = spec.get("ui") if isinstance(spec.get("ui"), dict) else spec``)
    so we hand the widget spec in directly, no synthetic ``ui`` wrapping.
    """
    if widget_type == _NATIVE_WIDGET_TYPE:
        return None
    if not isinstance(spec, dict):
        return None
    try:
        await _gate_catalog(
            spec,
            strict=True,
            actor=actor,
            workspace_id=workspace_id,
            pocket_id=pocket_id,
        )
    except CatalogViolationError as exc:
        return format_violations_for_agent(exc.violations)
    except ActionWiringViolationError as exc:
        return format_action_violations_for_agent(exc.violations)
    except MissingRequiredPropError as exc:
        return format_required_prop_violations_for_agent(exc.violations)
    return None


# ---------------------------------------------------------------------------
# Template compile-on-install + resolver (RFC 03 v2 / Wave 3e)
# ---------------------------------------------------------------------------
#
# A pocket created or updated with a ``template_slug`` is "instantiated"
# from one of the bundled RFC 03 v2 templates: the OSS loader reads the
# template off disk, the OSS compile pass translates it into a runtime-
# shaped dict, and the EE service MERGES that dict into the pocket's
# ``rippleSpec`` (option B — keep user-customized fields, replace the
# compile-output keys).
#
# Canvas ownership rule (extends option B; fix for the empty-canvas bug
# found when templates were deployed to a live instance): the compile
# pass translates the template's YAML metadata only — the render tree
# lives in the hand-authored sibling ``ripple_spec.json``. The merge
# decides who owns the canvas by looking at the pocket's EXISTING ``ui``:
#
#   * empty/absent ``ui``  → template-owned. The template's ``ui`` is
#     adopted, and its seed ``state`` / placeholder ``sources`` merge
#     UNDER the compiled values so the canvas renders non-empty.
#   * non-empty ``ui``     → user-owned. A recompile (template upgraded
#     out-of-band) refreshes the machinery only and never touches the
#     canvas — a user can tweak ``rippleSpec.ui`` after instantiation
#     and a re-apply won't blow those tweaks away.
#
# Tolerance posture: ``load_template(slug, strict=False)`` never raises;
# a stale / missing / malformed template returns ``None``. The pocket
# keeps its slug (so a later resolver attempt can retry) and its
# rippleSpec is left unmodified — the create/update never fails just
# because the template went away. A WARNING is logged so an operator can
# see the drift.


def _compile_template_to_runtime_dict(loaded: dict[str, Any] | None) -> dict[str, Any] | None:
    """Translate an OSS ``load_template`` result into the runtime rippleSpec dict.

    Returns the compile output dict on success, or ``None`` if the
    loader returned ``None`` (unknown / stale slug — see the section
    docstring for the tolerance posture). Validation failures inside
    the loader already became ``None`` under ``strict=False``; this
    function only re-runs ``PocketTemplate.model_validate`` to obtain
    the typed model the OSS ``compile_template`` consumes.
    """
    if loaded is None:
        return None
    meta = loaded.get("meta")
    if not isinstance(meta, dict):
        return None

    # Lazy imports — OSS bundled_templates is the only place that owns the
    # ``PocketTemplate`` schema + ``compile_template`` pure function.
    from pocketpaw.bundled_templates import PocketTemplate, compile_template

    try:
        template = PocketTemplate.model_validate(meta)
    except Exception as exc:  # noqa: BLE001 — the loader already deferred validation
        logger.warning("template compile: schema validation failed: %s", exc)
        return None
    try:
        return compile_template(template)
    except Exception as exc:  # noqa: BLE001 — compile is pure; bug in OSS if it raises
        logger.warning("template compile: compile_template raised: %s", exc)
        return None


def _ui_is_empty(ui: Any) -> bool:
    """True when a rippleSpec ``ui`` value counts as "no authored canvas".

    Absent, ``None``, a non-dict, or an empty dict are all empty. A dict
    with ANY content (even just ``{"type": "stack"}``) is a real canvas —
    we never second-guess a node the user (or an agent) authored.
    """
    return not isinstance(ui, dict) or not ui


def _merge_compile_into_ripple_spec(
    existing: dict[str, Any] | None,
    compiled: dict[str, Any],
    *,
    template_ripple_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge a ``compile_template`` result into an existing rippleSpec.

    Merge strategy (option B in the Wave 3e brief): keys produced by
    the compile pass (``sources``, ``state``, ``actions``, ``agents``,
    ``triggers``, ``outcomes``, etc.) REPLACE matching keys in the
    existing spec. The result is a NEW dict; no input is mutated.

    The ``ui`` tree follows an ownership rule keyed on the EXISTING
    spec's canvas (fix for the empty-canvas bug found on a live deploy —
    pockets created from a template rendered ``ui.children: 0``):

    * **Existing ``ui`` empty/absent → template-owned.** The pocket has
      no authored canvas, so the template's hand-authored sibling
      ``ripple_spec.json`` supplies it: its ``ui`` tree is adopted, and
      its seed ``state`` / placeholder ``sources`` are merged UNDER the
      compiled values (compiled wins per key) so the adopted canvas has
      the bindings it references and renders non-empty on first install.
    * **Existing ``ui`` non-empty → user-owned.** A re-apply (template
      upgraded out-of-band, recompile-on-update) refreshes the machinery
      without touching the canvas — exactly the pre-fix behaviour.

    ``template_ripple_spec`` is the loader's ``loaded["ripple_spec"]``
    sibling dict; pass ``None`` to skip canvas adoption entirely (the
    pre-fix machinery-only merge). The authoring-only
    ``_placeholder_note`` key is never copied — only ``ui`` / ``state``
    / ``sources`` participate in adoption. Adopted blocks are
    deep-copied so the merged spec never aliases the loader's dicts.
    """
    out: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    canvas_is_template_owned = _ui_is_empty(out.get("ui"))

    for key, value in compiled.items():
        out[key] = value

    if canvas_is_template_owned and isinstance(template_ripple_spec, dict):
        template_ui = template_ripple_spec.get("ui")
        if isinstance(template_ui, dict) and template_ui:
            out["ui"] = copy.deepcopy(template_ui)
            # The adopted canvas binds against the template's seed state
            # ({state.records}, {state.draft}, ...) and its placeholder
            # sources. Merge them under the compiled values: compiled
            # keys (the validated schema output) win every collision.
            template_state = template_ripple_spec.get("state")
            if isinstance(template_state, dict):
                compiled_state = out.get("state")
                out["state"] = {
                    **copy.deepcopy(template_state),
                    **(compiled_state if isinstance(compiled_state, dict) else {}),
                }
            template_sources = template_ripple_spec.get("sources")
            if isinstance(template_sources, dict):
                compiled_sources = out.get("sources")
                out["sources"] = {
                    **copy.deepcopy(template_sources),
                    **(compiled_sources if isinstance(compiled_sources, dict) else {}),
                }
    return out


async def _check_template_needs(loaded: dict[str, Any] | None, workspace_id: str) -> list[str]:
    """Return the template's declared Sense ids that NO enabled connector fills.

    A vertical template may declare ``needs: ["paw.payments.v1", ...]`` — the
    provider-agnostic capabilities it expects the tenant to have wired up. For
    each need, ask the EE Sense resolver whether an enabled connector fills it
    for ``workspace_id``; ids that resolve to ``None`` are returned as the
    ``missing`` set so the caller can surface a prompt-to-connect.

    Tolerance: this never raises and never blocks. A ``None`` loaded result, a
    missing/empty ``needs``, or a resolver error all yield ``[]`` (or skip the
    offending id) — mirrors the ``strict=False`` posture used elsewhere here.
    """
    if not isinstance(loaded, dict):
        return []
    meta = loaded.get("meta")
    if not isinstance(meta, dict):
        return []
    needs = meta.get("needs")
    if not needs:
        return []

    # Lazy import — keep the EE Sense resolver off the eager import path.
    from pocketpaw_ee.cloud.senses.resolver import resolve_many

    # One enabled-connector read for ALL needs (was one query per need).
    try:
        resolved = await resolve_many(list(needs), workspace_id)
    except Exception as exc:  # noqa: BLE001 — never block create on a resolver hiccup
        logger.warning(
            "template needs: resolve_many raised for workspace=%s: %s",
            workspace_id,
            exc,
        )
        return []

    return [sense_id for sense_id in needs if resolved.get(sense_id) is None]


async def resolve_pocket_template(
    workspace_id: str,
    pocket_id: str,
    *,
    templates_dir: Path | None = None,
) -> Any | None:
    """Return the resolved :class:`PocketTemplate` for a pocket, or ``None``.

    Tenant-filtered: a pocket in a different workspace, a missing
    pocket, an unset ``template_slug``, or a template the loader can't
    read all return ``None``. Callers (``bulk_dispatch``,
    ``temporal_scheduler``) treat ``None`` as "no-op for this pocket"
    — never an error.

    Args:
        workspace_id: Tenancy. Required.
        pocket_id: The pocket to resolve. Required.
        templates_dir: Optional override of the OSS loader's templates
            root — used by tests to point at a tmp install. Defaults to
            ``~/.pocketpaw/templates/`` via the OSS default.

    Returns:
        A validated ``PocketTemplate`` instance, or ``None`` for any
        of the failure modes above.
    """
    if not workspace_id or not pocket_id:
        return None
    try:
        doc = await _PocketDoc.get(PydanticObjectId(pocket_id))
    except Exception:  # noqa: BLE001 — malformed id surfaces as "not found"
        return None
    # Rule 7 — tenant filter on every read.
    if doc is None or doc.workspace != workspace_id:
        return None
    slug = getattr(doc, "template_slug", None)
    if not slug:
        return None

    # Lazy import — keep the OSS schema off the eager import path.
    from pocketpaw.bundled_templates import PocketTemplate, load_template

    loaded = load_template(slug, templates_dir=templates_dir, strict=False)
    if loaded is None:
        logger.warning(
            "resolve_pocket_template: pocket=%s slug=%r could not be loaded — returning None",
            pocket_id,
            slug,
        )
        return None
    meta = loaded.get("meta")
    if not isinstance(meta, dict):
        return None
    try:
        return PocketTemplate.model_validate(meta)
    except Exception as exc:  # noqa: BLE001 — strict=False already swallowed validation
        logger.warning(
            "resolve_pocket_template: pocket=%s slug=%r post-load validation failed: %s",
            pocket_id,
            slug,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


async def create(workspace_id: str, user_id: str, body: CreatePocketRequest) -> dict:
    """Create a pocket with optional agents, widgets, and rippleSpec.

    ``project_id`` is optional — when supplied, the service validates it
    points at a real project in the same workspace and rejects with
    ``project.not_found`` otherwise. Pockets without a ``project_id``
    surface as "unassigned" in the Mission Control project picker, which
    is exactly the pre-projects-rollout shape.
    """
    from pocketpaw_ee.cloud.sessions import service as sessions_service

    normalized_spec = normalize_ripple_spec(body.ripple_spec) if body.ripple_spec else None
    # Wave 3e — compile-on-install. When ``template_slug`` is set, load
    # the bundled template, compile it, and merge the runtime dict into
    # the caller-supplied rippleSpec (option B merge — see the section
    # docstring above ``_merge_compile_into_ripple_spec``). A stale /
    # missing slug logs a warning and leaves the rippleSpec unchanged —
    # the pocket still gets created and keeps the slug for retry.
    loaded: dict[str, Any] | None = None
    if body.template_slug:
        from pocketpaw.bundled_templates import load_template

        loaded = load_template(body.template_slug, strict=False)
        compiled = _compile_template_to_runtime_dict(loaded)
        if compiled is not None:
            normalized_spec = _merge_compile_into_ripple_spec(
                normalized_spec,
                compiled,
                template_ripple_spec=loaded.get("ripple_spec") if loaded else None,
            )

    if normalized_spec:
        validate_ripple_spec_logged(normalized_spec, workspace_id=workspace_id)
        # Human/import path — catalog drift is logged for triage, not blocked.
        await _gate_catalog(normalized_spec, strict=False, actor=user_id, workspace_id=workspace_id)
    widget_docs = [_build_widget_doc(w) for w in (body.widgets or [])]

    if body.project_id:
        await _ensure_project_in_workspace(workspace_id, body.project_id)

    doc = _PocketDoc(
        workspace=workspace_id,
        project_id=body.project_id,
        name=body.name,
        description=body.description,
        type=body.type,
        icon=body.icon,
        color=body.color,
        owner=user_id,
        visibility=body.visibility,
        agents=list(body.agents or []),
        widgets=widget_docs,
        rippleSpec=normalized_spec,
        template_slug=body.template_slug,
        pattern=body.pattern,
        engine=body.engine,
        source=body.source,
        surface_profile=body.surface_profile,
        # New pockets start with NO connectors allowed — the owner must
        # explicitly grant each connector via the permission UI.
        allowed_connectors=[],
    )
    await doc.insert()
    pocket = _pocket_to_domain(doc)

    if body.session_id:
        await sessions_service.link_pocket(workspace_id, body.session_id, pocket.id)

    # Sense tier chunk 6a — a template can declare the Senses it needs. For each,
    # check the tenant has an enabled provider; collect the ids with none. This
    # NEVER blocks creation (the pocket is already inserted) — it logs a warning
    # and rides out on the pocket.created event so the UX can prompt-to-connect.
    missing_senses = await _check_template_needs(loaded, workspace_id)
    if missing_senses:
        logger.warning(
            "template needs: pocket=%s slug=%r workspace=%s has no enabled "
            "provider for senses %s — pocket created, prompt-to-connect surfaced",
            str(doc.id),
            body.template_slug,
            workspace_id,
            missing_senses,
        )

    await emit(PocketCreated(data=await _pocket_event_payload(doc, missing_senses=missing_senses)))
    return await _resolved_wire_dict(doc, user_id)


async def ensure_home_pocket(workspace_id: str, user_id: str) -> tuple[dict, bool]:
    """Resolve-or-provision the caller's home pocket.

    Returns ``(pocket_wire_dict, created)``. ``created`` is ``True`` only
    when this call provisioned a brand-new home pocket, ``False`` when it
    returned an existing one. The home route surfaces ``created`` so the
    client can gate one-time work — seeding default widgets, migrating
    legacy ``localStorage`` widgets — on a genuinely fresh pocket and
    never re-seed a returning user who deliberately cleared their grid.

    Idempotent. If the user's ``home_pocket_id`` setting points at a pocket
    that still exists and is owned by the user, that pocket is returned
    unchanged with ``created=False``. Otherwise a fresh ``type="home"``
    pocket is created (``name="Home"``, ``visibility="private"``, no
    widgets), its id is persisted back onto the user setting, and it is
    returned with ``created=True``.

    A stale ``home_pocket_id`` — the pocket was deleted, or it now belongs
    to someone else — falls through to re-provisioning rather than raising.

    **Concurrency.** Provisioning persists the new id with an atomic
    compare-and-swap (``auth_service.claim_home_pocket_id``), not a plain
    write. If two concurrent first-login calls both insert a pocket, only
    one wins the swap; the loser deletes its own orphan pocket, adopts the
    winner's, and returns it with ``created=False``. The home page is
    therefore single-pocket even under a provision race.

    Provisioning is deliberately empty: the client owns the home page's
    default / seed widgets. This service only guarantees a backing pocket
    exists.
    """
    from pocketpaw_ee.cloud.auth import service as auth_service

    existing_id = await auth_service.get_home_pocket_id(user_id)
    if existing_id:
        try:
            doc = await _fetch_pocket(existing_id)
        except NotFound:
            doc = None
        if doc is not None and doc.owner == user_id:
            return await _resolved_wire_dict(doc, user_id), False

    doc = _PocketDoc(
        workspace=workspace_id,
        name="Home",
        description="",
        type=HOME_POCKET_TYPE,
        owner=user_id,
        visibility="private",
        widgets=[],
    )
    await doc.insert()

    # Compare-and-swap the new id onto the user. ``expected`` is whatever we
    # read above — None for a genuine first provision, the stale id when
    # re-provisioning. If the swap fails another writer beat us here.
    claimed = await auth_service.claim_home_pocket_id(user_id, str(doc.id), expected=existing_id)
    if not claimed:
        winner_id = await auth_service.get_home_pocket_id(user_id)
        if winner_id and winner_id != str(doc.id):
            try:
                winner = await _fetch_pocket(winner_id)
            except NotFound:
                winner = None
            if winner is not None and winner.owner == user_id:
                # Adopt the winner's pocket and drop our orphan.
                await doc.delete()
                return await _resolved_wire_dict(winner, user_id), False
        # The winner's pocket couldn't be resolved (deleted in the race
        # window). Keep our own pocket rather than orphan the user — but it
        # is not the id of record, so report created=True and move on.

    await emit(PocketCreated(data=await _pocket_event_payload(doc)))
    return await _resolved_wire_dict(doc, user_id), True


async def list_pockets(
    workspace_id: str,
    user_id: str,
    *,
    project_id: str | None = None,
    exclude_pocket_ids: set[str] | None = None,
) -> list[dict]:
    """List pockets visible to the user (owned, shared_with, or workspace-visible).

    Each returned pocket has its rippleSpec ``$source`` markers resolved
    against ``user_id``'s context — the desktop client renders the canvas
    directly from this list response, so unresolved markers would surface
    as empty widgets. Resolution per pocket is independent; sources that
    fail fall back to raw markers individually (see ``_resolved_wire_dict``).

    ``project_id`` filter: when provided, narrows the result to pockets
    whose ``project_id`` matches. Pass an empty string to filter for
    "no project assigned" — that's the Mission Control "Unassigned"
    bucket. Kept as a kwarg so existing callers don't change.

    ``exclude_pocket_ids`` filter: when provided, drops pockets whose id is
    in the set via ``{"_id": {"$nin": [...]}}``. The /pockets gallery passes
    the ids of pockets already published as Sites (they show under /sites,
    not in the pocket gallery). The ids arrive as STRINGS (the wire form), but
    Beanie persists ``_id`` as an ``ObjectId`` (the ``Pocket.id: str`` annotation
    is overridden by the Document base), so each id is cast to ``PydanticObjectId``
    before the ``$nin`` — a string ``$nin`` would silently match nothing and
    leak every site back into the gallery. Malformed ids are skipped (they can't
    match any stored ``_id`` anyway). A ``None`` / empty set is a no-op so every
    other caller (mission control, planners, kb, surface) is unchanged.
    """
    query: dict = {
        "workspace": workspace_id,
        "$or": [
            {"owner": user_id},
            {"shared_with": user_id},
            {"visibility": "workspace"},
        ],
    }
    if project_id is not None:
        # Empty string is intentional → "no project assigned".
        query["project_id"] = project_id or None
    if exclude_pocket_ids:
        # _id is stored as an ObjectId — cast the wire-string ids. Skip any
        # malformed id rather than raise: it can't match a stored _id anyway.
        oids: list[PydanticObjectId] = []
        for pid in exclude_pocket_ids:
            try:
                oids.append(PydanticObjectId(pid))
            except (InvalidId, TypeError, ValueError):
                continue
        if oids:
            query["_id"] = {"$nin": oids}
    docs = await _PocketDoc.find(query).to_list()
    return [await _resolved_wire_dict(d, user_id) for d in docs]


async def get(pocket_id: str, user_id: str) -> dict:
    """Get a single pocket. Access check: owner, shared_with, or workspace-visible.

    rippleSpec $source markers are resolved on read against the calling user's
    workspace context.
    """
    doc = await _fetch_pocket(pocket_id)
    pocket = _pocket_to_domain(doc)
    if (
        pocket.owner != user_id
        and user_id not in pocket.shared_with
        and pocket.visibility == "private"
    ):
        raise Forbidden("pocket.access_denied", "You do not have access to this pocket")
    return await _resolved_wire_dict(doc, user_id)


async def update(pocket_id: str, user_id: str, body: UpdatePocketRequest) -> dict:
    """Update pocket fields. Edit-access required; visibility changes require ownership."""
    doc = await _fetch_pocket(pocket_id)
    pocket = _pocket_to_domain(doc)

    _check_domain_edit_access(pocket, user_id)
    if body.visibility is not None:
        _check_domain_owner(pocket, user_id)

    normalized_spec = normalize_ripple_spec(body.ripple_spec) if body.ripple_spec else None
    # Wave 3e — recompile-on-update when ``template_slug`` is supplied in
    # the body (different OR same slug — same body slug forces a
    # refresh against the on-disk template). The compile output merges
    # into either the caller-supplied rippleSpec or the existing one.
    # Same tolerance posture as ``create``: a stale slug leaves the
    # rippleSpec untouched and logs a warning.
    if body.template_slug is not None:
        from pocketpaw.bundled_templates import load_template

        loaded = load_template(body.template_slug, strict=False)
        compiled = _compile_template_to_runtime_dict(loaded)
        if compiled is not None:
            merge_base = normalized_spec if normalized_spec is not None else doc.rippleSpec
            normalized_spec = _merge_compile_into_ripple_spec(
                merge_base,
                compiled,
                template_ripple_spec=loaded.get("ripple_spec") if loaded else None,
            )

    if normalized_spec:
        validate_ripple_spec_logged(
            normalized_spec, pocket_id=str(doc.id), workspace_id=doc.workspace
        )
        # Human/import path — catalog drift is logged for triage, not blocked.
        await _gate_catalog(
            normalized_spec,
            strict=False,
            actor=user_id,
            workspace_id=doc.workspace,
            pocket_id=str(doc.id),
        )

    if body.name is not None:
        doc.name = body.name
    if body.description is not None:
        doc.description = body.description
    if body.type is not None:
        doc.type = body.type
    if body.icon is not None:
        doc.icon = body.icon
    if body.color is not None:
        doc.color = body.color
    if body.visibility is not None:
        doc.visibility = body.visibility
    if normalized_spec is not None:
        doc.rippleSpec = normalized_spec
    if body.template_slug is not None:
        doc.template_slug = body.template_slug
    if body.project_id is not None:
        # Empty string is the "unassign" signal; non-empty must point at a
        # real project in the same workspace.
        if body.project_id:
            await _ensure_project_in_workspace(doc.workspace, body.project_id)
            doc.project_id = body.project_id
        else:
            doc.project_id = None
    # Per-entity surface-profile override (the auto-authoring write path).
    # ``None`` is BOTH the field default and the "clear" signal, so the
    # absent-vs-explicit-null distinction rides on ``model_fields_set``
    # (the ``exclude_unset`` convention) — NOT on ``is not None``:
    #   present + non-null → set/replace; explicit null → clear; absent →
    #   leave unchanged (no clobber on an unrelated edit). Assigns the
    #   ``PocketSurfaceProfile`` sub-model (or ``None``) onto the doc field
    #   exactly as the create path does — same shape, same normalization.
    if "surface_profile" in body.model_fields_set:
        doc.surface_profile = body.surface_profile
    await doc.save()
    await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
    return await _resolved_wire_dict(doc, user_id)


async def set_svelte_source_file(
    pocket_id: str,
    user_id: str,
    *,
    component_path: str,
    new_source: str,
) -> tuple[dict, str]:
    """Rewrite ONE file in a svelte-engine pocket's ``source`` map and persist it.

    The svelte analog of editing a single component of a Paw Site: ``source`` is
    a ``{relative_path: file_contents}`` map (the hand-written SvelteKit files),
    and this replaces the contents at ``component_path`` with ``new_source``. The
    Pocket Beanie write stays inside the pockets service (entity isolation — the
    sites service orchestrates the republish but never touches the Pocket model).

    Access mirrors ``update``: explicit ``(pocket_id, user_id)`` with
    ``_check_domain_edit_access`` (owner / shared_with / workspace-visible). A
    missing pocket raises ``NotFound``; a non-svelte pocket or a non-existent
    component path raise the obvious cloud errors rather than silently creating a
    file or dereferencing ``None``:
      * not a svelte pocket (``engine != "svelte"`` or no ``source`` map) →
        ``ValidationError`` (422);
      * ``component_path`` absent from the map → ``NotFound`` (404) on the
        component, so a typo'd path is not a silent create.

    Returns ``(resolved_wire_dict, previous_source)`` — the wire dict every other
    write returns, plus the file's PRIOR contents so the caller can roll the edit
    back if the downstream republish fails its smoke gate (the sites service does
    exactly this so a broken edit never leaves stale source on the pocket).
    """
    doc = await _fetch_pocket(pocket_id)
    _check_domain_edit_access(_pocket_to_domain(doc), user_id)

    if getattr(doc, "engine", "ripple") != "svelte" or not isinstance(doc.source, dict):
        raise ValidationError(
            "pocket.not_svelte_site",
            "This pocket is not a svelte Paw Site — it has no component source map to edit.",
        )
    if component_path not in doc.source:
        raise NotFound("site_component", component_path)

    previous_source = doc.source[component_path]
    # Reassign a fresh dict so Beanie tracks the change (in-place mutation of a
    # dict field is not always detected as dirty by the ODM).
    updated = dict(doc.source)
    updated[component_path] = new_source
    doc.source = updated
    await doc.save()
    await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
    # Branch primitive (BP-3): a svelte source edit ALSO writes a draft
    # ArtifactVersion. BP-1 only hooked the rippleSpec write (merge_spec →
    # _record_pocket_draft_version); svelte edits go through THIS function and
    # were not versioned. The snapshot is the full svelte ``source`` map (the
    # version model's ``content`` accepts either a rippleSpec dict OR a svelte
    # source map). Same best-effort try/except as the rippleSpec hook —
    # versioning is an additive history layer, never a gate on the edit.
    await _record_pocket_svelte_draft_version(doc, author=user_id)
    return await _resolved_wire_dict(doc, user_id), previous_source


async def merge_spec(
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    body: MergeSpecRequest | dict[str, Any],
) -> dict:
    """One-shot rippleSpec write — full replace OR partial merge.

    MVP entry point for the new ``POST /spec/merge`` endpoint. Replaces
    the 17-tool LangChain edit surface with a single server-side merge
    that the agent invokes via ``curl`` after the ``pocketpaw-pocket-
    specialist`` skill teaches it the rippleSpec shape + conventions.

    Body must carry EXACTLY ONE of ``replace`` (whole new spec wholesale
    replaces the existing one) or ``merge`` (partial spec — top-level
    keys overwrite, ``state`` is shallow-merged, ``ui`` nodes are
    replaced by id via ``merge_ripple_spec``).

    Accepts either the typed ``MergeSpecRequest`` model the router
    constructs from the HTTP body OR a plain dict (for internal
    callers — CLI / bus handlers / tests). The first line below
    re-validates via ``model_validate`` so the exactly-one rule is
    enforced regardless of which path the caller came in through —
    per cloud convention #6 (validate at entry).

    Validation runs the STRICT catalog + action-wiring gates (mirrors
    the agent-generation path in ``create_from_ripple_spec``). A
    catalog or action-wiring violation BLOCKS the persist and returns
    ``{ok: false, warnings: [...]}``. Expression-grammar warnings
    (``validate_ripple_spec_logged``) are non-blocking — they are
    surfaced in the warnings list but the merge persists anyway.

    Edit-access is required; ``visibility`` changes via this endpoint
    are NOT supported (use the existing PATCH ``/{pocket_id}``). The
    response mirrors the existing wire shape so the desktop client can
    re-render directly from the result.
    """
    # Validate at entry (cloud rule #6) — accept either the typed
    # model or a raw dict and normalize to the model. ``model_validate``
    # re-runs the exactly-one ``model_validator``, so a dict caller
    # carrying both / neither raises here instead of later.
    if isinstance(body, MergeSpecRequest):
        parsed = body
    else:
        try:
            parsed = MergeSpecRequest.model_validate(body)
        except Exception as exc:  # noqa: BLE001 — pydantic validation
            raise ValidationError(
                "spec_merge.invalid_body",
                str(exc),
            ) from exc

    doc = await _fetch_pocket(pocket_id)
    pocket = _pocket_to_domain(doc)
    _check_domain_edit_access(pocket, user_id)

    # Compute the new spec.
    orphans: list[str] = []
    base_spec = doc.rippleSpec or {}
    # Top-level state keys present BEFORE this edit — so the orphan-state
    # warning below names only keys this edit ADDED, not pre-existing
    # orphans the agent didn't touch (avoids re-warning noise on replace).
    base_state_keys: set[str] = set(
        base_spec.get("state", {}) if isinstance(base_spec.get("state"), dict) else {}
    )
    if parsed.replace is not None:
        new_spec_raw = parsed.replace
    else:
        assert parsed.merge is not None  # narrowing for type checkers
        new_spec_raw, orphans = merge_ripple_spec(base_spec, parsed.merge)

    # Normalize + validate (mirror the create_from_ripple_spec gate
    # sequence at line 794+ — strict on catalog + action-wiring, logged
    # on expression grammar).
    normalized = normalize_ripple_spec(new_spec_raw) if new_spec_raw else None
    warnings: list[str] = []
    if orphans:
        warnings.append(
            "Patch referenced node ids not present in the base tree — "
            f"dropped: {', '.join(orphans)}. To add a new node, re-state "
            "its parent with the new child appended to children."
        )

    # Orphan-state warning (issue #1301) — NON-BLOCKING. A state-only
    # merge patch (an add-widget intent that never carried a ui node)
    # adds a state key no widget reads, so it renders nothing. Mirror the
    # orphan-node warning above: name the newly-added unreferenced keys so
    # the specialist self-correction loop can wire a widget. Limit to keys
    # this edit ADDED — a pre-existing orphan or a legitimate ``sources``
    # bind target must not re-warn (the collector already excludes the
    # latter).
    if normalized:
        added_orphan_state = [
            k for k in find_unreferenced_state_keys(normalized) if k not in base_state_keys
        ]
        if added_orphan_state:
            warnings.append(
                "Added state key(s) with no ui widget reading them — "
                f"{', '.join(added_orphan_state)}. A new state key with no "
                "binding widget renders nothing. Add the ui node that reads "
                "it (a `{state.<key>}` expression or a `bind`), or declare a "
                "`sources` entry that binds it."
            )

    if normalized:
        # Expression-grammar — never blocks; collect for the caller.
        grammar_warnings = validate_ripple_spec_logged(
            normalized, pocket_id=str(doc.id), workspace_id=doc.workspace
        )
        for w in grammar_warnings:
            warnings.append(f"[expr] {w.path}: {w.detail} (expr: {w.expression})")

        # Strict catalog + action-wiring — these BLOCK. Convert the
        # raised error into an agent-readable warnings payload and bail
        # without persisting.
        try:
            await _gate_catalog(
                normalized,
                strict=True,
                actor=user_id,
                workspace_id=doc.workspace,
                pocket_id=str(doc.id),
            )
        except CatalogViolationError as exc:
            return {
                "ok": False,
                "pocket_id": str(doc.id),
                "rippleSpec": doc.rippleSpec,
                "warnings": warnings + [format_violations_for_agent(exc.violations)],
            }
        except ActionWiringViolationError as exc:
            return {
                "ok": False,
                "pocket_id": str(doc.id),
                "rippleSpec": doc.rippleSpec,
                "warnings": warnings + [format_action_violations_for_agent(exc.violations)],
            }
        except MissingRequiredPropError as exc:
            return {
                "ok": False,
                "pocket_id": str(doc.id),
                "rippleSpec": doc.rippleSpec,
                "warnings": warnings + [format_required_prop_violations_for_agent(exc.violations)],
            }

    # Persist + emit.
    doc.rippleSpec = normalized
    await doc.save()
    await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
    # Branch primitive (BP-1): record a draft ArtifactVersion snapshot of the
    # mutated rippleSpec. ADDITIVE — the destructive overwrite above already
    # happened and stays the source of truth for rendering; this captures a
    # draft/history snapshot so the artifact can later have draft/published/
    # rollback (BP-3/BP-4). Best-effort: a version-store failure must NOT break
    # the merge, so it is wrapped and only logged.
    await _record_pocket_draft_version(doc, author=user_id)
    resolved = await _resolved_wire_dict(doc, user_id)
    return {
        "ok": True,
        "pocket_id": str(doc.id),
        "rippleSpec": resolved.get("rippleSpec"),
        "pocket": resolved,
        "warnings": warnings,
    }


async def _record_pocket_draft_version(doc: _PocketDoc, *, author: str | None) -> None:
    """Snapshot a pocket's current rippleSpec as a draft ArtifactVersion.

    Branch-primitive hook (BP-1). Lazy-imports the versions service so the
    pockets entity does not take a hard import on the versions package and so
    a fork without the versions module degrades gracefully. The snapshot is
    the full ``rippleSpec`` dict (empty dict when None). Failures are logged
    and swallowed — versioning is an additive audit/history layer over the
    existing merge, never a gate on it.

    TODO(BP-3): a merge gate may branch this draft for human review before it
    becomes the published pointer.
    """
    try:
        from pocketpaw_ee.versions import service as versions_service

        await versions_service.write_draft(
            scope_type="pocket",
            scope_id=str(doc.id),
            workspace_id=doc.workspace,
            content=doc.rippleSpec or {},
            author=author,
        )
    except Exception:  # noqa: BLE001 — versioning must not break the merge
        logger.warning(
            "versions: failed to record draft version for pocket %s — "
            "merge persisted, version skipped",
            doc.id,
            exc_info=True,
        )


async def _record_pocket_svelte_draft_version(doc: _PocketDoc, *, author: str | None) -> None:
    """Snapshot a svelte pocket's current ``source`` map as a draft ArtifactVersion.

    Branch-primitive hook (BP-3). The svelte analog of
    ``_record_pocket_draft_version``: BP-1 versioned the rippleSpec write
    (merge_spec); svelte source edits go through ``set_svelte_source_file`` and
    were not versioned, so a published svelte site had no draft version to
    promote. This captures the full ``source`` map (``{path: contents}``) as the
    version ``content`` — the version model accepts either shape — with
    ``scope_type="pocket"`` (BP-2 keys site versions on the source pocket).

    Lazy-imports the versions service so the pockets entity takes no hard import
    on it; failures are logged and swallowed — versioning is an additive
    history/Branch layer over the edit, never a gate on it.
    """
    try:
        from pocketpaw_ee.versions import service as versions_service

        await versions_service.write_draft(
            scope_type="pocket",
            scope_id=str(doc.id),
            workspace_id=doc.workspace,
            content=dict(doc.source) if isinstance(doc.source, dict) else {},
            author=author,
        )
    except Exception:  # noqa: BLE001 — versioning must not break the edit
        logger.warning(
            "versions: failed to record svelte draft version for pocket %s — "
            "edit persisted, version skipped",
            doc.id,
            exc_info=True,
        )


async def _ensure_project_in_workspace(workspace_id: str, project_id: str) -> None:
    """Validate that ``project_id`` exists in the workspace. Lazy-imports
    the projects service to avoid a circular import at module load and
    to degrade silently if the projects entity is missing on a fork.
    """
    try:
        from pocketpaw_ee.cloud.projects import service as projects_service
    except Exception:
        # Projects entity unavailable on this branch — accept the id as-is
        # for forward-compat; the rollout PR has the entity present.
        return
    ok = await projects_service.exists_in_workspace(workspace_id, project_id)
    if not ok:
        raise NotFound("project", project_id)


async def unassign_project_on_pockets(workspace_id: str, project_id: str) -> int:
    """Soft-unassign every pocket in ``workspace_id`` whose ``project_id``
    matches. Called by ``projects.service.agent_delete`` when a project
    is removed — pockets keep their data, only the project reference
    clears. Returns the number of rows updated.

    Stays inside the pockets service so the 4-file rule holds (only
    ``pockets/service.py`` may write to the Pocket Beanie collection).
    """
    if not workspace_id or not project_id:
        return 0
    collection = _PocketDoc.get_pymongo_collection()
    result = await collection.update_many(
        {"workspace": workspace_id, "project_id": project_id},
        {"$set": {"project_id": None}},
    )
    return getattr(result, "modified_count", 0) or 0


async def delete(pocket_id: str, user_id: str) -> None:
    """Hard-delete a pocket. Owner only."""
    doc = await _fetch_pocket(pocket_id)
    if doc.owner != user_id:
        from pocketpaw_ee.guards.audit import log_denial

        log_denial(
            actor=user_id,
            action="pocket.share",
            code="pocket.not_owner",
            resource_id=str(doc.id),
        )
        raise Forbidden("pocket.not_owner", "Only the pocket owner can perform this action")
    # Capture audience before delete so receivers can drop the pocket from
    # their list. The wire dict isn't useful here — only the id is.
    delete_payload = {
        "pocket_id": str(doc.id),
        "recipient_ids": [doc.owner, *list(doc.shared_with or [])],
    }
    if doc.visibility == "workspace":
        delete_payload["workspace_id"] = doc.workspace
    await doc.delete()
    await emit(PocketDeleted(data=delete_payload))


# ---------------------------------------------------------------------------
# Agent-generated pockets
# ---------------------------------------------------------------------------


async def create_from_ripple_spec(
    workspace_id: str,
    owner_id: str,
    ripple_spec: dict,
    description: str = "",
    pattern: str | None = None,
) -> str | None:
    """Auto-create a pocket from an agent-generated ripple spec.
    Returns the pocket id on success, None on failure.

    This is the agent-generation path, so the catalog gate runs in
    STRICT mode — a spec with a node outside the widget catalog (or an
    ``embed`` URL violating the host policy) is BLOCKED: the function
    returns ``None`` rather than persist a pocket the renderer would
    draw as a red "Unknown widget type" box. The inline chat path has
    no LLM-retry loop, so blocking + logging is the strict behavior here
    (the specialist edit tools, which can retry, surface the corrective
    message instead).

    ``pattern`` records the create-pocket layout pattern (``"landing"``
    for a marketing site). Optional; defaults to ``None`` so the
    pre-existing inline auto-create path is unchanged.
    """
    try:
        normalized = normalize_ripple_spec(ripple_spec)
        if not normalized:
            return None
        validate_ripple_spec_logged(normalized, workspace_id=workspace_id)
        # Strict catalog gate — CatalogViolationError is caught by the
        # catch-all below, blocking the create and logging the reason.
        await _gate_catalog(normalized, strict=True, actor=owner_id, workspace_id=workspace_id)

        name = (
            normalized.get("lifecycle", {}).get("name")
            or normalized.get("name")
            or normalized.get("title")
            or "Agent-generated Pocket"
        )

        doc = _PocketDoc(
            workspace=workspace_id,
            name=name,
            description=description,
            type="ai-generated",
            owner=owner_id,
            visibility="workspace",
            rippleSpec=normalized,
            pattern=pattern,
            # New agent-generated pockets start with no connectors allowed.
            allowed_connectors=[],
        )
        await doc.insert()
        pocket_id = str(doc.id)
        logger.info("Auto-created pocket %s from ripple spec", pocket_id)
        await emit(PocketCreated(data=await _pocket_event_payload(doc)))
        return pocket_id
    except CatalogViolationError as exc:
        # Strict catalog gate blocked the auto-create; surface the field
        # paths explicitly instead of letting them disappear into the
        # catch-all stack trace below.
        logger.warning(
            "Auto-create blocked by catalog gate: %d violation(s); paths=%s",
            len(exc.violations),
            [v.get("path") for v in exc.violations],
        )
        return None
    except ActionWiringViolationError as exc:
        # Strict action-wiring gate blocked the auto-create (PR #1196).
        logger.warning(
            "Auto-create blocked by action-wiring gate: %d violation(s); paths=%s",
            len(exc.violations),
            [v.get("path") for v in exc.violations],
        )
        return None
    except MissingRequiredPropError as exc:
        # Strict required-prop gate blocked the auto-create (constraint-zone).
        logger.warning(
            "Auto-create blocked by required-prop gate: %d violation(s); paths=%s",
            len(exc.violations),
            [v.get("path") for v in exc.violations],
        )
        return None
    except Exception:
        logger.warning("Failed to auto-create pocket from ripple spec", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------


async def add_widget(pocket_id: str, user_id: str, body: AddWidgetRequest) -> dict:
    doc = await _fetch_pocket(pocket_id)
    _check_domain_edit_access(_pocket_to_domain(doc), user_id)

    widget = _build_widget_doc(
        {
            "name": body.name,
            "type": body.type,
            "icon": body.icon,
            "color": body.color,
            "span": body.span,
            "dataSourceType": body.data_source_type,
            "config": body.config,
            "props": body.props,
            "spec": body.spec,
            "assignedAgent": body.assigned_agent,
        }
    )
    doc.widgets.append(widget)
    await doc.save()
    await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
    return await _resolved_wire_dict(doc, user_id)


async def update_widget(
    pocket_id: str, widget_id: str, user_id: str, body: UpdateWidgetRequest
) -> dict:
    doc = await _fetch_pocket(pocket_id)
    _check_domain_edit_access(_pocket_to_domain(doc), user_id)

    widget = next((w for w in doc.widgets if w.id == widget_id), None)
    if widget is None:
        raise NotFound("widget", widget_id)
    if body.name is not None:
        widget.name = body.name
    if body.type is not None:
        widget.type = body.type
    if body.icon is not None:
        widget.icon = body.icon
    if body.config is not None:
        widget.config = body.config
    if body.props is not None:
        widget.props = body.props
    if body.data is not None:
        widget.data = body.data
    if body.assigned_agent is not None:
        widget.assignedAgent = body.assigned_agent
    await doc.save()
    await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
    return await _resolved_wire_dict(doc, user_id)


async def remove_widget(pocket_id: str, widget_id: str, user_id: str) -> dict:
    doc = await _fetch_pocket(pocket_id)
    _check_domain_edit_access(_pocket_to_domain(doc), user_id)

    before = len(doc.widgets)
    doc.widgets = [w for w in doc.widgets if w.id != widget_id]
    if len(doc.widgets) == before:
        raise NotFound("widget", widget_id)
    await doc.save()
    await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
    return await _resolved_wire_dict(doc, user_id)


async def reorder_widgets(pocket_id: str, user_id: str, widget_ids: list[str]) -> dict:
    """Reorder widgets. Tolerates legacy callers that may omit ids
    (missing ids appended at the end) or include unknown ids (dropped)."""
    doc = await _fetch_pocket(pocket_id)
    _check_domain_edit_access(_pocket_to_domain(doc), user_id)

    existing_ids = {w.id for w in doc.widgets}
    seen: set[str] = set()
    ordered: list[str] = []
    for wid in widget_ids:
        if wid in existing_ids and wid not in seen:
            ordered.append(wid)
            seen.add(wid)
    for w in doc.widgets:
        if w.id not in seen:
            ordered.append(w.id)
            seen.add(w.id)

    if set(ordered) != existing_ids:
        # Defensive: should be impossible after the fill above
        raise ValidationError(
            "widget.reorder_mismatch",
            "widget_ids must match the current set exactly",
        )
    widgets_by_id = {w.id: w for w in doc.widgets}
    doc.widgets = [widgets_by_id[wid] for wid in ordered]
    await doc.save()
    await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
    return await _resolved_wire_dict(doc, user_id)


# ---------------------------------------------------------------------------
# Sharing — Share links
# ---------------------------------------------------------------------------


async def generate_share_link(pocket_id: str, user_id: str, access: str) -> dict:
    doc = await _fetch_pocket(pocket_id)
    _check_domain_owner(_pocket_to_domain(doc), user_id)

    token = secrets.token_urlsafe(32)
    doc.share_link_token = token
    doc.share_link_access = access
    await doc.save()
    # no-event: share-link state is owner-only; the token comes back inline
    # in this REST response. Broadcasting would leak the token.
    return {"token": token, "access": access, "url": f"/shared/{token}"}


async def revoke_share_link(pocket_id: str, user_id: str) -> None:
    doc = await _fetch_pocket(pocket_id)
    _check_domain_owner(_pocket_to_domain(doc), user_id)

    doc.share_link_token = None
    doc.share_link_access = "view"
    await doc.save()
    # no-event: see ``generate_share_link``.


async def update_share_link(pocket_id: str, user_id: str, access: str) -> dict:
    doc = await _fetch_pocket(pocket_id)
    _check_domain_owner(_pocket_to_domain(doc), user_id)

    if not doc.share_link_token:
        raise NotFound("share_link", pocket_id)

    doc.share_link_access = access
    await doc.save()
    # no-event: see ``generate_share_link``.
    return {
        "token": doc.share_link_token,
        "access": access,
        "url": f"/shared/{doc.share_link_token}",
    }


async def access_via_share_link(token: str) -> dict:
    doc = await _PocketDoc.find_one(_PocketDoc.share_link_token == token)
    if doc is None:
        raise NotFound("pocket", "shared link")
    # no-resolve: share-link viewers have no auth context to build a ResolveCtx;
    # $source markers surface raw. v2: resolve with a guest-scoped context.
    return pocket_to_wire_dict(_pocket_to_domain(doc))


# ---------------------------------------------------------------------------
# Collaborators
# ---------------------------------------------------------------------------


async def add_collaborator(pocket_id: str, user_id: str, body: AddCollaboratorRequest) -> None:
    doc = await _fetch_pocket(pocket_id)
    pocket = _pocket_to_domain(doc)
    _check_domain_owner(pocket, user_id)

    await _mutate_list_field(pocket_id, "shared_with", body.user_id, "add")

    await event_bus.emit(
        "pocket.shared",
        {
            "pocket_id": pocket.id,
            "owner_id": user_id,
            "collaborator_id": body.user_id,
            "access": body.access,
        },
    )


async def remove_collaborator(pocket_id: str, user_id: str, target_user_id: str) -> None:
    doc = await _fetch_pocket(pocket_id)
    _check_domain_owner(_pocket_to_domain(doc), user_id)
    await _mutate_list_field(pocket_id, "shared_with", target_user_id, "remove")


# ---------------------------------------------------------------------------
# Team
# ---------------------------------------------------------------------------


async def add_team_member(pocket_id: str, user_id: str, member_id: str) -> dict:
    doc = await _fetch_pocket(pocket_id)
    _check_domain_edit_access(_pocket_to_domain(doc), user_id)
    await _mutate_list_field(pocket_id, "team", member_id, "add")
    doc = await _fetch_pocket(pocket_id)
    return await _resolved_wire_dict(doc, user_id)


async def remove_team_member(pocket_id: str, user_id: str, member_id: str) -> dict:
    doc = await _fetch_pocket(pocket_id)
    _check_domain_edit_access(_pocket_to_domain(doc), user_id)
    await _mutate_list_field(pocket_id, "team", member_id, "remove")
    doc = await _fetch_pocket(pocket_id)
    return await _resolved_wire_dict(doc, user_id)


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


async def add_agent(pocket_id: str, user_id: str, agent_id: str) -> dict:
    doc = await _fetch_pocket(pocket_id)
    _check_domain_edit_access(_pocket_to_domain(doc), user_id)
    await _mutate_list_field(pocket_id, "agents", agent_id, "add")
    doc = await _fetch_pocket(pocket_id)
    return await _resolved_wire_dict(doc, user_id)


async def remove_agent(pocket_id: str, user_id: str, agent_id: str) -> dict:
    doc = await _fetch_pocket(pocket_id)
    _check_domain_edit_access(_pocket_to_domain(doc), user_id)
    await _mutate_list_field(pocket_id, "agents", agent_id, "remove")
    doc = await _fetch_pocket(pocket_id)
    return await _resolved_wire_dict(doc, user_id)


# ---------------------------------------------------------------------------
# Agent-facing helpers — back the in-process MCP write tools the cloud
# SSE chat agent uses to edit the pocket it lives inside. The MCP shape
# wrapper (``{ok, error}`` returns + SSE event push) lives in
# ``pockets/agent_context.py``; the Beanie ops live here.
# ---------------------------------------------------------------------------


_AGENT_INVISIBLE_FIELDS = (
    "share_link_token",
    "shared_with",
    "team",
    "agents",
)


def _agent_view_dict(doc: _PocketDoc) -> dict:
    """Json-safe pocket dict with secrets/relationship fields stripped.

    Used by the in-process MCP tool channel — same shape every
    ``agent_*`` helper returns on success.

    LEADS with a ``_summary`` field (``spec_ops.summarize_ripple_spec``)
    so the agent reads the truth first: the dump's legacy top-level
    ``widgets[]`` array is empty on template-instantiated pockets, and
    agents that read it before ``rippleSpec`` concluded a fully composed
    pocket was "an empty shell". ``widgets`` itself stays in the view —
    other consumers may rely on it — but the summary names it legacy.
    """
    import json

    raw = doc.model_dump(mode="json", by_alias=True, exclude_none=True)
    for k in _AGENT_INVISIBLE_FIELDS:
        raw.pop(k, None)
    summary = spec_ops.summarize_ripple_spec(doc.rippleSpec, widgets_count=len(doc.widgets or []))
    if summary["has_ripple_spec"]:
        summary["note"] = (
            "The authoritative layout lives in rippleSpec.ui — the top-level "
            "widgets[] array is a legacy field"
            + (" and is empty for this pocket." if not summary["widgets_count"] else ".")
        )
    view: dict = {"_summary": summary}
    view.update(raw)
    return json.loads(json.dumps(view, default=str))


async def _agent_load_doc(pocket_id: str) -> tuple[_PocketDoc | None, str | None]:
    """Load a pocket for an agent-initiated mutation, with workspace +
    access-control checks.

    Pulls ``workspace_id`` and ``user_id`` from the per-stream
    ContextVars set by ``agent_router._run_agent_stream``. Rejects when
    no stream is active, when the pocket belongs to a different
    workspace, or when the caller lacks edit access — mirroring the
    REST path's ``_check_domain_edit_access`` (owner OR shared_with OR
    workspace-visible). Cross-workspace mismatches return the same
    "not found" message as a genuinely missing pocket so the agent can't
    enumerate the existence of pockets in other tenants.
    """
    from pocketpaw_ee.cloud.chat.agent_service import current_user_id, current_workspace_id

    if not pocket_id or not isinstance(pocket_id, str):
        return None, "pocket_id is required (string)"
    workspace_id = current_workspace_id()
    user_id = current_user_id()
    if not workspace_id or not user_id:
        return None, (
            "no active workspace/user — agent pocket mutations require a cloud SSE chat stream"
        )
    try:
        doc = await _PocketDoc.get(PydanticObjectId(pocket_id))
    except Exception as exc:  # noqa: BLE001
        return None, f"could not load pocket {pocket_id}: {exc}"
    if doc is None or doc.workspace != workspace_id:
        return None, f"pocket {pocket_id} not found"
    if (
        doc.owner != user_id
        and user_id not in (doc.shared_with or [])
        and doc.visibility != "workspace"
    ):
        return None, f"pocket {pocket_id} not found"
    return doc, None


async def has_edit_access(pocket_id: str, user_id: str) -> bool:
    """Return ``True`` if ``user_id`` may edit the pocket — owner,
    explicit shared_with, or workspace-visible. Raises ``NotFound`` if
    the pocket doesn't exist.

    Used by the ``require_pocket_edit`` FastAPI guard so the Pocket
    Beanie load stays inside the service.
    """
    try:
        pocket_oid = PydanticObjectId(pocket_id)
    except Exception as exc:  # noqa: BLE001
        raise NotFound("pocket", pocket_id) from exc

    doc = await _PocketDoc.get(pocket_oid)
    if doc is None:
        raise NotFound("pocket", pocket_id)

    if doc.owner == user_id:
        return True
    if user_id in (doc.shared_with or []):
        return True
    return doc.visibility == "workspace"


async def is_owner(pocket_id: str, user_id: str) -> bool:
    """Return ``True`` only if ``user_id`` owns the pocket. Raises
    ``NotFound`` if the pocket doesn't exist. Used by the
    ``require_pocket_owner`` FastAPI guard."""
    try:
        pocket_oid = PydanticObjectId(pocket_id)
    except Exception as exc:  # noqa: BLE001
        raise NotFound("pocket", pocket_id) from exc

    doc = await _PocketDoc.get(pocket_oid)
    if doc is None:
        raise NotFound("pocket", pocket_id)
    return doc.owner == user_id


async def has_action_run_access(pocket_id: str, user_id: str) -> bool:
    """Return ``True`` if ``user_id`` may RUN a write action on the pocket.

    The gate is OWNER or explicit ``shared_with`` ONLY — deliberately
    NARROWER than ``has_edit_access`` (which also allows any caller on a
    workspace-visible pocket). A write has blast radius: it hits a real
    backend with a real ``DELETE``. M2a keeps the run surface to people
    the owner explicitly invited; M2b widens this once Instinct gating is
    wired. Raises ``NotFound`` if the pocket doesn't exist — the
    ``require_pocket_action_run`` guard converts that to a 404.
    """
    try:
        pocket_oid = PydanticObjectId(pocket_id)
    except Exception as exc:  # noqa: BLE001
        raise NotFound("pocket", pocket_id) from exc

    doc = await _PocketDoc.get(pocket_oid)
    if doc is None:
        raise NotFound("pocket", pocket_id)

    if doc.owner == user_id:
        return True
    return user_id in (doc.shared_with or [])


async def is_member(pocket_id: str, user_id: str) -> bool:
    """Return ``True`` if ``user_id`` may read the pocket — owner, team
    member, explicit shared_with, or any caller when visibility is
    ``workspace`` / public.

    Mirrors the read-side rule in ``agent_service._resolve_pocket`` so
    the Files panel filter and the chat scope-resolver agree on who
    sees a pocket's files. Raises ``NotFound`` if the pocket doesn't
    exist — callers convert to a 403 / 404 as appropriate.

    Stage 3.E: used by ``files/router.py`` to gate ``GET /files?pocket_id=X``
    for non-members and by the upload router for the read-side ABAC
    check (writes go through ``has_edit_access``).
    """
    try:
        pocket_oid = PydanticObjectId(pocket_id)
    except Exception as exc:  # noqa: BLE001
        raise NotFound("pocket", pocket_id) from exc

    doc = await _PocketDoc.get(pocket_oid)
    if doc is None:
        raise NotFound("pocket", pocket_id)

    if doc.owner == user_id:
        return True
    if user_id in (doc.team or []):
        return True
    if user_id in (doc.shared_with or []):
        return True
    # Workspace-visible pockets: any workspace caller can read. The
    # route-level ``current_workspace_id`` dependency already enforced
    # workspace membership before we got here, so this branch implicitly
    # requires the caller be in this pocket's workspace.
    return getattr(doc, "visibility", "workspace") == "workspace"


async def _heal_node_ids(doc: _PocketDoc) -> None:
    """Defense-in-depth: stamp ``n_xxxxxxxx`` ids on a pocket's UISpec
    tree(s) if any node lacks one, persisting the heal.

    Pockets created or persisted after #1172 always carry node ids
    (``normalize_ripple_spec`` stamps them). This heals pockets written
    before the fix so they become editable on first agent read without
    a separate DB migration. Idempotent — a no-op when ids are already
    present.
    """
    spec = doc.rippleSpec
    if not isinstance(spec, dict):
        return
    changed = False
    ui = spec.get("ui")
    if isinstance(ui, dict) and spec_ops.ensure_ids(ui):
        changed = True
    panes = spec.get("panes")
    if isinstance(panes, dict):
        for pane in panes.values():
            if isinstance(pane, dict) and spec_ops.ensure_ids(pane):
                changed = True
    if changed:
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            logger.warning("pocket %s node-id heal save failed: %s", doc.id, exc)


async def _agent_backend_summary(doc: _PocketDoc) -> dict[str, Any]:
    """Return the NON-SECRET backend summary for a pocket, agent-safe.

    Shape: ``{base_url, auth_type, configured}`` — the same shape
    ``get_pocket_backend`` returns, never the token. When the pocket
    has no backend configured, returns ``{"configured": False}``.

    The backend credential lives in a separate collection (security
    design D1), so nothing surfaces it to the edit specialist by
    default. This makes ``get_pocket`` / ``agent_view`` carry the
    summary so the specialist knows whether a backend exists and what
    its base URL is — without ever seeing the token.
    """
    try:
        summary = await get_pocket_backend(doc.workspace, str(doc.id))
    except Exception:  # noqa: BLE001 — a backend-read failure must not break the view
        logger.warning(
            "agent_view: backend summary fetch failed for pocket %s", str(doc.id), exc_info=True
        )
        return {"configured": False}
    if summary is None:
        return {"configured": False}
    # get_pocket_backend already excludes the token — assert the shape
    # and never pass anything else through. ``allowed_writes`` is carried
    # so the edit specialist can see which write methods+paths the owner
    # has authorized before it authors a write action; ``approval_route``
    # so it knows who approves a `requires_instinct` write.
    # ``backend_type`` / ``connector_name`` so the specialist knows whether to
    # author http-path sources or connector-action sources.
    return {
        "backend_type": summary.get("backend_type", "http"),
        "connector_name": summary.get("connector_name"),
        "base_url": summary.get("base_url"),
        "auth_type": summary.get("auth_type"),
        "configured": True,
        "allowed_writes": summary.get("allowed_writes") or [],
        "approval_route": summary.get("approval_route"),
    }


async def agent_view(pocket_id: str) -> tuple[dict | None, str | None]:
    """Read-only fetch — returns ``(view_dict, None)`` on success or
    ``(None, error)`` on failure.

    Self-heals node ids: a legacy pocket persisted before #1172 has an
    id-less ``ui`` tree, so the chat agent would have no ``n_xxxxxxxx``
    id to address with granular edit ops. ``_heal_node_ids`` stamps and
    persists them on first read.

    The returned view carries a non-secret ``backend`` summary
    (``{base_url, auth_type, configured}``) so the edit specialist can
    see whether the pocket already has a backend configured before it
    authors a ``sources`` block. The token is NEVER included.

    Note: $source markers in rippleSpec are intentionally NOT resolved
    here. The agent must see raw markers so that on edit it preserves
    them; resolving would let the agent bake a snapshot of live data
    into the spec, defeating the marker mechanism. Resolution happens
    only in ``service.get`` (the user-facing read path)."""
    doc, err = await _agent_load_doc(pocket_id)
    if err or doc is None:
        return None, err
    await _heal_node_ids(doc)
    view = _agent_view_dict(doc)
    view["backend"] = await _agent_backend_summary(doc)
    return view, None


async def agent_list(workspace_id: str, user_id: str) -> list[dict]:
    """Compact list of pockets the user can see in this workspace.

    Returned shape per pocket: ``{id, name, description, type, icon,
    color, owner}``. The full ``rippleSpec`` is intentionally excluded —
    callers (the in-process MCP ``list_pockets`` tool, the
    ``cloud_list_pockets`` CLI) hit this on every creation flow as the
    "have we already got one of these?" check, so the payload stays
    cheap. Visibility rules mirror ``list_pockets``: owned by the user,
    explicitly shared, or workspace-visible.
    """
    if not workspace_id or not user_id:
        return []
    docs = await _PocketDoc.find(
        {
            "workspace": workspace_id,
            "$or": [
                {"owner": user_id},
                {"shared_with": user_id},
                {"visibility": "workspace"},
            ],
        }
    ).to_list()
    out: list[dict] = []
    for d in docs:
        out.append(
            {
                "id": str(d.id),
                "name": d.name,
                "description": d.description or "",
                "type": d.type or "",
                "icon": d.icon or "",
                "color": d.color or "",
                "owner": d.owner,
            }
        )
    return out


async def agent_update(
    pocket_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    icon: str | None = None,
    color: str | None = None,
    ripple_spec: dict | None = None,
) -> tuple[dict | None, str | None]:
    """Patch top-level pocket fields. Only fields the caller explicitly
    provides are touched. ``ripple_spec`` is normalized."""
    doc, err = await _agent_load_doc(pocket_id)
    if err:
        return None, err
    if name is not None:
        doc.name = name
    if description is not None:
        doc.description = description
    if icon is not None:
        doc.icon = icon
    if color is not None:
        doc.color = color
    if ripple_spec is not None:
        doc.rippleSpec = normalize_ripple_spec(ripple_spec)
        if doc.rippleSpec:
            validate_ripple_spec_logged(
                doc.rippleSpec, pocket_id=str(doc.id), workspace_id=doc.workspace
            )
            # Agent-generation path — strict catalog gate. Surface a
            # violation as the error string so the specialist can retry.
            try:
                await _gate_catalog(
                    doc.rippleSpec,
                    strict=True,
                    actor="agent",
                    workspace_id=doc.workspace,
                    pocket_id=str(doc.id),
                )
            except CatalogViolationError as exc:
                return None, format_violations_for_agent(exc.violations)
            except ActionWiringViolationError as exc:
                return None, format_action_violations_for_agent(exc.violations)
            except MissingRequiredPropError as exc:
                return None, format_required_prop_violations_for_agent(exc.violations)
    try:
        await doc.save()
    except Exception as exc:  # noqa: BLE001
        return None, f"save failed: {exc}"
    await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
    return _agent_view_dict(doc), None


def _merge_authored_sources_logged(
    doc: _PocketDoc, sources: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Merge agent-authored RFC-04 sources into ``doc.rippleSpec.sources``.

    This is the LOGGED (drift-allowed) sibling of ``agent_set_source``'s
    strict per-source validation — it mirrors how the create / human-import
    paths treat catalog drift (``validate_against_catalog_logged``) and how
    the source executor's own ``_parse_bindings`` treats a malformed entry:
    a bad binding is logged and SKIPPED, never raised, so one hallucinated
    source can't block the whole ``add_widget``. Each surviving entry is
    normalized through the ``SourceBinding`` model (the same schema the
    executor parses), so the binding the executor later runs is identical to
    a binding authored via ``set_source``.

    Mutates ``doc.rippleSpec`` in place via the pure ``sources_ops`` helper.
    A non-dict ``sources`` arg, or one with no valid entry, is a no-op — the
    ``sources`` key is only materialised when at least one binding validates.

    Returns ``(authored_keys, skipped_keys)`` so the caller can build an
    HONEST tool result: the agent can confirm only the sources that actually
    persisted and learn which it must NOT report as written. This closes the
    smoke-test gap where the agent claimed "source authored" for a binding
    that was silently dropped.
    """
    from pocketpaw_ee.cloud.pockets.source_executor import SourceBinding

    authored: list[str] = []
    skipped: list[str] = []
    if not isinstance(sources, dict) or not sources:
        return authored, skipped
    spec = _sources_root(doc)
    for key, entry in sources.items():
        if not key or not isinstance(entry, dict):
            logger.warning(
                "add_widget: skipping authored source %r — entry must be a non-empty "
                "keyed object (pocket %s)",
                key,
                str(doc.id),
            )
            skipped.append(key)
            continue
        try:
            normalized = SourceBinding.model_validate(entry).model_dump(mode="json")
        except PydanticValidationError as exc:
            logger.warning(
                "add_widget: skipping invalid authored source %r on pocket %s: %s",
                key,
                str(doc.id),
                exc.errors()[0].get("msg", exc) if exc.errors() else exc,
            )
            skipped.append(key)
            continue
        try:
            sources_ops.set_source(spec, key, normalized)
            authored.append(key)
        except ValueError as exc:
            logger.warning(
                "add_widget: could not merge authored source %r on pocket %s: %s",
                key,
                str(doc.id),
                exc,
            )
            skipped.append(key)
    return authored, skipped


async def agent_add_widget(
    pocket_id: str,
    widget: dict,
    *,
    sources: dict[str, Any] | None = None,
) -> tuple[dict | None, str | None]:
    """Append one widget tile to the pocket's embedded widget list, and —
    when ``sources`` is supplied — author the RFC-04 data sources that feed
    it onto the pocket's top-level ``rippleSpec.sources`` in the same write.

    ``sources`` is a dict keyed by source name; each value is an RFC-04
    ``SourceBinding`` (``method`` / ``path`` / ``bind`` / ``refresh`` / …).
    It is merged in LOGGED (drift-allowed) mode — see
    :func:`_merge_authored_sources_logged` — so a single bad binding is
    skipped rather than blocking the tile. The tile's own spec carries the
    ``{state.<path>}`` bind that references the source's ``bind`` target; no
    special handling beyond persisting it.
    """
    if not isinstance(widget, dict):
        return None, "widget must be a JSON object"
    doc, err = await _agent_load_doc(pocket_id)
    if err:
        return None, err
    # Agent-generation path — strict catalog + action-wiring gate on the
    # widget's own ``spec`` subtree (#1208). Mirrors the pocket-level gate
    # in ``agent_replace_node`` so the home-pocket widget-add surface gets
    # the same protection against hallucinated dispatcher verbs and unwired
    # "Refresh" buttons. Validate BEFORE constructing the doc so a violating
    # spec never reaches the save path.
    gate_error = await _gate_widget_spec_for_agent(
        widget.get("spec") if isinstance(widget.get("spec"), dict) else None,
        widget_type=widget.get("type") if isinstance(widget.get("type"), str) else None,
        actor="agent",
        workspace_id=doc.workspace,
        pocket_id=str(doc.id),
    )
    if gate_error is not None:
        return None, gate_error
    try:
        new_widget = _build_widget_doc(widget)
    except Exception as exc:  # noqa: BLE001
        return None, f"invalid widget spec: {exc}"
    doc.widgets.append(new_widget)
    # Author the tile's data sources onto the pocket's top-level
    # rippleSpec.sources in the SAME save (RFC-04). Logged/drift-allowed so
    # a hallucinated binding never blocks the tile. The authored/skipped keys
    # ride back on the view dict (under ``_source_merge``) so the agent_context
    # wrapper can build an HONEST tool result without us widening the 2-tuple
    # signature that the widget-gate callers depend on.
    authored: list[str] = []
    skipped: list[str] = []
    if sources is not None:
        authored, skipped = _merge_authored_sources_logged(doc, sources)
    try:
        await doc.save()
    except Exception as exc:  # noqa: BLE001
        return None, f"save failed: {exc}"
    await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
    view = _agent_view_dict(doc)
    if sources is not None:
        view["_source_merge"] = {"authored": authored, "skipped": skipped}
    return view, None


async def agent_update_widget(
    pocket_id: str,
    widget_id: str,
    fields: dict,
    *,
    sources: dict[str, Any] | None = None,
) -> tuple[dict | None, str | None]:
    """Patch fields on one embedded widget, and — when ``sources`` is supplied
    — author the RFC-04 data sources that feed it onto the pocket's top-level
    ``rippleSpec.sources`` in the same write. Mirrors ``agent_add_widget``:
    refining a static tile into a live one is one call (patch the bind + author
    the source). The sources merge is LOGGED (drift-allowed); the authored /
    skipped keys ride back on the view under ``_source_merge`` for an honest
    tool result."""
    if not isinstance(fields, dict):
        return None, "fields must be a JSON object"
    doc, err = await _agent_load_doc(pocket_id)
    if err:
        return None, err
    widget = next((w for w in doc.widgets if w.id == widget_id), None)
    if widget is None:
        return None, f"widget {widget_id} not found in pocket {pocket_id}"
    # Agent-generation path — strict catalog + action-wiring gate when the
    # update overwrites ``spec`` (#1208). Only fires when a new spec is
    # supplied; a name-only / colour-only patch skips the gate the same way
    # the MCP-layer manifest check does. ``type`` resolution mirrors the
    # post-update widget state: take ``fields.type`` when provided, else
    # fall back to the existing widget's type so a native widget that
    # accidentally received a non-empty spec dict still short-circuits.
    if "spec" in fields:
        new_spec = fields["spec"] if isinstance(fields["spec"], dict) else None
        effective_type = (
            fields["type"] if "type" in fields and isinstance(fields["type"], str) else widget.type
        )
        gate_error = await _gate_widget_spec_for_agent(
            new_spec,
            widget_type=effective_type,
            actor="agent",
            workspace_id=doc.workspace,
            pocket_id=str(doc.id),
        )
        if gate_error is not None:
            return None, gate_error
    for k in ("name", "type", "icon", "color", "span", "data", "spec", "assignedAgent"):
        if k in fields:
            setattr(widget, k, fields[k])
    if "config" in fields and isinstance(fields["config"], dict):
        widget.config = fields["config"]
    if "props" in fields and isinstance(fields["props"], dict):
        widget.props = fields["props"]
    if "dataSourceType" in fields:
        widget.dataSourceType = fields["dataSourceType"]
    # Author/refine the tile's data sources in the SAME save (RFC-04), logged.
    authored: list[str] = []
    skipped: list[str] = []
    if sources is not None:
        authored, skipped = _merge_authored_sources_logged(doc, sources)
    try:
        await doc.save()
    except Exception as exc:  # noqa: BLE001
        return None, f"save failed: {exc}"
    await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
    view = _agent_view_dict(doc)
    if sources is not None:
        view["_source_merge"] = {"authored": authored, "skipped": skipped}
    return view, None


async def agent_remove_widget(pocket_id: str, widget_id: str) -> tuple[dict | None, str | None]:
    doc, err = await _agent_load_doc(pocket_id)
    if err:
        return None, err
    before = len(doc.widgets)
    doc.widgets = [w for w in doc.widgets if w.id != widget_id]
    if len(doc.widgets) == before:
        return None, f"widget {widget_id} not found in pocket {pocket_id}"
    try:
        await doc.save()
    except Exception as exc:  # noqa: BLE001
        return None, f"save failed: {exc}"
    await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
    return _agent_view_dict(doc), None


# ---------------------------------------------------------------------------
# Granular rippleSpec.ui mutations — agent-facing
# ---------------------------------------------------------------------------

# Per-pocket asyncio.Lock cache. Granular ops on the same pocket
# serialize so a flurry of specialist calls can't race on doc.save().
# Bounded LRU so the cache doesn't grow unboundedly with pocket churn.
_POCKET_LOCK_CACHE_MAX = 256
_pocket_locks: OrderedDict[str, asyncio.Lock] = OrderedDict()


def _pocket_lock(pocket_id: str) -> asyncio.Lock:
    """Return (creating if needed) the per-pocket lock. LRU-evicted."""
    lock = _pocket_locks.get(pocket_id)
    if lock is None:
        lock = asyncio.Lock()
        _pocket_locks[pocket_id] = lock
        if len(_pocket_locks) > _POCKET_LOCK_CACHE_MAX:
            _pocket_locks.popitem(last=False)
    else:
        _pocket_locks.move_to_end(pocket_id)
    return lock


def _spec_root(doc: _PocketDoc) -> tuple[dict[str, Any] | None, str | None]:
    """Return the mutable ``rippleSpec.ui`` root for a doc, or
    ``(None, error)`` if the pocket has no spec or no root node.

    Mutating the returned dict mutates ``doc.rippleSpec['ui']`` in place
    — the caller follows up with ``doc.save()`` to persist.
    """
    spec = doc.rippleSpec
    if not isinstance(spec, dict):
        return None, "pocket has no rippleSpec to mutate"
    ui = spec.get("ui")
    if not isinstance(ui, dict):
        return None, "pocket rippleSpec has no 'ui' root"
    return ui, None


async def _load_and_ensure_ids(
    pocket_id: str,
) -> tuple[_PocketDoc | None, dict[str, Any] | None, str | None]:
    """Load the pocket doc, get its UI root, and ensure every node has
    an id (persisting if newly assigned). Returns
    ``(doc, ui_root, None)`` on success.
    """
    doc, err = await _agent_load_doc(pocket_id)
    if err or doc is None:
        return None, None, err
    ui, err = _spec_root(doc)
    if err or ui is None:
        return None, None, err
    if spec_ops.ensure_ids(ui):
        # Persist newly-assigned ids so subsequent ops can target them.
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            return None, None, f"id-assignment save failed: {exc}"
    return doc, ui, None


async def agent_add_node(
    pocket_id: str,
    *,
    parent_id: str,
    spec: dict[str, Any],
    after_id: str | None = None,
    index: int | None = None,
) -> tuple[dict | None, str | None]:
    """Insert ``spec`` as a child of ``parent_id`` (after ``after_id`` or
    appended). The new node's id is assigned if absent.

    Returns ``({"subtree": <new-node>, "pocket": <view>}, None)`` on
    success.
    """
    if not isinstance(spec, dict):
        return None, "spec must be a JSON object"
    if not parent_id:
        return None, "parent_id is required"
    async with _pocket_lock(pocket_id):
        doc, ui, err = await _load_and_ensure_ids(pocket_id)
        if err or doc is None or ui is None:
            return None, err
        parent = spec_ops.find_by_id(ui, parent_id)
        if parent is None:
            return None, f"no node with id {parent_id!r}"
        new_node = dict(spec)
        if not spec_ops.is_valid_id(new_node.get("id")):
            new_node["id"] = spec_ops.new_node_id()
        # Make sure any nested children also have ids.
        spec_ops.ensure_ids(new_node)
        try:
            spec_ops.insert_child(parent, new_node, after_id=after_id, index=index)
        except ValueError as exc:
            return None, str(exc)
        if doc.rippleSpec is not None:
            doc.rippleSpec = normalize_ripple_spec(doc.rippleSpec) or doc.rippleSpec
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            return None, f"save failed: {exc}"
        await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
        return (
            {"subtree": new_node, "pocket": _agent_view_dict(doc)},
            None,
        )


async def agent_replace_node(
    pocket_id: str,
    *,
    node_id: str,
    spec: dict[str, Any],
) -> tuple[dict | None, str | None]:
    """Replace the subtree rooted at ``node_id`` with ``spec``. The
    target's id is preserved if ``spec['id']`` is absent.

    Returns ``({"subtree": <new>, "old": <prev>, "pocket": <view>}, None)``.
    """
    if not isinstance(spec, dict):
        return None, "spec must be a JSON object"
    if not node_id:
        return None, "node_id is required"
    async with _pocket_lock(pocket_id):
        doc, ui, err = await _load_and_ensure_ids(pocket_id)
        if err or doc is None or ui is None:
            return None, err
        new_node = dict(spec)
        try:
            old = spec_ops.replace_node(ui, node_id, new_node)
        except ValueError as exc:
            return None, str(exc)
        spec_ops.ensure_ids(new_node)
        if doc.rippleSpec is not None:
            doc.rippleSpec = normalize_ripple_spec(doc.rippleSpec) or doc.rippleSpec
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            return None, f"save failed: {exc}"
        await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
        return (
            {"subtree": new_node, "old": old, "pocket": _agent_view_dict(doc)},
            None,
        )


async def agent_set_node_prop(
    pocket_id: str,
    *,
    node_id: str,
    prop: str,
    value: Any,
) -> tuple[dict | None, str | None]:
    """Set a single prop on ``node_id``. ``prop`` writes into ``props``
    by default; top-level node keys (``show``, ``bind``, ``on_click``,
    etc.) are addressable by their bare name. Dotted paths
    (``data.rows``) walk inside ``props``.

    Returns ``({"subtree": <node>, "old_value": <prev>, "pocket": <view>},
    None)``.
    """
    if not node_id:
        return None, "node_id is required"
    if not prop:
        return None, "prop is required"
    async with _pocket_lock(pocket_id):
        doc, ui, err = await _load_and_ensure_ids(pocket_id)
        if err or doc is None or ui is None:
            return None, err
        node = spec_ops.find_by_id(ui, node_id)
        if node is None:
            return None, f"no node with id {node_id!r}"
        try:
            old = spec_ops.set_prop(node, prop, value)
        except ValueError as exc:
            return None, str(exc)
        if doc.rippleSpec is not None:
            doc.rippleSpec = normalize_ripple_spec(doc.rippleSpec) or doc.rippleSpec
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            return None, f"save failed: {exc}"
        await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
        return (
            {
                "subtree": node,
                "old_value": old,
                "prop": prop,
                "pocket": _agent_view_dict(doc),
            },
            None,
        )


async def agent_set_prop_array_item(
    pocket_id: str,
    *,
    node_id: str,
    prop: str,
    match: dict[str, Any],
    partial: dict[str, Any],
) -> tuple[dict | None, str | None]:
    """Merge ``partial`` into the first item of ``node.props[prop]`` that
    matches ``match``. Surgical alternative to ``set_node_prop`` when the
    agent only wants to change one row/slice in a chart/table/etc.

    Merge is SHALLOW: top-level keys in ``partial`` overwrite the matched
    item's keys, but nested dicts/lists are replaced wholesale rather
    than deep-merged. Matches ``patch_state`` semantics; if the agent
    needs to preserve nested structure, fetch the item first and pass a
    fully-built nested dict in ``partial``. Non-dict matched items are
    replaced wholesale by ``partial`` (rare — most prop-array items are
    dicts).

    Returns ``({"item_index": int, "item": <new item>, "old_item": <prev>,
    "pocket": <view>}, None)``.

    Error codes (returned as the error string):
      * ``"unsupported_prop_array: <type>.<prop>"``
      * ``"no node with id 'n_xxx'"``
      * ``"prop {prop!r} is not an array on node {node_id!r}"``
      * ``"not_found: no item matched"``
      * ``"ambiguous: N items matched; candidates=[idx, ...]"``
    """
    if not node_id:
        return None, "node_id is required"
    if not prop:
        return None, "prop is required"
    if not isinstance(partial, dict):
        return None, "partial must be a dict"

    async with _pocket_lock(pocket_id):
        doc, ui, err = await _load_and_ensure_ids(pocket_id)
        if err or doc is None or ui is None:
            return None, err
        node = spec_ops.find_by_id(ui, node_id)
        if node is None:
            return None, f"no node with id {node_id!r}"

        wtype = node.get("type")
        if not isinstance(wtype, str) or not prop_arrays.is_allowed(wtype, prop):
            return None, f"unsupported_prop_array: {wtype}.{prop}"

        props = node.get("props")
        if props is None:
            return None, f"node {node_id!r} has no props"
        if not isinstance(props, dict):
            return None, f"node {node_id!r} has non-dict props"
        arr = props.get(prop)
        if not isinstance(arr, list):
            return None, f"prop {prop!r} is not an array on node {node_id!r}"

        try:
            candidates = spec_ops.match_array_item_candidates(arr, match)
        except ValueError as exc:
            return None, str(exc)
        if len(candidates) == 0:
            return None, "not_found: no item matched"
        if len(candidates) > 1:
            preview = candidates[:5]
            return None, f"ambiguous: {len(candidates)} items matched; candidates={preview}"

        idx = candidates[0]
        old_item = copy.deepcopy(arr[idx])
        if isinstance(arr[idx], dict):
            arr[idx] = {**arr[idx], **partial}
        else:
            arr[idx] = partial  # non-dict element: replace wholesale

        if doc.rippleSpec is not None:
            doc.rippleSpec = normalize_ripple_spec(doc.rippleSpec) or doc.rippleSpec
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            return None, f"save failed: {exc}"
        await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
        return (
            {
                "item_index": idx,
                "item": arr[idx],
                "old_item": old_item,
                "pocket": _agent_view_dict(doc),
            },
            None,
        )


async def agent_append_prop_array_item(
    pocket_id: str,
    *,
    node_id: str,
    prop: str,
    value: Any,
    after: dict[str, Any] | None = None,
) -> tuple[dict | None, str | None]:
    """Append ``value`` to ``node.props[prop]``. If ``after`` is given,
    insert immediately AFTER the first item matching that ItemMatch.

    Returns ``({"item_index": int, "item": <inserted>, "pocket": <view>}, None)``.

    Errors: see ``agent_set_prop_array_item``. ``after`` resolution uses
    the same match grammar; ``not_found`` / ``ambiguous`` propagate.
    """
    if not node_id:
        return None, "node_id is required"
    if not prop:
        return None, "prop is required"

    async with _pocket_lock(pocket_id):
        doc, ui, err = await _load_and_ensure_ids(pocket_id)
        if err or doc is None or ui is None:
            return None, err
        node = spec_ops.find_by_id(ui, node_id)
        if node is None:
            return None, f"no node with id {node_id!r}"

        wtype = node.get("type")
        if not isinstance(wtype, str) or not prop_arrays.is_allowed(wtype, prop):
            return None, f"unsupported_prop_array: {wtype}.{prop}"

        # Append intentionally creates props and the array on demand —
        # set/remove bail when either is missing because there is no
        # item to address, but append's whole job is to add the first
        # one. Asymmetry is by design.
        props = node.setdefault("props", {})
        if not isinstance(props, dict):
            return None, f"node {node_id!r} has non-dict props"
        arr = props.get(prop)
        if arr is None:
            arr = []
            props[prop] = arr
        if not isinstance(arr, list):
            return None, f"prop {prop!r} is not an array on node {node_id!r}"

        if after is None:
            arr.append(value)
            idx = len(arr) - 1
        else:
            try:
                candidates = spec_ops.match_array_item_candidates(arr, after)
            except ValueError as exc:
                return None, str(exc)
            if len(candidates) == 0:
                return None, "not_found: after target did not match"
            if len(candidates) > 1:
                return (
                    None,
                    f"ambiguous: after matched {len(candidates)} items; "
                    f"candidates={candidates[:5]}",
                )
            arr.insert(candidates[0] + 1, value)
            idx = candidates[0] + 1

        if doc.rippleSpec is not None:
            doc.rippleSpec = normalize_ripple_spec(doc.rippleSpec) or doc.rippleSpec
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            return None, f"save failed: {exc}"
        await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
        return (
            {
                "item_index": idx,
                "item": value,
                "pocket": _agent_view_dict(doc),
            },
            None,
        )


async def agent_remove_prop_array_item(
    pocket_id: str,
    *,
    node_id: str,
    prop: str,
    match: dict[str, Any],
) -> tuple[dict | None, str | None]:
    """Remove the first item in ``node.props[prop]`` matching ``match``.

    Returns ``({"removed_index": int, "removed_item": Any, "pocket": <view>},
    None)``.

    Errors: see ``agent_set_prop_array_item``. Refuses ambiguous matches —
    the agent must disambiguate.
    """
    if not node_id:
        return None, "node_id is required"
    if not prop:
        return None, "prop is required"

    async with _pocket_lock(pocket_id):
        doc, ui, err = await _load_and_ensure_ids(pocket_id)
        if err or doc is None or ui is None:
            return None, err
        node = spec_ops.find_by_id(ui, node_id)
        if node is None:
            return None, f"no node with id {node_id!r}"

        wtype = node.get("type")
        if not isinstance(wtype, str) or not prop_arrays.is_allowed(wtype, prop):
            return None, f"unsupported_prop_array: {wtype}.{prop}"

        props = node.get("props")
        if props is None:
            return None, f"node {node_id!r} has no props"
        if not isinstance(props, dict):
            return None, f"node {node_id!r} has non-dict props"
        arr = props.get(prop)
        if not isinstance(arr, list):
            return None, f"prop {prop!r} is not an array on node {node_id!r}"

        try:
            candidates = spec_ops.match_array_item_candidates(arr, match)
        except ValueError as exc:
            return None, str(exc)
        if len(candidates) == 0:
            return None, "not_found: no item matched"
        if len(candidates) > 1:
            return None, f"ambiguous: {len(candidates)} items matched; candidates={candidates[:5]}"

        idx = candidates[0]
        removed = arr.pop(idx)

        if doc.rippleSpec is not None:
            doc.rippleSpec = normalize_ripple_spec(doc.rippleSpec) or doc.rippleSpec
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            return None, f"save failed: {exc}"
        await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
        return (
            {
                "removed_index": idx,
                "removed_item": removed,
                "pocket": _agent_view_dict(doc),
            },
            None,
        )


async def agent_move_node(
    pocket_id: str,
    *,
    node_id: str,
    new_parent_id: str,
    after_id: str | None = None,
) -> tuple[dict | None, str | None]:
    """Move ``node_id`` under ``new_parent_id`` (after ``after_id`` or
    appended). Refuses to move the root or to move into a descendant.

    Returns ``({"subtree": <moved>, "old_parent_id": ..., "old_index": ...,
    "pocket": <view>}, None)``.
    """
    if not node_id:
        return None, "node_id is required"
    if not new_parent_id:
        return None, "new_parent_id is required"
    async with _pocket_lock(pocket_id):
        doc, ui, err = await _load_and_ensure_ids(pocket_id)
        if err or doc is None or ui is None:
            return None, err
        try:
            old_parent_id, old_idx = spec_ops.move_node(
                ui, node_id, new_parent_id, after_id=after_id
            )
        except ValueError as exc:
            return None, str(exc)
        moved = spec_ops.find_by_id(ui, node_id) or {}
        if doc.rippleSpec is not None:
            doc.rippleSpec = normalize_ripple_spec(doc.rippleSpec) or doc.rippleSpec
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            return None, f"save failed: {exc}"
        await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
        return (
            {
                "subtree": moved,
                "old_parent_id": old_parent_id,
                "old_index": old_idx,
                "pocket": _agent_view_dict(doc),
            },
            None,
        )


async def agent_remove_node(
    pocket_id: str,
    *,
    node_id: str,
) -> tuple[dict | None, str | None]:
    """Remove the subtree at ``node_id``. Returns the removed subtree
    plus enough position info to rebuild the inverse.

    Returns ``({"removed_id": ..., "removed": <subtree>, "parent_id": ...,
    "index": ..., "pocket": <view>}, None)``.
    """
    if not node_id:
        return None, "node_id is required"
    async with _pocket_lock(pocket_id):
        doc, ui, err = await _load_and_ensure_ids(pocket_id)
        if err or doc is None or ui is None:
            return None, err
        try:
            parent, _key, idx, removed = spec_ops.remove_node(ui, node_id)
        except ValueError as exc:
            return None, str(exc)
        if doc.rippleSpec is not None:
            doc.rippleSpec = normalize_ripple_spec(doc.rippleSpec) or doc.rippleSpec
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            return None, f"save failed: {exc}"
        await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
        return (
            {
                "removed_id": node_id,
                "removed": removed,
                "parent_id": str(parent.get("id", "")),
                "index": idx,
                "pocket": _agent_view_dict(doc),
            },
            None,
        )


# ---------------------------------------------------------------------------
# Granular rippleSpec.state mutations — agent-facing
# ---------------------------------------------------------------------------
#
# The "data" half of the 3-layer mutation rule:
# - data the user sees   → set_state / append_state / remove_state / patch_state
# - widget appearance    → set_node_prop
# - widget structure     → add_node / move_node / remove_node
#
# Reuses the per-pocket asyncio.Lock cache from the node ops above so
# concurrent state + node mutations on the same pocket serialize.


def _state_root(doc: _PocketDoc) -> tuple[dict[str, Any], str | None]:
    """Return the mutable ``rippleSpec.state`` dict for a doc, creating
    it (and the parent ``rippleSpec``) if absent.

    Unlike ``_spec_root`` (which requires a ``ui`` root), state is
    legitimately empty for new pockets, so we materialise it on demand.
    """
    spec = doc.rippleSpec
    if not isinstance(spec, dict):
        spec = {}
        doc.rippleSpec = spec
    state = spec.get("state")
    if not isinstance(state, dict):
        state = {}
        spec["state"] = state
    return state, None


async def agent_set_state(
    pocket_id: str,
    *,
    path: str,
    value: Any,
) -> tuple[dict | None, str | None]:
    """Write ``value`` at ``path`` in the pocket's state. Returns
    ``({"path": ..., "value": ..., "old_value": ..., "pocket": <view>},
    None)`` on success.

    See ``state_ops`` for path syntax (dotted with bracket indexing).
    """
    if not path:
        return None, "path is required"
    async with _pocket_lock(pocket_id):
        doc, err = await _agent_load_doc(pocket_id)
        if err or doc is None:
            return None, err
        state, err = _state_root(doc)
        if err:
            return None, err
        try:
            old = state_ops.set_path(state, path, value)
        except ValueError as exc:
            return None, str(exc)
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            return None, f"save failed: {exc}"
        await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
        return (
            {
                "path": path,
                "value": value,
                "old_value": old,
                "pocket": _agent_view_dict(doc),
            },
            None,
        )


async def agent_append_state(
    pocket_id: str,
    *,
    path: str,
    item: Any,
) -> tuple[dict | None, str | None]:
    """Append ``item`` to the array at ``path``. Creates an empty list
    if the path is absent. Returns the new length plus the appended item.
    """
    if not path:
        return None, "path is required"
    async with _pocket_lock(pocket_id):
        doc, err = await _agent_load_doc(pocket_id)
        if err or doc is None:
            return None, err
        state, err = _state_root(doc)
        if err:
            return None, err
        try:
            new_len = state_ops.append_path(state, path, item)
        except ValueError as exc:
            return None, str(exc)
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            return None, f"save failed: {exc}"
        await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
        return (
            {
                "path": path,
                "item": item,
                "new_length": new_len,
                "pocket": _agent_view_dict(doc),
            },
            None,
        )


async def agent_remove_state(
    pocket_id: str,
    *,
    path: str,
) -> tuple[dict | None, str | None]:
    """Remove the value at ``path``. Returns the removed value (used as
    the inverse for undo)."""
    if not path:
        return None, "path is required"
    async with _pocket_lock(pocket_id):
        doc, err = await _agent_load_doc(pocket_id)
        if err or doc is None:
            return None, err
        state, err = _state_root(doc)
        if err:
            return None, err
        try:
            removed = state_ops.remove_path(state, path)
        except ValueError as exc:
            return None, str(exc)
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            return None, f"save failed: {exc}"
        await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
        return (
            {"path": path, "removed": removed, "pocket": _agent_view_dict(doc)},
            None,
        )


async def agent_patch_state(
    pocket_id: str,
    *,
    partial: dict[str, Any],
) -> tuple[dict | None, str | None]:
    """Shallow-merge ``partial`` into state at the top level. For
    batched independent-key writes. Returns the previous values of
    overwritten keys."""
    if not isinstance(partial, dict):
        return None, "partial must be a dict"
    async with _pocket_lock(pocket_id):
        doc, err = await _agent_load_doc(pocket_id)
        if err or doc is None:
            return None, err
        state, err = _state_root(doc)
        if err:
            return None, err
        try:
            prev = state_ops.patch(state, partial)
        except ValueError as exc:
            return None, str(exc)
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            return None, f"save failed: {exc}"
        await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
        return (
            {"partial": partial, "previous": prev, "pocket": _agent_view_dict(doc)},
            None,
        )


# ---------------------------------------------------------------------------
# Granular rippleSpec.sources mutations — agent-facing (RFC 04 alpha)
# ---------------------------------------------------------------------------
#
# The third top-level rippleSpec key, alongside ``ui`` (node ops) and
# ``state`` (state ops). ``sources`` declares read-only GET data bindings
# the ``source_executor`` runs against the pocket's configured backend.
#
# Unlike hydrated state — which is response-body only and never persisted —
# a ``sources`` declaration IS part of the pocket document, so these ops
# call ``doc.save()``. The create flow already accepts ``sources`` in the
# rippleSpec it persists; these ops give the EDIT specialist the same
# reach on an existing pocket.
#
# Each binding is validated through ``SourceBinding`` (source_executor.py)
# before it is written — an invalid binding is rejected, never stored.


def _sources_root(doc: _PocketDoc) -> dict[str, Any]:
    """Return the mutable ``rippleSpec`` dict for a doc, creating it if
    absent. ``sources_ops`` materialises the ``sources`` key on demand."""
    spec = doc.rippleSpec
    if not isinstance(spec, dict):
        spec = {}
        doc.rippleSpec = spec
    return spec


async def agent_set_source(
    pocket_id: str,
    *,
    source_key: str,
    binding: dict[str, Any],
) -> tuple[dict | None, str | None]:
    """Declare (or replace) a read-only data source on the pocket.

    ``binding`` is validated through the ``SourceBinding`` model — an
    entry missing ``path`` / ``bind``, or carrying a write verb, is
    rejected before any write. The validated, normalized binding (with
    ``refresh`` defaulted) is what gets persisted.

    Returns ``({"source_key": ..., "binding": ..., "pocket": <view>},
    None)`` on success.
    """
    from pocketpaw_ee.cloud.pockets.source_executor import SourceBinding

    if not source_key:
        return None, "source_key is required"
    if not isinstance(binding, dict):
        return None, "binding must be an object"
    try:
        validated = SourceBinding.model_validate(binding)
    except PydanticValidationError as exc:
        return None, f"invalid source binding: {exc.errors()[0].get('msg', exc)}"

    normalized = validated.model_dump(mode="json")
    async with _pocket_lock(pocket_id):
        doc, err = await _agent_load_doc(pocket_id)
        if err or doc is None:
            return None, err
        spec = _sources_root(doc)
        try:
            sources_ops.set_source(spec, source_key, normalized)
        except ValueError as exc:
            return None, str(exc)
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            return None, f"save failed: {exc}"
        await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
        return (
            {
                "source_key": source_key,
                "binding": normalized,
                "pocket": _agent_view_dict(doc),
            },
            None,
        )


async def agent_remove_source(
    pocket_id: str,
    *,
    source_key: str,
) -> tuple[dict | None, str | None]:
    """Remove a read-only data source declaration from the pocket.

    Idempotent — removing a source that was never declared still
    succeeds. Returns ``({"source_key": ..., "pocket": <view>}, None)``.
    """
    if not source_key:
        return None, "source_key is required"
    async with _pocket_lock(pocket_id):
        doc, err = await _agent_load_doc(pocket_id)
        if err or doc is None:
            return None, err
        spec = _sources_root(doc)
        sources_ops.remove_source(spec, source_key)
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            return None, f"save failed: {exc}"
        await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
        return (
            {"source_key": source_key, "pocket": _agent_view_dict(doc)},
            None,
        )


# ---------------------------------------------------------------------------
# Granular rippleSpec.actions mutations — agent-facing (RFC 05 M2a)
# ---------------------------------------------------------------------------
#
# The write-action sibling of the ``sources`` ops above. ``actions`` is a
# fourth top-level rippleSpec key declaring write bindings (POST/PUT/PATCH/
# DELETE). Like ``sources`` it is part of the persisted pocket document, so
# these ops call ``doc.save()``. Each binding is validated through
# ``ActionBinding`` (action_executor.py) before it is written — an invalid
# binding is rejected, never stored. Authoring a binding does NOT authorize
# the write: the human-set ``allowed_writes`` allowlist on the backend
# config decides whether the write may actually fire.


async def agent_set_action(
    pocket_id: str,
    *,
    action_key: str,
    binding: dict[str, Any],
) -> tuple[dict | None, str | None]:
    """Declare (or replace) a write action on the pocket.

    ``binding`` is validated through the ``ActionBinding`` model — an
    entry missing ``method`` / ``path``, or carrying a non-write verb, is
    rejected before any write. M2b governance/metering fields
    (``requires_instinct``, ``outcome``, …) are CARRIED THROUGH unchanged:
    ``ActionBinding`` ignores them on parse, so they are taken from the
    raw ``binding`` dict and re-merged onto the validated core. The write
    only fires at run time if the human owner has allow-listed the
    method+path in backend settings.

    Returns ``({"action_key": ..., "binding": ..., "pocket": <view>},
    None)`` on success.
    """
    from pocketpaw_ee.cloud.pockets.action_executor import ActionBinding

    if not action_key:
        return None, "action_key is required"
    if not isinstance(binding, dict):
        return None, "binding must be an object"
    try:
        validated = ActionBinding.model_validate(binding)
    except PydanticValidationError as exc:
        return None, f"invalid action binding: {exc.errors()[0].get('msg', exc)}"

    # The validated model drops M2b governance fields (extra: ignore). Carry
    # them through verbatim so an M2b-aware client can author them now and
    # they survive a round-trip through an M2a runtime.
    normalized = {**binding, **validated.model_dump(mode="json")}
    async with _pocket_lock(pocket_id):
        doc, err = await _agent_load_doc(pocket_id)
        if err or doc is None:
            return None, err
        spec = _sources_root(doc)
        try:
            actions_ops.set_action(spec, action_key, normalized)
        except ValueError as exc:
            return None, str(exc)
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            return None, f"save failed: {exc}"
        await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
        return (
            {
                "action_key": action_key,
                "binding": normalized,
                "pocket": _agent_view_dict(doc),
            },
            None,
        )


async def agent_remove_action(
    pocket_id: str,
    *,
    action_key: str,
) -> tuple[dict | None, str | None]:
    """Remove a write-action declaration from the pocket.

    Idempotent — removing an action that was never declared still
    succeeds. Returns ``({"action_key": ..., "pocket": <view>}, None)``.
    """
    if not action_key:
        return None, "action_key is required"
    async with _pocket_lock(pocket_id):
        doc, err = await _agent_load_doc(pocket_id)
        if err or doc is None:
            return None, err
        spec = _sources_root(doc)
        actions_ops.remove_action(spec, action_key)
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            return None, f"save failed: {exc}"
        await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
        return (
            {"action_key": action_key, "pocket": _agent_view_dict(doc)},
            None,
        )


async def agent_create(
    *,
    workspace_id: str,
    owner_id: str,
    name: str,
    description: str = "",
    type_: str = "custom",
    pattern: str | None = None,
    icon: str = "",
    color: str = "",
    ripple_spec: dict | None = None,
    engine: str = "ripple",
    source: dict[str, str] | None = None,
    trusted: bool = False,
) -> tuple[dict | None, str | None, str | None]:
    """Insert a brand-new pocket owned by ``owner_id`` in ``workspace_id``.

    Returns ``(view_dict, pocket_id, None)`` on success or
    ``(None, None, error)`` on failure. Returning the id alongside the
    view lets the caller link sessions / push SSE events without
    re-parsing the dict.

    ``type_`` / ``pattern`` carry the create intent. The marketing-site
    brain (``pocketpaw-create-paw-site``) passes ``type_="site"`` +
    ``pattern="landing"`` via the specialist create path so the published
    page renders as a landing page rather than a dashboard. Both keep
    today's defaults (``type_="custom"``, ``pattern=None``) for callers
    that pass neither, so the change is additive — no Mongo migration.

    ``engine`` / ``source`` select the Paw Sites generation track. The
    default ``engine="ripple"`` compiles ``ripple_spec`` into the site;
    ``engine="svelte"`` materializes ``source`` (a hand-written SvelteKit
    source map ``{relative_path: file_contents}``) instead — the svelte
    analog of ``ripple_spec``. The svelte-site create flow
    (``create_svelte_site`` / ``pocketpaw-create-svelte-site``) passes
    ``engine="svelte"`` + ``source=<map>`` with ``ripple_spec=None`` and
    ``trusted=True`` (the source is author-controlled component files, not
    LLM-drafted ripple JSON, so there is no catalog gate to run). Both keep
    today's defaults (``engine="ripple"``, ``source=None``) for ripple
    callers — additive, no Mongo migration.

    ``trusted=True`` skips the STRICT catalog gate — use it ONLY for a
    code-assembled spec the caller fully controls (the deterministic Paw Site
    fast-path's ``assemble_landing_spec``), never for raw LLM output. The strict
    gate's allow-list is the PUBLISHED widget manifest, which lags the renderer:
    the real marketing widgets (navbar/feature-grid/testimonial/logo-cloud/cta/
    footer) ship in the renderer but are absent from the current published
    manifest, so the gate false-rejects a perfectly valid landing page and its
    "suggestion" pushes the caller toward generic widgets (avatar/data-grid/…) —
    the exact downgrade the deterministic fast-path exists to prevent. The spec
    is still normalized + structurally validated (``validate_ripple_spec_logged``)
    and the embed audit still runs; only the manifest-type allow-list (which is
    the stale part) is bypassed. The normal agent path keeps ``trusted=False``
    and the strict gate unchanged.
    """
    if not name:
        return None, None, "name is required"
    normalized = normalize_ripple_spec(ripple_spec) if ripple_spec else None
    if normalized:
        validate_ripple_spec_logged(normalized, workspace_id=workspace_id)
        # Agent-generation path — strict catalog gate. A violation is
        # returned as the error string so the specialist sees the
        # corrective detail and can retry, instead of persisting a spec
        # the renderer would draw as a red "Unknown widget type" box.
        # Trusted code-assembled specs skip the STRICT gate (the published
        # manifest is stale for marketing widgets — see the docstring) but
        # still get the LOGGED catalog walk so the embed audit runs and any
        # genuine drift is recorded, just not blocked.
        if trusted:
            await _gate_catalog(normalized, strict=False, actor=owner_id, workspace_id=workspace_id)
        else:
            try:
                await _gate_catalog(
                    normalized, strict=True, actor=owner_id, workspace_id=workspace_id
                )
            except CatalogViolationError as exc:
                return None, None, format_violations_for_agent(exc.violations)
            except ActionWiringViolationError as exc:
                return None, None, format_action_violations_for_agent(exc.violations)
            except MissingRequiredPropError as exc:
                return None, None, format_required_prop_violations_for_agent(exc.violations)
    try:
        doc = _PocketDoc(
            workspace=workspace_id,
            name=name,
            description=description,
            type=type_,
            pattern=pattern,
            icon=icon,
            color=color,
            owner=owner_id,
            rippleSpec=normalized,
            engine=engine,
            source=source,
            visibility="workspace",
        )
        await doc.insert()
    except Exception as exc:  # noqa: BLE001
        return None, None, f"insert failed: {exc}"
    await emit(PocketCreated(data=await _pocket_event_payload(doc)))
    return _agent_view_dict(doc), str(doc.id), None


async def create_pocket_and_session(
    spec: dict[str, Any],
    session_key: str,
    user_id: str | None = None,
    workspace_id: str | None = None,
) -> str | None:
    """Create a pocket + linked chat session in MongoDB. Returns the pocket
    id, or ``None`` on failure.

    Backs the core ``pocketpaw.pockets`` PocketWriter extension point:
    ``pocketpaw.agents.loop`` calls this when a local-mode CreatePocketTool
    emits a ``pocket_event: created``. Identity arrives as explicit args
    (threaded from ``InboundMessage.metadata``) rather than per-stream
    ContextVars, because the agent loop has no SSE request scope. When
    ``user_id`` / ``workspace_id`` are missing it falls back to legacy
    heuristics — ``user.active_workspace`` first, then first-owned
    workspace, then any workspace — so self-hosted single-user deployments
    (CLI, Telegram) without JWT auth still work.
    """
    try:
        from datetime import UTC, datetime

        from pocketpaw_ee.cloud.models.session import Session
        from pocketpaw_ee.cloud.models.user import User
        from pocketpaw_ee.cloud.models.workspace import Workspace

        # ── User selection ──────────────────────────────────────────────
        # Prefer an explicitly-threaded user id so agent-created pockets
        # land under the caller, not whichever user comes first out of Mongo.
        user = None
        if user_id:
            try:
                user = await User.get(PydanticObjectId(user_id))
            except Exception:  # noqa: BLE001
                logger.warning("Invalid cloud_user_id %r; falling back to first user", user_id)
                user = None
        if not user:
            user = await User.find_one()
        if not user:
            logger.warning("Cannot create pocket — no user in DB")
            return None
        user_id = str(user.id)

        # ── Workspace selection ─────────────────────────────────────────
        # Priority: explicit workspace_id → user.active_workspace → first
        # owned workspace → any workspace.
        workspace = None
        target_ws = workspace_id or getattr(user, "active_workspace", None)
        if target_ws:
            try:
                workspace = await Workspace.get(PydanticObjectId(target_ws))
            except Exception:  # noqa: BLE001
                workspace = None
        if not workspace:
            workspace = await Workspace.find_one(Workspace.owner == user_id)
        if not workspace:
            workspace = await Workspace.find_one()
        if not workspace:
            logger.warning("Cannot create pocket — no workspace in DB")
            return None
        workspace_id = str(workspace.id)

        # Create the pocket through the standard service entry point.
        meta = spec.get("metadata", {})
        pocket = await create(
            workspace_id,
            user_id,
            CreatePocketRequest(
                name=spec.get("title") or spec.get("name") or "Untitled",
                description=spec.get("description", ""),
                type=meta.get("category", "custom"),
                icon="sparkles",
                color=meta.get("color", "#0A84FF"),
                rippleSpec=spec,
            ),
        )
        pocket_id = str(pocket["_id"])

        # Link (find-or-create) the chat session to the new pocket.
        safe_key = session_key.replace(":", "_") if session_key else ""
        if safe_key:
            existing = await Session.find_one(Session.sessionId == safe_key)
            if existing:
                existing.pocket = pocket_id
                await existing.save()
            else:
                session = Session(
                    sessionId=safe_key,
                    workspace=workspace_id,
                    owner=user_id,
                    title=spec.get("title") or "New Chat",
                    pocket=pocket_id,
                    lastActivity=datetime.now(UTC),
                )
                await session.insert()

        logger.info("Created pocket %s + session %s in MongoDB", pocket_id, safe_key)
        return pocket_id
    except Exception:
        logger.warning("Failed to create pocket/session in MongoDB", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Pocket backend binding (RFC 04 alpha)
#
# A pocket is bound to ONE external backend (base URL + auth credential).
# The credential lives in the separate ``PocketBackendCredential``
# collection — never on the Pocket document, never in ``rippleSpec``. This
# service is the sole Beanie writer for that collection.
# ---------------------------------------------------------------------------


def _audit_backend_config(
    *, actor: str, action: str, workspace_id: str, pocket_id: str, base_url: str, auth_type: str
) -> None:
    """Write an audit-log entry for a backend-config mutation.

    Logs ``base_url`` / ``auth_type`` / ``pocket_id`` only — the token is
    NEVER passed here. Category ``pocket_backend_config``, severity WARNING.
    Audit failures must not break the config flow, so the call is wrapped.
    """
    try:
        from pocketpaw.security.audit import AuditEvent, AuditSeverity, get_audit_logger

        get_audit_logger().log(
            AuditEvent.create(
                severity=AuditSeverity.WARNING,
                actor=actor,
                action=action,
                target=pocket_id,
                status="success",
                category="pocket_backend_config",
                workspace_id=workspace_id,
                pocket_id=pocket_id,
                base_url=base_url,
                auth_type=auth_type,
            )
        )
    except Exception:  # noqa: BLE001 — audit must never break the mutation
        logger.warning("pocket backend config audit-log write failed", exc_info=True)


async def set_pocket_backend(
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    base_url: str,
    auth_type: str,
    auth_token: str,
    auth_header: str | None = None,
    *,
    backend_type: str = "http",
    connector_name: str | None = None,
) -> dict:
    """Bind a pocket to one backend — upsert its credential row.

    Two backend shapes:

    * ``backend_type="http"`` (the default) — ``base_url`` is validated
      strictly (https-only, no internal hosts), the token is encrypted via
      ``backend_crypto`` before it touches the DB, and the plaintext is never
      persisted or logged.
    * ``backend_type="connector"`` — ``connector_name`` names a
      workspace-bound connector. ``base_url``/``auth_*`` are ignored (and NOT
      required); the connector is validated to actually exist + be enabled for
      this workspace via ``connectors.service.is_connector_enabled_for_workspace``
      (rejected with ``pocket_backend.unknown_connector`` otherwise).

    Returns the non-secret summary (carries ``backend_type`` +
    ``connector_name``, never the token).
    """
    if backend_type not in ("http", "connector"):
        raise ValidationError(
            "pocket_backend.invalid_backend_type",
            f"backend_type must be 'http' or 'connector', got {backend_type!r}",
        )

    if backend_type == "connector":
        if not connector_name:
            raise ValidationError(
                "pocket_backend.missing_connector",
                "backend_type 'connector' requires a connector_name",
            )
        # The connector must be a real registry connector AND enabled for this
        # workspace. The Beanie read lives in the connectors service (the owner
        # of the connector docs) — imported lazily to keep the static import
        # graph free of a cycle (the connectors service imports this service).
        from pocketpaw_ee.cloud.connectors import service as connectors_service

        if not await connectors_service.is_connector_enabled_for_workspace(
            workspace_id, connector_name
        ):
            raise ValidationError(
                "pocket_backend.unknown_connector",
                f"connector {connector_name!r} is not enabled for this workspace — "
                "enable it before binding it as a pocket backend",
            )
        # A connector backend carries no base_url / auth — clear them so a
        # row that switched http -> connector never keeps a stale credential.
        base_url = ""
        auth_type = "none"
        auth_header = None
        encrypted_token = nonce = salt = None
    else:
        from pocketpaw.security.url_validators import validate_external_url_strict

        # Raises ValueError on a bad URL — surfaces as a 400 via the router.
        try:
            base_url = validate_external_url_strict(base_url)
        except ValueError as exc:
            raise ValidationError("pocket_backend.invalid_url", str(exc)) from exc

        if auth_type != "none" and not auth_token:
            raise ValidationError(
                "pocket_backend.missing_token",
                f"auth_type '{auth_type}' requires a non-empty auth_token",
            )

        encrypted_token = nonce = salt = None
        if auth_type != "none":
            encrypted_token, nonce, salt = backend_crypto.encrypt_token(auth_token)
        connector_name = None

    existing = await _BackendCredentialDoc.find_one(
        _BackendCredentialDoc.pocket_id == pocket_id,
        _BackendCredentialDoc.workspace_id == workspace_id,
    )
    if existing is not None:
        existing.backend_type = backend_type
        existing.connector_name = connector_name
        existing.base_url = base_url
        existing.auth_type = auth_type
        existing.auth_header = auth_header
        existing.encrypted_token = encrypted_token
        existing.nonce = nonce
        existing.salt = salt
        await existing.save()
    else:
        await _BackendCredentialDoc(
            pocket_id=pocket_id,
            workspace_id=workspace_id,
            backend_type=backend_type,
            connector_name=connector_name,
            base_url=base_url,
            auth_type=auth_type,
            auth_header=auth_header,
            encrypted_token=encrypted_token,
            nonce=nonce,
            salt=salt,
        ).insert()

    _audit_backend_config(
        actor=user_id,
        action="pocket.backend.configure",
        workspace_id=workspace_id,
        pocket_id=pocket_id,
        base_url=base_url,
        auth_type=auth_type,
    )
    # no-event: backend credentials are a separate collection, not pocket
    # state — no downstream handler keys off a PocketUpdated for them.
    return {
        "backend_type": backend_type,
        "connector_name": connector_name,
        "base_url": base_url,
        "auth_type": auth_type,
        "configured": True,
    }


def _allowed_writes_wire(doc: _BackendCredentialDoc) -> list[dict[str, str]]:
    """Render a backend doc's ``allowed_writes`` as plain wire dicts.

    Each rule is ``{method, path_pattern}``. An older row written before
    RFC 05 has no ``allowed_writes`` attribute — ``getattr`` defaults it
    to an empty list (fail-closed: no write fires).
    """
    rules = getattr(doc, "allowed_writes", None) or []
    out: list[dict[str, str]] = []
    for rule in rules:
        out.append({"method": rule.method, "path_pattern": rule.path_pattern})
    return out


def _approval_route_wire(doc: _BackendCredentialDoc) -> dict[str, str | None] | None:
    """Render a backend doc's ``approval_route`` as a plain wire dict.

    Returns ``{"mode": ..., "user_id": ...}`` or ``None`` when no route is
    set (the default — gated writes go to the pocket owner). A row written
    before RFC 05 M2b.1 has no attribute — ``getattr`` defaults it to
    ``None``.
    """
    route = getattr(doc, "approval_route", None)
    if route is None:
        return None
    return {"mode": route.mode, "user_id": route.user_id}


def _backend_summary_wire(doc: _BackendCredentialDoc) -> dict:
    """Render the full NON-SECRET backend summary for a credential row.

    The ONE place the wire summary is shaped so ``get_pocket_backend`` and the
    write-policy / approval-route setters never drift. NEVER includes the
    token. ``backend_type`` / ``connector_name`` come via ``getattr`` defaults
    so a legacy row reads as an http backend — back-compatible.
    """
    return {
        "backend_type": getattr(doc, "backend_type", "http"),
        "connector_name": getattr(doc, "connector_name", None),
        "base_url": doc.base_url,
        "auth_type": doc.auth_type,
        "configured": True,
        "allowed_writes": _allowed_writes_wire(doc),
        "approval_route": _approval_route_wire(doc),
    }


async def get_pocket_backend(workspace_id: str, pocket_id: str) -> dict | None:
    """Return the pocket's backend binding summary, or ``None`` if unset.

    NEVER returns the token — only ``backend_type`` / ``connector_name`` /
    ``base_url`` / ``auth_type`` / ``configured`` / ``allowed_writes`` (the
    write allowlist) / ``approval_route`` (the gated-write approver routing;
    ``None`` when unset). All owner/editor-facing non-secrets.

    ``backend_type`` / ``connector_name`` come via ``getattr`` defaults so a
    row written before this feature reads as ``backend_type="http"`` /
    ``connector_name=None`` — back-compatible.
    """
    doc = await _BackendCredentialDoc.find_one(
        _BackendCredentialDoc.pocket_id == pocket_id,
        _BackendCredentialDoc.workspace_id == workspace_id,
    )
    if doc is None:
        return None
    return _backend_summary_wire(doc)


async def get_pocket_backend_for_executor(
    workspace_id: str, pocket_id: str
) -> (
    tuple[
        str,
        str,
        str | None,
        str,
        list[dict[str, str]],
        dict[str, str | None] | None,
        str,
        str | None,
    ]
    | None
):
    """Internal — return ``(base_url, auth_type, auth_header, token,
    allowed_writes, approval_route, backend_type, connector_name)``.

    Decrypts the stored token (http backends only; a connector backend has no
    token). Used by the run-sources route handler (which now also reads the
    trailing ``backend_type`` / ``connector_name`` to route the run), the
    run-action route handler (it needs ``allowed_writes`` / ``approval_route``),
    and ``instinct_bridge`` / ``temporal_dispatcher`` / ``bulk_dispatch`` (which
    ignore the trailing elements). Returns ``None`` when the pocket has no
    backend configured. The plaintext token must never be logged or returned to
    a client.

    ``backend_type`` / ``connector_name`` come via ``getattr`` defaults so a
    legacy row reads as ``"http"`` / ``None`` — back-compatible.
    """
    doc = await _BackendCredentialDoc.find_one(
        _BackendCredentialDoc.pocket_id == pocket_id,
        _BackendCredentialDoc.workspace_id == workspace_id,
    )
    if doc is None:
        return None

    token = ""
    if doc.auth_type != "none" and doc.encrypted_token and doc.nonce and doc.salt:
        token = backend_crypto.decrypt_token(doc.encrypted_token, doc.nonce, doc.salt)
    return (
        doc.base_url,
        doc.auth_type,
        doc.auth_header,
        token,
        _allowed_writes_wire(doc),
        _approval_route_wire(doc),
        getattr(doc, "backend_type", "http"),
        getattr(doc, "connector_name", None),
    )


async def set_pocket_write_policy(
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    allowed_writes: list[dict[str, str]],
) -> dict:
    """Replace the pocket's write allowlist. Owner-only.

    ``allowed_writes`` is a list of ``{method, path_pattern}`` rules. The
    list lives on the backend-credential row — OUTSIDE the spec — so the
    agent (which authors the spec) cannot widen its own write blast
    radius. An empty list is valid and revokes every write.

    Rejects with ``pocket_backend.not_configured`` if the pocket has no
    backend configured: a write policy with no backend to apply it to is
    meaningless, and silently storing one would mask the misconfiguration.
    The mutation is audit-logged via the ``_audit_backend_config`` path.
    """
    doc = await _BackendCredentialDoc.find_one(
        _BackendCredentialDoc.pocket_id == pocket_id,
        _BackendCredentialDoc.workspace_id == workspace_id,
    )
    if doc is None:
        raise ValidationError(
            "pocket_backend.not_configured",
            "This pocket has no backend configured — set one before a write policy",
        )

    # `model_validate` re-checks the method Literal at runtime — the router
    # already validated each rule through `AllowedWriteDTO`, but internal
    # callers (bus handlers, jobs) re-parse here, matching the entry-point
    # validation rule.
    doc.allowed_writes = [_AllowedWriteDoc.model_validate(rule) for rule in allowed_writes]
    await doc.save()

    _audit_backend_config(
        actor=user_id,
        action="pocket.backend.write_policy",
        workspace_id=workspace_id,
        pocket_id=pocket_id,
        base_url=doc.base_url,
        auth_type=doc.auth_type,
    )
    # no-event: backend credentials (and the write policy on them) are a
    # separate collection, not pocket state — no downstream handler keys
    # off a PocketUpdated for them.
    return _backend_summary_wire(doc)


async def set_pocket_approval_route(
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    route: dict[str, str | None] | None,
) -> dict:
    """Set (or clear) the pocket's gated-write approval route. Owner-only.

    ``route`` is ``{"mode": "owner"|"user", "user_id": ...}`` or ``None``.
    ``None`` (or ``mode="owner"``) clears the route — ``requires_instinct``
    writes then route to the pocket owner. ``mode="user"`` routes to the
    named member; the ``user_id`` is validated as a CURRENT workspace
    member here, at set-time, so the bridge can trust a stored route
    without re-checking on every parked write.

    Lives on the backend-credential row alongside the write allowlist —
    owner-set, outside the spec. Rejects with ``pocket_backend.not_configured``
    when the pocket has no backend (a route with no backend to gate is
    meaningless), and ``pocket_backend.invalid_approver`` when a
    ``mode="user"`` route names a non-member. Audit-logged.
    """
    doc = await _BackendCredentialDoc.find_one(
        _BackendCredentialDoc.pocket_id == pocket_id,
        _BackendCredentialDoc.workspace_id == workspace_id,
    )
    if doc is None:
        raise ValidationError(
            "pocket_backend.not_configured",
            "This pocket has no backend configured — set one before an approval route",
        )

    new_route: _ApprovalRouteDoc | None = None
    if route is not None:
        parsed = _ApprovalRouteDoc.model_validate(route)
        if parsed.mode == "user":
            if not parsed.user_id:
                raise ValidationError(
                    "pocket_backend.invalid_approver",
                    "approval route mode 'user' requires a user_id",
                )
            from pocketpaw_ee.cloud.workspace import service as workspace_service

            members = await workspace_service.list_member_ids(workspace_id)
            if parsed.user_id not in members:
                raise ValidationError(
                    "pocket_backend.invalid_approver",
                    "the routed approver is not a member of this workspace",
                )
            new_route = parsed
        # mode == "owner" → store None (the explicit default).

    doc.approval_route = new_route
    await doc.save()

    _audit_backend_config(
        actor=user_id,
        action="pocket.backend.approval_route",
        workspace_id=workspace_id,
        pocket_id=pocket_id,
        base_url=doc.base_url,
        auth_type=doc.auth_type,
    )
    # no-event: see set_pocket_write_policy — credential rows aren't pocket state.
    return _backend_summary_wire(doc)


async def remove_pocket_backend(workspace_id: str, user_id: str, pocket_id: str) -> None:
    """Delete a pocket's backend credential row. Idempotent.

    Audit-logs the removal. A no-op (no row) still returns cleanly so the
    route is idempotent.
    """
    doc = await _BackendCredentialDoc.find_one(
        _BackendCredentialDoc.pocket_id == pocket_id,
        _BackendCredentialDoc.workspace_id == workspace_id,
    )
    if doc is None:
        return
    base_url, auth_type = doc.base_url, doc.auth_type
    await doc.delete()
    _audit_backend_config(
        actor=user_id,
        action="pocket.backend.remove",
        workspace_id=workspace_id,
        pocket_id=pocket_id,
        base_url=base_url,
        auth_type=auth_type,
    )
    # no-event: see set_pocket_backend — credential rows aren't pocket state.


# ---------------------------------------------------------------------------
# Pocket data-source refresh — interval scan + webhook secret (RFC 04 M3)
#
# Interval refresh: the in-process scheduler (``pockets/refresh_scheduler.py``)
# needs the set of pockets that carry at least one ``"interval"``-refresh
# source. ``list_interval_source_pockets`` is the scan helper — it stays in
# the service so the Pocket Beanie read obeys the 4-file rule.
#
# Webhook refresh: a ``"webhook"``-refresh source is re-run by an inbound
# authenticated POST. The shared secret lives on the backend-credential row
# (NEVER in the spec). The helpers below generate / rotate / resolve it.
# ---------------------------------------------------------------------------


async def list_interval_source_pockets() -> list[dict]:
    """Return every pocket that declares at least one interval-refresh source.

    Shape per row: ``{pocket_id, workspace_id, sources}`` where ``sources``
    is the raw ``rippleSpec.sources`` dict. The interval scheduler iterates
    this set, picks the ``"interval"`` sources, and re-runs each when due.

    Global read (no workspace filter) — the scheduler is a process-wide
    background loop with no tenant context; it serves every workspace. The
    Mongo ``$elemMatch`` narrows to sources whose ``refresh`` array contains
    ``"interval"`` so the loop only loads pockets it can act on.
    """
    # global-read: the interval scheduler is a process-wide background loop
    # with no per-tenant request context; it scans across all workspaces.
    query: dict[str, Any] = {
        "rippleSpec.sources": {"$exists": True},
    }
    docs = await _PocketDoc.find(query).to_list()
    out: list[dict] = []
    for d in docs:
        spec = d.rippleSpec
        if not isinstance(spec, dict):
            continue
        sources = spec.get("sources")
        if not isinstance(sources, dict) or not sources:
            continue
        has_interval = any(
            isinstance(b, dict) and "interval" in (b.get("refresh") or []) for b in sources.values()
        )
        if not has_interval:
            continue
        out.append(
            {
                "pocket_id": str(d.id),
                "workspace_id": d.workspace,
                "sources": sources,
            }
        )
    return out


async def get_pocket_ripple_spec(workspace_id: str, pocket_id: str) -> dict | None:
    """Return a pocket's raw ``rippleSpec`` for an internal refresh caller.

    Used by the webhook-refresh route — it has already authenticated via
    the per-pocket webhook secret, so this skips the user-facing access
    checks in ``get``. Returns ``None`` when the pocket is missing or
    belongs to a different workspace. ``$source`` markers are NOT resolved
    — the source executor only reads ``rippleSpec.sources``.
    """
    try:
        doc = await _PocketDoc.get(PydanticObjectId(pocket_id))
    except Exception:  # noqa: BLE001
        return None
    if doc is None or doc.workspace != workspace_id:
        return None
    spec = doc.rippleSpec
    return spec if isinstance(spec, dict) else {}


async def get_pocket_spec_and_slug(
    workspace_id: str, pocket_id: str
) -> tuple[dict | None, str | None] | None:
    """Return ``(rippleSpec, template_slug)`` for a pocket, or ``None``.

    Tenant-scoped, single doc read. ``None`` (the outer value) means the
    pocket is missing OR belongs to another workspace — the same
    not-an-oracle posture as :func:`get_pocket_ripple_spec`. On success the
    spec is a dict (``{}`` when unset) and the slug is the stored
    ``template_slug`` or ``None``.

    Added for the Template Reconcile service (P2.4) so reconcile resolves
    everything it needs through THIS service in one read and never imports
    the ``Pocket`` Beanie model itself — the "Beanie writes (and reads)
    funnel through service.py" boundary the import-linter pins.
    """
    try:
        doc = await _PocketDoc.get(PydanticObjectId(pocket_id))
    except Exception:  # noqa: BLE001
        return None
    if doc is None or doc.workspace != workspace_id:
        return None
    spec = doc.rippleSpec
    slug = getattr(doc, "template_slug", None)
    return (spec if isinstance(spec, dict) else {}), (slug or None)


async def resolve_webhook_pocket(pocket_id: str, presented_secret: str) -> tuple | None:
    """Authenticate an inbound webhook-refresh request by its secret.

    Returns the executor-creds tuple ``(base_url, auth_type, auth_header,
    token, allowed_writes, approval_route, workspace_id)`` ONLY when the
    pocket has a backend with a webhook secret AND ``presented_secret``
    matches it (constant-time compare). Returns ``None`` for EVERY failure
    — wrong secret, no secret set, no backend, missing pocket — so the
    caller raises one identical error and the endpoint is not a
    pocket-existence oracle.

    The comparison uses ``secrets.compare_digest`` so a timing side channel
    cannot leak the secret. An empty presented secret short-circuits to
    ``None`` without a DB read.
    """
    if not pocket_id or not presented_secret:
        return None
    # No workspace filter: the inbound webhook carries no tenant context.
    # The pocket_id + secret pair IS the credential; the secret compare
    # below is the gate. The credential row carries its own workspace_id.
    # global-read: webhook callers present no tenant context — the secret is the gate.
    doc = await _BackendCredentialDoc.find_one(
        _BackendCredentialDoc.pocket_id == pocket_id,
    )
    if doc is None:
        return None
    stored = getattr(doc, "webhook_secret", None)
    if not stored:
        return None
    if not secrets.compare_digest(stored, presented_secret):
        return None

    token = ""
    if doc.auth_type != "none" and doc.encrypted_token and doc.nonce and doc.salt:
        token = backend_crypto.decrypt_token(doc.encrypted_token, doc.nonce, doc.salt)
    return (
        doc.base_url,
        doc.auth_type,
        doc.auth_header,
        token,
        _allowed_writes_wire(doc),
        _approval_route_wire(doc),
        doc.workspace_id,
    )


def _new_webhook_secret() -> str:
    """Mint a fresh URL-safe webhook secret (32 bytes of entropy)."""
    return secrets.token_urlsafe(32)


async def get_webhook_secret(workspace_id: str, pocket_id: str) -> str | None:
    """Return the pocket's webhook secret, or ``None`` if none is set.

    Owner-only at the route layer. The secret is the credential a webhook
    caller echoes back, so the owner must be able to read it once to
    configure the upstream. Raises ``pocket_backend.not_configured`` when
    the pocket has no backend — a webhook secret with no backend to refresh
    against is meaningless.
    """
    doc = await _BackendCredentialDoc.find_one(
        _BackendCredentialDoc.pocket_id == pocket_id,
        _BackendCredentialDoc.workspace_id == workspace_id,
    )
    if doc is None:
        raise ValidationError(
            "pocket_backend.not_configured",
            "This pocket has no backend configured — set one before a webhook secret",
        )
    return getattr(doc, "webhook_secret", None)


async def rotate_webhook_secret(workspace_id: str, user_id: str, pocket_id: str) -> str:
    """Generate a fresh webhook secret for the pocket, replacing any prior.

    Owner-only. Rotating invalidates the previous secret immediately — any
    upstream still using the old value gets a 403 until reconfigured.
    Returns the new secret (the ONLY time it is handed back outside the
    owner-only GET). Audit-logged. Rejects with
    ``pocket_backend.not_configured`` when no backend is set.
    """
    doc = await _BackendCredentialDoc.find_one(
        _BackendCredentialDoc.pocket_id == pocket_id,
        _BackendCredentialDoc.workspace_id == workspace_id,
    )
    if doc is None:
        raise ValidationError(
            "pocket_backend.not_configured",
            "This pocket has no backend configured — set one before a webhook secret",
        )
    doc.webhook_secret = _new_webhook_secret()
    await doc.save()
    _audit_backend_config(
        actor=user_id,
        action="pocket.backend.webhook_rotate",
        workspace_id=workspace_id,
        pocket_id=pocket_id,
        base_url=doc.base_url,
        auth_type=doc.auth_type,
    )
    # no-event: see set_pocket_backend — credential rows aren't pocket state.
    return doc.webhook_secret


# ---------------------------------------------------------------------------
# Bulk action dispatch (RFC 03 v2 / Wave 3b)
# ---------------------------------------------------------------------------


async def dispatch_bulk_action(
    workspace_id: str,
    user_id: str,
    body: dict | Any,
    *,
    template: Any | None = None,
    now: Any | None = None,
) -> dict:
    """Fan out a ``kind: bulk`` action across the rows in ``body.rows``.

    Service-level entry point that:

    1. Re-validates the body via ``DispatchBulkRequest.model_validate``
       (rule 6 — internal callers re-parse the schema).
    2. Delegates the orchestration to ``bulk_dispatch.dispatch_bulk``,
       which calls the pure OSS planner, persists ONE batch approval
       if any row escalated, and fires each ready-to-run row through
       ``action_executor.run_action``.
    3. Emits a ``BulkActionDispatched`` event with the dispatch
       summary (counts + optional batch approval id) — rule 9.

    ``template`` is threaded through for internal callers (the future
    paw-enterprise UI fetches the template from its pocket and passes
    it here). Library tests pass it explicitly. A missing template
    raises ``ValidationError`` — we cannot fan out a bulk action
    without the template-level rules to evaluate.

    Returns a wire dict (rule 5) so the router can construct
    ``BulkDispatchResponse`` directly.
    """
    from pocketpaw_ee.cloud._core.realtime.events import BulkActionDispatched
    from pocketpaw_ee.cloud.pockets import bulk_dispatch
    from pocketpaw_ee.cloud.pockets.dto import DispatchBulkRequest

    body = DispatchBulkRequest.model_validate(body)

    if template is None:
        raise ValidationError(
            "bulk_action.template_required",
            "dispatch_bulk_action requires the resolved PocketTemplate",
        )

    result = await bulk_dispatch.dispatch_bulk(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=body.pocket_id,
        template=template,
        action_name=body.action_name,
        selected_rows=body.rows,
        now=now,
    )

    # Wire-friendly dict — frozen Pydantic models serialize cleanly.
    wire = result.model_dump()
    # Emit ONE event per dispatch call. Per EE rule 9, every state-
    # mutating service function emits its event on the way out. The
    # batch approval write inside ``dispatch_bulk`` already fired its
    # own ``InstinctApprovalCreated``; this event summarises the bulk
    # call so downstream audit / analytics listeners can key off a
    # single event per dispatch.
    approval_needed = len(result.approval_row_ids)
    await emit(
        BulkActionDispatched(
            data={
                "workspace_id": workspace_id,
                "pocket_id": body.pocket_id,
                "action_name": body.action_name,
                "total_rows": result.total_rows,
                "executed": len(result.executions),
                "blocked": len(result.blocked),
                "approval_needed": approval_needed,
                "batch_approval_id": result.batch_approval_id,
                "actor": user_id,
            }
        )
    )
    return wire


# ── Per-Pocket Connector Permissions ──────────────────────────────────


async def get_pocket_connector_permissions(
    pocket_id: str,
) -> list[str] | None:
    """Return the allowed connectors for a pocket.

    Returns ``None`` when the pocket inherits all workspace connectors
    (default). Returns a list (possibly empty) of connector names when
    the pocket has explicit restrictions.
    """
    doc = await _fetch_pocket(pocket_id)
    return doc.allowed_connectors


async def set_pocket_connector_permissions(
    pocket_id: str,
    allowed_connectors: list[str] | None,
) -> None:
    """Set the allowed connectors for a pocket.

    Pass ``None`` to inherit all workspace connectors.
    Pass ``[]`` to revoke all (pocket sees nothing).
    """
    doc = await _fetch_pocket(pocket_id)
    if allowed_connectors is not None:
        doc.allowed_connectors = allowed_connectors
    else:
        doc.allowed_connectors = None
    await doc.save()
    logger.info(
        "pocket.connector_permissions.set",
        extra={
            "pocket_id": pocket_id,
            "workspace_id": doc.workspace,
            "allowed_connectors": allowed_connectors,
        },
    )


async def grant_pocket_connector(
    pocket_id: str,
    connector_name: str,
) -> None:
    """Grant a connector to a pocket (additive)."""
    doc = await _fetch_pocket(pocket_id)
    current = doc.allowed_connectors
    if current is None:
        # Currently inheriting all — switch to explicit allowlist with just this connector.
        doc.allowed_connectors = [connector_name]
    elif connector_name not in current:
        doc.allowed_connectors = [*current, connector_name]
    else:
        return  # already granted
    await doc.save()
    logger.info(
        "pocket.connector_permissions.grant",
        extra={
            "pocket_id": pocket_id,
            "workspace_id": doc.workspace,
            "connector_name": connector_name,
        },
    )


async def revoke_pocket_connector(
    pocket_id: str,
    connector_name: str,
) -> None:
    """Revoke a connector from a pocket (removes from allowlist)."""
    doc = await _fetch_pocket(pocket_id)
    current = doc.allowed_connectors
    if current is None:
        return  # no restrictions, nothing to revoke
    next_list = [c for c in current if c != connector_name]
    # Keep the list (even empty) to distinguish "no restrictions" from "nothing allowed"
    doc.allowed_connectors = next_list
    await doc.save()
    logger.info(
        "pocket.connector_permissions.revoke",
        extra={
            "pocket_id": pocket_id,
            "workspace_id": doc.workspace,
            "connector_name": connector_name,
        },
    )


async def list_workspace_pocket_connector_permissions(
    workspace_id: str,
) -> dict[str, list[str] | None]:
    """Return the connector-permissions map for ALL pockets in a workspace.

    Returns a dict of pocket_id → allowed_connectors (list or None).
    ``None`` means the pocket inherits all workspace connectors.
    This is the bulk endpoint for the frontend's permission-store loader.
    """
    docs = await _PocketDoc.find({"workspace": workspace_id}).to_list()
    result: dict[str, list[str] | None] = {}
    for doc in docs:
        result[str(doc.id)] = doc.allowed_connectors
    return result


__all__ = [
    "access_via_share_link",
    "add_agent",
    "add_collaborator",
    "add_team_member",
    "add_widget",
    "agent_add_node",
    "agent_add_widget",
    "agent_append_state",
    "agent_create",
    "agent_list",
    "agent_move_node",
    "agent_patch_state",
    "agent_remove_action",
    "agent_remove_node",
    "agent_remove_source",
    "agent_remove_state",
    "agent_remove_widget",
    "agent_replace_node",
    "agent_set_action",
    "agent_set_node_prop",
    "agent_set_source",
    "agent_set_state",
    "agent_update",
    "agent_update_widget",
    "agent_view",
    "create",
    "create_from_ripple_spec",
    "create_pocket_and_session",
    "delete",
    "dispatch_bulk_action",
    "generate_share_link",
    "get",
    "get_pocket_backend",
    "get_pocket_backend_for_executor",
    "get_pocket_connector_permissions",
    "get_pocket_ripple_spec",
    "get_webhook_secret",
    "grant_pocket_connector",
    "has_action_run_access",
    "has_edit_access",
    "is_member",
    "is_owner",
    "list_interval_source_pockets",
    "list_pockets",
    "list_workspace_pocket_connector_permissions",
    "remove_agent",
    "remove_collaborator",
    "remove_pocket_backend",
    "remove_team_member",
    "remove_widget",
    "reorder_widgets",
    "resolve_pocket_template",
    "resolve_webhook_pocket",
    "revoke_pocket_connector",
    "revoke_share_link",
    "rotate_webhook_secret",
    "set_pocket_approval_route",
    "set_pocket_backend",
    "set_pocket_connector_permissions",
    "set_pocket_write_policy",
    "set_svelte_source_file",
    "unassign_project_on_pockets",
    "update",
    "update_share_link",
    "update_widget",
]
