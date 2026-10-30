# ee/pocketpaw_ee/cloud/models/foresight_run.py
# Created: 2026-05-25 (feat/foresight-v07-cloud-mount) — RFC 08 PR 7.
#
# Foresight run document — Mongo persistence for scenario runs that were
# previously held in the v0.1 in-memory ``RunStore`` (ee/foresight/api/
# run_store.py). One document per scenario run; carries the request body
# the operator submitted, the engine's ``RunResult.as_wire_dict()`` once
# the run completes, and an error string when the run fails.
#
# The full RFC §7.7 ProjectedDecision fan-out (per-tick aggregates +
# projected_decisions rows + calibration buffer entries) lands in PR 8+;
# this PR ships the run-level collection only so the cloud surface has
# durable storage for ``GET /foresight/runs/:id`` / ``GET /foresight/runs``
# across restarts and across workers.
#
# Only ``ee.cloud.foresight.service`` may import this module — enforced by
# the import-linter contract in ``ee/pyproject.toml``.

from __future__ import annotations

from typing import Any

from beanie import Indexed
from pydantic import Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class ForesightRun(TimestampedDocument):
    """One Foresight scenario run, persisted in Mongo.

    The wire shape (``ee.cloud.foresight.dto.ScenarioRunResponse``) mirrors
    the fields below 1-to-1 with workspace-stripping at the response layer —
    the operator's view never includes the tenancy key directly, that lives
    on the dependency-resolved ``RequestContext``.

    Status vocabulary matches the v0.1 in-memory store so the API contract
    is unchanged: ``queued | running | complete | failed``.

    ``request`` is the validated POST body (``CreateScenarioRequest.model_dump()``);
    ``result`` is the engine's ``RunResult.as_wire_dict()`` once the run
    completes (or ``None`` while queued / running / failed); ``error`` is
    the failure message when the run raises.
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    scenario_name: str
    status: str = Field(
        default="queued",
        pattern="^(queued|running|complete|failed)$",
    )
    request: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    created_by: str = ""

    class Settings:
        name = "foresight_runs"
        indexes = [
            [("workspace", 1), ("status", 1)],
            # ``createdAt`` ordering is the default Mongo cursor for the
            # list endpoint; an explicit index keeps the most-recent-first
            # query cheap once a workspace accumulates dozens of runs.
            [("workspace", 1), ("createdAt", -1)],
        ]
