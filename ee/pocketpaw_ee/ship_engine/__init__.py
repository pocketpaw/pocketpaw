# ee/pocketpaw_ee/ship_engine/__init__.py — the /ship deploy-engine package
# (SHIP-1, first slice of the /ship surface).
#
# Re-exports the provider-agnostic contract surface from ``port`` so consumers
# write ``from pocketpaw_ee.ship_engine import ShipEngine, DeployRequest, ...``.
# Drivers are NOT re-exported — import ``DokkuDriver`` from
# ``pocketpaw_ee.ship_engine.dokku`` explicitly, so depending on the contract
# never drags in a specific engine.
#
# Created 2026-07-21 (feat/ship-1-engine-contract): new package.

from __future__ import annotations

from pocketpaw_ee.ship_engine.port import (
    AppSpec,
    BackupResult,
    BoxHandle,
    BoxSpec,
    CommandFailed,
    DbResult,
    DeployRequest,
    DeployResult,
    DomainResult,
    LogChunk,
    MetricsSnapshot,
    ShipEngine,
    ShipEngineError,
    VerbNotSupported,
)

__all__ = [
    "AppSpec",
    "BackupResult",
    "BoxHandle",
    "BoxSpec",
    "CommandFailed",
    "DbResult",
    "DeployRequest",
    "DeployResult",
    "DomainResult",
    "LogChunk",
    "MetricsSnapshot",
    "ShipEngine",
    "ShipEngineError",
    "VerbNotSupported",
]
