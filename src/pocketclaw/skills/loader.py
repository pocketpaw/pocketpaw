"""
SkillLoader - Load and parse skills from the AgentSkills ecosystem.

Skills are loaded from:
1. ~/.agents/skills/ - Central location (installed via `npx skills add`)
2. ~/.pocketclaw/skills/ - PocketPaw-specific skills

Skills follow the AgentSkills spec: a directory with SKILL.md containing
YAML frontmatter and markdown instructions.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

# Skill search paths in priority order (later overrides earlier)
SKILL_PATHS = [
    Path.home() / ".agents" / "skills",  # From skills.sh (central)
    Path.home() / ".pocketclaw" / "skills",  # PocketPaw-specific
]


@dataclass
class Skill:
    """Represents a loaded skill."""

    name: str
    description: str
    content: str
    path: Path

    # Optional frontmatter fields
    user_invocable: bool = True
    disable_model_invocation: bool = False
    argument_hint: Optional[str] = None
    allowed_tools: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def build_prompt(self, args: str = "") -> str:
        """
        Build the prompt to send to the agent.

        Substitutes argument placeholders:
        - $ARGUMENTS or $0, $1, $2... for positional args
        """
        prompt = self.content

        # Split args
        arg_list = args.split() if args else []

        # Replace $ARGUMENTS with full args string
        prompt = prompt.replace("$ARGUMENTS", args)

        # Replace positional $0, $1, $2...
        for i, arg in enumerate(arg_list):
            prompt = prompt.replace(f"${i}", arg)

        return prompt


def parse_skill_md(skill_path: Path) -> Optional[Skill]:
    """
    Parse a SKILL.md file into a Skill object.

    Args:
        skill_path: Path to SKILL.md file

    Returns:
        Skill object or None if parsing fails
    """
    try:
        text = skill_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error(f"Failed to read {skill_path}: {e}")
        return None

    # Extract YAML frontmatter between --- markers
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)

    if not match:
        logger.warning(f"No frontmatter found in {skill_path}")
        return None

    try:
        frontmatter = yaml.safe_load(match.group(1)) or {}
        content = match.group(2).strip()
    except yaml.YAMLError as e:
        logger.error(f"Failed to parse YAML in {skill_path}: {e}")
        return None

    # Extract required fields
    name = frontmatter.get("name")
    if not name:
        # Use directory name as fallback
        name = skill_path.parent.name

    description = frontmatter.get("description", "")

    # Build skill object
    return Skill(
        name=name,
        description=description,
        content=content,
        path=skill_path,
        user_invocable=frontmatter.get("user-invocable", True),
        disable_model_invocation=frontmatter.get("disable-model-invocation", False),
        argument_hint=frontmatter.get("argument-hint"),
        allowed_tools=frontmatter.get("allowed-tools", []),
        metadata=frontmatter.get("metadata", {}),
    )


class SkillLoader:
    """
    Loads skills from configured paths.

    Supports hot-reloading when skills change on disk.
    """

    def __init__(self, extra_paths: Optional[list[Path]] = None):
        """
        Initialize the skill loader.

        Args:
            extra_paths: Additional paths to search for skills
        """
        self.paths = SKILL_PATHS.copy()
        if extra_paths:
            self.paths.extend(extra_paths)

        self._skills: dict[str, Skill] = {}
        self._loaded = False

    def load(self, force: bool = False) -> dict[str, Skill]:
        """
        Load all skills from configured paths.

        Args:
            force: Force reload even if already loaded

        Returns:
            Dict mapping skill names to Skill objects
        """
        if self._loaded and not force:
            return self._skills

        self._skills = {}

        for base_path in self.paths:
            if not base_path.exists():
                continue

            logger.debug(f"Scanning for skills in {base_path}")

            for item in base_path.iterdir():
                # Handle both directories and symlinks to directories
                if not item.is_dir():
                    continue

                skill_md = item / "SKILL.md"
                if not skill_md.exists():
                    continue

                skill = parse_skill_md(skill_md)
                if skill:
                    # Later paths override earlier (priority order)
                    self._skills[skill.name] = skill
                    logger.debug(f"Loaded skill: {skill.name}")

        self._loaded = True
        logger.info(f"Loaded {len(self._skills)} skills")

        return self._skills

    def reload(self) -> dict[str, Skill]:
        """Force reload all skills."""
        return self.load(force=True)

    def get(self, name: str) -> Optional[Skill]:
        """
        Get a skill by name.

        Args:
            name: Skill name (e.g., "find-skills")

        Returns:
            Skill object or None if not found
        """
        if not self._loaded:
            self.load()

        return self._skills.get(name)

    def get_all(self) -> dict[str, Skill]:
        """Get all loaded skills."""
        if not self._loaded:
            self.load()

        return self._skills.copy()

    def get_invocable(self) -> list[Skill]:
        """Get all user-invocable skills (for slash commands)."""
        if not self._loaded:
            self.load()

        return [s for s in self._skills.values() if s.user_invocable]

    def search(self, query: str = "") -> list[Skill]:
        """Search user-invocable skills by name and description.

        Args:
            query: Case-insensitive substring to match against name + description.
                   Empty string returns all invocable skills.

        Returns:
            List of matching Skill objects.
        """
        invocable = self.get_invocable()
        if not query:
            return invocable
        q = query.lower()
        return [s for s in invocable if q in s.name.lower() or q in s.description.lower()]

    def list_names(self) -> list[str]:
        """Get list of all skill names."""
        if not self._loaded:
            self.load()

        return list(self._skills.keys())


# Singleton instance
_skill_loader: Optional[SkillLoader] = None


def get_skill_loader() -> SkillLoader:
    """Get the singleton SkillLoader instance."""
    global _skill_loader
    if _skill_loader is None:
        _skill_loader = SkillLoader()
    return _skill_loader


def load_all_skills() -> dict[str, Skill]:
    """Convenience function to load all skills."""
    return get_skill_loader().load()
