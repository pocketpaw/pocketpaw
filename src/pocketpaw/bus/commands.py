"""
Cross-channel command handler.
Created: 2026-02-12

Parses text-based commands from any channel and returns OutboundMessage
responses without invoking the agent backend.
"""

import logging
import re
import uuid
from collections.abc import Callable

from pocketpaw.bus.events import InboundMessage, OutboundMessage
from pocketpaw.memory import get_memory_manager

logger = logging.getLogger(__name__)

_COMMANDS = frozenset(
    {
        "/new",
        "/sessions",
        "/resume",
        "/help",
        "/clear",
        "/rename",
        "/status",
        "/delete",
        "/backend",
        "/backends",
        "/model",
        "/tools",
    }
)

# Maps backend name → Settings field that holds its model override.
_BACKEND_MODEL_FIELDS: dict[str, str] = {
    "claude_agent_sdk": "claude_sdk_model",
    "openai_agents": "openai_agents_model",
    "google_adk": "google_adk_model",
    "codex_cli": "codex_cli_model",
    "opencode": "opencode_model",
    "copilot_sdk": "copilot_sdk_model",
}

# Matches "/cmd" or "!cmd" (with optional @BotName suffix) and trailing args.
# The "!" prefix is a fallback for channels where "/" is intercepted client-side
# (e.g. Matrix/Element treats unknown /commands locally).
_CMD_RE = re.compile(r"^([/!]\w+)(?:@\S+)?\s*(.*)", re.DOTALL)


def _normalize_cmd(raw: str) -> str:
    """Normalize ``!cmd`` → ``/cmd`` so the rest of the handler is prefix-agnostic."""
    if raw.startswith("!"):
        return "/" + raw[1:]
    return raw


