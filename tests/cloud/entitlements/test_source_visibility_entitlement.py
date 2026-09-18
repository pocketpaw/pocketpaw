# tests/cloud/entitlements/test_source_visibility_entitlement.py — proves the
# WORKSPACE capability ``Entitlements.site_source_visible`` ("may this account
# read the source code of the sites it owns") resolves, fails closed, and is
# overridable end to end.
#
# Four properties, in the order a reviewer should check them:
#   (a) every paid rung resolves it True and ``free`` resolves it False;
#   (b) a retired or unknown plan key resolves False — it must not be handed a
#       paid grant on the way through the free-tier fallback;
#   (c) a platform override flips it EITHER way, and an expired override set
#       flips nothing;
#   (d) the answer reaches the ``GET /entitlements`` wire and the platform
#       console's override surface, so a client can hide the source view and
#       name the reason instead of being refused.
#
# DETERMINISTIC AND DB-FREE. Every test drives ``resolve_entitlements`` with
# ``get_workspace_plan`` / ``get_workspace_overrides`` monkeypatched, which is
# the pattern the whole tree already uses on this resolver. It goes through the
# public resolver rather than ``_apply_overrides`` on purpose: the property worth
# pinning is that the SINGLE choke point every enforcement path calls applies the
# override, not that a private helper can.
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from pocketpaw_ee.cloud.entitlements import service as entitlements
from pocketpaw_ee.cloud.entitlements.domain import Entitlements
from pocketpaw_ee.cloud.entitlements.dto import entitlements_to_dto
from pocketpaw_ee.cloud.models.workspace import WorkspaceOverrides
from pocketpaw_ee.cloud.platform import entitlements as platform_routes

pytestmark = pytest.mark.asyncio

WS = "ws_source_visibility_test"

PAID_PLANS = ["go", "pro", "pro_max", "enterprise"]

# Retired SITE-plan keys that still sit in stored documents, plus plain typos and
# two keys from the pre-consumer-ladder rekey. None of them is a workspace plan
# the catalog carries, so each lands on the unknown-key path and must come back
# withheld.
STALE_PLANS = ["studio", "agency", "legacy_gold_tier", "business", "team"]


@pytest.fixture
def patch_workspace(monkeypatch: pytest.MonkeyPatch):
    """Drive the resolver with a fixed plan string and a fixed override doc."""

    def _patch(plan: str | None, overrides: WorkspaceOverrides | None = None) -> None:
        import pocketpaw_ee.cloud.workspace.service as ws_svc

        monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value=plan))
        monkeypatch.setattr(ws_svc, "get_workspace_overrides", AsyncMock(return_value=overrides))

    return _patch


# ---------------------------------------------------------------------------
# (a) The grant itself — paid yes, free no.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("plan", PAID_PLANS)
async def test_a_paid_workspace_may_read_its_site_source(patch_workspace, plan):
    """Every paid rung carries source visibility."""
    patch_workspace(plan)
    ent = await entitlements.resolve_entitlements(WS)
    assert ent.plan == plan
    assert ent.site_source_visible is True


async def test_a_free_workspace_may_not_read_its_site_source(patch_workspace):
    """Free is withheld — and by falling through the default, not by a denial."""
    patch_workspace("free")
    ent = await entitlements.resolve_entitlements(WS)
    assert ent.plan == "free"
    assert ent.site_source_visible is False


async def test_the_free_floor_is_not_in_the_granting_set():
    """The floor is absent from the allow-set, which is what makes it withheld.

    Asserted on the set as well as on the resolved value because those are two
    different bugs with the same symptom today: a resolver that stopped reading
    the set would still answer False for free, and only this catches a later edit
    that adds the floor to it.
    """
    assert "free" not in entitlements._SOURCE_VISIBLE_PLANS
    assert set(entitlements._SOURCE_VISIBLE_PLANS) == set(PAID_PLANS)


