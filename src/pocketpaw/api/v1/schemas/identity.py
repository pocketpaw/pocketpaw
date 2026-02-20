# Identity schemas.
# Created: 2026-02-20

from __future__ import annotations

from pydantic import BaseModel


class IdentityResponse(BaseModel):
    """Agent identity files."""

    identity_file: str = ""
    soul_file: str = ""
    style_file: str = ""
    instructions_file: str = ""
    user_file: str = ""


class IdentitySaveRequest(BaseModel):
    """Request to update identity files. Only provided fields are updated."""

    identity_file: str | None = None
    soul_file: str | None = None
    style_file: str | None = None
    instructions_file: str | None = None
    user_file: str | None = None


class IdentitySaveResponse(BaseModel):
    """Response from identity save."""

    ok: bool = True
    updated: list[str] = []
