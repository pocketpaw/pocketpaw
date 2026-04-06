# Skills schemas.
# Created: 2026-02-20

from __future__ import annotations

from pydantic import BaseModel, Field


class SkillInfo(BaseModel):
    name: str
    description: str = ""
    argument_hint: str = ""


class SkillInstallRequest(BaseModel):
    source: str = Field(..., min_length=3, description="owner/repo or owner/repo/skill")


class SkillRemoveRequest(BaseModel):
    name: str = Field(..., min_length=1)
