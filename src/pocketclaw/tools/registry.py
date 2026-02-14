# Tool registry for managing available tools.
# Created: 2026-02-02


from __future__ import annotations

import logging
from typing import Any

from pocketclaw.security import AuditSeverity, get_audit_logger
from pocketclaw.tools.policy import ToolPolicy
from pocketclaw.tools.protocol import ToolProtocol

logger = logging.getLogger(__name__)


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
            format: "openai", "anthropic", or "gemini"

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
            elif format == "gemini":
                definitions.append(defn.to_gemini_schema())
            else:
                definitions.append(defn.to_openai_schema())
        return definitions

    async def execute(self, name: str, **params: Any) -> str:
        """Execute a tool by name.

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

        audit.log_tool_use(name, params, severity=severity, status="attempt")

        try:
            logger.debug(f"🔧 Executing {name} with {params}")
            result = await tool.execute(**params)

            # Audit Log: Success
            # We don't log full result content in audit to avoid PII, usually
            # But we might log "success" with generic context
            audit.log_tool_use(name, params, severity=severity, status="success")

            # Injection scan on tool results (e.g. web content)
            try:
                from pocketclaw.config import get_settings
                from pocketclaw.security.injection_scanner import get_injection_scanner

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
        except Exception as e:
            # Audit Log: Error
            from pocketclaw.security.audit import AuditEvent
            from pocketclaw.security.audit import AuditSeverity as AS

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
