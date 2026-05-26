# ee/pocketpaw_ee/cloud/foresight/scenarios.py
# Created: 2026-05-26 (feat/foresight-v10-scenario-editor-backend) — RFC 08
# v1.0 wave 3. Workspace-scoped custom scenario CRUD service.
#
# Why a separate module from ``service.py``: ``service.py`` already
# owns the engine-driven run + backtest + insights paths; the
# custom-scenario CRUD is its own self-contained surface (Beanie
# writes, YAML validation, event emission) and benefits from a
# dedicated module that the run path can call into via a single
# function (``load_workspace_scenario`` for the
# ``custom_scenario_id`` integration). The module stays inside the
# cloud entity boundary — only this file imports
# :class:`pocketpaw_ee.cloud.models.foresight_workspace_scenario.ForesightWorkspaceScenario`,
# satisfying the import-linter contract (cloud rule #2 — service IS
# the repository for the workspace-scenarios collection).
#
# Public API:
#   - ``create_custom_scenario(ctx, body)`` — POST
#   - ``list_custom_scenarios(ctx, *, sub_type, limit, offset)`` — GET list
#   - ``get_custom_scenario(ctx, scenario_id)`` — GET detail
#   - ``update_custom_scenario(ctx, scenario_id, body)`` — PUT (full replace)
#   - ``delete_custom_scenario(ctx, scenario_id)`` — DELETE
#   - ``load_workspace_scenario(workspace_id, scenario_id)`` — read helper
#     used by ``service.create_scenario_run`` when ``custom_scenario_id``
#     is present on the run request.
#
# YAML validation strategy: the engine's
# ``ScenarioConfig.from_yaml`` already parses + validates the YAML
# grammar (sub_type, n_ticks bounds, personas non-empty, tier_mix
# triple sum). We lazy-import that loader inside the validation helper
# so the cloud module never statically depends on the engine — the
# import-linter contract (cloud → engine forbidden) is honoured the
# same way ``service.py`` honours it for the run path.
#
# v1.0 caps (RFC 08 §10):
#   - Persona count ≤ 100 (operator-facing soft cap; the engine itself
#     can run larger but the per-run cost / wall-clock budget grows
#     linearly with the cohort).
#   - Tick count ≤ 100.
#   - Tier-mix triple sums to 1.0 within ±0.001 tolerance.
#   - YAML body ≤ 64 KB (enforced at the DTO layer).

from __future__ import annotations

import logging
from typing import Any

from beanie import PydanticObjectId

