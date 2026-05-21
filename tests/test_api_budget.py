"""Tests for budget API router and handlers."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from pocketpaw.api.v1.budget import (
    BudgetOverrideRequest,
    clear_budget_override_route,
    get_budget_status,
    router,
    set_budget_override,
)
from pocketpaw.budget import BudgetSnapshot, BudgetWindow


def _snapshot(level: str = "ok") -> BudgetSnapshot:
    return BudgetSnapshot(
        window_start="2026-04-01T00:00:00+00:00",
        window_end="2026-05-01T00:00:00+00:00",
        window_key="2026-04-01_2026-05-01",
        configured_cap_usd=20.0,
        effective_cap_usd=20.0,
        override_active=False,
        warning_threshold=0.8,
        spent_usd=5.0,
        remaining_usd=15.0,
        percent_used=25.0,
        level=level,
        exhausted=level == "exhausted",
    )


def test_budget_router_has_routes() -> None:
    paths = {route.path for route in router.routes if hasattr(route, "path")}
    assert "/budget/status" in paths
    assert "/budget/override" in paths


def test_budget_router_registered_in_v1() -> None:
    from pocketpaw.api.v1 import _V1_ROUTERS

    modules = [item[0] for item in _V1_ROUTERS]
    assert "pocketpaw.api.v1.budget" in modules


@pytest.mark.asyncio
async def test_get_budget_status_returns_payload() -> None:
    settings = SimpleNamespace(
        budget_paused=False,
        budget_auto_pause=True,
        budget_reset_day=1,
        budget_monthly_usd=20.0,
        budget_warning_threshold=0.8,
        budget_override_usd=None,
        budget_override_reason="",
        budget_override_expires_at=None,
        save=MagicMock(),
    )

    with (
        patch("pocketpaw.api.v1.budget.Settings") as mock_settings_cls,
        patch("pocketpaw.api.v1.budget.get_settings") as mock_get_settings,
        patch("pocketpaw.api.v1.budget.sync_budget_state", return_value=(_snapshot(), False)),
    ):
        mock_settings_cls.load.return_value = settings
        mock_get_settings.cache_clear = MagicMock()

        response = await get_budget_status()

    assert response["paused"] is False
    assert response["budget"]["level"] == "ok"
    assert response["override"]["cap_usd"] is None


@pytest.mark.asyncio
async def test_set_budget_override_updates_until_window_end() -> None:
    settings = SimpleNamespace(
        budget_reset_day=1,
        budget_override_usd=None,
        budget_override_reason="",
        budget_override_expires_at=None,
        budget_paused=False,
        save=MagicMock(),
    )
    window = BudgetWindow(
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 5, 1, tzinfo=UTC),
        key="2026-04-01_2026-05-01",
    )

    with (
        patch("pocketpaw.api.v1.budget.Settings") as mock_settings_cls,
        patch("pocketpaw.api.v1.budget.get_settings") as mock_get_settings,
        patch("pocketpaw.api.v1.budget.sync_budget_state", return_value=(_snapshot(), False)),
        patch(
            "pocketpaw.api.v1.budget.set_budget_override_until_window_end",
            return_value=window,
        ) as mock_set_override,
    ):
        mock_settings_cls.load.return_value = settings
        mock_get_settings.cache_clear = MagicMock()

        response = await set_budget_override(BudgetOverrideRequest(cap_usd=30.0, reason="incident"))

    assert response["status"] == "ok"
    assert response["window_start"] == "2026-04-01T00:00:00+00:00"
    assert response["window_end"] == "2026-05-01T00:00:00+00:00"
    assert response["budget"]["level"] == "ok"
    assert mock_set_override.call_count == 1


@pytest.mark.asyncio
async def test_clear_budget_override_route_clears_and_returns_status() -> None:
    settings = SimpleNamespace(
        budget_override_usd=30.0,
        budget_override_reason="incident",
        budget_override_expires_at="2026-05-01T00:00:00+00:00",
        budget_paused=False,
        save=MagicMock(),
    )

    with (
        patch("pocketpaw.api.v1.budget.Settings") as mock_settings_cls,
        patch("pocketpaw.api.v1.budget.get_settings") as mock_get_settings,
        patch("pocketpaw.api.v1.budget.sync_budget_state", return_value=(_snapshot(), False)),
        patch("pocketpaw.api.v1.budget.clear_budget_override") as mock_clear_override,
    ):
        mock_settings_cls.load.return_value = settings
        mock_get_settings.cache_clear = MagicMock()

        response = await clear_budget_override_route()

    assert response["status"] == "ok"
    assert response["budget"]["level"] == "ok"
    assert mock_clear_override.call_count == 1
