# Tool protocol - simple, string-based tool interface.
# Created: 2026-02-02


from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class ToolDefinition:
    """Tool definition for LLM function calling."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema
    trust_level: str = "standard"  # standard, high, critical

    def to_openai_schema(self) -> dict[str, Any]:
        """Convert to OpenAI function calling format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_anthropic_schema(self) -> dict[str, Any]:
        """Convert to Anthropic tool format."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

    def to_gemini_schema(self) -> dict[str, Any]:
        """Convert to Google Gemini function calling format."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


class ToolProtocol(Protocol):
    """Protocol for tools.

    Tools are simple: they take parameters and return a string result.
    No streaming, no complex event types.
    """

    @property
    def name(self) -> str:
        """Tool name (used in function calls)."""
        ...

    @property
    def definition(self) -> ToolDefinition:
        """Tool definition for LLM."""
        ...

    async def execute(self, **params: Any) -> str:
        """Execute the tool with given parameters.

        Returns:
            String result (success or error message).
        """
        ...


class BaseTool(ABC):
    """Base class for tools with common functionality."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Tool name."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Tool description for LLM."""
        ...

    @property
    def trust_level(self) -> str:
        """Required trust level to use this tool."""
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        """Parameter schema. Override in subclass."""
        return {"type": "object", "properties": {}, "required": []}

    @property
    def definition(self) -> ToolDefinition:
        """Get the tool definition."""
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
            trust_level=self.trust_level,
        )

    @abstractmethod
    async def execute(self, **params: Any) -> str:
        """Execute the tool."""
        ...

    def _error(self, message: str) -> str:
        """Format an error response."""
        return f"Error: {message}"

    def _success(self, message: str) -> str:
        """Format a success response."""
        return message
