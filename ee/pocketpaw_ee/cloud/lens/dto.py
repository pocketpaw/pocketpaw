# ee/pocketpaw_ee/cloud/lens/dto.py — request bodies for the paw-lens proxy.
# Responses are paw-lens's JSON passed through untouched, so there are no
# response models here.

from __future__ import annotations

from pydantic import BaseModel, Field


class MuteIssueRequest(BaseModel):
    """Body of ``POST /lens/issues/{fingerprint}/mute``."""

    minutes: int = Field(ge=1, le=60 * 24 * 365)
