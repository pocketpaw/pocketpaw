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
  - 2026-02-02: Initial implementation with streaming support.
  - 2026-02-02: Added set_executor() for 2-layer architecture wiring.
  - 2026-02-02: Fixed streaming - properly handle all SDK message types.
  - 2026-02-02: REWRITE - Use official claude-agent-sdk properly with all features.
                Now uses real SDK imports (AssistantMessage, TextBlock, etc.)
  - 2026-02-12: Hardened _block_dangerous_hook — wrapped in try/except to prevent
                unhandled exceptions from tearing down the CLI stream. Updated hook
                signature to match SDK 0.1.31 types (PreToolUseHookInput, HookContext).
"""

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from pocketclaw.agents.protocol import AgentEvent, ExecutorProtocol
from pocketclaw.config import Settings
from pocketclaw.tools.policy import ToolPolicy

logger = logging.getLogger(__name__)

# Dangerous command patterns to block via PreToolUse hook
DANGEROUS_PATTERNS = [
    "rm -rf /",
    "rm -rf ~",
    "rm -rf *",
    "sudo rm",
    "> /dev/",
    "format ",
    "mkfs",
    "chmod 777 /",
    ":(){ :|:& };:",  # Fork bomb
    "dd if=/dev/zero",
    "dd if=/dev/random",
    "> /etc/passwd",
    "> /etc/shadow",
    "curl | sh",
    "curl | bash",
    "wget | sh",
    "wget | bash",
]

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
python -m pocketclaw.tools.cli <tool_name> '<json_args>'
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

## Guidelines

1. **Be AGENTIC** — execute tasks using tools, don't just describe how.
2. **Use PocketPaw tools** — always prefer `python -m pocketclaw.tools.cli` over platform-specific commands (AppleScript, PowerShell, etc.). These tools work on all operating systems.
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

        # SDK imports (set during initialization)
        self._query = None
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
            from pocketclaw.mcp.config import load_mcp_config
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
            # Compose final system prompt: identity/memory + tool docs
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
                "hooks": hooks,
                "cwd": str(self._cwd),
                "max_turns": 25,  # Safety net against runaway tool loops
            }

            # Pass API key to the Claude CLI subprocess via env.
            # The SDK spawns a subprocess that needs ANTHROPIC_API_KEY in its
            # environment — settings.anthropic_api_key alone is not enough.
            api_key = os.environ.get("ANTHROPIC_API_KEY") or self.settings.anthropic_api_key
            if api_key:
                options_kwargs["env"] = {"ANTHROPIC_API_KEY": api_key}

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

            # Smart model routing (opt-in)
            if self.settings.smart_routing_enabled:
                from pocketclaw.agents.model_router import ModelRouter

                model_router = ModelRouter(self.settings)
                selection = model_router.classify(message)
                options_kwargs["model"] = selection.model
                logger.info(
                    "Smart routing: %s -> %s (%s)",
                    selection.complexity.value,
                    selection.model,
                    selection.reason,
                )

            # Create options (after all kwargs are set, including model)
            options = self._ClaudeAgentOptions(**options_kwargs)

            logger.debug(f"🚀 Starting Claude Agent SDK query: {message[:100]}...")

            # State tracking for StreamEvent deduplication
            _streamed_via_events = False
            _announced_tools: set[str] = set()

            # Stream responses from the SDK
            async for event in self._query(prompt=message, options=options):
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
                        # Detect thinking block stop via the partial field
                        if getattr(event, "_block_type", None) == "thinking":
                            yield AgentEvent(type="thinking_done", content="")
                    continue

                # ========== SystemMessage - metadata, skip ==========
                if self._SystemMessage and isinstance(event, self._SystemMessage):
                    subtype = getattr(event, "subtype", "")
                    logger.debug(f"SystemMessage: {subtype}")
                    continue

                # ========== UserMessage - echo, skip ==========
                if self._UserMessage and isinstance(event, self._UserMessage):
                    logger.debug("UserMessage (echo), skipping")
                    continue

                # ========== AssistantMessage - main content ==========
                if self._AssistantMessage and isinstance(event, self._AssistantMessage):
                    # Skip text if already streamed via StreamEvent deltas
                    if not _streamed_via_events:
                        text = self._extract_text_from_message(event)
                        if text:
                            yield AgentEvent(type="message", content=text)

                    # Emit tool_use events only for tools NOT already announced
                    tools = self._extract_tool_info(event)
                    for tool in tools:
                        if tool["name"] not in _announced_tools:
                            logger.info(f"🔧 Tool: {tool['name']}")
                            yield AgentEvent(
                                type="tool_use",
                                content=f"Using {tool['name']}...",
                                metadata={"name": tool["name"], "input": tool["input"]},
                            )

                    # Reset for next turn in multi-turn loops
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
                        # Result is usually a summary, text was already streamed
                    continue

                # ========== Unknown event type - log it ==========
                event_class = event.__class__.__name__
                logger.debug(f"Unknown event type: {event_class}")

            yield AgentEvent(type="done", content="")

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Claude Agent SDK error: {error_msg}")

            # Provide helpful error messages
            if "CLINotFoundError" in error_msg or "not found" in error_msg.lower():
                yield AgentEvent(
                    type="error",
                    content=(
                        "❌ Claude Code CLI not found.\n\n"
                        "Install with: npm install -g @anthropic-ai/claude-code\n\n"
                        "Or switch to a different backend in "
                        "**Settings → General**."
                    ),
                )
            elif "API key" in error_msg.lower() or "authentication" in error_msg.lower():
                yield AgentEvent(
                    type="error",
                    content=(
                        "❌ Anthropic API key not configured.\n\n"
                        "Open **Settings → API Keys** in the sidebar "
                        "to add your key."
                    ),
                )
            else:
                yield AgentEvent(type="error", content=f"❌ Agent error: {error_msg}")

    async def stop(self) -> None:
        """Stop the agent execution."""
        self._stop_flag = True
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
