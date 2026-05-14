"""Claude Code SDK skill files bundled and auto-installed by PocketPaw.

Distinct from ``pocketpaw.skills`` (the AgentSkills / skills.sh
ecosystem loaded by PocketPaw's runtime). This module ships
Claude-Code-native skills — markdown files in ``~/.claude/skills/``
that the Claude Code CLI's chat agent picks up automatically. Use
these to bundle heavy guidance (e.g., pocket-creation workflow) that
should only land in the chat agent's context when explicitly invoked,
instead of permanently sitting in the system prompt.

The skill files live in
``src/pocketpaw/claude_skills/_bundled/<skill-name>/SKILL.md`` and
are mirrored to ``~/.claude/skills/<skill-name>/SKILL.md`` on every
PocketPaw boot when ``POCKETPAW_AUTO_INSTALL_SKILLS`` is True
(default). SHA-256 hash comparison handles idempotency — second boot
on the same content is a no-op.

Adding a new skill: drop a directory under ``_bundled/`` with a
``SKILL.md`` inside. No code changes required; the installer
discovers it.
"""

from pocketpaw.claude_skills.installer import (
    InstallResult,
    install_bundled_skills,
)

__all__ = ["InstallResult", "install_bundled_skills"]