from pocketpaw_ee.cloud._core.context import RequestContext
from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound, ValidationError
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import (
    ForesightCustomScenarioCreated,
    ForesightCustomScenarioDeleted,
    ForesightCustomScenarioUpdated,
)
from pocketpaw_ee.cloud._core.time import iso_utc
from pocketpaw_ee.cloud.foresight.domain import (
    CustomScenario,
    CustomScenarioParsedMeta,
)
from pocketpaw_ee.cloud.foresight.dto import (
    CreateCustomScenarioRequest,
    CustomScenarioListItem,
    CustomScenarioListResponse,
    CustomScenarioParsedMetaDto,
    CustomScenarioResponse,
)
from pocketpaw_ee.cloud.models.foresight_workspace_scenario import (
    ForesightWorkspaceScenario as _ForesightWorkspaceScenarioDoc,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# v1.0 caps (RFC 08 §10) — operator-facing limits enforced at the cloud
# layer so a malformed YAML can't drag the engine into a 100k-persona
# run by accident. The engine itself imposes no cap; the budgeting +
# UX story lives here.
# ---------------------------------------------------------------------------
MAX_PERSONAS: int = 100
MAX_TICKS: int = 100
TIER_MIX_TOLERANCE: float = 0.001
DEFAULT_TIER_MIX: dict[str, float] = {"premium": 0.05, "mid": 0.15, "tail": 0.80}


# ---------------------------------------------------------------------------
# Tenancy helpers
# ---------------------------------------------------------------------------


def _require_workspace(ctx: RequestContext) -> str:
    """Custom-scenario CRUD always operates within a workspace; reject
    workspace-less callers with a clean 403 rather than letting them
    insert untenanted docs."""
    if not ctx.workspace_id:
        raise Forbidden(
            "foresight.no_workspace",
            "Active workspace required for foresight operations",
        )
    return ctx.workspace_id


async def _fetch_in_workspace(
    workspace_id: str, scenario_id: str
) -> _ForesightWorkspaceScenarioDoc:
    """Fetch a custom scenario scoped to the caller's workspace; raise
    NotFound for malformed ids, missing docs, or cross-tenant ids — same
    collapsing rule the other foresight endpoints use so existence
    isn't leakable across tenants."""
    try:
        oid = PydanticObjectId(scenario_id)
    except Exception:
        raise NotFound("foresight_custom_scenario", scenario_id) from None
    doc = await _ForesightWorkspaceScenarioDoc.find_one({"_id": oid, "workspace_id": workspace_id})
    if doc is None:
        raise NotFound("foresight_custom_scenario", scenario_id)
    return doc


# ---------------------------------------------------------------------------
# YAML validation + parse-meta extraction.
#
# We delegate grammar validation to the engine's existing
# ``ScenarioConfig.from_yaml`` loader (lazy-imported) so the cloud
# surface stays in lockstep with the engine's notion of "valid" without
# carrying a parallel parser. The extra cloud-layer rules (persona /
# tick caps, sub_type match, tier-mix tolerance) layer on top of the
# engine's grammar check.
# ---------------------------------------------------------------------------


def _parse_yaml_safely(yaml_body: str) -> dict[str, Any]:
    """Run ``yaml.safe_load`` and surface a 422 with a stable code on
    malformed input. Pure-stdlib path (no engine import) so the
    parse-meta extraction can run on the list endpoint without
    pulling the engine extras into the cloud module."""
    import yaml  # type: ignore[import-untyped]  # noqa: PLC0415

    try:
        data = yaml.safe_load(yaml_body)
    except yaml.YAMLError as exc:
        raise ValidationError(
            "foresight.invalid_yaml",
            f"YAML parse error: {exc}",
        ) from exc
    if not isinstance(data, dict):
        raise ValidationError(
            "foresight.invalid_yaml",
            "YAML root must be a mapping with 'name' / 'sub_type' / 'personas'",
        )
    return data


def _extract_parsed_meta(yaml_data: dict[str, Any]) -> CustomScenarioParsedMeta:
    """Build a :class:`CustomScenarioParsedMeta` from a parsed YAML dict.

    Used both by the write path (to stamp ``parsed_meta`` on the doc)
    and by the response mappers (to serialize the stamped meta back
    onto the wire). Defaults match the engine's
    ``ScenarioConfig.from_yaml`` defaults so a partial YAML produces
    the same meta the engine would compute.
    """
    personas = yaml_data.get("personas") or []
    num_personas = len(personas) if isinstance(personas, list) else 0
    n_ticks = int(yaml_data.get("n_ticks") or 0)

    tier_mix_block = yaml_data.get("tier_mix") or {}
    tier_mix: dict[str, float] = {}
    if isinstance(tier_mix_block, dict):
        for tier_name in ("premium", "mid", "tail"):
            if tier_name in tier_mix_block:
                try:
                    tier_mix[tier_name] = float(tier_mix_block[tier_name])
                except (TypeError, ValueError):
                    continue
    if not tier_mix:
        tier_mix = dict(DEFAULT_TIER_MIX)

    raw_seed = yaml_data.get("precedent_seed")
    precedent_seed: str | None = str(raw_seed) if raw_seed not in (None, "") else None

    return CustomScenarioParsedMeta(
        num_personas=num_personas,
        num_ticks=n_ticks,
        tier_mix=tier_mix,
        precedent_seed=precedent_seed,
    )


def _validate_and_parse_yaml(
    yaml_body: str,
    requested_sub_type: str,
) -> CustomScenarioParsedMeta:
    """Validate the YAML against engine grammar + cloud v1.0 caps and
    return the denormalized parsed meta the doc stores.

    Validation order:
      1. ``yaml.safe_load`` — surfaces structural errors as
         ``foresight.invalid_yaml`` (422).
      2. Engine ``ScenarioConfig.from_yaml`` — grammar validation
         (sub_type in supported set, n_ticks ≥ 1, personas non-empty,
         tier_mix triple sums to 1.0 per the engine's
         :class:`pocketpaw_ee.foresight.llm.tier_pool.TierMix` ctor).
         Engine errors collapse to ``foresight.invalid_scenario`` (422).
      3. Request ``sub_type`` matches YAML ``sub_type`` — different
         vocab on the two sides surfaces as
         ``foresight.sub_type_mismatch`` (422) instead of silently
         saving the YAML's sub_type and ignoring the form field.
      4. v1.0 caps: persona count ≤ 100, tick count ≤ 100. These
         layer on top of the engine grammar (which has no cap of its
         own) so the operator can't accidentally schedule a 10k-persona
         run.
      5. Tier-mix triple sums to 1.0 within ±0.001 — the engine's
         own ``TierMix`` ctor enforces this strictly but uses an
         exception type the engine doesn't expose by name; we
         re-validate here so we can produce a stable error code the
         UI maps to.

    Returns the parsed meta on success; raises ``ValidationError`` on
    any failure.
    """
    yaml_data = _parse_yaml_safely(yaml_body)

    # Engine-side grammar validation. The loader takes a path; we write
    # to a tmp file so we can call the existing class method without
    # forking it. Tmp file lives for the duration of the call only.
    import tempfile  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    yaml_sub_type = str(yaml_data.get("sub_type", "decision_forecast"))

    # Sub-type alignment (rule 3) — surface BEFORE engine load so the
    # operator-facing message names the mismatch directly instead of
    # the engine's generic "unsupported sub_type" branch.
    if yaml_sub_type != requested_sub_type:
        raise ValidationError(
            "foresight.sub_type_mismatch",
            (
                f"Request sub_type {requested_sub_type!r} does not match "
                f"YAML sub_type {yaml_sub_type!r}; resave with matching values"
            ),
        )

    # Grammar validation via engine loader (lazy-import; cloud → engine
    # is forbidden statically but lazy imports inside functions are
    # allowed and used elsewhere in this entity).
    try:
        from pocketpaw_ee.foresight.scenarios.runner import (  # noqa: PLC0415
            ScenarioConfig,
        )
    except ImportError as exc:  # pragma: no cover — engine extra missing
        raise ValidationError(
            "foresight.engine_unavailable",
            "Foresight engine extras not installed",
        ) from exc

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".yaml",
        delete=False,
        encoding="utf-8",
    ) as fh:
        fh.write(yaml_body)
        tmp_path = Path(fh.name)
    try:
        try:
            ScenarioConfig.from_yaml(tmp_path)
        except KeyError as exc:
            # ``ScenarioConfig.from_yaml`` raises KeyError("name") when
            # the YAML omits a required field; surface as a clean 422.
            raise ValidationError(
                "foresight.invalid_scenario",
                f"Missing required field: {exc}",
            ) from exc
        except (TypeError, ValueError, NotImplementedError) as exc:
            raise ValidationError(
                "foresight.invalid_scenario",
                str(exc),
            ) from exc
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            # tmp cleanup is best-effort; the OS sweeps tmpdir on its own.
            pass

    parsed_meta = _extract_parsed_meta(yaml_data)

    # v1.0 cloud-layer caps (rule 4).
    if parsed_meta.num_personas > MAX_PERSONAS:
        raise ValidationError(
            "foresight.invalid_scenario",
            f"Persona count {parsed_meta.num_personas} exceeds v1.0 cap of {MAX_PERSONAS}",
        )
    if parsed_meta.num_ticks > MAX_TICKS:
        raise ValidationError(
            "foresight.invalid_scenario",
            f"Tick count {parsed_meta.num_ticks} exceeds v1.0 cap of {MAX_TICKS}",
        )

    # Tier-mix tolerance (rule 5). The engine's TierMix ctor already
    # enforces strict sum=1.0 inside the loader above, but we keep this
    # check too so the cloud-layer error code is stable + the
    # tolerance band is explicitly documented at the boundary.
    if parsed_meta.tier_mix:
        total = sum(parsed_meta.tier_mix.get(k, 0.0) for k in ("premium", "mid", "tail"))
        if abs(total - 1.0) > TIER_MIX_TOLERANCE:
            raise ValidationError(
                "foresight.invalid_scenario",
                (f"tier_mix must sum to 1.0 (±{TIER_MIX_TOLERANCE}); got {total:.4f}"),
            )

    return parsed_meta


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------


