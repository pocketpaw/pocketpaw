"""Wire DTOs for the chat-runs router."""

from __future__ import annotations

from pydantic import BaseModel


class StopRunResponse(BaseModel):
    status: str = "ok"
