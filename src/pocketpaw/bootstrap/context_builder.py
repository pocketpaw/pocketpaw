"""
Builder for assembling the full agent context.
Created: 2026-02-02
Updated: 2026-04-08 - kb injection: query kb-go for structured knowledge
alongside soul memories
Updated: 2026-04-01 - Context window budget tracking: priority-based injection with per-block caps
Updated: 2026-03-10 - AGENTS.md injection: read project-specific constraints from target repos
Updated: 2026-03-09 - Sanitize file_context paths before injecting into system prompt
Updated: 2026-02-17 - Inject health state into system prompt when degraded/unhealthy
Updated: 2026-02-07 - Semantic context injection for mem0 backend
Updated: 2026-02-10 - Channel-aware format hints
"""

from __future__ import annotations

import asyncio
import enum
import logging

from pocketpaw.bootstrap.default_provider import DefaultBootstrapProvider
from pocketpaw.bootstrap.protocol import BootstrapProviderProtocol
from pocketpaw.bus.events import Channel
from pocketpaw.bus.format import CHANNEL_FORMAT_HINTS
from pocketpaw.memory.manager import MemoryManager, get_memory_manager

logger = logging.getLogger(__name__)


class _Priority(enum.IntEnum):
    """Injection block priority — lower value = higher priority."""

    CRITICAL = 0  # Always include, truncate only as last resort
    HIGH = 1  # Include if budget allows, truncate to cap
    MEDIUM = 2  # Include if budget allows, skip if tight
    LOW = 3  # First to drop when budget is exceeded


# Default character caps per injection block (None = no cap, use remaining budget)
_INJECTION_CAPS: dict[str, int | None] = {
    "identity": None,  # Critical — never capped
    "instructions": None,  # Critical — never capped
    "memory_context": 4000,
    "kb_context": 3000,
    "sender_block": 500,
    "channel_hints": 500,
    "channel_instructions": 1000,
    "session_key": 200,
    "file_context": 2000,
    "health_state": 300,
    "skills_list": 2000,
    "agents_md": 3000,
    "gws_instructions": 1000,
}

_DEFAULT_BUDGET_CHARS = 32_000


