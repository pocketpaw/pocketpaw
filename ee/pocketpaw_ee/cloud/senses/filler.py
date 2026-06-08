# SenseFiller seam — the abstraction a future Skill/KB/Fabric filler plugs into.
# Created: 2026-06-08 — Sense tier chunk 2. A SenseFiller knows which providers
# (connector names) can fill a sense for a given workspace. v1 ships exactly
# one filler — ConnectorSenseFiller — which intersects the static
# connectors-for-sense index with the tenant's ENABLED connectors. The point
# here is the seam, not a framework: register more fillers (skills, KB, fabric)
# in a later phase by implementing the Protocol. Kept deliberately thin.

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pocketpaw.senses import connectors_for_sense
from pocketpaw_ee.cloud.models.connector import WorkspaceConnector as _WCDoc


@runtime_checkable
class SenseFiller(Protocol):
    """A source of providers that can fill a sense for a workspace.

    A filler answers one question: "which provider names can satisfy
    ``sense_id`` for this workspace right now?" The resolver collects
    candidates from the registered fillers and disambiguates. v1 has the
    connector-backed filler only; a SkillFiller / KBFiller / FabricFiller can
    register later without changing the resolver.
    """

    async def candidates(
        self,
        sense_id: str,
        workspace_id: str,
        *,
        pocket_id: str | None = None,
    ) -> list[str]:
        """Provider names that can fill ``sense_id`` for this workspace."""
        ...


class ConnectorSenseFiller:
    """The only v1 filler — connector-backed.

    Candidates = connectors that DECLARE the sense (static registry index)
    INTERSECT the connectors the workspace has ENABLED. The intersection is
    what makes resolution tenant-aware: a connector that can fill a sense but
    isn't enabled for the workspace is not a candidate.

    For v1 enablement is read at workspace scope (every enabled doc for the
    workspace counts, regardless of its own scope). ``pocket_id`` is accepted
    for signature parity and a future pocket-scoped narrowing, but workspace
    scope is the documented v1 behaviour.
    """

    def __init__(self, registry) -> None:
        self._registry = registry

    async def candidates(
        self,
        sense_id: str,
        workspace_id: str,
        *,
        pocket_id: str | None = None,
    ) -> list[str]:
        # Which connectors CAN fill this sense (provider-agnostic, static).
        declaring = set(connectors_for_sense(sense_id, self._registry.definitions))
        if not declaring:
            return []

        # Which connectors the workspace has ENABLED (tenant-filtered read).
        enabled_docs = await _WCDoc.find(
            _WCDoc.workspace == workspace_id,
            _WCDoc.enabled == True,  # noqa: E712 — Beanie expects ==
        ).to_list()
        enabled_names = {d.name for d in enabled_docs}

        # Intersection, sorted for deterministic disambiguation.
        return sorted(declaring & enabled_names)


__all__ = ["ConnectorSenseFiller", "SenseFiller"]
