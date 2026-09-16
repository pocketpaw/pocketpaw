"""Cross-tenant plan & entitlement overrides — the operator's comp/penalty lever.

Created: 2026-09-16 (feat/platform-entitlements) — chunk 7 of the Paw Admin PRD.

Read is SUPPORT; write and clear are OPERATOR, same split as every other pair
in this package. ``workspace_id`` is an explicit path parameter, the same
inversion every route under ``/platform`` uses.

**PRD errata C2, resolved here, not worked around.** Decision 7 originally
proposed overrides for ``monthly_credit_allotment`` and ``extra_features``.
Both are read by their enforcement points straight off the plan catalog
(``billing/service.py``'s renewal grant; both ``require_plan_feature``
implementations) and never through ``resolve_entitlements`` — an override on
either would be stored, shown on the console, and change nothing. Rather than
ship a control that lies about its own effect, this module does not expose
either field, and ``max_call_seconds_per_day`` — resolver-enforced but missing
from Decision 7's original list — is added in their place. See
``cloud.models.workspace.WorkspaceOverrides`` for the full accounting.

**Read-back, not just merge.** The read route returns the plan CATALOG's
values, the OVERRIDE-RESOLVED values, and the raw override document side by
side, so an operator can tell "the plan gives this" from "an override changed
it to that" — a merged number alone cannot answer which fields are actually
overridden versus coincidentally equal to the catalog.

**Idempotency key.** ``OverridesWriteIn``/``OverridesClearIn`` accept an
optional ``idempotency_key`` per the screen spine (6.6: minted once per form
open, so a retried submit cannot double-apply). Nothing under
``cloud/platform/`` has idempotency infrastructure to enforce it against
(confirmed by grep — zero existing usage), and building a dedup store is out
of scope for this chunk. The field is accepted so the console can send it
without a 422, and is deliberately NOT persisted or checked — a write to this
endpoint is idempotent (a PUT that sets the same fields twice yields the same
document either way), so the absence of enforcement does not double-apply
anything, and unenforced-but-safe was preferred over inventing dedup storage.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
from pocketpaw_ee.cloud._core.platform_deps import require_platform
from pocketpaw_ee.cloud.entitlements.domain import Entitlements
from pocketpaw_ee.cloud.entitlements.service import entitlements_from_plan, resolve_entitlements
from pocketpaw_ee.cloud.models.user import User
from pocketpaw_ee.cloud.models.workspace import WorkspaceOverrides
from pocketpaw_ee.cloud.platform import audit
from pocketpaw_ee.cloud.workspace import service as workspace_service

router = APIRouter(prefix="/workspaces", tags=["platform"])

_OverrideValue = int | Literal["uncapped"] | None


class EntitlementCeilingsOut(BaseModel):
    """The seven fields an override can actually reach.

    Deliberately excludes ``monthly_credit_allotment`` and ``features`` — see
    the module docstring for why an override on either is inert.
    """

    monthly_ceiling: int | None
    max_seats: int | None
    max_pockets: int | None
    max_connectors: int | None
    max_call_seconds_per_day: int | None
    max_storage_bytes: int | None
    included_sites: int | None


class OverridesOut(BaseModel):
    """A workspace's raw override document, or absent if none is set/active."""

    monthly_ceiling: _OverrideValue
    max_seats: _OverrideValue
    max_pockets: _OverrideValue
    max_connectors: _OverrideValue
    max_call_seconds_per_day: _OverrideValue
    max_storage_bytes: _OverrideValue
    included_sites: _OverrideValue
    expires_at: str | None = None


class EntitlementsOut(BaseModel):
    """Catalog vs. resolved vs. raw override, so nothing is only a merge."""

    workspace_id: str
    plan: str
    # What the plan catalog alone gives — no override applied.
    catalog: EntitlementCeilingsOut
    # What resolve_entitlements actually returns — override applied, and thus
    # what every enforcement path in the codebase currently sees.
    resolved: EntitlementCeilingsOut
    # The raw override document. None means no override is in effect (either
    # never set, or set and now expired) — distinct from a document whose
    # fields are all null, which is why this is not derived from `resolved`.
    overrides: OverridesOut | None


