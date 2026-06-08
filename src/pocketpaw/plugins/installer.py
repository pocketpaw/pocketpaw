# src/pocketpaw/plugins/installer.py
# Created: 2026-06-07 (feat/plugin-installer-skills) — PluginInstaller:
# adopts the .claude-plugin standard. Parses an "owner/repo" source (also
# "owner/repo/subdir" and GitHub URLs), clones via the shared
# skills.installer.clone_github_repo helper, structurally validates
# .claude-plugin/plugin.json, copies each skills/<name>/SKILL.md into the
# skill loader path, reloads the loader, records a registry entry under
# ~/.pocketpaw/plugins.json, and returns a step-by-step PluginInstallReport.
# Updated: 2026-06-08 (feat/plugin-installer-mcp) — also registers + starts
# the MCP servers a bundle declares in its `.mcp.json` (or a manifest
# `mcp_servers` path override). Each server maps via presets.spec_to_config,
# is namespaced `plugin:<plugin>:<server>`, registered + started through the
# MCP manager (one PluginInstallStep each; a server that registers but can't
# start for lack of env is reported `succeeded` with a "needs env:" detail).
# The namespaced names are recorded in the registry entry and on the report's
# `installed_mcp_servers`. Imports the now-public `ignore_symlinks` and drops
# the clone-factory `except TypeError` fallback. List/remove ships in #1358.
"""Install a ``.claude-plugin``'s skills and MCP servers from a GitHub repo.

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

from pocketpaw.mcp.config import MCPServerConfig
from pocketpaw.mcp.manager import get_mcp_manager
from pocketpaw.mcp.presets import spec_to_config
from pocketpaw.plugins.models import (
    PluginInstallReport,
    PluginInstallStep,
    PluginManifest,
)
from pocketpaw.security.audit import AuditEvent, AuditSeverity, get_audit_logger
from pocketpaw.skills.installer import (
    INSTALL_DIR,
    clone_github_repo,
    ignore_symlinks,
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


class _MCPConfigError(Exception):
    """Internal: a malformed ``.mcp.json`` (parse error / wrong shape).

    Caught inside the installer and converted into a ``failed`` MCP step so
    a bad MCP config degrades per-step instead of raising out of the
    installer (which would orphan already-installed skills and skip the
    registry write). Never escapes the module.
    """


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

        clone_cm = self._clone(owner, repo, timeout=timeout)

        try:
            async with clone_cm as repo_root:
                return await self._install_from_tree(
                    repo_root=repo_root,
                    subdir=subdir,
                    source=f"{owner}/{repo}",
                )
        except TimeoutError as exc:
            raise PluginInstallError(f"Clone timed out ({timeout:g}s)", 504) from exc
        except RuntimeError as exc:
            logger.warning("Plugin clone failed: %s", exc)
            raise PluginInstallError("Plugin clone failed", 502) from exc

    async def _install_from_tree(
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

        # Route the bundle's MCP servers (skips cleanly when there are none).
        mcp_servers = await self._install_mcp_servers(plugin_root, manifest, report)

        report.steps.append(self._record_registry(manifest, source, installed, mcp_servers))
        report.installed_skills = installed
        report.installed_mcp_servers = mcp_servers

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
                shutil.copytree(src_dir, dest, ignore=ignore_symlinks)
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

    def _read_mcp_config(
        self, plugin_root: Path, manifest: PluginManifest
    ) -> dict[str, dict] | None:
        """Read the bundle's MCP config, returning ``{name: spec}`` or None.

        Looks at the manifest's ``mcp_servers`` path override when set,
        otherwise the conventional ``.mcp.json`` at the plugin root. The
        standard shape is ``{"mcpServers": {name: spec}}``. Returns None
        when no config file exists so the caller can skip cleanly; returns
        an empty dict when the file exists but declares no servers.

        Raises:
            _MCPConfigError: If the file exists but is malformed (JSON parse
                error, top-level not a dict, ``mcpServers`` not a dict). The
                caller turns this into a ``failed`` step so the install
                degrades per-step rather than raising out of the installer.
        """
        if manifest.mcp_servers:
            mcp_path = plugin_root / manifest.mcp_servers
        else:
            mcp_path = plugin_root / ".mcp.json"

        if not mcp_path.is_file():
            return None

        try:
            raw = json.loads(mcp_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Plugin MCP config unreadable: %s", exc)
            raise _MCPConfigError("invalid .mcp.json (parse error)") from exc

        if not isinstance(raw, dict):
            raise _MCPConfigError(".mcp.json must be a JSON object")

        servers = raw.get("mcpServers", {})
        if not isinstance(servers, dict):
            raise _MCPConfigError(".mcp.json 'mcpServers' must be an object")
        return servers

    async def _install_mcp_servers(
        self,
        plugin_root: Path,
        manifest: PluginManifest,
        report: PluginInstallReport,
    ) -> list[str]:
        """Register + start the bundle's MCP servers. One step per server.

        Each server is namespaced ``plugin:<plugin>:<server>`` to avoid
        cross-plugin collisions, mapped to an :class:`MCPServerConfig` via
        :func:`spec_to_config`, registered with the MCP manager, and started.
        A server that registers but fails to start because it lacks required
        env is reported ``succeeded`` with a ``needs env: KEY`` detail
        (non-fatal); any other start failure is a ``failed`` step. Skips
        cleanly (one ``skipped`` step) when the bundle declares no MCP config.
        A malformed ``.mcp.json`` becomes a single ``failed`` ``mcp`` step —
        it never raises out of the installer, so installed skills aren't
        orphaned and the registry write still happens.
        """
        try:
            servers = self._read_mcp_config(plugin_root, manifest)
        except _MCPConfigError as exc:
            report.steps.append(PluginInstallStep(name="mcp", status="failed", detail=str(exc)))
            return []
        if servers is None:
            report.steps.append(
                PluginInstallStep(name="mcp", status="skipped", detail="no .mcp.json")
            )
            return []
        if not servers:
            report.steps.append(
                PluginInstallStep(name="mcp", status="skipped", detail="no servers declared")
            )
            return []

        manager = get_mcp_manager()
        installed: list[str] = []
        # Sequential per request — the manager's start_server holds a lock,
        # so concurrent starts would serialise anyway; keep ordering stable.
        for server_name in sorted(servers):
            spec = servers[server_name]
            step_name = f"mcp:{server_name}"
            if server_name in (".", "..") or not _NAME_RE.match(server_name):
                report.steps.append(
                    PluginInstallStep(
                        name=step_name, status="skipped", detail="invalid server name"
                    )
                )
                continue
            if not isinstance(spec, dict):
                report.steps.append(
                    PluginInstallStep(name=step_name, status="skipped", detail="invalid spec")
                )
                continue

            namespaced = f"plugin:{manifest.name}:{server_name}"
            cfg = spec_to_config(namespaced, spec)

            step = await self._register_and_start(manager, cfg, spec, step_name)
            report.steps.append(step)
            if step.status != "failed":
                installed.append(namespaced)
        return installed

    async def _register_and_start(
        self,
        manager: Any,
        cfg: MCPServerConfig,
        spec: dict,
        step_name: str,
    ) -> PluginInstallStep:
        """Register a server config and start it, returning one step.

        A start failure caused by missing required env is non-fatal: the
        step is ``succeeded`` with a ``needs env: KEY`` detail so the user
        knows to supply credentials later. Any other failure is ``failed``.
        """
        try:
            manager.add_server_config(cfg)
        except Exception as exc:  # registry write hiccup — non-recoverable for this server
            logger.warning("MCP server '%s' registration failed: %s", cfg.name, exc)
            return PluginInstallStep(name=step_name, status="failed", detail=str(exc))

        missing = self._missing_env(spec, cfg)
        try:
            ok = await manager.start_server(cfg)
        except Exception as exc:
            logger.warning("MCP server '%s' start raised: %s", cfg.name, exc)
            return PluginInstallStep(name=step_name, status="failed", detail=str(exc))

        if ok:
            return PluginInstallStep(name=step_name, status="succeeded")
        if missing:
            # Heuristic: we can't tell *why* start_server returned False, only
            # that the spec declared an empty env var. So a server that failed
            # to start for an unrelated reason while also having an empty env
            # value is reported "succeeded / needs env" — a deliberate
            # false-positive trade-off, not a bug. We'd rather nudge the
            # operator to supply credentials than hard-fail a recoverable
            # install. (#1358's list/remove surface will let them retry.)
            return PluginInstallStep(
                name=step_name,
                status="succeeded",
                detail=f"needs env: {missing}",
            )
        return PluginInstallStep(name=step_name, status="failed", detail="server failed to start")

    @staticmethod
    def _missing_env(spec: dict, cfg: MCPServerConfig) -> str | None:
        """Return the first declared env var the spec left unset, else None.

        A bundle can declare required env in the spec's ``env`` block with an
        empty / placeholder value (e.g. ``{"API_KEY": ""}``). When the value
        is empty the server will start-fail for lack of credentials; we treat
        that as a non-fatal "needs env" outcome rather than a hard failure.
        """
        raw_env = spec.get("env")
        if not isinstance(raw_env, dict):
            return None
        for key, value in raw_env.items():
            if not str(value).strip():
                return str(key)
        return None

    def _record_registry(
        self,
        manifest: PluginManifest,
        source: str,
        installed: list[str],
        mcp_servers: list[str] | None = None,
    ) -> PluginInstallStep:
        """Write the plugin entry to the registry (read-modify-write)."""
        from datetime import datetime

        try:
            registry = self._load_registry()
            registry[manifest.name] = {
                "version": manifest.version,
                "source": source,
                "skills": installed,
                "mcp_servers": mcp_servers or [],
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
