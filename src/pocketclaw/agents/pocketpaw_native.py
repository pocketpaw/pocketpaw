"""
PocketPaw Native Orchestrator - The Brain.

Your own orchestrator using raw Anthropic SDK + Open Interpreter as executor.
No LangChain, no Agent SDK - just simple, transparent control.

Created: 2026-02-02
Changes:
  - Initial implementation of PocketPaw Native Orchestrator.
  - 2026-02-02: Added comprehensive security layer (file jail, pattern matching, etc.)
  - 2026-02-02: Added 'computer' tool for full Open Interpreter delegation
                (Calendar, Mail, Browser, AppleScript, Python, etc.)
  - 2026-02-02: Made AGENTIC - system prompt now instructs Claude to QUERY and
                RETURN actual data from apps, not just open them.
                Example: "What's on my calendar?" → returns actual events as text
  - 2026-02-02: SPEED FIX - Shell commands now use direct subprocess (10x faster).
                'computer' tool uses OI for complex multi-step tasks only.
  - 2026-02-05: Added 'remember' and 'recall' tools for long-term memory.
"""

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Optional

from anthropic import AsyncAnthropic
from anthropic import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)

from pocketclaw.agents.errors import format_anthropic_error
from pocketclaw.agents.protocol import AgentEvent
from pocketclaw.config import Settings
from pocketclaw.tools.policy import ToolPolicy

logger = logging.getLogger(__name__)


# =============================================================================
# SECURITY CONFIGURATION
# =============================================================================

# Dangerous command patterns (regex for better matching)
DANGEROUS_PATTERNS = [
    # Destructive file operations
    r"rm\s+(-[rf]+\s+)*[/~]",  # rm -rf /, rm -r -f ~, etc.
    r"rm\s+(-[rf]+\s+)*\*",  # rm -rf *
    r"sudo\s+rm\b",  # Any sudo rm
    r">\s*/dev/",  # Write to devices
    r"mkfs\.",  # Format filesystem
    r"dd\s+if=",  # Disk operations
    r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;",  # Fork bomb
    r"chmod\s+(-R\s+)?777\s+/",  # Dangerous permissions
    # Remote code execution
    r"curl\s+.*\|\s*(ba)?sh",  # curl | sh
    r"wget\s+.*\|\s*(ba)?sh",  # wget | sh
    r"curl\s+.*-o\s*/",  # curl download to root
    r"wget\s+.*-O\s*/",  # wget download to root
    # System damage
    r">\s*/etc/passwd",  # Overwrite passwd
    r">\s*/etc/shadow",  # Overwrite shadow
    r"systemctl\s+(stop|disable)\s+(ssh|sshd|firewall)",  # Disable security
    r"iptables\s+-F",  # Flush firewall
    r"shutdown",  # Shutdown system
    r"reboot",  # Reboot system
    r"init\s+0",  # Halt system
]

# Sensitive paths that should never be read or written
SENSITIVE_PATHS = [
    # SSH keys
    ".ssh/id_rsa",
    ".ssh/id_ed25519",
    ".ssh/id_ecdsa",
    ".ssh/authorized_keys",
    # Credentials
    ".aws/credentials",
    ".aws/config",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".docker/config.json",
    ".kube/config",
    # Secrets
    ".env",
    ".envrc",
    "secrets.json",
    "credentials.json",
    ".git-credentials",
    # System files
    "/etc/passwd",
    "/etc/shadow",
    "/etc/sudoers",
]

# Patterns to redact from output (API keys, passwords, etc.)
REDACT_PATTERNS = [
    r"(sk-[a-zA-Z0-9]{20,})",  # OpenAI/Anthropic keys
    r"(AKIA[A-Z0-9]{16})",  # AWS access key
    r"(ghp_[a-zA-Z0-9]{36})",  # GitHub token
    r"(xox[baprs]-[a-zA-Z0-9-]+)",  # Slack token
    r"password[\"']?\s*[:=]\s*[\"']([^\"']+)",  # password = "..."
    r"api[_-]?key[\"']?\s*[:=]\s*[\"']([^\"']+)",  # api_key = "..."
]