class OverridesWriteIn(BaseModel):
    """PUT body — replaces the workspace's entire override set.

    PUT rather than PATCH: a field left off this body is not overridden,
    exactly as if it were sent as ``null``. That is a deliberate simplification
    of strict PUT-requires-every-field semantics — every field here already
    defaults to "not overridden", so the two phrasings mean the same thing.
    """

    monthly_ceiling: _OverrideValue = None
    max_seats: _OverrideValue = None
    max_pockets: _OverrideValue = None
    max_connectors: _OverrideValue = None
    max_call_seconds_per_day: _OverrideValue = None
    max_storage_bytes: _OverrideValue = None
    included_sites: _OverrideValue = None
    expires_at: datetime | None = None
    # Required, non-empty (checked in the handler body, not a field_validator,
    # so a test can still construct an invalid body and drive it through the
    # route the way the console does). Screen spine 6.5: free text, no canned
    # reasons, because a dropdown produces a log that says nothing.
    reason: str = ""
    # Accepted, not enforced — see the module docstring.
    idempotency_key: str | None = None


class OverridesClearIn(BaseModel):
    """DELETE body — clearing overrides is still a write and still needs a reason."""

    reason: str = ""
    idempotency_key: str | None = None


def _ceilings(ent: Entitlements) -> EntitlementCeilingsOut:
    return EntitlementCeilingsOut(
        monthly_ceiling=ent.monthly_ceiling,
        max_seats=ent.max_seats,
        max_pockets=ent.max_pockets,
        max_connectors=ent.max_connectors,
        max_call_seconds_per_day=ent.max_call_seconds_per_day,
        max_storage_bytes=ent.max_storage_bytes,
        included_sites=ent.included_sites,
    )


def _overrides_out(overrides: WorkspaceOverrides | None) -> OverridesOut | None:
    """Render the raw override doc, respecting whole-set expiry.

    An expired override set reads back as ``None`` — the same thing
    ``resolve_entitlements`` treats it as — so the console never shows a grant
    that is no longer in effect.
    """
    if overrides is None:
        return None
    if overrides.expires_at is not None:
        # Mongo round-trips a naive datetime even though it was written as
        # UTC-aware — treat naive as UTC rather than let the comparison raise.
        # Mirrors ``entitlements.service._apply_overrides``.
        expires_at = overrides.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= datetime.now(UTC):
            return None
    return OverridesOut(
        monthly_ceiling=overrides.monthly_ceiling,
        max_seats=overrides.max_seats,
        max_pockets=overrides.max_pockets,
        max_connectors=overrides.max_connectors,
        max_call_seconds_per_day=overrides.max_call_seconds_per_day,
        max_storage_bytes=overrides.max_storage_bytes,
        included_sites=overrides.included_sites,
        expires_at=overrides.expires_at.isoformat() if overrides.expires_at else None,
    )


def _override_audit_dict(overrides: WorkspaceOverrides | None) -> dict:
    """Render an override doc for an audit row's before/after — {} if absent/expired."""
    out = _overrides_out(overrides)
    return out.model_dump(mode="json") if out is not None else {}


async def _entitlements_out(workspace_id: str) -> EntitlementsOut:
    plan_key, overrides = await workspace_service.get_workspace_plan_and_overrides(workspace_id)
    if plan_key is None:
        raise NotFound("workspace", workspace_id)

    catalog = entitlements_from_plan(workspace_id, plan_key)
    resolved = await resolve_entitlements(workspace_id)

    return EntitlementsOut(
        workspace_id=workspace_id,
        plan=plan_key,
        catalog=_ceilings(catalog),
        resolved=_ceilings(resolved),
        overrides=_overrides_out(overrides),
    )


