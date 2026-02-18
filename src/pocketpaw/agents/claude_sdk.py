"""
Claude Agent SDK wrapper for PocketPaw.

Uses the official Claude Agent SDK (pip install claude-agent-sdk) which provides:
- Built-in tools: Bash, Read, Write, Edit, Glob, Grep, WebSearch, WebFetch
- Streaming responses
- PreToolUse hooks for security
- Permission management
- MCP server support for custom tools

Created: 2026-02-02
Changes:
  - 2026-02-17: Added health/diagnostics tools to system prompt (health_check, error_log, config_doctor).
  - 2026-02-02: Initial implementation with streaming support.
  - 2026-02-02: Added set_executor() for 2-layer architecture wiring.
  - 2026-02-02: Fixed streaming - properly handle all SDK message types.
  - 2026-02-02: REWRITE - Use official claude-agent-sdk properly with all features.
                Now uses real SDK imports (AssistantMessage, TextBlock, etc.)
  - 2026-02-12: Hardened _block_dangerous_hook — wrapped in try/except to prevent
                unhandled exceptions from tearing down the CLI stream. Updated hook
                signature to match SDK 0.1.31 types (PreToolUseHookInput, HookContext).
  - 2026-02-16: Fixed CLI OAuth auth bug — chat() now passes force_provider="anthropic"
                to resolve_llm_client() so the CLI subprocess uses its own OAuth
                credentials instead of falling back to Ollama when no API key is set.
"""

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from pocketpaw.agents.protocol import AgentEvent, ExecutorProtocol
from pocketpaw.config import Settings
from pocketpaw.security.rails import DANGEROUS_SUBSTRINGS as DANGEROUS_PATTERNS
from pocketpaw.tools.policy import ToolPolicy

logger = logging.getLogger(__name__)

# Default identity fallback (used when AgentContextBuilder prompt is not available)
_DEFAULT_IDENTITY = (
    "You are PocketPaw, a helpful AI assistant running locally on the user's computer."
)

