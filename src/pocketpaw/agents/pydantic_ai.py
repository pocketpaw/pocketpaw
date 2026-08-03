"""Pydantic AI agent backend — in-process, dispatch-only.

Created 2026-07-29 (feat/pydantic-ai-backend). Backend #9 in
``_BACKEND_REGISTRY``. Implements the ``AgentBackend`` protocol on top of
``pydantic-ai-slim`` with an OpenAI-compatible model client pointed at the
self-hosted LiteLLM proxy.

Design source: ``docs/design/drafts/2026-07-29-pydantic-ai-agent-backend-prd.md``.

Why this exists: ``claude_agent_sdk`` spawns a Claude Code CLI Node subprocess
per concurrent run (~300-500 MB RSS). At the 300-400 concurrent-user target
that is ~75 GB of agent RSS before any other cost. This backend runs the agent
loop IN-PROCESS, so per-run cost is roughly the conversation context and the
binding constraint moves from process memory to the event loop — which is
addressed by adding web processes rather than boxes.

**Dispatch-only.** This backend emits tool calls; it does NOT execute local
file or shell work. Execution happens in Daytona / WebContainers / Tauri, S3,
or MCP servers. That is what collapses per-run memory AND what removes the
tenant-jail requirement: the per-run cwd jail (``agent_jail.resolve_agent_cwd``)
exists only on the ``claude_sdk`` chain, and PocketPaw's own
``tools/builtin/{shell,filesystem}.py`` jail against a PROCESS-GLOBAL
``file_jail_path``, so any in-process backend granted local fs/shell tools
would share one jail across every tenant. ``_POCKET_BLOCKED_TOOLS`` below is the
mechanical expression of that constraint, not a preference.

Two failure modes this file is deliberately shaped around:

1. **Per-run cancellation, never instance state.** ``AgentPool`` caches ONE
   backend instance per agent and drives concurrent runs through it. The
   sibling ``deep_agents`` backend keeps a single ``self._stop_flag``, so one
   run's ``stop()`` truncates every concurrent run and each new run's entry
   reset un-stops the others — observed 2026-07-29 in the load-test rig, where
   33 of 49 concurrent runs returned a clean ``stream_end`` carrying no
   content. Here each run owns a private ``_RunHandle``; ``stop()`` signals the
   handles that are live AT THAT MOMENT and a run starting afterwards gets a
   fresh one. The property is pinned by
   ``test_a_new_run_does_not_resurrect_a_stopped_one``, which was mutation-checked
   against a faithful shared-flag reproduction — note that the obvious
   "N concurrent runs all produce content" test does NOT catch the bug, because
   no ``stop()`` lands between those runs.

2. **Sync tools cap the whole process.** One blocking tool function runs on
   anyio's bounded worker thread pool, so a single sync tool throttles every
   concurrent run in the process. ``build_pydantic_ai_tools`` asserts every
   bridged tool is a coroutine.

**Prompt caching DOES survive this path — measured, not assumed.** This was the
design's open question 2, and the answer is yes, at least for
``litellm:deepseek-v3.2``. Six turns sharing one ~4.4k-token system prefix,
2026-07-29:

===========  ==========  ================  ==========
turn         cache read  uncached input    hit rate
===========  ==========  ================  ==========
0 (cold)              0  (full prompt)             0%
1                 4,416                19          100%
2                 4,416             9,316           32%
3                 4,416                19          100%
4                 8,000               943           89%
5                 4,416                19          100%
===========  ==========  ================  ==========

So a large stable SYSTEM PROMPT is cached upstream and read back without us
doing anything: ``deep_agents`` earns its margin by patching Anthropic
``cache_control`` markers into the request, and this route needs no equivalent
hook. The first turn is always a cold write and one turn in six missed, so
measure a WARM window when comparing rather than reading turn 1 alone.

**It does NOT appear to cover tool schemas, which is the load-bearing caveat.**
Same model, same day, with a SHORT system prompt and the full 49-tool bridged
surface attached: 13,509 input tokens and ``cache_read_tokens`` of **zero** on
every turn, repeatedly. Drop the tools and a 4.4k-token system prompt caches at
100%. So the cacheable thing here is the text prefix, not the tool definitions.

What that means for the ``/sites`` A/B: the pocket specialist's prompt is a
~12-17k-token design-rules block, which is exactly the shape that DOES cache, so
the target workload is likely fine. A chat agent carrying a small prompt and a
big tool surface is the case that will not cache and should be measured on its
own rather than assumed from the specialist's numbers.

The counts come from ``RunUsage``, which documents ``input_tokens`` as the
INCLUSIVE total with ``cache_read_tokens`` / ``cache_write_tokens`` as subsets
and normalizes providers (Anthropic, Bedrock) that report them disjointly — so
``_usage_event`` subtracts them back out to get the uncached remainder.

Verified live 2026-07-29 through a real LiteLLM proxy: a streamed turn, a tool
round-trip (tool actually executed, ``tool_use`` before ``tool_result``), and 8
concurrent runs on ONE cached instance with zero empty ``stream_end``.

Updated 2026-07-31 (a) — **dispatch-only is now enforced unconditionally.** The
paragraph above has always said this backend does not do local file or shell
work, but the code stripped those tools on POCKET SESSIONS ALONE, so an ordinary
chat turn was handed ``shell``, ``read_file``, ``write_file``, ``run_python``,
``install_package``, ``delegate_claude_code`` and the rest — 49 tools where 37
was the intent. On a backend whose whole point is that ONE process serves every
tenant, and whose builtin tools jail against a PROCESS-GLOBAL ``file_jail_path``,
that is a tenant reading the server. ``_LOCAL_MACHINE_TOOLS`` is now applied in
``_build_custom_tools`` — no surface, session type or deny set involved, because
the constraint does not vary and a per-surface control implies it might.

Updated 2026-07-31 (c) — **per-session transcript retention.** The cloud
persists conversation as ``[{role, content}]`` TEXT; tool calls and their
results are not stored at all. ``claude_agent_sdk`` does not care, because its
CLI subprocess holds the real transcript and the text is only a
restore-from-restart fallback (``load_history_for_scope`` says so in its
docstring). This backend had no equivalent, so every turn rebuilt from text and
dropped every tool result — including the ``pocket_id`` a site draft hands back,
which is why "publish it" on the following turn had nothing to publish.
``_session_messages`` keeps the real messages per ``session_key``, bounded by
session count AND a trailing message window; a miss degrades to the text
history rather than failing.

Updated 2026-07-31 (b) — ``run`` accepts the per-surface tool-gating kwargs
(``deny_mcp_tool_ids`` / ``allow_mcp_tool_ids`` / ``exclusive_mcp_tools``) and
HONOURS them; see ``_expand_tool_ids`` and ``_gate_mcp_toolsets``. Omitting them
crashed the run outright: ``AgentPool.run`` forwards each ONLY when a surface
sets it, so /chat (empty deny, no allow) worked while the first /sites turn died
with ``TypeError: run() got an unexpected keyword argument
'deny_mcp_tool_ids'``. Accepting them is only half the fix — they are a
surface's tool-REMOVAL controls, so swallowing them would have handed a
restricted surface the full tool set and reported success. The other six
non-Claude backends still carry the narrow signature and will crash the same way
on that surface.

Updated 2026-07-31 (d) — **deferred tool loading**, opt-in via
``POCKETPAW_PYDANTIC_AI_DEFER_MCP_TOOLS``. The caching caveat above is what
makes this worth doing: tool schemas do not cache, so an ungated surface pays
for every one of them on every single request. Measured on an ungated surface,
through the real ``OpenAIChatModel`` this backend builds:

===========  =================  ==============  ===============
surface      tools on the wire  schema bytes    tokens/request
===========  =================  ==============  ===============
today                      131         121,750           30,437
deferred                    35          23,320            5,830
===========  =================  ==============  ===============

pydantic-ai's own mechanism (``AbstractToolset.defer_loading`` plus the
``ToolSearch`` capability): undeferred tools ride the wire as usual, deferred
ones are dropped and reachable through a ``search_tools`` function. Chat
Completions has no native tool-search surface, which is the path that actually
drops them; on Anthropic or the OpenAI Responses API the provider drives
discovery instead, and Responses currently rejects the pairing pydantic-ai
sends (pydantic-ai#5938).

``pocketpaw_tool_search`` replaces the built-in ranking, which counts every
token equally wherever it lands and so answers "publish it" with five tools
that merely mention publishing. See ``_defer_mcp_toolsets`` for the two
boundaries this rides on — deferral happens AFTER surface gating, and only MCP
tools defer.

Updated 2026-08-01 (e) — **branch review**. Five things the sections above
claimed but did not deliver:

1. **Dispatch-only now fails closed.** ``_LOCAL_MACHINE_TOOLS`` was a denylist,
   so it covered what its author thought of and granted the rest. It missed
   ``discord_cli``, which spawns ``discli`` as a SUBPROCESS under the operator's
   credentials, and the whole class of tools that READ the shared process:
   ``error_log`` (process-global, tracebacks, scoped to no workspace),
   ``system_info`` (host CPU/RAM/disk and top processes by pid),
   ``config_doctor``, ``health_check``. The surface is built from
   ``_TENANT_SAFE_TOOLS`` now; anything unclassified is withheld and logged.
2. **The per-agent tool policy reaches this backend.** ``AgentPool._build``
   injected one into ``ClaudeSDKBackend`` alone, so a cloud agent that switched
   here silently ran under the process-wide policy — default profile ``full`` —
   and its ``mcp_servers_allow`` opt-ins were unreachable, which made the
   ``OPT_IN_MCP_SERVERS`` branch in the bridge dead code. The pool asks the
   signature now, so this is fixed for every backend at once.
3. **The ``anthropic`` provider shares the instance HTTP client.** ``_build_model``
   runs every turn while the agent cache returns the previous model, so a
   provider that builds its own client leaked one connection pool per turn
   (measured: 3 turns, 3 clients, none closed, ``stop()`` closed none). It also
   meant that branch ignored ``pydantic_ai_timeout`` and kept the 600s read
   deadline the setting exists to replace.
4. **In-process MCP results are scanned and capped** like bridged function
   tools. 97 of the tools skipped ``_scan_tool_output`` entirely, including the
   connector tools, which return data from external systems.
5. Smaller: the agent cache key uses ``_tools_version`` rather than
   ``id(_custom_tools)``, and deferral is skipped on a surface that already
   named its tools.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from collections.abc import AsyncIterator, Sequence
from typing import Any

from pocketpaw.agents.backend import _DEFAULT_IDENTITY, BackendInfo, Capability
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.config import Settings
from pocketpaw.tools.policy import ToolPolicy

logger = logging.getLogger(__name__)

# Providers whose wire format is OpenAI chat-completions. All of them are
# served by ``OpenAIChatModel`` + ``OpenAIProvider(base_url=...)``; only the
# base URL and key source differ.
_OPENAI_COMPATIBLE = frozenset({"litellm", "openai", "openai_compatible", "openrouter", "ollama"})

# Same gate as ``claude_sdk`` and ``deep_agents``: ``<pocket-scope>`` opens every
# pocket/site prompt. Retained for the prompt-shape signal it carries into the
# agent cache key; the shell/fs strip it used to perform is now unconditional
# (see below), because it was never really about pocket sessions.
_POCKET_SCOPE_SENTINEL = "<pocket-scope>"

# The name the model uses to pull the skills catalog when
# ``pydantic_ai_defer_skills`` is on. ``SkillsCapability`` requires an id once
# ``defer_loading`` is set, and it appears in the model's ``load_capability``
# call, so it is stable and readable rather than generated per run.
_SKILLS_CAPABILITY_ID = "pocketpaw-skills"

# Bounds on the per-session transcript cache (see ``_session_messages``).
# ``logfire.configure`` is process-global and one backend instance exists per
# agent, so the capability builder would otherwise reconfigure it per agent.
_LOGFIRE_CONFIGURED = False

_MAX_TRACKED_SESSIONS = 200
_MAX_SESSION_MESSAGES = 60

# Tools that act on THIS PROCESS'S MACHINE. Never built for this backend — not
# gated by surface, not by session type, not by a deny set.
#
# This backend SERVES: one process answers every tenant over an API. PocketPaw's
# ``tools/builtin/{shell,filesystem}.py`` jail against a PROCESS-GLOBAL
# ``file_jail_path``, and the per-run cwd jail (``agent_jail.resolve_agent_cwd``)
# exists only on the ``claude_sdk`` chain — so a tenant's ``read_file`` here
# reads the server, in a directory shared with every other tenant. The same
# argument retires ``install_package`` (pip-installs into the shared runtime),
# ``open_in_explorer`` (a GUI file browser on a headless box) and
# ``delegate_claude_code`` (spawns the full CLI subprocess whose ~300-500 MB RSS
# is the entire reason this backend exists) — and ``create_skill``, which writes
# SKILL.md into the process-global skills directory every tenant loads from.
#
# The module docstring has always said dispatch-only. Until 2026-07-31 the code
# enforced it on pocket sessions ALONE, so an ordinary chat turn was handed the
# lot. Surface deny sets are not the mechanism for this and never were: they
# vary per surface, and this constraint does not.
_LOCAL_MACHINE_TOOLS = frozenset(
    {
        "shell",
        "run_python",
        "install_package",
        "code_mode",
        "read_file",
        "write_file",
        "edit_file",
        "list_dir",
        "directory_tree",
        "open_in_explorer",
        "delegate_claude_code",
        "create_skill",
        # Spawns ``discli`` as a SUBPROCESS on this server, under the
        # OPERATOR's Discord credentials. Same argument as ``shell``; it was
        # missed because its name reads like an API client.
        "discord_cli",
    }
)

# Tools that READ this process or this host. The dispatch-only argument covers
# them for the same reason it covers ``read_file`` — one process serves every
# tenant — but the first pass only caught tools that WRITE or EXECUTE, so these
# stayed on every surface:
#
# * ``error_log`` reads ``~/.pocketpaw/health/errors.jsonl``, which is
#   process-global, append-only, carries tracebacks, and is scoped to no
#   workspace. One tenant asking "what went wrong?" reads every other tenant's
#   failures.
# * ``system_info`` reports host CPU / RAM / disk / network and the top
#   processes BY NAME AND PID.
# * ``config_doctor`` and ``health_check`` report the operator's configuration
#   and startup checks.
#
# None of them are per-tenant data, so none of them belong to a tenant's agent.
_HOST_STATE_TOOLS = frozenset({"error_log", "system_info", "config_doctor", "health_check"})

_WITHHELD_TOOLS = _LOCAL_MACHINE_TOOLS | _HOST_STATE_TOOLS

# Reviewed as safe for an agent on a SHARED process: external services, or
# PocketPaw state already scoped to the caller's tenant.
#
# This list is what makes the boundary fail CLOSED. A denylist grants anything
# added later by default, which is backwards for a security constraint — and
# not hypothetical: ``discord_cli`` sat in the granted set through a whole
# review because nobody had reason to look at it again. An unclassified tool is
# now WITHHELD and logged, and ``test_every_bridged_tool_is_classified`` fails
# until someone decides which side it belongs on.
_TENANT_SAFE_TOOLS = frozenset(
    {
        # pockets / widgets — tenant-scoped
        "add_widget",
        "remove_widget",
        "create_pocket",
        "start_flow",
        "run_step_pipeline",
        # memory + sessions — scoped by the caller's own session key
        "remember",
        "recall",
        "forget",
        "clear_session",
        "delete_session",
        "list_sessions",
        "new_session",
        "rename_session",
        "switch_session",
        # connectors — the tenant's own integrations
        "connector_actions",
        "connector_connect",
        "connector_execute",
        "connector_list",
        # outbound services and media
        "currency",
        "deliver_artifact",
        "delegate_to_a2a_agent",
        "image_generate",
        "ocr",
        "research",
        "search_stock_images",
        "speech_to_text",
        "text_to_speech",
        "translate",
        "url_extract",
        "weather",
        "web_search",
        "wiki",
        # Present only when Composio is NOT configured — with it on,
        # ``_COMPOSIO_OVERLAPPING_TOOL_NAMES`` drops them so each integration
        # has exactly one path. They are the tenant's own OAuth-scoped
        # integrations, same class as ``connector_*``. Missing them is how the
        # first version of this allowlist would have silently removed Gmail,
        # Calendar, Drive, Docs, Reddit and Spotify from every deployment that
        # does not run Composio: they never appeared on the machine the list
        # was written on. CI caught it.
        "calendar_create",
        "calendar_list",
        "calendar_prep",
        "docs_create",
        "docs_read",
        "docs_search",
        "drive_download",
        "drive_list",
        "drive_share",
        "drive_upload",
        "gmail_batch_modify",
        "gmail_create_label",
        "gmail_list_labels",
        "gmail_modify",
        "gmail_read",
        "gmail_search",
        "gmail_send",
        "gmail_trash",
        "reddit_read",
        "reddit_search",
        "reddit_trending",
        "spotify_now_playing",
        "spotify_playback",
        "spotify_playlist",
        "spotify_search",
        # Present only when a soul is active, and they REPLACE
        # ``remember`` / ``recall`` / ``forget`` when they appear. The soul
        # belongs to the agent, so its memory is the tenant's.
        "soul_context",
        "soul_core_memory",
        "soul_edit_core",
        "soul_evaluate",
        "soul_forget",
        "soul_recall",
        "soul_reload",
        "soul_remember",
        "soul_status",
    }
)

# -- per-surface tool gating -------------------------------------------------
#
# A ``SurfaceProfile``'s deny/allow sets are written in the Claude SDK's
# vocabulary, because that is the backend they were built for: MCP tools spelled
# ``mcp__<server>__<tool>`` and bare built-in names like ``Bash``. NEITHER
# spelling exists here — pydantic-ai's ``PrefixedToolset`` names an MCP tool
# ``<server>_<tool>``, and there are no SDK built-ins at all. Comparing the raw
# strings therefore matches nothing, which is the dangerous failure: a surface
# that removed shell access would run with the full tool set and report success.


def _normalize_tool_id(tool_id: str) -> str:
    """``mcp__srv__do_thing`` -> ``srv_do_thing``. Other spellings pass through."""
    if tool_id.startswith("mcp__"):
        return tool_id.removeprefix("mcp__").replace("__", "_")
    return tool_id


# A surface's tool id -> the bridged PocketPaw tool that is the SAME CAPABILITY
# under a different name. Without a row the deny matches nothing and the
# capability survives.
#
# There is deliberately NO row for ``Bash`` / ``Read`` / ``Write`` / ``Edit`` /
# ``Glob`` / ``Grep``. Those name local-machine work, which
# ``_LOCAL_MACHINE_TOOLS`` removes from every run — a per-surface row would be
# dead code implying a boundary that lives elsewhere.
# ``test_local_machine_tools_are_never_offered`` is what holds that line.
_SURFACE_TOOL_EQUIVALENTS: dict[str, frozenset[str]] = {
    # /sites svelte-create denies ``pocket_specialist__create`` so the agent
    # CANNOT fall back to building a rippleSpec landing page (claude_sdk:1865 —
    # "prose-only 'do not call the ripple tool' routing was proven to fail").
    # It reaches this backend as a bridged tool under the OSS name.
    "mcp__pocketpaw_pocket_specialist__create": frozenset({"create_pocket"}),
    # Local delegation is already gone with the rest of the local-machine
    # family; this is the REMOTE one, a network call to an external agent.
    "Agent": frozenset({"delegate_to_a2a_agent"}),
}


# Model-name substrings whose providers REJECT a request when an assistant
# message comes back without the reasoning field they produced. DeepSeek and
# Moonshot are the two that emit ``reasoning_content`` (see the field list in
# ``pydantic_ai/models/openai.py::_process_thinking``).
_REASONING_ECHO_REQUIRED = ("deepseek", "moonshot", "kimi")


def _reasoning_echo_model_class(model_name: str) -> type | None:
    """An ``OpenAIChatModel`` that never omits ``reasoning_content``, or ``None``.

    DeepSeek in thinking mode 400s the WHOLE request if any assistant message
    in the history lacks ``reasoning_content``::

        The `reasoning_content` in the thinking mode must be passed back to the API.

    pydantic-ai writes that field only when the ``ModelResponse`` carries a
    ``ThinkingPart``, so any assistant turn produced without one — most
    reliably the continuation after a deferred capability's
    ``load_capability`` — poisons every following request in the run.

    The fix is the empty string, and that is not a guess. Replaying one captured
    failing request against the proxy four ways:

    ====================================================  ======
    variant                                               status
    ====================================================  ======
    as captured                                              400
    ``reasoning_content`` stripped from every message        400
    empty ``reasoning_content`` where missing                200
    placeholder text where missing                           200
    ====================================================  ======

    Note the second row: leaving the field off entirely does NOT work, so this
    cannot be fixed by suppressing thinking. ``pydantic_ai_thinking=off`` was
    tried and the run still 400s.

    Applied by model name rather than to every OpenAI-compatible model, because
    an unknown ``reasoning_content`` on a provider that does not expect one is a
    new failure mode in exchange for nothing. Returns ``None`` for models that
    do not need it, and the caller falls back to the stock class.
    """
    name = (model_name or "").lower()
    if not any(tag in name for tag in _REASONING_ECHO_REQUIRED):
        return None
    try:
        from pydantic_ai.models.openai import OpenAIChatModel
    except ImportError:  # pragma: no cover - pydantic-ai always present here
        return None

    class _ReasoningEchoChatModel(OpenAIChatModel):
        """``_map_model_response`` is pydantic-ai's documented subclass hook."""

        def _map_model_response(self, message: Any) -> Any:
            param = super()._map_model_response(message)
            if (
                param is not None
                and param.get("role") == "assistant"
                and "reasoning_content" not in param
            ):
                param["reasoning_content"] = ""
            return param

    return _ReasoningEchoChatModel


