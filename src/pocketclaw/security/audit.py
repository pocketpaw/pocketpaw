"""
Audit Logging System.
Created: 2026-02-02

This module provides a secure, append-only audit log for all critical agent actions.
It is designed to be immutable and persistent.
"""

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pocketclaw.config import get_settings

logger = logging.getLogger("audit")


class AuditSeverity(str, Enum):
    INFO = "info"  # Normal operation (e.g. reading a file)
    WARNING = "warning"  # Potentially dangerous (e.g. writing a file)
    CRITICAL = "critical"  # High risk (e.g. shell command, deleting file)
    ALERT = "alert"  # Security violation (e.g. blocked command)


@dataclass
class AuditEvent:
    """A single audit log entry."""

    id: str
    timestamp: str
    severity: AuditSeverity
    actor: str  # Who performed the action (user_id or "agent")
    action: str  # What happened (e.g. "tool_execution", "permission_grant")
    target: str  # The object of the action (e.g. "rm -rf /", "network_request")
    status: str  # "allow", "block", "error", "success"
    context: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        severity: AuditSeverity,
        actor: str,
        action: str,
        target: str,
        status: str,
        **context: Any,
    ) -> "AuditEvent":
        return cls(
            id=str(uuid.uuid4()),
            timestamp=datetime.now(tz=UTC).isoformat(),
            severity=severity,
            actor=actor,
            action=action,
            target=target,
            status=status,
            context=context,
        )


class AuditLogger:
    """
    Append-only audit logger.
    Writes to ~/.pocketclaw/audit.log in JSONL format.
    """

    def __init__(self, log_path: Path | None = None):
        if log_path:
            self.log_path = log_path
        else:
            settings = get_settings()
            # Default to adjacent to config file, or explicit audit path
            # Since settings might not have audit_path, we derive it.
            # Assuming settings.config_path is ~/.pocketclaw/config.json
            base_dir = Path.home() / ".pocketclaw"
            base_dir.mkdir(parents=True, exist_ok=True)
            self.log_path = base_dir / "audit.jsonl"

        self._callbacks: list[Callable[[dict], None]] = []

    def on_log(self, callback: Callable[[dict], None]) -> None:
        """Register a callback to be called after each audit log write."""
        self._callbacks.append(callback)

    def log(self, event: AuditEvent) -> None:
        """Write an event to the audit log."""
        try:
            event_dict = asdict(event)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event_dict) + "\n")
            for cb in self._callbacks:
                try:
                    cb(event_dict)
                except Exception:
                    pass
        except Exception as e:
            # Fallback to system logger if audit fails (critical failure)
            logger.critical(f"FAILED TO WRITE AUDIT LOG: {e} | Event: {event}")

    def log_tool_use(
        self,
        tool_name: str,
        params: dict,
        severity: AuditSeverity = AuditSeverity.INFO,
        status: str = "attempt",
    ) -> str:
        """Helper to log tool usage."""
        event = AuditEvent.create(
            severity=severity,
            actor="agent",
            action="tool_use",
            target=tool_name,
            status=status,
            params=params,
        )
        self.log(event)
        return event.id


# Singleton
_audit_logger: AuditLogger | None = None


def get_audit_logger() -> AuditLogger:
    global _audit_logger
    if _audit_logger is None:
        _audit_logger = AuditLogger()
    return _audit_logger
