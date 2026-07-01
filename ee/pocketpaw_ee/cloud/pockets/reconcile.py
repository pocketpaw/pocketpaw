# ee/pocketpaw_ee/cloud/pockets/reconcile.py
# Created: 2026-06-13 (feat/pocket-template-reconcile) — P2.4 Template
# Reconcile, the FIRST primitive built to the "universal access" shape from
# docs/design/drafts/2026-06-13-pocket-native-primitives.md. It is the
# CANONICAL REFERENCE the other native primitives copy: ONE typed service
# (this module) + THIN adapters per surface (REST routes in router.py, CLI
# command in src/pocketpaw/cli/pocket.py). All reconcile logic lives HERE;
# every adapter is a few lines that resolve identity and call this.
#
# Service / adapter split (the reference pattern):
#   * SERVICE  (this file): preview_reconcile / apply_reconcile — pure
#     in-process API. Other runtime modules (belt, automations, a future
#     scheduler) import and call these directly. No FastAPI, no Click here.
#   * REST adapter (router.py): POST /api/v1/pockets/{id}/reconcile/preview
#     and .../reconcile/apply — resolve the user, call the service, return
#     the result dict.
#   * CLI adapter (cli/pocket.py): `pocketpaw pocket reconcile <id>` — diff
#     by default, `--apply` to write.
#
# What reconcile does: a pocket stores ``doc.template_slug`` (the template it
# was installed from). Re-running an install/deploy script today CLOBBERS
# instance edits. Reconcile re-applies ONLY the template-owned regions of the
# source template while PRESERVING the instance-owned regions:
#
#   * TEMPLATE-OWNED (reconcile overwrites from the freshly-loaded template):
#       rippleSpec.ui, rippleSpec.actions, rippleSpec.sources, rippleSpec.shape
#   * INSTANCE-OWNED (reconcile NEVER touches):
#       rippleSpec.state (rows, selected_id, pending_proposal, last_decision,
#       ...) AND the pocket doc's name / owner / team / visibility.
#
# preview_reconcile is a pure dry-run (writes nothing, returns the diff).
# apply_reconcile writes through the existing spec write path
# (``service.update``), which normalizes + validates the spec and emits the
# ``PocketUpdated`` bus event — so reconcile rides the same rails as every
# other pocket mutation and never pokes the Pocket Beanie doc directly
# (the cloud "Beanie writes only from service.py" import-linter contract).
#
# Modified 2026-06-19 (feat/typed-ripplespec-phase1): the template-owned region
# partition is now DERIVED from ``TemplateLayer.model_fields`` (the typed
# layer-split model in ``pocketpaw.bundled_templates.schema``) instead of a
# hand-maintained tuple — single source of truth shared with ``service.update``.
# ``_build_reconciled_spec`` overlays the template-owned regions via the typed
# ``RippleSpec.with_template_layer`` merge, which preserves the instance-owned
# regions (``state`` / ``selections``) structurally rather than by string list.
# Behaviour is unchanged — the existing reconcile tests stay green; the typed
# model just makes "reconcile never touches state" a type boundary, not a
# convention. The reconcile service still exchanges flat dicts at its
# boundaries (the typed model is used INTERNALLY only).
"""Template Reconcile service — re-apply template-owned regions, preserve
instance-owned regions.

The partition is the load-bearing contract (see ``_TEMPLATE_OWNED_REGIONS``).
Everything else about a pocket — its ``state``, its name, its sharing — is
the instance's, and reconcile is a no-op for it.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any

from pocketpaw.bundled_templates.schema import RippleSpec, TemplateLayer
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets.dto import UpdatePocketRequest
from pocketpaw_ee.cloud.shared.errors import Forbidden, NotFound, ValidationError

logger = logging.getLogger(__name__)

# The rippleSpec regions the TEMPLATE owns. Reconcile overwrites exactly
# these keys from the source template and leaves every other key (notably
# ``state``) exactly as the instance has it. DERIVED from the typed
# ``TemplateLayer`` so there is ONE source of truth for the partition shared
# with ``service.update``'s clobber-fix — a field added to ``TemplateLayer``
# automatically becomes template-owned here (and a test pins the invariant).
# ``model_fields`` is insertion-ordered, which is the canonical display order
# used in the diff: ("ui", "actions", "sources", "shape").
_TEMPLATE_OWNED_REGIONS: tuple[str, ...] = tuple(TemplateLayer.model_fields)


def _resolve_template_owned(
    loaded: dict[str, Any] | None,
    compiled: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the template-owned region values from a loaded+compiled template.

    The authored canvas (``ui``) lives in the template's hand-authored
    sibling ``ripple_spec.json`` (``loaded["ripple_spec"]``), exactly like
    the install-time merge in ``service._merge_compile_into_ripple_spec``.
    The machinery regions (``actions``, ``sources``, ``shape``) come from the
    pure ``compile_template`` output. A region the template simply doesn't
    declare is returned as its empty default so reconcile can still report
    "this region would be cleared" rather than silently leaving stale data.

    Returns a dict containing ONLY keys from ``_TEMPLATE_OWNED_REGIONS`` that
    the template actually provides a value for. ``ui`` is deep-copied so the
    caller never aliases the loader's cached dict.
    """
    owned: dict[str, Any] = {}
    compiled = compiled or {}

    # ui — from the authored sibling spec.
    ripple_spec = loaded.get("ripple_spec") if isinstance(loaded, dict) else None
    if isinstance(ripple_spec, dict):
        template_ui = ripple_spec.get("ui")
        if isinstance(template_ui, dict) and template_ui:
            owned["ui"] = copy.deepcopy(template_ui)

    # actions / sources / shape — from the compile output.
    if "actions" in compiled:
        owned["actions"] = copy.deepcopy(compiled["actions"])
    if "sources" in compiled:
        owned["sources"] = copy.deepcopy(compiled["sources"])
    if "shape" in compiled:
        owned["shape"] = copy.deepcopy(compiled["shape"])

    return owned


