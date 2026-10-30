# AiVisibilityCheck Beanie document — one "do AI assistants name this business?"
# check.
#
# Created 2026-10-03 (feat/ai-visibility-core, AV-3). Written once by
# ``ai_visibility.service.run_check``. ``workspace`` and ``site_id`` are None for
# an anonymous check (the public free check AV-4 adds). ``runs`` holds one row per
# engine x question x sample: the answer text, cited and consulted URLs, the
# mention judgement and its cost (failed calls are kept with ``ok=False``).
# ``summary`` is per engine ``{named, of, failed, competitors}`` where ``of``
# counts answers actually received. ``fix`` is the one recommended fix.
#
# Only ``ee.cloud.ai_visibility.service`` imports this doc (import-linter
# "AiVisibility" contract).

from __future__ import annotations

from typing import Any

from pydantic import Field
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class AiVisibilityCheck(TimestampedDocument):
    """One stored AI visibility check."""

    workspace: str | None = None
    site_id: str | None = None
    business: dict[str, Any]
    location: dict[str, Any]
    questions: list[str] = Field(default_factory=list)
    runs: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    fix: dict[str, Any] = Field(default_factory=dict)
    total_cost_usd: float = 0.0

    class Settings(TimestampedDocument.Settings):
        name = "ai_visibility_checks"
        indexes = [
            IndexModel([("workspace", 1), ("createdAt", -1)]),
            IndexModel([("site_id", 1), ("createdAt", -1)]),
        ]
