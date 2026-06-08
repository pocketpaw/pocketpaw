# Sense tier — EE cloud half (resolver + filler seam + preference store).
# Created: 2026-06-08 — Sense tier chunk 2. Binds a provider-agnostic Sense
# (e.g. paw.email.v1) to whichever connector a tenant ENABLED, then executes
# via the EXISTING connectors_service path. READ-FIRST (v1): only auto-trust
# (read) actions run; confirm/restricted (write) actions are blocked, never
# executed. Imports OSS (pocketpaw.senses, pocketpaw.connectors) freely; OSS
# never imports this package (import-linter contract).

from __future__ import annotations

from pocketpaw_ee.cloud.senses.resolver import (
    ResolvedSense,
    SenseExecutionResult,
    execute_sense,
    resolve,
)

__all__ = [
    "ResolvedSense",
    "SenseExecutionResult",
    "execute_sense",
    "resolve",
]
