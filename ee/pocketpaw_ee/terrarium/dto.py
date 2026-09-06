# ee/pocketpaw_ee/terrarium/dto.py
#
# Request bodies for the terrarium router. Thin by design: the physics file is
# validated by ``physics.parse_physics`` (which owns every hard rule and the
# error messages), so the DTO only asserts "an object arrived", not its shape.
# Responses are the wire dicts the service builds — there is no response model
# layer, matching the mandates entity.

"""Terrarium request DTOs."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CreateUniverseRequest(BaseModel):
    """``POST /universes`` — a physics file, and whether the world is watchable
    by anonymous viewers (still requires the server-wide public flag)."""

    physics: dict[str, Any]
    public: bool = False


class SpeakRequest(BaseModel):
    """``POST /universes/{id}/speak`` — one line from a human."""

    text: str = Field(min_length=1, max_length=500)


class PledgeRequest(BaseModel):
    """``POST /universes/{id}/weather/pledge`` — tokens toward a god power.
    ``line`` is only read for ``omen``."""

    kind: str
    tokens: int = Field(ge=1, le=10_000)
    line: str | None = Field(default=None, max_length=280)


__all__ = ["CreateUniverseRequest", "PledgeRequest", "SpeakRequest"]
