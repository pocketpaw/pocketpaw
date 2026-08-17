"""
Builder for assembling the full agent context.
Created: 2026-02-02
Updated: 2026-08-03 (PA-7b, feat/prompt-assembler-channel) — the digest stops
being thrown away. The assembler call moved into a new
``assemble_system_prompt`` that returns the whole ``AssembledPrompt``;
``build_system_prompt`` is now a delegate returning its ``.text``. The return
type stayed ``str`` because this is public OSS API and ~30 in-tree call sites
(the byte goldens among them) read it as one — the same courtesy the digest
threading extends to an out-of-tree backend whose ``run`` never declared the
parameter. One body, so the text a caller reads and the text the digest was
computed over cannot drift. ``AgentLoop`` calls the new method and forwards
``stable_digest`` to the backend; see ``agents/loop.py`` and ``agents/router.py``.
Updated: 2026-08-03 (PA-7a, feat/prompt-assembler-channel) — THIS MODULE NO
LONGER ASSEMBLES ANYTHING. ``build_system_prompt`` resolves the fifteen
``channel.*`` layers from ``pocketpaw.prompt.registry`` and calls
``pocketpaw.prompt.assemble``; ``_assemble_with_budget``, ``_Priority`` and
``_INJECTION_CAPS`` are deleted. The runtime had two prompt assemblers with two
sets of rules for caps and budgets — the cloud path moved to the layered one in
PR #1851 and this is the channel half following it. The block bodies moved into
``pocketpaw.prompt.channel``; the caps moved onto each layer's ``max_chars``
unchanged, including the two blocks (``pocket_context``, ``current_pocket``)
that ``_INJECTION_CAPS`` never had an entry for and which are therefore still
uncapped. What STAYS here is the ``asyncio.gather``: the bootstrap, memory and
kb fetches run concurrently and their results are handed to the layers as plain
data, because ``assemble`` renders sequentially and three self-fetching layers
would pay their sum where the gather pays their max. One intended behaviour
change: a CRITICAL block that overruns the budget is now emitted WHOLE rather
than cut to ``remaining`` — a budget-sized cut is sized from what the block's
SIBLINGS rendered, so one cache key would name two different texts (see
``prompt.layer.Priority``). The assembled digest is available and DISCARDED;
nothing on the channel path caches an agent on it yet.
Updated: 2026-07-02 (feat/atlas-surface, AT-3) — new always-on "Paw OS
primer" block (#8b, ``atlas_primer``): a compact OS-identity paragraph, the
primitive one-liners generated at build time from the atlas store (never
hard-coded, so seed edits can't drift), and the instruction to call
``atlas_search`` before guessing about OS capabilities and to include the
``surface`` route when pointing a user somewhere. Same MEDIUM priority as
the skills block (#8) and wrapped in try/except so an atlas load failure
never breaks prompt building.
Updated: 2026-07-05 (fix/atlas-relevance-round2) — ``_build_atlas_primer``
now prefers each primitive's authored ``gist`` field (a complete,
distinguishing one-liner) over the old fixed-108-char truncation of
``summary``. The truncation dropped load-bearing words mid-phrase (Belt's
"Instinct gate", Branch's "review/merge/publish + revert"), weakening exactly
the Instinct-vs-Branch distinction the primer exists to sharpen. The gist
falls back to a clause-aware truncation of ``summary`` for any primitive
without one; the whole block still fits the ~500-token / 2000-char budget.
Updated: 2026-06-08 (VIP Onboarding Phase B — session-gated per-user KB scope)
— ``KbContext`` gains an optional ``user_id``; ``_resolve_kb_scopes`` emits a
member-private ``user:{user_id}`` scope at the HEAD of the list (highest
priority, ahead of pocket/agent/workspace) ONLY when ``user_id`` is set. This
is the read-side of the VIP isolation gate: a member's private Gmail/calendar
KB is injected into the agent context only in that member's own session. The
caller (the EE cloud chat gate) decides whether to set ``user_id`` based on
room membership; this resolver only honors the field. ``user_id`` absent →
legacy ordering is byte-identical.
Updated: 2026-06-07 (feat/entity-pocket-profile-field, entity-rooms A2) —
``build_system_prompt`` accepts an optional ``skill_names: frozenset[str]``.
When non-empty, the "Available Skills" block (#8) advertises ONLY those skills —
the non-SDK backends' equivalent of the Claude SDK backend's per-run materialized
skill plugin. ``None`` / empty keeps the legacy all-skills advertisement, so
every non-entity run is byte-identical.
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
import logging
from dataclasses import dataclass

from pocketpaw.bootstrap.default_provider import DefaultBootstrapProvider
from pocketpaw.bootstrap.protocol import BootstrapProviderProtocol
from pocketpaw.bus.events import Channel
from pocketpaw.memory.manager import MemoryManager, get_memory_manager
from pocketpaw.prompt import AssembledPrompt, PromptContext, assemble, prompt_layer_registry
from pocketpaw.prompt.channel import CHANNEL_PROMPT_LAYERS, ChannelInputs

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KbContext:
    """Per-request context for ``_get_kb_context`` scope resolution.

    Stage 3.E "Files as Knowledge". Cloud chat builds one of these from a
    ``ScopeContext`` and threads it into the system-prompt builder so KB
    queries hit the most-specific scope available. Channels and CLI keep
    using the static ``settings.kb_scopes`` fallback.

    ``user_id`` (VIP Onboarding Phase B) carries a MEMBER-PRIVATE scope id —
    the cloud user id (opaque Mongo ObjectId / uuid, never an email, so
    kb-go's on-disk ``:``→``_`` sanitize can't alias two members). When set,
    ``_resolve_kb_scopes`` emits ``user:{user_id}`` at the HEAD of the scope
    list so a member's private mail/calendar KB outranks every shared scope.
    The CALLER is solely responsible for the isolation decision: it must set
    ``user_id`` ONLY in that member's own solo session and NEVER in a shared /
    multi-member room (see the EE gate ``_member_private_user_scope`` /
    ``_kb_scopes_for_context`` in ``cloud/chat/agent_service.py``). This
    resolver does not — and cannot — know room membership; it just honors the
    field. Leaving ``user_id`` unset keeps the legacy pocket/agent/workspace
    ordering byte-identical.
    """

    pocket_id: str | None = None
    agent_id: str | None = None
    workspace_id: str | None = None
    user_id: str | None = None


def _resolve_kb_scopes(ctx: KbContext | None, settings) -> list[str]:
    """Build the prioritised scope list for a request.

    Priority: user > pocket > agent > workspace > whatever's in
    ``settings.kb_scopes``. Most-specific wins, and a member-private
    ``user:`` scope (when present) outranks every shared scope. The static
    settings list is the fallback for runtime paths that don't carry a
    context (CLI, channels without ee/cloud) and for requests that arrive
    with an empty ``KbContext``.

    The ``user:`` tier is emitted ONLY when ``ctx.user_id`` is set. The
    caller (the EE cloud chat gate) is responsible for setting it ONLY in a
    member's own solo session — never a shared room — so one member's private
    data never lands in another member's context. This function trusts the
    field; it has no visibility into room membership.
    """
    if ctx is None:
        return list(settings.kb_scopes or [])
    scopes: list[str] = []
    if ctx.user_id:
        scopes.append(f"user:{ctx.user_id}")
    if ctx.pocket_id:
        scopes.append(f"pocket:{ctx.pocket_id}")
    if ctx.agent_id:
        scopes.append(f"agent:{ctx.agent_id}")
    if ctx.workspace_id:
        scopes.append(f"workspace:{ctx.workspace_id}")
    if not scopes:
        scopes = list(settings.kb_scopes or [])
    return scopes


# ``_Priority`` and ``_INJECTION_CAPS`` lived here until PA-7a. The drop order
# is now ``pocketpaw.prompt.layer.Priority`` — one enum for the whole runtime,
# with the same members and the same values, so nothing had to be translated.
# The caps are now each layer's ``max_chars`` in ``pocketpaw.prompt.channel``,
# with the numbers carried across unchanged. Two things the old table got wrong
# were carried across too rather than fixed, because PA-9 is the task with the
# measurements: it capped ``instructions``, a block this path never produced,
# and it had no entry at all for ``pocket_context`` or ``current_pocket``.
#
# PA-9 (2026-08-03) closed that loop, and the second half above is now stale in
# a way worth recording: ``current_pocket`` is NO LONGER unbounded. PA-8a put the
# ceiling on the layer's INPUTS (``_WIDGET_SUMMARY_MAX_CHARS``) rather than on
# its rendered text, which is why it is invisible from here — there is still no
# ``max_chars``, and there correctly never will be, since this assembler's cap
# truncates the tail and this block's tail is the ``get_pocket`` instruction.
# Measured against the live layer: the block renders at 3,240 chars for a
# 300-widget pocket and 3,241 for a 1,000-widget one, so it does not grow with
# pocket size. ``pocket_context`` remains genuinely uncapped and unmeasured.

# MEASURED 2026-08-03 (PA-9, scripts/evals/prompt_cache_eval.py --arm caps):
# 32,000 chars is 8,874 tokens on the live route — our own prompt text runs 3.61
# chars/token here, not the 4.0 the rules of thumb assume.
#
# KEPT at 32,000. The measurement that argues for keeping it is a caching one:
# 8,874 tokens is comfortably above every Anthropic cache floor (512..4096, see
# ``pocketpaw.llm.caching.CACHE_MIN_TOKENS``), so a byte-stable prompt assembled
# at this budget is large enough to cache. Cutting the budget toward ~14,000
# chars would drop a Haiku-4.5 prompt under its 4096-token floor and silently
# forfeit the ~12x warm-turn saving — the budget would look thriftier per turn
# and cost more per conversation.
_DEFAULT_BUDGET_CHARS = 32_000


class AgentContextBuilder:
    """
    Assembles the final system prompt by combining:
    1. Static Identity (Bootstrap)
    2. Dynamic Memory (MemoryManager)
    3. Current State (e.g., date/time, active tasks)

    Since PA-7a it does not do the assembling. It resolves the inputs — running
    the three independent fetches concurrently — hands them to the
    ``channel.*`` layers as a :class:`~pocketpaw.prompt.channel.inputs.ChannelInputs`,
    and lets :func:`pocketpaw.prompt.assemble` apply the per-layer caps and the
    budget. Lower-priority layers are dropped whole when the budget is tight;
    a CRITICAL one is emitted whole and the budget overruns, because a
    budget-sized cut cannot be reconciled with a cache key.
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
        skill_names: frozenset[str] | None = None,
    ) -> str:
        """The assembled prompt TEXT. See :meth:`assemble_system_prompt` for the rest.

        STILL RETURNS ``str``, and that is PA-7b's return-type decision rather
        than an omission. The digest had to reach ``AgentLoop`` and this method
        had two things to hand back; the options were to widen the return type
        or to split the method, and the split wins on one argument that the
        surrounding sprint already accepts elsewhere: this is public OSS API. An
        out-of-tree embedder calling it gets a string today, and the same
        reasoning that leaves an out-of-tree BACKEND whose ``run`` lacks
        ``system_prompt_digest`` working untouched (see
        ``agents.backend._accepts_prompt_digest``) applies to a caller that does
        ``len(prompt)`` on the result. Thirty in-tree call sites — every golden,
        every bootstrap test — also keep working unedited, which means the byte
        baseline is checked by the tests that already existed rather than by
        thirty edited ones.

        There is exactly ONE body: this delegates, so the text can never drift
        from the text the digest was computed over. Every argument is forwarded
        unchanged; they are documented on ``assemble_system_prompt``.
        """
        return (
            await self.assemble_system_prompt(
                include_memory=include_memory,
                user_query=user_query,
                channel=channel,
                sender_id=sender_id,
                session_key=session_key,
                file_context=file_context,
                agents_md_dir=agents_md_dir,
                metadata=metadata,
                budget_chars=budget_chars,
                image_bytes=image_bytes,
                kb_ctx=kb_ctx,
                skill_names=skill_names,
            )
        ).text

    async def assemble_system_prompt(
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
        skill_names: frozenset[str] | None = None,
    ) -> AssembledPrompt:
        """Build the complete system prompt, and the digest of its stable layers.

        The real body; ``build_system_prompt`` is its ``.text``. Named to match
        ``AgentPool._assemble_system_prompt``, which returns the same type on the
        cloud path — one shape for "assemble a prompt", two callers.

        ``stable_digest`` is a hash over the KEYED layers' ``cache_key`` values,
        NOT over the returned text (see ``pocketpaw.prompt.assembler``). What it
        deliberately does not cover is everything done to the text AFTER this
        returns: ``AgentLoop._reinforce_identity`` appends the identity block
        every 5th message, and ``ClaudeSDKBackend.run`` splices a growing
        ``# Recent Conversation`` block into ``options.system_prompt``. Both are
        per-turn mutations of stable content, and a digest that moved for them
        would rebuild the warm client on the turns they fire — the exact churn
        the layered digest exists to remove.

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
            skill_names: Optional per-entity skill subset (entity-rooms A2,
                resolved from the entity pocket's ``surface_profile.skill_names``).
                When set to a NON-EMPTY frozenset, the "Available Skills" block
                advertises ONLY those skills (the non-SDK backends' equivalent of
                the SDK's per-run materialized plugin). ``None`` / empty keeps the
                legacy all-skills advertisement — every non-entity run is
                unchanged.
        """
        # 1. Load static identity, memory context, and kb context concurrently
        # (independent I/O — identity is a function call, memory hits disk/vector db,
        # kb shells out to a subprocess). asyncio.gather keeps the critical path fast.
        #
        # THIS STAYS HERE RATHER THAN MOVING INTO THREE LAYERS, and it is the one
        # structural decision PA-7a made that is not "move the block". ``assemble``
        # renders layers in a sequential ``for`` loop, so a memory layer and a kb
        # layer each awaiting their own fetch would cost their SUM on every channel
        # turn where this gather costs their MAX — and the kb one spawns a
        # subprocess. Making ``assemble`` render concurrently would fix it too, and
        # was rejected: it changes the CLOUD path's execution model to solve a
        # channel-path problem, and no cloud layer has been audited for
        # order-independence. So the fetches stay batched here and their results
        # cross as plain data, which is the discipline ``surface_preamble`` and
        # ``atlas_primer`` already use. Pinned by
        # ``tests/test_channel_prompt_layers.py::test_the_three_io_fetches_still_run_concurrently``.
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

        # When soul is active, soul's bootstrap provider already handles persistent
        # memory (identity, personality, knowledge domains). Skip regular long-term
        # memory injection to avoid duplication — the agent should use soul_recall
        # for fact retrieval instead. Session history is still managed by regular
        # memory. The decision lives here, not in the layer, because only the
        # builder holds the provider it is a fact about.
        from pocketpaw.soul import SoulBootstrapProvider

        if isinstance(self.bootstrap, SoulBootstrapProvider):
            memory_context = ""

        ctx = PromptContext(
            # No ``AgentInstance`` on this path and none faked: the pool's
            # instance is a cloud concept, and every field the channel layers
            # read arrives on ``channel_inputs`` instead. The cloud-only fields
            # take their no-content defaults, so a cloud layer that ever appears
            # in a channel list renders nothing rather than reading a stub.
            instance=None,
            agent_id="",
            message=user_query or "",
            instructions="",
            knowledge_context="",
            system_message_override=None,
            channel_inputs=ChannelInputs(
                identity=context.to_system_prompt(),
                identity_cache_key=getattr(context, "identity_cache_key", None),
                memory_context=memory_context,
                kb_context=kb_context,
                channel=channel,
                sender_id=sender_id,
                session_key=session_key,
                file_context=file_context,
                metadata=metadata,
                agents_md_dir=agents_md_dir,
                skill_names=skill_names,
            ),
        )
        layers = [prompt_layer_registry.get(name) for name in CHANNEL_PROMPT_LAYERS]
        # Returned WHOLE as of PA-7b. Until then this method took ``.text`` and
        # threw the digest away, because nothing downstream could receive it;
        # ``AgentLoop`` now forwards it through ``AgentRouter`` to any backend
        # whose ``run`` declares the parameter, so the channel path's warm client
        # keys on what the LAYERS said about themselves instead of on
        # ``claude_sdk._behavior_prefix``'s guess at where the volatile region
        # starts.
        return await assemble(layers, ctx, budget_chars=budget_chars)

    @staticmethod
    def _build_atlas_primer() -> str:
        """Delegate to the atlas layer's builder (moved there in PA-7a).

        The body moved to ``pocketpaw.prompt.channel.environment`` so it renders
        inside a layer and under the assembler's render guard, which is what
        replaced this block's ``try/except``. The name stays because
        ``tests/atlas/test_primer_block.py`` drives the seed's content through
        it, and that content is worth keeping under test at a stable name.
        """
        from pocketpaw.prompt.channel.environment import build_atlas_primer

        return build_atlas_primer()

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
        ``user:{id} > pocket:{id} > agent:{id} > workspace:{id}`` —
        most-specific wins, and a member-private ``user:`` scope (set by the
        cloud chat gate only in a member's own session) outranks every shared
        scope. Without a ``kb_ctx`` (channel paths, CLI), the static
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

        from pocketpaw._registry import first

        provider = first("pocketpaw.embeddings")
        if provider is None:
            logger.debug("no embeddings provider registered; falling back to BM25")
            return None

        embedder = provider.build_embedder(settings)
        if embedder is None or "image" not in embedder.supports_modalities:
            return None

        try:
            emb = await embedder.embed_query(text=user_query, image_bytes=image_bytes)
        except Exception:
            logger.exception("query embedding failed; falling back to BM25 for this turn")
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
                logger.debug("kb hybrid fetch for scope %s timed out after 5s", scope)
                return ""
        except FileNotFoundError:
            logger.debug("kb binary not found at %s — skipping kb injection", binary)
            return ""
        except Exception as exc:  # noqa: BLE001
            logger.debug("kb hybrid fetch for scope %s failed (non-fatal): %s", scope, exc)
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
