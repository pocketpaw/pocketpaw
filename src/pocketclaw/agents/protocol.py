"""
Agent Protocol - Abstract interfaces for swappable agent backends.
Created: 2026-02-02
Changes: 2026-02-02 - Added ExecutorProtocol and OrchestratorProtocol for 2-layer architecture.
"""

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, Optional


@dataclass
class AgentEvent:
    """Standardized event from any agent backend.

    Types:
        - "message": Text content from the agent
        - "code": Code block being executed
        - "tool_use": Tool is being invoked
        - "tool_result": Tool execution result
        - "thinking": Extended thinking content (Activity panel only, not sent to channels)
        - "thinking_done": Thinking phase completed
        - "error": Error message
        - "done": Agent finished processing
    """

    type: str
    content: Any
    metadata: dict = field(default_factory=dict)


# =============================================================================
# Layer 1: AgentProtocol - Simple interface for standalone backends
# =============================================================================


class AgentProtocol(Protocol):
    """Interface that all agent backends must implement.

    This allows swapping backends (Open Interpreter, Claude Code, Claude Agent SDK)
    without changing the calling code in dashboard.py or bot_gateway.py.
    """

    async def chat(self, message: str) -> AsyncIterator[AgentEvent]:
        """Process a message and stream events."""
        ...

    async def stop(self) -> None:
        """Gracefully stop the agent execution."""
        ...

    async def get_status(self) -> dict:
        """Get current agent status."""
        ...


# =============================================================================
# Layer 2: ExecutorProtocol - The "Hands" (OS Control)
# =============================================================================


class ExecutorProtocol(Protocol):
    """Interface for execution layer (OS control).

    The executor handles actual system operations:
    - Shell commands
    - File read/write
    - Browser automation

    Implementations: OpenInterpreterExecutor, DockerSandboxExecutor (future)
    """

    async def run_shell(self, command: str) -> str:
        """Execute a shell command and return output."""
        ...

    async def read_file(self, path: str) -> str:
        """Read file contents."""
        ...

    async def write_file(self, path: str, content: str) -> None:
        """Write content to file."""
        ...

    async def list_directory(self, path: str) -> list[str]:
        """List directory contents."""
        ...


# =============================================================================
# Layer 2: OrchestratorProtocol - The "Brain" (Decision Making)
# =============================================================================


class OrchestratorProtocol(Protocol):
    """Interface for orchestration layer (decision making).

    The orchestrator handles:
    - Session management
    - Lifecycle hooks (PreToolUse, PostToolUse)
    - Context compression
    - Tool routing to executor

    Implementations: ClaudeAgentSDKOrchestrator, PocketPawNativeOrchestrator (future)
    """

    def set_executor(self, executor: ExecutorProtocol) -> None:
        """Inject the executor to use for actual commands."""
        ...

    async def process(self, message: str) -> AsyncIterator[AgentEvent]:
        """Process message with full orchestration."""
        ...

    async def stop(self) -> None:
        """Stop the orchestrator."""
        ...
