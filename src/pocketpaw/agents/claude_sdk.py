"""
Claude Agent SDK backend for PocketPaw.
Updated: 2026-05-21 — Gate the ``pocketpaw_planner`` in-process MCP server
  behind an explicit policy opt-in (``is_mcp_server_explicitly_allowed``).
  It was the only in-process MCP server with no gate, so the
  ``plan_project`` tool schema loaded into every agent run. It now
  registers only when the agent opts in. ``__init__`` accepts an optional
  ``policy`` so AgentPool can inject a per-agent ToolPolicy carrying that
  opt-in; when omitted the policy is built from settings as before.
Updated: 2026-05-20 — Fix concurrency lease race in run(). On every exit path
  (the finally block AND the outer except handler) run() cleared the shared
  self._client_in_use flag and nulled self._client unconditionally, so a
  non-owning run — a stateless-fallback run, or one that failed before
  acquiring the lease — would steal a still-streaming sibling persistent run's
  lease and destroy its subprocess. run() now tracks ownership with a local
  acquired_lease flag (declared above the try so it is in scope for the except
  handler) and gates the flag clear and the persistent-client teardown on it
  on both exit paths — only the run that actually acquired the lease may
  release it or disconnect the shared subprocess. The event_stream.aclose()
  in the finally is unaffected: a run always owns its own stream.
Updated: 2026-03-11 — Always bypass permissions in headless mode. Without this,
  tool calls (like memory save via Bash) hang on messaging channels (Telegram,
  Discord, Slack) because there's no terminal to approve permission prompts.

Uses the official Claude Agent SDK (pip install claude-agent-sdk) which provides:
- Built-in tools: Bash, Read, Write, Edit, Glob, Grep, WebSearch, WebFetch
- Streaming responses
- PreToolUse hooks for security
- Permission management
- MCP server support for custom tools
"""

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from pocketpaw.agents.backend import BackendInfo, BaseAgentBackend, Capability
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.config import Settings
from pocketpaw.security.rails import is_substring_blocked
from pocketpaw.tools.policy import OPT_IN_MCP_SERVERS, ToolPolicy

logger = logging.getLogger(__name__)

# Default identity fallback (used when AgentContextBuilder prompt is not available)
_DEFAULT_IDENTITY = (
    "You are PocketPaw, a helpful AI assistant running locally on the user's computer."
)

_HTTP_TRANSPORTS: frozenset[str] = frozenset({"http", "sse", "streamable-http"})


