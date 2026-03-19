# Agent-to-Agent (A2A) Protocol support for PocketPaw.
#
# Phase 1 — A2A Server: exposes PocketPaw as a remote A2A-compatible agent.
# Phase 2 — A2A Client: allows PocketPaw to delegate tasks to external agents.
# Phase 3 — A2A Registry: multi-agent orchestration and dashboard UI.

from pocketpaw.a2a.errors import JSONRPCError
from pocketpaw.a2a.models import (
    A2AMessage,
    AgentCard,
    AgentSkill,
    Artifact,
    DataPart,
    FilePart,
    JSONRPCRequest,
    JSONRPCResponse,
    Part,
    Task,
    TaskArtifactUpdateEvent,
    TaskSendParams,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
)
from pocketpaw.a2a.server import register_routes

__all__ = [
    "A2AMessage",
    "AgentCard",
    "AgentSkill",
    "Artifact",
    "DataPart",
    "FilePart",
    "JSONRPCError",
    "JSONRPCRequest",
    "JSONRPCResponse",
    "Part",
    "Task",
    "TaskArtifactUpdateEvent",
    "TaskSendParams",
    "TaskState",
    "TaskStatus",
    "TaskStatusUpdateEvent",
    "TextPart",
    "register_routes",
]
