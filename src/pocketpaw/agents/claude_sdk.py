"""
Claude Agent SDK backend for PocketPaw.

Uses the official Claude Agent SDK (pip install claude-agent-sdk) which provides:
- Built-in tools: Bash, Read, Write, Edit, Glob, Grep, WebSearch, WebFetch
- Streaming responses
- PreToolUse hooks for security
- Permission management
- MCP server support for custom tools
"""

import logging
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from pocketpaw.agents.backend import BackendInfo, Capability
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.config import Settings
from pocketpaw.security.rails import DANGEROUS_SUBSTRINGS as DANGEROUS_PATTERNS
from pocketpaw.tools.policy import ToolPolicy

logger = logging.getLogger(__name__)

# Default identity fallback (used when AgentContextBuilder prompt is not available)
_DEFAULT_IDENTITY = (
    "You are PocketPaw, a helpful AI assistant running locally on the user's computer."
)


class ClaudeSDKBackend:
    """Claude Agent SDK backend — the recommended default.

    Provides all built-in tools (Bash, Read, Write, Edit, Glob, Grep,
    WebSearch, WebFetch), streaming responses, PreToolUse hooks for
    security, and MCP server support.

    Requires: pip install claude-agent-sdk
    """

    _TOOL_POLICY_MAP: dict[str, str] = {
        "Bash": "shell",
        "Read": "read_file",
        "Write": "write_file",
        "Edit": "edit_file",
        "Glob": "list_dir",
        "Grep": "shell",
        "WebSearch": "browser",
        "WebFetch": "browser",
        "Skill": "skill",
    }

    @staticmethod
    def info() -> BackendInfo:
        return BackendInfo(
            name="claude_agent_sdk",
            display_name="Claude Agent SDK",
            capabilities=(
                Capability.STREAMING
                | Capability.TOOLS
                | Capability.MCP
                | Capability.MULTI_TURN
                | Capability.CUSTOM_SYSTEM_PROMPT
            ),
            builtin_tools=[
                "Bash",
                "Read",
                "Write",
                "Edit",
                "Glob",
                "Grep",
                "WebSearch",
                "WebFetch",
            ],
            tool_policy_map=ClaudeSDKBackend._TOOL_POLICY_MAP,
            required_keys=["anthropic_api_key"],
            supported_providers=["anthropic", "ollama", "openai_compatible"],
        )

    def __init__(self, settings: Settings):
        self.settings = settings
        self._stop_flag = False
        self._sdk_available = False
        self._cli_available = False  # Whether the `claude` CLI binary is installed
        self._cwd = settings.file_jail_path  # Default working directory
        self._policy = ToolPolicy(
            profile=settings.tool_profile,
            allow=settings.tools_allow,
            deny=settings.tools_deny,
        )

        # Persistent client — reuses subprocess across messages.
        # _client_in_use prevents concurrent queries on the same client
        # (cross-session messages fall back to stateless query()).
        self._client = None
        self._client_options_key: str | None = None
        self._client_in_use = False

        # SDK imports (set during initialization)
        self._query = None
        self._ClaudeSDKClient = None
        self._ClaudeAgentOptions = None
        self._HookMatcher = None
        self._AssistantMessage = None
        self._UserMessage = None
        self._SystemMessage = None
        self._ResultMessage = None
        self._TextBlock = None
        self._ToolUseBlock = None
        self._ToolResultBlock = None
        self._StreamEvent = None

        self._initialize()

    def _initialize(self) -> None:
        """Initialize the Claude Agent SDK with all imports."""
        try:
            # Core SDK imports
            # Message type imports
            # Content block imports
            from claude_agent_sdk import (
                AssistantMessage,
                ClaudeAgentOptions,
                ClaudeSDKClient,
                HookMatcher,
                ResultMessage,
                SystemMessage,
                TextBlock,
                ToolResultBlock,
                ToolUseBlock,
                UserMessage,
                query,
            )

            # Store references
            self._query = query
            self._ClaudeSDKClient = ClaudeSDKClient
            self._ClaudeAgentOptions = ClaudeAgentOptions
            self._HookMatcher = HookMatcher
            self._AssistantMessage = AssistantMessage
            self._UserMessage = UserMessage
            self._SystemMessage = SystemMessage
            self._ResultMessage = ResultMessage
            self._TextBlock = TextBlock
            self._ToolUseBlock = ToolUseBlock
            self._ToolResultBlock = ToolResultBlock

            # StreamEvent for token-by-token streaming (optional)
            try:
                from claude_agent_sdk import StreamEvent

                self._StreamEvent = StreamEvent
            except ImportError:
                self._StreamEvent = None
                logger.info("StreamEvent not available - coarse-grained streaming only")

            self._sdk_available = True

            # Check if the `claude` CLI binary is actually installed
            import shutil

            if shutil.which("claude"):
                self._cli_available = True
                logger.info("✓ Claude Agent SDK ready ─ cwd: %s", self._cwd)
            else:
                logger.warning(
                    "⚠️ Claude Code CLI not found on PATH. "
                    "Install with: npm install -g @anthropic-ai/claude-code"
                )

        except ImportError as e:
            logger.warning("⚠️ Claude Agent SDK not installed ─ pip install claude-agent-sdk")
            logger.debug("Import error: %s", e)
            self._sdk_available = False
        except Exception as e:
            logger.error(f"❌ Failed to initialize Claude Agent SDK: {e}")
            self._sdk_available = False

    def set_working_directory(self, path: Path) -> None:
        """Set the working directory for file operations."""
        self._cwd = path
        logger.info(f"📂 Working directory set to: {path}")

    def _is_dangerous_command(self, command: str) -> str | None:
        """Check if a command matches dangerous patterns.

        Args:
            command: Command string to check

        Returns:
            The matched pattern if dangerous, None otherwise
        """
        command_lower = command.lower()
        for pattern in DANGEROUS_PATTERNS:
            if pattern.lower() in command_lower:
                return pattern
        return None

    # Patterns that indicate an OS-level "open file" command.
    _FILE_OPEN_PATTERNS = [
        re.compile(
            r"(?:^|&&|\|\||;)\s*start\s+(?:\"\"?\s*)?(.+)",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?:^|&&|\|\||;)\s*explorer(?:\.exe)?\s+(.+)",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?:^|&&|\|\||;)\s*xdg-open\s+(.+)",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?:^|&&|\|\||;)\s*open\s+(?!-a)(.+)",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?:^|&&|\|\||;)\s*(?:powershell(?:\.exe)?\s+(?:-[Cc]ommand\s+)?)?"
            r"Invoke-Item\s+(.+)",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?:^|&&|\|\||;)\s*cmd\s+/[cC]\s+start\s+(?:\"\"?\s*)?(.+)",
            re.IGNORECASE,
        ),
    ]

    def _is_file_open_command(self, command: str) -> str | None:
        """Detect OS-level file-open commands and extract the file path.

        Returns the file path if the command is an OS open, or None.
        """
        stripped = command.strip()
        for pattern in self._FILE_OPEN_PATTERNS:
            m = pattern.search(stripped)
            if m:
                path = m.group(1).strip().strip("'\"")
                # Skip if it's opening a URL (http/https) — not a local file
                if path.startswith(("http://", "https://")):
                    return None
                return path
        return None

    async def _block_dangerous_hook(self, input_data, tool_use_id: str | None, context) -> dict:
        """PreToolUse hook to block dangerous commands.

        This hook is called before any Bash command is executed.
        Returns a deny decision for dangerous commands.

        The callback must be resilient — an unhandled exception here
        tears down the entire CLI stream.

        Args:
            input_data: PreToolUseHookInput (TypedDict with tool_name,
                tool_input, tool_use_id, etc.)
            tool_use_id: Match group or None
            context: HookContext from the SDK

        Returns:
            Empty dict to allow, or deny decision dict to block
        """
        try:
            tool_name = input_data.get("tool_name", "")
            tool_input = input_data.get("tool_input", {})

            # Only check Bash commands
            if tool_name != "Bash":
                return {}

            command = str(tool_input.get("command", ""))

            matched = self._is_dangerous_command(command)
            if matched:
                logger.warning(f"🛑 BLOCKED dangerous command: {command[:100]}")
                logger.warning(f"   └─ Matched pattern: {matched}")
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            f"PocketPaw security: '{matched}' pattern is blocked"
                        ),
                    }
                }

            # Redirect OS file-open commands to the in-app viewer.
            # Matches: start, explorer, xdg-open, open (macOS), Invoke-Item
            redirect = self._is_file_open_command(command)
            if redirect:
                logger.info("↩ Redirecting OS open command to open_in_explorer: %s", redirect)
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            "Do not use OS commands to open files. "
                            "Instead, use the PocketPaw in-app viewer:\n"
                            "python -m pocketpaw.tools.cli open_in_explorer "
                            f'\'{{"path": "{redirect}", "action": "view"}}\''
                        ),
                    }
                }

            logger.debug(f"✅ Allowed command: {command[:50]}...")
            return {}
        except Exception as e:
            logger.error(f"Hook callback error (allowing command): {e}")
            return {}

    def _extract_text_from_message(self, message: Any) -> str:
        """Extract text content from an AssistantMessage.

        Args:
            message: AssistantMessage with content blocks

        Returns:
            Concatenated text from all TextBlocks
        """
        if not hasattr(message, "content"):
            return ""

        content = message.content
        if content is None:
            return ""

        if isinstance(content, str):
            return content

        if isinstance(content, list):
            texts = []
            for block in content:
                # Check if it's a TextBlock
                if self._TextBlock and isinstance(block, self._TextBlock):
                    if hasattr(block, "text") and block.text:
                        texts.append(block.text)
                # Fallback: check for text attribute
                elif hasattr(block, "text") and isinstance(block.text, str):
                    texts.append(block.text)
            return "".join(texts)

        return ""

    def _extract_tool_info(self, message: Any) -> list[dict]:
        """Extract tool use information from an AssistantMessage.

        Args:
            message: AssistantMessage with content blocks

        Returns:
            List of tool use dicts with name and input
        """
        if not hasattr(message, "content") or message.content is None:
            return []

        tools = []
        for block in message.content:
            if self._ToolUseBlock and isinstance(block, self._ToolUseBlock):
                tools.append(
                    {
                        "name": getattr(block, "name", "unknown"),
                        "input": getattr(block, "input", {}),
                    }
                )
            elif hasattr(block, "name") and hasattr(block, "input"):
                # Fallback check
                tools.append(
                    {
                        "name": block.name,
                        "input": block.input,
                    }
                )
        return tools

    def _get_mcp_servers(self) -> dict[str, dict]:
        """Load enabled MCP server configs, filtered by tool policy.

        Returns a dict keyed by server name.  The SDK supports three
        transport types: stdio, sse, and http — each with its own
        TypedDict shape (McpStdioServerConfig, McpSSEServerConfig,
        McpHttpServerConfig).
        """
        try:
            from pocketpaw.mcp.config import load_mcp_config
        except ImportError:
            return {}

        configs = load_mcp_config()
        servers: dict[str, dict] = {}
        for cfg in configs:
            if not cfg.enabled:
                continue
            if not self._policy.is_mcp_server_allowed(cfg.name):
                logger.info("MCP server '%s' blocked by tool policy", cfg.name)
                continue

            if cfg.transport == "stdio":
                entry: dict = {"type": "stdio", "command": cfg.command}
                if cfg.args:
                    entry["args"] = cfg.args
                if cfg.env:
                    entry["env"] = cfg.env
            elif cfg.transport in ("http", "sse", "streamable-http"):
                if not cfg.url:
                    logger.warning("MCP server '%s' (%s) has no url", cfg.name, cfg.transport)
                    continue
                # Claude SDK expects "http" for both SSE and streamable-http
                sdk_type = "http" if cfg.transport == "streamable-http" else cfg.transport
                entry = {"type": sdk_type, "url": cfg.url}
                if cfg.env:
                    entry["headers"] = cfg.env
            else:
                logger.debug("Skipping MCP '%s' (unknown transport=%s)", cfg.name, cfg.transport)
                continue

            servers[cfg.name] = entry
        return servers

    @staticmethod
    def _merge_consecutive_roles(messages: list[dict]) -> list[dict]:
        """Merge consecutive messages with the same role for API compliance.

        The Anthropic API requires alternating user/assistant roles.
        Consecutive same-role messages are concatenated with newlines.
        """
        if not messages:
            return []
        merged: list[dict] = [messages[0].copy()]
        for msg in messages[1:]:
            if msg["role"] == merged[-1]["role"]:
                merged[-1]["content"] += "\n" + msg["content"]
            else:
                merged.append(msg.copy())
        return merged

    async def _fast_chat(
        self,
        message: str,
        *,
        system_prompt: str,
        history: list[dict] | None = None,
        model: str,
    ) -> AsyncIterator[AgentEvent]:
        """Direct Anthropic API path for simple messages.

        Bypasses the Claude CLI subprocess entirely, saving ~1.5-3s of
        process fork + Node.js startup + CLI initialization overhead.
        No tools are provided (simple messages don't need them).
        """
        try:
            import time

            from pocketpaw.llm.client import resolve_llm_client

            t0 = time.monotonic()
            llm = resolve_llm_client(self.settings)
            client = llm.create_anthropic_client()
            t1 = time.monotonic()
            logger.info("Fast-path: client created in %.0fms", (t1 - t0) * 1000)

            # Build API messages from history + current message
            api_messages: list[dict] = []
            if history:
                for msg in history:
                    role = msg.get("role", "user")
                    content = msg.get("content", "")
                    if role in ("user", "assistant") and content:
                        api_messages.append({"role": role, "content": content})
            api_messages.append({"role": "user", "content": message})

            # Merge consecutive same-role messages for API compliance
            api_messages = self._merge_consecutive_roles(api_messages)

            logger.info(
                "Fast-path: calling %s (system=%d chars, msgs=%d)",
                model,
                len(system_prompt),
                len(api_messages),
            )
            t2 = time.monotonic()

            async with client.messages.stream(
                model=model,
                system=system_prompt,
                messages=api_messages,
                max_tokens=1024,
            ) as stream:
                t3 = time.monotonic()
                logger.info("Fast-path: stream opened in %.0fms", (t3 - t2) * 1000)
                first_token = True
                async for text in stream.text_stream:
                    if first_token:
                        t4 = time.monotonic()
                        logger.info(
                            "Fast-path: first token in %.0fms (total %.0fms)",
                            (t4 - t3) * 1000,
                            (t4 - t0) * 1000,
                        )
                        first_token = False
                    if self._stop_flag:
                        logger.info("Fast-path: stop flag set, breaking stream")
                        break
                    yield AgentEvent(type="message", content=text)

            yield AgentEvent(type="done", content="")

        except Exception as e:
            from pocketpaw.llm.client import resolve_llm_client

            llm = resolve_llm_client(self.settings)
            logger.error("Fast-path API error: %s", e)
            yield AgentEvent(type="error", content=llm.format_api_error(e))

    async def _get_or_create_client(self, options: Any, *, session_key: str | None = None) -> Any:
        """Get or create a persistent ClaudeSDKClient.

        Reuses the existing subprocess if model, tools, **and session** haven't
        changed.  Different sessions get a fresh subprocess so the CLI's
        internal conversation context doesn't leak between chats.
        """
        import time

        key = (
            f"{session_key or ''}:"
            f"{getattr(options, 'model', '')}:{sorted(getattr(options, 'allowed_tools', []) or [])}"
        )

        if self._client is not None and self._client_options_key == key:
            logger.debug("Reusing persistent client (key=%s)", key)
            return self._client

        # Disconnect stale client
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None

        # Create and connect new client
        t0 = time.monotonic()
        self._client = self._ClaudeSDKClient(options=options)
        await self._client.connect()
        self._client_options_key = key
        t1 = time.monotonic()
        logger.info("Persistent client connected in %.0fms (key=%s)", (t1 - t0) * 1000, key)
        return self._client

    async def cleanup(self) -> None:
        """Disconnect the persistent client and release resources."""
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None
            self._client_options_key = None
            self._client_in_use = False
            logger.info("Persistent client disconnected")

    async def _resilient_receive(self, client):
        """Iterate over client messages, recovering from parse errors.

        Uses ``receive_messages()`` directly (not ``receive_response()``)
        and handles generator death from ``MessageParseError`` by
        re-creating the iterator from the same underlying anyio channel.

        When ``parse_message()`` raises inside the SDK's
        ``receive_messages()`` generator, the exception kills the entire
        generator chain.  The old ``_safe_iter`` wrapper caught the error
        and called ``continue``, but the generator was already dead — so
        the next ``__anext__()`` returned ``StopAsyncIteration`` and the
        loop exited early, leaving unconsumed events in the channel that
        leaked into the *next* turn.

        This method instead re-creates the ``receive_messages()``
        iterator after a parse error, which reads from the same
        underlying anyio memory channel and picks up where it left off.
        """
        _max_consecutive_errors = 50  # safety valve
        _consecutive = 0
        while _consecutive < _max_consecutive_errors:
            try:
                async for msg in client.receive_messages():
                    _consecutive = 0  # reset on every successful message
                    yield msg
                    if self._ResultMessage and isinstance(msg, self._ResultMessage):
                        return  # normal completion
                # Generator ended naturally (end-of-stream) without ResultMessage
                return
            except Exception as exc:
                if "MessageParseError" in type(exc).__name__:
                    _consecutive += 1
                    logger.debug(
                        "Skipping unrecognised SDK event (retry %d), re-creating iterator: %s",
                        _consecutive,
                        exc,
                    )
                    continue
                raise  # re-raise non-parse errors
        logger.error("Too many consecutive MessageParseErrors — aborting stream")

    async def run(
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list[dict] | None = None,
        session_key: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Process a message through Claude Agent SDK with streaming.

        Yields AgentEvent objects as the agent responds.
        """
        if not self._sdk_available:
            yield AgentEvent(
                type="error",
                content=(
                    "❌ Claude Agent SDK Python package not found.\n\n"
                    "Install with: pip install claude-agent-sdk\n\n"
                    "Or switch to **PocketPaw Native** backend in **Settings → General**."
                ),
            )
            return

        if not self._cli_available:
            yield AgentEvent(
                type="error",
                content=(
                    "❌ Claude Code CLI not found on this machine.\n\n"
                    "Install with: `npm install -g @anthropic-ai/claude-code`\n\n"
                    "Or switch to **PocketPaw Native** backend in "
                    "**Settings → General** — it uses the Anthropic API directly "
                    "and doesn't need the CLI."
                ),
            )
            return

        import os

        self._stop_flag = False

        # ── Prevent the SDK from closing stdin too early ──────────
        # When hooks are present the SDK's stream_input() waits for
        # the first ResultMessage before closing stdin.  The default
        # timeout is 60 s which is far too short for long-running
        # tool use (file search, code analysis, etc.).  Set to 24 h
        # so the agent can work as long as it needs.
        os.environ.setdefault(
            "CLAUDE_CODE_STREAM_CLOSE_TIMEOUT",
            str(24 * 60 * 60 * 1000),  # 24 hours in ms
        )

        try:
            # Resolve LLM provider early — needed for routing + env.
            # Use per-backend provider setting (defaults to "anthropic").
            # An API key is REQUIRED for Anthropic provider — OAuth tokens from
            # Claude Free/Pro/Max plans are not permitted for third-party use.
            # See: https://code.claude.com/docs/en/legal-and-compliance
            from pocketpaw.llm.client import resolve_llm_client

            provider = self.settings.claude_sdk_provider or "anthropic"
            llm = resolve_llm_client(self.settings, force_provider=provider)

            # ── API key check for Anthropic provider ──────────────
            if not (llm.is_ollama or llm.is_openai_compatible or llm.is_gemini):
                has_api_key = bool(llm.api_key or os.environ.get("ANTHROPIC_API_KEY"))
                if not has_api_key:
                    yield AgentEvent(
                        type="error",
                        content=(
                            "**API key required** -- The Claude SDK backend needs "
                            "an Anthropic API key.\n\n"
                            "**How to fix:**\n"
                            "1. Get an API key at "
                            "[console.anthropic.com](https://console.anthropic.com/api-keys)\n"
                            "2. Add it in **Settings > API Keys > Anthropic API Key**\n"
                            "3. Or set the `ANTHROPIC_API_KEY` environment variable\n\n"
                            "*Alternatively, switch to **Ollama (Local)** in Settings "
                            "> General for free local inference.*"
                        ),
                    )
                    return

            # Smart model routing — classify BEFORE prompt composition so we
            # can skip tool instructions for SIMPLE messages and dispatch to
            # the fast-path (direct API) for simple queries.
            is_simple = False
            selection = None
            if (
                self.settings.smart_routing_enabled
                and not llm.is_ollama
                and not llm.is_openai_compatible
                and not llm.is_gemini
            ):
                from pocketpaw.agents.model_router import ModelRouter, TaskComplexity

                model_router = ModelRouter(self.settings)
                selection = model_router.classify(message)
                is_simple = selection.complexity == TaskComplexity.SIMPLE
                logger.info(
                    "Smart routing: %s -> %s (%s)",
                    selection.complexity.value,
                    selection.model,
                    selection.reason,
                )

            # Fast path: bypass CLI subprocess entirely for simple messages.
            # Uses the Anthropic API directly (requires API key, already enforced above).
            has_api_key = bool(llm.api_key or os.environ.get("ANTHROPIC_API_KEY"))
            if is_simple and selection is not None and has_api_key:
                identity = system_prompt or _DEFAULT_IDENTITY
                async for event in self._fast_chat(
                    message,
                    system_prompt=identity,
                    history=history,
                    model=selection.model,
                ):
                    yield event
                return

            # System prompt — instructions are now part of identity
            # (injected by BootstrapContext.to_system_prompt() via INSTRUCTIONS.md)
            identity = system_prompt or _DEFAULT_IDENTITY
            final_prompt = identity

            # Inject session history into system prompt (SDK query() takes a single string)
            if history:
                lines = ["# Recent Conversation"]
                for msg in history:
                    role = msg.get("role", "user").capitalize()
                    content = msg.get("content", "")
                    # Truncate very long messages to keep prompt manageable
                    if len(content) > 500:
                        content = content[:500] + "..."
                    lines.append(f"**{role}**: {content}")
                final_prompt += "\n\n" + "\n".join(lines)

            # Build allowed tools list, filtered by tool policy
            all_sdk_tools = [
                "Bash",
                "Read",
                "Write",
                "Edit",
                "Glob",
                "Grep",
                "WebSearch",
                "WebFetch",
                "Skill",
            ]
            allowed_tools = [
                t
                for t in all_sdk_tools
                if self._policy.is_tool_allowed(self._TOOL_POLICY_MAP.get(t, t))
            ]
            if len(allowed_tools) < len(all_sdk_tools):
                blocked = set(all_sdk_tools) - set(allowed_tools)
                logger.info("Tool policy blocked SDK tools: %s", blocked)

            # Build hooks for security
            hooks = {
                "PreToolUse": [
                    self._HookMatcher(
                        matcher="Bash",  # Only hook Bash commands
                        hooks=[self._block_dangerous_hook],
                    )
                ]
            }

            # Build options
            options_kwargs = {
                "system_prompt": final_prompt,
                "allowed_tools": allowed_tools,
                "setting_sources": ["user", "project"],
                "hooks": hooks,
                "cwd": str(self._cwd),
                "max_turns": self.settings.claude_sdk_max_turns or None,
            }

            # Configure LLM provider for the Claude CLI subprocess.
            # Ollama/OpenAI-compat providers set their own env vars via to_sdk_env().
            sdk_env = llm.to_sdk_env()
            if not sdk_env:
                env_key = os.environ.get("ANTHROPIC_API_KEY")
                if env_key:
                    sdk_env = {"ANTHROPIC_API_KEY": env_key}

            # Strip nesting-detection env vars (set when launched from
            # a Claude Code terminal) so the subprocess starts cleanly.
            # These should already be removed by main(), but do it here
            # too as a safety net.
            for _strip_key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
                os.environ.pop(_strip_key, None)
            if sdk_env:
                options_kwargs["env"] = sdk_env
            if llm.is_ollama or llm.is_openai_compatible or llm.is_gemini:
                options_kwargs["model"] = llm.model

            # ── Debug logging for troubleshooting SDK startup ──
            import shutil as _shutil

            logger.info(
                "SDK launch: provider=%s, has_api_key=%s, "
                "CLAUDECODE=%s, CLAUDE_CODE_ENTRYPOINT=%s, "
                "ANTHROPIC_API_KEY=%s, sdk_env_keys=%s, "
                "cli_path=%s, cwd=%s",
                provider,
                bool(llm.api_key),
                os.environ.get("CLAUDECODE", "<unset>"),
                os.environ.get("CLAUDE_CODE_ENTRYPOINT", "<unset>"),
                "set" if os.environ.get("ANTHROPIC_API_KEY") else "<unset>",
                list(sdk_env.keys()) if sdk_env else "none",
                _shutil.which("claude") or "<not found>",
                self._cwd,
            )

            # Wire in MCP servers (policy-filtered)
            mcp_servers = self._get_mcp_servers()
            if mcp_servers:
                options_kwargs["mcp_servers"] = mcp_servers
                logger.info("MCP: passing %d servers to Claude SDK", len(mcp_servers))

            # Enable token-by-token streaming if StreamEvent is available
            if self._StreamEvent is not None:
                options_kwargs["include_partial_messages"] = True

            # Permission handling — PocketPaw runs headless (web/chat), so
            # there is no terminal to show interactive permission prompts.
            # bypassPermissions auto-approves ALL tool calls (including MCP).
            # Dangerous Bash commands are still caught by the PreToolUse hook.
            if self.settings.bypass_permissions:
                options_kwargs["permission_mode"] = "bypassPermissions"

            # Model selection for Anthropic providers:
            # 1. Smart routing (opt-in) — overrides with complexity-based model
            # 2. Explicit claude_sdk_model — user-chosen fixed model
            # 3. Neither set — let Claude Code CLI auto-select (recommended)
            if not (llm.is_ollama or llm.is_openai_compatible or llm.is_gemini):
                if self.settings.smart_routing_enabled:
                    from pocketpaw.agents.model_router import ModelRouter

                    model_router = ModelRouter(self.settings)
                    selection = model_router.classify(message)
                    options_kwargs["model"] = selection.model
                elif self.settings.claude_sdk_model:
                    options_kwargs["model"] = self.settings.claude_sdk_model

            # Capture stderr for better error diagnostics
            _stderr_lines: list[str] = []

            def _on_stderr(line: str) -> None:
                _stderr_lines.append(line)
                logger.debug("Claude CLI stderr: %s", line)

            options_kwargs["stderr"] = _on_stderr

            # Create options (after all kwargs are set, including model)
            options = self._ClaudeAgentOptions(**options_kwargs)

            logger.debug(f"🚀 Starting Claude Agent SDK query: {message[:100]}...")

            # Try persistent client first, fall back to stateless query.
            # _client_in_use guard prevents concurrent queries on the same
            # subprocess — cross-session messages fall back to stateless query.
            event_stream = None
            logger.info(
                "SDK dispatch: _client_in_use=%s, session_key=%s",
                self._client_in_use,
                session_key,
            )
            _persistent_client = None
            if not self._client_in_use:
                try:
                    self._client_in_use = True
                    _persistent_client = await self._get_or_create_client(
                        options, session_key=session_key
                    )
                    logger.info("Persistent client: sending query (%d chars)", len(message))
                    await _persistent_client.query(message)
                    # Use _resilient_receive instead of receive_response() +
                    # _safe_iter.  This handles MessageParseError by
                    # re-creating the iterator from the same anyio channel,
                    # preventing stale events from leaking into the next turn.
                    event_stream = self._resilient_receive(_persistent_client)
                    logger.info("Persistent client: _resilient_receive() ready")
                except Exception as client_err:
                    logger.warning(
                        "Persistent client failed, falling back to stateless query: %s",
                        client_err,
                    )
                    # Log stderr lines captured so far
                    if _stderr_lines:
                        logger.warning(
                            "CLI stderr during persistent client failure:\n%s",
                            "\n".join(_stderr_lines),
                        )
                    # Clear broken client so next call creates a fresh one
                    self._client = None
                    self._client_options_key = None
                    self._client_in_use = False
                    _persistent_client = None

            if event_stream is None:
                logger.info("Starting stateless query (fallback — _client_in_use was True)")
                event_stream = self._query(prompt=message, options=options)

            # State tracking for StreamEvent deduplication
            _streamed_via_events = False
            _announced_tools: set[str] = set()
            _event_count = 0
            _saw_result = False  # Track if ResultMessage was consumed

            # Stream responses — release the persistent client guard when done
            try:
                async for event in event_stream:
                    _event_count += 1
                    if _event_count <= 3:
                        logger.info(
                            "SDK event #%d: type=%s",
                            _event_count,
                            type(event).__name__,
                        )
                    if self._stop_flag:
                        logger.info("🛑 Stop flag set, breaking stream")
                        break

                    # Handle different message types using isinstance checks

                    # ========== StreamEvent - token-by-token streaming ==========
                    if self._StreamEvent and isinstance(event, self._StreamEvent):
                        raw = getattr(event, "event", None) or {}
                        event_type = raw.get("type", "")
                        delta = raw.get("delta", {})

                        if event_type == "content_block_delta":
                            if "text" in delta:
                                yield AgentEvent(type="message", content=delta["text"])
                                _streamed_via_events = True
                            elif "thinking" in delta:
                                yield AgentEvent(type="thinking", content=delta["thinking"])
                        elif event_type == "content_block_start":
                            cb = raw.get("content_block", {})
                            if cb.get("type") == "tool_use":
                                tool_name = cb.get("name", "unknown")
                                _announced_tools.add(tool_name)
                                yield AgentEvent(
                                    type="tool_use",
                                    content=f"Using {tool_name}...",
                                    metadata={"name": tool_name, "input": {}},
                                )
                        elif event_type == "content_block_stop":
                            if getattr(event, "_block_type", None) == "thinking":
                                yield AgentEvent(type="thinking_done", content="")
                        continue

                    # ========== SystemMessage - metadata, skip ==========
                    if self._SystemMessage and isinstance(event, self._SystemMessage):
                        subtype = getattr(event, "subtype", "")
                        logger.debug(f"SystemMessage: {subtype}")
                        continue

                    # ========== UserMessage - extract media from tool results ==========
                    if self._UserMessage and isinstance(event, self._UserMessage):
                        # UserMessages in multi-turn SDK flow contain ToolResultBlocks
                        # with the raw output of Bash commands (including media tags).
                        if hasattr(event, "content") and isinstance(event.content, list):
                            for block in event.content:
                                if not (
                                    self._ToolResultBlock
                                    and isinstance(block, self._ToolResultBlock)
                                ):
                                    continue
                                block_content = getattr(block, "content", "")
                                if isinstance(block_content, str):
                                    result_text = block_content
                                elif isinstance(block_content, list):
                                    result_text = " ".join(
                                        getattr(b, "text", "")
                                        for b in block_content
                                        if hasattr(b, "text")
                                    )
                                else:
                                    continue
                                if result_text and "<!-- media:" in result_text:
                                    yield AgentEvent(
                                        type="tool_result",
                                        content=result_text,
                                        metadata={"name": "bash"},
                                    )
                        logger.debug("UserMessage processed")
                        continue

                    # ========== AssistantMessage - main content ==========
                    if self._AssistantMessage and isinstance(event, self._AssistantMessage):
                        if not _streamed_via_events:
                            text = self._extract_text_from_message(event)
                            if text:
                                yield AgentEvent(type="message", content=text)

                        tools = self._extract_tool_info(event)
                        for tool in tools:
                            if tool["name"] not in _announced_tools:
                                logger.info(f"🔧 Tool: {tool['name']}")
                                yield AgentEvent(
                                    type="tool_use",
                                    content=f"Using {tool['name']}...",
                                    metadata={
                                        "name": tool["name"],
                                        "input": tool["input"],
                                    },
                                )

                        _streamed_via_events = False
                        _announced_tools.clear()
                        continue

                    # ========== ResultMessage - final result ==========
                    if self._ResultMessage and isinstance(event, self._ResultMessage):
                        _saw_result = True
                        is_error = getattr(event, "is_error", False)
                        result = getattr(event, "result", "")

                        if is_error:
                            logger.error(f"ResultMessage error: {result}")
                            yield AgentEvent(type="error", content=str(result))
                        else:
                            logger.debug(f"ResultMessage: {str(result)[:100]}...")
                        continue

                    # ========== Unknown event type - log it ==========
                    event_class = event.__class__.__name__
                    logger.debug(f"Unknown event type: {event_class}")
            finally:
                # ── Drain remaining events if the main loop exited
                # before consuming the ResultMessage.  For the persistent
                # client, _resilient_receive handles this.  For the
                # stateless path or early-break scenarios (stop flag),
                # we still need to ensure the pipe is clean. ──
                if _persistent_client is not None and not _saw_result and self._client is not None:
                    logger.warning(
                        "Main loop exited without ResultMessage — "
                        "destroying persistent client to avoid stale data"
                    )
                    try:
                        await self._client.disconnect()
                    except Exception:
                        pass
                    self._client = None
                    self._client_options_key = None

                self._client_in_use = False
                logger.info(
                    "SDK stream finished: %d events, _client_in_use=False",
                    _event_count,
                )

            yield AgentEvent(type="done", content="")

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Claude Agent SDK error: {error_msg}", exc_info=True)

            # Log any stderr captured from the CLI subprocess
            if _stderr_lines:
                logger.error("CLI stderr output:\n%s", "\n".join(_stderr_lines))

            # Clear client on unexpected errors
            self._client = None
            self._client_options_key = None
            self._client_in_use = False

            # Provide helpful error messages
            if "CLINotFoundError" in error_msg:
                yield AgentEvent(
                    type="error",
                    content=(
                        "❌ Claude Code CLI not found.\n\n"
                        "Install with: npm install -g @anthropic-ai/claude-code\n\n"
                        "Or switch to a different backend in "
                        "**Settings → General**."
                    ),
                )
            else:
                stderr_text = "\n".join(_stderr_lines) if _stderr_lines else ""
                yield AgentEvent(
                    type="error",
                    content=llm.format_api_error(e, stderr=stderr_text),
                )

    async def stop(self) -> None:
        """Stop the agent execution and disconnect persistent client."""
        self._stop_flag = True
        if self._client is not None:
            try:
                await self._client.interrupt()
            except Exception:
                pass
        await self.cleanup()
        logger.info("🛑 Claude Agent SDK stop requested")

    async def get_status(self) -> dict:
        """Get current agent status."""
        ready = self._sdk_available and self._cli_available
        return {
            "backend": "claude_agent_sdk",
            "available": ready,
            "sdk_installed": self._sdk_available,
            "cli_installed": self._cli_available,
            "running": not self._stop_flag,
            "cwd": str(self._cwd),
            "features": ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebSearch", "WebFetch"]
            if ready
            else [],
        }


# Backward-compat aliases
ClaudeAgentSDK = ClaudeSDKBackend
ClaudeAgentSDKWrapper = ClaudeSDKBackend
