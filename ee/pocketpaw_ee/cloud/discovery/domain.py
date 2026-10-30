# Discovery — domain value objects (cloud 4-file rule §3).
# Created: 2026-06-21 (SZD finish slice F1 / feat/szd-finish-core) — a frozen
#   value object carrying the tenancy + knobs for one workspace-discovery run.
#   Tenancy (``workspace_id`` / ``user_id``) is required at construction with no
#   defaults, per the ee/cloud rule §3, so a run request can never be built
#   without the originating tenant. The service builds this from the validated
#   request body before handing the run to the orchestrator.

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DiscoveryRunCommand:
    """One workspace-discovery run, fully scoped.

    Constructed in ``service.py`` from the validated ``DiscoveryRunRequest``
    plus the caller's resolved tenancy. ``connector_ids`` is the FINAL list to
    sample (already resolved from the request override or the workspace's
    enabled connectors), and ``sample_cap`` the resolved per-connector cap.
    """

    workspace_id: str
    user_id: str
    connector_ids: tuple[str, ...]
    sample_cap: int
