# SenseFiller seam — the abstraction a future Skill/KB/Fabric filler plugs into.
# Created: 2026-06-08 — Sense tier chunk 2. A SenseFiller knows which providers
# (connector names) can fill a sense for a given workspace. v1 ships exactly
# one filler — ConnectorSenseFiller — which intersects the static
# connectors-for-sense index with the tenant's ENABLED connectors. The point
# here is the seam, not a framework: register more fillers (skills, KB, fabric)
# in a later phase by implementing the Protocol. Kept deliberately thin.
# Updated: 2026-06-08 (sense-tier efficiency fix) — split ConnectorSenseFiller
#   .candidates() into a one-shot enabled-connector READ (enabled_connector_names)
#   and a PURE intersection (candidates_from) so a batch caller (resolve_many)
#   can fetch the workspace's enabled connectors ONCE and reuse the set across
#   N senses instead of re-querying Beanie per sense. candidates() is kept as
#   the compose of the two so existing single-sense callers/tests are unchanged.

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

    async def enabled_connector_names(
        self,
        workspace_id: str,
        *,
        pocket_id: str | None = None,  # noqa: ARG002 — parity / future pocket scope
    ) -> set[str]:
        """The connector names the workspace has ENABLED (tenant-filtered read).

        This is the ONE Beanie query a batch resolution needs: fetch it once,
        then intersect it (via :meth:`candidates_from`) against each sense's
        declaring set. v1 reads at workspace scope — every enabled doc for the
        workspace counts, regardless of its own scope. ``pocket_id`` is accepted
        for parity / a future pocket-scoped narrowing.
        """
        enabled_docs = await _WCDoc.find(
            _WCDoc.workspace == workspace_id,
            _WCDoc.enabled == True,  # noqa: E712 — Beanie expects ==
        ).to_list()
        return {d.name for d in enabled_docs}

    def candidates_from(self, sense_id: str, enabled_names: set[str]) -> list[str]:
        """PURE intersection — no I/O.

        Connectors that DECLARE ``sense_id`` (static registry index) INTERSECT
        the already-fetched ``enabled_names`` set, sorted for deterministic
        disambiguation. Split out of :meth:`candidates` so a batch caller can
        reuse one ``enabled_names`` read across many senses.
        """
        declaring = set(connectors_for_sense(sense_id, self._registry.definitions))
        if not declaring:
            return []
        return sorted(declaring & enabled_names)

    async def candidates(
        self,
        sense_id: str,
        workspace_id: str,
        *,
        pocket_id: str | None = None,
    ) -> list[str]:
        # The single-sense path: one enabled-connector read + the pure
        # intersection. Identical result to the pre-split implementation.
        enabled_names = await self.enabled_connector_names(workspace_id, pocket_id=pocket_id)
        return self.candidates_from(sense_id, enabled_names)


__all__ = ["ConnectorSenseFiller", "SenseFiller"]