def _build_reconciled_spec(
    existing_spec: dict[str, Any] | None,
    template_owned: dict[str, Any],
) -> dict[str, Any]:
    """Return a NEW rippleSpec: the instance's spec with template-owned
    regions overwritten from ``template_owned``.

    Instance-owned regions (``state`` and any other key the instance carries)
    are preserved verbatim. No input is mutated. This is intentionally a
    region-level overwrite, NOT the node-id-keyed ``merge_ripple_spec`` walk
    — reconcile owns whole regions, not individual UI nodes.

    Implemented via the typed ``RippleSpec.with_template_layer`` merge: it
    overlays exactly the template-owned regions ``template_owned`` provides
    and preserves every instance-owned + passthrough key on the existing spec.
    ``template_owned`` carries ONLY keys from ``_TEMPLATE_OWNED_REGIONS`` (built
    by ``_resolve_template_owned``), so promoting it to a ``TemplateLayer`` is
    safe even under the layer's ``extra="forbid"``. Falls back to the prior
    deepcopy-overlay if the existing spec cannot be promoted (corrupt spec),
    so reconcile never drops the template refresh.
    """
    spec = RippleSpec.from_flat_dict(existing_spec)
    if spec is not None:
        # Only the regions the template actually provides — an absent region is
        # NOT cleared (matches the prior "if region in template_owned" guard).
        layer = TemplateLayer.model_validate(
            {k: v for k, v in template_owned.items() if k in _TEMPLATE_OWNED_REGIONS}
        )
        return spec.with_template_layer(layer).to_flat_dict()

    # Fallback: existing spec unpromotable — preserve the historical behaviour.
    out: dict[str, Any] = copy.deepcopy(existing_spec) if isinstance(existing_spec, dict) else {}
    for region in _TEMPLATE_OWNED_REGIONS:
        if region in template_owned:
            out[region] = copy.deepcopy(template_owned[region])
    return out


