"""pocketpaw update - self-update via uv."""

import shutil
import subprocess
import sys

from pocketpaw.update_check import check_for_updates

# ANSI helpers
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def _find_uv() -> str | None:
    """Return the path to the uv binary, or None if not found."""
    return shutil.which("uv")


def run_update(current_version: str) -> int:
    """Check PyPI for a newer version and upgrade via uv.

    Returns 0 on success, 1 on failure.
    """
    print(f"\n  {BOLD}PocketPaw Update{RESET}")
    print(f"  {DIM}Current version: {current_version}{RESET}\n")

    # 1. Check if uv is available
    uv_bin = _find_uv()
    if not uv_bin:
        print(f"  {RED}uv not found.{RESET} Install it first:")
        print(f"  {DIM}  curl -LsSf https://astral.sh/uv/install.sh | sh{RESET}")
        print(f"  {DIM}  (or) pip install uv{RESET}\n")
        return 1

    # 2. Check for updates
    print("  Checking PyPI for latest version...")
    from pocketpaw.config import get_config_dir

    info = check_for_updates(current_version, get_config_dir())

    if info is None:
        print(f"  {RED}Failed to check for updates (network error).{RESET}\n")
        return 1

    latest = info["latest"]

    if not info["update_available"]:
        print(f"  {GREEN}Already up to date!{RESET} ({current_version})\n")
        return 0

    print(f"  {YELLOW}Update available:{RESET} {current_version} -> {GREEN}{latest}{RESET}")
    print("  Updating via uv...\n")

    # 3. Run uv pip install --upgrade pocketpaw
    cmd = [uv_bin, "pip", "install", "--upgrade", "pocketpaw", "--python", sys.executable]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        print(f"  {RED}Update timed out after 120 seconds.{RESET}\n")
        return 1

    if result.returncode != 0:
        stderr = result.stderr.strip()
        # On Windows, the running pocketpaw.exe locks the file. uv downloads
        # the packages successfully but fails on the final file replace. Detect
        # this and offer a workaround instead of a generic failure.
        if sys.platform == "win32" and "os error 32" in stderr:
            print(f"  {YELLOW}Packages downloaded, but the running process locks the exe.{RESET}")
            print(f"  {DIM}Stop pocketpaw first, then run:{RESET}")
            print("    uv pip install --upgrade pocketpaw\n")
            return 1
        print(f"  {RED}Update failed:{RESET}")
        if stderr:
            for line in stderr.splitlines()[:10]:
                print(f"    {line}")
        print()
        return 1

    print(f"  {GREEN}{BOLD}Updated to {latest}!{RESET}")
    print(f"  {DIM}Restart pocketpaw to use the new version.{RESET}\n")
    return 0
