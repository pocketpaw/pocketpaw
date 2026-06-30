"""
Claude Agent SDK backend for PocketPaw.
Updated: 2026-06-30 (feat/session-supervisor SS-1) — ``run`` accepts an optional
  ``session_handle: SessionHandle | None``. When it carries a non-None
  ``cli_session_id``, ``_build_options`` sets ``ClaudeAgentOptions.resume`` so the
  CLI subprocess RESUMES that on-disk session natively (no Mongo-history replay),
  and ``run`` routes the turn down the FRESH stateless ``query()`` launch path
  rather than the warm persistent client (the warm client applies its options
  only at first ``connect()`` and its cache key omits ``resume``, so a reused warm
  client would silently ignore a fresh ``resume`` — the documented hazard). The
  freshly-rebuilt per-turn ``system_prompt`` still rides ``--system-prompt`` on
  every turn, so a resumed session honors a new system prompt. Turn-1 capture: the
  SDK's init/system message carries a ``session_id`` in its ``data``; when a
  ``session_handle`` is present, ``run`` extracts it and surfaces it once as a
  ``session_id`` ``AgentEvent`` (mirroring the ``token_usage`` event) so the
  controller can persist it for a later resume (SS-3). ``cli_session_id is None``
  / no handle = the unchanged legacy warm-client path. ``session_handle`` is
  forwarded by ``AgentPool.run`` only when non-None (withhold-when-empty idiom).
Updated: 2026-06-26 (ART-2) — the agent's working directory is now resolved
  PER-RUN via ``_resolve_cwd`` instead of being frozen to
  ``settings.file_jail_path`` at ``__init__``. OSS / dedicated behavior is
  unchanged (still ``file_jail_path``); when an EE ``pocketpaw.agent_extensions``
  provider supplies ``agent_cwd`` (the cloud product), the run uses a
  per-workspace/session jail so a tenant's file ops never co-mingle in the
  shared home dir. A provider that RAISES (a cloud run with no resolvable
  workspace) propagates — fail-closed, never a silent fallback to ``~``.
  ``_build_options`` carries the resolved cwd, so ``run`` and ``prewarm`` warm
  the same per-session jail. ART-2 hardening: the resolved cwd is folded into
  ``_client_cache_key`` so warm-client tenant isolation is STRUCTURAL (a changed
  cwd forces a fresh subprocess), not merely an implicit session_key<->cwd
  coupling; the now-inert ``set_working_directory`` setter was removed
  (``_build_options`` no longer reads ``self._cwd``); and ``get_status`` reports
  ``base_cwd`` (the OSS/default base) instead of a misleading ``cwd``.
Updated: 2026-06-26 (integration/model-catalog-v2, MCG-11) — the ResultMessage
  token-usage path now runs ``pocketpaw.llm.caching.report_savings`` over the SDK
  usage to surface STRUCTURED prompt-cache telemetry (cache_read_tokens,
  cache_write_tokens, cache_hit_rate, cache_est_tokens_saved) on the
  ``token_usage`` AgentEvent metadata and log the per-turn margin. This is the
  measurement hook for the byte-stable cached prefix used by site/pocket-gen;
  the existing ``cached_input_tokens`` field is unchanged for back-compat.
Updated: 2026-06-13 (feat/claude-sdk-prewarm) — added ``prewarm``: eagerly
  ``connect()`` the warm CLI subprocess for a session BEFORE its first turn so
  the first real ``run`` reuses it instead of paying the ~12s cold connect. To
  make the prewarmed client's cache key MATCH the first turn's (else turn 1
  evicts it — a net loss), the whole ``options_kwargs`` -> ``options`` assembly
  was extracted from ``run`` into a shared ``_build_options`` helper that both
  call; ``run``'s behavior is byte-identical (the only behavioral fix: ``llm`` is
  now declared above the ``try`` so the error handler is safe if option assembly
  itself raises). ``prewarm`` is fire-and-forget: it swallows ALL errors, never
  raises, no-ops when a run holds the lease or the SDK/CLI is unavailable, and on
  failure tears down only a client no run owns. A new ``_client_lock`` serializes
  the reuse-or-connect critical section in ``_get_or_create_client`` so a prewarm
  racing the first ``run`` (the trigger fires prewarm as a background task)
  cannot double-connect — the loser of the lock reuses the winner's client. The
  EE trigger lives in ``run_core._prewarm_session`` (gated to smart-routing-OFF,
  where the model is message-independent so a message-less prewarm matches the
  turn's key). Skill sessions on smart-routing-ON deployments still cold-start
  turn 1 (documented limitation). Supersedes the prior "prewarm out of scope" note.
Updated: 2026-06-13 (fix/claude-sdk-warm-client-skills) — skill/tool-bearing
  runs now REUSE the warm persistent CLI subprocess instead of re-spawning a
  fresh stateless query every turn (a ~6s/turn floor on any skill chat). The
  2026-06-07 entry below BYPASSED the warm client for skill runs because
  ``_client_cache_key`` did not hash the plugin set, so a warm client could not
  tell a skill turn from a non-skill one. The cache key now folds in
  ``_plugin_digest`` — a hash of the skill IDENTITY (sorted ``skill_names`` +
  whether the bundled-skills plugin is loaded), NEVER the materialized
  ``plugins=`` PATH (``materialize_run_skills`` mints a fresh ``mkdtemp`` per
  run, so hashing the path would change the key every turn and defeat reuse).
  With identity in the key, ``_get_or_create_client`` reuses the subprocess for
  a same-skill turn and rebuilds it for a changed skill set. Lifecycle: because
  the warm subprocess keeps the ``plugins=`` path from its first ``connect()``,
  the materialized dir is cached per digest on the instance
  (``_skills_dir_by_digest``) and reused across same-skill turns; it is removed
  only when its warm client is evicted (``_get_or_create_client``) or on
  ``cleanup()`` — NOT by the per-run ``finally``, which now rmtree's the dir
  ONLY in the genuine stateless-fallback case (when ``_client_in_use`` forced a
  stateless query and no warm client adopted the dir). The ``skip_warm_client``
  bypass is removed. (``prewarm`` was deferred here and shipped in the
  feat/claude-sdk-prewarm follow-up above.)
Updated: 2026-06-07 (feat/entity-pocket-profile-field, entity-rooms A2) — ``run``
  also accepts ``skill_names: frozenset[str]``, the per-entity skill subset
  (resolved upstream from the entity pocket's ``surface_profile.skill_names``).
  When non-empty, those skills are MATERIALIZED into a throwaway local-plugin
  dir (``pocketpaw.skills.materialize``) and appended to the SDK ``plugins=``
  list so the agent sees ONLY the named skills (coexisting with the bundled
  plugin). Because ``setting_sources=[]`` disables filesystem + ``skills=``
  discovery, a local plugin is the only working channel — same mechanism the
  bundled skills use. The persistent ("warm") client is BYPASSED for skill runs
  (its cache key omits ``plugins=`` and it only applies options at first
  connect), so the run goes through a fresh stateless query whose options carry
  the plugin; the temp dir is removed in the outer ``finally``. Empty
  ``skill_names`` is a no-op. Crosses the EE→OSS boundary as a plain frozenset.
Updated: 2026-06-06 (feat/entity-pocket-profile-field, entity-rooms chunk ①) —
  ``run`` also accepts ``allow_sdk_tools: frozenset[str]``, the per-entity
  ADDITIVE SDK-tool allowlist (resolved upstream from the entity pocket's
  ``surface_profile.allowed_sdk_tools`` and forwarded by ``AgentPool.run``). It
  is UNIONed into ``allowed_tools`` BEFORE the deny set is subtracted, so the
  precedence is ``effective = (agent_tools ∪ allow) − deny`` (the surface deny is
  the HARD cap — an allow can never re-add a denied id). Empty for every legacy /
  non-entity run, so the allowlist is unchanged there. Like the deny set, it
  crosses the EE→OSS boundary as a plain ``frozenset[str]`` and never imports
  ``pocketpaw_ee``. The persistent-client cache key already folds in
  ``allowed_tools``, so an entity's allow/deny change rebuilds the warm
  subprocess on the next turn automatically.
Updated: 2026-06-05 (feat/sites-svelte-engine) — ``run`` now accepts a threaded
  ``deny_mcp_tool_ids: frozenset[str]`` per-surface MCP-tool deny set (resolved
  upstream from the request's ``SurfaceProfile`` and forwarded by
  ``AgentPool.run``) and subtracts those ids from ``allowed_tools`` BEFORE the
  SDK launches, so the agent is physically unable to call them. This REPLACES the
  prior prompt-SNIFFING gate that string-matched a ``<surface ... engine="svelte"
  />`` marker in the system prompt to strip the ripple-create tools — brittle (a
  preamble wording change or an unrelated prompt quoting the marker flipped it)
  and unable to express the three-mode /sites policy the ``SurfaceProfile``
  resolver now owns. On /sites svelte-create the resolved set forbids the two
  ripple-create tools (``create_landing_site`` + ``pocket_specialist__create``)
  so the agent cannot fall back to a rippleSpec landing page — leaving
  ``create_svelte_site`` + ``publish`` as the only create path; prose-only routing
  ("PREFER create_svelte_site, do NOT call create_landing_site") was proven
  insufficient (the ``ripple_spec.unknown_widget_type`` warnings). The set is
  empty for refine / ripple-engine / non-sites runs, so their tools (incl.
  ``pocket_specialist__edit``) are untouched. The OSS backend takes a plain
  ``frozenset[str]`` and never imports ``pocketpaw_ee``.
Updated: 2026-05-31 (fix/home-backend-summary-per-turn) — the persistent-client
  cache key now folds in a digest of the system prompt's STABLE behavioral
  prefix (``_client_cache_key`` / ``_behavior_prefix``), not just
  session+model+tools. The home agent bakes its non-secret backend summary
  ({base_url, auth_type, configured}) into the static system prompt; that
  prompt is applied to the subprocess only at connect() time and ignored on
  warm reuse, so configuring a pocket's backend mid-session stayed frozen
  until a cold restart. Keying on the behavioral prefix makes a config flip
  change the key, which rebuilds the client on the very next turn. The volatile
  per-turn tail (KB block, soul memories, conversation history) is stripped
  before hashing so ordinary turns still reuse the warm subprocess.
Updated: 2026-05-28 (#FU-F) — promote silent MCP provider build failures from
  DEBUG to WARNING. A stale editable install (CloudForesightMcpProvider with a
  missing SDK dependency) failed silently; the diagnostic took 30+ minutes.
  Now logs provider class name, exception type, and message at WARNING with
  exc_info so operators see it immediately on dashboard restart. Added an INFO
  startup summary log (``MCP servers registered: …``) after the
  ``pocketpaw.mcp_servers`` entry-point loop so the operator can confirm the
  full registered set at a glance.
Updated: 2026-05-25 (PR #1222 R1 Blocker 1) — added
  ``attach_subprocess_env``. The pocket-specialist runtime calls it to
  thread per-request tenancy values (``POCKETPAW_WORKSPACE_ID`` /
  ``POCKETPAW_USER_ID`` / ``POCKETPAW_INTERNAL_TOKEN``) into the
  Claude Code subprocess at spawn time without mutating the parent
  process's ``os.environ``. The original MVP path wrote those vars to
  the parent env from a request handler — racy across concurrent
  requests. ``run()`` merges the attached dict into
  ``options_kwargs["env"]`` AFTER the LLM-auth env so an attached value
  cannot accidentally clobber the auth key. Each isolated backend
  instance carries its own stash, so one request's tenancy can never
  leak into another's subprocess.
Updated: 2026-06-12 — ``_collect_mcp_tool_ids`` now also allowlists EXTERNAL
  MCP servers from ``load_mcp_config`` (``~/.pocketpaw/mcp_servers.json``) with
  a bare ``mcp__<server>`` entry. They are registered with the SDK in
  ``_get_mcp_servers`` but, lacking an in-process ``tool_ids()`` provider, their
  tools never reached the allowlist and were uncallable (a deployment's
  ``fabric`` server was registered yet the agent could not call
  ``fabric_query`` / ``fabric_stats``).
Updated: 2026-05-22 (#1174) — extracted the in-process MCP tool-id allowlist
  collection into ``_collect_mcp_tool_ids``. The cloud ``pocketpaw_pocket``
  server now carries a writable ``add_widget`` tool alongside the read tools;
  its id flows through the same provider loop, so the home-pocket agent can
  call ``add_widget`` on the ``claude_agent_sdk`` backend.
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
import hashlib
import logging
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, NamedTuple

from pocketpaw.agents.backend import (
    BackendInfo,
    BaseAgentBackend,
    Capability,
    SessionHandle,
)
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.config import Settings
from pocketpaw.security.rails import is_substring_blocked
from pocketpaw.tools.policy import OPT_IN_MCP_SERVERS, ToolPolicy

logger = logging.getLogger(__name__)


class _BuiltOptions(NamedTuple):
    """The product of ``ClaudeSDKBackend._build_options`` (feat/claude-sdk-prewarm).

    Bundles the assembled ``ClaudeAgentOptions`` plus everything ``run``'s
    dispatch + finally still need after option assembly was extracted into a
    shared helper so ``prewarm`` can build the IDENTICAL options the first turn
    will (same cache key → the prewarmed warm client is reused, not evicted):

      * ``options`` — the ``ClaudeAgentOptions`` instance to connect / query with.
      * ``options_kwargs`` — the raw kwargs dict; the token-usage event reads
        ``model`` off it.
      * ``llm`` — the resolved LLM client; ``run`` uses it to format API errors.
      * ``run_skills_root`` / ``skills_dir_adopted`` / ``plugin_digest`` — the
        per-run materialized-skills-plugin lifecycle triple (entity-rooms A2 +
        the warm-reuse fix): the dir path (or None), whether a warm client
        adopted it (so the per-run finally must NOT rmtree it), and the
        plugin-identity hash threaded into the cache key + the dir cache.
    """

    options: Any
    options_kwargs: dict[str, Any]
    llm: Any
    run_skills_root: Path | None
    skills_dir_adopted: bool
    plugin_digest: str


# Default identity fallback (used when AgentContextBuilder prompt is not available)
_DEFAULT_IDENTITY = (
    "You are PocketPaw, a helpful AI assistant running locally on the user's computer."
)

_HTTP_TRANSPORTS: frozenset[str] = frozenset({"http", "sse", "streamable-http"})

# Universal pocket-creation grant. When a surface imposes a restrictive MCP
# allow-list (``SurfaceProfile.allow_mcp_tool_ids``), these ids are always kept
# so "create a pocket" works from every chat mode — the core capability. The
# create-pocket SKILL is plugin-loaded (not an MCP tool), so it stays reachable
# regardless. Plain ids (no EE import): allow/deny sets cross the OSS boundary
# as bare ``frozenset[str]``.
POCKET_CREATION_GRANT: frozenset[str] = frozenset(
    {
        "mcp__pocketpaw_pocket_specialist__create",
        "mcp__pocketpaw_pocket_planner__plan_pocket",
    }
)

# MCP servers whose tools survive ANY restrictive allow-list — the "general
# tools everywhere" set: connectors (composio) + the pocket lifecycle (read /
# widget edit / create / edit / plan). A mode's allow-list only names its
# SPECIALIZED tools; these servers stay available so every mode can still use
# connectors and build/edit pockets. Server is ``<server>`` in
# ``mcp__<server>__<tool>``.
ALWAYS_ALLOWED_MCP_SERVERS: frozenset[str] = frozenset(
    {
        "composio",
        "pocketpaw_pocket",
        "pocketpaw_pocket_specialist",
        "pocketpaw_pocket_planner",
    }
)


def _mcp_server_of(tool_id: str) -> str:
    """Extract ``<server>`` from an ``mcp__<server>__<tool>`` id (else "")."""
    parts = tool_id.split("__")
    return parts[1] if len(parts) >= 2 and parts[0] == "mcp" else ""


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
        # Serializes the connect-or-reuse critical section in
        # ``_get_or_create_client`` (feat/claude-sdk-prewarm). ``prewarm`` runs
        # CONCURRENTLY with the first ``run`` (fired as a background task before
        # the turn), and ``_get_or_create_client`` ``await``s ``disconnect()`` /
        # ``connect()`` — without this lock the run could enter the section while
        # prewarm is mid-connect and the two would race to create / evict the
        # subprocess (double connect, or the run throwing away the half-built
        # prewarmed client). With the lock, whichever arrives second sees the
        # other's finished client under a MATCHING key and reuses it — which is
        # exactly the win. Lazily created so a backend built off-loop is safe.
        self._client_lock: asyncio.Lock | None = None
        # Plugin-identity digest of the currently-live warm client, and a map of
        # plugin_digest -> materialized per-run skills dir (fix/claude-sdk-warm-
        # client-skills). A skill run now REUSES the warm subprocess instead of
        # re-spawning, so the materialized dir it was connected with must outlive
        # the per-run finally — the subprocess holds that path from its first
        # connect(). The dir is keyed on the stable plugin IDENTITY (sorted skill
        # names + bundled flag), never the throwaway mkdtemp PATH, so two turns
        # with the same skills find the same cached dir. Ownership: the dir is
        # removed only when its warm client is evicted (_get_or_create_client) or
        # on cleanup() — NOT by a normal per-run finally.
        self._client_plugin_digest: str = ""
        self._skills_dir_by_digest: dict[str, Path] = {}

        # Per-run subprocess env injected by the pocket-specialist
        # runtime via ``attach_subprocess_env`` (PR #1222 R1 Blocker 1).
        # Merged into ``options_kwargs["env"]`` at spawn time so the
        # Claude Code subprocess inherits per-request tenancy values
        # (``POCKETPAW_WORKSPACE_ID`` / ``POCKETPAW_USER_ID`` /
        # ``POCKETPAW_INTERNAL_TOKEN``) without the runtime mutating
        # the parent's ``os.environ`` — which would race across
        # concurrent requests.
        self._extra_subprocess_env: dict[str, str] = {}

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

    def get_tool_policy(self) -> ToolPolicy:
        return self._policy

    def set_tool_policy(self, policy: ToolPolicy) -> None:
        self._policy = policy

    def attach_subprocess_env(self, env: dict[str, str]) -> None:
        """Merge ``env`` into the Claude Code subprocess env at next spawn.

        The pocket-specialist runtime calls this once per isolated run
        (PR #1222 R1 Blocker 1) to ship per-request tenancy values
        (``POCKETPAW_WORKSPACE_ID`` / ``POCKETPAW_USER_ID`` /
        ``POCKETPAW_INTERNAL_TOKEN``) into the subprocess without
        mutating the parent process's ``os.environ`` — which would
        race across concurrent requests sharing the same parent.

        ``run()`` merges this dict into ``options_kwargs["env"]`` after
        the LLM-provider env (``ANTHROPIC_API_KEY`` /
        ``CLAUDE_CODE_OAUTH_TOKEN``) so an attached value cannot
        accidentally clobber the auth key. Each call REPLACES the
        previous dict — an isolated backend instance is per-run, so
        the new run wants a fresh tenancy, not a merge with stale.
        """
        # Defensive copy so the caller can mutate their dict without
        # corrupting the backend's stash.
        self._extra_subprocess_env = dict(env)

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

    def _resolve_cwd(self) -> Path:
        """Resolve the agent's working directory for THIS run.

        NOTE: the per-tenant cwd jail + fail-closed live ONLY in this backend.
        Other backends (codex_cli, deep_agents, …) receive workspace tenancy via
        ``subprocess_env`` but NOT the cwd jail — a non-``claude_agent_sdk`` cloud
        agent would run in ``file_jail_path``. Cloud chat defaults to this
        backend; see ART-2's report for the residual non-claude gap.

        Defaults to ``settings.file_jail_path`` (the OSS / dedicated behavior,
        unchanged). When an EE ``pocketpaw.agent_extensions`` provider supplies
        an ``agent_cwd`` (the cloud product), its result wins — a
        per-workspace/session jail that keeps each tenant's file operations
        isolated instead of co-mingling in the shared home dir.

        A provider that RAISES (a multi-tenant cloud run with no resolvable
        workspace) is propagated, NOT swallowed: that fail-closed is the whole
        point — we must never silently fall back to ``~`` and let one tenant's
        files land on another's. Resolved per-run (not cached on the instance)
        so a single warm backend serving multiple sessions reads each session's
        own jail; the warm-client cache key folds in the resolved cwd (ART-2), so
        a changed cwd rebuilds the subprocess with its correct working directory.
        """
        from pocketpaw._registry import providers as _ext_providers

        for ext in _ext_providers("pocketpaw.agent_extensions"):
            resolver = getattr(ext, "agent_cwd", None)
            if resolver is None:
                continue
            resolved = resolver()  # may raise (fail-closed) — let it propagate
            if resolved:
                return Path(resolved)
        return self.settings.file_jail_path

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
            provider_name = type(provider).__name__
            try:
                built = provider.build_server()
            except Exception as exc:  # noqa: BLE001
                # 2026-05-28 (#FU-F): a stale editable install + dashboard
                # restart left CloudForesightMcpProvider unable to import its
                # SDK server, which silently swallowed the failure at DEBUG.
                # The diagnostic took 30+ minutes because the failure mode was
                # invisible. Promote to WARNING + include exception type +
                # module path so the operator sees it on startup.
                logger.warning(
                    "MCP server provider %s failed to build: %s: %s",
                    provider_name,
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )
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

        # Startup summary — operators check this on dashboard restart to confirm
        # their install picked up the expected entry-point set.
        if servers:
            logger.info("MCP servers registered: %s", ", ".join(sorted(servers)))
        else:
            logger.info("No MCP servers registered.")

        return servers

    def _collect_mcp_tool_ids(self) -> list[str]:
        """Collect the in-process MCP tool ids to add to the SDK allowlist.

        An MCP tool is only callable if its id is on the allowlist. This
        gathers the core ripple widget-spec ids plus every cloud
        ``pocketpaw.mcp_servers`` provider's ``tool_ids()`` (which includes
        the ``pocketpaw_pocket`` server's writable ``add_widget`` tool).

        Opt-in servers (the planner) are skipped unless the policy opts
        them in, mirroring the registration gate in ``_get_mcp_servers``.
        Tool ids follow the ``mcp__<server>__<tool>`` convention, so the
        server name is the segment between the first and second ``__``.
        """
        from pocketpaw._registry import providers as _ext_providers
        from pocketpaw.agents.sdk_mcp_widgets import WIDGET_TOOL_IDS

        ids: list[str] = list(WIDGET_TOOL_IDS)
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
                ids.append(tool_id)

        # External stdio/http MCP servers (``~/.pocketpaw/mcp_servers.json`` via
        # ``load_mcp_config``) are registered with the SDK in ``_get_mcp_servers``
        # but have no in-process ``tool_ids()`` provider — their tool names are
        # only known after the SDK connects to the server. Without an allowlist
        # entry the SDK refuses every call (e.g. a deployment's ``fabric`` server
        # exposing ``fabric_query`` / ``fabric_stats`` was registered yet
        # uncallable). Allow each enabled external server wholesale with a bare
        # ``mcp__<server>`` entry — the Claude Code permission convention that
        # admits all of a server's tools — gated by the same tool policy that
        # gates registration.
        try:
            from pocketpaw.mcp.config import load_mcp_config

            for cfg in load_mcp_config():
                if not cfg.enabled:
                    continue
                if cfg.name in self._BUILTIN_SEARCH_MCP_NAMES:
                    continue
                if not self._policy.is_mcp_server_allowed(cfg.name):
                    continue
                ids.append(f"mcp__{cfg.name}")
        except Exception as exc:  # noqa: BLE001
            logger.debug("External MCP server allowlist not added: %s", exc)

        return ids

    # Section markers that ``AgentPool.run`` appends to the system prompt
    # AFTER the authoritative behavioral instructions. Everything from the
    # first marker onward is per-turn and volatile (query-specific KB hits,
    # soul-memory recall, injected conversation history on a cold subprocess),
    # so it MUST NOT participate in the persistent-client cache key — otherwise
    # every turn would needlessly tear down and rebuild the subprocess. The
    # behavioral prefix BEFORE these markers carries the home-pocket backend
    # summary, which is exactly the mutable state we want the key to track.
    _VOLATILE_PROMPT_MARKERS = (
        "\n\n## Your Knowledge Base",
        "\n\n## Relevant Past Memories",
        "\n\n# Recent Conversation",
    )

    @classmethod
    def _behavior_prefix(cls, system_prompt: Any) -> str:
        """Return the stable behavioral prefix of ``system_prompt``.

        Strips the volatile per-turn tail (KB block, soul memories, injected
        history) so two turns that differ only in retrieved context hash to the
        same value. On Windows the SDK may pass ``system_prompt`` as a
        ``{type: "file", path: ...}`` dict — there is no inline text to key on,
        so fall back to the path (stable per connect) repr.
        """
        if isinstance(system_prompt, dict):
            return f"file:{system_prompt.get('path', '')}"
        if not isinstance(system_prompt, str):
            return ""
        cut = len(system_prompt)
        for marker in cls._VOLATILE_PROMPT_MARKERS:
            idx = system_prompt.find(marker)
            if idx != -1:
                cut = min(cut, idx)
        return system_prompt[:cut]

    @staticmethod
    def _plugin_digest(skill_names: frozenset[str], *, bundled: bool) -> str:
        """Stable digest of the agent's plugin IDENTITY for the cache key.

        Folds the per-entity skill subset (sorted ``skill_names``) and whether
        the bundled-skills plugin is loaded into one short hash. Empty when no
        skills and no bundled plugin participate, so a plain run's key is
        unchanged.

        CRITICAL: this digests the IDENTITY of the skills, never the
        materialized ``plugins=`` PATH. ``materialize_run_skills`` creates a
        fresh ``tempfile.mkdtemp`` dir on every run, so hashing the path would
        change the cache key every turn and defeat warm-client reuse entirely —
        the exact latency bug this fix removes. Two turns that request the same
        skills (and the same bundled state) MUST hash identically so the warm
        subprocess is reused; a changed skill set MUST hash differently so it
        rebuilds.
        """
        if not skill_names and not bundled:
            return ""
        payload = ("b1:" if bundled else "b0:") + ",".join(sorted(skill_names))
        return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:16]

    @classmethod
    def _client_cache_key(
        cls, options: Any, *, session_key: str | None = None, plugin_digest: str = ""
    ) -> str:
        """Persistent-client cache key: session + cwd + model + tools + a digest
        of the system prompt's stable behavioral prefix + the plugin-identity
        digest.

        The prefix digest is what makes a mid-session backend config change
        (configured:false -> configured:true, baked into the static home
        prompt) evict and rebuild the warm subprocess on the next turn, instead
        of staying frozen until a cold restart. The ``plugin_digest`` does the
        same for the agent's skill set (per-entity ``skill_names`` + bundled
        flag): folding it in lets a warm client tell a skill run apart from a
        non-skill one, so a skill run can REUSE the subprocess instead of
        re-spawning every turn. Empty ``plugin_digest`` (the default) leaves the
        key byte-for-byte identical to the pre-fix behavior for non-skill
        callers. Hashing keeps the key bounded regardless of prompt length.

        ``cwd`` (ART-2) is folded in so warm-client tenant isolation is
        STRUCTURAL, not an implicit consequence of the session_key<->cwd
        coupling: if cwd derivation ever changes to depend on something not in
        session_key, a stale warm subprocess can never be reused across two
        different working directories (i.e. two tenants). The SDK fixes cwd at
        connect() time, so a changed cwd MUST force a fresh subprocess.
        """
        prefix = cls._behavior_prefix(getattr(options, "system_prompt", None))
        prefix_digest = hashlib.sha256(prefix.encode("utf-8", "replace")).hexdigest()[:16]
        return (
            f"{session_key or ''}:"
            f"{getattr(options, 'cwd', '')}:"
            f"{getattr(options, 'model', '')}:"
            f"{sorted(getattr(options, 'allowed_tools', []) or [])}:"
            f"{prefix_digest}:"
            f"{plugin_digest}"
        )

    async def _get_or_create_client(
        self, options: Any, *, session_key: str | None = None, plugin_digest: str = ""
    ) -> Any:
        """Get or create a persistent ClaudeSDKClient.

        Reuses the existing subprocess if model, tools, session, the system
        prompt's behavioral prefix, **and the plugin-identity digest** haven't
        changed. Different sessions get a fresh subprocess so the CLI's internal
        conversation context doesn't leak between chats; a changed behavioral
        prefix (e.g. the home pocket's backend summary flipping to "configured"
        mid-session) or a changed skill set (``plugin_digest``) also forces a
        fresh subprocess so the new prompt / plugins actually take effect — the
        SDK applies both only at connect() time.

        When a stale client is evicted, its materialized per-run skills dir
        (tracked by the old ``plugin_digest``) is removed here: the subprocess
        that held that path is being torn down, so nothing references it
        anymore. Re-materializing the dir per turn does NOT work — the warm
        subprocess keeps the original path from its first connect — so the dir
        is cached per digest and only dropped on eviction or cleanup().
        """
        import time

        # Serialize the whole reuse-or-connect section so a concurrent prewarm +
        # first run (feat/claude-sdk-prewarm) cannot both create / evict the
        # client across the ``connect()`` await. Whichever wins the lock first
        # connects; the other then re-reads ``_client`` / ``_client_options_key``
        # under the SAME key and reuses it. Lazily created on the running loop.
        if self._client_lock is None:
            self._client_lock = asyncio.Lock()

        key = self._client_cache_key(options, session_key=session_key, plugin_digest=plugin_digest)

        async with self._client_lock:
            # Re-check INSIDE the lock: a prewarm (or sibling) may have connected
            # a matching client while we awaited the lock — reuse it, don't churn.
            if self._client is not None and self._client_options_key == key:
                logger.debug("Reusing persistent client (key=%s)", key)
                return self._client

            # Disconnect stale client and drop the skills dir it was connected with.
            if self._client is not None:
                try:
                    await self._client.disconnect()
                except Exception as e:
                    logger.debug("Failed to disconnect Claude client: %s", e)
                self._client = None
                self._drop_skills_dir(self._client_plugin_digest)

            # Create and connect new client
            t0 = time.monotonic()
            self._client = self._ClaudeSDKClient(options=options)
            await self._client.connect()
            self._client_options_key = key
            self._client_plugin_digest = plugin_digest
            t1 = time.monotonic()
            logger.info("Persistent client connected in %.0fms (key=%s)", (t1 - t0) * 1000, key)
            return self._client

    def _drop_skills_dir(self, plugin_digest: str) -> None:
        """Remove the materialized per-run skills dir cached under
        ``plugin_digest`` (if any). Best-effort; never raises. Called when the
        warm client that referenced the dir is evicted or on cleanup()."""
        if not plugin_digest:
            return
        root = self._skills_dir_by_digest.pop(plugin_digest, None)
        if root is not None:
            from pocketpaw.skills import cleanup_run_skills

            cleanup_run_skills(root)

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
        # Sweep every materialized per-run skills dir adopted by a warm client.
        # Safe even when no client existed (the map is just empty).
        if self._skills_dir_by_digest:
            from pocketpaw.skills import cleanup_run_skills

            for root in self._skills_dir_by_digest.values():
                cleanup_run_skills(root)
            self._skills_dir_by_digest.clear()
        self._client_plugin_digest = ""

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

    async def _build_options(
        self,
        message: str,
        *,
        system_prompt: str | None,
        history: list[dict] | None,
        session_key: str | None,
        deny_mcp_tool_ids: frozenset[str],
        allow_sdk_tools: frozenset[str],
        allow_mcp_tool_ids: frozenset[str] | None,
        skill_names: frozenset[str],
        stderr_sink: list[str],
        session_handle: SessionHandle | None = None,
    ) -> _BuiltOptions:
        """Assemble the ``ClaudeAgentOptions`` a turn (or a prewarm) will run on.

        Extracted from ``run`` (feat/claude-sdk-prewarm) so ``prewarm`` can build
        the EXACT same options the first real turn will, and therefore compute
        the same ``_client_cache_key`` — model + tools + system-prompt behavioral
        prefix + ``plugin_digest``. If the keys diverged, the prewarmed warm
        client would be EVICTED on the first turn (a net loss: prewarm paid a
        connect the run then threw away), which is the whole hazard this
        extraction removes.

        Returns a ``_BuiltOptions`` carrying everything ``run``'s dispatch +
        finally still need: the ``options`` object, the raw ``options_kwargs``
        (the token-usage event reads ``model`` off it), the resolved ``llm`` (for
        error formatting), and the per-run skills-plugin lifecycle triple
        (``run_skills_root`` / ``skills_dir_adopted`` / ``plugin_digest``).

        Pure assembly — no ``yield``, no streaming, no client creation. Reads the
        warm-client state (``self._client_in_use`` / ``self._skills_dir_by_digest``)
        only to decide whether to reuse a cached materialized skills dir, exactly
        as the inline block did. ``stderr_sink`` is the caller's list that the
        ``stderr`` callback appends to (so ``run`` keeps capturing CLI stderr for
        diagnostics; ``prewarm`` passes a throwaway list).
        """
        import os

        run_skills_root: Path | None = None
        skills_dir_adopted = False
        plugin_digest = ""

        # Per-run working directory. OSS / dedicated → ``file_jail_path``; cloud
        # → a per-workspace/session jail (or a fail-closed raise when a cloud run
        # has no resolvable workspace). Resolved here so BOTH ``run`` and
        # ``prewarm`` warm the SAME cwd for a session — the warm-client cache key
        # already keys on ``session_key``, so a session change rebuilds the
        # subprocess with its own jail.
        resolved_cwd = self._resolve_cwd()

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
        is_non_anthropic = (
            llm.is_ollama
            or llm.is_openai_compatible
            or llm.is_gemini
            or llm.is_litellm
            or llm.is_openrouter
        )

        # Smart model routing — classify complexity to pick the model tier.
        # All messages go through the Claude Code CLI subprocess, which
        # handles conversation compaction automatically (PreCompact hook).
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

        # Native-resume session id (feat/session-supervisor SS-1). When set, the
        # CLI subprocess will be launched with ``resume=<id>`` and reloads that
        # session's transcript NATIVELY, so injecting Mongo ``history`` into the
        # prompt below would DUPLICATE the conversation. The whole point of the
        # slice is native continuity INSTEAD of history replay, so a resume turn
        # skips the injection. ``None`` (legacy / no handle) keeps every existing
        # cold-start run injecting history exactly as before.
        resume_session_id = session_handle.cli_session_id if session_handle is not None else None

        # Inject prior turns into the system prompt at connect time. The
        # persistent ClaudeSDKClient accumulates new turns natively after
        # connect, but a fresh subprocess (after eviction, restart, or
        # session switch) has empty native history — without this, those
        # cold-start runs lose all conversation context. Reused clients
        # keep the prompt set at first connect and ignore later option
        # changes, so there's no duplication on the warm path. Skipped on a
        # native-resume turn (the resumed session already carries its history).
        final_prompt = identity
        if history and not resume_session_id:
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
        # an OSS install). The cloud ``pocketpaw_pocket`` server carries
        # both read tools (get_pocket / list_pockets) and the writable
        # ``add_widget`` tool — they all flow through the loop below.
        allowed_tools.extend(self._collect_mcp_tool_ids())

        # Per-entity ADDITIVE allowlist (entity-rooms chunk ①). UNION the
        # entity's ``allowed_sdk_tools`` into the allowlist BEFORE the deny
        # subtraction below, so the precedence is
        # ``effective = (agent_tools ∪ allow) − deny``. Dedup-preserve order:
        # only append ids not already present. Empty for legacy / non-entity
        # runs, so this is a no-op there. The deny set (subtracted next) is
        # the hard cap — an id in BOTH allow and deny stays denied.
        if allow_sdk_tools:
            existing = set(allowed_tools)
            for tool_id in allow_sdk_tools:
                if tool_id not in existing:
                    allowed_tools.append(tool_id)
                    existing.add(tool_id)
            logger.info("Surface tool-allow: unioned %s into allowlist", sorted(allow_sdk_tools))

        # Per-surface MCP-tool deny set (threaded from the chat loop's
        # resolved ``SurfaceProfile``). Any denied id is subtracted from the
        # allowlist BEFORE the SDK launches, so the agent is physically
        # unable to call it. On the /sites svelte-create surface this forbids
        # the two ripple-create tools (``create_landing_site`` +
        # ``pocket_specialist__create``) so the agent CANNOT fall back to
        # building a rippleSpec landing page — prose-only "do not call the
        # ripple tool" routing was proven to fail. Empty for every other
        # surface (a no-op), so ``create_svelte_site`` / ``publish`` /
        # ``pocket_specialist__edit`` and the ripple-engine / refine /
        # non-sites flows are untouched. This is the typed replacement for
        # the old prompt-sniffing ``engine="svelte"`` marker gate.
        if deny_mcp_tool_ids:
            before_count = len(allowed_tools)
            allowed_tools = [t for t in allowed_tools if t not in deny_mcp_tool_ids]
            if len(allowed_tools) < before_count:
                logger.info(
                    "Surface tool-deny: excluded %s from allowlist",
                    sorted(deny_mcp_tool_ids),
                )

        # Per-MODE restrictive MCP allow-list (distinct from the additive
        # ``allow_sdk_tools`` above). ``None`` keeps every MCP tool (broad
        # surfaces like /chat). When set, keep only MCP tools that are in the
        # mode's set, in the pocket-creation grant, a ripple widget tool, OR
        # from an always-allowed server (connectors + pocket lifecycle).
        # Built-in SDK tools (Read/Write/Bash/...) are NEVER filtered here —
        # only ``mcp__*`` ids — so scoping a mode can't strip core tools.
        # Applied AFTER deny so a denied id can't sneak back via the grant.
        if allow_mcp_tool_ids is not None:
            from pocketpaw.agents.sdk_mcp_widgets import WIDGET_TOOL_IDS

            grant = allow_mcp_tool_ids | POCKET_CREATION_GRANT | frozenset(WIDGET_TOOL_IDS)
            before_count = len(allowed_tools)
            allowed_tools = [
                t
                for t in allowed_tools
                if not t.startswith("mcp__")
                or t in grant
                or _mcp_server_of(t) in ALWAYS_ALLOWED_MCP_SERVERS
            ]
            if len(allowed_tools) < before_count:
                logger.info(
                    "Mode MCP-allow: scoped to %s (+ general grant)",
                    sorted(allow_mcp_tool_ids),
                )

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

        # ``setting_sources=[]`` keeps the agent on its OWN persona.
        # PocketPaw is not Claude Code: we pass a custom ``system_prompt``
        # string (never the ``claude_code`` preset), and an empty
        # setting-source list stops the SDK from injecting CLAUDE.md,
        # output styles, or filesystem settings as context. The repo
        # CLAUDE.md literally opens with "guidance to Claude Code
        # (claude.ai/code)" — loading it bled that identity into the
        # agent. Hooks, MCP servers, allowed_tools and permissions are
        # all passed explicitly below, so none of them depend on
        # setting sources. See
        # https://code.claude.com/docs/en/agent-sdk/modifying-system-prompts
        options_kwargs = {
            "system_prompt": system_prompt_arg,
            "allowed_tools": allowed_tools,
            "setting_sources": [],
            "hooks": hooks,
            "cwd": str(resolved_cwd),
            "max_turns": self.settings.claude_sdk_max_turns or None,
        }

        # Load PocketPaw's bundled skills as a Claude Code *local plugin*.
        # ``setting_sources=[]`` above disables the SDK's ~/.claude/skills
        # discovery, so the boot-time ~/.claude/skills mirror is invisible
        # to this backend and a local plugin is the ONLY way the bundled
        # skills reach it. Persona isolation is preserved — a plugin loads
        # only its own ``skills/`` directory, never the rest of ~/.claude
        # (CLAUDE.md, output styles). Empirically verified 2026-06-03: the
        # ``skills=`` option is also gated by setting_sources, but
        # ``plugins=`` is not. Toggle via ``sdk_load_bundled_skills``.
        bundled_loaded = False
        if self.settings.sdk_load_bundled_skills:
            from pocketpaw.bundled_skills import bundled_skills_plugin_dir

            plugin_dir = bundled_skills_plugin_dir()
            if plugin_dir is not None:
                options_kwargs["plugins"] = [{"type": "local", "path": str(plugin_dir)}]
                bundled_loaded = True
                logger.info("SDK: loading bundled-skills plugin from %s", plugin_dir)

        # Plugin-identity digest (fix/claude-sdk-warm-client-skills): folds
        # the requested skill set + the bundled flag into the cache key so a
        # skill run can REUSE the warm subprocess instead of re-spawning.
        # Keyed on identity, never the mkdtemp path (see _plugin_digest).
        plugin_digest = self._plugin_digest(skill_names, bundled=bundled_loaded)

        # Per-entity skill subset (entity-rooms A2). Materialize ONLY the
        # named skills into a local plugin and append it to the ``plugins=``
        # list (creating the list if the bundled plugin above was off). It
        # coexists with the bundled entry. Empty ``skill_names`` is a no-op.
        #
        # The warm client keeps whatever ``plugins=`` path it was connected
        # with from its first connect(), so the materialized dir must be
        # STABLE per plugin_digest across turns. When this run will reuse the
        # warm client and a dir for this digest already exists, reuse it
        # rather than re-materializing — the live subprocess already points
        # at it. Otherwise materialize fresh; cache + adopt it on the warm
        # path (cleaned on eviction/cleanup), or leave it owned by this run
        # on the stateless path (the per-run finally removes it).
        if skill_names:
            from pocketpaw.skills import materialize_run_skills

            will_reuse_warm = not self._client_in_use
            cached_dir = self._skills_dir_by_digest.get(plugin_digest)
            if will_reuse_warm and cached_dir is not None and cached_dir.exists():
                run_skills_root = cached_dir
                skills_dir_adopted = True
                logger.info(
                    "SDK: reusing cached per-run skill plugin (%d requested) at %s",
                    len(skill_names),
                    run_skills_root,
                )
            else:
                run_skills_root = materialize_run_skills(skill_names, run_id=session_key)
                if run_skills_root is not None and will_reuse_warm:
                    self._skills_dir_by_digest[plugin_digest] = run_skills_root
                    skills_dir_adopted = True

            if run_skills_root is not None:
                options_kwargs.setdefault("plugins", [])
                options_kwargs["plugins"].append({"type": "local", "path": str(run_skills_root)})
                if not skills_dir_adopted:
                    logger.info(
                        "SDK: loading per-run skill plugin (%d requested) from %s "
                        "(stateless — dir owned by this run)",
                        len(skill_names),
                        run_skills_root,
                    )

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
        # Merge per-run subprocess env (PR #1222 R1 Blocker 1) —
        # e.g. the pocket-specialist runtime attaches
        # ``POCKETPAW_WORKSPACE_ID`` / ``POCKETPAW_USER_ID`` /
        # ``POCKETPAW_INTERNAL_TOKEN`` here instead of writing to
        # the parent process's ``os.environ``. LLM-auth env wins:
        # we lay extras DOWN FIRST so anything the caller attaches
        # cannot accidentally clobber the auth key. An isolated
        # backend has its own ``_extra_subprocess_env`` so the
        # tenancy of one request cannot leak into another.
        if self._extra_subprocess_env:
            merged_env = dict(self._extra_subprocess_env)
            if sdk_env:
                merged_env.update(sdk_env)
            sdk_env = merged_env
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
            resolved_cwd,
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
            stderr_sink.append(line)
            logger.debug("Claude CLI stderr: %s", line)

        options_kwargs["stderr"] = _on_stderr

        # Native-resume (feat/session-supervisor SS-1). When the caller threads a
        # ``session_handle`` carrying a ``cli_session_id``, set the SDK's
        # ``resume`` field so the freshly-launched CLI subprocess loads that
        # session's transcript natively (the ``ClaudeAgentOptions.resume: str |
        # None`` field). ``run`` routes a resume-bearing turn down the stateless
        # ``query()`` path precisely so this fresh-launch option is honored (the
        # warm client applies options only at first ``connect()``). Absent /
        # ``None`` leaves ``resume`` unset — the unchanged legacy path.
        if resume_session_id:
            options_kwargs["resume"] = resume_session_id

        # Create options (after all kwargs are set, including model)
        options = self._ClaudeAgentOptions(**options_kwargs)

        return _BuiltOptions(
            options=options,
            options_kwargs=options_kwargs,
            llm=llm,
            run_skills_root=run_skills_root,
            skills_dir_adopted=skills_dir_adopted,
            plugin_digest=plugin_digest,
        )

    async def prewarm(
        self,
        *,
        session_key: str,
        system_prompt: str | None = None,
        history: list[dict] | None = None,
        deny_mcp_tool_ids: frozenset[str] = frozenset(),
        allow_sdk_tools: frozenset[str] = frozenset(),
        allow_mcp_tool_ids: frozenset[str] | None = None,
        skill_names: frozenset[str] = frozenset(),
    ) -> None:
        """Eagerly ``connect()`` the warm CLI subprocess for a session before its
        first turn, so the first real ``run`` reuses it instead of paying the
        ~12s cold ``connect()`` (feat/claude-sdk-prewarm).

        Builds the SAME options the first turn will (via ``_build_options``) and
        hands them to ``_get_or_create_client`` with the SAME ``session_key`` +
        ``plugin_digest`` — so the cache key matches and the first turn finds the
        client already live. If the keys diverged the first turn would EVICT the
        prewarmed client (a net loss), which is why the caller must prewarm with
        the same model/tools/prefix/skills the first turn will use.

        FIRE-AND-FORGET, never-break-a-turn semantics:
          * ALL exceptions are logged and SWALLOWED — a failed prewarm must never
            propagate to (or poison) a later turn. On failure the half-built
            client is torn down and the lease released, so the next ``run`` starts
            clean and simply pays the cold connect it would have paid anyway (no
            regression).
          * If a run is already active (``_client_in_use``) prewarm is a NO-OP —
            it must not contend for the lease or disturb an in-flight stream.
          * If the SDK / CLI is unavailable it is a no-op (nothing to warm).

        The skills-dir lifecycle is identical to ``run``'s warm path: when
        ``skill_names`` is non-empty ``_build_options`` materializes + adopts the
        plugin dir into ``self._skills_dir_by_digest`` keyed on ``plugin_digest``,
        and the warm client created here owns it until eviction / ``cleanup()``.
        On a prewarm failure that adopted dir is dropped so it doesn't leak.
        """
        if not self._sdk_available or not self._cli_available:
            return
        # A run holds the lease — never contend. The in-flight turn will create
        # (or already created) the warm client itself.
        if self._client_in_use:
            logger.debug("prewarm skipped: a run is active (_client_in_use)")
            return

        adopted_digest = ""
        try:
            built = await self._build_options(
                "",  # no real message yet — safe only when the model is
                # message-independent (smart routing OFF). The trigger gates on
                # this; see prewarm_session in run_core.
                system_prompt=system_prompt,
                history=history,
                session_key=session_key,
                deny_mcp_tool_ids=deny_mcp_tool_ids,
                allow_sdk_tools=allow_sdk_tools,
                allow_mcp_tool_ids=allow_mcp_tool_ids,
                skill_names=skill_names,
                stderr_sink=[],
            )
            # Remember the digest we may have adopted a dir under, so a failed
            # connect can release it instead of leaking.
            if built.skills_dir_adopted:
                adopted_digest = built.plugin_digest
            await self._get_or_create_client(
                built.options,
                session_key=session_key,
                plugin_digest=built.plugin_digest,
            )
            logger.info(
                "Prewarmed Claude client for session_key=%s (skills=%d)",
                session_key,
                len(skill_names),
            )
        except Exception as exc:  # noqa: BLE001 — prewarm must NEVER raise
            logger.warning("Prewarm failed (swallowed, turn unaffected): %s", exc)
            # Tear down any half-built client so the next run starts on a clean
            # slate (and just pays the cold connect). CRITICAL: prewarm never
            # acquired the ``_client_in_use`` lease, so it must NEVER clear it —
            # a run firing concurrently may legitimately own it, and stealing it
            # would corrupt that run. Do the teardown UNDER the client lock and
            # only when no run holds the lease, so we can't disconnect a client a
            # sibling run is actively using.
            if self._client_lock is None:
                self._client_lock = asyncio.Lock()
            async with self._client_lock:
                if not self._client_in_use:
                    try:
                        if self._client is not None:
                            await self._client.disconnect()
                    except Exception as disc_exc:  # noqa: BLE001
                        logger.debug("prewarm cleanup disconnect error (ignored): %s", disc_exc)
                    self._client = None
                    self._client_options_key = None
                    # If _build_options adopted a materialized skills dir for this
                    # digest but no live client now references it, drop it so it
                    # doesn't leak.
                    if adopted_digest:
                        self._drop_skills_dir(adopted_digest)
                        self._client_plugin_digest = ""

    async def run(
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list[dict] | None = None,
        session_key: str | None = None,
        deny_mcp_tool_ids: frozenset[str] = frozenset(),
        allow_sdk_tools: frozenset[str] = frozenset(),
        allow_mcp_tool_ids: frozenset[str] | None = None,
        skill_names: frozenset[str] = frozenset(),
        session_handle: SessionHandle | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Process a message through Claude Agent SDK with streaming.

        Yields AgentEvent objects as the agent responds.

        ``session_handle`` (feat/session-supervisor SS-1) carries native-resume
        identity. When it holds a non-None ``cli_session_id``, the SDK options
        get ``resume=<cli_session_id>`` (so the CLI subprocess resumes that
        on-disk session natively instead of replaying Mongo history) and THIS
        run is routed down the FRESH stateless ``query()`` launch path — never
        the warm persistent client, whose options freeze at first ``connect()``
        and whose cache key omits ``resume`` (so a reused warm client would
        silently ignore a fresh ``resume``). The per-turn ``system_prompt`` is
        still passed on every turn, so a resumed session honors a rebuilt
        prompt. When a handle is present, the SDK's turn-1 init/system message
        ``session_id`` is extracted and surfaced once as a ``session_id``
        AgentEvent (for the controller to persist — SS-3). ``cli_session_id is
        None`` / no handle = the UNCHANGED legacy warm-client path. The
        ``session_store`` field is opaque here (SS-2 owns it).

        ``deny_mcp_tool_ids`` is a per-surface MCP-tool deny set threaded down
        from the chat loop (resolved from the request's ``SurfaceProfile``).
        Any id in it is subtracted from ``allowed_tools`` before the SDK
        launches, so the agent is physically unable to call those tools. Empty
        by default (a no-op for legacy / non-/sites runs); non-empty only on the
        /sites svelte-create surface, where it forbids the two ripple-create
        tools so the agent cannot fall back to a rippleSpec landing page. This
        is the typed replacement for the deleted prompt-sniffing gate.

        ``allow_sdk_tools`` is the per-entity ADDITIVE SDK-tool allowlist
        (entity-rooms chunk ①), resolved from the entity pocket's
        ``surface_profile.allowed_sdk_tools``. It is UNIONed into
        ``allowed_tools`` BEFORE the deny subtraction — precedence
        ``effective = (agent_tools ∪ allow) − deny`` (the deny is the hard cap,
        so an allow can never re-enable a denied id). Empty by default (a no-op
        for legacy / non-entity runs).

        ``skill_names`` is the per-entity skill subset (entity-rooms A2), resolved
        from the entity pocket's ``surface_profile.skill_names``. When non-empty,
        the named skills are MATERIALIZED into a throwaway local-plugin directory
        and appended to the SDK ``plugins=`` list, so the agent sees ONLY those
        skills (coexisting with the bundled-skills plugin when that is enabled).
        ``setting_sources=[]`` disables both filesystem discovery and the SDK
        ``skills=`` option, so a local plugin is the only working channel — the
        same mechanism the bundled skills already use. CRITICAL: the persistent
        ("warm") client applies its options only at first ``connect()`` and the
        cache key does NOT include ``plugins=``, so a warm client connected
        WITHOUT these skills would silently ignore them. So when ``skill_names``
        is non-empty we BYPASS the warm client and run on a fresh stateless query
        whose options carry the materialized plugin. The temp dir is removed in a
        ``finally`` after the stream drains. Empty by default (a no-op).
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

        # Per-run materialized-skills plugin dir (entity-rooms A2). Declared
        # above the try so the finally can always clean it up, even if an
        # exception fires before/after materialization. None on every run that
        # doesn't pass a non-empty ``skill_names``.
        run_skills_root: Path | None = None

        # fix/claude-sdk-warm-client-skills: ``skills_dir_adopted`` is True when
        # ``run_skills_root`` was handed to (or reused by) a WARM client — its
        # lifetime then belongs to ``self._skills_dir_by_digest`` and the
        # per-run finally must NOT rmtree it (the live subprocess still holds the
        # path). It stays False on the genuine stateless-fallback path, where the
        # dir is this run's alone and the finally is the only thing that removes
        # it. ``plugin_digest`` is the plugin-identity hash threaded into the
        # cache key + the dir cache.
        skills_dir_adopted = False
        plugin_digest = ""

        # Ownership flag — True only if THIS run acquired the shared
        # _client_in_use lease. Declared above the try/except so it is always
        # in scope in the except handler (an exception can fire before the
        # dispatch block runs). Both the finally block and the except handler
        # gate the lease clear and the persistent-client teardown on this so a
        # non-owning run (stateless fallback, or a failure before acquisition)
        # can never release a sibling's lease or destroy its subprocess.
        acquired_lease = False
        # Resolved LLM client — bound by ``_build_options`` below. Declared above
        # the try (feat/claude-sdk-prewarm) so the ``except`` handler's
        # ``llm.format_api_error`` call is safe even when ``_build_options`` itself
        # raises before returning (e.g. the provider resolves but options
        # construction fails); the handler guards ``llm is None`` for that case.
        llm: Any = None
        try:
            # Assemble the SDK options (feat/claude-sdk-prewarm). The whole
            # block that built ``options_kwargs`` -> ``options`` (LLM resolve,
            # model routing, system-prompt assembly, tool allow/deny, bundled +
            # per-run skills materialization, subprocess env, MCP wiring) now
            # lives in the shared ``_build_options`` helper so ``prewarm`` can
            # build the IDENTICAL options the first turn will — same cache key,
            # so a prewarmed warm client is REUSED here, not evicted. The
            # returned triple (run_skills_root / skills_dir_adopted /
            # plugin_digest) drives the dispatch + finally exactly as before; the
            # stderr sink is this run's ``_stderr_lines`` so CLI diagnostics still
            # flow to the error handlers below.
            _built = await self._build_options(
                message,
                system_prompt=system_prompt,
                history=history,
                session_key=session_key,
                deny_mcp_tool_ids=deny_mcp_tool_ids,
                allow_sdk_tools=allow_sdk_tools,
                allow_mcp_tool_ids=allow_mcp_tool_ids,
                skill_names=skill_names,
                session_handle=session_handle,
                stderr_sink=_stderr_lines,
            )
            options = _built.options
            options_kwargs = _built.options_kwargs
            llm = _built.llm
            run_skills_root = _built.run_skills_root
            skills_dir_adopted = _built.skills_dir_adopted
            plugin_digest = _built.plugin_digest

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
            # Native-resume runs (feat/session-supervisor SS-1) MUST take the
            # fresh stateless ``query()`` launch path, never the warm persistent
            # client: the warm client applies its options (incl. ``resume``) only
            # at first ``connect()``, and ``_client_cache_key`` does NOT fold in
            # ``resume`` — so a reused warm client would silently ignore the fresh
            # ``resume`` and continue its OWN in-memory conversation instead of
            # the requested on-disk session. The stateless ``query()`` spawns a
            # fresh subprocess per call that honors ``options.resume`` directly.
            _resume_active = (
                session_handle is not None and session_handle.cli_session_id is not None
            )
            # fix/claude-sdk-warm-client-skills: the warm-client bypass for skill
            # runs is REMOVED. ``_client_cache_key`` now folds in
            # ``plugin_digest`` (the skill-identity hash), so a warm client can
            # distinguish a skill run from a non-skill one — a same-skill turn
            # reuses the subprocess and a changed skill set rebuilds it. The
            # materialized plugin dir was cached + adopted above so the warm
            # subprocess keeps a valid path across turns. Fallbacks to the
            # stateless path: the original concurrency guard (a sibling run holds
            # the lease, ``_client_in_use``) OR a native-resume turn.
            if not self._client_in_use and not _resume_active:
                try:
                    self._client_in_use = True
                    acquired_lease = True
                    _persistent_client = await self._get_or_create_client(
                        options, session_key=session_key, plugin_digest=plugin_digest
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
                    # No warm client adopted the skills dir we cached for this
                    # digest (connect failed before/at adoption). The stateless
                    # query below uses ``options`` (which already point at the
                    # dir) for THIS run only, so transfer ownership back to the
                    # per-run finally: drop it from the digest cache and clear the
                    # adopted flag so the finally cleans it up exactly once.
                    if skills_dir_adopted:
                        self._skills_dir_by_digest.pop(plugin_digest, None)
                        self._client_plugin_digest = ""
                        skills_dir_adopted = False

            if event_stream is None:
                logger.info("Starting stateless query (reason: _client_in_use was True)")
                # ``_build_options`` already baked Mongo history into the system
                # prompt inside ``options``, so the stateless path uses the same
                # options as the persistent path — no separate prompt swap needed.
                event_stream = self._resilient_query(prompt=message, options=options)

            # State tracking for StreamEvent deduplication
            _streamed_via_events = False
            _announced_tools: set[str] = set()
            _event_count = 0
            _saw_result = False  # Track if ResultMessage was consumed
            # feat/session-supervisor SS-1: emit the native session id at most
            # once per run (from the SDK's turn-1 init/system message). Gated on
            # an opted-in ``session_handle`` so the legacy stream is byte-identical.
            _session_id_emitted = False

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

                    # ========== SystemMessage - metadata ==========
                    if self._SystemMessage and isinstance(event, self._SystemMessage):
                        subtype = getattr(event, "subtype", "")
                        # feat/session-supervisor SS-1: the SDK's init/system
                        # message carries the native ``session_id`` in its
                        # ``data`` dict. When the caller opted into a
                        # ``session_handle``, capture it on turn 1 and surface it
                        # ONCE as a ``session_id`` AgentEvent (mirroring the
                        # ``token_usage`` metadata event) so the controller can
                        # persist it for a later ``resume`` turn (SS-3). Gated on
                        # the handle so the legacy stream stays byte-identical.
                        if session_handle is not None and not _session_id_emitted:
                            _data = getattr(event, "data", None)
                            _sid = _data.get("session_id") if isinstance(_data, dict) else None
                            if _sid:
                                _session_id_emitted = True
                                yield AgentEvent(
                                    type="session_id",
                                    content="",
                                    metadata={
                                        "session_id": _sid,
                                        "backend": "claude_agent_sdk",
                                    },
                                )
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
                            # MCG-11 — read prompt-cache effectiveness off the
                            # SDK usage via the universal helper so the margin
                            # from the byte-stable cached prefix (site/pocket-gen)
                            # is MEASURABLE: hit-rate + est. input-token-equivalents
                            # saved, surfaced to metering alongside the raw counts.
                            from pocketpaw.llm.caching import report_savings

                            savings = report_savings(usage)
                            if savings.cache_read_tokens or savings.cache_write_tokens:
                                logger.info(
                                    "[claude_sdk] prompt-cache: read=%d write=%d "
                                    "hit_rate=%.1f%% est_saved=%.0f input-tok-equiv",
                                    savings.cache_read_tokens,
                                    savings.cache_write_tokens,
                                    savings.hit_rate * 100,
                                    savings.est_tokens_saved,
                                )
                            yield AgentEvent(
                                type="token_usage",
                                content="",
                                metadata={
                                    "input_tokens": usage.get("input_tokens", 0),
                                    "output_tokens": usage.get("output_tokens", 0),
                                    "cached_input_tokens": usage.get("cache_read_input_tokens", 0)
                                    + usage.get("cache_creation_input_tokens", 0),
                                    # Structured cache telemetry (MCG-11) — metering
                                    # can attribute the margin without re-parsing.
                                    "cache_read_tokens": savings.cache_read_tokens,
                                    "cache_write_tokens": savings.cache_write_tokens,
                                    "cache_hit_rate": savings.hit_rate,
                                    "cache_est_tokens_saved": savings.est_tokens_saved,
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
                        # Preserve native-resume identity across the crash retry
                        # (feat/session-supervisor SS-1) so a resumed turn does
                        # not silently restart a fresh session after a Bun crash.
                        session_handle=session_handle,
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
            elif llm is not None:
                yield AgentEvent(
                    type="error",
                    content=llm.format_api_error(e, stderr=stderr_text),
                )
            else:
                # ``_build_options`` raised before binding ``llm`` — fall back to
                # a plain message so the error still surfaces (no UnboundLocalError).
                yield AgentEvent(
                    type="error",
                    content=f"❌ Claude Agent SDK error: {error_msg}",
                )
        finally:
            # Remove the per-run materialized-skills plugin dir (entity-rooms
            # A2) ONLY when this run owns it — i.e. the genuine stateless-
            # fallback case where no warm client adopted the dir
            # (fix/claude-sdk-warm-client-skills). When ``skills_dir_adopted`` is
            # True the dir belongs to ``self._skills_dir_by_digest`` and a LIVE
            # warm subprocess still references its path; deleting it here would
            # leave the next reuse pointing at a missing dir. Adopted dirs are
            # instead removed on client eviction (_get_or_create_client) or
            # cleanup(). Best-effort; never raises.
            if run_skills_root is not None and not skills_dir_adopted:
                from pocketpaw.skills import cleanup_run_skills

                cleanup_run_skills(run_skills_root)

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
            # Base (OSS/default) working dir only. The ACTUAL per-run cwd is
            # resolved each turn by ``_resolve_cwd`` — in cloud it's a per-tenant
            # jail, not this base — so labelling it ``base_cwd`` keeps status
            # honest (and avoids resolving here, which would fail closed off-run).
            "base_cwd": str(self._cwd),
            "features": ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebSearch", "WebFetch"]
            if ready
            else [],
        }


# Backward-compat aliases
ClaudeAgentSDK = ClaudeSDKBackend
ClaudeAgentSDKWrapper = ClaudeSDKBackend
