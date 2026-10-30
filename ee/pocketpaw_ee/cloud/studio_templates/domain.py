# Studio templates — domain value object.
#
# Created 2026-10-02 (feat/studio-templates): frozen, built in service.py from a
# StudioTemplate doc. Tenancy (``workspace_id``) and ``owner`` are required with
# no defaults. Reports never enter it.

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class StudioTemplateMeta:
    """A published Studio generation (never its reports)."""

    workspace_id: str
    owner: str
    id: str
    template_type: str
    source_generation_id: str
    kind: str
    title: str
    description: str
    audiences: tuple[str, ...]
    visibility: str
    cover: dict[str, Any] = field(default_factory=dict)
    recipe: dict[str, Any] = field(default_factory=dict)
    uses_input_images: bool = False
    hidden: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


__all__ = ["StudioTemplateMeta"]