def _parsed_meta_from_doc(doc: _ForesightWorkspaceScenarioDoc) -> CustomScenarioParsedMeta:
    """Pull the denormalized ``parsed_meta`` block off the doc into a
    domain value object. Defaults to zeros + the captain-locked tier
    mix when the doc was written before the parsed-meta column existed
    (a future migration may backfill; v1.0 always stamps the field at
    write time)."""
    meta_dict = dict(doc.parsed_meta or {})
    tier_mix = meta_dict.get("tier_mix") or dict(DEFAULT_TIER_MIX)
    if not isinstance(tier_mix, dict):
        tier_mix = dict(DEFAULT_TIER_MIX)
    return CustomScenarioParsedMeta(
        num_personas=int(meta_dict.get("num_personas", 0)),
        num_ticks=int(meta_dict.get("num_ticks", 0)),
        tier_mix={k: float(v) for k, v in tier_mix.items()},
        precedent_seed=meta_dict.get("precedent_seed"),
    )


def _to_domain(doc: _ForesightWorkspaceScenarioDoc) -> CustomScenario:
    return CustomScenario(
        id=str(doc.id),
        workspace_id=doc.workspace_id,
        name=doc.name,
        sub_type=doc.sub_type,
        description=doc.description or "",
        author=doc.author or "",
        created_at=doc.createdAt,
        updated_at=doc.updatedAt,
        yaml_body=doc.yaml_body or "",
        parsed_meta=_parsed_meta_from_doc(doc),
    )


