"""Regression tests for import-time filesystem side effects."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Read-only HOME simulation relies on POSIX chmod semantics",
)


def _run_in_read_only_home(tmp_path, code: str) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    home.mkdir()
    home.chmod(stat.S_IREAD | stat.S_IEXEC)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)

    pythonpath = env.get("PYTHONPATH")
    repo_src = str((REPO_ROOT / "src").resolve())
    env["PYTHONPATH"] = repo_src if not pythonpath else os.pathsep.join([repo_src, pythonpath])

    try:
        return subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(REPO_ROOT),
            check=False,
        )
    finally:
        home.chmod(stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)


def test_settings_load_does_not_require_writable_home(tmp_path):
    result = _run_in_read_only_home(
        tmp_path,
        "from pocketpaw.config import Settings; Settings.load(); print('ok')",
    )

    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
    assert not (tmp_path / "home" / ".pocketpaw").exists()


def test_create_api_app_does_not_require_writable_home(tmp_path):
    result = _run_in_read_only_home(
        tmp_path,
        "from pocketpaw.api.serve import create_api_app; create_api_app(); print('ok')",
    )

    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
    assert not (tmp_path / "home" / ".pocketpaw").exists()


def test_dashboard_import_does_not_require_writable_home(tmp_path):
    result = _run_in_read_only_home(
        tmp_path,
        "import pocketpaw.dashboard; print('ok')",
    )

    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
    assert not (tmp_path / "home" / ".pocketpaw").exists()