def _diff_regions(
    existing_spec: dict[str, Any] | None,
    template_owned: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Compare each template-owned region's current value against the value
    reconcile would write. Returns ``(changed, unchanged)`` region-name lists.

    A region the template provides whose current pocket value differs (by
    value equality) is ``changed``; one that already matches is ``unchanged``.
    A region the template does NOT provide is omitted from both lists — there
    is nothing to reconcile for it. ``state`` is never considered here; it is
    instance-owned and reported separately as "preserved" by the caller.
    """
    existing = existing_spec if isinstance(existing_spec, dict) else {}
    changed: list[str] = []
    unchanged: list[str] = []
    for region in _TEMPLATE_OWNED_REGIONS:
        if region not in template_owned:
            continue
        if existing.get(region) == template_owned[region]:
            unchanged.append(region)
        else:
            changed.append(region)
    return changed, unchanged


async def _check_access(pocket_id: str, user_id: str | None, *, require_edit: bool) -> None:
    """Enforce read (preview) or edit (apply) access, or raise ``Forbidden``.

    Gated only when a ``user_id`` is supplied — an in-process caller (belt,
    automations) passing ``None`` is trusted, the same posture the
    internal-refresh reader uses. Runs against the live doc through the
    service's access helpers, so workspace-visibility / shared_with rules
    match every other pocket route.
    """
    if user_id is None:
        return
    allowed = (
        await pockets_service.has_edit_access(pocket_id, user_id)
        if require_edit
        else await pockets_service.is_member(pocket_id, user_id)
    )
    if not allowed:
        raise Forbidden(
            "reconcile.access_denied",
            "You do not have "
            + ("edit access to" if require_edit else "access to")
            + " this pocket.",
        )


async def _load_for_reconcile(
    pocket_id: str,
    workspace_id: str,
    *,
    user_id: str | None = None,
    require_edit: bool = False,
    templates_dir: Path | None = None,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Resolve everything reconcile needs, with clean errors.

    Returns ``(existing_spec, template_slug, template_owned)``.

    The access check runs BEFORE the template is resolved, so a caller who
    can't see the pocket gets ``Forbidden`` rather than leaking
    "this pocket has no template" / "the template is stale".

    Raises:
        NotFound: the pocket is missing or belongs to another workspace
            (tenant-scoped — same posture as ``get_pocket_ripple_spec``).
        Forbidden: ``user_id`` lacks the required (read / edit) access.
        ValidationError: the pocket has no ``template_slug`` (nothing to
            reconcile against), or the slug no longer resolves on disk
            (stale template — surfaced, not silently skipped, so the operator
            knows the source is gone before trusting a reconcile).
    """
    # Tenant-scoped read of BOTH the spec and the slug in one go, through the
    # service (reconcile never touches the Pocket Beanie model itself — the
    # cloud "funnel through service.py" boundary). ``None`` means missing OR
    # cross-tenant — both are NotFound (never a cross-tenant oracle).
    loaded_pocket = await pockets_service.get_pocket_spec_and_slug(workspace_id, pocket_id)
    if loaded_pocket is None:
        raise NotFound("pocket", pocket_id)
    # Access gate FIRST (before any template-existence signal leaks).
    await _check_access(pocket_id, user_id, require_edit=require_edit)
    existing_spec, slug = loaded_pocket
    if not slug:
        raise ValidationError(
            "reconcile.no_template",
            "This pocket was not installed from a template (no template_slug), "
            "so there is nothing to reconcile against.",
        )

    from pocketpaw.bundled_templates import load_template

    loaded = load_template(slug, templates_dir=templates_dir, strict=False)
    if loaded is None:
        raise ValidationError(
            "reconcile.template_unresolved",
            f"The pocket's template '{slug}' could not be loaded — it may have "
            "been removed or is malformed. Reconcile cannot run against a "
            "missing template.",
        )

    compiled = pockets_service._compile_template_to_runtime_dict(loaded)
    if compiled is None:
        raise ValidationError(
            "reconcile.template_unresolved",
            f"The pocket's template '{slug}' failed to compile. Reconcile "
            "cannot run against an invalid template.",
        )

    template_owned = _resolve_template_owned(loaded, compiled)
    return existing_spec, slug, template_owned


def _build_diff(
    *,
    pocket_id: str,
    template_slug: str,
    existing_spec: dict[str, Any] | None,
    template_owned: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the wire-shaped diff dict shared by preview and apply.

    Shape (stable contract for the REST + CLI adapters):

        {
          "pocket_id": "...",
          "template_slug": "applications-triage",
          "template_owned_regions": ["ui", "actions", "sources", "shape"],
          "changed_regions": ["ui"],          # template-owned + differs now
          "unchanged_regions": ["actions", "sources", "shape"],
          "preserved_regions": ["state"],     # instance-owned, never touched
          "has_changes": true
        }
    """
    changed, unchanged = _diff_regions(existing_spec, template_owned)
    return {
        "pocket_id": pocket_id,
        "template_slug": template_slug,
        "template_owned_regions": list(_TEMPLATE_OWNED_REGIONS),
        "changed_regions": changed,
        "unchanged_regions": unchanged,
        # ``state`` is the canonical instance-owned region; reconcile never
        # writes it, so it is always reported as preserved.
        "preserved_regions": ["state"],
        "has_changes": bool(changed),
    }


async def preview_reconcile(
    pocket_id: str,
    workspace_id: str,
    user_id: str | None = None,
    *,
    templates_dir: Path | None = None,
) -> dict[str, Any]:
    """Dry-run a reconcile: report what WOULD change, write NOTHING.

    Returns the diff dict described in :func:`_build_diff`. Pure read — no
    Beanie write, no event emission. ``user_id`` is accepted for adapter
    symmetry with :func:`apply_reconcile` but is not required for a preview
    (a read-only diff against the pocket the caller could already read).

    Raises ``NotFound`` (missing / cross-tenant pocket), ``Forbidden`` (caller
    lacks read access to a private pocket), or ``ValidationError`` (no
    template_slug / unresolvable template) — see :func:`_load_for_reconcile`.
    """
    existing_spec, slug, template_owned = await _load_for_reconcile(
        pocket_id,
        workspace_id,
        user_id=user_id,
        require_edit=False,
        templates_dir=templates_dir,
    )
    return _build_diff(
        pocket_id=pocket_id,
        template_slug=slug,
        existing_spec=existing_spec,
        template_owned=template_owned,
    )


async def apply_reconcile(
    pocket_id: str,
    workspace_id: str,
    user_id: str,
    *,
    templates_dir: Path | None = None,
) -> dict[str, Any]:
    """Apply a reconcile: re-write the template-owned regions, preserve the
    instance-owned regions, persist through the existing spec write path.

    Returns ``{"ok": True, "diff": <diff dict>, "pocket": <wire dict>}``.
    When the pocket already matches its template (nothing template-owned
    differs) the write is SKIPPED and ``{"ok": True, "skipped": True, ...}``
    is returned — reconcile is idempotent and never emits a no-op
    ``PocketUpdated``.

    The write goes through ``service.update(ripple_spec=...)``, so it runs the
    same normalize + catalog/action-wiring validation gates and emits the
    same ``PocketUpdated`` bus event as any other spec edit. Edit-access is
    enforced there (owner / shared_with / workspace-visible) and a
    non-permitted caller raises ``Forbidden``.

    Raises ``NotFound`` / ``ValidationError`` like :func:`preview_reconcile`,
    plus ``Forbidden`` if ``user_id`` lacks edit access to the pocket.
    """
    # Access gate (edit) runs inside _load_for_reconcile, BEFORE the
    # idempotent-skip below — so a no-op reconcile by a non-editor still fails
    # closed (``service.update`` enforces edit access too, but the skip path
    # never reaches it; without this a non-editor could probe "is this pocket
    # in sync?" against any pocket they can't edit).
    existing_spec, slug, template_owned = await _load_for_reconcile(
        pocket_id,
        workspace_id,
        user_id=user_id,
        require_edit=True,
        templates_dir=templates_dir,
    )

    diff = _build_diff(
        pocket_id=pocket_id,
        template_slug=slug,
        existing_spec=existing_spec,
        template_owned=template_owned,
    )

    if not diff["has_changes"]:
        # Idempotent: already in sync with the template. Skip the write so we
        # don't emit a no-op event or churn the updated_at timestamp.
        logger.info(
            "reconcile: pocket=%s slug=%r already matches template — skipping write",
            pocket_id,
            slug,
        )
        return {"ok": True, "skipped": True, "diff": diff}

    reconciled = _build_reconciled_spec(existing_spec, template_owned)
    # Persist via the canonical spec write path. We pass the rippleSpec only
    # (NOT template_slug) so ``update`` does a wholesale spec write of our
    # already-reconciled spec rather than re-running its own compile/merge —
    # the partition is decided HERE, not re-derived there.
    wire = await pockets_service.update(
        pocket_id,
        user_id,
        UpdatePocketRequest(ripple_spec=reconciled),
    )
    return {"ok": True, "skipped": False, "diff": diff, "pocket": wire}


__all__ = ["preview_reconcile", "apply_reconcile"]
