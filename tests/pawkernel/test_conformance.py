# Conformance suite for the paw composition kernel (Python runtime).
# Created: 2026-08-24 (feat/pawkernel-compose) — one pytest case per fixture
#   in tests/conformance/paw-compose/, plus a guard asserting the expected
#   fixture set is present so a deleted fixture reads as a failure rather than
#   a shrinking suite.

from __future__ import annotations

from typing import Any

import pytest

from tests.pawkernel.conformance_harness import load_fixtures, run_fixture

EXPECTED_FIXTURE_IDS = {
    "async-disposer-awaited",
    "dep-appears",
    "dep-disappears",
    "dispose-during-load",
    "effect-disposed-on-unload",
    "effect-rejected-while-unloading",
    "failed-apply-rolls-back",
    "isolate-scope",
    "listener-removed-on-unload",
    "load-order-inject",
    "nested-recursive-dispose",
    "waterfall-delegate",
    "waterfall-shortcircuit",
}

FIXTURES = load_fixtures()


def test_every_expected_fixture_is_present() -> None:
    found = {fixture["id"] for fixture in FIXTURES}
    missing = EXPECTED_FIXTURE_IDS - found
    assert not missing, f"conformance fixtures missing: {sorted(missing)}"


@pytest.mark.parametrize("fixture", FIXTURES, ids=[fixture["id"] for fixture in FIXTURES])
async def test_conformance(fixture: dict[str, Any]) -> None:
    await run_fixture(fixture)
