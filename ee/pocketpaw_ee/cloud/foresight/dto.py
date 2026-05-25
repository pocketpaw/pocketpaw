# ee/pocketpaw_ee/cloud/foresight/dto.py
# Modified: 2026-05-25 (feat/foresight-v07-cloud-mount) — PR 7 adds
#   ScenarioRunListItemResponse (lighter shape for GET /runs without
#   the inline ``result`` blob) and re-exports the existing v0.1 shapes
#   unchanged so any v0.1 caller keeps working.
# Created: 2026-05-25 (feat/foresight-v01-scaffold) — RFC 08 v0.1 scaffold.
#
# Request / response models for the Foresight REST surface. Per the
# ee/cloud rule #4 (DTOs separate request and response), every
# operation has its own *Request and *Response shape — even though
# v0.1 only ships two endpoints (POST /scenarios, GET /runs/:id),
# both have distinct request/response contracts that v1.0 will
# extend without breaking compatibility.

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PersonaSpecRequest(BaseModel):
    """One persona declared inline in a POST /scenarios body.

    The shape mirrors ``foresight.scenarios.runner.PersonaSpec`` but is
    a Pydantic model so FastAPI's request parser handles validation.
    v1.0 adds a soul_path field for soul-file-anchored personas
    (RFC §16.2 — synthesized souls in did:soul:synthesized:* namespace).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=128)
    role: str = Field(default="participant", max_length=64)
    ocean: dict[str, float] = Field(default_factory=dict)


class CreateScenarioRequest(BaseModel):
    """POST /api/v1/foresight/scenarios body.

    v0.1 accepts the inline scenario shape only (declarative personas
    in the body). v1.0 adds:
      - ``scenario_path``: load a YAML by path (for saved scenarios)
      - ``scenario_id``: reference a stored scenario by id
      - ``tier_mix_override``, ``budget_cap_usd``, ``activation_overlay``
        and the rest of RFC §18's grammar.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=128)
    sub_type: str = Field(default="decision_forecast", max_length=64)
    n_ticks: int = Field(default=1, ge=1, le=1000)
    personas: list[PersonaSpecRequest] = Field(..., min_length=1, max_length=1000)


class ScenarioRunResponse(BaseModel):
    """POST /scenarios response + GET /runs/:id response.

    v0.1 returns a single shape for both endpoints (immediately-completed
    run on POST; same shape on GET). v1.0 will split these — POST
    returns a "queued" envelope with the run id and a websocket subscription
    URL, GET returns the full result with the per-tick aggregates and
    projected decisions stream.

    PR 7 keeps the v0.1 wire field set (id, scenario_name, status,
    created_at, request, result, error) and adds an optional
    ``workspace_id`` so the cloud surface can echo the tenancy key the
    persistence layer enforces. Older callers that only consumed the
    v0.1 fields keep working — Pydantic's default ``extra="forbid"``
    constraint is unchanged at the request side; responses tolerate
    additional fields client-side.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    workspace_id: str | None = None
    scenario_name: str
    status: str  # "queued" | "running" | "complete" | "failed"
    created_at: str  # ISO-8601
    updated_at: str | None = None
    request: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None


class ScenarioRunListItemResponse(BaseModel):
    """Lighter shape for ``GET /runs`` — drops the inline ``result`` and
    ``request`` blobs so the list endpoint stays cheap on workspaces
    that have accumulated dozens of runs.

    The detail endpoint (``GET /runs/{id}``) returns the full
    :class:`ScenarioRunResponse` shape; the frontend Scenarios + Live
    panels (RFC §11.2 / §11.3) use the list shape for cards and call
    the detail endpoint when the operator clicks through.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    workspace_id: str | None = None
    scenario_name: str
    status: str
    created_at: str
    updated_at: str | None = None
    error: str | None = None


__all__ = [
    "CreateScenarioRequest",
    "PersonaSpecRequest",
    "ScenarioRunListItemResponse",
    "ScenarioRunResponse",
]