class CommandHandler:
    """Unified handler for cross-channel slash commands."""

    def __init__(self):
        # Per-session-key cache of the last shown session list
        # so /resume <n> can reference by number
        self._last_shown: dict[str, list[dict]] = {}
        self._on_settings_changed: Callable[[], None] | None = None

    def set_on_settings_changed(self, callback: Callable[[], None]) -> None:
        """Register a callback invoked after any command mutates settings."""
        self._on_settings_changed = callback

    def _notify_settings_changed(self) -> None:
        """Fire the settings-changed callback (if registered)."""
        if self._on_settings_changed is not None:
            self._on_settings_changed()

    def is_command(self, content: str) -> bool:
        """Check if the message content is a recognised command."""
        m = _CMD_RE.match(content.strip())
        return bool(m and _normalize_cmd(m.group(1).lower()) in _COMMANDS)

    async def handle(self, message: InboundMessage) -> OutboundMessage | None:
        """Process a command and return the response message.

        Returns None if the content isn't a valid command.
        """
        session_key = message.session_key

        m = _CMD_RE.match(message.content.strip())
        if m:
            cmd = _normalize_cmd(m.group(1).lower())
            if cmd in _COMMANDS:
                args = m.group(2).strip()
                return await self._dispatch(cmd, args, message, session_key)

        return None

    async def _dispatch(
        self, cmd: str, args: str, message: InboundMessage, session_key: str
    ) -> OutboundMessage | None:
        """Route a parsed command to the appropriate handler."""
        if cmd == "/new":
            return await self._cmd_new(message, session_key)
        elif cmd == "/sessions":
            return await self._cmd_sessions(message, session_key)
        elif cmd == "/resume":
            return await self._cmd_resume(message, session_key, args)
        elif cmd == "/clear":
            return await self._cmd_clear(message, session_key)
        elif cmd == "/rename":
            return await self._cmd_rename(message, session_key, args)
        elif cmd == "/status":
            return await self._cmd_status(message, session_key)
        elif cmd == "/delete":
            return await self._cmd_delete(message, session_key)
        elif cmd == "/backends":
            return self._cmd_backends(message)
        elif cmd == "/backend":
            return self._cmd_backend(message, args)
        elif cmd == "/model":
            return self._cmd_model(message, args)
        elif cmd == "/tools":
            return self._cmd_tools(message, args)
        elif cmd == "/help":
            return self._cmd_help(message)
        return None

    # ------------------------------------------------------------------
    # /new
    # ------------------------------------------------------------------

    async def _cmd_new(self, message: InboundMessage, session_key: str) -> OutboundMessage:
        """Start a fresh conversation session."""
        memory = get_memory_manager()
        new_key = f"{session_key}:{uuid.uuid4().hex[:8]}"
        await memory.set_session_alias(session_key, new_key)
        return OutboundMessage(
            channel=message.channel,
            chat_id=message.chat_id,
            content=(
                "Started a new conversation. Previous sessions"
                " are preserved — use /sessions to list them."
            ),
        )

    # ------------------------------------------------------------------
    # /sessions
    # ------------------------------------------------------------------

    async def _cmd_sessions(self, message: InboundMessage, session_key: str) -> OutboundMessage:
        """List all sessions for this chat."""
        memory = get_memory_manager()
        sessions = await memory.list_sessions_for_chat(session_key)

        if not sessions:
            return OutboundMessage(
                channel=message.channel,
                chat_id=message.chat_id,
                content="No sessions found. Start chatting to create one!",
            )

        # Store for /resume <n> lookup
        self._last_shown[session_key] = sessions

        lines = ["**Sessions:**\n"]
        for i, s in enumerate(sessions, 1):
            marker = " (active)" if s["is_active"] else ""
            title = s["title"] or "New Chat"
            count = s["message_count"]
            lines.append(f"{i}. {title} ({count} msgs){marker}")

        lines.append("\nUse /resume <number> to switch.")
        return OutboundMessage(
            channel=message.channel,
            chat_id=message.chat_id,
            content="\n".join(lines),
        )

    # ------------------------------------------------------------------
    # /resume
    # ------------------------------------------------------------------

    async def _cmd_resume(
        self, message: InboundMessage, session_key: str, args: str
    ) -> OutboundMessage:
        """Resume a previous session by number or search text."""
        memory = get_memory_manager()

        # No args → show sessions list (same as /sessions)
        if not args:
            return await self._cmd_sessions(message, session_key)

        # Try numeric reference
        if args.isdigit():
            n = int(args)
            shown = self._last_shown.get(session_key)
            if not shown:
                # Fetch sessions first
                shown = await memory.list_sessions_for_chat(session_key)
                self._last_shown[session_key] = shown

            if not shown:
                return OutboundMessage(
                    channel=message.channel,
                    chat_id=message.chat_id,
                    content="No sessions found.",
                )

            if n < 1 or n > len(shown):
                return OutboundMessage(
                    channel=message.channel,
                    chat_id=message.chat_id,
                    content=f"Invalid session number. Choose 1-{len(shown)}.",
                )

            target = shown[n - 1]
            await memory.set_session_alias(session_key, target["session_key"])
            return OutboundMessage(
                channel=message.channel,
                chat_id=message.chat_id,
                content=f"Resumed session: {target['title']}",
            )

        # Text search
        sessions = await memory.list_sessions_for_chat(session_key)
        query_lower = args.lower()
        matches = [
            s
            for s in sessions
            if query_lower in s["title"].lower() or query_lower in s["preview"].lower()
        ]

        if not matches:
            return OutboundMessage(
                channel=message.channel,
                chat_id=message.chat_id,
                content=f'No sessions matching "{args}". Use /sessions to see all.',
            )

        if len(matches) == 1:
            target = matches[0]
            await memory.set_session_alias(session_key, target["session_key"])
            return OutboundMessage(
                channel=message.channel,
                chat_id=message.chat_id,
                content=f"Resumed session: {target['title']}",
            )

        # Multiple matches — show numbered list
        self._last_shown[session_key] = matches
        lines = [f'Multiple sessions match "{args}":\n']
        for i, s in enumerate(matches, 1):
            marker = " (active)" if s["is_active"] else ""
            lines.append(f"{i}. {s['title']} ({s['message_count']} msgs){marker}")
        lines.append("\nUse /resume <number> to switch.")
        return OutboundMessage(
            channel=message.channel,
            chat_id=message.chat_id,
            content="\n".join(lines),
        )

    # ------------------------------------------------------------------
    # /clear
    # ------------------------------------------------------------------

    async def _cmd_clear(self, message: InboundMessage, session_key: str) -> OutboundMessage:
        """Clear the current session's conversation history."""
        memory = get_memory_manager()
        resolved = await memory.resolve_session_key(session_key)
        count = await memory.clear_session(resolved)
        if count:
            return OutboundMessage(
                channel=message.channel,
                chat_id=message.chat_id,
                content=f"Cleared {count} messages from the current session.",
            )
        return OutboundMessage(
            channel=message.channel,
            chat_id=message.chat_id,
            content="Session is already empty.",
        )

    # ------------------------------------------------------------------
    # /rename
    # ------------------------------------------------------------------

    async def _cmd_rename(
        self, message: InboundMessage, session_key: str, args: str
    ) -> OutboundMessage:
        """Rename the current session."""
        if not args:
            return OutboundMessage(
                channel=message.channel,
                chat_id=message.chat_id,
                content="Usage: /rename <new title>",
            )

        memory = get_memory_manager()
        resolved = await memory.resolve_session_key(session_key)
        ok = await memory.update_session_title(resolved, args)
        if ok:
            return OutboundMessage(
                channel=message.channel,
                chat_id=message.chat_id,
                content=f'Session renamed to "{args}".',
            )
        return OutboundMessage(
            channel=message.channel,
            chat_id=message.chat_id,
            content="Could not rename — session not found in index.",
        )

    # ------------------------------------------------------------------
    # /status
    # ------------------------------------------------------------------

    async def _cmd_status(self, message: InboundMessage, session_key: str) -> OutboundMessage:
        """Show current session info."""
        from pocketpaw.config import get_settings

        memory = get_memory_manager()
        settings = get_settings()

        resolved = await memory.resolve_session_key(session_key)
        sessions = await memory.list_sessions_for_chat(session_key)

        # Find active session metadata
        active = None
        for s in sessions:
            if s["is_active"]:
                active = s
                break

        title = active["title"] if active else "Default"
        msg_count = active["message_count"] if active else 0
        is_aliased = resolved != session_key

        lines = [
            "**Session Status:**\n",
            f"Title: {title}",
            f"Messages: {msg_count}",
            f"Channel: {message.channel.value}",
            f"Session key: {resolved}",
            f"Backend: {settings.agent_backend}",
        ]
        if is_aliased:
            lines.append(f"Base key: {session_key}")

        return OutboundMessage(
            channel=message.channel,
            chat_id=message.chat_id,
            content="\n".join(lines),
        )

    # ------------------------------------------------------------------
    # /delete
    # ------------------------------------------------------------------

    async def _cmd_delete(self, message: InboundMessage, session_key: str) -> OutboundMessage:
        """Delete the current session and reset to a fresh state."""
        memory = get_memory_manager()
        resolved = await memory.resolve_session_key(session_key)

        deleted = await memory.delete_session(resolved)
        # Remove alias so next message uses the default session key
        if hasattr(memory._store, "remove_session_alias"):
            await memory._store.remove_session_alias(session_key)

        if deleted:
            return OutboundMessage(
                channel=message.channel,
                chat_id=message.chat_id,
                content=("Session deleted. Your next message will start a fresh conversation."),
            )
        return OutboundMessage(
            channel=message.channel,
            chat_id=message.chat_id,
            content="No session to delete.",
        )

    # ------------------------------------------------------------------
    # /backends
    # ------------------------------------------------------------------

    def _cmd_backends(self, message: InboundMessage) -> OutboundMessage:
        """List all registered backends with install status and capabilities."""
        from pocketpaw.agents.registry import get_backend_class, get_backend_info, list_backends
        from pocketpaw.config import get_settings

        settings = get_settings()
        active = settings.agent_backend
        names = list_backends()

        lines = ["**Available Backends:**\n"]
        for name in names:
            marker = " (active)" if name == active else ""
            info = get_backend_info(name)
            if info is not None:
                try:
                    caps = ", ".join(
                        f.name.lower().replace("_", " ")
                        for f in type(info.capabilities)
                        if f in info.capabilities
                    )
                except TypeError:
                    caps = str(info.capabilities)
                lines.append(f"- **{info.display_name}** (`{name}`){marker} — {caps}")
            else:
                # Backend registered but not installed
                cls = get_backend_class(name)
                status = "not installed" if cls is None else "available"
                lines.append(f"- `{name}`{marker} — {status}")

        lines.append("\nUse /backend <name> to switch.")
        return OutboundMessage(
            channel=message.channel,
            chat_id=message.chat_id,
            content="\n".join(lines),
        )

    # ------------------------------------------------------------------
    # /backend
    # ------------------------------------------------------------------

    def _cmd_backend(self, message: InboundMessage, args: str) -> OutboundMessage:
        """Show or switch the active backend."""
        from pocketpaw.agents.registry import get_backend_class, list_backends
        from pocketpaw.config import get_settings

        settings = get_settings()

        if not args:
            model_field = _BACKEND_MODEL_FIELDS.get(settings.agent_backend, "")
            model = getattr(settings, model_field, "") if model_field else ""
            model_info = f" (model: `{model}`)" if model else " (default model)"
            return OutboundMessage(
                channel=message.channel,
                chat_id=message.chat_id,
                content=f"Current backend: **{settings.agent_backend}**{model_info}",
            )

        name = args.strip().lower()
        available = list_backends()

        if name not in available:
            return OutboundMessage(
                channel=message.channel,
                chat_id=message.chat_id,
                content=(
                    f"Unknown backend `{name}`. Available: {', '.join(f'`{b}`' for b in available)}"
                ),
            )

        if name == settings.agent_backend:
            return OutboundMessage(
                channel=message.channel,
                chat_id=message.chat_id,
                content=f"Already using `{name}`.",
            )

        cls = get_backend_class(name)
        if cls is None:
            return OutboundMessage(
                channel=message.channel,
                chat_id=message.chat_id,
                content=f"Backend `{name}` is not installed. Check dependencies.",
            )

        settings.agent_backend = name
        settings.save()
        get_settings.cache_clear()
        self._notify_settings_changed()

        return OutboundMessage(
            channel=message.channel,
            chat_id=message.chat_id,
            content=f"Switched backend to **{name}**.",
        )

    # ------------------------------------------------------------------
    # /model
    # ------------------------------------------------------------------

    def _cmd_model(self, message: InboundMessage, args: str) -> OutboundMessage:
        """Show or switch the model for the active backend."""
        from pocketpaw.config import get_settings

        settings = get_settings()
        backend = settings.agent_backend
        model_field = _BACKEND_MODEL_FIELDS.get(backend)

        if model_field is None:
            return OutboundMessage(
                channel=message.channel,
                chat_id=message.chat_id,
                content=f"Backend `{backend}` does not support model selection.",
            )

        current = getattr(settings, model_field, "") or ""

        if not args:
            display = f"`{current}`" if current else "default"
            return OutboundMessage(
                channel=message.channel,
                chat_id=message.chat_id,
                content=f"Current model for `{backend}`: {display}",
            )

        new_model = args.strip()
        setattr(settings, model_field, new_model)
        settings.save()
        get_settings.cache_clear()
        self._notify_settings_changed()

        return OutboundMessage(
            channel=message.channel,
            chat_id=message.chat_id,
            content=f"Model for `{backend}` set to **{new_model}**.",
        )

    # ------------------------------------------------------------------
    # /tools
    # ------------------------------------------------------------------

    def _cmd_tools(self, message: InboundMessage, args: str) -> OutboundMessage:
        """Show or switch the tool profile."""
        from pocketpaw.config import get_settings
        from pocketpaw.tools.policy import TOOL_PROFILES

        settings = get_settings()
        profiles = list(TOOL_PROFILES)

        if not args:
            return OutboundMessage(
                channel=message.channel,
                chat_id=message.chat_id,
                content=(
                    f"Current tool profile: **{settings.tool_profile}**\n"
                    f"Available: {', '.join(f'`{p}`' for p in profiles)}"
                ),
            )

        name = args.strip().lower()
        if name not in TOOL_PROFILES:
            return OutboundMessage(
                channel=message.channel,
                chat_id=message.chat_id,
                content=(
                    f"Unknown profile `{name}`. Available: {', '.join(f'`{p}`' for p in profiles)}"
                ),
            )

        if name == settings.tool_profile:
            return OutboundMessage(
                channel=message.channel,
                chat_id=message.chat_id,
                content=f"Already using `{name}` profile.",
            )

        settings.tool_profile = name
        settings.save()
        get_settings.cache_clear()
        self._notify_settings_changed()

        return OutboundMessage(
            channel=message.channel,
            chat_id=message.chat_id,
            content=f"Tool profile switched to **{name}**.",
        )

    # ------------------------------------------------------------------
    # /help
    # ------------------------------------------------------------------

    def _cmd_help(self, message: InboundMessage) -> OutboundMessage:
        """List all available commands."""
        text = (
            "**PocketPaw Commands:**\n\n"
            "/new — Start a fresh conversation\n"
            "/sessions — List your conversation sessions\n"
            "/resume <n> — Resume session #n from the list\n"
            "/resume <text> — Search and resume a session by title\n"
            "/clear — Clear the current session history\n"
            "/rename <title> — Rename the current session\n"
            "/status — Show current session info\n"
            "/delete — Delete the current session\n"
            "/backend — Show or switch agent backend\n"
            "/backends — List all available backends\n"
            "/model — Show or switch model for current backend\n"
            "/tools — Show or switch tool profile\n"
            "/help — Show this help message\n\n"
            "_Tip: Use !command instead of /command on channels"
            " where / is intercepted (e.g. Matrix)._"
        )
        return OutboundMessage(
            channel=message.channel,
            chat_id=message.chat_id,
            content=text,
        )


# Singleton
_handler: CommandHandler | None = None


def get_command_handler() -> CommandHandler:
    """Get the global CommandHandler instance."""
    global _handler
    if _handler is None:
        _handler = CommandHandler()
    return _handler
