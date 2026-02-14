"""
Open Interpreter Executor - The "Hands" layer for OS control.

Created: 2026-02-02
Changes:
  - 2026-02-02: Initial implementation of ExecutorProtocol using Open Interpreter.
  - 2026-02-02: SPEED FIX - Direct subprocess for shell commands instead of OI chat.
                Shell commands now 10x faster. OI reserved for complex multi-step tasks.
"""

import asyncio
import logging
from pathlib import Path

from pocketclaw.config import Settings

logger = logging.getLogger(__name__)


class OpenInterpreterExecutor:
    """Open Interpreter as the executor layer.

    Implements ExecutorProtocol - handles actual OS operations:
    - Shell commands
    - File read/write
    - Directory listing

    Used by orchestrators (Claude Agent SDK) to execute tool calls.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._interpreter = None
        self._initialize()

    def _initialize(self) -> None:
        """Initialize Open Interpreter instance."""
        try:
            from interpreter import interpreter

            from pocketclaw.llm.client import resolve_llm_client

            # Configure for execution mode (minimal LLM usage)
            interpreter.auto_run = True
            interpreter.loop = False  # Single command execution

            # Set LLM for any reasoning needed
            llm = resolve_llm_client(self.settings)
            if llm.is_ollama:
                interpreter.llm.model = f"ollama/{llm.model}"
                interpreter.llm.api_base = llm.ollama_host
            elif llm.api_key:
                interpreter.llm.model = llm.model
                interpreter.llm.api_key = llm.api_key

            self._interpreter = interpreter
            logger.info("=" * 50)
            logger.info("🔧 EXECUTOR: Open Interpreter initialized")
            logger.info("   └─ Role: Shell, files, system commands")
            logger.info("=" * 50)

        except ImportError:
            logger.error("❌ Open Interpreter not installed")
            self._interpreter = None
        except Exception as e:
            logger.error(f"❌ Failed to initialize executor: {e}")
            self._interpreter = None

    async def run_shell(self, command: str) -> str:
        """Execute a shell command and return output.

        Uses DIRECT subprocess execution for speed (not OI chat).
        This makes simple commands like 'ls', 'git status' ~10x faster.
        """
        logger.info(f"🔧 EXECUTOR: run_shell({command[:50]}...)")

        try:
            # Direct async subprocess - FAST
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(Path.home()),  # Default to home directory
            )

            # Wait with timeout
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=60.0,  # 60 second timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                return "Error: Command timed out after 60 seconds"

            # Combine output
            output = stdout.decode("utf-8", errors="replace")
            if stderr:
                err_text = stderr.decode("utf-8", errors="replace")
                if err_text.strip():
                    output += f"\n[stderr]: {err_text}"

            return output if output.strip() else "(no output)"

        except Exception as e:
            logger.error(f"Shell execution error: {e}")
            return f"Error: {str(e)}"

    async def run_complex_task(self, task: str) -> str:
        """Execute a complex multi-step task using Open Interpreter.

        Use this for tasks that need:
        - Multi-step reasoning
        - Code generation
        - Browser automation
        - AppleScript/Python for app queries

        For simple shell commands, use run_shell() instead.
        """
        if not self._interpreter:
            return "Error: Open Interpreter not available"

        logger.info(f"🤖 EXECUTOR: run_complex_task({task[:80]}...)")

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, self._run_interpreter_sync, task)
            return result
        except Exception as e:
            logger.error(f"Complex task error: {e}")
            return f"Error: {str(e)}"

    def _run_interpreter_sync(self, task: str) -> str:
        """Synchronous Open Interpreter execution for complex tasks."""
        output_parts = []

        for chunk in self._interpreter.chat(task, stream=True):
            if isinstance(chunk, dict):
                content = chunk.get("content", "")
                if content:
                    output_parts.append(str(content))
            elif isinstance(chunk, str):
                output_parts.append(chunk)

        return "".join(output_parts) or "(no output)"

    async def read_file(self, path: str) -> str:
        """Read file contents."""
        logger.info(f"🔧 EXECUTOR: read_file({path})")

        try:
            # Direct file read - no need for interpreter
            with open(path, "r") as f:
                return f.read()
        except Exception as e:
            logger.error(f"File read error: {e}")
            return f"Error reading file: {str(e)}"

    async def write_file(self, path: str, content: str) -> None:
        """Write content to file."""
        logger.info(f"🔧 EXECUTOR: write_file({path})")

        try:
            with open(path, "w") as f:
                f.write(content)
        except Exception as e:
            logger.error(f"File write error: {e}")
            raise

    async def list_directory(self, path: str) -> list[str]:
        """List directory contents."""
        logger.info(f"🔧 EXECUTOR: list_directory({path})")

        import os

        try:
            return os.listdir(path)
        except Exception as e:
            logger.error(f"Directory list error: {e}")
            return []
