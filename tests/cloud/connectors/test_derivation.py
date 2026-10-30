# tests/cloud/connectors/test_derivation.py
# Created: 2026-06-07 (M3 connector→skill auto-authoring) — pins the PURE
#   derivation helper ``derive_surface_profile``: single connector → its profile;
#   multiple → UNION of skills + allow + deny; a connector with no block →
#   no contribution; empty set → None. Also checks determinism/idempotence
#   (sorted, order-free) and that ripple_mode / system_message_override are left
#   ``None`` (user-owned dims the helper never touches).

from __future__ import annotations

from pocketpaw_ee.cloud.connectors.derivation import derive_surface_profile
from pocketpaw_ee.cloud.connectors.domain import (
    AvailableConnector,
    ConnectorSurfaceContribution,
)


def _conn(name: str, contribution: ConnectorSurfaceContribution | None) -> AvailableConnector:
    return AvailableConnector(
        name=name,
        display_name=name.title(),
        type="communication",
        icon="plug",
        auth_method="oauth2",
        surface_profile=contribution,
    )


def test_single_connector_yields_its_profile() -> None:
    c = _conn("gmail", ConnectorSurfaceContribution(skill="gmail"))
    profile = derive_surface_profile([c])
    assert profile is not None
    assert profile.skill_names == ["gmail"]
    # No tool patterns → no SDK-tool restriction (None), empty deny.
    assert profile.allowed_sdk_tools is None
    assert profile.deny_mcp_tool_ids == []
    # User-owned dims untouched.
    assert profile.ripple_mode is None
    assert profile.system_message_override is None


def test_multiple_connectors_union() -> None:
    c1 = _conn(
        "gmail",
        ConnectorSurfaceContribution(
            skill="gmail", allow_tools=("mcp__a",), deny_tools=("mcp__x",)
        ),
    )
    c2 = _conn(
        "cal",
        ConnectorSurfaceContribution(
            skill="gcalendar", allow_tools=("mcp__b",), deny_tools=("mcp__y",)
        ),
    )
    profile = derive_surface_profile([c1, c2])
    assert profile is not None
    assert profile.skill_names == ["gcalendar", "gmail"]  # sorted
    assert profile.allowed_sdk_tools == ["mcp__a", "mcp__b"]
    assert profile.deny_mcp_tool_ids == ["mcp__x", "mcp__y"]


def test_connector_without_block_contributes_nothing() -> None:
    c1 = _conn("gmail", ConnectorSurfaceContribution(skill="gmail"))
    c2 = _conn("airtable", None)
    profile = derive_surface_profile([c1, c2])
    assert profile is not None
    assert profile.skill_names == ["gmail"]


def test_only_blockless_connectors_yields_none() -> None:
    profile = derive_surface_profile([_conn("airtable", None), _conn("stripe", None)])
    assert profile is None


def test_empty_set_yields_none() -> None:
    assert derive_surface_profile([]) is None


def test_idempotent_and_order_free() -> None:
    c1 = _conn("gmail", ConnectorSurfaceContribution(skill="gmail", deny_tools=("mcp__x",)))
    c2 = _conn("cal", ConnectorSurfaceContribution(skill="gcalendar"))
    a = derive_surface_profile([c1, c2])
    b = derive_surface_profile([c2, c1])
    assert a == b
    # Re-deriving from the same full set is stable.
    assert derive_surface_profile([c1, c2]) == a


def test_duplicate_skills_deduped() -> None:
    c1 = _conn("g1", ConnectorSurfaceContribution(skill="gmail"))
    c2 = _conn("g2", ConnectorSurfaceContribution(skill="gmail"))
    profile = derive_surface_profile([c1, c2])
    assert profile is not None
    assert profile.skill_names == ["gmail"]