def _parsed_meta_to_dto(meta: CustomScenarioParsedMeta) -> CustomScenarioParsedMetaDto:
    return CustomScenarioParsedMetaDto(
        num_personas=meta.num_personas,
        num_ticks=meta.num_ticks,
        tier_mix=dict(meta.tier_mix),
        precedent_seed=meta.precedent_seed,
    )


def _to_response(scenario: CustomScenario) -> CustomScenarioResponse:
    return CustomScenarioResponse(
        id=scenario.id,
        workspace_id=scenario.workspace_id,
        name=scenario.name,
        sub_type=scenario.sub_type,  # type: ignore[arg-type]
        description=scenario.description,
        author=scenario.author,
        created_at=iso_utc(scenario.created_at) or "",
        updated_at=iso_utc(scenario.updated_at) or "",
        yaml_body=scenario.yaml_body,
        parsed_meta=_parsed_meta_to_dto(scenario.parsed_meta),
    )


def _to_list_item(scenario: CustomScenario) -> CustomScenarioListItem:
    return CustomScenarioListItem(
        id=scenario.id,
        name=scenario.name,
        sub_type=scenario.sub_type,  # type: ignore[arg-type]
        description=scenario.description,
        author=scenario.author,
        num_personas=scenario.parsed_meta.num_personas,
        num_ticks=scenario.parsed_meta.num_ticks,
        updated_at=iso_utc(scenario.updated_at) or "",
    )