# Default identity fallback (used when AgentContextBuilder prompt is not available)
_DEFAULT_IDENTITY = (
    "You are PocketPaw, a helpful AI assistant that runs locally on the user's computer.\n"
    "You have access to powerful tools that let you control the computer, access apps, "
    "and manage files.\nYou are resourceful, careful, and efficient."
)

# Tool-specific guide — appended to every system prompt regardless of source
_TOOL_GUIDE = """
## CRITICAL: Be AGENTIC - Query and Return Data

**DO NOT just open apps. ALWAYS query actual data and tell the user the information.**

When the user asks about their calendar, emails, reminders, etc.:
1. Use 'computer' to QUERY the actual data (events, emails, tasks)
2. Parse and understand the results
3. Tell the user the ACTUAL information in a clear, useful way

❌ WRONG: "I'll open Calendar.app for you" (not helpful)
✅ RIGHT: "You have 3 events today: 10am Team standup, 2pm Design review, 5pm 1:1 with Sarah"

## Tool Selection Guide

**Use 'computer' (Open Interpreter) for:**
- QUERYING data from apps: Calendar events, emails, reminders, notes, contacts
- Complex tasks that need Python or AppleScript
- Browser automation and web searches
- Multi-step operations
- Anything requiring intelligent problem-solving

**Use 'shell' only for:**
- Simple, well-known commands (ls, git status, npm install, etc.)
- When you know the EXACT command needed

## Examples of AGENTIC Behavior

| User Says | You Should Do |
|-----------|---------------|
| "Show my calendar" | Query Calendar.app via AppleScript/Python and LIST the actual events |
| "What meetings today?" | Query events for today and TELL the user what they are |
| "Any emails from Bob?" | Query Mail.app and SUMMARIZE emails from Bob |
| "What's on my todo list?" | Query Reminders.app and LIST the actual items |
| "Find that PDF I downloaded" | Search for it and TELL the user where it is |
| "What's the weather?" | Search the web and TELL the user the forecast |

## Instructions for computer tool

When using 'computer', be SPECIFIC about what you want:

❌ BAD: "show my calendar"
✅ GOOD: "Use AppleScript or Python to query Calendar.app and return ALL events for today including title, time, and location. Return the data as text, do not open any apps."

❌ BAD: "check my email"
✅ GOOD: "Use AppleScript to query Mail.app for unread emails from the last 24 hours. Return sender, subject, and preview for each email."

## Memory Tools

You have access to long-term memory:
- Use 'remember' to save important facts about the user (preferences, projects, personal info)
- Use 'recall' to search your memories when relevant context is needed

**When to remember:**
- User explicitly asks you to remember something: "Remember that I prefer dark mode"
- Important facts are mentioned: "My name is...", "I work at...", "My project is..."
- User preferences: "I like...", "I prefer...", "Always do X when..."

**When to recall:**
- User asks about something you might know: "What's my name?", "What project am I working on?"
- Before starting a task, recall relevant context about the user's preferences

## Guidelines
- Be concise and helpful
- ALWAYS return actual data/information, not just open apps
- Prefer 'computer' over 'shell' when in doubt
- Report results clearly in a human-readable format
- Use 'remember' to learn about the user over time"""

