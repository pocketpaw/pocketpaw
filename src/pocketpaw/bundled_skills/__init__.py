"""AgentSkills-format SKILL.md files bundled and auto-installed by PocketPaw.

PocketPaw ships skill files that target the **AgentSkills / skills.sh
ecosystem** (the same SKILL.md format used by ``~/.agents/skills/`` and
``~/.claude/skills/``). They live under
``_bundled/skills/<skill-name>/SKILL.md`` and reach the chat agent by two
*independent* routes, because no single route covers every backend:

1. **Mirror into ``~/.claude/skills/``** (the auto-installer). That path is
   one of the three ``pocketpaw.skills.loader.SKILL_PATHS`` PocketPaw's own
   ``SkillLoader`` scans, so the desktop ``dashboard_ws`` slash-command
   dispatcher resolves them for *every* backend (codex_cli, openai_agents,
   deep_agents, …). It does NOT help the claude_agent_sdk backend — see (2).

2. **Local plugin into the claude_agent_sdk backend** (``plugins=``). The
   default backend launches with ``setting_sources=[]`` for persona
   isolation, which disables the SDK's filesystem skill discovery — the
   ``~/.claude/skills`` mirror from (1) is invisible to it (empirically
   verified 2026-06-03: a slash hits the SDK as an unknown command and the
   run returns with no assistant turn). ``_bundled`` is therefore also a
   valid Claude Code *local plugin* (``.claude-plugin/plugin.json`` +
   ``skills/``); the backend passes it via ``plugins=`` so the skills load
   regardless of ``setting_sources`` — slash AND natural-language — without
   leaking the rest of ``~/.claude`` into the agent. See
   ``bundled_skills_plugin_dir`` and ``settings.sdk_load_bundled_skills``.

This module is intentionally distinct from ``pocketpaw.skills`` —
that's the runtime loader / executor. This module is the **shipping
side**: bundled SKILL.md files + the auto-installer + the plugin entry.

Adding a new bundled skill: drop a directory under
``_bundled/skills/<your-skill>/`` with a ``SKILL.md`` inside. No code
changes required — the installer and the plugin both discover it via
directory iteration.
"""

from pocketpaw.bundled_skills.installer import (
    InstallResult,
    bundled_skills_plugin_dir,
    install_bundled_skills,
)

__all__ = [
    "InstallResult",
    "bundled_skills_plugin_dir",
    "install_bundled_skills",
]
