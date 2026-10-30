# src/pocketpaw/bundled_skills/installer.py
# Created: 2026-05-14 (feat/pocket-creator-skill) — auto-installs the
# bundled Claude Code skill files from
# ``src/pocketpaw/bundled_skills/_bundled/skills/<name>/`` into the user's
# ``~/.claude/skills/<name>/`` so the chat agent can invoke them
# without the operator manually staging the files. Idempotent via
# SHA-256 hash comparison.
# Updated: 2026-06-03 (feat/sdk-bundled-skills-plugin) — the bundled skills
# now live under ``_bundled/skills/`` and ``_bundled`` carries a
# ``.claude-plugin/plugin.json`` so the whole directory is a valid Claude
# Code *local plugin*. ``bundled_skills_plugin_dir()`` exposes that path for
# the claude_agent_sdk backend, which passes it via ``plugins=`` — the only
# way bundled skills reach the SDK under ``setting_sources=[]`` (the
# ~/.claude/skills mirror below is invisible to that backend; it serves the
# desktop SkillLoader + non-SDK backends).
"""Auto-install bundled Claude Code skills into the user config dir.

Why this exists
---------------

Claude Code looks for skills in ``~/.claude/skills/<name>/SKILL.md``
(and in cwd-local ``.claude/skills/`` when running from a project
directory). PocketPaw runs from the user's home dir, so cwd-local
skills aren't visible — the chat agent only sees ``~/.claude/skills/``.

We ship the ``pocketpaw-create-pocket`` skill (and any future skills)
inside the Python package at
``src/pocketpaw/bundled_skills/_bundled/<name>/SKILL.md``. On boot,
the installer mirrors those files into ``~/.claude/skills/<name>/``
so the SDK picks them up.

Behavior
--------

- **First boot**: skill file copied to ``~/.claude/skills/<name>/SKILL.md``.
- **Subsequent boots, same content**: no-op (SHA-256 match).
- **PocketPaw upgrade with new skill content**: overwrites the user's
  copy. We don't merge user customizations — if you've edited the
  file by hand, set ``POCKETPAW_AUTO_INSTALL_BUNDLED_SKILLS=false`` to freeze
  your version.
- **Permissions / I/O failures**: logged at WARNING, never raised.
  Skill installation is best-effort — pocket creation still works
  via the MCP tool even when the skill isn't installed.

Opt-out
-------

Set ``POCKETPAW_AUTO_INSTALL_BUNDLED_SKILLS=false`` in the environment to
disable the installer entirely. The MCP-tool flow still works; users
who want the skill behavior can stage the files manually from
``src/pocketpaw/bundled_skills/_bundled/``.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Where the bundled skills live inside the Python package. ``_BUNDLED_DIR``
# is itself a Claude Code *local plugin* (carries ``.claude-plugin/plugin.json``);
# the individual SKILL.md files sit under ``_BUNDLED_DIR/skills/<name>/``.
_BUNDLED_DIR = Path(__file__).parent / "_bundled"
_SKILLS_DIR = _BUNDLED_DIR / "skills"
_PLUGIN_MANIFEST = _BUNDLED_DIR / ".claude-plugin" / "plugin.json"


def bundled_skills_plugin_dir() -> Path | None:
    """Return the bundled-skills local-plugin directory, or ``None``.

    The claude_agent_sdk backend passes this path via the SDK ``plugins=``
    option (``[{"type": "local", "path": <dir>}]``). That is the ONLY way the
    bundled skills reach that backend: it launches with ``setting_sources=[]``
    for persona isolation, which disables the SDK's filesystem skill discovery
    (``~/.claude/skills`` and ``.claude/skills`` are both invisible). A local
    plugin loads regardless of ``setting_sources``, so the skills become
    invokable via both slash command and natural language without leaking the
    rest of ``~/.claude`` (CLAUDE.md, output styles) into the agent.

    Returns the directory only when it actually contains a plugin manifest, so
    a partial/old install can't hand the SDK an invalid plugin path.
    """

    if _PLUGIN_MANIFEST.is_file() and _SKILLS_DIR.is_dir():
        return _BUNDLED_DIR.resolve()
    return None


@dataclass(frozen=True)
class InstallResult:
    """Per-skill install outcome surfaced by ``install_bundled_skills``.

    ``status`` is one of:
      - ``"installed"`` — destination didn't exist; freshly copied.
      - ``"updated"``   — destination existed but content hash differed;
                          overwritten.
      - ``"skipped"``   — destination existed and hash matched; no-op.
      - ``"failed"``    — I/O error during install; details in ``error``.
    """

    name: str
    status: str
    destination: Path
    error: str | None = None


def install_bundled_skills(*, destination_root: Path | None = None) -> list[InstallResult]:
    """Mirror every bundled skill into the user's Claude config.

    Args:
        destination_root: Override the install target — useful for
            tests. Defaults to ``~/.claude/skills/``. When the override
            is supplied, the standard home-dir resolution is skipped
            entirely.

    Returns:
        A list of ``InstallResult``s, one per bundled skill. The list
        is sorted by skill name for deterministic ordering.

    The function never raises. Per-skill failures are caught and
    surfaced via ``InstallResult.status == "failed"`` so a permission
    error on one skill doesn't block install of the others.
    """

    if destination_root is None:
        destination_root = Path.home() / ".claude" / "skills"

    if not _SKILLS_DIR.is_dir():
        logger.warning(
            "bundled_skills.installer: bundled skills dir %s missing — nothing to install",
            _SKILLS_DIR,
        )
        return []

    results: list[InstallResult] = []
    for skill_dir in sorted(p for p in _SKILLS_DIR.iterdir() if p.is_dir()):
        result = _install_one(skill_dir, destination_root)
        results.append(result)
        logger.info(
            "bundled_skills.installer: %s -> %s",
            skill_dir.name,
            result.status,
        )
    return results


def _install_one(skill_src: Path, destination_root: Path) -> InstallResult:
    """Copy a single bundled skill directory into the user's Claude
    config. Used by ``install_bundled_skills`` per directory entry.

    The skill directory is mirrored verbatim — every file under
    ``_bundled/<name>/`` is copied to ``~/.claude/skills/<name>/``
    preserving the subdirectory structure. SHA-256 hash comparison
    decides between ``installed`` / ``updated`` / ``skipped``.
    """

    name = skill_src.name
    dest_dir = destination_root / name

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return InstallResult(
            name=name,
            status="failed",
            destination=dest_dir,
            error=f"mkdir failed: {exc}",
        )

    # The dest_dir was just created if missing; if it pre-existed it
    # may already have files. We track that to disambiguate
    # "installed" vs "updated" in the result.
    dest_existed = any(dest_dir.iterdir())

    any_change = False
    try:
        for src_file in skill_src.rglob("*"):
            if not src_file.is_file():
                continue
            relative = src_file.relative_to(skill_src)
            dest_file = dest_dir / relative
            dest_file.parent.mkdir(parents=True, exist_ok=True)

            if dest_file.exists() and _sha256(dest_file) == _sha256(src_file):
                # Content matches — leave it alone.
                continue
            shutil.copy2(src_file, dest_file)
            any_change = True
    except OSError as exc:
        return InstallResult(
            name=name,
            status="failed",
            destination=dest_dir,
            error=f"copy failed: {exc}",
        )

    if not any_change:
        status = "skipped"
    elif dest_existed:
        status = "updated"
    else:
        status = "installed"
    return InstallResult(name=name, status=status, destination=dest_dir)


def _sha256(path: Path) -> str:
    """Return the SHA-256 hex digest of a file's contents.

    Reads in 64 KB chunks so the function works for skill files of any
    size without holding the whole file in memory. The hash is the
    comparison primitive that decides install / update / skip.
    """

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


__all__ = [
    "InstallResult",
    "bundled_skills_plugin_dir",
    "install_bundled_skills",
]
