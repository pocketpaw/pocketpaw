# src/pocketpaw/plugins/installer.py
# Created: 2026-06-07 (feat/plugin-installer-skills) — PluginInstaller:
# adopts the .claude-plugin standard. Parses an "owner/repo" source (also
# "owner/repo/subdir" and GitHub URLs), clones via the shared
# skills.installer.clone_github_repo helper, structurally validates
# .claude-plugin/plugin.json, copies each skills/<name>/SKILL.md into the
# skill loader path, reloads the loader, records a registry entry under
# ~/.pocketpaw/plugins.json, and returns a step-by-step PluginInstallReport.
# Skills-only slice — MCP + list/remove ship separately (#1357, #1358).
"""Install a ``.claude-plugin``'s skills from a GitHub repo.

Each unit of work is a :class:`PluginInstallStep` that never raises out of
the installer — failures become ``failed``/``skipped`` steps so the caller
gets a full report even on partial success. The two genuinely up-front
errors (a malformed source string and a clone failure) raise
:class:`PluginInstallError` so the API layer can map them to 400/404/502.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

from pocketpaw.plugins.models import (
    PluginInstallReport,
    PluginInstallStep,
    PluginManifest,
)
from pocketpaw.security.audit import AuditEvent, AuditSeverity, get_audit_logger
from pocketpaw.skills.installer import (
    INSTALL_DIR,
    _ignore_symlinks,
    clone_github_repo,
)
from pocketpaw.skills.loader import get_skill_loader

logger = logging.getLogger(__name__)

# Registry of installed plugins (read-modify-write JSON).
REGISTRY_PATH = Path.home() / ".pocketpaw" / "plugins.json"

# GitHub naming rules — reused from skills/installer.py validation regexes.
_OWNER_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
_REPO_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
# Skill/subdir path segments: same charset, no traversal.
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Type alias for the clone factory so tests can inject a local fixture dir.
CloneFactory = Callable[..., AbstractAsyncContextManager[Path]]


class PluginInstallError(Exception):
    """Raised for up-front, non-recoverable install errors (bad source,
    clone failure, missing manifest/skills). Carries an HTTP status code so
    the API layer returns 400/404/502 instead of a 500.
    """

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def parse_source(source: str) -> tuple[str, str, str | None]:
    """Parse a plugin source string into ``(owner, repo, subdir)``.

    Accepts:
      * ``owner/repo``
      * ``owner/repo/subdir`` (subdir may itself contain ``/``)
      * a full GitHub URL (``https://github.com/owner/repo`` with an
        optional trailing ``.git`` and/or ``/tree/<ref>/<subdir>``).

    Returns ``(owner, repo, subdir_or_None)``.

    Raises:
        PluginInstallError: If the source is empty or malformed.
    """
    source = (source or "").strip()
    if not source:
        raise PluginInstallError("Missing 'source' field", 400)

    subdir: str | None = None

    # Normalise a GitHub URL down to its path component.
    url_match = re.match(
        r"^(?:https?://)?(?:www\.)?github\.com/(.+)$",
        source,
        flags=re.IGNORECASE,
    )
    if url_match:
        path = url_match.group(1)
        # Drop a /tree/<ref> or /blob/<ref> segment, keeping any subdir after it.
        tree_match = re.match(r"^([^/]+)/([^/]+)/(?:tree|blob)/[^/]+/?(.*)$", path)
        if tree_match:
            owner, repo, subdir_part = tree_match.groups()
            subdir = subdir_part or None
        else:
            parts = path.split("/", 2)
            if len(parts) < 2:
                raise PluginInstallError("Invalid GitHub URL", 400)
            owner, repo = parts[0], parts[1]
            subdir = parts[2] if len(parts) == 3 and parts[2] else None
    else:
        parts = source.split("/", 2)
        if len(parts) < 2:
            raise PluginInstallError("Source must be owner/repo or owner/repo/subdir", 400)
        owner, repo = parts[0], parts[1]
        subdir = parts[2] if len(parts) == 3 and parts[2] else None

    repo = repo[:-4] if repo.endswith(".git") else repo
    if subdir:
        subdir = subdir.strip("/") or None

    if not _OWNER_RE.match(owner):
        raise PluginInstallError("Invalid owner format", 400)
    if not _REPO_RE.match(repo):
        raise PluginInstallError("Invalid repo format", 400)
    if subdir:
        for segment in subdir.split("/"):
            if segment in (".", "..") or not _NAME_RE.match(segment):
                raise PluginInstallError("Invalid subdir format", 400)

    return owner, repo, subdir


class PluginInstaller:
    """Install a ``.claude-plugin``'s skills from a GitHub source."""

    def __init__(
        self,
        *,
        clone_factory: CloneFactory = clone_github_repo,
        install_dir: Path = INSTALL_DIR,
        registry_path: Path = REGISTRY_PATH,
        actor: str = "dashboard_user",
    ):
        self._clone = clone_factory
        self._install_dir = install_dir
        self._registry_path = registry_path
        self._actor = actor

    async def install(self, source: str, *, timeout: float = 60) -> PluginInstallReport:
        """Clone, validate, install skills, reload, and record the plugin.

        Args:
            source: ``owner/repo`` / ``owner/repo/subdir`` / GitHub URL.
            timeout: Clone timeout in seconds.

        Returns:
            A :class:`PluginInstallReport` describing every step.

        Raises:
            PluginInstallError: bad source / clone failure / missing
                manifest / no skills. (Per-skill problems are non-fatal and
                surface as steps, not exceptions.)
        """
        owner, repo, subdir = parse_source(source)

        try:
            clone_cm = self._clone(owner, repo, timeout=timeout)
        except TypeError:
            # Test fixtures may inject a clone factory with no timeout kwarg.
            clone_cm = self._clone(owner, repo)

        try:
            async with clone_cm as repo_root:
                return self._install_from_tree(
                    repo_root=repo_root,
                    subdir=subdir,
                    source=f"{owner}/{repo}",
                )
        except TimeoutError as exc:
            raise PluginInstallError(f"Clone timed out ({timeout:g}s)", 504) from exc
        except RuntimeError as exc:
            logger.warning("Plugin clone failed: %s", exc)
            raise PluginInstallError("Plugin clone failed", 502) from exc

    def _install_from_tree(
        self,
        *,
        repo_root: Path,
        subdir: str | None,
        source: str,
    ) -> PluginInstallReport:
        """Run the install pipeline against an already-cloned working tree."""
        plugin_root = (repo_root / subdir) if subdir else repo_root

        manifest = self._read_manifest(plugin_root)  # raises PluginInstallError on failure
        report = PluginInstallReport(plugin=manifest.name)
        report.steps.append(
            PluginInstallStep(
                name="read_manifest",
                status="succeeded",
                detail=f"{manifest.name} v{manifest.version}",
            )
        )

        skill_sources = self._discover_skills(plugin_root, manifest)
        if not skill_sources:
            raise PluginInstallError("No skills found in plugin", 404)

        installed = self._install_skills(skill_sources, report)

        report.steps.append(self._reload_loader())
        report.steps.append(self._record_registry(manifest, source, installed))
        report.installed_skills = installed

        self._audit(source, manifest, installed, report.succeeded())
        return report

    def _read_manifest(self, plugin_root: Path) -> PluginManifest:
        """Read + structurally validate ``.claude-plugin/plugin.json``.

        Precedent: bundled_skills/installer.py treats the manifest + skills
        dir as the markers of a valid local plugin.
        """
        manifest_path = plugin_root / ".claude-plugin" / "plugin.json"
        if not manifest_path.is_file():
            raise PluginInstallError("No .claude-plugin/plugin.json found", 404)
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PluginInstallError("Invalid plugin.json", 400) from exc
        if not isinstance(raw, dict):
            raise PluginInstallError("plugin.json must be a JSON object", 400)
        try:
            manifest = PluginManifest.model_validate(raw)
        except Exception as exc:  # pydantic ValidationError
            raise PluginInstallError("plugin.json failed validation", 400) from exc
        if not _NAME_RE.match(manifest.name):
            raise PluginInstallError("Invalid plugin name in manifest", 400)
        return manifest

    def _discover_skills(
        self, plugin_root: Path, manifest: PluginManifest
    ) -> list[tuple[str, Path]]:
        """Find ``<skills>/<name>/SKILL.md`` directories.

        Looks under the manifest's ``skills`` path override when set,
        otherwise the conventional ``skills/`` dir, and finally bare
        ``<name>/SKILL.md`` at the plugin root.
        """
        scan_dirs: list[Path] = []
        if manifest.skills:
            scan_dirs.append(plugin_root / manifest.skills)
        else:
            scan_dirs.append(plugin_root / "skills")
            scan_dirs.append(plugin_root)

        found: dict[str, Path] = {}
        for scan_dir in scan_dirs:
            if not scan_dir.is_dir():
                continue
            for item in sorted(scan_dir.iterdir()):
                if not item.is_dir() or not (item / "SKILL.md").is_file():
                    continue
                found.setdefault(item.name, item)
        return list(found.items())

    def _install_skills(
        self, skill_sources: list[tuple[str, Path]], report: PluginInstallReport
    ) -> list[str]:
        """Copy each skill dir into the loader path. One step per skill."""
        self._install_dir.mkdir(parents=True, exist_ok=True)
        installed: list[str] = []
        for name, src_dir in skill_sources:
            step_name = f"skill:{name}"
            if name in (".", "..") or not _NAME_RE.match(name):
                report.steps.append(
                    PluginInstallStep(name=step_name, status="skipped", detail="invalid skill name")
                )
                continue
            try:
                dest = self._install_dir / name
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(src_dir, dest, ignore=_ignore_symlinks)
            except OSError as exc:
                report.steps.append(
                    PluginInstallStep(name=step_name, status="failed", detail=str(exc))
                )
                continue
            installed.append(name)
            report.steps.append(PluginInstallStep(name=step_name, status="succeeded"))
        return installed

    def _reload_loader(self) -> PluginInstallStep:
        """Reload the skill loader so new skills become invocable."""
        try:
            get_skill_loader().reload()
            return PluginInstallStep(name="reload_loader", status="succeeded")
        except Exception as exc:  # never block the install on a reload hiccup
            logger.warning("Skill loader reload failed: %s", exc)
            return PluginInstallStep(name="reload_loader", status="failed", detail=str(exc))

    def _record_registry(
        self, manifest: PluginManifest, source: str, installed: list[str]
    ) -> PluginInstallStep:
        """Write the plugin entry to the registry (read-modify-write)."""
        from datetime import datetime

        try:
            registry = self._load_registry()
            registry[manifest.name] = {
                "version": manifest.version,
                "source": source,
                "skills": installed,
                "installed_at": datetime.now().isoformat(),
            }
            self._registry_path.parent.mkdir(parents=True, exist_ok=True)
            self._registry_path.write_text(
                json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8"
            )
            return PluginInstallStep(name="record_registry", status="succeeded")
        except OSError as exc:
            logger.warning("Plugin registry write failed: %s", exc)
            return PluginInstallStep(name="record_registry", status="failed", detail=str(exc))

    def _load_registry(self) -> dict[str, Any]:
        """Load the registry JSON, returning an empty dict if absent/corrupt."""
        if not self._registry_path.is_file():
            return {}
        try:
            data = json.loads(self._registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Plugin registry unreadable; starting fresh")
            return {}
        return data if isinstance(data, dict) else {}

    def _audit(self, source: str, manifest: PluginManifest, installed: list[str], ok: bool) -> None:
        """Audit-log the install (best-effort; precedent: skills installer)."""
        try:
            get_audit_logger().log(
                AuditEvent.create(
                    severity=AuditSeverity.INFO if ok else AuditSeverity.WARNING,
                    actor=self._actor,
                    action="plugin_install",
                    target=source,
                    status="success" if ok else "partial",
                    plugin=manifest.name,
                    version=manifest.version,
                    installed=installed,
                )
            )
        except Exception:  # audit must never break the install
            logger.warning("Plugin install audit log failed", exc_info=True)


__all__ = [
    "PluginInstallError",
    "PluginInstaller",
    "parse_source",
]
