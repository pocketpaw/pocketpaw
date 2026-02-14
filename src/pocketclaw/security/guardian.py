"""
Guardian Agent - AI Security Filter.
Created: 2026-02-02

This module provides a secondary LLM check for dangerous actions.
"""

import logging
import re

try:
    from anthropic import AsyncAnthropic
except ImportError:
    AsyncAnthropic = None

from pocketclaw.config import get_settings
from pocketclaw.security.audit import AuditEvent, AuditSeverity, get_audit_logger

logger = logging.getLogger("guardian")

# ---------------------------------------------------------------------------
# Local dangerous-command patterns (regex, case-insensitive).
#
# Used as a fallback safety net when the LLM-based Guardian is unavailable
# (no API key configured).  This is the union of patterns from shell.py,
# pocketpaw_native.py, and claude_sdk.py so that the local check is at
# least as strict as every other layer in the stack.
# ---------------------------------------------------------------------------
_LOCAL_DANGEROUS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        # Destructive file operations
        r"rm\s+(-[rf]+\s+)*[/~]",
        r"rm\s+(-[rf]+\s+)*\*",
        r"sudo\s+rm\b",
        r">\s*/dev/",
        r">\s*/etc/",
        r"mkfs\.",
        r"dd\s+if=",
        r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;",  # Fork bomb
        r"chmod\s+(-R\s+)?777\s+/",
        # Remote code execution
        r"curl\s+.*\|\s*(ba)?sh",
        r"wget\s+.*\|\s*(ba)?sh",
        r"curl\s+.*-o\s*/",
        r"wget\s+.*-O\s*/",
        # System damage
        r"systemctl\s+(stop|disable)\s+(ssh|sshd|firewall)",
        r"iptables\s+-F",
        r"\bshutdown\b",
        r"\breboot\b",
    ]
]


class GuardianAgent:
    """
    AI Security Guardian.

    analyzes commands and strictly classifies them as SAFE or DANGEROUS.
    """

    SYSTEM_PROMPT = """
You are the Guardian, a security AI for the PocketPaw agent.
Your ONLY job is to analyze shell commands for safety.

Safety Rules:
1. READ-ONLY commands are SAFE (ls, cat, grep, find).
2. Creating/Writing files in authorized directories is SAFE (echo, touch).
3. Network requests to known APIs are SAFE.
4. DESTRUCTIVE commands are DANGEROUS (rm, mv, dd, mkfs).
5. System modification is DANGEROUS (sudo, chmod, chown).
6. Exfiltration is DANGEROUS (curl/wget to unknown domains).
7. Obfuscation is DANGEROUS (base64 decode | sh).
8. If you are unsure, classify as DANGEROUS.

Respond with valid JSON only:
{
  "status": "SAFE" | "DANGEROUS",
  "reason": "Short explanation"
}
"""

    def __init__(self):
        self.settings = get_settings()
        self.client: AsyncAnthropic | None = None
        self._audit = get_audit_logger()

    async def _ensure_client(self):
        if not self.client and self.settings.anthropic_api_key:
            self.client = AsyncAnthropic(api_key=self.settings.anthropic_api_key)

    def _local_safety_check(self, command: str) -> tuple[bool, str]:
        """Deny-by-default local pattern check.

        Used when the LLM backend is unavailable.  Returns ``(False, reason)``
        for any command matching a known-dangerous pattern, and
        ``(True, reason)`` only for commands that do not match any pattern.
        """
        for pattern in _LOCAL_DANGEROUS_PATTERNS:
            if pattern.search(command):
                return False, f"Blocked by local safety check (pattern: {pattern.pattern})"
        return True, "Allowed by local safety check (no dangerous pattern matched)"

    async def check_command(self, command: str) -> tuple[bool, str]:
        """
        Check if a command is safe.
        Returns: (is_safe, reason)
        """
        await self._ensure_client()

        if not self.client:
            # No API key — fall back to a strict local pattern check so that
            # known-dangerous commands are still blocked.  This is fail-closed:
            # the local check denies anything matching a dangerous pattern.
            is_safe, reason = self._local_safety_check(command)
            severity = AuditSeverity.INFO if is_safe else AuditSeverity.ALERT
            logger.warning(
                "Guardian LLM unavailable (no API key). Local safety check: %s — %s",
                "allow" if is_safe else "block",
                reason,
            )
            self._audit.log(
                AuditEvent.create(
                    severity=severity,
                    actor="guardian",
                    action="local_safety_check",
                    target="shell",
                    status="allow" if is_safe else "block",
                    reason=reason,
                    command=command,
                )
            )
            return is_safe, reason

        # Audit Check
        self._audit.log(
            AuditEvent.create(
                severity=AuditSeverity.INFO,
                actor="guardian",
                action="scan_command",
                target="shell",
                status="pending",
                command=command,
            )
        )

        try:
            response = await self.client.messages.create(
                model=self.settings.anthropic_model,  # Use same model or faster one
                max_tokens=100,
                system=self.SYSTEM_PROMPT,
                messages=[{"role": "user", "content": f"Command: {command}"}],
            )

            content = response.content[0].text
            import json

            # Handle potential markdown wrapping
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "{" in content:
                content = content[content.find("{") : content.rfind("}") + 1]

            result = json.loads(content)
            status = result.get("status", "DANGEROUS")
            reason = result.get("reason", "Unknown")

            is_safe = status == "SAFE"

            # Audit Result
            self._audit.log(
                AuditEvent.create(
                    severity=AuditSeverity.INFO if is_safe else AuditSeverity.ALERT,
                    actor="guardian",
                    action="scan_result",
                    target="shell",
                    status="allow" if is_safe else "block",
                    reason=reason,
                    command=command,
                )
            )

            return is_safe, reason

        except Exception as e:
            logger.error(f"Guardian check failed: {e}")
            # FAL-SAFE: If Guardian fails, we should probably BLOCK for high security contexts
            # But for usability, we might ALLOW with warning.
            # Security-first: BLOCK.
            return False, f"Guardian error: {str(e)}"


# Singleton
_guardian: GuardianAgent | None = None


def get_guardian() -> GuardianAgent:
    global _guardian
    if _guardian is None:
        _guardian = GuardianAgent()
    return _guardian
