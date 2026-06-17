# tests/ee/sites/conftest.py
# Created: 2026-06-17 (fix/sites-plan-gate-asymmetry) — Sites is now plan-gated at
# the service chokepoint: sites.service.publish()/publish_pocket() and the create
# MCP handlers call require_sites_plan(), which reads the workspace plan via
# workspace_service.get_workspace_plan and raises Forbidden('plan.feature_denied')
# (or NotFound when the workspace is missing). The existing mechanics tests in
# this directory call publish/create with SYNTHETIC workspace ids that have no
# seeded Workspace doc, so the gate would now raise NotFound and mask what they
# actually exercise. This autouse fixture patches get_workspace_plan to return a
# plan that INCLUDES Sites ("business") by default, so the happy-path mechanics
# tests pass the gate. The dedicated gate test (test_plan_gate.py) overrides the
# plan per-test with its own patch (an inner patch of the same target wins while
# active), so the team-plan denial cases still assert correctly.

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pocketpaw_ee")


@pytest.fixture(autouse=True)
def _default_sites_plan(request: pytest.FixtureRequest):
    """Default the workspace plan to one that unlocks Sites ('business') for the
    sites mechanics tests, so the new service-level plan gate doesn't reject their
    synthetic workspace ids. test_plan_gate.py patches the same target per-test to
    exercise the denial paths."""
    # The gate test owns the plan itself — don't double-patch under it.
    if request.module.__name__.endswith("test_plan_gate"):
        yield
        return
    from unittest.mock import patch

    with patch(
        "pocketpaw_ee.cloud.workspace.service.get_workspace_plan",
        new=AsyncMock(return_value="business"),
    ):
        yield
