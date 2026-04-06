"""
SkillExecutor - Execute skills through the AgentRouter.

Handles:
1. Building the prompt from skill content + user args
2. Running through the configured agent (Open Interpreter / Claude Code)
3. Streaming results back
"""

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from ..agents.router import AgentRouter
from ..config import Settings, get_settings
from .loader import Skill, SkillLoader, get_skill_loader

logger = logging.getLogger(__name__)


class SkillExecutor:
    """
    Executes skills through the agent backend.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        skill_loader: SkillLoader | None = None,
    ):
        """
        Initialize the executor.

        Args:
            settings: Settings instance (uses singleton if not provided)
            skill_loader: SkillLoader instance (uses singleton if not provided)
        """
        self.settings = settings or get_settings()
        self.skill_loader = skill_loader or get_skill_loader()

        # Agent router (created lazily)
        self._agent_router: AgentRouter | None = None

    def _get_agent_router(self) -> AgentRouter:
        """Get or create the agent router."""
        if self._agent_router is None:
            self._agent_router = AgentRouter(self.settings)
        return self._agent_router

    async def execute(
        self,
        skill_name: str,
        args: str = "",
    ) -> AsyncIterator[dict]:
        """
        Execute a skill by name.

        Args:
            skill_name: Name of the skill to execute
            args: Arguments to pass to the skill

        Yields:
            Chunks from the agent execution
        """
        # Load skill
        skill = self.skill_loader.get(skill_name)

        if not skill:
            yield {
                "type": "error",
                "content": f"Skill not found: {skill_name}",
            }
            return

        async for chunk in self.execute_skill(skill, args):
            yield chunk

    async def execute_skill(
        self,
        skill: Skill,
        args: str = "",
    ) -> AsyncIterator[dict]:
        """
        Execute a skill object.

        Args:
            skill: Skill object to execute
            args: Arguments to pass to the skill

        Yields:
            Chunks from the agent execution
        """
        logger.info(f"Executing skill: {skill.name} with args: {args}")

        # Notify start
        yield {
            "type": "skill_started",
            "skill_name": skill.name,
            "args": args,
            "timestamp": datetime.now(tz=UTC).isoformat(),
        }

        try:
            # Build prompt
            prompt = skill.build_prompt(args)

            # Prepend skill context
            full_prompt = f"""You are executing the "{skill.name}" skill.

{skill.description}

---

{prompt}

---

User request: {args if args else "(no additional input)"}
"""

            logger.debug(f"Skill prompt: {full_prompt[:200]}...")

            # Execute through agent
            agent = self._get_agent_router()

            async for chunk in agent.run(full_prompt):
                yield chunk

            # Notify completion
            yield {
                "type": "skill_completed",
                "skill_name": skill.name,
                "timestamp": datetime.now(tz=UTC).isoformat(),
            }

        except Exception as e:
            logger.error(f"Error executing skill {skill.name}: {e}")

            yield {
                "type": "skill_error",
                "skill_name": skill.name,
                "error": str(e),
                "timestamp": datetime.now(tz=UTC).isoformat(),
            }

    def reset_agent(self) -> None:
        """Reset the agent router (e.g., after settings change)."""
        self._agent_router = None
        logger.info("Agent router reset")

    def list_skills(self) -> list[dict]:
        """
        List all available skills.

        Returns:
            List of skill info dicts
        """
        skills = self.skill_loader.get_invocable()

        return [
            {
                "name": s.name,
                "description": s.description,
                "argument_hint": s.argument_hint,
                "path": str(s.path),
            }
            for s in skills
        ]


# Singleton instance
_skill_executor: SkillExecutor | None = None


def get_skill_executor() -> SkillExecutor:
    """Get the singleton SkillExecutor instance."""
    global _skill_executor
    if _skill_executor is None:
        _skill_executor = SkillExecutor()
    return _skill_executor
