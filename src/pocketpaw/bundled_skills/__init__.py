"""AgentSkills-format SKILL.md files bundled and auto-installed by PocketPaw.

PocketPaw ships skill files that target the **AgentSkills / skills.sh
ecosystem** (the same SKILL.md format used by ``~/.agents/skills/`` and
``~/.claude/skills/``). The bundled files are mirrored to
``~/.claude/skills/<skill-name>/SKILL.md`` on every PocketPaw boot —
that location is one of the three paths PocketPaw's own
``SkillLoader`` scans (see ``pocketpaw.skills.loader.SKILL_PATHS``), so
the slash-command dispatcher in ``dashboard_ws`` picks them up
regardless of which chat backend is configured (claude_agent_sdk,
codex_cli, openai_agents, deep_agents, …).

Additionally, the Claude Code SDK auto-discovers skills at
``~/.claude/skills/`` natively, so claude_agent_sdk users get the
skill on natural-language invocation (the agent recognises intent and
loads the skill body into the conversation) without needing the user
to type a slash command. Other backends require the ``/<skill-name>``
slash-command invocation through PocketPaw's chat UI.

This module is intentionally distinct from ``pocketpaw.skills`` —
that's the runtime loader / executor. This module is the **shipping
side**: bundled SKILL.md files + the auto-installer that lays them
into the user's home directory on boot.

Adding a new bundled skill: drop a directory under ``_bundled/<your-skill>/``
with a ``SKILL.md`` inside. No code changes required — the installer
discovers it via directory iteration.
"""

from pocketpaw.bundled_skills.installer import (
    InstallResult,
    install_bundled_skills,
)

__all__ = ["InstallResult", "install_bundled_skills"]
