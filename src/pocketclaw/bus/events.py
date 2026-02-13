"""
Message bus event types.
Created: 2026-02-02
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Channel(str, Enum):
    """Supported communication channels."""

    TELEGRAM = "telegram"
    WEBSOCKET = "websocket"
    CLI = "cli"
    DISCORD = "discord"
    SLACK = "slack"
    WHATSAPP = "whatsapp"
    SIGNAL = "signal"
    MATRIX = "matrix"
    TEAMS = "teams"
    GOOGLE_CHAT = "google_chat"
    WEBHOOK = "webhook"
    SYSTEM = "system"  # Internal (subagents, intentions)


@dataclass(frozen=True)
class InboundMessage:
    """Message received from any channel.

    Immutable dataclass representing an incoming message.
    The session_key uniquely identifies the conversation.
    """

    channel: Channel
    sender_id: str
    chat_id: str
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def session_key(self) -> str:
        """Unique session identifier."""
        return f"{self.channel.value}:{self.chat_id}"

    def with_content(self, new_content: str) -> "InboundMessage":
        """Create a copy with different content."""
        return InboundMessage(
            channel=self.channel,
            sender_id=self.sender_id,
            chat_id=self.chat_id,
            content=new_content,
            timestamp=self.timestamp,
            media=self.media,
            metadata=self.metadata,
        )


@dataclass
class OutboundMessage:
    """Message to send to a channel."""

    channel: Channel
    chat_id: str
    content: str
    reply_to: str | None = None
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    # Streaming support
    is_stream_chunk: bool = False
    is_stream_end: bool = False


@dataclass
class SystemEvent:
    """Internal system events (tool execution, errors, etc.)."""

    event_type: str  # "tool_start", "tool_end", "error", "agent_start", "agent_end"
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