_AGENTAPI_GATED_SURFACE = (
    "This surface controls which tools the agent may use, and the `agentapi` "
    "model cannot honour that.\n\n"
    "AgentAPI wraps a COMPLETE CLI agent. That agent plans and calls its own "
    "tools — Write, Bash, Edit — on this server, underneath PocketPaw. It never "
    "receives the tools this surface granted and never sees the ones it denied, "
    "so instead of building a site it writes a file to the server's disk and "
    "waits on a permission prompt in the terminal running `agentapi server`.\n\n"
    "`agentapi` is a text-only development model. Point this at a real one:\n"
    "  POCKETPAW_PYDANTIC_AI_MODEL=openrouter:<model>   (or litellm:<model>)\n\n"
    "To make the wrapped CLI genuinely text-only, restart it as:\n"
    "  agentapi server -- claude --permission-mode plan"
)


def _expand_tool_ids(tool_ids: frozenset[str]) -> frozenset[str]:
    """Translate a surface's tool ids into the names this backend uses."""
    out: set[str] = set()
    for raw in tool_ids:
        out.add(_normalize_tool_id(raw))
        out |= _SURFACE_TOOL_EQUIVALENTS.get(raw, frozenset())
    return frozenset(out)


# -- tool search (deferred loading) -----------------------------------------

