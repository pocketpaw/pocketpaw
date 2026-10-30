# tests/cloud/pockets/test_source_visibility_per_site.py — proves a Paw Site's
# code is visible (``sourceVisible`` and ``source`` on the pocket wire dict) only
# when the SITE ITSELF is on the ``site`` or ``staff`` tier with an active
# subscription. The workspace plan grants nothing.
#
# The sibling ``test_source_redaction_gate.py`` covers the gate's mechanics (the
# cohort stamp, the retroactive switch, every path the payload travels). This file
# covers the entitlement question alone: which pocket's Site row lets code through.
#
# Every test runs with both gate flags on, so every pocket is in scope and the
# answer comes from the entitlement. Site rows are inserted directly in the shapes
# production writes: a draft carries no tier and status "none", a plan-carried
# site carries ``staff`` / ``active`` / ``billing_rail="plan"``.
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.cloud.models.workspace import WorkspaceOverrides
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets.dto import CreatePocketRequest

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("mongo_db")]

PRO_MAX_WS = "ws_src_tier_promax"
FREE_WS = "ws_src_tier_free"
USER = "user_src_tier"

SOURCE_MAP = {
    "src/routes/+page.svelte": "<h1>Tiered</h1>\n",
    "src/app.css": ":root { --ink: #111; }\n",
}

RIPPLE_SPEC: dict[str, Any] = {"ui": {"type": "flex", "children": []}, "state": {}}


@pytest.fixture
def workspace(monkeypatch: pytest.MonkeyPatch):
    """Pin each workspace's plan and override document."""
    import pocketpaw_ee.cloud.workspace.service as ws_svc

    plans = {PRO_MAX_WS: "pro_max", FREE_WS: "free"}
    overrides: dict[str, WorkspaceOverrides | None] = {}

    async def _plan(workspace_id: str) -> str | None:
        return plans.get(workspace_id, "free")

    async def _overrides(workspace_id: str) -> WorkspaceOverrides | None:
        return overrides.get(workspace_id)

    monkeypatch.setattr(ws_svc, "get_workspace_plan", _plan)
    monkeypatch.setattr(ws_svc, "get_workspace_overrides", _overrides)
    return overrides


@pytest.fixture(autouse=True)
def gate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from pocketpaw.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "sites_source_gate_enabled", True)
    monkeypatch.setattr(settings, "sites_source_gate_retroactive", True)


async def _site_pocket(
    workspace_id: str,
    *,
    plan_tier: str | None,
    subscription_status: str = "none",
    billing_rail: str = "",
    name: str = "Site",
    with_site_row: bool = True,
) -> str:
    """A svelte site pocket plus (optionally) its Site row on the given tier."""
    created = await pockets_service.create(
        workspace_id,
        USER,
        CreatePocketRequest(
            name=name,
            type="site",
            pattern="landing",
            engine="svelte",
            source=dict(SOURCE_MAP),
            ripple_spec=dict(RIPPLE_SPEC),
        ),
    )
    if with_site_row:
        await Site(
            workspace=workspace_id,
            pocket_id=created["_id"],
            owner=USER,
            name=name,
            plan_tier=plan_tier,
            subscription_status=subscription_status,
            billing_rail=billing_rail,
        ).insert()
    return created["_id"]


async def _wire(pocket_id: str) -> dict:
    return await pockets_service.get_for_wire(pocket_id, USER)


def _assert_hidden(wire: dict) -> None:
    assert wire["sourceVisible"] is False
    assert wire["source"] is None


def _assert_visible(wire: dict) -> None:
    assert wire["sourceVisible"] is True
    assert wire["source"] == SOURCE_MAP


# ---------------------------------------------------------------------------
# The workspace plan no longer decides.
# ---------------------------------------------------------------------------


async def test_a_free_site_in_a_pro_max_workspace_is_hidden(workspace) -> None:
    pid = await _site_pocket(PRO_MAX_WS, plan_tier="free", subscription_status="none")
    _assert_hidden(await _wire(pid))


async def test_b_an_unpublished_draft_in_a_pro_max_workspace_is_hidden(workspace) -> None:
    """A draft's Site row carries no tier and no subscription: the free floor."""
    pid = await _site_pocket(PRO_MAX_WS, plan_tier=None, subscription_status="none")
    _assert_hidden(await _wire(pid))


