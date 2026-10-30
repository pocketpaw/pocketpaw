# tests/cloud/pockets/test_pocket_cap.py
# Created 2026-07-08 (feat/billing-smb-caps) — locks the per-plan POCKET cap on the
# create seam. The cap (``_pocket_cap_exceeded``) resolves the workspace's plan
# ``max_pockets`` and counts its live pockets; it is enforced at CREATE time only
# (never removes an existing pocket) and is a no-op unless ``billing_enforced``.
#
# Two create seams are covered:
#   * ``create`` — the HTTP path — RAISES ``PocketLimitError`` (402) at/over the cap.
#   * ``create_from_ripple_spec`` — the agent auto-create path (does NOT funnel
#     through ``create``) — returns ``None`` at/over the cap so the agent turn
#     degrades gracefully instead of surfacing a raw 402 through the loop.
#
# The plan resolver is stubbed to a controlled ``max_pockets`` so the count/limit
# logic is exercised against a small ceiling (no need to insert 200 real pockets);
# the catalog-value + fail-closed contract itself lives in test_plans.py /
# test_entitlements.py. ``billing_enforced`` is toggled by pointing the config's
# ``get_settings`` at a flag stub — the same flag-mode lane the credit gate uses.

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pocketpaw_ee.cloud._core.errors import PocketLimitError
from pocketpaw_ee.cloud.entitlements import service as entitlements_service
from pocketpaw_ee.cloud.entitlements.domain import Entitlements
from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets.dto import CreatePocketRequest

import pocketpaw.config as ppconfig

pytestmark = pytest.mark.usefixtures("mongo_db")

_WS = "ws_pocket_cap"
_USER = "user_pocket_cap"

_RIPPLE_SPEC = {
    "version": "1.0",
    "state": {},
    "ui": {"id": "n_root0001", "type": "flex", "props": {}, "children": []},
}


def _enforce(monkeypatch, *, on: bool) -> None:
    """Point the config's ``get_settings`` (lazily imported inside the cap helper)
    at a flag stub carrying the billing posture."""
    monkeypatch.setattr(
        ppconfig,
        "get_settings",
        lambda: SimpleNamespace(billing_enforced=on, dodo_plan_products=None),
    )


def _cap(monkeypatch, *, max_pockets: int | None) -> None:
    """Stub the entitlements resolver to a controlled ``max_pockets`` ceiling."""
    ent = Entitlements(
        workspace_id=_WS,
        plan="free",
        monthly_credit_allotment=0,
        monthly_ceiling=1_000,
        max_seats=5,
        max_pockets=max_pockets,
        max_connectors=50,
        features=frozenset(),
    )
    monkeypatch.setattr(entitlements_service, "resolve_entitlements", AsyncMock(return_value=ent))


async def _seed_pockets(n: int) -> None:
    for i in range(n):
        await _PocketDoc(workspace=_WS, name=f"p{i}", owner=_USER).insert()


# ---------------------------------------------------------------------------
# create() — the HTTP path raises PocketLimitError.
# ---------------------------------------------------------------------------


async def test_create_raises_pocket_limit_at_cap(monkeypatch) -> None:
    """At/over the plan cap, create() raises PocketLimitError (402) — no write."""
    _enforce(monkeypatch, on=True)
    _cap(monkeypatch, max_pockets=2)
    await _seed_pockets(2)  # already at the cap

    with pytest.raises(PocketLimitError) as exc:
        await pockets_service.create(_WS, _USER, CreatePocketRequest(name="over"))
    assert exc.value.status_code == 402
    assert exc.value.code == "billing.pocket_limit"
    # The blocked create did NOT persist a 3rd pocket.
    assert await _PocketDoc.find(_PocketDoc.workspace == _WS).count() == 2


async def test_create_allows_below_cap(monkeypatch) -> None:
    """Below the cap, create() proceeds normally."""
    _enforce(monkeypatch, on=True)
    _cap(monkeypatch, max_pockets=2)
    await _seed_pockets(1)  # one under the cap

    wire = await pockets_service.create(_WS, _USER, CreatePocketRequest(name="ok"))
    assert wire["_id"]
    assert await _PocketDoc.find(_PocketDoc.workspace == _WS).count() == 2


async def test_create_pocket_cap_is_noop_when_billing_disabled(monkeypatch) -> None:
    """With billing_enforced OFF (OSS / self-host), the cap never fires — even well
    over the ceiling."""
    _enforce(monkeypatch, on=False)
    _cap(monkeypatch, max_pockets=2)
    await _seed_pockets(5)  # way over the (ignored) cap

    wire = await pockets_service.create(_WS, _USER, CreatePocketRequest(name="ok"))
    assert wire["_id"]


async def test_create_pocket_cap_is_noop_when_uncapped(monkeypatch) -> None:
    """An uncapped plan (Enterprise, max_pockets=None) never trips the cap."""
    _enforce(monkeypatch, on=True)
    _cap(monkeypatch, max_pockets=None)
    await _seed_pockets(5)

    wire = await pockets_service.create(_WS, _USER, CreatePocketRequest(name="ok"))
    assert wire["_id"]


# ---------------------------------------------------------------------------
# create_from_ripple_spec() — the agent path returns None at the cap.
# ---------------------------------------------------------------------------


async def test_create_from_ripple_spec_returns_none_at_cap(monkeypatch) -> None:
    """The agent auto-create path degrades to None (not a 402) at/over the cap."""
    _enforce(monkeypatch, on=True)
    _cap(monkeypatch, max_pockets=2)
    await _seed_pockets(2)

    pocket_id = await pockets_service.create_from_ripple_spec(_WS, _USER, _RIPPLE_SPEC)
    assert pocket_id is None
    assert await _PocketDoc.find(_PocketDoc.workspace == _WS).count() == 2


async def test_create_from_ripple_spec_succeeds_below_cap(monkeypatch) -> None:
    """Below the cap, the agent auto-create path still creates the pocket."""
    _enforce(monkeypatch, on=True)
    _cap(monkeypatch, max_pockets=5)
    await _seed_pockets(1)

    pocket_id = await pockets_service.create_from_ripple_spec(_WS, _USER, _RIPPLE_SPEC)
    assert pocket_id is not None