# ---------------------------------------------------------------------------
# Public service API
# ---------------------------------------------------------------------------


async def create_custom_scenario(
    ctx: RequestContext, body: CreateCustomScenarioRequest
) -> CustomScenarioResponse:
    """Persist a new custom scenario and emit ``foresight.custom_scenario.created``.

    Validation flow:
      1. DTO field bounds (FastAPI parses; we re-parse via
         ``model_validate`` per cloud rule #6 for internal callers).
      2. YAML grammar + cloud v1.0 caps (persona/tick/tier-mix) via
         ``_validate_and_parse_yaml`` — any failure surfaces as a clean
         422 with a stable code the UI maps to.
      3. Doc insert; the TimestampedDocument hook stamps
         ``createdAt`` / ``updatedAt`` automatically.
      4. Emit ``ForesightCustomScenarioCreated`` so the Scenarios panel
         refreshes without polling.
    """
    body = CreateCustomScenarioRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)

    parsed_meta = _validate_and_parse_yaml(body.yaml_body, body.sub_type)

    doc = _ForesightWorkspaceScenarioDoc(
        workspace_id=workspace_id,
        name=body.name,
        sub_type=body.sub_type,
        description=body.description,
        author=ctx.user_id or "",
        yaml_body=body.yaml_body,
        parsed_meta={
            "num_personas": parsed_meta.num_personas,
            "num_ticks": parsed_meta.num_ticks,
            "tier_mix": dict(parsed_meta.tier_mix),
            "precedent_seed": parsed_meta.precedent_seed,
        },
    )
    await doc.insert()

    response = _to_response(_to_domain(doc))
    await emit(ForesightCustomScenarioCreated(data=response.model_dump()))
    return response


async def list_custom_scenarios(
    ctx: RequestContext,
    *,
    sub_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> CustomScenarioListResponse:
    """List custom scenarios in the caller's workspace, most-recently-edited first.

    Pagination:
      - ``limit`` clamped at 100 (router enforces; service re-clamps
        defensively for non-HTTP callers).
      - ``offset`` >= 0.
      - ``has_more`` derived as ``offset + len(items) < total``.

    Filter:
      - ``sub_type`` optional; when provided, the query narrows to that
        sub-type. The index ``(workspace_id, sub_type)`` keeps the
        narrow query cheap.

    Tenancy: tenant filter on every read (cloud rule #7) — the
    ``workspace_id`` clause is the leading key of the list index.
    """
    workspace_id = _require_workspace(ctx)
    if limit < 1:
        raise ValidationError("foresight.invalid_limit", "limit must be >= 1")
    if limit > 100:
        limit = 100
    if offset < 0:
        raise ValidationError("foresight.invalid_offset", "offset must be >= 0")

    query: dict[str, Any] = {"workspace_id": workspace_id}
    if sub_type:
        query["sub_type"] = sub_type

    total = await _ForesightWorkspaceScenarioDoc.find(query).count()
    docs = (
        await _ForesightWorkspaceScenarioDoc.find(query)
        .sort([("updatedAt", -1), ("_id", -1)])  # type: ignore[list-item]
        .skip(offset)
        .limit(limit)
        .to_list()
    )
    items = [_to_list_item(_to_domain(doc)) for doc in docs]
    return CustomScenarioListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(items) < total,
    )


async def get_custom_scenario(ctx: RequestContext, scenario_id: str) -> CustomScenarioResponse:
    """Fetch one custom scenario by id, scoped to the caller's workspace.

    Returns 404 (``foresight_custom_scenario.not_found``) for unknown,
    malformed, or cross-tenant ids — same collapsing rule the other
    foresight endpoints use so existence isn't leakable.
    """
    workspace_id = _require_workspace(ctx)
    doc = await _fetch_in_workspace(workspace_id, scenario_id)
    # no-event: read-only path; emit only on writes (cloud rule #9).
    return _to_response(_to_domain(doc))


