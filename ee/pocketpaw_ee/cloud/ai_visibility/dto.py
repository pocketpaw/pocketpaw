# AI visibility — DTOs.
#
# Created 2026-10-03 (feat/ai-visibility-core, AV-3): ``CheckResponse``, one stored
# check as ``run_check`` returns it. Requests arrive with the HTTP routes (AV-4 /
# AV-6), which decide what the public wire shows; this is the full internal view.

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class CheckResponse(BaseModel):
    id: str
    workspace_id: str | None
    site_id: str | None
    business: dict[str, Any]
    location: dict[str, Any]
    questions: list[str]
    #: One row per engine x question x sample; failed calls have ``ok=False``.
    runs: list[dict[str, Any]]
    #: Per engine: ``{named, of, failed, near_miss, competitors}``. ``of`` counts
    #: answers received, so "named X of N" never counts a failed call.
    summary: dict[str, Any]
    fix: dict[str, Any]
    total_cost_usd: float
    created_at: datetime | None = None


__all__ = ["CheckResponse"]
