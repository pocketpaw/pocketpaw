# PocketPaw Desktop Launcher — Shared Constants & Helpers
# Deduplicates paths, version checks, and Python/uv discovery used by
# bootstrap.py, server.py, updater.py, and uninstall.py.
# Created: 2026-02-12

from __future__ import annotations

import logging
import platform
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────
POCKETCLAW_HOME = Path.home() / ".pocketclaw"
VENV_DIR = POCKETCLAW_HOME / "venv"
UV_DIR = POCKETCLAW_HOME / "uv"

# ── Package metadata ───────────────────────────────────────────────────
PACKAGE_NAME = "pocketpaw"
GIT_REPO_URL = "https://github.com/pocketpaw/pocketpaw.git"
DEV_MODE_MARKER = POCKETCLAW_HOME / ".dev-mode"

# ── Callback types ─────────────────────────────────────────────────────
StatusCallback = Callable[[str], None]


def noop_status(msg: str) -> None:
    """No-op status callback."""


# ── Helpers ────────────────────────────────────────────────────────────


def venv_python() -> Path:
    """Path to the Python executable inside the venv."""
    if platform.system() == "Windows":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def find_uv() -> str | None:
    """Find uv binary — checks our download location first, then system PATH."""
    if platform.system() == "Windows":
        local_uv = UV_DIR / "uv.exe"
    else:
        local_uv = UV_DIR / "uv"
    if local_uv.exists():
        return str(local_uv)
    return shutil.which("uv")


def get_installed_version(
    python: str | Path | None = None,
    uv: str | None = None,
) -> str | None:
    """Get the installed pocketpaw version from the venv.

    Args:
        python: Path to the venv Python. Defaults to ``venv_python()``.
        uv: Path to uv binary. Defaults to ``find_uv()``.
    """
    if python is None:
        python = venv_python()
        if not python.exists():
            return None
    python = str(python)

    if uv is None:
        uv = find_uv()

    try:
        if uv:
            result = subprocess.run(
                [uv, "pip", "show", PACKAGE_NAME, "--python", python],
                capture_output=True,
                text=True,
                timeout=30,
            )
        else:
            result = subprocess.run(
                [python, "-m", "pip", "show", PACKAGE_NAME],
                capture_output=True,
                text=True,
                timeout=30,
            )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.lower().startswith("version:"):
                    return line.split(":", 1)[1].strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None
