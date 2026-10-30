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
# Updated 2026-07-21 (review fixes): re-export ``InvalidSpec`` (DTO-boundary
#   validation error added to the port).
# Updated 2026-07-23 (feat/ship-14-source-deploy, SHIP-14): re-export the
#   ``SourceSpec`` tagged union + its ``GitSource`` member (the deploy_source
#   input).
# Updated 2026-07-24 (feat/ship-17-databases, SHIP-17): re-export the Wave 2
#   additions — the ``DbType`` literal and the ``HealthcheckResult`` /
#   ``ScaleResult`` result DTOs (the zero-downtime + process-scaling verbs).

from __future__ import annotations

from pocketpaw_ee.ship_engine.port import (
    AppSpec,
    BackupResult,
    BoxHandle,
    BoxSpec,
    CommandFailed,
    DbResult,
    DbType,
    DeployRequest,
    DeployResult,
    DomainResult,
    GitSource,
    HealthcheckResult,
    InvalidSpec,
    LogChunk,
    MetricsSnapshot,
    ScaleResult,
    ShipEngine,
    ShipEngineError,
    SourceSpec,
    VerbNotSupported,
)

__all__ = [
    "AppSpec",
    "BackupResult",
    "BoxHandle",
    "BoxSpec",
    "CommandFailed",
    "DbResult",
    "DbType",
    "DeployRequest",
    "DeployResult",
    "DomainResult",
    "GitSource",
    "HealthcheckResult",
    "InvalidSpec",
    "LogChunk",
    "MetricsSnapshot",
    "ScaleResult",
    "ShipEngine",
    "ShipEngineError",
    "SourceSpec",
    "VerbNotSupported",
]
