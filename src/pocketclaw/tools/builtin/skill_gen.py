# Skill Generation tool — create new agent skills at runtime.
# Created: 2026-02-06
# Part of Phase 1 Quick Wins

import logging
import re
from pathlib import Path
from typing import Any

from pocketclaw.config import get_config_dir
from pocketclaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)


def _get_skills_dir() -> Path:
    """Get (and create) the PocketPaw skills directory."""
    d = get_config_dir() / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


_VALID_SKILL_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class CreateSkillTool(BaseTool):
    """Create a new reusable agent skill (SKILL.md)."""

    @property
    def name(self) -> str:
        return "create_skill"

    @property
    def description(self) -> str:
        return (
            "Create a new reusable skill that can be invoked later. "
            "Skills are stored as SKILL.md files with YAML frontmatter. "
            "After creation the skill is immediately available."
        )

    @property
    def trust_level(self) -> str:
        return "high"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": (
                        "Unique skill identifier (lowercase, hyphens/underscores ok). "
                        "Example: 'summarize-pr'"
                    ),
                },
                "description": {
                    "type": "string",
                    "description": "Short human-readable description of the skill",
                },
                "instructions": {
                    "type": "string",
                    "description": "The full markdown instructions for the skill",
                },
                "allowed_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of tool names the skill is allowed to use",
                },
                "user_invocable": {
                    "type": "boolean",
                    "description": "Whether users can invoke this skill directly (default: true)",
                    "default": True,
                },
            },
            "required": ["skill_name", "description", "instructions"],
        }

    async def execute(
        self,
        skill_name: str,
        description: str,
        instructions: str,
        allowed_tools: list[str] | None = None,
        user_invocable: bool = True,
    ) -> str:
        """Create a SKILL.md file and reload the skill loader."""
        # Validate name
        if not _VALID_SKILL_NAME.match(skill_name):
            return self._error(
                f"Invalid skill name '{skill_name}'. "
                "Must be lowercase alphanumeric with hyphens/underscores, 1-64 chars."
            )

        skills_dir = _get_skills_dir()
        skill_dir = skills_dir / skill_name

        # Overwrite protection
        skill_file = skill_dir / "SKILL.md"
        if skill_file.exists():
            return self._error(
                f"Skill '{skill_name}' already exists at {skill_file}. "
                "Delete it first if you want to recreate."
            )

        # Build YAML frontmatter
        fm_lines = [
            "---",
            f"name: {skill_name}",
            f"description: {description}",
            f"user-invocable: {str(user_invocable).lower()}",
        ]
        if allowed_tools:
            fm_lines.append("allowed-tools:")
            for t in allowed_tools:
                fm_lines.append(f"  - {t}")
        fm_lines.append("---")

        content = "\n".join(fm_lines) + "\n\n" + instructions.strip() + "\n"

        # Write file
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file.write_text(content)

        # Reload skill loader so the new skill is immediately available
        try:
            from pocketclaw.skills import get_skill_loader

            loader = get_skill_loader()
            loader.reload()
        except Exception as e:
            logger.warning("Could not reload skill loader: %s", e)

        logger.info("Created skill '%s' at %s", skill_name, skill_file)
        return (
            f"Skill '{skill_name}' created successfully.\n"
            f"Path: {skill_file}\n"
            f"Description: {description}\n"
            f"User invocable: {user_invocable}"
        )
