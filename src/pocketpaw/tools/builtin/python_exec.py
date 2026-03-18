# Python execution tool - sandboxed Python script runner.
# Created: 2026-03-12

import asyncio
import subprocess
import sys
import uuid
from typing import Any

from pocketpaw.config import get_settings
from pocketpaw.security import get_guardian
from pocketpaw.tools.protocol import BaseTool


class RunPythonTool(BaseTool):
    """Execute a Python script in a sandboxed subprocess."""

    @property
    def name(self) -> str:
        return "run_python"

    @property
    def description(self) -> str:
        return (
            "Execute a Python script in a sandboxed subprocess and return its output. "
            "Use for data processing, file generation, calculations, or running installed packages."
        )

    @property
    def trust_level(self) -> str:
        return "elevated"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to execute",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Execution timeout in seconds (default: 120)",
                    "default": 120,
                },
            },
            "required": ["code"],
        }

    async def execute(self, code: str, timeout: int = 120) -> str:  # type: ignore[override]
        """Execute Python code in a sandboxed subprocess."""
        # Guardian AI check on the code before execution
        is_safe, reason = await get_guardian().check_command(code)
        if not is_safe:
            return self._error(f"Code blocked by Guardian: {reason}")

        jail_path = get_settings().file_jail_path
        jail_path.mkdir(parents=True, exist_ok=True)

        # Write code to a temp file in the jail so multiline scripts work cleanly
        script_name = f"_pocketpaw_run_{uuid.uuid4().hex}.py"
        script_path = jail_path / script_name

        try:
            script_path.write_text(code, encoding="utf-8")

            result = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    [sys.executable, str(script_path)],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=str(jail_path),
                ),
            )

            output = result.stdout
            if result.stderr:
                output += f"\nSTDERR:\n{result.stderr}"
            if result.returncode != 0:
                output += f"\nExit code: {result.returncode}"

            return output.strip() or "(no output)"

        except subprocess.TimeoutExpired:
            return self._error(f"Python script timed out after {timeout}s")
        except Exception as e:
            return self._error(str(e))
        finally:
            # Always clean up the temp script file
            if script_path.exists():
                try:
                    script_path.unlink()
                except Exception:
                    pass