# Tool-specific instructions — appended to every system prompt regardless of source
_TOOL_INSTRUCTIONS = """
## Built-in SDK Tools
- Bash: Run shell commands
- Read/Write/Edit: File operations
- Glob/Grep: Search files and content
- WebSearch/WebFetch: Search the web and fetch URLs

## PocketPaw Tools (call via Bash)

You have extra tools installed. Call them with:
```bash
python -m pocketpaw.tools.cli <tool_name> '<json_args>'
```

### Memory
- `remember '{"content": "User name is Alice", "tags": ["personal"]}'` — save to long-term memory
- `forget '{"query": "old preference"}'` — remove outdated memories

**When to use remember:**
- User tells you their name, preferences, or personal details
- User explicitly asks "remember this"
- You learn something important about the user's projects or workflow

**Always remember proactively** — don't wait to be asked.
If someone shares personal info, immediately call remember.

**Reading memories:** Your system prompt already contains a "Memory
Context" section with ALL saved memories pre-loaded. Just read it
directly — never use a tool to look up what you already know.

### Email (Gmail — requires OAuth)
- `gmail_search '{"query": "is:unread", "max_results": 10}'` — search emails
- `gmail_read '{"message_id": "MSG_ID"}'` — read full email
- `gmail_send '{"to": "x@y.com", "subject": "Hi", "body": "..."}'` — send email
- `gmail_list_labels '{}'` — list all labels
- `gmail_create_label '{"name": "MyLabel"}'` — create label (use / for nesting)
- `gmail_modify '{"message_id": "ID", "add_labels": ["LABEL"], "remove_labels": ["INBOX"]}'`
- `gmail_trash '{"message_id": "ID"}'` — trash a message
- `gmail_batch_modify '{"message_ids": ["ID1","ID2"], "add_labels": ["L1"]}'`
  Built-in label IDs: INBOX, SPAM, TRASH, UNREAD, STARRED, IMPORTANT

### Calendar (Google Calendar — requires OAuth)
- `calendar_list '{"max_results": 10}'` — list upcoming events
- `calendar_create '{"summary": "Meeting", "start": "2026-02-08T10:00:00", "end": "2026-02-08T11:00:00"}'`
- `calendar_prep '{"hours_ahead": 24}'` — prep summary for upcoming meetings

### Voice / TTS
- `text_to_speech '{"text": "Hello world", "voice": "alloy"}'` — generate speech audio
  Voices (OpenAI): alloy, echo, fable, onyx, nova, shimmer
- `speech_to_text '{"audio_file": "/path/to/audio.mp3"}'` — transcribe audio to text
  Optional: `"language": "en"` (auto-detected if omitted). Supports mp3/wav/m4a/webm.

### Research
- `research '{"topic": "quantum computing", "depth": "standard"}'` — multi-source research
  Depths: quick (3 sources), standard (5), deep (10)

### Image Generation
- `image_generate '{"prompt": "a sunset over mountains", "aspect_ratio": "16:9"}'`

### Web Content
- `web_search '{"query": "latest news on AI"}'` — web search (Tavily/Brave)
- `url_extract '{"urls": ["https://example.com"]}'` — extract clean text from URLs

### Skills
- `create_skill '{"skill_name": "my-skill", "description": "...", "prompt_template": "..."}'`

### Google Drive (requires OAuth)
- `drive_list '{"query": "name contains \\'report\\'"}'` — list/search files
- `drive_download '{"file_id": "FILE_ID"}'` — download a file
- `drive_upload '{"file_path": "/path/to/file.pdf", "folder_id": "FOLDER_ID"}'` — upload file
- `drive_share '{"file_id": "FILE_ID", "email": "user@example.com", "role": "reader"}'` — share

### Google Docs (requires OAuth)
- `docs_read '{"document_id": "DOC_ID"}'` — read document as plain text
- `docs_create '{"title": "My Doc", "content": "Hello world"}'` — create a new document
- `docs_search '{"query": "meeting notes"}'` — search Google Docs by name

### Spotify (requires OAuth)
- `spotify_search '{"query": "bohemian rhapsody", "type": "track"}'` — search tracks/albums/artists
- `spotify_now_playing '{}'` — what's currently playing
- `spotify_playback '{"action": "play"}'` — play/pause/next/prev/volume (actions: play, pause, next, prev, volume)
- `spotify_playlist '{"action": "list"}'` — list playlists or add track

### OCR
- `ocr '{"image_path": "/path/to/image.png"}'` — extract text from image (uses GPT-4o vision)

### Reddit
- `reddit_search '{"query": "best python frameworks", "subreddit": "python"}'` — search Reddit
- `reddit_read '{"url": "https://reddit.com/r/python/comments/..."}'` — read post + comments
- `reddit_trending '{"subreddit": "all", "limit": 10}'` — trending posts

### Delegation
- `delegate_claude_code '{"task": "refactor the auth module", "timeout": 300}'` — delegate to Claude Code CLI

### Health & Diagnostics
- `health_check '{}'` — run all system health checks (config, API keys, dependencies, storage)
- `health_check '{"include_connectivity": true}'` — include LLM reachability test
- `error_log '{}'` — read recent errors from the persistent error log
- `error_log '{"limit": 5, "search": "deep_work"}'` — search errors by keyword
- `config_doctor '{}'` — full config diagnosis with fix hints
- `config_doctor '{"section": "api_keys"}'` — diagnose a specific section (api_keys, storage)

**When the user reports something isn't working**, use these tools to diagnose:
1. Run `health_check` to see what's broken
2. Check `error_log` for recent errors with tracebacks
3. Use `config_doctor` for step-by-step fix instructions
4. Fix the issue, then run `health_check` again to verify

## Guidelines

1. **Be AGENTIC** — execute tasks using tools, don't just describe how.
2. **Use PocketPaw tools** — always prefer `python -m pocketpaw.tools.cli` over platform-specific commands (AppleScript, PowerShell, etc.). These tools work on all operating systems.
3. **Be concise** — give clear, helpful responses.
4. **Be safe** — don't run destructive commands. Ask for confirmation if unsure.
5. If Gmail/Calendar/Drive/Docs returns "not authenticated", tell the user to visit:
   http://localhost:8888/api/oauth/authorize?service=google_gmail (or google_calendar, google_drive, google_docs)
6. If Spotify returns "not authenticated", tell the user to visit:
   http://localhost:8888/api/oauth/authorize?service=spotify
"""


