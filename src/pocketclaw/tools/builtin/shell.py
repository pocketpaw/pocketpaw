# Shell execution tool.
# Created: 2026-02-02


import asyncio
import subprocess
import re
from typing import Any

from pocketclaw.tools.protocol import BaseTool
from pocketclaw.config import get_settings
from pocketclaw.security import get_guardian


class ShellTool(BaseTool):
    """Execute shell commands."""

    # Dangerous patterns to block
    DANGEROUS_PATTERNS = [
        r"rm\s+-rf\s+/",
        r"rm\s+-rf\s+\*",
        r">\s*/etc/",
        r"sudo\s+rm",
        r"mkfs\.",
        r"dd\s+if=/dev/",
        r":\(\)\s*\{\s*:\|:&\s*\}\s*;",  # Fork bomb
        r"curl.*\|\s*sh",
        r"wget.*\|\s*bash",
    ]

    def __init__(self, working_dir: str | None = None, timeout: int = 120):
        self.working_dir = working_dir or str(get_settings().file_jail_path)
        self.timeout = timeout

    @property
    def name(self) -> str:
        return "shell"

    @property
    def description(self) -> str:
        return "Execute a shell command and return the output."

    @property
    def trust_level(self) -> str:
        return "critical"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                }
            },
            "required": ["command"],
        }

    async def execute(self, command: str) -> str:
        """Execute a shell command."""
        # Security check
        for pattern in self.DANGEROUS_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return self._error(f"Dangerous command blocked: {command}")

        # Check with Guardian Agent
        is_safe, reason = await get_guardian().check_command(command)
        if not is_safe:
            return self._error(f"Command blocked by Guardian: {reason}")

        try:
            # Run in thread pool to not block
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    cwd=self.working_dir,
                ),
            )

            output = result.stdout
            if result.stderr:
                output += f"\nSTDERR:\n{result.stderr}"

            if result.returncode != 0:
                output += f"\n\nExit code: {result.returncode}"

            return output.strip() or "(no output)"

        except subprocess.TimeoutExpired:
            return self._error(f"Command timed out after {self.timeout}s")
        except Exception as e:
            return self._error(str(e))
