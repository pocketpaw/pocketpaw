# tests/cloud/entitlements/test_source_visibility_entitlement.py — proves the
# WORKSPACE-level ``Entitlements.site_source_visible`` is the platform operator's
# override and nothing else: no workspace plan answers it, ``True`` / ``False``
# from an override reach the resolved object, and an expired override is absent.
#
# Whether a given SITE's code shows is a per-site question
# (``entitlements.service.site_code_entitled``), pinned in
# ``tests/cloud/pockets/test_source_visibility_per_site.py``. ``None`` here means
# "no workspace-wide answer, each site decides".
#
# Four properties, in the order a reviewer should check them:
#   (a) every plan, known or stale, resolves ``None`` — the plan grants nothing;
#   (b) an ``Entitlements`` built without the field defers (``None``), never grants;
#   (c) a platform override sets it EITHER way, and an expired override set sets
#       nothing;
#   (d) the answer reaches the ``GET /entitlements`` wire and the platform
#       console's override surface.
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

ALL_PLANS = ["free", "go", "pro", "pro_max", "enterprise"]

# Retired SITE-plan keys that still sit in stored documents, plus plain typos and
# two keys from the pre-consumer-ladder rekey. None of them is a workspace plan
# the catalog carries, so each lands on the unknown-key path.
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
# (a) No workspace plan answers the source question.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("plan", [*ALL_PLANS, *STALE_PLANS, None])
async def test_a_no_workspace_plan_answers_source(patch_workspace, plan):
    """Paid, free, stale or missing: the plan has no opinion, so each site's own
    tier decides. A Pro Max workspace does not unlock a free site's code."""
    patch_workspace(plan)
    ent = await entitlements.resolve_entitlements(WS)
    assert ent.site_source_visible is None


async def test_a_catalog_with_no_base_tier_has_no_opinion_either(patch_workspace, monkeypatch):
    """The last-resort branch, reached by emptying the catalog, grants nothing."""
    monkeypatch.setattr(entitlements.plan_catalog, "get_plan", lambda _key: None, raising=True)
    patch_workspace("pro")
    ent = await entitlements.resolve_entitlements(WS)
    assert ent.plan == "free"
    assert ent.site_source_visible is None


def _paid_entitlements(*, site_source_visible: bool | None = True) -> Entitlements:
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


async def test_b_an_entitlements_object_built_without_the_field_defers():
    """The domain default is "no workspace-wide answer", so an omission can only
    defer to the per-site rule (which fails closed), never grant."""
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
    assert ent.site_source_visible is None


# ---------------------------------------------------------------------------
# (c) A platform override sets it either way.
# ---------------------------------------------------------------------------


async def test_an_override_grants_source_to_a_free_workspace(patch_workspace):
    """The comp lever: an operator turns source on for every site in a tenant."""
    patch_workspace("free", WorkspaceOverrides(site_source_visible=True))
    ent = await entitlements.resolve_entitlements(WS)
    assert ent.plan == "free"
    assert ent.site_source_visible is True


async def test_an_override_revokes_source_from_a_paid_workspace(patch_workspace):
    """The abuse lever. ``False`` must survive as ``False``, not read as "no
    opinion", because ``None`` would let every paid site keep its source."""
    patch_workspace("pro", WorkspaceOverrides(site_source_visible=False))
    ent = await entitlements.resolve_entitlements(WS)
    assert ent.plan == "pro"
    assert ent.site_source_visible is False


async def test_the_flag_overlay_lets_a_revocation_beat_a_grant():
    """The overlay helper itself. With every catalog answer ``None`` today, an
    overlay written as ``catalog_value or override`` would still pass the resolver
    tests above, so the revocation is pinned against a granting catalog value."""
    assert entitlements._resolve_override_flag(True, False) is False
    assert entitlements._resolve_override_flag(None, False) is False
    assert entitlements._resolve_override_flag(None, None) is None
    assert entitlements._resolve_override_flag(False, True) is True


async def test_an_override_that_omits_the_field_leaves_it_unset(patch_workspace):
    """An override set for some other field does not touch this one."""
    patch_workspace("pro", WorkspaceOverrides(max_seats=99))
    ent = await entitlements.resolve_entitlements(WS)
    assert ent.max_seats == 99
    assert ent.site_source_visible is None


async def test_an_expired_override_cannot_grant_source(patch_workspace):
    """An expired set is wholly absent, so there is no workspace-wide answer."""
    patch_workspace(
        "free",
        WorkspaceOverrides(
            site_source_visible=True,
            expires_at=datetime.now(UTC) - timedelta(days=1),
        ),
    )
    ent = await entitlements.resolve_entitlements(WS)
    assert ent.site_source_visible is None


# ---------------------------------------------------------------------------
# (d) The answer reaches the wire and the operator console.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (None, None),
        (WorkspaceOverrides(site_source_visible=True), True),
        (WorkspaceOverrides(site_source_visible=False), False),
    ],
)
async def test_the_override_reaches_the_entitlements_wire(patch_workspace, overrides, expected):
    """``GET /entitlements`` carries the workspace-wide answer, ``None`` included."""
    patch_workspace("pro", overrides)
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
    # And the response the console renders shows the override in effect: no plan
    # answers source (catalog ``None``), the override grants it.
    assert out.catalog.site_source_visible is None
    assert out.resolved.site_source_visible is True
