# tests/cloud/discover/conftest.py — shared fixtures for the Discover suites.
#
# Created 2026-10-01 (feat/discover-index): the Sites plan gate and the builtin
# source registry, moved here from test_discover.py so the router tests get them.
from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.discover import sources


@pytest.fixture(autouse=True)
def sites_plan(monkeypatch) -> None:
    """Synthetic workspaces have no Workspace doc: answer the Sites plan gate."""
    from pocketpaw_ee.cloud.workspace import service as workspace_service

    async def _plan(_workspace_id: str) -> str:
        return "go"

    monkeypatch.setattr(workspace_service, "get_workspace_plan", _plan)


@pytest.fixture(autouse=True)
def builtin_sources() -> None:
    sources.register_builtin_sources()