# Tool definitions for Claude
TOOLS = [
    {
        "name": "computer",
        "description": """Execute a task using Open Interpreter - an AI agent with FULL computer control.

IMPORTANT: Use this to QUERY and RETURN data, not just open apps!

When querying apps (Calendar, Mail, Reminders), be SPECIFIC:
- "Query Calendar.app for today's events and return title, time, location as text"
- "Query Mail.app for unread emails and return sender, subject, preview"
- "Query Reminders.app and return all incomplete tasks"

DO NOT just say "show calendar" - that opens the app without returning data.

CAPABILITIES:
- Query macOS apps via AppleScript/Python (Calendar, Mail, Reminders, Contacts, Notes)
- Run Python code for data processing
- Browser automation and web searches
- Complex multi-step operations

ALWAYS instruct it to RETURN data as text, not open GUI apps.""",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "SPECIFIC task description. Include: what data to query, which app, and 'return as text' or 'do not open app'",
                }
            },
            "required": ["task"],
        },
    },
    {
        "name": "shell",
        "description": "Execute a simple shell command. Only use for basic operations where you know the exact command. For complex tasks, use 'computer' instead.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to execute"}
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read the contents of a file",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path to the file to read"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file (creates or overwrites)",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to write"},
                "content": {"type": "string", "description": "Content to write to the file"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_dir",
        "description": "List contents of a directory",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the directory to list"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "remember",
        "description": "Save important information to long-term memory. Use this to remember facts about the user, their preferences, project details, or anything they want you to remember for future conversations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The information to remember (be specific and clear)",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags to categorize the memory (e.g., 'preference', 'project', 'personal')",
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "recall",
        "description": "Search long-term memories for previously saved information about the user, their preferences, or project details.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for in memories"},
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of memories to return (default: 5)",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "forget",
        "description": (
            "Remove information from long-term memory. "
            "Searches for matching memories and deletes them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to search for and forget",
                },
            },
            "required": ["query"],
        },
    },
]


