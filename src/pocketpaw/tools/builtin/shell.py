# Shell execution tool.
# Created: 2026-02-02


import asyncio
import subprocess
from typing import Any

from pocketpaw.config import get_settings
from pocketpaw.security import get_guardian
from pocketpaw.security.rails import COMPILED_DANGEROUS_PATTERNS
from pocketpaw.tools.protocol import BaseTool


class ShellTool(BaseTool):
    """Execute shell commands."""

    # Dangerous-command patterns — shared rail (see security/rails.py)
    DANGEROUS_PATTERNS = COMPILED_DANGEROUS_PATTERNS

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
        # Handle 'pwd' natively for cross-platform compatibility
        if command.strip() == "pwd":
            return self.working_dir

        # Security check
        for pattern in self.DANGEROUS_PATTERNS:
            if pattern.search(command):
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
