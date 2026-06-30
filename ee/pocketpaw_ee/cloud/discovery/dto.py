# Discovery — request/response DTOs (cloud 4-file rule §4).
# Created: 2026-06-21 (SZD finish slice F1 / feat/szd-finish-core) — the wire
#   contract for the workspace-discovery TRIGGER endpoint
#   ``POST /cloud/discovery/run``. ``DiscoveryRunRequest`` is the (optional)
#   body; ``DiscoveryRunResponse`` mirrors ``DiscoveryProposalResult``
#   (orchestrate.py) so the same proposal-id shape the orchestrator returns is
#   surfaced on the wire. Request and response are distinct models per the
#   ee/cloud rule §4 (never reuse one model for both directions).

from __future__ import annotations

from pydantic import BaseModel, Field


class DiscoveryRunRequest(BaseModel):
    """Body for ``POST /cloud/discovery/run`` — every field optional.

    * ``sample_cap`` — max records sampled per connector (the "N" in "N of M").
      ``None`` lets the run default (``DEFAULT_SAMPLE_CAP``).
    * ``connector_ids`` — explicit override of which bound connectors to sample.
      ``None`` (the common case) lets the service enumerate the workspace's
      ENABLED connectors server-side, so the UI never has to gather ids.
    """

    sample_cap: int | None = None
    connector_ids: list[str] | None = None


class DiscoveryRunResponse(BaseModel):
    """The trigger's response — the staged-proposal ids for optimistic confirm.

    Mirrors :class:`pocketpaw_ee.discovery.orchestrate.DiscoveryProposalResult`.

    NOTE ON THE ASYNC SHAPE: the orchestrate call is fire-and-forget
    (``asyncio.create_task``), so when the run is dispatched in the background
    the action-id fields below are NOT yet known at response time — they are
    returned EMPTY (``None`` / ``[]``) and the proposals surface separately as
    pending Instinct Actions that the ApprovalsPanel already polls. ``run_id``
    is always populated immediately for the optimistic confirmation. The
    full-id shape is still modelled so a synchronous/awaited caller (tests, a
    future awaited mode) gets the complete result.
    """

    run_id: str
    fabric_objects_action_id: str | None = None
    pocket_action_id: str | None = None
    instinct_action_ids: list[str] = Field(default_factory=list)
    materialised_types: list[str] = Field(default_factory=list)
    skipped_types: dict[str, str] = Field(default_factory=dict)
