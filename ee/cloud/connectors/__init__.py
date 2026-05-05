# Cloud connectors entity — re-exports for the mount_cloud wiring.
# Created: 2026-05-03 — PR-1 of Phase 1 connector consolidation.
# Strategy locked at ee/cloud/connectors/CHARTER.md. The runtime
# (registry + adapters + protocol) lives at src/pocketpaw/connectors/;
# this module owns the tenanted state and the cloud REST router.

from __future__ import annotations

from ee.cloud.connectors.router import router

__all__ = ["router"]
