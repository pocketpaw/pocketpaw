# A2A Protocol: Pydantic models for request/response payloads.
#
# Follows the A2A Protocol specification (v0.2.5+):
# https://google.github.io/A2A/specification/
#
# Task lifecycle: submitted → working → completed | failed | canceled | rejected
# Supports text, file, and data content parts plus artifacts.

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Task states
# ---------------------------------------------------------------------------


class TaskState(enum.StrEnum):
    """A2A task lifecycle states."""

    SUBMITTED = "submitted"
    WORKING = "working"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    INPUT_REQUIRED = "input_required"
    REJECTED = "rejected"
    AUTH_REQUIRED = "auth-required"


# State transition validation
VALID_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.SUBMITTED: {TaskState.WORKING, TaskState.REJECTED, TaskState.CANCELED},
    TaskState.WORKING: {
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CANCELED,
        TaskState.INPUT_REQUIRED,
    },
    TaskState.INPUT_REQUIRED: {TaskState.WORKING, TaskState.CANCELED},
    # Terminal states: no outgoing transitions
    TaskState.COMPLETED: set(),
    TaskState.FAILED: set(),
    TaskState.CANCELED: set(),
    TaskState.REJECTED: set(),
    TaskState.AUTH_REQUIRED: {TaskState.WORKING, TaskState.CANCELED},
}


def validate_transition(from_state: TaskState, to_state: TaskState) -> bool:
    """Check whether a state transition is valid per the A2A spec."""
    allowed = VALID_TRANSITIONS.get(from_state, set())
    return to_state in allowed


# ---------------------------------------------------------------------------
# Content parts (discriminated union on "type")
# ---------------------------------------------------------------------------


class TextPart(BaseModel):
    """A plain-text content part."""

    type: Literal["text"] = "text"
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class FilePart(BaseModel):
    """A file content part (base64 data or URI reference)."""

    type: Literal["file"] = "file"
    name: str | None = None
    media_type: str | None = None
    bytes_data: str | None = None  # base64-encoded
    uri: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DataPart(BaseModel):
    """A structured data content part (arbitrary JSON)."""

    type: Literal["data"] = "data"
    data: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)


Part = Annotated[TextPart | FilePart | DataPart, Field(discriminator="type")]


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


class A2AMessage(BaseModel):
    """A single message in an A2A task conversation."""

    message_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    role: str  # "user" | "agent"
    parts: list[Part]
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Task status & artifacts
# ---------------------------------------------------------------------------


class TaskStatus(BaseModel):
    """Current status of a task."""

    state: TaskState
    message: A2AMessage | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class Artifact(BaseModel):
    """An artifact produced by a task."""

    artifact_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    name: str | None = None
    description: str | None = None
    parts: list[Part] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Task(BaseModel):
    """An A2A task resource."""

    id: str
    context_id: str | None = None
    session_id: str | None = None
    status: TaskStatus
    history: list[A2AMessage] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Request params
# ---------------------------------------------------------------------------


class MessageSendConfiguration(BaseModel):
    """Configuration for message/send requests per A2A v0.2.5 spec."""

    accepted_output_modes: list[str] = Field(default=["text/plain"])
    history_length: int | None = None
    blocking: bool = False


class TaskSendParams(BaseModel):
    """Parameters for message/send and tasks/send."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    context_id: str | None = None
    session_id: str | None = None
    message: A2AMessage
    configuration: MessageSendConfiguration | None = None
    # PocketPaw extension: structured conversation history (preserves role/turn
    # boundaries). Pass prior A2AMessage objects here rather than flattening their
    # parts into the current message. The remote agent needs to distinguish its own
    # previous responses from user turns. Not part of the base A2A spec; remote
    # agents that don't understand this field will simply ignore it.
    history: list[A2AMessage] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskQueryParams(BaseModel):
    """Parameters for tasks/get per A2A v0.2.5 spec."""

    id: str
    history_length: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Agent Card
# ---------------------------------------------------------------------------


class AgentCapabilities(BaseModel):
    """Agent capability flags advertised in the agent card."""

    streaming: bool = True
    push_notifications: bool = False
    state_transition_history: bool = True


class AgentSkill(BaseModel):
    """A skill advertised by an A2A agent."""

    id: str
    name: str
    description: str
    tags: list[str] = Field(default_factory=list)
    input_modes: list[str] = Field(default=["text/plain"])
    output_modes: list[str] = Field(default=["text/plain"])


class AgentCard(BaseModel):
    """A2A Agent Card, advertised at /.well-known/agent.json."""

    name: str
    description: str
    url: str
    version: str
    protocol_version: str = "0.2.5"
    provider: dict[str, Any] | None = None
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    skills: list[AgentSkill] = Field(default_factory=list)
    default_input_modes: list[str] = Field(default=["text/plain"])
    default_output_modes: list[str] = Field(default=["text/plain"])
    supported_interfaces: list[dict[str, Any]] = Field(default_factory=list)
    security_schemes: dict[str, Any] = Field(default_factory=dict)
    security_requirements: list[dict[str, list[str]]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 envelope models
# ---------------------------------------------------------------------------


class JSONRPCRequest(BaseModel):
    """JSON-RPC 2.0 request envelope."""

    jsonrpc: str = "2.0"
    id: int | str | None = None
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class JSONRPCErrorData(BaseModel):
    """JSON-RPC 2.0 error object."""

    code: int
    message: str
    data: Any = None


class JSONRPCResponse(BaseModel):
    """JSON-RPC 2.0 response envelope."""

    jsonrpc: str = "2.0"
    id: int | str | None = None
    result: Any = None
    error: JSONRPCErrorData | None = None


# ---------------------------------------------------------------------------
# Streaming event models
# ---------------------------------------------------------------------------


class TaskStatusUpdateEvent(BaseModel):
    """SSE event for task status changes."""

    task_id: str
    context_id: str | None = None
    status: TaskStatus
    final: bool = False


class TaskArtifactUpdateEvent(BaseModel):
    """SSE event for artifact updates during streaming."""

    task_id: str
    context_id: str | None = None
    artifact: Artifact
    append: bool = False
    last_chunk: bool = False