class PocketPawOrchestrator:
    """PocketPaw Native Orchestrator - Your own AI brain.

    Architecture:
        User Message → PocketPaw (Brain) → Tool Calls → Open Interpreter (Hands) → Result

    Security layers:
    1. Dangerous command regex matching
    2. Sensitive path protection (no reading SSH keys, credentials, etc.)
    3. File jail (restricts to home directory by default)
    4. Output redaction (hides API keys, passwords in output)

    This is a simple, transparent orchestrator:
    - Uses Anthropic SDK directly for reasoning
    - Routes tool calls to Open Interpreter executor
    - You control the loop, prompts, and security
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: Optional[AsyncAnthropic] = None
        self._executor = None
        self._stop_flag = False
        self._file_jail = settings.file_jail_path.resolve()
        self._policy = ToolPolicy(
            profile=settings.tool_profile,
            allow=settings.tools_allow,
            deny=settings.tools_deny,
        )
        self._initialize()

    def _initialize(self) -> None:
        """Initialize the orchestrator."""
        # Initialize Anthropic client
        if not self.settings.anthropic_api_key:
            logger.error("❌ Anthropic API key required for PocketPaw Native")
            return

        # Initialize client with timeout to prevent hanging
        try:
            self._client = AsyncAnthropic(
                api_key=self.settings.anthropic_api_key,
                timeout=60.0,  # 60 second timeout for API requests
                max_retries=2,  # Limit retries to prevent long hangs
            )
        except Exception as e:
            logger.error(f"Failed to initialize Anthropic client: {e}")
            self._client = None
            return

        # Initialize executor (Open Interpreter)
        try:
            from pocketclaw.agents.executor import OpenInterpreterExecutor

            self._executor = OpenInterpreterExecutor(self.settings)
        except Exception as e:
            logger.warning(f"⚠️ Executor init failed: {e}. Using fallback.")
            self._executor = None

        logger.info("=" * 50)
        logger.info("🐾 POCKETPAW NATIVE ORCHESTRATOR")
        logger.info("   └─ Brain: Anthropic API (direct)")
        logger.info("   └─ Hands: Open Interpreter")
        logger.info("   └─ Model: %s", self.settings.anthropic_model)
        logger.info("   └─ File Jail: %s", self._file_jail)
        logger.info("   └─ Tool Profile: %s", self.settings.tool_profile)
        logger.info("   └─ Security: Enabled (patterns, paths, redaction)")
        logger.info("=" * 50)

    def _get_filtered_tools(self) -> list[dict]:
        """Return TOOLS filtered by the active tool policy, plus MCP tools."""
        base = [t for t in TOOLS if self._policy.is_tool_allowed(t["name"])]
        base.extend(self._get_mcp_tools())
        return base

    def _get_mcp_tools(self) -> list[dict]:
        """Convert MCP tools to Anthropic tool format, filtered by policy."""
        try:
            from pocketclaw.mcp.manager import get_mcp_manager
        except ImportError:
            return []

        mgr = get_mcp_manager()
        result = []
        for tool_info in mgr.get_all_tools():
            if not self._policy.is_mcp_tool_allowed(tool_info.server_name, tool_info.name):
                continue
            # Build unique name: mcp_<server>__<tool>
            tool_name = f"mcp_{tool_info.server_name}__{tool_info.name}"
            result.append(
                {
                    "name": tool_name,
                    "description": (f"[MCP:{tool_info.server_name}] {tool_info.description}"),
                    "input_schema": tool_info.input_schema or {"type": "object", "properties": {}},
                }
            )
        return result

    def _parse_mcp_tool_name(self, tool_name: str) -> tuple[str, str] | None:
        """Parse an MCP tool name back to (server_name, original_tool_name).

        Returns None if not an MCP tool name.
        """
        if not tool_name.startswith("mcp_"):
            return None
        rest = tool_name[4:]  # strip "mcp_"
        parts = rest.split("__", 1)
        if len(parts) != 2:
            return None
        return parts[0], parts[1]

    # =========================================================================
    # SECURITY METHODS
    # =========================================================================

    def _is_dangerous_command(self, command: str) -> Optional[str]:
        """Check if a command matches dangerous patterns using regex."""
        for pattern in DANGEROUS_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return pattern
        return None

    def _is_sensitive_path(self, path: str) -> bool:
        """Check if a path is sensitive and should be protected."""
        # Normalize path
        try:
            normalized = Path(path).expanduser().resolve()
            path_str = str(normalized)
        except Exception:
            path_str = path

        # Check against sensitive paths
        for sensitive in SENSITIVE_PATHS:
            if sensitive in path_str or path_str.endswith(sensitive):
                return True

        return False

    def _is_path_in_jail(self, path: str) -> bool:
        """Check if a path is within the allowed file jail."""
        try:
            # Resolve the path (handles .., symlinks, etc.)
            resolved = Path(path).expanduser().resolve()

            # Check if it's within the jail
            resolved.relative_to(self._file_jail)
            return True
        except ValueError:
            # relative_to raises ValueError if not a subpath
            return False
        except Exception:
            return False

    def _redact_secrets(self, text: str) -> str:
        """Redact sensitive information from output."""
        redacted = text
        for pattern in REDACT_PATTERNS:
            redacted = re.sub(pattern, r"[REDACTED]", redacted, flags=re.IGNORECASE)
        return redacted

    def _validate_file_access(self, path: str, operation: str) -> tuple[bool, str]:
        """Validate file access for read/write operations.

        Returns:
            (allowed: bool, reason: str)
        """
        # Check sensitive paths
        if self._is_sensitive_path(path):
            return False, f"🛑 BLOCKED: '{path}' is a sensitive file (credentials, keys, etc.)"

        # Check file jail
        if not self._is_path_in_jail(path):
            return False, f"🛑 BLOCKED: '{path}' is outside allowed directory ({self._file_jail})"

        return True, ""

    def _validate_command(self, command: str) -> tuple[bool, str]:
        """Validate a shell command for execution.

        Returns:
            (allowed: bool, reason: str)
        """
        # Check dangerous patterns
        danger = self._is_dangerous_command(command)
        if danger:
            return False, f"🛑 BLOCKED: Command matches dangerous pattern: {danger}"

        return True, ""

    async def _execute_tool(self, tool_name: str, tool_input: dict) -> str:
        """Execute a tool and return the result.

        All tool execution goes through security validation:
        1. Command validation (dangerous patterns)
        2. Path validation (sensitive files, jail check)
        3. Output redaction (secrets removal)
        """
        logger.info(f"🔧 Tool: {tool_name}({tool_input})")

        try:
            # =================================================================
            # COMPUTER TOOL - Full Open Interpreter power (for complex tasks)
            # =================================================================
            if tool_name == "computer":
                task = tool_input.get("task", "")

                if not task:
                    return "Error: No task provided"

                logger.info(f"🖥️ Delegating to Open Interpreter: {task[:100]}...")

                # Use executor's run_complex_task method (cleaner interface)
                if self._executor and hasattr(self._executor, "run_complex_task"):
                    result = await self._executor.run_complex_task(task)
                    return self._redact_secrets(result or "(no output)")
                else:
                    return "Error: Open Interpreter not available. Install with: pip install open-interpreter"

            # =================================================================
            # SHELL TOOL - Simple command execution
            # =================================================================
            elif tool_name == "shell":
                command = tool_input.get("command", "")

                # Security: validate command
                allowed, reason = self._validate_command(command)
                if not allowed:
                    logger.warning(f"Security block: {reason}")
                    return reason

                # Execute via Open Interpreter or fallback
                if self._executor:
                    result = await self._executor.run_shell(command)
                else:
                    # Fallback: direct execution
                    import subprocess

                    try:
                        proc = subprocess.run(
                            command,
                            shell=True,
                            capture_output=True,
                            text=True,
                            timeout=60,
                            cwd=str(self._file_jail),  # Run in jail directory
                        )
                        result = proc.stdout + proc.stderr
                    except subprocess.TimeoutExpired:
                        result = "Command timed out after 60 seconds"
                    except Exception as e:
                        result = f"Error: {e}"

                # Security: redact secrets from output
                return self._redact_secrets(result or "(no output)")

            elif tool_name == "read_file":
                path = tool_input.get("path", "")

                # Security: validate path
                allowed, reason = self._validate_file_access(path, "read")
                if not allowed:
                    logger.warning(f"Security block: {reason}")
                    return reason

                if self._executor:
                    content = await self._executor.read_file(path)
                else:
                    with open(Path(path).expanduser(), "r") as f:
                        content = f.read()

                # Security: redact secrets from file content
                return self._redact_secrets(content)

            elif tool_name == "write_file":
                path = tool_input.get("path", "")
                content = tool_input.get("content", "")

                # Security: validate path
                allowed, reason = self._validate_file_access(path, "write")
                if not allowed:
                    logger.warning(f"Security block: {reason}")
                    return reason

                if self._executor:
                    await self._executor.write_file(path, content)
                else:
                    with open(Path(path).expanduser(), "w") as f:
                        f.write(content)

                return f"✓ Written to {path}"

            elif tool_name == "list_dir":
                path = tool_input.get("path", ".")

                # Security: validate path
                allowed, reason = self._validate_file_access(path, "list")
                if not allowed:
                    logger.warning(f"Security block: {reason}")
                    return reason

                if self._executor:
                    items = await self._executor.list_directory(path)
                    return "\n".join(items)
                else:
                    import os

                    return "\n".join(os.listdir(Path(path).expanduser()))

            elif tool_name == "remember":
                from pocketclaw.memory.manager import get_memory_manager

                content = tool_input.get("content", "")
                tags = tool_input.get("tags", [])

                if not content:
                    return "Error: No content provided to remember"

                manager = get_memory_manager()
                await manager.remember(content, tags=tags)

                tags_str = f" with tags: {', '.join(tags)}" if tags else ""
                return (
                    f"✅ Remembered{tags_str}: {content[:100]}{'...' if len(content) > 100 else ''}"
                )

            elif tool_name == "recall":
                from pocketclaw.memory.manager import get_memory_manager

                query = tool_input.get("query", "")
                limit = tool_input.get("limit", 5)

                if not query:
                    return "Error: No query provided for recall"

                manager = get_memory_manager()
                results = await manager.search(query, limit=limit)

                if not results:
                    return f"No memories found matching: {query}"

                lines = [f"Found {len(results)} memories:\n"]
                for i, entry in enumerate(results, 1):
                    tags_str = f" [{', '.join(entry.tags)}]" if entry.tags else ""
                    lines.append(f"{i}. {entry.content[:200]}{tags_str}")

                return "\n".join(lines)

            elif tool_name == "forget":
                from pocketclaw.memory.manager import get_memory_manager

                query = tool_input.get("query", "")
                if not query:
                    return "Error: No query provided for forget"

                manager = get_memory_manager()
                results = await manager.search(query, limit=5)
                if not results:
                    return f"No memories found matching: {query}"

                deleted = 0
                for entry in results:
                    ok = await manager._store.delete(entry.id)
                    if ok:
                        deleted += 1
                return f"Forgot {deleted} memory(ies) matching: {query}"

            else:
                # Check if it's an MCP tool
                mcp_parsed = self._parse_mcp_tool_name(tool_name)
                if mcp_parsed:
                    server_name, original_tool = mcp_parsed
                    from pocketclaw.mcp.manager import get_mcp_manager

                    mgr = get_mcp_manager()
                    result = await mgr.call_tool(server_name, original_tool, tool_input)
                    return self._redact_secrets(result)
                return f"Unknown tool: {tool_name}"

        except Exception as e:
            logger.error(f"Tool execution error: {e}")
            # Security: redact secrets from error messages too
            return self._redact_secrets(f"Error executing {tool_name}: {e}")

    async def chat(
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list[dict] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Process a message through the orchestrator.

        This is the main agentic loop:
        1. Send message to Claude with tools
        2. If Claude responds with text → yield it
        3. If Claude wants to use a tool → execute it → feed result back
        4. Repeat until done

        Args:
            message: User message to process.
            system_prompt: Dynamic system prompt from AgentContextBuilder.
            history: Recent session history as {"role", "content"} dicts.
                Prepended to the messages list for multi-turn context.
        """
        if not self._client:
            yield AgentEvent(
                type="error", content="❌ PocketPaw not initialized. Check Anthropic API key."
            )
            return

        self._stop_flag = False

        # Conversation history for this request
        messages: list[dict] = []

        # Prepend session history for multi-turn context (Anthropic API supports this natively)
        if history:
            for msg in history:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if content:
                    messages.append({"role": role, "content": content})

        messages.append({"role": "user", "content": message})

        # Maximum iterations to prevent infinite loops
        max_iterations = 10
        iteration = 0

        try:
            while iteration < max_iterations and not self._stop_flag:
                iteration += 1
                logger.debug(f"Iteration {iteration}/{max_iterations}")

                # Smart model routing (opt-in)
                model = self.settings.anthropic_model
                if self.settings.smart_routing_enabled:
                    from pocketclaw.agents.model_router import ModelRouter

                    model_router = ModelRouter(self.settings)
                    selection = model_router.classify(message)
                    model = selection.model
                    logger.info(
                        "Smart routing: %s -> %s (%s)",
                        selection.complexity.value,
                        selection.model,
                        selection.reason,
                    )

                # Compose final system prompt: identity/memory + tool guide
                identity = system_prompt or _DEFAULT_IDENTITY
                final_system = identity + "\n" + _TOOL_GUIDE

                # Call Claude with timeout wrapper for safety
                try:
                    response = await asyncio.wait_for(
                        self._client.messages.create(
                            model=model,
                            max_tokens=4096,
                            system=final_system,
                            tools=self._get_filtered_tools(),
                            messages=messages,
                        ),
                        timeout=90.0,  # Additional asyncio timeout as safety net
                    )
                except asyncio.TimeoutError:
                    yield AgentEvent(
                        type="error",
                        content=(
                            "⏱️ **Request Timed Out**\n\n"
                            "The request took too long to complete.\n\n"
                            "**Try:**\n"
                            "- Check your internet connection\n"
                            "- Try a smaller model in Settings → General\n"
                            "- Try again"
                        ),
                    )
                    return
                except AuthenticationError as e:
                    yield AgentEvent(type="error", content=format_anthropic_error(e))
                    return
                except RateLimitError as e:
                    yield AgentEvent(type="error", content=format_anthropic_error(e))
                    return
                except (APIConnectionError, APITimeoutError) as e:
                    yield AgentEvent(type="error", content=format_anthropic_error(e))
                    return
                except (BadRequestError, PermissionDeniedError, NotFoundError) as e:
                    yield AgentEvent(type="error", content=format_anthropic_error(e))
                    return
                except Exception as api_error:
                    logger.error(f"Anthropic API error: {api_error}")
                    yield AgentEvent(type="error", content=format_anthropic_error(api_error))
                    return
                    return
                except RateLimitError as e:
                    yield AgentEvent(type="error", content=format_anthropic_error(e))
                    return
                except (APIConnectionError, APITimeoutError) as e:
                    yield AgentEvent(type="error", content=format_anthropic_error(e))
                    return
                except (BadRequestError, PermissionDeniedError, NotFoundError) as e:
                    yield AgentEvent(type="error", content=format_anthropic_error(e))
                    return
                except Exception as api_error:
                    logger.error(f"Anthropic API error: {api_error}")
                    yield AgentEvent(type="error", content=format_anthropic_error(api_error))
                    return

                # Process response content blocks
                assistant_content = []
                tool_results_needed = []

                for block in response.content:
                    if block.type == "text":
                        # Text response - yield to user
                        if block.text:
                            yield AgentEvent(type="message", content=block.text)
                        assistant_content.append(block)

                    elif block.type == "tool_use":
                        # Tool call - execute it
                        tool_name = block.name
                        tool_input = block.input
                        tool_id = block.id

                        # Emit tool_use event
                        yield AgentEvent(
                            type="tool_use",
                            content=f"🔧 Using {tool_name}...",
                            metadata={"name": tool_name, "input": tool_input},
                        )

                        # Execute
                        result = await self._execute_tool(tool_name, tool_input)

                        # Emit tool_result event
                        yield AgentEvent(
                            type="tool_result",
                            content=result[:500] + ("..." if len(result) > 500 else ""),
                            metadata={"name": tool_name},
                        )

                        assistant_content.append(block)
                        tool_results_needed.append(
                            {"type": "tool_result", "tool_use_id": tool_id, "content": result}
                        )

                # Add assistant message to history
                messages.append({"role": "assistant", "content": assistant_content})

                # Check if we're done
                if response.stop_reason == "end_turn":
                    break

                # If tools were used, add results and continue
                if tool_results_needed:
                    messages.append({"role": "user", "content": tool_results_needed})
                else:
                    # No tools and not end_turn - shouldn't happen, but break anyway
                    break

            yield AgentEvent(type="done", content="")

        except Exception as e:
            logger.error(f"PocketPaw error: {e}")
            yield AgentEvent(type="error", content=format_anthropic_error(e))

    async def run(
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list[dict] | None = None,
    ) -> AsyncIterator[dict]:
        """Run method for compatibility with router."""
        async for event in self.chat(message, system_prompt=system_prompt, history=history):
            yield {"type": event.type, "content": event.content}

    async def stop(self) -> None:
        """Stop the orchestrator."""
        self._stop_flag = True
        logger.info("🛑 PocketPaw stopped")

    async def get_status(self) -> dict:
        """Get orchestrator status."""
        return {
            "backend": "pocketpaw_native",
            "available": self._client is not None,
            "executor": "open_interpreter" if self._executor else "fallback",
            "model": self.settings.anthropic_model,
        }
