"""Codex CLI backend for PocketPaw.

Spawns OpenAI's Codex CLI (npm install -g @openai/codex) as a subprocess
and parses its streaming NDJSON output. Analogous to Gemini CLI but for Codex.

Built-in tools: shell (command_execution), file editing (file_change),
MCP tool calls, web search.

Requires: OPENAI_API_KEY (or CODEX_API_KEY) env var and `codex` on PATH.
"""

import asyncio
import json
import logging
import shutil
from collections.abc import AsyncIterator
from typing import Any

from pocketpaw.agents.backend import BackendInfo, Capability
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.config import Settings

logger = logging.getLogger(__name__)


class CodexCLIBackend:
    """Codex CLI backend — subprocess wrapper for OpenAI's terminal AI agent."""

    @staticmethod
    def info() -> BackendInfo:
        return BackendInfo(
            name="codex_cli",
            display_name="Codex CLI",
            capabilities=(
                Capability.STREAMING
                | Capability.TOOLS
                | Capability.MCP
                | Capability.MULTI_TURN
                | Capability.CUSTOM_SYSTEM_PROMPT
            ),
            builtin_tools=["shell", "file_edit", "web_search", "mcp"],
            tool_policy_map={
                "shell": "shell",
                "file_edit": "write_file",
                "web_search": "browser",
                "mcp": "mcp",
            },
            required_keys=["openai_api_key"],
            supported_providers=["openai"],
            install_hint={
                "external_cmd": "npm install -g @openai/codex",
            },
            beta=True,
        )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._stop_flag = False
        self._cli_available = shutil.which("codex") is not None
        self._process: asyncio.subprocess.Process | None = None
        if self._cli_available:
            logger.info("Codex CLI found on PATH")
        else:
            logger.warning("Codex CLI not found — install with: npm install -g @openai/codex")

    @staticmethod
    def _inject_history(instruction: str, history: list[dict]) -> str:
        """Append conversation history to instruction as text."""
        lines = ["# Recent Conversation"]
        for msg in history:
            role = msg.get("role", "user").capitalize()
            content = msg.get("content", "")
            if len(content) > 500:
                content = content[:500] + "..."
            lines.append(f"**{role}**: {content}")
        return instruction + "\n\n" + "\n".join(lines)

    async def run(
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list[dict] | None = None,
        session_key: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        if not self._cli_available:
            yield AgentEvent(
                type="error",
                content=(
                    "Codex CLI not found on PATH.\n\nInstall with: npm install -g @openai/codex"
                ),
            )
            return

        self._stop_flag = False

        try:
            # Build the prompt: system prompt + history + user message
            prompt_parts = []
            if system_prompt:
                prompt_parts.append(f"[System Instructions]\n{system_prompt}\n")
            if history:
                prompt_parts.append(self._inject_history("", history).strip())
            prompt_parts.append(message)
            full_prompt = "\n\n".join(prompt_parts)

            model = self.settings.codex_cli_model or "gpt-5.3-codex"

            cmd = [
                "codex",
                "exec",
                "--json",
                "--full-auto",
                "--model",
                model,
                full_prompt,
            ]

            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            if self._process.stdout is None:
                yield AgentEvent(type="error", content="Failed to capture Codex CLI stdout")
                return

            async for raw_line in self._process.stdout:
                if self._stop_flag:
                    break

                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                try:
                    event_data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = event_data.get("type", "")

                if event_type == "thread.started":
                    thread_id = event_data.get("thread_id", "unknown")
                    logger.info("Codex CLI thread: %s", thread_id)

                elif event_type == "turn.started":
                    logger.debug("Codex CLI turn started")

                elif event_type == "turn.completed":
                    usage = event_data.get("usage", {})
                    if usage:
                        yield AgentEvent(
                            type="token_usage",
                            content="",
                            metadata={
                                "input_tokens": usage.get("input_tokens", 0),
                                "output_tokens": usage.get("output_tokens", 0),
                                "cached_input_tokens": usage.get("cached_input_tokens", 0),
                            },
                        )

                elif event_type == "turn.failed":
                    yield AgentEvent(
                        type="error",
                        content=event_data.get("message", "Codex CLI turn failed"),
                    )

                elif event_type == "item.started":
                    item = event_data.get("item", {})
                    item_type = item.get("type", "")
                    if item_type == "command_execution":
                        cmd_str = item.get("command", "")
                        yield AgentEvent(
                            type="tool_use",
                            content=f"Running: {cmd_str}",
                            metadata={"name": "shell", "input": {"command": cmd_str}},
                        )
                    elif item_type == "file_change":
                        filename = item.get("filename", "unknown")
                        yield AgentEvent(
                            type="tool_use",
                            content=f"Editing: {filename}",
                            metadata={"name": "file_edit", "input": {"filename": filename}},
                        )
                    elif item_type == "mcp_tool_call":
                        tool_name = item.get("name", "mcp_tool")
                        yield AgentEvent(
                            type="tool_use",
                            content=f"MCP: {tool_name}",
                            metadata={"name": tool_name, "input": item.get("arguments", {})},
                        )
                    elif item_type == "web_search":
                        query = item.get("query", "")
                        yield AgentEvent(
                            type="tool_use",
                            content=f"Searching: {query}",
                            metadata={"name": "web_search", "input": {"query": query}},
                        )

                elif event_type == "item.completed":
                    item = event_data.get("item", {})
                    item_type = item.get("type", "")
                    if item_type == "agent_message":
                        text = item.get("text", "")
                        if text:
                            yield AgentEvent(type="message", content=text)
                    elif item_type == "command_execution":
                        output = item.get("output", "")
                        yield AgentEvent(
                            type="tool_result",
                            content=str(output)[:200],
                            metadata={"name": "shell"},
                        )
                    elif item_type == "file_change":
                        filename = item.get("filename", "unknown")
                        yield AgentEvent(
                            type="tool_result",
                            content=f"Updated {filename}",
                            metadata={"name": "file_edit"},
                        )
                    elif item_type == "mcp_tool_call":
                        tool_name = item.get("name", "mcp_tool")
                        output = item.get("output", "")
                        yield AgentEvent(
                            type="tool_result",
                            content=str(output)[:200],
                            metadata={"name": tool_name},
                        )
                    elif item_type == "web_search":
                        output = item.get("output", "")
                        yield AgentEvent(
                            type="tool_result",
                            content=str(output)[:200],
                            metadata={"name": "web_search"},
                        )
                    elif item_type == "reasoning":
                        text = item.get("text", "")
                        if text:
                            yield AgentEvent(type="thinking", content=text)

                elif event_type == "error":
                    error_msg = event_data.get("message", "Unknown Codex CLI error")
                    yield AgentEvent(type="error", content=error_msg)

            # Wait for process to finish
            await self._process.wait()
            exit_code = self._process.returncode

            if exit_code and exit_code != 0 and not self._stop_flag:
                stderr_output = ""
                if self._process.stderr:
                    stderr_bytes = await self._process.stderr.read()
                    stderr_output = stderr_bytes.decode("utf-8", errors="replace").strip()

                base_msg = f"Codex CLI exited with code {exit_code}"
                if stderr_output:
                    base_msg += f": {stderr_output[:200]}"
                yield AgentEvent(type="error", content=base_msg)

            self._process = None
            yield AgentEvent(type="done", content="")

        except Exception as e:
            logger.error("Codex CLI error: %s", e)
            yield AgentEvent(type="error", content=f"Codex CLI error: {e}")

    async def stop(self) -> None:
        self._stop_flag = True
        if self._process and self._process.returncode is None:
            try:
                self._process.terminate()
            except ProcessLookupError:
                pass

    async def get_status(self) -> dict[str, Any]:
        return {
            "backend": "codex_cli",
            "cli_available": self._cli_available,
            "running": self._process is not None and self._process.returncode is None,
            "model": self.settings.codex_cli_model or "gpt-5.3-codex",
        }
