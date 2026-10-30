# tests/ee/test_dotenv_no_parent_walk.py
# Library code reads only the .env in the working directory, the same file
# Settings(env_file=".env") reads. A bare load_dotenv() / dotenv_values() walks up the
# directory tree until it finds any .env, so a pocketpaw worktree nested inside another
# project silently imported that project's variables (PAW_CF_DEPLOY_MODE=workers among
# them) and a local run deployed to real Cloudflare.
#
# The subprocess test runs the real load with dotenv enabled. The test process itself
# has PYTHON_DOTENV_DISABLED=1 (tests/conftest.py), so the child gets a clean env.
# `python -c` makes python-dotenv search from the cwd, which is the walk under test.
# The scan test stops a new bare call from coming back anywhere in src/ or ee/.

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SENTINEL = "PAW_TEST_PARENT_DOTENV_SENTINEL"


def _child_env() -> dict[str, str]:
    import os

    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHON_DOTENV_DISABLED": "0",
        "POCKETPAW_ENV": "development",
    }


_LOAD_LICENSE = (
    "import os\n"
    "from pocketpaw_ee.cloud import license\n"
    "try:\n"
    "    license.load_license()\n"
    "except Exception:\n"
    "    pass\n"
    f"print(os.environ.get({_SENTINEL!r}, 'ABSENT'))\n"
)


def test_load_license_ignores_a_dotenv_in_a_parent_directory(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"{_SENTINEL}=leaked\n", encoding="utf-8")
    child = tmp_path / "child"
    child.mkdir()

    out = subprocess.run(
        [sys.executable, "-c", _LOAD_LICENSE],
        cwd=child,
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "ABSENT", (
        "load_license() imported a .env from a parent directory"
    )


def test_load_license_still_reads_the_dotenv_in_the_working_directory(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"{_SENTINEL}=own\n", encoding="utf-8")

    out = subprocess.run(
        [sys.executable, "-c", _LOAD_LICENSE],
        cwd=tmp_path,
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "own"


# load_dotenv() / dotenv_values() with no path, optionally with keyword arguments
# (override=False): every form that falls back to find_dotenv's walk. dotenv_path=
# is an explicit path, so it passes.
_BARE_CALL = re.compile(r"(load_dotenv|dotenv_values)\(\s*(\)|(?!dotenv_path\b)[a-z_]+\s*=)")


def test_no_library_code_calls_dotenv_without_a_path() -> None:
    offenders: list[str] = []
    for root in ("src", "ee"):
        for path in (_REPO / root).rglob("*.py"):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                if _BARE_CALL.search(code):
                    offenders.append(f"{path.relative_to(_REPO)}:{lineno}")

    assert offenders == [], (
        "these calls search parent directories for a .env; pass '.env' so only the "
        f"working directory's file is read: {offenders}"
    )
