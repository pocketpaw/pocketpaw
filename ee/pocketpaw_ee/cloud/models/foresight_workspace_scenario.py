# ee/pocketpaw_ee/cloud/models/foresight_workspace_scenario.py
# Created: 2026-05-26 (feat/foresight-v10-scenario-editor-backend) — RFC 08
# v1.0 wave 3. Workspace-scoped custom scenario YAMLs.
#
# One document per user-saved scenario. The scenario YAML body is
# embedded inline (max 64 KB) so the cloud surface doesn't need a
# separate blob store for v1.0; the doc also carries a denormalized
# ``parsed_meta`` block populated by the service at write time so the
# list endpoint can render per-row counts (personas, ticks, tier mix)
# without re-parsing every YAML on every request.
#
# Indexes match the two read paths the surface exposes:
#   - List (workspace_id, updated_at desc) — the GET /scenarios/custom
#     endpoint orders by most-recent-edit-first.
#   - Filter (workspace_id, sub_type) — the same endpoint paginates by
#     sub_type when the operator opens the Decision Forecast / Market
#     Sim / Org Change panes.
#
# Only ``ee.cloud.foresight.scenarios`` may import this module —
# enforced by the import-linter contract in ``ee/pyproject.toml``.

from __future__ import annotations

from typing import Any, Literal

from beanie import Indexed
from pydantic import Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class ForesightWorkspaceScenario(TimestampedDocument):
    """One workspace-scoped custom Foresight scenario, persisted in Mongo.

    Fields:
      - ``workspace_id`` — tenancy key (Indexed for list/filter queries).
      - ``name`` — operator-supplied display name; capped at 120 chars at
        the DTO layer so the doc never stores something the UI can't
        render in a card title.
      - ``sub_type`` — one of ``decision_forecast`` /
        ``market_sim`` / ``org_change_rehearsal``. v1.0 ships the same
        three sub-types the engine supports; future sub-types (RFC §4)
        broaden this set in lockstep with the engine layer.
      - ``description`` — short operator-supplied prose (≤500 chars).
      - ``author`` — the ``user_id`` of the operator who saved the
        scenario. Carried for audit + the list endpoint's "saved by"
        column.
      - ``yaml_body`` — the full scenario YAML as a string. Capped at
        64 KB at the service layer; the engine's loader parses this on
        run-time when the scenario is referenced via
        ``custom_scenario_id``.
      - ``parsed_meta`` — denormalized parse result. Service computes
        this at write time so the list endpoint serves
        ``num_personas`` / ``num_ticks`` / ``tier_mix`` /
        ``precedent_seed`` without re-parsing the YAML on every read.
        Stored as ``dict[str, Any]`` so future engine fields layer on
        without a schema migration.
      - ``createdAt`` / ``updatedAt`` — inherited from
        :class:`TimestampedDocument`. The TimestampedDocument hook bumps
        ``updatedAt`` on every Save, which the list ordering relies on.
    """

    workspace_id: Indexed(str)  # type: ignore[valid-type]
    name: str = Field(..., min_length=1, max_length=120)
    sub_type: Literal["decision_forecast", "market_sim", "org_change_rehearsal"] = Field(
        default="decision_forecast",
    )
    description: str = Field(default="", max_length=500)
    author: str = Field(default="")
    yaml_body: str = Field(default="")
    parsed_meta: dict[str, Any] = Field(default_factory=dict)

    class Settings:
        name = "foresight_workspace_scenarios"
        indexes = [
            # Most-recent-edit-first listing (the wave-3 UI's left rail).
            [("workspace_id", 1), ("updatedAt", -1)],
            # Filter by sub_type within a workspace (the wave-3 UI's
            # sub-type tabs).
            [("workspace_id", 1), ("sub_type", 1)],
        ]


__all__ = ["ForesightWorkspaceScenario"]
