# tests/cloud/connectors/test_connector_cap.py
# Created 2026-07-08 (feat/billing-smb-caps) — locks the per-plan CONNECTOR cap on
# the enable seam. ``enable_connector`` raises ``ConnectorLimitError`` (402) when the
# workspace already has its plan's ``max_connectors`` enabled and the call would
# turn a NEW connector on (a fresh row, or re-enabling a disabled one). The cap
# (``_connector_cap_exceeded``) is a no-op unless ``billing_enforced`` and never
# trips on an uncapped Enterprise plan. An idempotent re-enable of an
# already-enabled connector is NEVER blocked — enforcement is enable-time only,
# never retroactive.
#
# The plan resolver is stubbed to a controlled ``max_connectors`` so the count/limit
# logic is exercised against a small ceiling; the catalog-value + fail-closed
# contract lives in test_plans.py / test_entitlements.py. ``billing_enforced`` is
# toggled by pointing the config's ``get_settings`` at a flag stub. ``stripe`` /
# ``gmail`` / ``github`` are YAML connectors shipped in repo /connectors, available
# in every registry instance so tests need no catalog seeding.

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pocketpaw_ee.cloud._core.errors import ConnectorLimitError
from pocketpaw_ee.cloud.connectors import service as connectors_service
from pocketpaw_ee.cloud.connectors.dto import EnableConnectorRequest
from pocketpaw_ee.cloud.entitlements import service as entitlements_service
from pocketpaw_ee.cloud.entitlements.domain import Entitlements

import pocketpaw.config as ppconfig

pytestmark = pytest.mark.usefixtures("mongo_db")

_WS = "ws_connector_cap"


def _enforce(monkeypatch, *, on: bool) -> None:
    """Point the config's ``get_settings`` (lazily imported inside the cap helper)
    at a flag stub carrying the billing posture."""
    monkeypatch.setattr(
        ppconfig,
        "get_settings",
        lambda: SimpleNamespace(billing_enforced=on, dodo_plan_products=None),
    )


def _cap(monkeypatch, *, max_connectors: int | None) -> None:
    """Stub the entitlements resolver to a controlled ``max_connectors`` ceiling."""
    ent = Entitlements(
        workspace_id=_WS,
        plan="free",
        monthly_credit_allotment=0,
        monthly_ceiling=1_000,
        max_seats=5,
        max_pockets=200,
        max_connectors=max_connectors,
        features=frozenset(),
    )
    monkeypatch.setattr(entitlements_service, "resolve_entitlements", AsyncMock(return_value=ent))


async def _enable(name: str):
    return await connectors_service.enable_connector(
        _WS, name, EnableConnectorRequest(scope="workspace")
    )


async def test_enable_raises_connector_limit_at_cap(monkeypatch) -> None:
    """At/over the plan cap, enabling a NEW connector raises ConnectorLimitError."""
    _enforce(monkeypatch, on=True)
    _cap(monkeypatch, max_connectors=2)

    await _enable("stripe")  # 1 enabled
    await _enable("gmail")  # 2 enabled — at the cap

    with pytest.raises(ConnectorLimitError) as exc:
        await _enable("github")
    assert exc.value.status_code == 402
    assert exc.value.code == "billing.connector_limit"


async def test_enable_allows_below_cap(monkeypatch) -> None:
    """Below the cap, enable proceeds normally."""
    _enforce(monkeypatch, on=True)
    _cap(monkeypatch, max_connectors=2)

    resp = await _enable("stripe")
    assert resp.enabled is True


async def test_reenable_already_enabled_connector_is_never_blocked(monkeypatch) -> None:
    """An idempotent re-enable of an already-enabled connector is NOT blocked, even
    when the workspace sits exactly at the cap — the cap is create/enable-time only
    and must never retroactively strip an existing connector."""
    _enforce(monkeypatch, on=True)
    _cap(monkeypatch, max_connectors=1)

    await _enable("stripe")  # 1 enabled — at the cap

    # Re-enabling the SAME already-enabled connector adds nothing, so it must pass.
    resp = await _enable("stripe")
    assert resp.enabled is True


async def test_reenable_disabled_connector_is_capped(monkeypatch) -> None:
    """Re-enabling a DISABLED connector counts as turning one on — blocked at cap."""
    _enforce(monkeypatch, on=True)
    _cap(monkeypatch, max_connectors=1)

    await _enable("stripe")  # 1 enabled
    await connectors_service.disable_connector(_WS, "stripe")  # 0 enabled
    await _enable("gmail")  # 1 enabled — back at the cap

    # stripe's row still exists but is disabled; re-enabling it would exceed the cap.
    with pytest.raises(ConnectorLimitError):
        await _enable("stripe")


async def test_connector_cap_is_noop_when_billing_disabled(monkeypatch) -> None:
    """With billing_enforced OFF, the cap never fires — even past the ceiling."""
    _enforce(monkeypatch, on=False)
    _cap(monkeypatch, max_connectors=1)

    await _enable("stripe")
    await _enable("gmail")
    resp = await _enable("github")  # 3rd, well over the (ignored) cap
    assert resp.enabled is True


async def test_connector_cap_is_noop_when_uncapped(monkeypatch) -> None:
    """An uncapped plan (Enterprise, max_connectors=None) never trips the cap."""
    _enforce(monkeypatch, on=True)
    _cap(monkeypatch, max_connectors=None)

    await _enable("stripe")
    await _enable("gmail")
    resp = await _enable("github")
    assert resp.enabled is True
