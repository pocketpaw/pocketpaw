# tests/cloud/test_automations_status.py — external-alerting C2/C3: the
# per-workspace automation opt-out (WorkspaceAutomationConfig +
# sweeps_enabled_for_workspace / filter_sweep_enabled_workspaces), the
# constructed sweep registry, the OSS evaluator autostart flag, and the
# _evaluate_threshold real-Fabric-query replacement of the return-False stub.
# Created: 2026-07-11 (feat/external-alerting-c2c3).
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

# NOTE: the suite runs pytest-asyncio in AUTO mode — async tests need no marks.

# ---------------------------------------------------------------------------
# Per-workspace opt-out (service layer, real Beanie docs via mongo_db)
# ---------------------------------------------------------------------------


async def test_sweeps_enabled_defaults_true_without_config(mongo_db):  # noqa: ARG001
    from pocketpaw_ee.cloud.automations_status import service

    assert await service.sweeps_enabled_for_workspace("ws-unconfigured") is True


async def test_opt_out_round_trip_and_filter(mongo_db):  # noqa: ARG001
    from pocketpaw_ee.cloud.automations_status import service

    # Opt one workspace out; leave another untouched.
    await service.set_workspace_config("ws-off", sweeps_enabled=False, automations_enabled=True)
    assert await service.sweeps_enabled_for_workspace("ws-off") is False
    assert await service.sweeps_enabled_for_workspace("ws-on") is True

    # The shared fan-out filter drops ONLY the opted-out tenant.
    kept = await service.filter_sweep_enabled_workspaces({"ws-off", "ws-on"})
    assert kept == {"ws-on"}

    # Re-enable restores the always-on default behavior.
    await service.set_workspace_config("ws-off", sweeps_enabled=True, automations_enabled=True)
    assert await service.sweeps_enabled_for_workspace("ws-off") is True


async def test_automations_enabled_is_independent_of_sweeps(mongo_db):  # noqa: ARG001
    from pocketpaw_ee.cloud.automations_status import service

    await service.set_workspace_config("ws-mix", sweeps_enabled=True, automations_enabled=False)
    assert await service.sweeps_enabled_for_workspace("ws-mix") is True
    assert await service.automations_enabled_for_workspace("ws-mix") is False


# ---------------------------------------------------------------------------
# The constructed sweep registry (no queryable registry existed before)
# ---------------------------------------------------------------------------


def test_sweep_registry_enumerates_the_fleet():
    from pocketpaw_ee.cloud.automations_status.service import build_sweep_registry

    registry = build_sweep_registry()
    names = {d.key for d in registry}
    # The fleet the opt-out must cover — each descriptor carries its gate flag.
    for expected in ("cycles", "member_ingest", "fabric_ingest", "temporal", "refresh"):
        assert any(expected in n for n in names), f"missing sweep descriptor: {expected}"
    assert all(d.env_flag for d in registry)


# ---------------------------------------------------------------------------
# OSS evaluator — autostart flag + the real threshold query
# ---------------------------------------------------------------------------


def test_evaluator_autostart_flag_defaults_on():
    from pocketpaw.config import Settings

    assert Settings().automation_evaluator_autostart is True


def _threshold_rule(**over) -> SimpleNamespace:
    base = dict(
        id="rule-1",
        object_type="Product",
        property="stock",
        operator="less_than",
        value="10",
    )
    base.update(over)
    return SimpleNamespace(**base)


async def _run_threshold(rule, total: int) -> bool:
    from pocketpaw.automations.evaluator import AutomationEvaluator

    fabric = MagicMock()
    fabric.query = AsyncMock(return_value=SimpleNamespace(total=total))
    with (
        patch("pocketpaw.automations.evaluator.get_automation_store", return_value=MagicMock()),
        patch("pocketpaw.stores.get_fabric_store", return_value=fabric),
    ):
        return await AutomationEvaluator()._evaluate_threshold(rule)


async def test_threshold_fires_on_matching_object():
    assert await _run_threshold(_threshold_rule(), total=1) is True


async def test_threshold_quiet_when_nothing_matches():
    assert await _run_threshold(_threshold_rule(), total=0) is False


async def test_threshold_skips_unmapped_operator():
    # An operator with no comparison mapping must never fire (and never query).
    assert await _run_threshold(_threshold_rule(operator="resembles"), total=5) is False
