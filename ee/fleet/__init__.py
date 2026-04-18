# Fleet — installable bundles of soul + pocket + connectors + scopes.
# Created: 2026-04-13 (Move 7 PR-B) — A FleetTemplate is a YAML manifest
# that a non-technical operator can install with one command. Reads the
# manifest, creates the soul (via SoulFactory.from_template), creates the
# pocket, registers the listed connectors, and seeds scope tags. Outputs
# an InstallReport so the UI/CLI can show what landed and what failed.

from ee.fleet.installer import (
    FleetInstallReport,
    FleetInstallStep,
    install_fleet,
    list_bundled_fleets,
    load_fleet,
)
from ee.fleet.models import FleetConnector, FleetTemplate

__all__ = [
    "FleetConnector",
    "FleetInstallReport",
    "FleetInstallStep",
    "FleetTemplate",
    "install_fleet",
    "list_bundled_fleets",
    "load_fleet",
]