# ---------------------------------------------------------------------------
# (b) Fail closed on a key the catalog does not know.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("plan", STALE_PLANS)
async def test_a_retired_or_unknown_plan_key_is_withheld(patch_workspace, plan):
    """A stale stored key resolves to the free floor, so it carries no source.

    ``studio`` and ``agency`` are the load-bearing rows here: they are retired
    keys that still appear in stored documents and resolve to no tier. A resolver
    that read the RAW plan string instead of the resolved tier's key would see
    "not free" and grant them.
    """
    patch_workspace(plan)
    ent = await entitlements.resolve_entitlements(WS)
    assert ent.plan == "free"
    assert ent.site_source_visible is False


async def test_a_missing_workspace_is_withheld(patch_workspace):
    """``get_workspace_plan`` -> None (missing / deleted / malformed id)."""
    patch_workspace(None)
    ent = await entitlements.resolve_entitlements(WS)
    assert ent.site_source_visible is False


async def test_a_catalog_with_no_base_tier_is_withheld(patch_workspace, monkeypatch):
    """The last-resort branch, reached by emptying the catalog, still withholds.

    That branch carries a ``pragma: no cover`` because the base tier always
    exists in practice, and an untested branch is where a fail-open default hides
    — it spells out the Free values precisely so a future catalog edit cannot
    quietly grant. Forcing ``get_plan`` to answer None for every key, including
    the base key, is the only way to reach it.
    """
    monkeypatch.setattr(entitlements.plan_catalog, "get_plan", lambda _key: None, raising=True)
    patch_workspace("pro")
    ent = await entitlements.resolve_entitlements(WS)
    assert ent.plan == "free"
    assert ent.site_source_visible is False


def _paid_entitlements(*, site_source_visible: bool = True) -> Entitlements:
    """A fully specified paid ``Entitlements``, for the tests that need an object
    rather than a resolver run."""
    return Entitlements(
        workspace_id=WS,
        plan="pro",
        monthly_credit_allotment=1000,
        monthly_ceiling=None,
        max_seats=25,
        max_pockets=5000,
        max_connectors=250,
        max_call_seconds_per_day=7200,
        max_storage_bytes=50_000_000_000,
        included_sites=3,
        site_source_visible=site_source_visible,
    )


async def test_an_entitlements_object_built_without_the_field_withholds_source():
    """The domain default is the withheld answer, so an omission cannot leak code."""
    ent = Entitlements(
        workspace_id=WS,
        plan="pro",
        monthly_credit_allotment=1000,
        monthly_ceiling=None,
        max_seats=25,
        max_pockets=5000,
        max_connectors=250,
        max_call_seconds_per_day=7200,
        max_storage_bytes=50_000_000_000,
        included_sites=3,
    )
    assert ent.site_source_visible is False


# ---------------------------------------------------------------------------
# (c) A platform override flips it either way.
# ---------------------------------------------------------------------------


async def test_an_override_grants_source_to_a_free_workspace(patch_workspace):
    """The comp lever: an operator turns source on for a tenant the plan withholds."""
    patch_workspace("free", WorkspaceOverrides(site_source_visible=True))
    ent = await entitlements.resolve_entitlements(WS)
    assert ent.plan == "free"
    assert ent.site_source_visible is True


async def test_an_override_revokes_source_from_a_paid_workspace(patch_workspace):
    """The other direction, which is the one a truthy overlay would silently drop.

    ``False`` here must not read as "no opinion". An overlay written as
    ``catalog_value or override`` passes the grant test above and fails this one.
    """
    patch_workspace("pro", WorkspaceOverrides(site_source_visible=False))
    ent = await entitlements.resolve_entitlements(WS)
    assert ent.plan == "pro"
    assert ent.site_source_visible is False


async def test_an_override_that_omits_the_field_leaves_the_plan_alone(patch_workspace):
    """An override set for some other field does not touch this one."""
    patch_workspace("pro", WorkspaceOverrides(max_seats=99))
    ent = await entitlements.resolve_entitlements(WS)
    assert ent.max_seats == 99
    assert ent.site_source_visible is True