class ClaudeAgentSDK:
    """Wraps Claude Agent SDK for autonomous task execution.

    This is the RECOMMENDED backend for PocketPaw - it provides:
    - All built-in tools (Bash, Read, Write, Edit, Glob, Grep, WebSearch, WebFetch)
    - Streaming responses for real-time feedback
    - PreToolUse hooks for security (block dangerous commands)
    - Permission management (can bypass for automation)

    Requires: pip install claude-agent-sdk
    """

    # Map SDK tool names to policy tool names for filtering
    _SDK_TO_POLICY: dict[str, str] = {
        "Bash": "shell",
        "Read": "read_file",
        "Write": "write_file",
        "Edit": "edit_file",
        "Glob": "list_dir",
        "Grep": "shell",  # search is shell-adjacent
        "WebSearch": "browser",
        "WebFetch": "browser",
        "Skill": "skill",
    }

    def __init__(self, settings: Settings, executor: ExecutorProtocol | None = None):
        self.settings = settings
        self._executor = executor  # Optional - SDK has built-in execution
        self._stop_flag = False
        self._sdk_available = False
        self._cli_available = False  # Whether the `claude` CLI binary is installed
        self._cwd = Path.home()  # Default working directory
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

    def set_executor(self, executor: ExecutorProtocol) -> None:
        """Inject an optional executor for custom tool execution.

        Note: Claude Agent SDK has built-in execution, so this is optional.
        Can be used for custom tools or fallback execution.
        """
        self._executor = executor
        logger.info("🔗 Optional executor connected to Claude Agent SDK")

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

    async def _get_or_create_client(self, options: Any) -> Any:
        """Get or create a persistent ClaudeSDKClient.

        Reuses the existing subprocess if model and tools haven't changed.
        Reconnects when configuration changes are detected.
        """
        import time

        key = (
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

    async def chat(
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list[dict] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Process a message through Claude Agent SDK with streaming.

        Uses the SDK's built-in tools and streaming capabilities.

        Args:
            message: User message to process.
            system_prompt: Dynamic system prompt from AgentContextBuilder.
                Falls back to _DEFAULT_IDENTITY if not provided.
            history: Recent session history as {"role", "content"} dicts.
                Injected into the system prompt (SDK query() takes a single prompt string).

        Yields:
            AgentEvent objects as the agent responds
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

        try:
            # Resolve LLM provider early — needed for routing + env.
            # Force anthropic: the Claude SDK backend runs the CLI as a subprocess,
            # which has its own OAuth credentials. Without force_provider, auto-
            # resolution with no API key falls back to Ollama (wrong for SDK).
            from pocketpaw.llm.client import resolve_llm_client

            llm = resolve_llm_client(self.settings, force_provider="anthropic")

            # Gemini & plain OpenAI are not compatible with Claude SDK —
            # the SDK speaks Anthropic Messages API, but Gemini/OpenAI
            # endpoints speak the OpenAI Chat Completions API format.
            if llm.is_gemini:
                yield AgentEvent(
                    type="error",
                    content=(
                        "❌ Gemini is not compatible with the **Claude Agent SDK** "
                        "backend.\n\n"
                        "The Claude SDK uses the Anthropic Messages API format, "
                        "but Gemini's endpoint speaks OpenAI format.\n\n"
                        "**Fix:** Switch to **PocketPaw Native** backend in "
                        "**Settings → General → Agent Backend**. "
                        "PocketPaw Native fully supports Gemini."
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
            # Requires an API key — the Claude CLI uses OAuth which we can't
            # reuse here. Fall back to standard path if no key is available.
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

            # Compose final system prompt: identity/memory + tool docs
            # Always include tool instructions — even SIMPLE messages may need tools
            # (e.g., "remind me to call mom" is short but requires scheduler access)
            identity = system_prompt or _DEFAULT_IDENTITY
            final_prompt = identity + "\n" + _TOOL_INSTRUCTIONS

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
                if self._policy.is_tool_allowed(self._SDK_TO_POLICY.get(t, t))
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
                "max_turns": self.settings.claude_sdk_max_turns,
            }

            # Configure LLM provider for the Claude CLI subprocess.
            sdk_env = llm.to_sdk_env()
            if not sdk_env:
                # Fall back to env var if settings has no key
                env_key = os.environ.get("ANTHROPIC_API_KEY")
                if env_key:
                    sdk_env = {"ANTHROPIC_API_KEY": env_key}
            if sdk_env:
                options_kwargs["env"] = sdk_env
            if llm.is_ollama or llm.is_openai_compatible or llm.is_gemini:
                options_kwargs["model"] = llm.model

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
            if not self._client_in_use:
                try:
                    self._client_in_use = True
                    client = await self._get_or_create_client(options)
                    await client.query(message)
                    event_stream = client.receive_response()
                except Exception as client_err:
                    logger.warning(
                        "Persistent client failed, falling back to stateless query: %s",
                        client_err,
                    )
                    # Clear broken client so next call creates a fresh one
                    self._client = None
                    self._client_options_key = None
                    self._client_in_use = False

            if event_stream is None:
                event_stream = self._query(prompt=message, options=options)

            # State tracking for StreamEvent deduplication
            _streamed_via_events = False
            _announced_tools: set[str] = set()

            # Stream responses — release the persistent client guard when done
            try:
                async for event in event_stream:
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
                self._client_in_use = False

            yield AgentEvent(type="done", content="")

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Claude Agent SDK error: {error_msg}")

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


# Backwards-compatible wrapper for router
class ClaudeAgentSDKWrapper(ClaudeAgentSDK):
    """Wrapper to match existing agent interface expected by router.

    Provides the `run()` method that yields dicts instead of AgentEvents.
    """

    async def run(
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list[dict] | None = None,
    ) -> AsyncIterator[dict]:
        """Run the agent, yielding dict chunks for compatibility."""
        async for event in self.chat(message, system_prompt=system_prompt, history=history):
            yield {
                "type": event.type,
                "content": event.content,
                "metadata": getattr(event, "metadata", None),
            }
