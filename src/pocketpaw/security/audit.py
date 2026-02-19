"""
Audit Logging System.
Created: 2026-02-02
Updated: 2026-02-16 — Added optional PII filtering on audit log entries.

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
    Writes to ~/.pocketpaw/audit.log in JSONL format.
    """

    def __init__(self, log_path: Path | None = None):
        if log_path:
            self.log_path = log_path
        else:
            # Default to adjacent to config file
            base_dir = Path.home() / ".pocketpaw"
            base_dir.mkdir(parents=True, exist_ok=True)
            self.log_path = base_dir / "audit.jsonl"

        self._callbacks: list[Callable[[dict], None]] = []
        self._pii_filter_enabled = False
        self._pii_scanner: Any = None

    def enable_pii_filter(self) -> None:
        """Enable PII filtering on audit log entries."""
        from pocketpaw.security.pii import PIIAction, PIIScanner

        self._pii_scanner = PIIScanner(default_action=PIIAction.MASK)
        self._pii_filter_enabled = True

    def _filter_pii(self, event_dict: dict) -> dict:
        """Recursively scan string values in the event dict for PII."""
        if not self._pii_scanner:
            return event_dict

        def _scan_value(v: Any) -> Any:
            if isinstance(v, str):
                result = self._pii_scanner.scan(v)
                return result.sanitized_text if result.has_pii else v
            elif isinstance(v, dict):
                return {k: _scan_value(val) for k, val in v.items()}
            elif isinstance(v, list):
                return [_scan_value(item) for item in v]
            return v

        return _scan_value(event_dict)

    def on_log(self, callback: Callable[[dict], None]) -> None:
        """Register a callback to be called after each audit log write."""
        self._callbacks.append(callback)

    def log(self, event: AuditEvent) -> None:
        """Write an event to the audit log."""
        try:
            event_dict = asdict(event)
            if self._pii_filter_enabled:
                event_dict = self._filter_pii(event_dict)
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