async def test_an_expired_override_cannot_grant_source(patch_workspace):
    """An expired set is wholly absent, so the plan's answer stands."""
    patch_workspace(
        "free",
        WorkspaceOverrides(
            site_source_visible=True,
            expires_at=datetime.now(UTC) - timedelta(days=1),
        ),
    )
    ent = await entitlements.resolve_entitlements(WS)
    assert ent.site_source_visible is False


# ---------------------------------------------------------------------------
# (d) The answer reaches the wire and the operator console.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("plan", "expected"), [("free", False), ("pro", True)])
async def test_the_capability_reaches_the_entitlements_wire(patch_workspace, plan, expected):
    """``GET /entitlements`` carries it, so a client need not re-derive the tier."""
    patch_workspace(plan)
    ent = await entitlements.resolve_entitlements(WS)
    assert entitlements_to_dto(ent).site_source_visible is expected


async def test_the_operator_console_can_set_and_read_back_the_capability():
    """The platform override surface accepts the field and renders it both ways.

    A route-shape test, not a DB one: the handlers are covered by
    ``tests/cloud/platform/test_entitlements.py``, and what this pins is that the
    field exists on the write body, on the raw-override read, and on the
    catalog/resolved pair — a field missing from any one of those is an override
    an operator can set and then cannot see.
    """
    assert "site_source_visible" in platform_routes.OverridesWriteIn.model_fields
    assert "site_source_visible" in platform_routes.OverridesOut.model_fields
    assert "site_source_visible" in platform_routes.EntitlementCeilingsOut.model_fields

    body = platform_routes.OverridesWriteIn(site_source_visible=True, reason="Design partner")
    assert body.site_source_visible is True

    rendered = platform_routes._overrides_out(WorkspaceOverrides(site_source_visible=False))
    assert rendered is not None
    assert rendered.site_source_visible is False

    # The catalog/resolved pair the console shows side by side reads the capability
    # off the entitlement rather than re-deriving it.
    assert platform_routes._ceilings(_paid_entitlements()).site_source_visible is True


async def test_an_operators_write_reaches_the_stored_override_document(monkeypatch):
    """The PUT body's flag lands on the ``WorkspaceOverrides`` that gets persisted.

    Drives the handler with its collaborators mocked — no DB, no audit backend —
    because the property under test is the body-to-document mapping. A field
    accepted on the body and dropped before the write is an override that reports
    success and changes nothing, which is the failure this catches.
    """
    written: list[WorkspaceOverrides | None] = []

    monkeypatch.setattr(
        platform_routes.workspace_service,
        "get_workspace_plan_and_overrides",
        AsyncMock(return_value=("free", None)),
    )
    monkeypatch.setattr(
        platform_routes.workspace_service,
        "platform_set_workspace_overrides",
        AsyncMock(side_effect=lambda _ws, ov: written.append(ov)),
    )
    monkeypatch.setattr(platform_routes.audit, "begin", AsyncMock(return_value=object()))
    monkeypatch.setattr(platform_routes.audit, "settle", AsyncMock(return_value=None))
    monkeypatch.setattr(
        platform_routes.workspace_service, "get_workspace_plan", AsyncMock(return_value="free")
    )
    monkeypatch.setattr(
        platform_routes.workspace_service,
        "get_workspace_overrides",
        AsyncMock(return_value=WorkspaceOverrides(site_source_visible=True)),
    )

    out = await platform_routes.set_overrides(
        workspace_id="ws-1",
        body=platform_routes.OverridesWriteIn(
            site_source_visible=True, reason="Design partner, source access"
        ),
        # Both are consumed only by the mocked audit calls, so None is safe here.
        request=None,  # type: ignore[arg-type]
        operator=None,  # type: ignore[arg-type]
    )

    assert written == [WorkspaceOverrides(site_source_visible=True)]
    # And the response the console renders shows the override in effect: the free
    # plan withholds source, the override grants it.
    assert out.catalog.site_source_visible is False
    assert out.resolved.site_source_visible is True
