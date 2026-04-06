"""Shared skill installer -- clone GitHub repos and install SKILL.md directories.

Created: 2026-03-22
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

from pocketpaw.security.audit import AuditEvent, AuditSeverity, get_audit_logger
from pocketpaw.skills.loader import get_skill_loader

logger = logging.getLogger(__name__)

INSTALL_DIR = Path.home() / ".agents" / "skills"


def _ignore_symlinks(src: str, names: list[str]) -> set[str]:
    """Return names that are symlinks so ``shutil.copytree`` skips them."""
    return {n for n in names if os.path.islink(os.path.join(src, n))}


class SkillInstallError(Exception):
    """Raised when skill installation fails."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


async def install_skills_from_github(
    owner: str,
    repo: str,
    skill_name: str | None = None,
    prefix_filter: str | None = None,
    timeout: float = 60,
) -> list[str]:
    """Clone a GitHub repo and install SKILL.md directories.

    Args:
        owner: GitHub owner (e.g. "googleworkspace").
        repo: GitHub repo name (e.g. "cli").
        skill_name: Install only this specific skill (by directory name).
        prefix_filter: Only install skills whose directory name starts with this
            prefix (e.g. "gws-"). Ignored when *skill_name* is set.
        timeout: Git clone timeout in seconds.

    Returns:
        List of installed skill names.

    Raises:
        RuntimeError: If the clone fails or no skills are found.
    """
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "clone",
            "--depth=1",
            f"https://github.com/{owner}/{repo}.git",
            tmpdir,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            raise RuntimeError(f"Clone failed: {err}")

        tmp = Path(tmpdir)
        skill_dirs: list[tuple[str, Path]] = []

        if skill_name:
            # Look for a specific skill by name
            for candidate in [tmp / skill_name, tmp / "skills" / skill_name]:
                if (candidate / "SKILL.md").exists():
                    skill_dirs.append((skill_name, candidate))
                    break
        else:
            # Scan for all skills
            for scan_dir in [tmp, tmp / "skills"]:
                if not scan_dir.is_dir():
                    continue
                for item in sorted(scan_dir.iterdir()):
                    if not item.is_dir() or not (item / "SKILL.md").exists():
                        continue
                    if prefix_filter and not item.name.startswith(prefix_filter):
                        continue
                    skill_dirs.append((item.name, item))

        if not skill_dirs:
            target = skill_name or f"{owner}/{repo}"
            raise RuntimeError(f"No SKILL.md found for '{target}'")

        installed: list[str] = []
        for name, src_dir in skill_dirs:
            # Reject path traversal and validate discovered directory names.
            if name in (".", "..") or not re.match(r"^[a-zA-Z0-9._-]+$", name):
                logger.warning("Skipping skill directory with invalid name: %r", name)
                continue
            dest = INSTALL_DIR / name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src_dir, dest, ignore=_ignore_symlinks)
            installed.append(name)

        logger.info("Installed %d skills from %s/%s", len(installed), owner, repo)
        return installed


async def install_skill_from_source(source: str) -> list[str]:
    """Validate, clone, and install a skill from a GitHub source string.

    High-level entry point with input validation, audit logging, and sanitised
    error messages.  Used by the dashboard and REST API endpoints.

    Args:
        source: GitHub source in format "owner/repo" or "owner/repo/skill_name"

    Returns:
        List of installed skill names.

    Raises:
        SkillInstallError: If validation, cloning, or installation fails.
    """
    source = source.strip()
    if not source:
        raise SkillInstallError("Missing 'source' field", 400)

    parts = source.split("/")
    if len(parts) < 2 or len(parts) > 3:
        raise SkillInstallError("Source must be owner/repo or owner/repo/skill", 400)

    owner, repo = parts[0], parts[1]
    skill_name = parts[2] if len(parts) == 3 else None

    # Whitelist owner/repo to GitHub's actual naming rules.
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$", owner):
        raise SkillInstallError("Invalid owner format", 400)
    if not re.match(r"^[a-zA-Z0-9._-]+$", repo):
        raise SkillInstallError("Invalid repo format", 400)
    if skill_name:
        if skill_name in (".", ".."):
            raise SkillInstallError("Invalid skill name format", 400)
        if not re.match(r"^[a-zA-Z0-9._-]+$", skill_name):
            raise SkillInstallError("Invalid skill name format", 400)

    try:
        installed = await install_skills_from_github(
            owner=owner,
            repo=repo,
            skill_name=skill_name,
            timeout=30,
        )

        if not installed:
            raise SkillInstallError("No valid skill directories found after validation", 400)

        get_audit_logger().log(
            AuditEvent.create(
                severity=AuditSeverity.INFO,
                actor="dashboard_user",
                action="skill_install",
                target=f"{owner}/{repo}",
                status="success",
                installed=installed,
            )
        )

        loader = get_skill_loader()
        loader.reload()
        return installed

    except TimeoutError:
        raise SkillInstallError("Clone timed out (30s)", 504)
    except SkillInstallError:
        raise
    except RuntimeError as exc:
        detail = str(exc)
        if "No SKILL.md" in detail:
            raise SkillInstallError(detail, 404)
        # Log the real error but don't leak internal paths to clients.
        logger.warning("Skill install failed: %s", detail)
        raise SkillInstallError("Skill clone failed", 500)
    except Exception:
        logger.exception("Skill install failed")
        raise SkillInstallError("Skill install failed", 500)
