"""Wire DTOs for the chat-runs router."""

from __future__ import annotations

from pydantic import BaseModel


class StopRunResponse(BaseModel):
    """Response body for ``POST /cloud/chat/runs/{run_id}/stop``."""

    status: str = "ok"