class ClaudeSDKBackend(BaseAgentBackend):
    """Claude Agent SDK backend — the recommended default.

    Provides all built-in tools (Bash, Read, Write, Edit, Glob, Grep,
    WebSearch, WebFetch), streaming responses, PreToolUse hooks for
    security, and MCP server support.

    Requires: pip install claude-agent-sdk
    """

    _TOOL_POLICY_MAP: dict[str, str] = {
        # NOTE: is_tool_allowed() returns True for any key not explicitly
        # denied when the profile is 'full' (empty _allowed_set). For
        # restrictive profiles ('minimal', 'coding') it returns False for
        # any key absent from the resolved allow set. 'Agent' therefore
        # MUST have an explicit entry here; without it, any registered
        # subagent (general-purpose claude_agent_sdk capability) would
        # be silently blocked for every non-full profile. Mapped to
        # 'shell' because invoking a subagent has comparable privilege
        # scope to running a shell command — the gating is deliberately
        # conservative.
        "Agent": "shell",
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
            supported_providers=[
                "anthropic",
                "ollama",
                "openrouter",
                "openai_compatible",
                "litellm",
            ],
        )

    def __init__(self, settings: Settings, policy: ToolPolicy | None = None):
        self.settings = settings
        self._stop_flag = False
        self._sdk_available = False
        self._cli_available = False  # Whether the `claude` CLI binary is installed
        self._cwd = settings.file_jail_path  # Default working directory
        # ``policy`` lets a caller (AgentPool) inject a per-agent
        # ToolPolicy — e.g. one that opts the agent into the planner MCP
        # server. When omitted, build the process-wide policy from
        # settings, which is the behaviour every other caller relies on.
        self._policy = policy or ToolPolicy(
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
                    "Install with: npm install -g @anthropic-ai/claude-code "
                    "and set ANTHROPIC_API_KEY, or switch to a different backend in Settings."
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

        Uses both regex patterns (for complex matching) and substring
        patterns (for literal matches).

        Args:
            command: Command string to check

        Returns:
            The matched pattern if dangerous, None otherwise
        """
        # Primary: regex matching (catches obfuscation, spacing tricks)
        from pocketpaw.security.rails import COMPILED_DANGEROUS_PATTERNS

        for pattern in COMPILED_DANGEROUS_PATTERNS:
            if pattern.search(command):
                return pattern.pattern

        # Secondary: substring matching (catches simple literal fragments).
        # is_substring_blocked() applies .lower() on both sides so that
        # uppercase variants like "SUDO RM" are caught (OWASP A01).
        return is_substring_blocked(command)

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
                # Scrub before logging — dangerous commands routinely carry
                # Authorization headers or API keys inline (#893).
                from pocketpaw.security.scrub import scrub_command

                safe_command = scrub_command(command)
                logger.warning("🛑 BLOCKED dangerous command: %s", safe_command[:100])
                logger.warning("   └─ Matched pattern: %s", matched)
                try:
                    from pocketpaw.security.audit import (
                        AuditEvent,
                        AuditSeverity,
                        get_audit_logger,
                    )

                    get_audit_logger().log(
                        AuditEvent.create(
                            severity=AuditSeverity.ALERT,
                            actor="agent",
                            action="dangerous_command_blocked",
                            target="bash",
                            status="block",
                            command=safe_command[:500],
                            matched_pattern=matched,
                        )
                    )
                except Exception:
                    pass  # Don't let audit failure break the hook
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
            logger.error(f"Hook callback error (blocking command as precaution): {e}")
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        "Safety hook encountered an internal error — "
                        "blocking command as a precaution"
                    ),
                }
            }

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

    # MCP servers whose functionality is already provided by Claude Code's
    # built-in WebSearch tool.  Passing these causes duplicate/conflicting
    # search behaviour and wastes context on redundant tool definitions.
    _BUILTIN_SEARCH_MCP_NAMES = frozenset(
        {
            "brave-search",
            "tavily-search",
            "exa-search",
            "Brave Search",
            "Tavily Search",
            "Exa Search",
        }
    )

    def _get_mcp_servers(self) -> dict[str, dict]:
        """Load enabled MCP server configs, filtered by tool policy.

        Returns a dict keyed by server name.  The SDK supports three
        transport types: stdio, sse, and http — each with its own
        TypedDict shape (McpStdioServerConfig, McpSSEServerConfig,
        McpHttpServerConfig).

        Web search MCP servers (Tavily, Brave, Exa) are excluded because
        Claude Code already provides a built-in WebSearch tool.
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
            if cfg.name in self._BUILTIN_SEARCH_MCP_NAMES:
                logger.info(
                    "MCP server '%s' skipped — Claude Code has built-in WebSearch", cfg.name
                )
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
            elif cfg.transport in _HTTP_TRANSPORTS:
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

        # In-process MCP server: ripple widget-spec lookups (get_widget_spec,
        # get_inline_widget_help). Pure core — the ripple manifest / inline
        # catalog have no cloud dependency, so this server is always built
        # locally. Why in-process MCP at all: the rippleSpec.ui tree can be
        # tens of KB, which would blow the Windows CLI command-line limit if
        # embedded in the system prompt.
        try:
            from pocketpaw.agents.sdk_mcp_widgets import build_widgets_context_server

            widgets_server = build_widgets_context_server()
            if widgets_server is not None:
                name, cfg_entry = widgets_server
                if self._policy.is_mcp_server_allowed(name):
                    servers[name] = cfg_entry
                else:
                    logger.info("MCP server '%s' blocked by tool policy", name)
        except Exception as exc:  # noqa: BLE001
            logger.debug("pocketpaw_widgets MCP server not registered: %s", exc)

        # EE-provided in-process MCP servers — cloud pocket context, Mission
        # Control tasks, the planner, and the pocket specialist. Discovered
        # via the ``pocketpaw.mcp_servers`` entry-point (see
        # pocketpaw_ee.extensions); an OSS install registers none and this
        # loop is a no-op.
        #
        # Most of these servers are ambient: allow-by-default policy lets
        # them register on every agent run. The planner is the exception —
        # it is *opt-in, not ambient*. Most agent runs never plan a
        # project, and carrying the ``plan_project`` schema in every
        # context is dead weight. For a server in ``OPT_IN_MCP_SERVERS``
        # the loop uses ``is_mcp_server_explicitly_allowed``, which
        # registers it only when the policy's ``mcp_servers_allow`` set
        # names it. AgentPool builds that set from the cloud agent's
        # ``tools`` field — an agent enables the planner by listing the
        # bare token ``pocketpaw_planner`` there. Deny still wins.
        from pocketpaw._registry import providers as _ext_providers

        for provider in _ext_providers("pocketpaw.mcp_servers"):
            try:
                built = provider.build_server()
            except Exception as exc:  # noqa: BLE001
                logger.debug("MCP server provider %r failed: %s", provider, exc)
                continue
            if built is None:
                continue
            name, cfg_entry = built
            if name in OPT_IN_MCP_SERVERS:
                if not self._policy.is_mcp_server_explicitly_allowed(name):
                    logger.debug(
                        "MCP server '%s' not registered — agent has not opted "
                        "in (add '%s' to the agent's tools)",
                        name,
                        name,
                    )
                    continue
            elif not self._policy.is_mcp_server_allowed(name):
                logger.info("MCP server '%s' blocked by tool policy", name)
                continue
            servers[name] = cfg_entry

        return servers

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
            except Exception as e:
                logger.debug("Failed to disconnect Claude client: %s", e)
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
            except Exception as e:
                logger.debug("Failed to disconnect Claude client: %s", e)
            self._client = None
            self._client_options_key = None
            self._client_in_use = False
            logger.info("Persistent client disconnected")

    async def _resilient_query(self, prompt: str, options):
        """Wrap stateless _query with MessageParseError recovery."""
        try:
            async for event in self._query(prompt=prompt, options=options):
                yield event
        except Exception as exc:
            if "MessageParseError" in type(exc).__name__:
                logger.warning("Skipping unrecognised SDK event in stateless query: %s", exc)
            else:
                raise

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
                    "The Claude Agent SDK backend requires the CLI. To fix this:\n\n"
                    "**Install Claude Code CLI:**\n"
                    "- Windows: `irm https://claude.ai/install.ps1 | iex`\n"
                    "- macOS/Linux: `curl -fsSL https://claude.ai/install.sh | bash`\n"
                    "- Or: `npm install -g @anthropic-ai/claude-code`\n\n"
                    "Then set your `ANTHROPIC_API_KEY` in **Settings → General**.\n\n"
                    "Or switch to a different backend in **Settings → General** "
                    "(OpenAI Agents, Google ADK, Codex, etc.) that doesn't need the CLI."
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

        _stderr_lines: list[str] = []

        # Ownership flag — True only if THIS run acquired the shared
        # _client_in_use lease. Declared above the try/except so it is always
        # in scope in the except handler (an exception can fire before the
        # dispatch block runs). Both the finally block and the except handler
        # gate the lease clear and the persistent-client teardown on this so a
        # non-owning run (stateless fallback, or a failure before acquisition)
        # can never release a sibling's lease or destroy its subprocess.
        acquired_lease = False
        try:
            # Resolve LLM provider early -- needed for routing + env.
            # Use per-backend provider setting (defaults to "anthropic").
            # An API key is REQUIRED for Anthropic provider -- OAuth tokens from
            # Claude Free/Pro/Max plans are not permitted for third-party use.
            # See: https://code.claude.com/docs/en/legal-and-compliance
            from pocketpaw.llm.client import resolve_llm_client

            provider = self.settings.claude_sdk_provider or "anthropic"
            llm = resolve_llm_client(self.settings, force_provider=provider)

            # ── API key check for Anthropic provider ──────────────
            # Skip if using a non-Anthropic provider, or if the active
            # provider is claude_code (it handles OAuth auth via its CLI).
            _is_claude_code_provider = provider in ("claude_code", "claude_agent_sdk")
            is_non_anthropic = (
                llm.is_ollama
                or llm.is_openai_compatible
                or llm.is_gemini
                or llm.is_litellm
                or llm.is_openrouter
            )
            # if not is_non_anthropic:
            #     has_api_key = bool(llm.api_key or os.environ.get("ANTHROPIC_API_KEY"))
            #     if not has_api_key and not is_claude_code_provider:
            #         yield AgentEvent(
            #             type="error",
            #             content=(
            #                 "**API key required** -- The Claude SDK backend needs "
            #                 "an Anthropic API key.\n\n"
            #                 "**How to fix:**\n"
            #                 "1. Get an API key at "
            #                 "[console.anthropic.com](https://console.anthropic.com/settings/keys)\n"
            #                 "2. Add it in **Settings > API Keys > Anthropic API Key**\n"
            #                 "3. Or set the `ANTHROPIC_API_KEY` environment variable\n\n"
            #                 "*Alternatively, switch to **Ollama (Local)** in Settings "
            #                 "> General for free local inference.*"
            #             ),
            #         )
            #         return

            # Smart model routing — classify complexity to pick the model tier.
            # All messages go through the Claude Code CLI subprocess, which
            # handles conversation compaction automatically (PreCompact hook).
            selection = None
            if self.settings.smart_routing_enabled and not is_non_anthropic:
                from pocketpaw.agents.model_router import ModelRouter

                model_router = ModelRouter(self.settings)
                selection = model_router.classify(message)
                logger.info(
                    "Smart routing: %s -> %s (%s)",
                    selection.complexity.value,
                    selection.model,
                    selection.reason,
                )

            # System prompt — instructions are now part of identity
            # (injected by BootstrapContext.to_system_prompt() via INSTRUCTIONS.md)
            identity = system_prompt or _DEFAULT_IDENTITY

            # Inject connector instructions so the agent can use data sources
            try:
                from pocketpaw.connectors.registry import ConnectorRegistry

                reg = ConnectorRegistry()
                if reg.available:
                    names = ", ".join(c["name"] for c in reg.available)
                    identity += (
                        "\n\n# Data Connectors\n"
                        f"Available connectors: {names}\n"
                        "To manage connectors, use Bash to call the local API:\n"
                        "- List: curl -s http://localhost:8888/api/v1/connectors\n"
                        "- Detail: curl -s http://localhost:8888/api/v1/connectors/<name>\n"
                        "- Connect: curl -s -X POST "
                        "http://localhost:8888/api/v1/connectors/connect "
                        "-H 'Content-Type: application/json' "
                        '-d \'{"connector_name":"<name>","config":{...}}\'\n'
                        "- Execute: curl -s -X POST "
                        "http://localhost:8888/api/v1/connectors/execute "
                        "-H 'Content-Type: application/json' "
                        '-d \'{"connector_name":"<name>","action":"<action>"'
                        ',"params":{...}}\'\n'
                        "- Disconnect: curl -s -X POST "
                        "http://localhost:8888/api/v1/connectors/disconnect "
                        "-H 'Content-Type: application/json' "
                        '-d \'{"connector_name":"<name>"}\'\n'
                    )
            except Exception:
                pass  # Don't break agent if connector registry fails

            # Inject prior turns into the system prompt at connect time. The
            # persistent ClaudeSDKClient accumulates new turns natively after
            # connect, but a fresh subprocess (after eviction, restart, or
            # session switch) has empty native history — without this, those
            # cold-start runs lose all conversation context. Reused clients
            # keep the prompt set at first connect and ignore later option
            # changes, so there's no duplication on the warm path.
            final_prompt = identity
            if history:
                lines = ["# Recent Conversation"]
                for msg in history:
                    role = msg.get("role", "user").capitalize()
                    content = msg.get("content", "")
                    if len(content) > 2000:
                        content = content[:2000] + "..."
                    lines.append(f"**{role}**: {content}")
                final_prompt += "\n\n" + "\n".join(lines)

            # Pocket sessions don't need shell or filesystem access — the
            # MCP pocket tools (get_pocket / list_pockets / set_state /
            # set_node_prop / add_node / etc.) are the complete interface.
            # Detect via the <pocket-scope> marker every pocket prompt
            # carries; lock tools down to delegation + web + pocket MCP.
            #
            # Without this gate, the agent has been observed reaching for
            # shell introspection (e.g. `env | grep pocket; curl localhost`)
            # to "figure out" pocket state, which trips the security rails
            # AND is the wrong path — the MCP tools already expose
            # everything the agent needs.
            is_pocket_session = "<pocket-scope>" in (final_prompt or "")

            if is_pocket_session:
                all_sdk_tools = ["Agent", "WebSearch", "WebFetch"]
                logger.info(
                    "Pocket session detected — tool surface locked to %s",
                    all_sdk_tools,
                )
            else:
                all_sdk_tools = [
                    "Agent",
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

            # In-process MCP tool ids must be on the allowlist to be
            # callable. The ripple widget-spec tools are core; the cloud
            # pocket / Mission Control tasks / planner / pocket-specialist
            # ids come from the ``pocketpaw.mcp_servers`` providers (none on
            # an OSS install). Mutations are NOT here — pocket writes flow
            # through the pocket_specialist create/edit tools.
            #
            # Opt-in servers (the planner) are skipped here unless the
            # policy opts them in, mirroring the registration gate in
            # ``_get_mcp_servers``. An allowlist id without a registered
            # server is harmless, but keeping the two gates consistent
            # avoids a misleading entry. Tool ids follow the
            # ``mcp__<server>__<tool>`` convention, so the server name is
            # the segment between the first and second ``__``.
            from pocketpaw._registry import providers as _ext_providers
            from pocketpaw.agents.sdk_mcp_widgets import WIDGET_TOOL_IDS

            allowed_tools.extend(WIDGET_TOOL_IDS)
            for provider in _ext_providers("pocketpaw.mcp_servers"):
                try:
                    tool_ids = list(provider.tool_ids())
                except Exception as exc:  # noqa: BLE001
                    logger.debug("MCP provider tool ids not added to allowlist: %s", exc)
                    continue
                for tool_id in tool_ids:
                    parts = tool_id.split("__")
                    server = parts[1] if len(parts) >= 3 and parts[0] == "mcp" else ""
                    if server in OPT_IN_MCP_SERVERS and not (
                        self._policy.is_mcp_server_explicitly_allowed(server)
                    ):
                        continue
                    allowed_tools.append(tool_id)

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
            #
            # Windows note: CreateProcess caps the entire command line at
            # ~32,767 chars. The SDK passes string ``system_prompt`` inline
            # via ``--system-prompt``; long KB/identity blobs blow that limit
            # and surface as a misleading ``CLINotFoundError``. Since SDK
            # 0.1.72 we can pass a ``SystemPromptFile`` dict instead, which
            # the CLI reads via ``--system-prompt-file <path>``.
            system_prompt_arg: Any = final_prompt
            if os.name == "nt" and len(final_prompt) > 24_000:
                runtime_dir = Path.home() / ".pocketpaw" / "runtime"
                runtime_dir.mkdir(parents=True, exist_ok=True)
                prompt_path = runtime_dir / "system_prompt.md"
                prompt_path.write_text(final_prompt, encoding="utf-8")
                system_prompt_arg = {"type": "file", "path": str(prompt_path)}
                logger.info(
                    "System prompt %d chars exceeds Windows CLI safe limit; "
                    "passing via --system-prompt-file %s",
                    len(final_prompt),
                    prompt_path,
                )

            options_kwargs = {
                "system_prompt": system_prompt_arg,
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

            # Pass Claude Code OAuth token (Max/Pro subscription in Docker/headless)
            oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
            if oauth_token:
                sdk_env = sdk_env or {}
                sdk_env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token

            # Strip nesting-detection env vars (set when launched from
            # a Claude Code terminal) so the subprocess starts cleanly.
            # These should already be removed by main(), but do it here
            # too as a safety net.
            for _strip_key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
                os.environ.pop(_strip_key, None)
            if sdk_env:
                options_kwargs["env"] = sdk_env
            if is_non_anthropic:
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

            # Permission handling — PocketPaw always runs headless (web dashboard,
            # Telegram, Discord, Slack, etc.) with no terminal for interactive
            # permission prompts. Without bypassPermissions, tool calls that need
            # approval (like Bash — used by memory save, web search, etc.) hang
            # indefinitely on messaging channels.
            # Dangerous Bash commands are still caught by the PreToolUse hook.
            options_kwargs["permission_mode"] = "bypassPermissions"

            # Model selection for Anthropic providers:
            # 1. Smart routing (opt-in) — overrides with complexity-based model
            # 2. Explicit claude_sdk_model — user-chosen fixed model
            # 3. Neither set — let Claude Code CLI auto-select (recommended)
            if not is_non_anthropic:
                if self.settings.smart_routing_enabled:
                    from pocketpaw.agents.model_router import ModelRouter

                    model_router = ModelRouter(self.settings)
                    selection = model_router.classify(message)
                    options_kwargs["model"] = selection.model
                elif self.settings.claude_sdk_model:
                    options_kwargs["model"] = self.settings.claude_sdk_model

            # Capture stderr for better error diagnostics
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
                    acquired_lease = True
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
                    # Clear broken client so next call creates a fresh one.
                    # This run is falling back to stateless: it no longer owns
                    # the persistent client, so drop the lease and the
                    # ownership flag so the finally/except teardown below
                    # cannot misfire on a client this run no longer holds.
                    self._client = None
                    self._client_options_key = None
                    self._client_in_use = False
                    acquired_lease = False
                    _persistent_client = None

            if event_stream is None:
                logger.info("Starting stateless query (fallback — _client_in_use was True)")
                # final_prompt already carries Mongo history (injected above),
                # so the stateless path uses the same options as the persistent
                # path — no separate system prompt swap is needed.
                event_stream = self._resilient_query(prompt=message, options=options)

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
                                if result_text:
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

                        # Extract token usage from ResultMessage
                        # Per SDK docs: ResultMessage has total_cost_usd and usage dict
                        total_cost = getattr(event, "total_cost_usd", None)
                        usage = getattr(event, "usage", None) or {}
                        if isinstance(usage, dict) and (usage or total_cost):
                            _model_name = options_kwargs.get("model", "claude")
                            yield AgentEvent(
                                type="token_usage",
                                content="",
                                metadata={
                                    "input_tokens": usage.get("input_tokens", 0),
                                    "output_tokens": usage.get("output_tokens", 0),
                                    "cached_input_tokens": usage.get("cache_read_input_tokens", 0)
                                    + usage.get("cache_creation_input_tokens", 0),
                                    "total_cost_usd": total_cost,
                                    "model": _model_name
                                    if isinstance(_model_name, str)
                                    else "claude",
                                    "backend": "claude_agent_sdk",
                                },
                            )

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
                # Only a run that actually acquired the lease may tear down
                # the shared persistent client — a stateless-fallback run does
                # not own it and must leave a sibling's subprocess alone.
                if (
                    acquired_lease
                    and _persistent_client is not None
                    and not _saw_result
                    and self._client is not None
                ):
                    logger.warning(
                        "Main loop exited without ResultMessage — "
                        "destroying persistent client to avoid stale data"
                    )
                    try:
                        await self._client.disconnect()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("Failed to disconnect client during cleanup: %s", exc)
                    self._client = None
                    self._client_options_key = None

                # Only release the lease if this run acquired it. Clearing it
                # unconditionally would steal a sibling persistent run's lease.
                if acquired_lease:
                    self._client_in_use = False
                logger.info(
                    "SDK stream finished: %d events, _client_in_use=%s",
                    _event_count,
                    self._client_in_use,
                )

                # ── Close the inner async generator LAST. ──
                # ``_resilient_receive`` / ``_resilient_query`` spawn
                # background ``asend`` tasks under the hood; without
                # ``aclose()`` those tasks linger in the loop's pending
                # set until GC, surfacing as
                # ``Task exception was never retrieved`` +
                # ``StopAsyncIteration`` log noise on every turn (most
                # visible right after the soul-mutation hook fires).
                #
                # Order matters: aclose runs AFTER the drain decision
                # has read ``_saw_result`` so closing the generator
                # cannot influence that branch. Idempotent + safe on a
                # generator that already exited cleanly.
                close = getattr(event_stream, "aclose", None)
                if close is not None:
                    try:
                        await close()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("event_stream aclose error (non-fatal): %s", exc)

            yield AgentEvent(type="done", content="")

        except Exception as e:
            error_msg = str(e)

            # ── Detect Bun/subprocess crash and auto-retry once ──
            # The bundled claude.exe uses Bun, which can crash on Windows
            # with "switch on corrupt value" (exit code 3).
            stderr_text = "\n".join(_stderr_lines) if _stderr_lines else ""
            _is_bun_crash = "exit code" in error_msg.lower() and any(
                hint in stderr_text.lower()
                for hint in ["bun has crashed", "panic", "switch on corrupt value"]
            )

            # Clear client on any error — but only if THIS run owned it.
            # A non-owning run (stateless fallback, or a failure before
            # lease acquisition) must not destroy a sibling persistent run's
            # subprocess or release its lease on the error path.
            if acquired_lease:
                self._client = None
                self._client_options_key = None
                self._client_in_use = False

            if _is_bun_crash and not getattr(self, "_bun_retry_done", False):
                self._bun_retry_done = True
                logger.warning(
                    "Bun runtime crashed — retrying with fresh client (stderr: %s)",
                    stderr_text[:200],
                )
                yield AgentEvent(
                    type="status",
                    content="Runtime crashed, retrying with a fresh process...",
                )
                await asyncio.sleep(1)
                # Lease state is consistent before the recursive retry, on
                # both branches of the ownership gate above:
                #  - acquired_lease True  → this run owned the persistent
                #    client; the gate already cleared _client and set
                #    _client_in_use=False, so the retry starts on a clean
                #    lease and may take the persistent path itself.
                #  - acquired_lease False → this run never owned the lease
                #    (stateless fallback, or a failure before acquisition);
                #    the gate left _client_in_use untouched, so a sibling
                #    persistent run still holds it. The recursive run() will
                #    correctly see _client_in_use=True and fall back to
                #    stateless again — it cannot steal or double-release the
                #    sibling's lease.
                try:
                    async for retry_event in self.run(
                        message,
                        system_prompt=system_prompt,
                        history=history,
                        session_key=session_key,
                    ):
                        yield retry_event
                finally:
                    self._bun_retry_done = False
                return

            logger.error(f"Claude Agent SDK error: {error_msg}", exc_info=True)

            # Log any stderr captured from the CLI subprocess
            if _stderr_lines:
                logger.error("CLI stderr output:\n%s", "\n".join(_stderr_lines))

            # Provide helpful error messages
            if "CLINotFoundError" in error_msg:
                yield AgentEvent(
                    type="error",
                    content=(
                        "❌ Claude Code CLI not found.\n\n"
                        "**Install Claude Code CLI:**\n"
                        "- Windows: `irm https://claude.ai/install.ps1 | iex`\n"
                        "- macOS/Linux: `curl -fsSL https://claude.ai/install.sh | bash`\n"
                        "- Or: `npm install -g @anthropic-ai/claude-code`\n\n"
                        "Then set your `ANTHROPIC_API_KEY` in **Settings → General**.\n\n"
                        "Or switch to a different backend in **Settings → General** "
                        "(OpenAI Agents, Google ADK, Codex, etc.)."
                    ),
                )
            else:
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
            except Exception as e:
                logger.debug("Failed to interrupt Claude client: %s", e)
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
