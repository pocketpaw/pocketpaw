# Settings schemas.
# Created: 2026-02-20

from __future__ import annotations

from pydantic import BaseModel


class SettingsResponse(BaseModel):
    """Current settings (non-secret fields)."""

    # Dynamic dict — settings vary, so we use a generic dict
    pass  # Actual endpoint returns dict directly


class SettingsUpdateRequest(BaseModel):
    """Settings update — only provided fields are changed."""

    settings: dict