async def test_b_a_pocket_with_no_site_row_in_a_pro_max_workspace_is_hidden(workspace) -> None:
    pid = await _site_pocket(PRO_MAX_WS, plan_tier=None, with_site_row=False)
    _assert_hidden(await _wire(pid))


async def test_c_a_paid_site_in_a_free_workspace_is_visible(workspace) -> None:
    pid = await _site_pocket(FREE_WS, plan_tier="site", subscription_status="active")
    _assert_visible(await _wire(pid))


async def test_c_a_legacy_paid_key_resolves_like_its_current_tier(workspace) -> None:
    """``pro`` is the pre-rekey name of ``site`` and still sits in stored rows."""
    pid = await _site_pocket(FREE_WS, plan_tier="pro", subscription_status="active")
    _assert_visible(await _wire(pid))


async def test_d_a_plan_carried_staff_site_is_visible(workspace) -> None:
    pid = await _site_pocket(
        FREE_WS, plan_tier="staff", subscription_status="active", billing_rail="plan"
    )
    _assert_visible(await _wire(pid))


@pytest.mark.parametrize("status", ["cancelled", "pending", "none"])
async def test_e_a_staff_site_without_an_active_subscription_is_hidden(workspace, status) -> None:
    pid = await _site_pocket(PRO_MAX_WS, plan_tier="staff", subscription_status=status)
    _assert_hidden(await _wire(pid))


# ---------------------------------------------------------------------------
# The operator override still applies workspace-wide.
# ---------------------------------------------------------------------------


async def test_f_override_false_hides_even_a_staff_site(workspace) -> None:
    workspace[PRO_MAX_WS] = WorkspaceOverrides(site_source_visible=False)
    pid = await _site_pocket(PRO_MAX_WS, plan_tier="staff", subscription_status="active")
    _assert_hidden(await _wire(pid))


async def test_f_override_true_grants_a_free_site(workspace) -> None:
    workspace[FREE_WS] = WorkspaceOverrides(site_source_visible=True)
    pid = await _site_pocket(FREE_WS, plan_tier="free", subscription_status="none")
    _assert_visible(await _wire(pid))


async def test_f_an_override_on_another_field_defers_to_the_site(workspace) -> None:
    workspace[PRO_MAX_WS] = WorkspaceOverrides(max_seats=99)
    free_pid = await _site_pocket(PRO_MAX_WS, plan_tier="free", name="Free")
    paid_pid = await _site_pocket(
        PRO_MAX_WS, plan_tier="staff", subscription_status="active", name="Paid"
    )
    _assert_hidden(await _wire(free_pid))
    _assert_visible(await _wire(paid_pid))


# ---------------------------------------------------------------------------
# The gallery agrees with the single read, per pocket, without an N+1.
# ---------------------------------------------------------------------------


async def test_g_gallery_agrees_with_the_single_read_for_a_mixed_page(
    workspace, monkeypatch
) -> None:
    from pocketpaw_ee.sites import service as sites_service

    expected = {
        await _site_pocket(PRO_MAX_WS, plan_tier="free", name="Free"): False,
        await _site_pocket(PRO_MAX_WS, plan_tier=None, name="Draft"): False,
        await _site_pocket(
            PRO_MAX_WS, plan_tier="site", subscription_status="active", name="Site"
        ): True,
        await _site_pocket(
            PRO_MAX_WS,
            plan_tier="staff",
            subscription_status="active",
            billing_rail="plan",
            name="Carried",
        ): True,
        await _site_pocket(
            PRO_MAX_WS, plan_tier="staff", subscription_status="cancelled", name="Lapsed"
        ): False,
        await _site_pocket(PRO_MAX_WS, plan_tier=None, with_site_row=False, name="NoRow"): False,
    }

    real = sites_service.site_billing_for_pockets
    lookups = AsyncMock(side_effect=real)
    monkeypatch.setattr(sites_service, "site_billing_for_pockets", lookups)

    rows = await pockets_service.list_pockets(PRO_MAX_WS, USER)
    by_id = {r["_id"]: r for r in rows}
    assert lookups.await_count == 1, "the page's Site rows must load in one batch"

    for pid, visible in expected.items():
        assert by_id[pid]["sourceVisible"] is visible, by_id[pid]["name"]
        assert (await _wire(pid))["sourceVisible"] is visible
