# Package installation tool - pip install with Guardian review.
# Created: 2026-03-12

import asyncio
import re
import subprocess
import sys
from typing import Any

from pocketpaw.security import get_guardian
from pocketpaw.tools.protocol import BaseTool

# Whitelist: only characters valid in a single pip package spec are allowed.
# Covers package names, extras (brackets), version specifiers, and version numbers.
# No whitespace: this tool installs one package at a time.
# Anything outside this set (semicolons, pipes, ampersands, backticks, dollar signs,
# parens, newlines, spaces) will fail the match and be rejected.
_VALID_PACKAGE_SPEC_RE = re.compile(r"^[a-zA-Z0-9_\-\.\[\],~>=<!]+$")


class InstallPackageTool(BaseTool):
    """Install a Python package using pip with Guardian review."""

    def __init__(self, timeout: int = 300):
        self.timeout = timeout

    @property
    def name(self) -> str:
        return "install_package"

    @property
    def description(self) -> str:
        return (
            "Install a Python package using pip. Guardian AI reviews the package name "
            "before installation to prevent typosquatting and malicious packages."
        )

    @property
    def trust_level(self) -> str:
        return "elevated"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "package": {
                    "type": "string",
                    "description": (
                        "Package name with optional version specifier "
                        '(e.g. "requests", "paw-ytp>=0.1.0")'
                    ),
                },
                "upgrade": {
                    "type": "boolean",
                    "description": "Whether to use --upgrade flag",
                    "default": False,
                },
            },
            "required": ["package"],
        }

    def _is_valid_package_spec(self, package: str) -> bool:
        """Return True if the package spec contains only safe, pip-legal characters."""
        return bool(_VALID_PACKAGE_SPEC_RE.match(package))

    async def execute(self, package: str, upgrade: bool = False) -> str:
        """Install a package via pip after Guardian review."""

        # 1. Validate package spec, reject shell metacharacters
        if not self._is_valid_package_spec(package):
            return self._error(
                f"Invalid package spec '{package}': contains disallowed characters. "
                "Only alphanumeric characters, hyphens, underscores, dots, brackets, "
                "and version specifiers are allowed."
            )

        # 2. Guardian AI review
        pip_command = f"pip install {package}"
        is_safe, reason = await get_guardian().check_command(pip_command)
        if not is_safe:
            return self._error(f"Package blocked by Guardian: {reason}")

        # 3. Build the subprocess command
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            package,
            "--no-input",
            "--disable-pip-version-check",
        ]
        if upgrade:
            cmd.append("--upgrade")

        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                ),
            )

            if result.returncode != 0:
                error_output = result.stderr.strip() or result.stdout.strip()
                return self._error(f"pip exited with code {result.returncode}:\n{error_output}")

            return result.stdout.strip() or "(pip produced no output)"

        except subprocess.TimeoutExpired:
            return self._error(f"pip install timed out after {self.timeout}s")
        except Exception as e:
            return self._error(str(e))
