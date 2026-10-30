# StudioTemplate Beanie document — a Studio generation published as a template.
#
# Created 2026-10-02 (feat/studio-templates): a FROZEN SNAPSHOT of one Studio
# generation (``StudioGeneration``), never a pointer to it. ``cover`` is copied
# from one of the generation's assets (``url`` stays backend-relative,
# ``/api/v1/media/...``; Discover absolutizes it) and ``recipe`` is the
# generation's ``{kind, model, prompt, params}`` with every input-image field
# stripped (the inputs were the owner's own uploads and never leave the
# workspace). ``uses_input_images`` records that the original run had inputs,
# so a remix knows it must ask for its own.
#
# ``kind`` is the Discover kind: "image" | "video" | "music" (a generation's
# "audio" maps to "music"); ``recipe.kind`` keeps the generation's own kind.
# ``visibility`` / ``hidden`` / ``reports`` mirror ``SiteTemplate``: "private"
# (owner only), "workspace", "public" (listed on Discover unless hidden).
#
# Only ``ee.cloud.studio_templates.service`` and ``service_admin`` import this
# doc (import-linter "StudioTemplates" contract).

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class StudioTemplate(TimestampedDocument):
    """One published Studio generation, scoped to a workspace and owned by one user."""

    workspace: str
    owner: str
    template_type: Literal["generation"] = "generation"
    source_generation_id: str
    kind: Literal["image", "video", "music"]
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    audiences: list[Literal["shop", "design", "everyone", "fun"]] = Field(default_factory=list)
    visibility: Literal["private", "workspace", "public"] = "private"
    # {url (backend-relative), mime, width, height, poster_url}
    cover: dict[str, Any] = Field(default_factory=dict)
    # {kind, model, prompt, params}; params never carry input-image fields.
    recipe: dict[str, Any] = Field(default_factory=dict)
    uses_input_images: bool = False
    hidden: bool = False
    reports: list[dict[str, Any]] = Field(default_factory=list)

    class Settings(TimestampedDocument.Settings):
        name = "studio_templates"
        indexes = [
            IndexModel([("workspace", 1), ("createdAt", -1)]),
            IndexModel([("visibility", 1), ("hidden", 1)]),
        ]
