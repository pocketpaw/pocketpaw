# Tool registry for managing available tools.
# Created: 2026-02-02
# Updated: 2026-02-25 — Strengthen param validation: also reject None for required params.
# Updated: 2026-03-07 — Add per-tool execution timeout (asyncio.wait_for) to prevent
#                        stuck tools from blocking the agent session indefinitely.


from __future__ import annotations

import asyncio
import logging
from typing import Any

from pocketpaw.security import AuditSeverity, get_audit_logger
from pocketpaw.tools.policy import ToolPolicy
from pocketpaw.tools.protocol import ToolProtocol

logger = logging.getLogger(__name__)

# Default timeout (seconds) used when Settings is unavailable (e.g. in tests
# that don't bootstrap the full config stack).  Must match the default in
# ``Settings.tool_timeout``.
_DEFAULT_TOOL_TIMEOUT: int = 60


class ToolRegistry:
    """
    Registry for managing tools.

    Usage:
        registry = ToolRegistry()
        registry.register(ShellTool())
        registry.register(ReadFileTool())

        # Get definitions for LLM
        definitions = registry.get_definitions()

        # Execute a tool
        result = await registry.execute("shell", command="ls -la")
    """

    def __init__(self, policy: ToolPolicy | None = None):
        self._tools: dict[str, ToolProtocol] = {}
        self._policy = policy

    def register(self, tool: ToolProtocol) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool
        logger.debug(f"🔧 Registered tool: {tool.name}")

    def unregister(self, name: str) -> None:
        """Unregister a tool."""
        if name in self._tools:
            del self._tools[name]
            logger.debug(f"🔧 Unregistered tool: {name}")

    def get(self, name: str) -> ToolProtocol | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """Check if tool is registered."""
        return name in self._tools

    def set_policy(self, policy: ToolPolicy) -> None:
        """Set or replace the tool policy."""
        self._policy = policy

    def get_definitions(self, format: str = "openai") -> list[dict[str, Any]]:
        """Get tool definitions, filtered by the active policy.

        Args:
            format: "openai" or "anthropic"

        Returns:
            List of tool definitions in the specified format.
        """
        definitions = []
        for tool in self._tools.values():
            if self._policy and not self._policy.is_tool_allowed(tool.name):
                logger.info("Tool '%s' blocked by policy", tool.name)
                continue
            defn = tool.definition
            if format == "anthropic":
                definitions.append(defn.to_anthropic_schema())
            else:
                definitions.append(defn.to_openai_schema())
        return definitions

    def _get_tool_timeout(self) -> int:
        """Return the configured per-tool timeout in seconds.

        Reads ``Settings.tool_timeout`` at call-time (lazy import) so
        hot-reloaded config changes take effect without restarting the
        process.

        Returns:
            A non-negative ``int``.  ``0`` means "no timeout".
            Falls back to :data:`_DEFAULT_TOOL_TIMEOUT` when Settings
            cannot be loaded (e.g. during isolated unit tests).
        """
        try:
            from pocketpaw.config import get_settings

            value = int(get_settings().tool_timeout or 0)
            return max(value, 0)  # Guard against negative values
        except Exception:
            # Settings unavailable (e.g. during tests) — use sensible default.
            return _DEFAULT_TOOL_TIMEOUT

    async def execute(self, name: str, **params: Any) -> str:
        """Execute a tool by name.

        The call is wrapped in ``asyncio.wait_for`` with a configurable
        per-tool timeout (see ``Settings.tool_timeout``).  If the tool
        does not complete within the limit the coroutine is cancelled and
        a clear error string is returned to the agent loop so it can
        recover gracefully.

        **Timeout precedence:** Individual tools may enforce their own
        internal timeout (e.g. ``ShellTool`` has a 120 s subprocess
        timeout).  This registry-level timeout acts as an upper bound —
        whichever fires first wins.

        Args:
            name: Tool name.
            **params: Tool parameters.

        Returns:
            Tool result as string.
        """
        tool = self._tools.get(name)

        if not tool:
            return f"Error: Tool '{name}' not found. Available: {list(self._tools.keys())}"

        # Policy check
        if self._policy and not self._policy.is_tool_allowed(name):
            logger.warning("Tool '%s' blocked by policy at execution time", name)
            return f"Error: Tool '{name}' is not allowed by the current tool policy."

        # Audit Log: Attempt
        audit = get_audit_logger()

        # Map trust_level to severity
        t_level = getattr(tool, "trust_level", "standard")
        if t_level == "critical":
            severity = AuditSeverity.CRITICAL
        elif t_level == "high":
            severity = AuditSeverity.WARNING
        else:
            severity = AuditSeverity.INFO

        # Basic parameter validation using stdlib
        schema = tool.definition.parameters
        if schema and "required" in schema:
            required_params = schema.get("required", [])
            missing_params = [p for p in required_params if params.get(p) is None]
            if missing_params:
                error_msg = f"Missing required parameter(s): {', '.join(missing_params)}"
                logger.warning("Parameter validation failed for %s: %s", name, error_msg)
                # Log the failed validation attempt to audit
                audit.log_tool_use(name, params, severity=severity, status="validation_failed")
                return f"Error: Tool '{name}' {error_msg}"

        audit.log_tool_use(name, params, severity=severity, status="attempt")

        try:
            logger.debug(f"🔧 Executing {name} with {params}")

            # ── Per-tool timeout guard ──────────────────────────────
            timeout = self._get_tool_timeout()
            if timeout > 0:
                result = await asyncio.wait_for(tool.execute(**params), timeout=timeout)
            else:
                # Timeout disabled — preserve original behaviour.
                result = await tool.execute(**params)
            # ────────────────────────────────────────────────────────

            # Audit Log: Success
            # We don't log full result content in audit to avoid PII, usually
            # But we might log "success" with generic context
            audit.log_tool_use(name, params, severity=severity, status="success")

            # Injection scan on tool results (e.g. web content)
            try:
                from pocketpaw.config import get_settings
                from pocketpaw.security.injection_scanner import get_injection_scanner

                settings = get_settings()
                if settings.injection_scan_enabled and result:
                    scanner = get_injection_scanner()
                    scan = scanner.scan(result, source=f"tool:{name}")
                    if scan.threat_level.value != "none":
                        result = scan.sanitized_content
            except Exception:
                pass  # Don't let scanner errors break tool execution

            # Log truncation to avoid massive log files
            log_result = result[:200] + "..." if len(result) > 200 else result
            logger.debug(f"🔧 {name} result: {log_result}")
            return result

        except asyncio.TimeoutError:  # noqa: UP041 — intentional; explicit asyncio origin
            # ── Timeout handling ────────────────────────────────────
            # asyncio.wait_for() cancels the underlying coroutine before
            # raising TimeoutError.  If the tool ignores CancelledError
            # or performs lengthy cleanup, cancellation may not be
            # immediate — but the agent loop is unblocked regardless.
            timeout = self._get_tool_timeout()
            logger.error("🔧 %s timed out after %ds", name, timeout)
            logger.debug(
                "🔧 Tool '%s' coroutine was cancelled by asyncio.wait_for. "
                "If the tool spawned background tasks they may still be "
                "running — tool authors should honour CancelledError.",
                name,
            )

            # Audit Log: Timeout
            from pocketpaw.security.audit import AuditEvent
            from pocketpaw.security.audit import AuditSeverity as AS

            audit.log(
                AuditEvent.create(
                    severity=AS.WARNING,
                    actor="agent",
                    action="tool_timeout",
                    target=name,
                    status="timeout",
                    timeout_seconds=timeout,
                    params=params,
                )
            )

            return (
                f"Error: Tool '{name}' timed out after {timeout}s. "
                "The operation was cancelled. You may retry with simpler input "
                "or try an alternative approach."
            )
            # ────────────────────────────────────────────────────────

        except asyncio.CancelledError:
            # ── Upstream cancellation (e.g. agent shutdown) ─────────
            # Re-raise so the caller (AgentLoop) can perform its own
            # shutdown logic.  Do NOT swallow this.
            logger.warning("🔧 Tool '%s' execution was cancelled (agent shutdown?).", name)
            raise

        except Exception as e:
            # Audit Log: Error
            from pocketpaw.security.audit import AuditEvent
            from pocketpaw.security.audit import AuditSeverity as AS

            audit.log(
                AuditEvent.create(
                    severity=AS.WARNING,
                    actor="agent",
                    action="tool_error",
                    target=name,
                    status="error",
                    error=str(e),
                    params=params,
                )
            )
            logger.error(f"🔧 {name} failed: {e}")
            return f"Error executing {name}: {str(e)}"

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names (unfiltered)."""
        return list(self._tools.keys())

    @property
    def allowed_tool_names(self) -> list[str]:
        """Get list of tool names that pass the active policy."""
        if not self._policy:
            return self.tool_names
        return [n for n in self._tools if self._policy.is_tool_allowed(n)]

    def __len__(self) -> int:
        return len(self._tools)
