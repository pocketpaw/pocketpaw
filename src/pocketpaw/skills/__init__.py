"""
PocketPaw Skills Module

Integrates with the AgentSkills ecosystem (skills.sh).
Loads skills from ~/.agents/skills/ and ~/.pocketpaw/skills/
"""

from .executor import SkillExecutor
from .installer import SkillInstallError, install_skill_from_source, install_skills_from_github
from .loader import SkillLoader, get_skill_loader, load_all_skills

__all__ = [
    "SkillLoader",
    "SkillInstallError",
    "get_skill_loader",
    "install_skill_from_source",
    "install_skills_from_github",
    "load_all_skills",
    "SkillExecutor",
]
