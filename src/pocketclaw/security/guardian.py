"""
Guardian Agent - AI Security Filter.
Created: 2026-02-02

This module provides a secondary LLM check for dangerous actions.
"""

import logging

try:
    from anthropic import AsyncAnthropic
except ImportError:
    AsyncAnthropic = None

from pocketclaw.config import get_settings
from pocketclaw.security.audit import AuditEvent, AuditSeverity, get_audit_logger

logger = logging.getLogger("guardian")


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

    async def check_command(self, command: str) -> tuple[bool, str]:
        """
        Check if a command is safe.
        Returns: (is_safe, reason)
        """
        await self._ensure_client()

        if not self.client:
            # No API key = Guardian not configured. Log a one-time warning
            # and allow, but record it in the audit trail so admins notice.
            logger.warning("Guardian disabled (no API key). Allowing command.")
            self._audit.log(
                AuditEvent.create(
                    severity=AuditSeverity.ALERT,
                    actor="guardian",
                    action="scan_command",
                    target="shell",
                    status="allow",
                    reason="No API key — Guardian inactive",
                    command=command,
                )
            )
            return True, "Guardian disabled (no API key)"

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
