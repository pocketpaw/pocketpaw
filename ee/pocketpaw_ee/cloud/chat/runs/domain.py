"""Value objects for chat runs. RunSpec is the JSON-serializable payload
handed to a RunExecutor — it must survive an arq pickle round-trip, so it
holds only primitives."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class RunSpec(BaseModel):
    """Everything execute_run() needs, decoupled from the HTTP request."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    workspace_id: str
    context_type: str
    scope_id: str
    session_key: str
    group: str | None
    user_id: str
    agent_id: str
    client_message_id: str
    user_message_id: str
    content: str
    history: list[dict[str, str]]
    intent: str | None
    attachments: list[dict[str, Any]] = []
    mentions: list[str] = []
    reply_to: str | None = None
