"""Agent configuration document."""

from __future__ import annotations

from beanie import Indexed
from pydantic import BaseModel, Field

from ee.cloud.models.base import TimestampedDocument


class AgentConfig(BaseModel):
    backend: str = "claude_agent_sdk"
    model: str = ""  # empty = use backend default
    system_prompt: str = ""
    tools: list[str] = Field(default_factory=list)
    trust_level: int = Field(default=3, ge=1, le=5)
    temperature: float = Field(default=0.7, ge=0, le=2)
    max_tokens: int = Field(default=4096, ge=1)
    # Soul integration
    soul_enabled: bool = True
    soul_persona: str = ""
    soul_archetype: str = ""
    soul_values: list[str] = Field(default_factory=lambda: ["helpfulness", "accuracy"])
    soul_ocean: dict[str, float] = Field(
        default_factory=lambda: {
            "openness": 0.7,
            "conscientiousness": 0.85,
            "extraversion": 0.5,
            "agreeableness": 0.8,
            "neuroticism": 0.2,
        }
    )


class Agent(TimestampedDocument):
    """Agent configuration (not execution — config only)."""

    workspace: Indexed(str)  # type: ignore[valid-type]
    name: str
    slug: str
    avatar: str = ""
    config: AgentConfig = Field(default_factory=AgentConfig)
    visibility: str = Field(default="private", pattern="^(private|workspace|public)$")
    owner: str  # User ID

    class Settings:
        name = "agents"
        indexes = [
            [("workspace", 1), ("slug", 1)],
        ]
