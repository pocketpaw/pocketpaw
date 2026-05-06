"""
Builder for assembling the full agent context.
Created: 2026-02-02
Updated: 2026-05-03 - Stage 3.E "Files as Knowledge". Added ``KbContext``
dataclass + ``_resolve_kb_scopes`` so per-request callers (the cloud chat
path) can prioritise pocket > agent > workspace ahead of the static
``settings.kb_scopes`` fallback. ``_get_kb_context`` accepts an optional
``kb_ctx``; the existing channel + CLI paths continue to use the static
list with no change in behaviour.
Updated: 2026-04-30 - Stage 2.D "Files as Knowledge". _get_kb_context now
accepts ``image_bytes`` for the chat-with-image path. When set and a
multimodal embedder is configured, it embeds (text + image) once,
caches the resulting vector to a temp file, and runs each scope's kb
search in hybrid mode (BM25 + cosine via RRF). When unset (the common
case) the call shape stays identical to the Phase 1 BM25-only path.
Updated: 2026-04-30 - Multi-scope KB injection (Stage 1.B "Files as
Knowledge"). _get_kb_context now reads ``settings.kb_scopes`` (list) and
queries each scope independently, dividing the token budget by scope count
and concatenating the results under ``### From <scope>`` headers. The
deprecated ``kb_scope`` (string) feeds in via a back-compat shim in
``Settings``.
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
from dataclasses import dataclass

from pocketpaw.bootstrap.default_provider import DefaultBootstrapProvider
from pocketpaw.bootstrap.protocol import BootstrapProviderProtocol
from pocketpaw.bus.events import Channel
from pocketpaw.bus.format import CHANNEL_FORMAT_HINTS
from pocketpaw.memory.manager import MemoryManager, get_memory_manager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KbContext:
    """Per-request context for ``_get_kb_context`` scope resolution.

    Stage 3.E "Files as Knowledge". Cloud chat builds one of these from a
    ``ScopeContext`` and threads it into the system-prompt builder so KB
    queries hit the most-specific scope available. Channels and CLI keep
    using the static ``settings.kb_scopes`` fallback.
    """

    pocket_id: str | None = None
    agent_id: str | None = None
    workspace_id: str | None = None


def _resolve_kb_scopes(ctx: KbContext | None, settings) -> list[str]:
    """Build the prioritised scope list for a request.

    Priority: pocket > agent > workspace > whatever's in ``settings.kb_scopes``.
    Most-specific wins. The static settings list is the fallback for runtime
    paths that don't carry a context (CLI, channels without ee/cloud) and for
    requests that arrive with an empty ``KbContext``.
    """
    if ctx is None:
        return list(settings.kb_scopes or [])
    scopes: list[str] = []
    if ctx.pocket_id:
        scopes.append(f"pocket:{ctx.pocket_id}")
    if ctx.agent_id:
        scopes.append(f"agent:{ctx.agent_id}")
    if ctx.workspace_id:
        scopes.append(f"workspace:{ctx.workspace_id}")
    if not scopes:
        scopes = list(settings.kb_scopes or [])
    return scopes


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
        image_bytes: bytes | None = None,
        kb_ctx: KbContext | None = None,
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
            image_bytes: Optional inline image attached to the chat message.
                When set together with user_query and a multimodal embedder
                is configured, the KB context fetch switches to hybrid mode
                (BM25 + vector cosine fused via RRF). Phase 2 of "Files as
                Knowledge". When None the call shape is identical to the
                Phase 1 BM25-only path.
            kb_ctx: Optional per-request scope context. When set, KB queries
                resolve scope priority pocket > agent > workspace before
                falling through to ``settings.kb_scopes``. Stage 3.E of
                "Files as Knowledge". When None, the static settings list
                is used unchanged — channel and CLI paths keep working
                without changes.
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
                self._get_kb_context(user_query, image_bytes=image_bytes, kb_ctx=kb_ctx),
            )
        else:
            context, kb_context = await asyncio.gather(
                self.bootstrap.get_context(),
                self._get_kb_context(user_query, image_bytes=image_bytes, kb_ctx=kb_ctx),
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

        # 4d. Inject current pocket info so the AI knows what pocket is open.
        # The full pocket document is NOT embedded here — that would blow the
        # Windows CLI arg limit for large rippleSpec.ui trees. The agent
        # retrieves it on demand via the `mcp__pocketpaw_pocket__get_pocket`
        # tool (in-process MCP server; see agents/sdk_mcp_pocket.py).
        if metadata and metadata.get("pocket_context"):
            import json

            pc = metadata["pocket_context"]
            pocket_id = pc.get("id", "unknown")
            widget_summary = pc.get("widgets", [])
            pocket_tag = (
                f"\n<current-pocket>\n"
                f"id: {pocket_id}\n"
                f"name: {pc.get('name', 'Untitled')}\n"
                f"widgets_summary: {json.dumps(widget_summary)}\n"
                f"\n"
                f"SCOPE — read this carefully before doing anything:\n"
                f"In this conversation, \"pocket\" / \"this pocket\" / \"the\n"
                f"pocket\" always means THIS workspace dashboard\n"
                f"(id ``{pocket_id}``) — a MongoDB document the user is\n"
                f"viewing on screen. It is NOT the PocketPaw application,\n"
                f"NOT the source tree on disk, NOT any file under\n"
                f"``D:\\paw`` or ``backend/`` or ``ee/cloud/``. \"Edit the\n"
                f"pocket\", \"add a widget\", \"more widgets\" all refer to\n"
                f"this document — operate on it through the\n"
                f"``mcp__pocketpaw_pocket__*`` tools ONLY. Do NOT use\n"
                f"shell, file_edit, grep, or web_search for pocket\n"
                f"operations — they cannot read or write the document.\n"
                f"\n"
                f"NOTE: `widgets_summary` is a shallow hint (names + types)\n"
                f"and is OFTEN EMPTY for UISpec-tree pockets — absence here\n"
                f"does NOT mean the pocket is empty. The real content lives\n"
                f"in rippleSpec.ui.\n"
                f"\n"
                f"BEFORE answering any question about this pocket's contents,\n"
                f"widgets, layout, data, or configuration, you MUST first call:\n"
                f"  tool: mcp__pocketpaw_pocket__get_pocket\n"
                f"  args: {{\"pocket_id\": \"{pocket_id}\"}}\n"
                f"That returns the full document (rippleSpec, widgets,\n"
                f"metadata, visibility). Base your answer on that, not on\n"
                f"the summary above.\n"
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
    async def _get_kb_context(
        user_query: str | None,
        *,
        image_bytes: bytes | None = None,
        kb_ctx: KbContext | None = None,
    ) -> str:
        """Fetch relevant articles from the kb-go CLI across configured scopes.

        Each scope in the resolved scope list is queried independently with
        ``kb search <query> --scope <s> --context --limit M`` where
        ``M = max(1, total_limit // len(scopes))``. Results are concatenated
        under ``### From <scope>`` headers so the model can attribute hits.
        Per-scope failures are logged at debug and skipped so one missing
        scope cannot break the prompt build.

        When ``kb_ctx`` is provided (Stage 3.E), scope priority is
        ``pocket:{id} > agent:{id} > workspace:{id}`` — most-specific wins.
        Without a ``kb_ctx`` (channel paths, CLI), the static
        ``settings.kb_scopes`` list is used unchanged.

        When ``image_bytes`` is set and a multimodal embedder is configured,
        the call shape switches to hybrid mode: a single embedding pass
        builds the (text + image) query vector and each scope is searched
        with ``--hybrid --query-vec <vec.json>``. The temp vec file is
        cleaned up before returning. Embedder failures fall back to the
        BM25-only path so a transient cloud outage doesn't kill chat.

        Returns an empty string when ``user_query`` is empty, when no scopes
        are configured (or only the deprecated ``kb_scope`` is set, see the
        ``_migrate_kb_scope`` validator on ``Settings``), or when every
        scope errors / returns nothing.
        """
        if not user_query:
            return ""

        from pocketpaw.config import get_settings

        settings = get_settings()
        # Stage 3.E: per-request scope resolution wins over the static list.
        # ``kb_scopes`` (the static list) is the canonical fallback. The
        # deprecated single ``kb_scope`` is folded into ``kb_scopes`` by
        # the model validator, so by the time we read settings here we
        # only ever see the list.
        raw_scopes = _resolve_kb_scopes(kb_ctx, settings)
        scopes = [s.strip() for s in raw_scopes if s and s.strip()]
        if not scopes:
            return ""

        binary = settings.kb_binary or "kb"
        total_limit = settings.kb_limit or 3
        per_scope_limit = max(1, total_limit // len(scopes))

        # Stage 2.D: if the user attached an image, embed (text + image) once
        # and run hybrid searches across scopes. The vec file is shared
        # across per-scope subprocesses to avoid re-serializing on each call.
        query_vec_path: str | None = None
        try:
            query_vec_path = await AgentContextBuilder._maybe_build_query_vec(
                user_query=user_query,
                image_bytes=image_bytes,
                settings=settings,
            )
            sections: list[str] = []
            for scope in scopes:
                section = await AgentContextBuilder._fetch_kb_scope(
                    binary=binary,
                    query=user_query,
                    scope=scope,
                    limit=per_scope_limit,
                    query_vec_path=query_vec_path,
                )
                if section:
                    sections.append(f"### From {scope}\n{section}")
        finally:
            if query_vec_path:
                import os

                try:
                    os.unlink(query_vec_path)
                except OSError:
                    logger.debug("query-vec cleanup failed for %s", query_vec_path)

        return "\n\n".join(sections)

    @staticmethod
    async def _maybe_build_query_vec(
        *,
        user_query: str,
        image_bytes: bytes | None,
        settings,
    ) -> str | None:
        """Embed (text + image) and write the vector to a temp JSON file.

        Returns the path on success, ``None`` when the embedder isn't
        configured / can't handle images / fails. The caller is
        responsible for unlinking the file.
        """
        if image_bytes is None:
            return None
        if not getattr(settings, "kb_vectors_enabled", False):
            return None

        try:
            from ee.cloud.embeddings import build_embedder
        except Exception:
            logger.debug("embeddings package unavailable; falling back to BM25")
            return None

        embedder = build_embedder(settings)
        if embedder is None or "image" not in embedder.supports_modalities:
            return None

        try:
            emb = await embedder.embed_query(text=user_query, image_bytes=image_bytes)
        except Exception:
            logger.exception(
                "query embedding failed; falling back to BM25 for this turn"
            )
            return None

        import json
        import tempfile

        tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115 — manual lifecycle
            mode="w",
            prefix="paw-query-vec-",
            suffix=".json",
            delete=False,
            encoding="utf-8",
        )
        try:
            tmp.write(json.dumps({"vector": emb.vector}))
            tmp.flush()
        finally:
            tmp.close()
        return tmp.name

    @staticmethod
    async def _fetch_kb_scope(
        *,
        binary: str,
        query: str,
        scope: str,
        limit: int,
        query_vec_path: str | None = None,
    ) -> str:
        """Run ``kb search ... --scope <scope>`` once. Empty on any failure.

        When ``query_vec_path`` is set the call switches to hybrid mode
        (``--hybrid --query-vec <path> --topk <limit>``). The plain-text
        ``--context`` flag is dropped in hybrid mode because kb-go's
        hybrid output is JSON-shaped, so we re-derive the human-readable
        section from the JSON title + summary fields.
        """
        if query_vec_path:
            return await AgentContextBuilder._fetch_hybrid_scope(
                binary=binary,
                query=query,
                scope=scope,
                limit=limit,
                query_vec_path=query_vec_path,
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                binary,
                "search",
                query,
                "--scope",
                scope,
                "--context",
                "--limit",
                str(limit),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                logger.debug("kb context fetch for scope %s timed out after 3s", scope)
                return ""
        except FileNotFoundError:
            logger.debug("kb binary not found at %s — skipping kb injection", binary)
            return ""
        except Exception as exc:  # noqa: BLE001
            logger.debug("kb context fetch for scope %s failed (non-fatal): %s", scope, exc)
            return ""

        if proc.returncode != 0:
            return ""

        return stdout.decode("utf-8", errors="replace").strip()

    @staticmethod
    async def _fetch_hybrid_scope(
        *,
        binary: str,
        query: str,
        scope: str,
        limit: int,
        query_vec_path: str,
    ) -> str:
        """Run ``kb search <query> --hybrid --query-vec <path> --scope <s>``.

        Hybrid kb output is a JSON array of ``{id, title, summary, ...}``
        rows. We render a compact text section per hit so the system
        prompt assembler can drop it under ``### From <scope>`` without
        further processing.
        """
        import json

        try:
            proc = await asyncio.create_subprocess_exec(
                binary,
                "search",
                query,
                "--scope",
                scope,
                "--hybrid",
                "--query-vec",
                query_vec_path,
                "--topk",
                str(limit),
                "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                logger.debug(
                    "kb hybrid fetch for scope %s timed out after 5s", scope
                )
                return ""
        except FileNotFoundError:
            logger.debug("kb binary not found at %s — skipping kb injection", binary)
            return ""
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "kb hybrid fetch for scope %s failed (non-fatal): %s", scope, exc
            )
            return ""

        if proc.returncode != 0:
            return ""

        try:
            rows = json.loads(stdout.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return ""
        if not isinstance(rows, list):
            return ""

        parts: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            title = row.get("title") or row.get("id") or ""
            summary = (row.get("summary") or "").strip()
            if title and summary:
                parts.append(f"- {title}\n  {summary}")
            elif title:
                parts.append(f"- {title}")
        return "\n".join(parts)

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
