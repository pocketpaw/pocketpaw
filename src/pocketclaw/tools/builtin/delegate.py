# External Agent Delegation tool — delegate tasks to Claude Code CLI.
# Created: 2026-02-07
# Part of Phase 2 Integration Ecosystem

import logging
from typing import Any

from pocketclaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)


class DelegateToClaudeCodeTool(BaseTool):
    """Delegate a task to Claude Code CLI for autonomous execution."""

    @property
    def name(self) -> str:
        return "delegate_claude_code"

    @property
    def description(self) -> str:
        return (
            "Delegate a complex coding task to Claude Code CLI for autonomous execution. "
            "Claude Code has full access to the filesystem, shell, and web tools. "
            "Use this for tasks that require multi-step file editing, debugging, or project setup. "
            "Requires Claude Code CLI installed (npm install -g @anthropic-ai/claude-code)."
        )

    @property
    def trust_level(self) -> str:
        return "critical"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Detailed task description for Claude Code to execute",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Maximum execution time in seconds (default: 300)",
                    "default": 300,
                },
            },
            "required": ["task"],
        }

    async def execute(self, task: str, timeout: int = 300) -> str:
        from pocketclaw.agents.delegation import ExternalAgentDelegate

        if not ExternalAgentDelegate.is_available("claude"):
            return self._error(
                "Claude Code CLI not found. Install with: npm install -g @anthropic-ai/claude-code"
            )

        result = await ExternalAgentDelegate.run(
            agent="claude",
            prompt=task,
            timeout=float(min(timeout, 600)),
        )

        if result.error:
            return self._error(f"Claude Code error: {result.error}")

        if not result.output:
            return "Claude Code completed with no output."

        # Truncate very long output
        output = result.output
        if len(output) > 10000:
            output = output[:10000] + "\n\n... (truncated)"

        return f"**Claude Code Result:**\n\n{output}"
