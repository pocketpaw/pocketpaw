# PocketPaw Desktop Launcher — Update Checker
# Checks PyPI for newer versions and upgrades the venv install.
# Uses uv when available for faster installs, falls back to pip.
# Created: 2026-02-10

from __future__ import annotations

import json
import logging
import platform
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from installer.launcher.common import (
    DEV_MODE_MARKER,
    GIT_REPO_URL,
    PACKAGE_NAME,
    POCKETCLAW_HOME,
    VENV_DIR,
    StatusCallback,
    find_uv,
    get_installed_version,
    noop_status,
)

logger = logging.getLogger(__name__)

PYPI_URL = f"https://pypi.org/pypi/{PACKAGE_NAME}/json"


@dataclass
class UpdateInfo:
    """Result of an update check."""

    current_version: str | None = None
    latest_version: str | None = None
    update_available: bool = False
    dev_mode: bool = False
    dev_branch: str | None = None
    error: str | None = None


class Updater:
    """Check for and apply PocketPaw updates."""

    def __init__(self, on_status: StatusCallback | None = None) -> None:
        self.on_status = on_status or noop_status

    def is_dev_mode(self) -> bool:
        """Check if the launcher is running in dev/branch mode."""
        return DEV_MODE_MARKER.exists()

    def _read_dev_branch(self) -> str | None:
        """Read the branch name from the dev mode marker file."""
        if not DEV_MODE_MARKER.exists():
            return None
        try:
            for line in DEV_MODE_MARKER.read_text(encoding="utf-8").splitlines():
                if line.startswith("branch=") and line.split("=", 1)[1]:
                    return line.split("=", 1)[1]
        except OSError:
            pass
        return None

    def check(self) -> UpdateInfo:
        """Check if a newer version is available on PyPI (or git for dev mode)."""
        info = UpdateInfo()

        # Detect dev mode
        if self.is_dev_mode():
            info.dev_mode = True
            info.dev_branch = self._read_dev_branch()
            info.current_version = self._get_installed_version()
            # In dev mode, always offer "re-pull from branch" instead of PyPI check
            info.update_available = True
            return info

        # Get current version from venv
        info.current_version = self._get_installed_version()
        if not info.current_version:
            info.error = "PocketPaw not installed"
            return info

        # Get latest version from PyPI
        info.latest_version = self._get_pypi_version()
        if not info.latest_version:
            info.error = "Could not check PyPI for updates"
            return info

        # Compare versions
        info.update_available = self._version_newer(info.latest_version, info.current_version)

        return info

    def apply(self) -> bool:
        """Upgrade pocketpaw in the venv to the latest version (PyPI or git branch)."""
        python = self._venv_python()
        if not python.exists():
            self.on_status("PocketPaw not installed")
            return False

        uv = self._find_uv()

        # Dev mode: reinstall from git branch
        if self.is_dev_mode():
            branch = self._read_dev_branch()
            if branch:
                self.on_status(f"Pulling latest from branch '{branch}'...")
                return self._update_from_branch(python, uv, branch)
            # Local mode — user should re-run with --local
            self.on_status("Local dev mode — run launcher with --local to update")
            return False

        self.on_status("Updating PocketPaw...")

        try:
            if uv:
                logger.info("Running uv pip install --upgrade %s", PACKAGE_NAME)
                # Use overrides file if it exists (created by bootstrap)
                overrides = POCKETCLAW_HOME / "uv-overrides.txt"
                cmd = [uv, "pip", "install", "--upgrade", PACKAGE_NAME, "--python", str(python)]
                if overrides.exists():
                    cmd.extend(["--override", str(overrides)])
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
            else:
                logger.info("Running pip install --upgrade %s", PACKAGE_NAME)
                result = subprocess.run(
                    [str(python), "-m", "pip", "install", "--upgrade", PACKAGE_NAME, "--quiet"],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
            if result.returncode == 0:
                new_ver = self._get_installed_version()
                self.on_status(f"Updated to v{new_ver}")
                return True
            else:
                logger.error("Update failed: %s", result.stderr[-1000:])
                self.on_status("Update failed. Check logs.")
                return False
        except subprocess.TimeoutExpired:
            self.on_status("Update timed out")
            return False

    def _update_from_branch(self, python: Path, uv: str | None, branch: str) -> bool:
        """Re-install pocketpaw from a git branch (force-reinstall to pick up new commits)."""
        pkg = f"{PACKAGE_NAME} @ git+{GIT_REPO_URL}@{branch}"
        logger.info("Updating from git branch '%s'", branch)

        try:
            if uv:
                cmd = [
                    uv,
                    "pip",
                    "install",
                    "--reinstall",
                    pkg,
                    "--python",
                    str(python),
                ]
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
            else:
                result = subprocess.run(
                    [str(python), "-m", "pip", "install", "--force-reinstall", pkg, "--quiet"],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
            if result.returncode == 0:
                new_ver = self._get_installed_version()
                self.on_status(f"Updated to {branch} (v{new_ver})")
                return True
            else:
                logger.error("Branch update failed: %s", result.stderr[-1000:])
                self.on_status(f"Update from {branch} failed. Check logs.")
                return False
        except subprocess.TimeoutExpired:
            self.on_status("Update timed out")
            return False

    # ── Internal ───────────────────────────────────────────────────────

    def _venv_python(self) -> Path:
        if platform.system() == "Windows":
            return VENV_DIR / "Scripts" / "python.exe"
        return VENV_DIR / "bin" / "python"

    def _find_uv(self) -> str | None:
        """Find uv binary (downloaded by bootstrap or on system PATH)."""
        return find_uv()

    def _get_installed_version(self) -> str | None:
        """Get installed pocketpaw version from venv."""
        python = self._venv_python()
        if not python.exists():
            return None
        return get_installed_version(python=python, uv=self._find_uv())

    def _get_pypi_version(self) -> str | None:
        """Fetch the latest version from PyPI JSON API."""
        try:
            req = urllib.request.Request(
                PYPI_URL,
                headers={"Accept": "application/json"},
            )
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            return data.get("info", {}).get("version")
        except Exception as exc:
            logger.warning("PyPI check failed: %s", exc)
            return None

    def _version_newer(self, latest: str, current: str) -> bool:
        """Compare version strings. Returns True if latest > current.

        Handles pre-release suffixes (e.g. 0.2.0a1) via packaging.version,
        falling back to tuple comparison for simple X.Y.Z versions.
        """
        try:
            from packaging.version import Version

            return Version(latest) > Version(current)
        except ImportError:
            pass
        # Fallback: strip non-numeric suffixes and compare tuples
        import re

        def _parse(v: str) -> tuple[int, ...]:
            return tuple(int(x) for x in re.findall(r"\d+", v))

        try:
            return _parse(latest) > _parse(current)
        except (ValueError, AttributeError):
            return latest != current