async def update_custom_scenario(
    ctx: RequestContext,
    scenario_id: str,
    body: CreateCustomScenarioRequest,
) -> CustomScenarioResponse:
    """Full-replace the custom scenario fields. Validation is identical
    to the create path; on success the doc's ``updatedAt`` bumps via
    the TimestampedDocument hook and the
    ``foresight.custom_scenario.updated`` event fires.

    Author / workspace tenancy stay pinned to the original doc — the
    edit doesn't reassign the author column to the editor (the
    audit log is the source of truth for who edited).
    """
    body = CreateCustomScenarioRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)
    doc = await _fetch_in_workspace(workspace_id, scenario_id)

    parsed_meta = _validate_and_parse_yaml(body.yaml_body, body.sub_type)

    doc.name = body.name
    doc.sub_type = body.sub_type  # type: ignore[assignment]
    doc.description = body.description
    doc.yaml_body = body.yaml_body
    doc.parsed_meta = {
        "num_personas": parsed_meta.num_personas,
        "num_ticks": parsed_meta.num_ticks,
        "tier_mix": dict(parsed_meta.tier_mix),
        "precedent_seed": parsed_meta.precedent_seed,
    }
    await doc.save()

    response = _to_response(_to_domain(doc))
    await emit(ForesightCustomScenarioUpdated(data=response.model_dump()))
    return response


async def delete_custom_scenario(ctx: RequestContext, scenario_id: str) -> None:
    """Remove the custom scenario row and emit
    ``foresight.custom_scenario.deleted``. Idempotency note: a second
    call against the same id returns 404 — we never silently no-op a
    delete against an unknown doc.
    """
    workspace_id = _require_workspace(ctx)
    doc = await _fetch_in_workspace(workspace_id, scenario_id)
    # Snapshot the response payload BEFORE delete so listeners see the
    # full pre-delete shape (id + sub_type + author) instead of a bare
    # id reference; the doc is gone after ``delete``.
    response = _to_response(_to_domain(doc))
    await doc.delete()
    await emit(ForesightCustomScenarioDeleted(data=response.model_dump()))
    return None


# ---------------------------------------------------------------------------
# Run-integration helper — used by ``service.create_scenario_run`` when
# the request carries ``custom_scenario_id``. Kept here (rather than in
# ``service.py``) so all Beanie reads of the workspace-scenarios
# collection live in one module per the import-linter contract.
# ---------------------------------------------------------------------------


async def load_workspace_scenario(workspace_id: str, scenario_id: str) -> CustomScenario:
    """Load one custom scenario by id, scoped to a workspace, returning
    the frozen domain value object the run path can read fields off.

    Raises ``ValidationError("foresight.custom_scenario_not_found")`` on
    unknown / malformed / cross-tenant ids — different from the GET
    endpoint's 404 because the run path treats this as a 422 (the run
    body is malformed) rather than "the scenario doesn't exist".
    """
    try:
        oid = PydanticObjectId(scenario_id)
    except Exception:
        raise ValidationError(
            "foresight.custom_scenario_not_found",
            f"Custom scenario {scenario_id!r} not found or cross-tenant",
        ) from None
    doc = await _ForesightWorkspaceScenarioDoc.find_one({"_id": oid, "workspace_id": workspace_id})
    if doc is None:
        raise ValidationError(
            "foresight.custom_scenario_not_found",
            f"Custom scenario {scenario_id!r} not found or cross-tenant",
        )
    return _to_domain(doc)


__all__ = [
    "DEFAULT_TIER_MIX",
    "MAX_PERSONAS",
    "MAX_TICKS",
    "TIER_MIX_TOLERANCE",
    "create_custom_scenario",
    "delete_custom_scenario",
    "get_custom_scenario",
    "list_custom_scenarios",
    "load_workspace_scenario",
    "update_custom_scenario",
]