@router.get("/{workspace_id}/entitlements", response_model=EntitlementsOut)
async def get_entitlements(
    workspace_id: str,
    request: Request,
    operator: Annotated[User, Depends(require_platform("platform.entitlements.read"))],
) -> EntitlementsOut:
    """A tenant's plan, its catalog ceilings, its resolved ceilings, and any override."""
    out = await _entitlements_out(workspace_id)

    await audit.record_read(
        operator=operator,
        action="platform.entitlements.read",
        query=f"workspace={workspace_id}",
        target_type="workspace_entitlements",
        target_workspace=workspace_id,
        request=request,
    )

    return out


@router.put("/{workspace_id}/entitlements/overrides", response_model=EntitlementsOut)
async def set_overrides(
    workspace_id: str,
    body: OverridesWriteIn,
    request: Request,
    operator: Annotated[User, Depends(require_platform("platform.entitlements.write"))],
) -> EntitlementsOut:
    """Replace a tenant's entitlement overrides.

    ``reason`` is checked here rather than in a field_validator so a test can
    still build an invalid body and drive it through this function directly,
    matching the direct-handler-call pattern the rest of this package's tests
    use.
    """
    if not body.reason.strip():
        raise ValidationError("platform.entitlements.reason_required", "reason is required")

    before_plan, before_overrides = await workspace_service.get_workspace_plan_and_overrides(
        workspace_id
    )
    if before_plan is None:
        raise NotFound("workspace", workspace_id)

    new_overrides = WorkspaceOverrides(
        monthly_ceiling=body.monthly_ceiling,
        max_seats=body.max_seats,
        max_pockets=body.max_pockets,
        max_connectors=body.max_connectors,
        max_call_seconds_per_day=body.max_call_seconds_per_day,
        max_storage_bytes=body.max_storage_bytes,
        included_sites=body.included_sites,
        expires_at=body.expires_at,
    )

    event = await audit.begin(
        operator=operator,
        action="platform.entitlements.write",
        reason=body.reason,
        target_type="workspace_entitlements",
        target_workspace=workspace_id,
        before=_override_audit_dict(before_overrides),
        request=request,
    )

    out: EntitlementsOut | None = None
    ok = False
    try:
        await workspace_service.platform_set_workspace_overrides(workspace_id, new_overrides)
        out = await _entitlements_out(workspace_id)
        ok = True
    finally:
        await audit.settle(
            event,
            ok=ok,
            after=(_override_audit_dict(new_overrides) if ok else {}),
        )

    assert out is not None  # ok=True path always sets it before the finally runs
    return out


@router.delete("/{workspace_id}/entitlements/overrides", response_model=EntitlementsOut)
async def clear_overrides(
    workspace_id: str,
    body: OverridesClearIn,
    request: Request,
    operator: Annotated[User, Depends(require_platform("platform.entitlements.write"))],
) -> EntitlementsOut:
    """Clear a tenant's entitlement overrides back to whatever the plan gives."""
    if not body.reason.strip():
        raise ValidationError("platform.entitlements.reason_required", "reason is required")

    before_plan, before_overrides = await workspace_service.get_workspace_plan_and_overrides(
        workspace_id
    )
    if before_plan is None:
        raise NotFound("workspace", workspace_id)

    event = await audit.begin(
        operator=operator,
        action="platform.entitlements.write",
        reason=body.reason,
        target_type="workspace_entitlements",
        target_workspace=workspace_id,
        before=_override_audit_dict(before_overrides),
        request=request,
    )

    out: EntitlementsOut | None = None
    ok = False
    try:
        await workspace_service.platform_set_workspace_overrides(workspace_id, None)
        out = await _entitlements_out(workspace_id)
        ok = True
    finally:
        await audit.settle(event, ok=ok, after={})

    assert out is not None  # ok=True path always sets it before the finally runs
    return out


__all__ = ["router"]
