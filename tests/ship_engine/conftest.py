# tests/ship_engine/conftest.py — registers ShipEngine implementations with
# the contract suite (SHIP-1) and guards the directory on OSS-only installs.
#
# ``engine_case`` is the parametrized fixture the whole contract suite runs
# over. Adding a future driver (Dokploy, own-Go) means: build its wiring
# module (see ``dokku_wiring.py``), append its ``EngineCase`` to
# ``ENGINE_CASES`` — the suite runs unchanged.
#
# The ``pocketpaw_ee`` guard mirrors the tests/ee pattern: a plain
# ``uv sync --dev`` (OSS-only, what the OSS CI lane runs) has no enterprise
# package, so this directory must skip collection instead of erroring.
#
# Created 2026-07-21 (feat/ship-1-engine-contract): new module.

from __future__ import annotations

import pytest

try:
    import pocketpaw_ee  # noqa: F401

    _HAVE_EE = True
except ModuleNotFoundError:
    _HAVE_EE = False

# On an OSS-only install, skip collecting everything in this directory.
collect_ignore_glob = [] if _HAVE_EE else ["*"]

if _HAVE_EE:
    from tests.ship_engine.contract import EngineCase
    from tests.ship_engine.dokku_wiring import DOKKU_CASE

    ENGINE_CASES: dict[str, EngineCase] = {
        DOKKU_CASE.name: DOKKU_CASE,
    }

    @pytest.fixture(params=sorted(ENGINE_CASES), ids=sorted(ENGINE_CASES))
    def engine_case(request: pytest.FixtureRequest) -> EngineCase:
        """One registered ShipEngine implementation's contract wiring."""
        return ENGINE_CASES[request.param]
