# tests/cloud/connectors/test_surface_profile_authoring.py
# Created: 2026-06-07 (M3 connector→skill auto-authoring) — end-to-end pins for
#   the bind/unbind auto-authoring path through Mongo (``mongo_db`` fixture):
#     * enable(scope=pocket) for gmail WRITES the derived surface_profile onto
#       the pocket (skill_names=["gmail"]).
#     * disable RE-DERIVES — the dropped connector's contribution leaves the
#       pocket (skill_names back to empty / cleared).
#     * a multi-connector union (monkeypatched catalog) merges both skills.
#     * a user-owned ripple_mode / system_message_override set on the pocket is
#       PRESERVED across a re-derive (the helper only owns the connector dims).
#     * a WORKSPACE-scoped enable does NOT touch any pocket surface_profile.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.connectors import service as connectors_service
from pocketpaw_ee.cloud.connectors.domain import (
    AvailableConnector,
    ConnectorSurfaceContribution,
)
from pocketpaw_ee.cloud.connectors.dto import EnableConnectorRequest
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets.dto import CreatePocketRequest, UpdatePocketRequest

pytestmark = pytest.mark.usefixtures("mongo_db")

_WS = "ws_m3"
_USER = "user_m3"


async def _make_pocket(name: str = "Inbox entity") -> str:
    wire = await pockets_service.create(_WS, _USER, CreatePocketRequest(name=name))
    return wire["_id"]


async def test_enable_pocket_scoped_gmail_writes_derived_profile() -> None:
    """Binding Gmail to a pocket auto-authors skill_names=['gmail']."""
    pocket_id = await _make_pocket()

    await connectors_service.enable_connector(
        _WS, "gmail", EnableConnectorRequest(scope="pocket", pocket_id=pocket_id)
    )

    fetched = await pockets_service.get(pocket_id, _USER)
    assert fetched["surfaceProfile"] is not None
    assert fetched["surfaceProfile"]["skill_names"] == ["gmail"]


async def test_disable_rederives_and_drops_contribution() -> None:
    """Unbinding Gmail re-derives — its skill drops off the pocket."""
    pocket_id = await _make_pocket()
    await connectors_service.enable_connector(
        _WS, "gmail", EnableConnectorRequest(scope="pocket", pocket_id=pocket_id)
    )
    pre = await pockets_service.get(pocket_id, _USER)
    assert pre["surfaceProfile"]["skill_names"] == ["gmail"]

    await connectors_service.disable_connector(_WS, "gmail")

    post = await pockets_service.get(pocket_id, _USER)
    # Only gmail contributed and nothing else is set → override cleared to None.
    assert post["surfaceProfile"] is None


async def test_workspace_scope_does_not_touch_pocket(monkeypatch) -> None:
    """A workspace-scoped enable never authors a pocket surface_profile."""
    pocket_id = await _make_pocket()

    await connectors_service.enable_connector(
        _WS, "gmail", EnableConnectorRequest(scope="workspace")
    )

    fetched = await pockets_service.get(pocket_id, _USER)
    assert fetched["surfaceProfile"] is None


async def test_multi_connector_union(monkeypatch) -> None:
    """Two pocket-scoped connectors UNION their skills on the pocket.

    Patches the catalog so two connectors both carry surface_profile blocks
    (the shipped catalog only tags gmail today). ``stripe`` is a real YAML
    connector so the enable/NotFound guard passes; we override its catalog row.
    """
    pocket_id = await _make_pocket()

    real = connectors_service._available_from_registry()
    by_name = {a.name: a for a in real}

    def _patched() -> list[AvailableConnector]:
        out = []
        for a in real:
            if a.name == "gmail":
                out.append(
                    AvailableConnector(
                        name=a.name,
                        display_name=a.display_name,
                        type=a.type,
                        icon=a.icon,
                        auth_method=a.auth_method,
                        actions=a.actions,
                        surface_profile=ConnectorSurfaceContribution(skill="gmail"),
                    )
                )
            elif a.name == "stripe":
                out.append(
                    AvailableConnector(
                        name=a.name,
                        display_name=a.display_name,
                        type=a.type,
                        icon=a.icon,
                        auth_method=a.auth_method,
                        actions=a.actions,
                        surface_profile=ConnectorSurfaceContribution(
                            skill="payments", allow_tools=("mcp__pay",)
                        ),
                    )
                )
            else:
                out.append(a)
        return out

    assert "stripe" in by_name  # guard: the connector must exist in the registry
    monkeypatch.setattr(connectors_service, "_available_from_registry", _patched)

    await connectors_service.enable_connector(
        _WS, "gmail", EnableConnectorRequest(scope="pocket", pocket_id=pocket_id)
    )
    await connectors_service.enable_connector(
        _WS, "stripe", EnableConnectorRequest(scope="pocket", pocket_id=pocket_id)
    )

    fetched = await pockets_service.get(pocket_id, _USER)
    assert fetched["surfaceProfile"]["skill_names"] == ["gmail", "payments"]
    assert fetched["surfaceProfile"]["allowed_sdk_tools"] == ["mcp__pay"]


async def test_user_owned_dims_preserved_across_rederive() -> None:
    """ripple_mode + system_message_override set by the user survive a re-derive."""
    pocket_id = await _make_pocket()
    # User hand-sets the user-owned dims (not the connector dims).
    await pockets_service.update(
        pocket_id,
        _USER,
        UpdatePocketRequest(
            surfaceProfile={
                "ripple_mode": "off",
                "system_message_override": "be terse",
            }
        ),
    )

    await connectors_service.enable_connector(
        _WS, "gmail", EnableConnectorRequest(scope="pocket", pocket_id=pocket_id)
    )

    fetched = await pockets_service.get(pocket_id, _USER)
    sp = fetched["surfaceProfile"]
    # Connector dim authored…
    assert sp["skill_names"] == ["gmail"]
    # …while the user-owned dims are preserved.
    assert sp["ripple_mode"] == "off"
    assert sp["system_message_override"] == "be terse"

    # And on unbind, the connector dim drops but the user dims remain.
    await connectors_service.disable_connector(_WS, "gmail")
    after = await pockets_service.get(pocket_id, _USER)
    assert after["surfaceProfile"]["skill_names"] == []
    assert after["surfaceProfile"]["ripple_mode"] == "off"
    assert after["surfaceProfile"]["system_message_override"] == "be terse"