_TOOL_TOKEN_RE = re.compile(r"[a-z0-9]+")

_TOOL_SEARCH_STOPWORDS = frozenset(
    """a an the it its this that these those my me mine you your i we our us for
    to of and or but on in at by with from as is are was were be been am do does
    did please can could would should will just now then here there what which
    who whom how why when where all any some more most other into up out over
    under again once no not only own same so than too very""".split()
)

# Our tool names are engineering nouns; users type product nouns. Mapping the
# second onto the first is the whole difference between the built-in algorithm
# and this one on the queries that matter — "publish it" and "make me a
# webpage" both miss upstream. Hand-tuned against the real 97-tool corpus, not
# learned, so it is a list to extend when a surface adds vocabulary.
_TOOL_SEARCH_SYNONYMS: dict[str, str] = {
    "website": "site",
    "webpage": "site",
    "web": "site",
    "page": "site",
    "landing": "site",
    "deploy": "publish",
    "ship": "publish",
    "live": "publish",
    "launch": "publish",
    "picture": "image",
    "photo": "image",
    "graphic": "image",
    "chart": "widget",
    "graph": "widget",
    "card": "widget",
    "integration": "connector",
    "gmail": "connector",
    "calendar": "connector",
    "colour": "color",
    "scheme": "palette",
    "theme": "palette",
    "brand": "palette",
    "font": "typography",
    "app": "pocket",
    "workspace": "pocket",
}


def _tool_search_tokens(text: str) -> set[str]:
    return set(_TOOL_TOKEN_RE.findall((text or "").lower()))


def _tool_search_terms(queries: Sequence[str]) -> set[str]:
    """Query tokens, minus stopwords, plus this product's synonyms."""
    terms: set[str] = set()
    for token in _tool_search_tokens(" ".join(queries)):
        if token in _TOOL_SEARCH_STOPWORDS or len(token) < 3:
            continue
        terms.add(token)
        synonym = _TOOL_SEARCH_SYNONYMS.get(token)
        if synonym:
            terms.add(synonym)
    return terms


def _tool_search_score(terms: set[str], name: str, description: str) -> int:
    """Weight a NAME match above a description match.

    The built-in algorithm counts one point per token wherever it lands, so
    every tool whose description happens to mention publishing outranks the
    tool actually called ``publish``. Measured: "publish it" does not surface
    ``sites_manager_publish`` in the top five at all.
    """
    name_tokens = _tool_search_tokens(name)
    description_tokens = _tool_search_tokens(description)
    total = 0
    for term in terms:
        if term in name_tokens:
            total += 3
        elif any(len(n) >= 4 and (term in n or n in term) for n in name_tokens):
            total += 2
        elif term in description_tokens:
            total += 1
    return total


def pocketpaw_tool_search(_ctx: Any, queries: Sequence[str], tools: Sequence[Any]) -> list[str]:
    """Rank deferred tools for a search query. A ``ToolSearchFunc``.

    Supplied to ``ToolSearch(strategy=...)`` in place of pydantic-ai's default
    keyword-overlap algorithm, which scores 8/12 on realistic phrasings against
    our corpus where this scores 11/12. The two failures it fixes are the ones
    that matter most: "publish it" (stopwords outvote the tool name) and "make
    me a webpage" (no product vocabulary, so it returns Daytona and Foresight
    tools).
    """
    terms = _tool_search_terms(queries)
    if not terms:
        return []
    scored: list[tuple[int, str]] = []
    for tool_def in tools:
        name = getattr(tool_def, "name", "")
        score = _tool_search_score(terms, name, getattr(tool_def, "description", "") or "")
        if score > 0:
            scored.append((score, name))
    # Name as the tiebreak so equal scores rank deterministically — the cached
    # agent is shared across tenants and an unstable order is an unstable
    # prompt.
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [name for _, name in scored]


class _RunHandle:
    """Private per-run cancellation state.

    One instance per ``run()`` invocation, held in the generator's own frame.
    This is the whole fix for failure mode 1 in the module docstring: because
    the flag lives here and not on the backend, a ``stop()`` for one run cannot
    truncate a sibling, and a run that starts after a ``stop()`` is not born
    already-cancelled.
    """

    __slots__ = ("stopped",)

    def __init__(self) -> None:
        self.stopped = False