class AgentContextBuilder:
    """
    Assembles the final system prompt by combining:
    1. Static Identity (Bootstrap)
    2. Dynamic Memory (MemoryManager)
    3. Current State (e.g., date/time, active tasks)

    Uses a priority-based budget system to prevent unbounded prompt growth.
    Each injection block has a priority (_Priority) and optional per-block cap
    (_INJECTION_CAPS). When the total exceeds budget_chars, lower-priority
    blocks are dropped first while CRITICAL blocks are truncated as a last resort.
    """

    def __init__(
        self,
        bootstrap_provider: BootstrapProviderProtocol | None = None,
        memory_manager: MemoryManager | None = None,
    ):
        self.bootstrap = bootstrap_provider or DefaultBootstrapProvider()
        self.memory = memory_manager or get_memory_manager()

    async def build_system_prompt(
        self,
        include_memory: bool = True,
        user_query: str | None = None,
        channel: Channel | None = None,
        sender_id: str | None = None,
        session_key: str | None = None,
        file_context: dict | None = None,
        agents_md_dir: str | None = None,
        metadata: dict | None = None,
        budget_chars: int = _DEFAULT_BUDGET_CHARS,
    ) -> str:
        """Build the complete system prompt.

        Args:
            include_memory: Whether to include memory context.
            user_query: Current user message for semantic memory search (mem0).
            channel: Target channel for format-aware hints.
            sender_id: Sender identifier for memory scoping and identity injection.
            session_key: Current session key for session management tools.
            file_context: Optional file/directory context from the desktop client.
            agents_md_dir: Directory to search for AGENTS.md (walks up to repo root).
            metadata: Channel-specific metadata (e.g. discord username, guild_id).
            budget_chars: Maximum character budget for the assembled prompt.
        """
        blocks: list[tuple[str, _Priority, str]] = []

        # 1. Load static identity, memory context, and kb context concurrently
        # (independent I/O — identity is a function call, memory hits disk/vector db,
        # kb shells out to a subprocess). asyncio.gather keeps the critical path fast.
        if include_memory:
            if user_query:
                memory_coro = self.memory.get_semantic_context(user_query, sender_id=sender_id)
            else:
                memory_coro = self.memory.get_context_for_agent(sender_id=sender_id)
            context, memory_context, kb_context = await asyncio.gather(
                self.bootstrap.get_context(),
                memory_coro,
                self._get_kb_context(user_query),
            )
        else:
            context, kb_context = await asyncio.gather(
                self.bootstrap.get_context(),
                self._get_kb_context(user_query),
            )
            memory_context = ""

        base_prompt = context.to_system_prompt()
        blocks.append(("identity", _Priority.CRITICAL, base_prompt))

        # 2. Inject memory context (scoped to sender)
        # When soul is active, soul's bootstrap provider already handles persistent
        # memory (identity, personality, knowledge domains). Skip regular long-term
        # memory injection to avoid duplication — the agent should use soul_recall
        # for fact retrieval instead. Session history is still managed by regular memory.
        from pocketpaw.paw.soul_bridge import SoulBootstrapProvider

        soul_active = isinstance(self.bootstrap, SoulBootstrapProvider)
        if include_memory and memory_context and not soul_active:
            mem_block = (
                "\n# Memory Context (already loaded — use this directly, "
                "do NOT call recall unless you need something not listed here)\n" + memory_context
            )
            blocks.append(("memory_context", _Priority.HIGH, mem_block))

        # 2b. Inject kb (knowledge base) context — structured articles from source files
        # This runs alongside soul memory: soul handles "what we discussed", kb handles
        # "what the code currently says". The two complement each other, so we inject
        # both when available. See https://github.com/qbtrix/kb-go for the kb tool.
        if kb_context:
            kb_block = (
                "\n# Knowledge Base (relevant articles from the project wiki)\n"
                "These are compiled from source files. Use them for implementation "
                "details and current-state facts. Use soul_recall for past decisions "
                "and conversation history.\n\n" + kb_context
            )
            blocks.append(("kb_context", _Priority.HIGH, kb_block))

        # 3. Inject sender identity block
        if sender_id:
            from pocketpaw.config import get_settings

            settings = get_settings()
            if settings.owner_id:
                is_owner = sender_id == settings.owner_id
                role = "owner" if is_owner else "external user"
                identity_block = (
                    f"\n# Current Conversation\n"
                    f"You are speaking with sender_id={sender_id} (role: {role})."
                )
                if is_owner:
                    identity_block += "\nThis is your owner."
                else:
                    identity_block += (
                        "\nThis is NOT your owner. Be helpful but do not share "
                        "owner-private information."
                    )
                blocks.append(("sender_block", _Priority.HIGH, identity_block))

        # 4. Inject channel format hint
        if channel:
            hint = CHANNEL_FORMAT_HINTS.get(channel, "")
            if hint:
                blocks.append(("channel_hints", _Priority.LOW, f"\n# Response Format\n{hint}"))

        # 4b. Inject channel-specific instructions (e.g. discord.md)
        if channel:
            channel_instructions = self._load_channel_instructions(channel)
            if channel_instructions:
                # Inject dynamic context (username, guild_id) from metadata
                meta = metadata or {}
                username = meta.get("username", "")
                guild_id = meta.get("guild_id", "")
                ctx_lines = []
                if sender_id:
                    ctx_lines.append(f"sender_id: {sender_id}")
                if username:
                    ctx_lines.append(f"discord_username: {username}")
                if guild_id:
                    ctx_lines.append(f"discord_guild_id: {guild_id}")
                if ctx_lines:
                    channel_instructions += "\n\n## Current Context\n" + "\n".join(ctx_lines)
                blocks.append(("channel_instructions", _Priority.MEDIUM, channel_instructions))

        # 4c. Inject pocket creation context (from pocket chat endpoint)
        if metadata and metadata.get("pocket_system_context"):
            blocks.append(("pocket_context", _Priority.HIGH, metadata["pocket_system_context"]))

        # 4d. Inject current pocket info so the AI knows what pocket is open
        if metadata and metadata.get("pocket_context"):
            import json

            pc = metadata["pocket_context"]
            pocket_tag = (
                f"\n<current-pocket>\n"
                f"id: {pc.get('id', 'unknown')}\n"
                f"name: {pc.get('name', 'Untitled')}\n"
                f"widgets: {json.dumps(pc.get('widgets', []))}\n"
                f"</current-pocket>\n"
            )
            blocks.append(("current_pocket", _Priority.HIGH, pocket_tag))

        # 5. Inject session key for session management tools
        if session_key:
            session_block = (
                f"\n# Session Management\n"
                f"Current session_key: {session_key}\n"
                f"Pass this value to any session tool (new_session, list_sessions, "
                f"switch_session, clear_session, rename_session, delete_session)."
            )
            blocks.append(("session_key", _Priority.MEDIUM, session_block))

        # 6. Inject file context from desktop client
        if file_context:
            import re

            def _sanitize_path(p: str) -> str:
                """Strip non-path characters to prevent prompt injection."""
                return re.sub(r"[^\w\s\-./\\:~]", "", p).strip()

            fc_parts = []
            if file_context.get("current_dir"):
                fc_parts.append(f"Working directory: {_sanitize_path(file_context['current_dir'])}")
            if file_context.get("open_file"):
                fc_parts.append(f"Open file: {_sanitize_path(file_context['open_file'])}")
            if file_context.get("selected_files"):
                safe_files = [_sanitize_path(f) for f in file_context["selected_files"]]
                fc_parts.append(f"Selected files: {', '.join(safe_files)}")
            if fc_parts:
                blocks.append(
                    (
                        "file_context",
                        _Priority.MEDIUM,
                        "\n# File Context\n" + "\n".join(fc_parts),
                    )
                )

        # 7. Inject health state (only when degraded/unhealthy — saves context window)
        try:
            from pocketpaw.health import get_health_engine

            health_block = get_health_engine().get_health_prompt_section()
            if health_block:
                blocks.append(("health_state", _Priority.LOW, health_block))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Health engine failure (non-fatal, skipping health block): %s", exc)

        # 8. Inject available skills so the agent knows what exists
        try:
            from pocketpaw.skills import get_skill_loader

            loader = get_skill_loader()
            skills = loader.get_all()
            if skills:
                skill_lines = []
                for s in skills.values():
                    invocable = " (user-invocable)" if s.user_invocable else ""
                    skill_lines.append(f"- **{s.name}**: {s.description}{invocable}")
                search_dirs = ", ".join(str(p) for p in loader.paths)
                skills_block = (
                    "\n# Available Skills\n"
                    "The following skills have been created and are available. "
                    "Do NOT recreate them or forget they exist.\n"
                    + "\n".join(skill_lines)
                    + f"\n\nSkills directories: {search_dirs}"
                )
                blocks.append(("skills_list", _Priority.MEDIUM, skills_block))
        except Exception as exc:
            logger.debug("Skill injection skipped: %s", exc)

        # 9. Inject AGENTS.md constraints from the target repo
        if agents_md_dir:
            try:
                from pocketpaw.agents_md import AgentsMdLoader

                agents_md = AgentsMdLoader().find_and_load(agents_md_dir)
                if agents_md:
                    blocks.append(("agents_md", _Priority.MEDIUM, agents_md.constraints_block))
            except Exception:
                pass  # AGENTS.md failure never breaks prompt building

        # 10. Inject GWS CLI guidance when google-workspace MCP server is active
        try:
            gws_block = self._load_gws_instructions()
            if gws_block:
                blocks.append(("gws_instructions", _Priority.MEDIUM, gws_block))
        except Exception:
            pass  # GWS injection failure never breaks prompt building

        return self._assemble_with_budget(blocks, budget_chars=budget_chars)

    @staticmethod
    def _assemble_with_budget(
        blocks: list[tuple[str, _Priority, str]],
        budget_chars: int = _DEFAULT_BUDGET_CHARS,
    ) -> str:
        """Assemble system prompt blocks respecting a character budget.

        Blocks are processed in priority order (CRITICAL first).
        Each block is capped by _INJECTION_CAPS if defined.
        Lower-priority blocks are skipped when budget is exceeded.
        """
        # Sort by priority (CRITICAL=0 first), preserving insertion order for ties
        sorted_blocks = sorted(blocks, key=lambda b: b[1])
        result_parts: list[str] = []
        remaining = budget_chars

        for name, priority, content in sorted_blocks:
            if not content or not content.strip():
                continue

            # Apply per-block cap
            cap = _INJECTION_CAPS.get(name)
            if cap and len(content) > cap:
                content = content[:cap] + "\n[...truncated]"

            # Check budget
            if len(content) > remaining:
                if priority == _Priority.CRITICAL:
                    # Critical blocks get truncated to fit
                    content = content[:remaining]
                    logger.warning(
                        "Truncated CRITICAL block '%s' to %d chars (budget exhausted)",
                        name,
                        remaining,
                    )
                else:
                    logger.info(
                        "Skipped block '%s' (%d chars, priority %s) — budget exhausted"
                        " (%d remaining)",
                        name,
                        len(content),
                        priority.name,
                        remaining,
                    )
                    continue

            result_parts.append(content)
            remaining -= len(content)

        return "\n\n".join(result_parts)

    @staticmethod
    async def _get_kb_context(user_query: str | None) -> str:
        """Fetch relevant articles from the kb-go CLI.

        Uses `kb search <query> --scope <scope> --context` to get pre-formatted
        text ready for prompt injection. Runs the kb binary as a subprocess so
        kb stays a standalone tool and paw-runtime never takes a hard dependency
        on it.

        Returns empty string on any failure (missing binary, missing scope,
        empty query, timeout, non-zero exit). Failures never break prompt
        building — kb is a nice-to-have, not a critical path.
        """
        if not user_query:
            return ""

        from pocketpaw.config import get_settings

        settings = get_settings()
        scope = (settings.kb_scope or "").strip()
        if not scope:
            return ""

        binary = settings.kb_binary or "kb"
        limit = str(settings.kb_limit or 3)

        try:
            proc = await asyncio.create_subprocess_exec(
                binary,
                "search",
                user_query,
                "--scope",
                scope,
                "--context",
                "--limit",
                limit,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                logger.debug("kb context fetch timed out after 3s")
                return ""
        except FileNotFoundError:
            logger.debug("kb binary not found at %s — skipping kb injection", binary)
            return ""
        except Exception as exc:  # noqa: BLE001
            logger.debug("kb context fetch failed (non-fatal): %s", exc)
            return ""

        if proc.returncode != 0:
            return ""

        output = stdout.decode("utf-8", errors="replace").strip()
        return output

    @staticmethod
    def _load_channel_instructions(channel: Channel) -> str:
        """Load channel-specific instruction file (e.g. discord.md)."""
        from pathlib import Path

        _channel_files = {
            Channel.DISCORD: "discord.md",
        }
        filename = _channel_files.get(channel)
        if not filename:
            return ""
        path = Path(__file__).parent / filename
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8").strip()
        except Exception:
            return ""

    @staticmethod
    def _load_gws_instructions() -> str:
        """Load GWS CLI guidance if the google-workspace MCP server is active."""
        from pathlib import Path

        from pocketpaw.mcp.config import load_mcp_config

        configs = load_mcp_config()
        gws_active = any(c.name == "google-workspace" and c.enabled for c in configs)
        if not gws_active:
            return ""

        path = Path(__file__).parent / "gws.md"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8").strip()