class PydanticAIBackend:
    """Pydantic AI backend — in-process agent loop, dispatch-only tools."""

    @staticmethod
    def info() -> BackendInfo:
        return BackendInfo(
            name="pydantic_ai",
            display_name="Pydantic AI",
            capabilities=(
                Capability.STREAMING
                | Capability.TOOLS
                | Capability.MCP
                | Capability.MULTI_TURN
                | Capability.CUSTOM_SYSTEM_PROMPT
            ),
            # Dispatch-only: this backend ships no built-in local file or shell
            # tools of its own. Everything it can call arrives through the tool
            # bridge under the active ToolPolicy.
            builtin_tools=[],
            tool_policy_map={},
            required_keys=[],
            supported_providers=[
                "litellm",
                "agentapi",
                "anthropic",
                "openai",
                "openai_compatible",
                "openrouter",
                "ollama",
            ],
            install_hint={
                "pip_package": "pydantic-ai-slim",
                "pip_spec": "pocketpaw[pydantic-ai]",
                "verify_import": "pydantic_ai",
            },
            beta=True,
        )

    def __init__(self, settings: Settings, policy: ToolPolicy | None = None) -> None:
        self.settings = settings
        self._sdk_available = False
        self._custom_tools: list | None = None
        # Bumped on every mutation of the tool surface. Was ``id(_custom_tools)``
        # in the agent cache key, which is correct only by accident: CPython
        # reuses an address once the old list is collected, so the key could
        # collide across a rebuild. A counter says what is meant.
        self._tools_version = 0
        self._mcp_tools: list | None = None
        # Holds every MCP server open for this instance's lifetime so the
        # refcount never returns to zero. Unwound in ``stop()``.
        self._mcp_stack: Any = None
        # ``_build_mcp_tools`` awaits while starting servers, so two concurrent
        # first runs would otherwise both see an empty cache and each spawn a
        # full set of subprocesses — the exact cost this guards against.
        self._mcp_lock = asyncio.Lock()
        # One HTTP client for the instance, not one per run. ``_build_model``
        # runs on every turn while ``_get_or_create_agent`` usually hands back a
        # CACHED agent holding the previous model — so a client built here would
        # be created and dropped each turn, leaking its connection pool.
        self._http_client: Any = None
        # Per-session pydantic-ai message history, keyed by ``session_key``.
        #
        # The cloud persists conversation as ``[{role, content}]`` TEXT — tool
        # calls and their results are not stored at all. That is fine for
        # ``claude_agent_sdk``, whose CLI subprocess keeps the real transcript
        # and treats the text as a restore-from-restart fallback
        # (``load_history_for_scope``'s docstring says exactly this). This
        # backend had no equivalent, so every turn rebuilt from text and lost
        # every tool result — including the ``pocket_id`` a site draft returns,
        # which is why "publish it" on the next turn had no id to publish.
        #
        # Bounded on both axes: sessions evict oldest-first, and each session
        # keeps a trailing window, so a long-lived instance cannot grow without
        # limit. Losing an entry degrades to the text history, never to an error.
        self._session_messages: OrderedDict[str, list] = OrderedDict()
        # An injected policy is the PER-AGENT one. ``AgentPool._build`` used to
        # hand it to ClaudeSDKBackend alone, so a cloud agent that switched to
        # this backend silently ran under the process-wide policy — default
        # profile ``full``, no narrowing — and its ``mcp_servers_allow`` opt-ins
        # (``pocketpaw_planner``) could never be honoured, because that set has
        # no other way in.
        self._policy = policy or ToolPolicy(
            profile=settings.tool_profile,
            allow=settings.tools_allow,
            deny=settings.tools_deny,
        )
        # Live runs. A set, not a flag — see ``_RunHandle``.
        self._active: set[_RunHandle] = set()
        self._cached_agent: Any = None
        self._cached_agent_key: Any = None
        self._initialize()

    # -- policy -------------------------------------------------------------

    def get_tool_policy(self) -> ToolPolicy:
        return self._policy

    def set_tool_policy(self, policy: ToolPolicy) -> None:
        self._policy = policy
        self._custom_tools = None
        self._tools_version += 1
        self._mcp_tools = None
        self._cached_agent = None
        self._cached_agent_key = None

    def _initialize(self) -> None:
        try:
            import pydantic_ai  # noqa: F401

            self._sdk_available = True
            logger.info("Pydantic AI SDK ready")
        except ImportError:
            logger.warning("Pydantic AI SDK not installed -- pip install 'pocketpaw[pydantic-ai]'")

    # -- model --------------------------------------------------------------

    def _parse_provider_model(self, model_spec: str | None = None) -> tuple[str, str]:
        """Split a ``provider:model`` spec into its parts.

        Defaults to ``pydantic_ai_model``; ``model_spec`` lets the fast-model
        selector reuse the same parsing and fallback chain rather than growing a
        second, subtly different one.

        Accepts ``provider:model`` or a bare model name, falling back to
        ``pydantic_ai_provider`` then ``llm_provider`` then ``litellm``.
        Mirrors ``DeepAgentsBackend._parse_provider_model`` so an operator can
        move a value between the two settings without reformatting it.
        """
        model_str = (
            model_spec if model_spec is not None else (self.settings.pydantic_ai_model or "")
        ).strip()
        if ":" in model_str:
            provider, _, model = model_str.partition(":")
            return provider.strip(), model.strip()

        provider = getattr(self.settings, "pydantic_ai_provider", "auto")
        if provider == "auto":
            provider = self.settings.llm_provider
        if provider == "auto":
            provider = "litellm"
        return provider, model_str

    def _build_model(self, model_spec: str | None = None) -> Any:
        """Build the pydantic-ai model client for the configured provider."""
        provider, model = self._parse_provider_model(model_spec)

        if provider == "agentapi":
            # Development path: borrow a local CLI's own authentication instead
            # of a provider key. Text only — the wrapped agent never emits
            # structured tool calls, so the tool loop is inert. See
            # pydantic_ai_agentapi for the full caveat.
            from pocketpaw.agents.pydantic_ai_agentapi import AgentAPIModel

            return AgentAPIModel(
                model or "claude",
                base_url=str(getattr(self.settings, "agentapi_base_url", "") or ""),
                timeout=float(getattr(self.settings, "agentapi_timeout", 0) or 600),
            )

        if provider == "anthropic":
            from pydantic_ai.models.anthropic import AnthropicModel
            from pydantic_ai.providers.anthropic import AnthropicProvider

            # ``http_client`` for the same reason as the OpenAI branch below:
            # ``_build_model`` runs EVERY turn while the agent cache hands back
            # the previous model, so a provider that builds its own client
            # leaks one connection pool per turn (measured: 3 turns, 3 clients,
            # none closed, and ``stop()`` closed none of them). Sharing it also
            # applies ``pydantic_ai_timeout``, which this branch otherwise
            # ignored — it kept the SDK's 600s read deadline that setting exists
            # to replace.
            provider_kwargs: dict[str, Any] = {"api_key": self.settings.anthropic_api_key or ""}
            anthropic_http_client = self._get_http_client()
            if anthropic_http_client is not None:
                provider_kwargs["http_client"] = anthropic_http_client
            return AnthropicModel(
                model or "claude-sonnet-4-6",
                provider=AnthropicProvider(**provider_kwargs),
            )

        if provider not in _OPENAI_COMPATIBLE:
            raise ValueError(
                f"pydantic_ai backend: unsupported provider {provider!r}. "
                f"Supported: {', '.join(sorted(_OPENAI_COMPATIBLE | {'anthropic', 'agentapi'}))}."
            )

        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        base_url, api_key, model = self._resolve_openai_compatible(provider, model)
        provider_kwargs: dict[str, Any] = {"base_url": base_url, "api_key": api_key}
        http_client = self._get_http_client()
        if http_client is not None:
            provider_kwargs["http_client"] = http_client
        logger.info(
            "Pydantic AI: OpenAIChatModel(%r) via provider=%s base_url=%s",
            model,
            provider,
            base_url,
        )
        cls = _reasoning_echo_model_class(model) or OpenAIChatModel
        return cls(model, provider=OpenAIProvider(**provider_kwargs))

    def _get_http_client(self) -> Any:
        """The instance's shared HTTP client, built once.

        Exists for one reason: the OpenAI SDK's default READ timeout is 600s,
        and an agent turn is not bounded by ten minutes. A long tool chain, a
        slow upstream or a reasoning model thinking between tokens trips it, and
        the run dies mid-generation with everything already spent.

        Note this is only OUR half. ``Upstream idle timeout exceeded`` comes
        from the gateway in front of the model (LiteLLM / OpenRouter), which
        enforces its own idle window and cannot be raised from here — if that
        message survives this change, the limit is theirs, not ours.

        ``connect`` stays short on purpose: a dead host should fail in seconds
        rather than inherit the hour-long budget meant for a working one.
        """
        if self._http_client is not None:
            return self._http_client
        seconds = float(getattr(self.settings, "pydantic_ai_timeout", 0) or 0)
        try:
            import httpx

            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    None if seconds <= 0 else seconds,
                    connect=15.0,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not build HTTP client, using library defaults: %s", exc)
            self._http_client = None
        return self._http_client

    def _resolve_openai_compatible(self, provider: str, model: str) -> tuple[str | None, str, str]:
        """Return ``(base_url, api_key, model)`` for an OpenAI-compatible provider."""
        if provider == "litellm":
            # NOTE the ``/v1``. ``deep_agents`` passes ``litellm_api_base`` WITHOUT
            # it because ChatLiteLLM hands the URL to the LiteLLM SDK, which
            # appends the path itself. Here the OpenAI client appends only
            # ``/chat/completions``, so the version segment has to be on the base
            # URL or every request 404s. Same setting, two different contracts —
            # this is the single easiest thing to get wrong in this file.
            base = (self.settings.litellm_api_base or "http://localhost:4000").rstrip("/")
            if not base.endswith("/v1"):
                base = f"{base}/v1"
            # The proxy is the auth boundary: this is the tenant's virtual key,
            # not an upstream provider key. A placeholder keeps the OpenAI client
            # happy on proxies configured without auth.
            return (
                base,
                self.settings.litellm_api_key or "not-needed",
                (model or self.settings.litellm_model or ""),
            )

        if provider == "openai":
            return (
                None,
                self.settings.openai_api_key or "",
                (model or self.settings.openai_model or "gpt-5.2"),
            )

        if provider == "openrouter":
            return (
                "https://openrouter.ai/api/v1",
                self.settings.openrouter_api_key or self.settings.openai_compatible_api_key or "",
                model or self.settings.openrouter_model or "",
            )

        if provider == "ollama":
            host = (self.settings.ollama_host or "http://localhost:11434").rstrip("/")
            if not host.endswith("/v1"):
                host = f"{host}/v1"
            # Ollama's OpenAI-compatible endpoint ignores the key but the client
            # requires a non-empty one.
            return host, "ollama", (model or self.settings.ollama_model or "llama3.2")

        # openai_compatible
        base = (self.settings.openai_compatible_base_url or "").rstrip("/") or None
        return (
            base,
            self.settings.openai_compatible_api_key or "",
            (model or self.settings.openai_compatible_model or ""),
        )

    # -- tools --------------------------------------------------------------

    def _build_custom_tools(self) -> list:
        """Lazily build and cache PocketPaw tools as pydantic-ai ``Tool`` objects.

        Early-returns when ``_custom_tools`` is already populated. That guard is
        load-bearing, not an optimisation: ``attach_specialist_tools`` pre-fills
        the list for an isolated specialist run, and returning here is what keeps
        ``pocket_specialist__create`` — auto-injected by the bridge for every
        main-agent run — OUT of the specialist's own backend. Without it the
        specialist can call itself. (``deep_agents._build_custom_tools`` carries
        the same guard for the same reason.)
        """
        if self._custom_tools is not None:
            return self._custom_tools
        try:
            from pocketpaw.agents.tool_bridge import build_pydantic_ai_tools

            bridged = build_pydantic_ai_tools(
                self.settings, backend="pydantic_ai", policy=self._policy
            )
            # Filtered HERE, at the only place the bridged surface is built, so
            # no later code path can reintroduce them — a caller cannot forget
            # to pass a flag and a surface cannot grant them back.
            #
            # An ALLOWLIST, so the boundary fails closed: a tool nobody has
            # classified is withheld rather than handed to every tenant.
            self._custom_tools = [
                t for t in bridged if getattr(t, "name", "") in _TENANT_SAFE_TOOLS
            ]
            unclassified = sorted(
                name
                for t in bridged
                if (name := getattr(t, "name", "")) not in _TENANT_SAFE_TOOLS
                and name not in _WITHHELD_TOOLS
            )
            if unclassified:
                # Loud: silence here is the failure mode. The tool is already
                # withheld by the line above; this is how someone finds out.
                logger.warning(
                    "Dispatch-only: withholding UNCLASSIFIED tool(s) %s — add each to "
                    "_TENANT_SAFE_TOOLS or _WITHHELD_TOOLS in agents/pydantic_ai.py",
                    unclassified,
                )
            dropped = len(bridged) - len(self._custom_tools)
            if dropped:
                logger.info(
                    "Dispatch-only: withheld %d tool(s); %d offered",
                    dropped,
                    len(self._custom_tools),
                )
        except Exception as exc:
            logger.info("Could not build custom tools: %s", exc)
            self._custom_tools = []
        return self._custom_tools

    async def _build_mcp_tools(self) -> list:
        """Build pydantic-ai toolsets from PocketPaw's configured MCP servers.

        Two separate things keep this off the request path, and BOTH are
        required — instance caching alone is not enough:

        1. **Built once per instance.** Constructing servers per run would put a
           process spawn in every turn.
        2. **Held open for the instance's lifetime.** pydantic-ai's MCP servers
           are refcounted (``mcp.py:_running_count``): a shared server tears down
           the moment concurrent runs reach zero and RESPAWNS on the next run. A
           cached-but-unheld server therefore still spawns a stdio subprocess per
           run whenever traffic is sparse — which is most of the time outside a
           load test. Entering each server once into ``self._mcp_stack`` pins the
           refcount at >= 1, so per-run enter/exit can never drop it to zero.
           ``stop()`` unwinds the stack.

        ``test_mcp_servers_spawn_once_across_many_runs`` measures exactly that
        and is mutation-checked: drop the exit-stack hold and the spawn count
        goes from 1 to one-per-run.
        """
        if self._mcp_tools is not None:
            return self._mcp_tools

        async with self._mcp_lock:
            # Re-check: a concurrent first run may have built it while we waited.
            if self._mcp_tools is None:
                self._mcp_tools = await self._start_mcp_servers()
            return self._mcp_tools

    @staticmethod
    def _mcp_client_for(cfg: Any) -> Any:
        """Build the fastmcp transport for one PocketPaw MCP server config.

        ``MCPToolset`` takes anything fastmcp can build a transport from. Stdio
        needs an explicit ``StdioTransport`` so ``keep_alive`` is set on purpose
        rather than inherited: it keeps the child process alive across client
        sessions, which is the second half of not spawning per run (the first
        being the exit-stack hold in the caller).
        """
        transport = getattr(cfg, "transport", "")
        if transport == "stdio" and getattr(cfg, "command", None):
            from fastmcp.client.transports import StdioTransport

            return StdioTransport(
                command=cfg.command,
                args=list(cfg.args or []),
                env=cfg.env or None,
                keep_alive=True,
            )
        if transport in ("sse", "http", "streamable-http") and getattr(cfg, "url", None):
            # fastmcp infers SSE vs streamable-HTTP from the URL.
            return cfg.url
        return None

    async def _start_mcp_servers(self) -> list:
        """Construct and start the configured MCP servers. Caller holds the lock."""
        if not getattr(self.settings, "pydantic_ai_mcp_enabled", True):
            return []

        try:
            from pydantic_ai.mcp import MCPToolset
            from pydantic_ai.toolsets import PrefixedToolset
        except ImportError:
            logger.debug("pydantic-ai MCP extra not installed, skipping MCP tools")
            return []

        try:
            from pocketpaw.mcp.config import load_mcp_config
        except ImportError:
            return []

        servers: list = []
        for cfg in load_mcp_config() or []:
            if not cfg.enabled:
                continue
            if not self._policy.is_mcp_server_allowed(cfg.name):
                logger.info("MCP server '%s' blocked by tool policy", cfg.name)
                continue
            try:
                client = self._mcp_client_for(cfg)
                if client is None:
                    continue
                # Prefix with the server name so tools from two servers can't
                # collide, matching what ``load_mcp_toolsets`` does for the
                # config-file path.
                servers.append(PrefixedToolset(MCPToolset(client), cfg.name))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Skipping MCP server '%s': %s", cfg.name, exc)

        # Pin the refcount. Without this every server is torn down as soon as
        # concurrent runs reach zero and respawned on the next turn — see the
        # docstring. A server that fails to start is dropped rather than
        # failing the run: MCP is additive to the tool surface, never
        # load-bearing.
        if servers:
            from contextlib import AsyncExitStack

            stack = AsyncExitStack()
            held: list = []
            for server in servers:
                try:
                    await stack.enter_async_context(server)
                    held.append(server)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "MCP server %r failed to start; continuing without it: %s",
                        getattr(server, "tool_prefix", server),
                        exc,
                    )
            if held:
                self._mcp_stack = stack
            else:
                await stack.aclose()
            servers = held

        if servers:
            logger.info(
                "Built %d MCP toolsets for Pydantic AI, held open for the instance lifetime",
                len(servers),
            )

        # PocketPaw's OWN in-process servers — sites, pocket, connectors, media.
        # Appended after the exit-stack hold on purpose: they are ordinary
        # objects in this process, with no subprocess to keep alive and nothing
        # to tear down. Joining the MCP list rather than the function-tool list
        # is what makes a surface's ``mcp__<server>__<tool>`` deny and allow ids
        # apply to them, which is where those ids were always aimed.
        try:
            from pocketpaw.agents.tool_bridge import build_inprocess_mcp_toolsets

            servers = servers + await build_inprocess_mcp_toolsets(self._policy, self.settings)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not bridge in-process MCP servers: %s", exc)

        return servers

    def attach_specialist_tools(self, tools: list[Any]) -> None:
        """Merge specialist-internal tools into the cache for an isolated run.

        Implementing this is what makes ``pydantic_ai`` eligible for
        ``pocket_specialist_backend`` at all — ``AgentBackend`` excludes any
        backend whose ``attach_specialist_tools`` raises (``backend.py:218``).

        Also pre-sets ``_mcp_tools = []`` to short-circuit MCP loading:
        specialist runs are short-lived and need only the tools passed here, so
        spinning up the user's full MCP server set would add startup latency and
        a hang risk for no benefit.

        Each call EXTENDS the list; tools are not deduplicated. Use an isolated
        backend instance (``AgentRouter.create_isolated_backend``) so tools don't
        accumulate across specialist runs.
        """
        if self._custom_tools is None:
            self._custom_tools = []
        self._custom_tools.extend(tools)
        self._tools_version += 1
        self._mcp_tools = []
        self._cached_agent = None
        self._cached_agent_key = None

    def attach_subprocess_env(self, env: dict[str, str]) -> None:  # noqa: ARG002
        """No-op — this backend spawns no subprocess.

        Part of the ``AgentBackend`` contract that is subprocess-shaped. An
        in-process backend has nothing to inject into, and per-request tenancy
        reaches it through ContextVars instead.
        """
        return None

    def _build_capabilities(self, skill_names: frozenset[str] = frozenset()) -> list:
        """Build the ``pydantic-ai-harness`` capabilities for this backend.

        Four of the PRD's six are wired. The other two are dropped, with the
        reason recorded here rather than left as a silent gap — the PRD's own
        done-condition allows dropping a capability that assumes the FileSystem
        or Shell this design excludes.

        **Wired:**

        * ``SlidingWindow`` + ``ClearToolResults`` (Compaction) — a long tool
          loop is exactly what blows the context on a dispatch-only agent, and
          neither strategy touches disk. ``DeduplicateFileReads`` is NOT used:
          it keys off file-read tools this backend does not have.
        * ``Planning`` — a todo toolset, no filesystem.
        * ``OverflowingToolOutput`` — the per-tool ceiling, enforced in the
          harness rather than only in our bridge wrapper. ``Truncate``, not
          ``Spill``: spilling writes overflow to a store on disk, and a
          process-global path is shared across tenants here.
        * ``StepPersistence`` with an in-memory store — ``FileStepStore`` and
          ``SqliteStepStore`` are both disk-backed. In-memory keeps the run
          record available for the turn without a shared-path write.

        * **Skills** — via ``pydantic-ai-skills`` (``SkillsCapability``), NOT
          the harness, which ships no skills capability of its own in 0.8.0.
          See ``_build_skills_capability``.

        **Dropped:**

        * **Subagents** — ``SubAgents`` defaults to discovering agents from an
          ``agents`` FOLDER on disk, and we have no in-code subagents to
          register. Wiring it with an empty list would add a capability that
          can never fire. Revisit when there is a real subagent to declare.
        """
        # Built first and outside the harness gate: tool search belongs to
        # pydantic-ai core, so turning the harness off must not silently drop
        # our ranking function back to the built-in one.
        capabilities: list = []
        for build in (
            self._build_tool_search_capability,
            self._build_thinking_capability,
            self._build_select_model_capability,
            self._build_instrumentation_capability,
        ):
            cap = build()
            if cap is not None:
                capabilities.append(cap)
        capabilities += self._build_web_capabilities()

        if not getattr(self.settings, "pydantic_ai_harness_enabled", True):
            return capabilities
        try:
            from pydantic_ai_harness.compaction import ClearToolResults, SlidingWindow
            from pydantic_ai_harness.overflowing_tool_output import (
                Band,
                OverflowingToolOutput,
                Truncate,
            )
            from pydantic_ai_harness.planning import Planning
            from pydantic_ai_harness.step_persistence import InMemoryStepStore, StepPersistence
        except ImportError:
            logger.debug("pydantic-ai-harness not installed, running without capabilities")
            return capabilities

        limit = int(getattr(self.settings, "pydantic_ai_max_tool_output_chars", 0) or 0)
        capabilities += [
            SlidingWindow(max_messages=self.settings.pydantic_ai_compaction_max_messages),
            ClearToolResults(max_messages=self.settings.pydantic_ai_compaction_max_messages),
            Planning(),
            StepPersistence(store=InMemoryStepStore(), agent_name="pocketpaw"),
        ]
        if limit:
            capabilities.append(
                OverflowingToolOutput(bands=[Band(over=limit, action=Truncate(max_chars=limit))])
            )

        skills = self._build_skills_capability(skill_names)
        if skills is not None:
            capabilities.append(skills)
        return capabilities

    def _build_skills_capability(self, skill_names: frozenset[str] = frozenset()) -> Any:
        """Expose PocketPaw's skills through ``pydantic-ai-skills``.

        Skills reach the model by progressive disclosure: the agent sees a list
        of names and descriptions, and pulls a skill's full body only when it
        decides to use one. That matters here because the alternative — pasting
        every skill into the system prompt — is what makes the prompt enormous
        on a backend whose per-run cost IS the context.

        Three deliberate constraints, and the first is the one measured:

        * **Source is PocketPaw's BUNDLED skills, not the machine's skill
          directories.** ``SkillLoader``'s default ``SKILL_PATHS`` scan
          ``~/.agents/skills``, ``~/.claude/skills`` and
          ``~/.pocketpaw/skills`` — the OPERATOR's own skills. In a
          multi-tenant process those are not tenant content and have no
          business in a tenant's agent, and the cost scales with whatever the
          operator happens to have installed.

          Measured live 2026-07-29 against ``litellm:deepseek-v3.2``, input
          tokens for one trivial turn:

          ===========================================  =============
          configuration                                input tokens
          ===========================================  =============
          harness off, skills off                                 13
          harness on, skills off                                 833
          harness on + 19 BUNDLED skills (shipped)             5,784
          harness on + 42 skills from ``~/.claude``            8,644
          ===========================================  =============

          Progressive disclosure is doing its job — inlining the 42 would have
          been ~118k tokens. The prefix does get cached upstream (see the module
          docstring), so a warm turn pays little of this; the cold turn and any
          cache miss pay all of it. Hence: ship the product's own skills, which
          is a bounded and intentional set, and let a caller narrow further with
          ``skill_names``.
        * **Skills are passed programmatically, never discovered by the
          capability.** ``SkillsCapability`` can scan directories, clone git
          repos, or read S3. All three are declined: PocketPaw owns skill
          discovery, and a second mechanism is a second place a tenant's
          surface could differ from what the policy says it is.
        * **``run_skill_script`` is excluded.** It executes a skill's bundled
          script — local execution, which dispatch-only rules out and which has
          no per-tenant jail on an in-process backend. ``read_skill_resource``
          goes too, since we pass no resources, so leaving it would advertise a
          tool that can only fail.

        *only* is the subset named by ``skill_names`` when the caller supplies
        one — the same per-entity kwarg the Claude SDK backend uses to narrow a
        run's skills.

        Returns ``None`` when the package is absent, the feature is off, or no
        skills resolve — an empty capability would just add tool surface.
        """
        if not getattr(self.settings, "pydantic_ai_skills_enabled", True):
            return None
        try:
            from pydantic_ai_skills import Skill as PaiSkill
            from pydantic_ai_skills import SkillsCapability
        except ImportError:
            logger.debug("pydantic-ai-skills not installed, running without skills")
            return None

        try:
            loaded = self._load_bundled_skills()
        except Exception as exc:  # noqa: BLE001
            logger.info("Could not load PocketPaw skills: %s", exc)
            return None

        skills = [
            PaiSkill(name=s.name, description=s.description, content=s.content)
            for s in loaded
            if not getattr(s, "disable_model_invocation", False)
            and (not skill_names or s.name in skill_names)
        ]
        if not skills:
            return None

        defer = bool(getattr(self.settings, "pydantic_ai_defer_skills", False))
        logger.info(
            "Pydantic AI: exposing %d PocketPaw skills%s",
            len(skills),
            " (deferred behind load_capability)" if defer else "",
        )
        if not defer:
            return SkillsCapability(
                skills=skills,
                exclude_tools={"run_skill_script", "read_skill_resource"},
                validate=False,
            )

        # Deferred: the catalog leaves the system prompt and the model pulls it
        # with ``load_capability`` first. ``id`` is REQUIRED once
        # ``defer_loading`` is set, and it is what the model names to load this,
        # so it is a stable string rather than anything per-run.
        #
        # The description is written rather than left to the library's default.
        # That default is ``'Provides specialized skills: ' + ', '.join(names)``,
        # and our names alone ("pocketpaw-create-paw-site",
        # "foresight-create-sim") do not tell a model that a request to build a
        # landing page is one of these. A deferred capability nobody loads is
        # strictly worse than no deferral: the skills are still built, still
        # cost the round trip to discover, and never reach the model.
        return SkillsCapability(
            skills=skills,
            exclude_tools={"run_skill_script", "read_skill_resource"},
            validate=False,
            id=_SKILLS_CAPABILITY_ID,
            defer_loading=True,
            description=(
                "PocketPaw's own skills. Load this before building or editing "
                "anything the product owns — pockets, dashboards, Paw Sites "
                "(landing, dynamic, Svelte), Foresight scenarios, connector "
                "workflows (Gmail, GitHub) and design/taste guidance. Each "
                "skill carries the step-by-step procedure and the exact tools "
                "for that job, so load this first rather than improvising one."
            ),
        )

    @staticmethod
    def _load_bundled_skills() -> list:
        """Load ONLY the skills PocketPaw ships, ignoring the machine's dirs.

        ``SkillLoader`` is reused for the parsing, but pointed exclusively at
        the package's own ``_bundled/skills`` tree by clearing the default
        ``SKILL_PATHS``. See ``_build_skills_capability`` for why the operator's
        home directories are deliberately not a source.
        """
        from pathlib import Path

        import pocketpaw.bundled_skills as bundled_pkg
        from pocketpaw.skills.loader import SkillLoader

        bundled_dir = Path(bundled_pkg.__file__).parent / "_bundled" / "skills"
        if not bundled_dir.is_dir():
            return []
        loader = SkillLoader()
        loader.paths = [bundled_dir]  # NOT extra_paths — replaces the home dirs
        return list(loader.load(force=True).values())

    # -- agent assembly -----------------------------------------------------

    @staticmethod
    def _gate_mcp_toolsets(
        mcp_toolsets: list,
        deny: frozenset[str],
        allow_mcp_tool_ids: frozenset[str] | None,
        exclusive_mcp_tools: bool,
    ) -> list:
        """Apply the surface's deny / allow sets to the MCP toolsets.

        Mirrors ``claude_sdk``'s precedence: deny is subtracted first and is the
        hard boundary, then the RESTRICTIVE allow set keeps only what it names.

        The allow set is applied to MCP toolsets ONLY, matching the SDK, where
        "built-in SDK tools are NEVER filtered here — only ``mcp__*`` ids". The
        split lands differently on this backend but in the same place: our MCP
        toolsets are the user's EXTERNAL configured servers, while the
        in-process pocket / widget / sites tools arrive as bridged function
        tools — which is exactly the group the SDK's grant unions back in
        (``POCKET_CREATION_GRANT`` / widget / atlas ids). Restricting them here
        would be stricter than the surface asks for and would break /sites.
        """
        if not mcp_toolsets or (not deny and allow_mcp_tool_ids is None):
            return mcp_toolsets

        # An exclusive turn CAPS the surface to the allow set alone — with no
        # allow set that is an EMPTY permitted set, so every MCP tool goes. That
        # is how a dedicated agent wins over a broad surface (claude_sdk CX-1).
        permitted = (
            (allow_mcp_tool_ids or frozenset()) if exclusive_mcp_tools else allow_mcp_tool_ids
        )
        allowed = None if permitted is None else _expand_tool_ids(permitted)

        def _keep(_ctx: Any, tool_def: Any) -> bool:
            name = getattr(tool_def, "name", "")
            if name in deny:
                return False
            return allowed is None or name in allowed

        return [ts.filtered(_keep) for ts in mcp_toolsets]

    def _defer_mcp_toolsets(
        self,
        mcp_toolsets: list,
        *,
        allow_mcp_tool_ids: frozenset[str] | None = None,
        exclusive_mcp_tools: bool = False,
    ) -> list:
        """Hide the MCP tools behind tool search instead of advertising them.

        An ungated surface carries 134 tools, and their schemas are ~30,500
        tokens of JSON on EVERY model request — measured against the real
        corpus, and a cost paid in full because tool schemas do not
        prompt-cache on our proxy (see the caching table in the module
        docstring: that measurement covers the text prefix, not the tool
        block). Deferring the 97 bridged ones puts 38 on the wire for ~5,900
        tokens, and the model pulls what it needs by calling ``search_tools``.

        Two deliberate boundaries:

        * **After gating.** ``_gate_mcp_toolsets`` has already removed what the
          surface denies, so a denied tool is never in the search corpus and no
          query can reveal it. Being straight about how much this order carries:
          a mutation probe reversing it left
          ``test_a_denied_tool_cannot_be_discovered_by_searching_for_it``
          GREEN, because ``filtered()`` wraps the deferred toolset and still
          matches on name. So the order is the clearer arrangement rather than
          the thing that makes the property hold, and the test pins the
          property — denied tools stay undiscoverable — not the order.
        * **MCP toolsets only.** The ~37 builtin function tools stay visible.
          They are the small half of the bill and the half the agent reaches
          for constantly; hiding them would buy little and cost a round trip
          on almost every turn.

        The cost is one extra model request per discovery. That is cheap at
        these ratios — a two-call turn is ~61k tool tokens today against ~18k
        deferred — but it is not free, and on a surface that already gates hard
        (``/sites`` cuts 97 to 12) there is little left to save.
        """
        if not mcp_toolsets or not getattr(self.settings, "pydantic_ai_defer_mcp_tools", False):
            return mcp_toolsets
        # A surface that already named its tools has nothing to save and
        # something to lose: hiding three tools behind a search still costs the
        # discovery round trip, and a dedicated agent that fails to search finds
        # nothing at all. ``/sites`` cuts 97 MCP tools to 12 this way.
        if exclusive_mcp_tools or allow_mcp_tool_ids is not None:
            return mcp_toolsets
        return [ts.defer_loading() for ts in mcp_toolsets]

    def _build_tool_search_capability(self) -> Any:
        """The ``ToolSearch`` capability, carrying OUR ranking function.

        pydantic-ai auto-injects a default ``ToolSearch`` into every agent, so
        this is an override rather than an addition: naming the capability
        explicitly is the supported way to replace its keyword-overlap
        algorithm. Returns ``None`` when deferral is off, which leaves the
        auto-injected default in place with an empty corpus and nothing to do.
        """
        if not getattr(self.settings, "pydantic_ai_defer_mcp_tools", False):
            return None
        try:
            from pydantic_ai.capabilities import ToolSearch
        except ImportError:  # pragma: no cover - pydantic-ai too old
            logger.warning("pydantic-ai has no ToolSearch capability; deferral disabled")
            return None
        return ToolSearch(strategy=pocketpaw_tool_search)

    # Bridged tool -> the capability that supersedes it when native web tools
    # are on. The bridged tool is not dropped; it becomes that capability's
    # LOCAL fallback, which is what keeps one implementation serving both paths
    # and stops the model being offered two tools for one job.
    _NATIVE_WEB_EQUIVALENTS: dict[str, str] = {"web_search": "WebSearch", "url_extract": "WebFetch"}

    def _build_instrumentation_capability(self) -> Any:
        """OTel spans for the run, so latency claims can be checked.

        This backend's whole argument is a cost curve, and PA-1 — the
        concurrency measurement that decides whether it beats ``deep_agents`` —
        is still unrun. Spans are what make that measurable from the inside
        rather than by wrapping a stopwatch around the whole request.

        ``logfire.configure`` is called with ``send_to_logfire='if-token-present'``
        so this is safe to enable on a deployment with no Logfire account: the
        spans go to whatever OTel exporter is already configured, or nowhere.
        Configuration is process-global and guarded by a module flag, because
        one instance per agent means this method runs many times.
        """
        if not getattr(self.settings, "pydantic_ai_instrumentation", False):
            return None
        try:
            from pydantic_ai.capabilities import Instrumentation
        except ImportError:  # pragma: no cover - pydantic-ai too old
            return None

        global _LOGFIRE_CONFIGURED
        if not _LOGFIRE_CONFIGURED:
            _LOGFIRE_CONFIGURED = True
            try:
                import logfire

                logfire.configure(
                    send_to_logfire="if-token-present",
                    service_name="pocketpaw-pydantic-ai",
                    console=False,
                )
            except Exception as exc:  # noqa: BLE001
                # Instrumentation is observability, never load-bearing: a
                # misconfigured exporter must not stop a tenant's run.
                logger.warning("Could not configure logfire, spans stay local: %s", exc)
        return Instrumentation()

    def _build_web_capabilities(self) -> list:
        """Move web search and fetch provider-side, keeping ours as the fallback.

        The win is specific to this backend. A bridged web tool makes its HTTP
        call from inside the agent process, so on one process serving every
        tenant our event loop does the waiting; a native tool is executed by the
        provider and arrives as a result. ``research`` is deliberately left
        alone — it is a multi-step routine of ours, not a single fetch, so no
        native tool is equivalent to it.

        ``local=`` takes our own ``Tool`` rather than pydantic-ai's DuckDuckGo
        fallback on purpose: a second search implementation would drift from the
        one the other backends use, and the charter's one-canonical-module rule
        is the reason the in-process MCP bridge exists at all.
        """
        if not getattr(self.settings, "pydantic_ai_native_web_tools", False):
            return []
        try:
            from pydantic_ai.capabilities import WebFetch, WebSearch
        except ImportError:  # pragma: no cover - pydantic-ai too old
            return []

        by_name = {getattr(t, "name", ""): t for t in self._build_custom_tools()}
        out: list = []
        for tool_name, cap_name in self._NATIVE_WEB_EQUIVALENTS.items():
            local = by_name.get(tool_name)
            if local is None:
                # The surface withheld it, so there is nothing to fall back to.
                # Registering the native tool anyway would GRANT a capability
                # the policy just removed.
                logger.info("Native %s not wired: %r is not on this surface", cap_name, tool_name)
                continue
            cap = WebSearch if cap_name == "WebSearch" else WebFetch
            out.append(cap(local=local))
        return out

    def _build_thinking_capability(self) -> Any:
        """Set reasoning effort explicitly instead of inheriting the provider's.

        ``Thinking`` writes pydantic-ai's portable ``thinking`` model setting,
        so one value works across Anthropic, OpenAI and the proxy rather than
        needing each vendor's own spelling. Returns ``None`` for ``default``,
        which leaves the setting absent entirely — not the same as ``off``,
        which actively disables thinking on a model that would otherwise use it.
        """
        raw = str(getattr(self.settings, "pydantic_ai_thinking", "default") or "default").lower()
        if raw == "default":
            return None
        try:
            from pydantic_ai.capabilities import Thinking
        except ImportError:  # pragma: no cover - pydantic-ai too old
            return None
        if raw in ("off", "false", "no"):
            return Thinking(effort=False)
        if raw in ("minimal", "low", "medium", "high", "xhigh"):
            return Thinking(effort=raw)
        logger.warning(
            "Ignoring POCKETPAW_PYDANTIC_AI_THINKING=%r — expected one of "
            "default, off, minimal, low, medium, high, xhigh",
            raw,
        )
        return None

    def _build_select_model_capability(self) -> Any:
        """Downshift to a cheaper model part-way through a long run.

        Off unless a fast model AND at least one threshold are configured, so
        the default path is byte-for-byte what it was: one model, first step to
        last.

        The selector is deliberately dumb. ``ModelSelectionContext`` offers the
        step number, the accumulated usage and the whole message history, and a
        cleverer policy is easy to write and hard to justify — which of them
        actually pays is a per-model empirical question, and answering it is
        what the evals harness is for. This ships the mechanism with the two
        thresholds that can be reasoned about without a benchmark: a step count
        and a token ceiling.
        """
        spec = str(getattr(self.settings, "pydantic_ai_fast_model", "") or "").strip()
        after_step = int(getattr(self.settings, "pydantic_ai_fast_model_after_step", 0) or 0)
        after_tokens = int(getattr(self.settings, "pydantic_ai_fast_model_after_tokens", 0) or 0)
        if not spec or (after_step <= 0 and after_tokens <= 0):
            return None
        try:
            from pydantic_ai.capabilities import SelectModel
        except ImportError:  # pragma: no cover - pydantic-ai too old
            return None

        # Built once, not per step. The fast model shares the instance HTTP
        # client through ``_build_model``, so this does not reintroduce the
        # per-turn connection pool the anthropic branch used to leak.
        fast_model = self._build_model(spec)

        def _select(ctx: Any) -> Any:
            step = getattr(ctx, "run_step", 1) or 1
            used = int(getattr(getattr(ctx, "usage", None), "input_tokens", 0) or 0)
            if (after_step > 0 and step >= after_step) or (
                after_tokens > 0 and used >= after_tokens
            ):
                return fast_model
            return ctx.model

        logger.info(
            "Pydantic AI: downshifting to %r after step %s / %s input tokens",
            spec,
            after_step or "never",
            after_tokens or "never",
        )
        return SelectModel(selector=_select)

    def _get_or_create_agent(
        self,
        model: Any,
        instructions: str,
        mcp_toolsets: list,
        skill_names: frozenset[str] = frozenset(),
        *,
        deny_mcp_tool_ids: frozenset[str] = frozenset(),
        allow_mcp_tool_ids: frozenset[str] | None = None,
        exclusive_mcp_tools: bool = False,
    ) -> Any:
        """Build (and cache) the pydantic-ai ``Agent``.

        Cached on everything that shapes the tool surface or the model, so
        flipping between pocket and non-pocket sessions on one instance rebuilds
        rather than silently reusing the wrong tool set.
        """
        from pydantic_ai import Agent

        is_pocket_session = _POCKET_SCOPE_SENTINEL in (instructions or "")
        deny = _expand_tool_ids(deny_mcp_tool_ids)

        # Build tools BEFORE the cache key. ``_build_custom_tools`` populates
        # ``self._custom_tools`` on first call, so keying off it beforehand
        # compares ``id(None)`` against ``id(list)`` on the next run and the
        # cache NEVER hits — every run re-instantiates the whole tool set. That
        # is not a slow path, it is a per-run cost on the thing whose entire
        # purpose is a low per-run cost, and it is invisible except as latency.
        tools = list(self._build_custom_tools())

        agent_key = (
            self.settings.pydantic_ai_model,
            is_pocket_session,
            len(mcp_toolsets),
            self._tools_version,
            len(tools),
            # In the key, not just a constructor argument: the skill subset
            # shapes the agent's capabilities, so an entity with a narrower set
            # must not be served an agent cached for a wider one.
            tuple(sorted(skill_names)),
            # Same reason, and here it decides a security boundary rather than a
            # capability: ``AgentPool`` drives EVERY surface through one cached
            # instance, so without these in the key whichever surface ran first
            # would pick the tool surface for all of them — a restricted turn
            # would silently be served the unrestricted agent.
            tuple(sorted(deny)),
            None if allow_mcp_tool_ids is None else tuple(sorted(allow_mcp_tool_ids)),
            exclusive_mcp_tools,
        )
        if self._cached_agent is not None and self._cached_agent_key == agent_key:
            return self._cached_agent

        if deny:
            before = len(tools)
            tools = [t for t in tools if getattr(t, "name", "") not in deny]
            if before != len(tools):
                logger.info(
                    "Surface tool-deny: stripped %d tool(s) for %s",
                    before - len(tools),
                    sorted(deny_mcp_tool_ids),
                )

        mcp_toolsets = self._gate_mcp_toolsets(
            mcp_toolsets, deny, allow_mcp_tool_ids, exclusive_mcp_tools
        )
        mcp_toolsets = self._defer_mcp_toolsets(
            mcp_toolsets,
            allow_mcp_tool_ids=allow_mcp_tool_ids,
            exclusive_mcp_tools=exclusive_mcp_tools,
        )

        # A belt-and-braces sweep, cheap and last. ``_build_custom_tools`` is
        # the real boundary, but ``attach_specialist_tools`` also writes into
        # ``_custom_tools`` and takes whatever a caller hands it — this is what
        # makes the guarantee hold for the tools THIS AGENT gets, whatever the
        # source.
        #
        # A DENYLIST here on purpose, where the builder uses an allowlist:
        # specialist-internal tools are legitimately not in ``_TENANT_SAFE_TOOLS``
        # (they are private to one specialist run), so screening them against it
        # would strip the very tools the caller just attached.
        tools = [t for t in tools if getattr(t, "name", "") not in _WITHHELD_TOOLS]

        # A tool that is now a native capability's LOCAL fallback must not also
        # ride the plain tool list: the capability puts it on the wire itself on
        # a provider without native support, and two entries for one job is the
        # duplicate problem this backend already has four of.
        if getattr(self.settings, "pydantic_ai_native_web_tools", False):
            superseded = set(self._NATIVE_WEB_EQUIVALENTS)
            tools = [t for t in tools if getattr(t, "name", "") not in superseded]

        agent = Agent(
            model,
            instructions=instructions,
            tools=tools,
            toolsets=list(mcp_toolsets) or None,
            capabilities=self._build_capabilities(skill_names) or None,
            # The agent is shared across concurrent runs; conversation state
            # rides in ``message_history`` per run, never on the agent.
            retries=2,
        )
        self._cached_agent = agent
        self._cached_agent_key = agent_key
        return agent

    def _retain_run_transcript(self, session_key: str | None, event: Any) -> None:
        """Capture the finished run's messages off the terminal result event."""
        if not session_key:
            return
        from pydantic_ai.run import AgentRunResultEvent

        if not isinstance(event, AgentRunResultEvent):
            return
        try:
            self._retain_session(session_key, event.result.all_messages())
        except Exception as exc:  # noqa: BLE001
            # Never fail a run over bookkeeping — the next turn just falls back
            # to the text history.
            logger.debug("Could not retain session transcript: %s", exc)

    def _session_history(self, session_key: str | None, history: list[dict] | None) -> list:
        """The message history for this turn — the real transcript when we have it.

        Prefers the retained pydantic-ai messages for ``session_key`` because
        they carry the TOOL CALLS AND RESULTS. The ``history`` argument cannot:
        the cloud persists ``[{role, content}]`` text and nothing else, so a
        ``pocket_id`` handed back by ``create_html_site`` is gone by the next
        turn and "publish it" has no id to publish.

        Falls back to the text history whenever nothing is retained — a fresh
        process, an evicted entry, a first turn. That is a degraded context, not
        a failure, which is the same trade ``claude_agent_sdk`` makes when its
        subprocess is gone (``load_history_for_scope``).
        """
        if session_key:
            retained = self._session_messages.get(session_key)
            if retained:
                self._session_messages.move_to_end(session_key)
                return list(retained)
        return self._build_history(history)

    def _retain_session(self, session_key: str | None, messages: list | None) -> None:
        """Keep this run's transcript for the next turn on the same session."""
        if not session_key or not messages:
            return
        # Trailing window: the head of a conversation is the least useful part
        # to carry and the most expensive, and compaction capabilities already
        # operate inside a run.
        self._session_messages[session_key] = list(messages)[-_MAX_SESSION_MESSAGES:]
        self._session_messages.move_to_end(session_key)
        while len(self._session_messages) > _MAX_TRACKED_SESSIONS:
            self._session_messages.popitem(last=False)

    def _build_history(self, history: list[dict] | None) -> list:
        """Convert PocketPaw's ``[{role, content}]`` history to pydantic-ai messages."""
        from pydantic_ai.messages import (
            ModelRequest,
            ModelResponse,
            TextPart,
            UserPromptPart,
        )

        messages: list = []
        for msg in history or []:
            content = msg.get("content") or ""
            if not content:
                continue
            if msg.get("role") == "assistant":
                messages.append(ModelResponse(parts=[TextPart(content=content)]))
            else:
                messages.append(ModelRequest(parts=[UserPromptPart(content=content)]))
        return messages

    # -- run ----------------------------------------------------------------

    async def run(
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list[dict] | None = None,
        session_key: str | None = None,
        # Per-entity skill subset. Rides the withhold-when-empty contract:
        # ``AgentPool.run`` forwards it only when non-empty, so an empty set
        # means "no per-entity narrowing" and every bundled skill is offered.
        skill_names: frozenset[str] = frozenset(),
        # -- per-surface tool gating (see ``_gate_mcp_toolsets``) ------------
        # These ride the same withhold-when-empty contract, which is why their
        # absence was invisible: the pool forwards them ONLY when a surface
        # actually sets one, so every test and every /chat turn passed and the
        # first /sites turn died with ``TypeError: run() got an unexpected
        # keyword argument 'deny_mcp_tool_ids'`` (observed live 2026-07-31).
        # ``test_run_accepts_every_kwarg_the_pool_forwards`` reads the pool's
        # real forwarding table so the next kwarg fails a test, not a run.
        deny_mcp_tool_ids: frozenset[str] = frozenset(),
        allow_mcp_tool_ids: frozenset[str] | None = None,
        exclusive_mcp_tools: bool = False,
        # Accepted and deliberately unused — each is Claude-SDK plumbing with no
        # analogue here, and each is safe to drop:
        #   ``allow_sdk_tools``   ADDITIVE grant of SDK built-ins. There are no
        #                         SDK built-ins on this backend, so there is
        #                         nothing to grant; ignoring it removes tools,
        #                         never adds them.
        #   ``model_override``    per-send model choice, consumed only by the
        #                         Claude SDK backend (as on the other six).
        #   ``session_handle`` /  native CLI-session resume and warm-client
        #   ``warm_client`` /     reuse — this backend has no subprocess to
        #   ``on_client_built``   resume or lease.
        allow_sdk_tools: frozenset[str] = frozenset(),  # noqa: ARG002
        model_override: str | None = None,  # noqa: ARG002
        session_handle: Any = None,  # noqa: ARG002
        warm_client: Any = None,  # noqa: ARG002
        on_client_built: Any = None,  # noqa: ARG002
    ) -> AsyncIterator[AgentEvent]:
        if not self._sdk_available:
            yield AgentEvent(
                type="error",
                content=(
                    "Pydantic AI SDK not installed.\n\n"
                    "Install with: pip install 'pocketpaw[pydantic-ai]'"
                ),
            )
            return

        # Per-run cancellation. Registered so ``stop()`` can reach it, but owned
        # by this frame so no sibling run can flip it. See ``_RunHandle``.
        handle = _RunHandle()
        self._active.add(handle)

        # Tool ids already announced, so the early PartStartEvent signal and the
        # authoritative FunctionToolCallEvent don't double-announce one call.
        announced: set[str] = set()

        try:
            model = self._build_model()

            # A gated surface is one where WHICH tools the agent has is part of
            # the contract. The agentapi model cannot be part of that contract —
            # it wraps a complete CLI agent that plans and uses its OWN tools,
            # below this backend entirely, so it receives nothing this surface
            # granted and honours nothing it denied. Observed 2026-07-31 on
            # /sites: the wrapped CLI wrote a landing page to the SERVER's disk
            # and blocked on a permission prompt in the `agentapi server`
            # terminal, having never called a sites tool. Refuse rather than let
            # that read as a working turn.
            if (deny_mcp_tool_ids or allow_mcp_tool_ids is not None) and (
                getattr(model, "system", "") == "agentapi"
            ):
                yield AgentEvent(type="error", content=_AGENTAPI_GATED_SURFACE)
                yield AgentEvent(type="done", content="")
                return

            instructions = system_prompt or _DEFAULT_IDENTITY
            mcp_toolsets = await self._build_mcp_tools()
            agent = self._get_or_create_agent(
                model,
                instructions,
                mcp_toolsets,
                skill_names,
                deny_mcp_tool_ids=deny_mcp_tool_ids,
                allow_mcp_tool_ids=allow_mcp_tool_ids,
                exclusive_mcp_tools=exclusive_mcp_tools,
            )

            kwargs: dict[str, Any] = {
                "message_history": self._session_history(session_key, history)
            }
            max_turns = self.settings.pydantic_ai_max_turns
            if max_turns and max_turns > 0:
                from pydantic_ai.usage import UsageLimits

                kwargs["usage_limits"] = UsageLimits(request_limit=max_turns)

            async with agent.run_stream_events(message, **kwargs) as stream:
                async for event in stream:
                    if handle.stopped:
                        break
                    # Retain BEFORE mapping: a run that ends on the terminal
                    # result event must still leave its transcript behind, and
                    # ``_map_event`` returns nothing for a usage-less result.
                    self._retain_run_transcript(session_key, event)
                    for agent_event in self._map_event(event, announced):
                        yield agent_event

        except asyncio.CancelledError:
            # Caller cancelled this run specifically — the correct per-run
            # cancellation path. Propagate; do not degrade it into "done".
            raise
        except Exception as exc:
            logger.error("Pydantic AI streaming error: %s", exc, exc_info=True)
            yield AgentEvent(type="error", content=self._explain_error(exc))
            yield AgentEvent(type="done", content="")
            return
        finally:
            self._active.discard(handle)

        yield AgentEvent(type="done", content="")

    def _explain_error(self, exc: Exception) -> str:
        """Turn a provider error into something that names the actual problem.

        A proxy auth failure is the single most confusing error on this path,
        because there are TWO credentials and the raw body implicates neither.
        The virtual key authenticated fine — the request was routed, a model
        group was chosen, fallbacks were attempted — and then the PROXY's own
        upstream credential was rejected. The unhelpful default reading is
        "my key is wrong", which sends you to change the one thing that works.

        This cost real time to diagnose by hand, so the backend says it now.
        """
        text = str(exc)
        raw = f"Pydantic AI error: {text}"

        is_auth = "status_code: 401" in text or "authentication_error" in text
        provider, model = self._parse_provider_model()
        if not (is_auth and provider == "litellm"):
            return raw

        base = (self.settings.litellm_api_base or "").rstrip("/")
        return (
            f"The LiteLLM proxy rejected model {model!r} with a 401 from its UPSTREAM "
            f"provider — not from your virtual key, which authenticated fine (the request "
            f"was routed and fallbacks were tried).\n\n"
            f"So the credential to fix is the one the PROXY holds for that model's "
            f"provider, not POCKETPAW_LITELLM_API_KEY.\n\n"
            f"To pick a model whose upstream is alive:\n"
            f'  curl -H "Authorization: Bearer $POCKETPAW_LITELLM_API_KEY" {base}/health\n'
            f"and set POCKETPAW_PYDANTIC_AI_MODEL=litellm:<a healthy model group>.\n\n"
            f"Original error: {text[:400]}"
        )

    def _map_event(self, event: Any, announced: set[str]) -> list[AgentEvent]:
        """Translate one pydantic-ai stream event into zero or more ``AgentEvent``.

        The mapping was read off the real event stream rather than the docs:

          PartStartEvent(TextPart)         -> message   (initial content, if any)
          PartDeltaEvent(TextPartDelta)    -> message
          PartStartEvent(ThinkingPart)     -> thinking
          PartDeltaEvent(ThinkingPartDelta)-> thinking
          PartStartEvent(ToolCallPart)     -> tool_use  (early UI signal)
          FunctionToolCallEvent            -> tool_use  (authoritative args)
          FunctionToolResultEvent          -> tool_result
          AgentRunResultEvent              -> token_usage
        """
        from pydantic_ai.messages import (
            FunctionToolCallEvent,
            FunctionToolResultEvent,
            PartDeltaEvent,
            PartStartEvent,
            TextPart,
            TextPartDelta,
            ThinkingPart,
            ThinkingPartDelta,
            ToolCallPart,
        )
        from pydantic_ai.run import AgentRunResultEvent

        out: list[AgentEvent] = []

        if isinstance(event, PartStartEvent):
            part = event.part
            if isinstance(part, TextPart) and part.content:
                out.append(AgentEvent(type="message", content=part.content))
            elif isinstance(part, ThinkingPart) and part.content:
                out.append(AgentEvent(type="thinking", content=part.content))
            elif isinstance(part, ToolCallPart):
                # Early signal so the UI flips from "Thinking..." to
                # "Using <tool>..." before the args finish streaming.
                self._announce_tool(part, announced, out, args={})

        elif isinstance(event, PartDeltaEvent):
            delta = event.delta
            if isinstance(delta, TextPartDelta) and delta.content_delta:
                out.append(AgentEvent(type="message", content=delta.content_delta))
            elif isinstance(delta, ThinkingPartDelta) and delta.content_delta:
                out.append(AgentEvent(type="thinking", content=delta.content_delta))

        elif isinstance(event, FunctionToolCallEvent):
            self._announce_tool(event.part, announced, out, args=event.part.args)

        elif isinstance(event, FunctionToolResultEvent):
            part = event.part
            content = getattr(part, "content", "")
            text = content if isinstance(content, str) else str(content)
            out.append(
                AgentEvent(
                    type="tool_result",
                    content=text[:200],
                    metadata={"name": getattr(part, "tool_name", "tool")},
                )
            )

        elif isinstance(event, AgentRunResultEvent):
            usage_event = self._usage_event(event)
            if usage_event is not None:
                out.append(usage_event)

        return out

    @staticmethod
    def _announce_tool(part: Any, announced: set[str], out: list[AgentEvent], *, args: Any) -> None:
        """Emit a ``tool_use`` for *part* unless its call id was already announced."""
        name = getattr(part, "tool_name", None)
        if not name:
            return
        call_id = getattr(part, "tool_call_id", None)
        if call_id:
            if call_id in announced:
                return
            announced.add(call_id)
        out.append(
            AgentEvent(
                type="tool_use",
                content=f"Using {name}...",
                metadata={"name": name, "input": args if isinstance(args, dict) else {}},
            )
        )

    def _usage_event(self, event: Any) -> AgentEvent | None:
        """Build the ``token_usage`` event from a finished run's ``RunUsage``.

        ``RunUsage.input_tokens`` is the INCLUSIVE total — pydantic-ai documents
        cache reads/writes as subsets of it and normalizes the providers that
        report them disjointly. ``report_savings`` wants the Anthropic-native
        shape, where ``input_tokens`` is the UNCACHED remainder, so the two
        subsets come back out here. Getting this subtraction backwards inflates
        the reported hit rate, which is exactly the number the A/B turns on.
        """
        usage = getattr(getattr(event, "result", None), "usage", None)
        if usage is None:
            return None
        total = int(getattr(usage, "input_tokens", 0) or 0)
        read = int(getattr(usage, "cache_read_tokens", 0) or 0)
        write = int(getattr(usage, "cache_write_tokens", 0) or 0)
        if not read and not write:
            return None

        from pocketpaw.llm.caching import report_savings

        savings = report_savings(
            {
                "input_tokens": max(0, total - read - write),
                "cache_read_input_tokens": read,
                "cache_creation_input_tokens": write,
            }
        )
        logger.info(
            "[pydantic_ai] prompt-cache: read=%d write=%d hit_rate=%.1f%% "
            "est_saved=%.0f input-tok-equiv",
            savings.cache_read_tokens,
            savings.cache_write_tokens,
            savings.hit_rate * 100,
            savings.est_tokens_saved,
        )
        return AgentEvent(
            type="token_usage",
            content="",
            metadata={
                "input_tokens": max(0, total - read - write),
                "cache_read_tokens": savings.cache_read_tokens,
                "cache_write_tokens": savings.cache_write_tokens,
                "cache_hit_rate": savings.hit_rate,
                "cache_est_tokens_saved": savings.est_tokens_saved,
                "backend": "pydantic_ai",
            },
        )

    # -- lifecycle ----------------------------------------------------------

    async def stop(self) -> None:
        """Signal every run live RIGHT NOW, then release MCP resources.

        Snapshot-then-signal, and no instance-level flag: a run started after
        this call gets a fresh ``_RunHandle`` and is unaffected. To cancel ONE
        run, close its generator (or cancel its task) instead — that is the
        per-run path, and it is what the cloud executor uses on supersession.
        """
        for handle in list(self._active):
            handle.stopped = True

        # Release the MCP servers this instance has been holding open. This is
        # the ONLY place they are torn down — the whole point of the exit stack
        # is that no per-run exit can do it.
        if self._mcp_stack is not None:
            stack, self._mcp_stack = self._mcp_stack, None
            try:
                await stack.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.debug("MCP server shutdown error: %s", exc)
            finally:
                self._mcp_tools = None

        if self._http_client is not None:
            client, self._http_client = self._http_client, None
            try:
                await client.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.debug("HTTP client shutdown error: %s", exc)
            # The cached agent holds a model bound to the client just closed.
            self._cached_agent = None
            self._cached_agent_key = None

    async def get_status(self) -> dict[str, Any]:
        provider, model = self._parse_provider_model()
        return {
            "backend": "pydantic_ai",
            "available": self._sdk_available,
            "running": bool(self._active),
            "active_runs": len(self._active),
            "mcp_servers": len(self._mcp_tools or ()),
            "model": self.settings.pydantic_ai_model,
            "provider": provider,
            "resolved_model": model,
        }
