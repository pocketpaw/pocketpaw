"""Composio per-backend providers — the documented in-process pattern.

Composio publishes a dedicated provider package per agent SDK:

    backend_kind        provider package + class
    ────────────────    ─────────────────────────────────────────────
    claude_agent_sdk    composio_claude_agent_sdk.ClaudeAgentSDKProvider
    openai_agents       composio_openai_agents.OpenAIAgentsProvider
    google_adk          composio_google_adk.GoogleAdkProvider
    deep_agents         composio_langgraph.LanggraphProvider
                        (deep_agents is langgraph under the hood)

All four follow the same shape (see Composio docs):

    composio = Composio(provider=XxxProvider())
    session = composio.create(user_id="user_123")
    tools = session.tools()
    # → pass `tools` to whatever the backend's agent constructor expects

This module is invoked from each backend's tool-build path. The
``user_id`` we pass is the namespaced ``f"{enterprise_id}:{user_id}"``
form (so two PocketPaw deployments sharing one Composio org never
collide). The backend's per-stream tool-build resolves the active user
from ``pocketpaw_ee.cloud.chat.agent_service.current_user_id`` so multi-tenancy
is per-call, not per-instance.

Tools are cached per ``(user_id, backend_kind)`` for
``settings.composio_mcp_url_ttl_seconds`` (default 1h). ``composio.create``
costs a network round-trip; without caching we'd pay it on every chat
turn even though the per-user toolset rarely changes within a session.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from pocketpaw.config import Settings
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.composio import service as composio_service

logger = logging.getLogger(__name__)

# Backend kinds we have Composio providers for. The string keys match
# ``settings.agent_backend`` / agent doc ``backend`` values exactly so
# callers can pass their backend name through without translation.
BACKEND_CLAUDE_SDK = "claude_agent_sdk"
BACKEND_OPENAI_AGENTS = "openai_agents"
BACKEND_GOOGLE_ADK = "google_adk"
BACKEND_DEEP_AGENTS = "deep_agents"

SUPPORTED_BACKENDS: frozenset[str] = frozenset(
    {
        BACKEND_CLAUDE_SDK,
        BACKEND_OPENAI_AGENTS,
        BACKEND_GOOGLE_ADK,
        BACKEND_DEEP_AGENTS,
    }
)


@dataclass(frozen=True, slots=True)
class _CachedTools:
    """Process-local cache entry — per-user tools list + expiry epoch."""

    tools: list[Any]
    expires_at: float


# Process-global cache keyed by (namespaced_user_id, backend_kind).
# Composio sessions are user-scoped and the tools change only when the
# user (un)connects an integration upstream — so caching for the TTL
# duration is safe in practice.
_tools_cache: dict[tuple[str, str], _CachedTools] = {}
_tools_cache_lock = threading.Lock()


def build_tools_for_backend(
    backend_kind: str,
    *,
    settings: Settings | None = None,
) -> list[Any]:
    """Return the per-user Composio tools for ``backend_kind``.

    Resolves the active user from ``pocketpaw_ee.cloud.chat.agent_service``
    contextvars (per-stream), builds a Composio session for that user
    via the documented provider pattern, and returns
    ``session.tools()``.

    Returns an empty list when:
        * Composio isn't configured (no api_key + enterprise_id).
        * No active chat-stream context (e.g. CLI run, KB rebuild) —
          we have no user to scope to.
        * The backend isn't in ``SUPPORTED_BACKENDS``.
        * Upstream Composio call fails — we log and degrade rather
          than 500'ing the chat run.

    Caching: by ``(namespaced_user_id, backend_kind)`` for
    ``settings.composio_mcp_url_ttl_seconds`` (default 1h).
    """
    if backend_kind not in SUPPORTED_BACKENDS:
        return []

    s = settings or Settings.load()
    if not composio_service.is_enabled(s):
        return []

    ctx = _resolve_ctx()
    if ctx is None:
        return []

    try:
        namespaced = str(composio_service.composio_user_id(ctx, s))
    except Exception:  # noqa: BLE001
        logger.debug("composio: user_id resolution failed", exc_info=True)
        return []

    cache_key = (namespaced, backend_kind)
    now = time.monotonic()
    with _tools_cache_lock:
        cached = _tools_cache.get(cache_key)
        if cached is not None and cached.expires_at > now:
            return cached.tools

    try:
        tools = _build_session_and_tools(backend_kind, namespaced, s)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Composio: failed to build tools for backend=%s user=%s — "
            "agent will run without Composio this turn",
            backend_kind,
            namespaced,
        )
        return []

    with _tools_cache_lock:
        _tools_cache[cache_key] = _CachedTools(
            tools=tools,
            expires_at=now + s.composio_mcp_url_ttl_seconds,
        )
    return tools


def reset_cache_for_tests() -> None:
    """Wipe the tools cache. ONLY for tests."""
    with _tools_cache_lock:
        _tools_cache.clear()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _resolve_ctx() -> RequestContext | None:
    """Build a RequestContext from per-stream contextvars, or return None."""
    try:
        from datetime import UTC, datetime

        from pocketpaw_ee.cloud.chat.agent_service import (
            current_user_id,
            current_workspace_id,
        )
    except ImportError:
        return None

    user_id = current_user_id()
    if not user_id:
        return None
    return RequestContext(
        user_id=user_id,
        workspace_id=current_workspace_id(),
        request_id="composio-provider",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


def _build_session_and_tools(
    backend_kind: str, namespaced_user_id: str, settings: Settings
) -> list[Any]:
    """Build a fresh Composio session for the backend + user, return tools.

    Each provider has its own ``Composio`` instance because the
    provider is bound at construction time. We could share a singleton
    per backend_kind but the SDK is light enough that re-init per
    cache-miss is fine (and avoids races with concurrent provider
    swaps).
    """
    composio_cls, provider_cls = _provider_class_for(backend_kind)

    api_key = settings.composio_api_key
    base_url = settings.composio_base_url

    # Composio's SDK reads ``COMPOSIO_API_KEY`` from the env in several
    # internal code paths (not just the ctor kwarg). Setting the env
    # var here as a fallback guarantees the SDK sees the key even when
    # something internal bypasses our kwarg.
    import os as _os

    if api_key:
        _os.environ.setdefault("COMPOSIO_API_KEY", api_key)

    kwargs: dict[str, Any] = {"provider": provider_cls()}
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url

    composio_client = composio_cls(**kwargs)

    # Use ``c.tools.get(user_id, toolkits=[...])`` — the "direct tools"
    # path — instead of ``c.create(user_id).tools()``. The latter
    # returns a Tool Router session that ships only 6 *meta-tools*
    # (``COMPOSIO_SEARCH_TOOLS``, ``COMPOSIO_MANAGE_CONNECTIONS``, …),
    # which is a discovery-based pattern designed for agents trained to
    # use it. Most production LLMs hallucinate other instructions when
    # given meta-tools without concrete ones, so we surface concrete
    # tools (``GMAIL_SEND_EMAIL``, ``GMAIL_FETCH_EMAILS``, …) directly.
    #
    # Requires ``composio_toolkits`` to be non-empty — otherwise we'd
    # surface all 200+ integrations and blow context budgets. Failing
    # loud here is friendlier than silently sending 0 tools.
    toolkits = list(settings.composio_toolkits)
    if not toolkits:
        logger.warning(
            "Composio: no toolkits configured. Set POCKETPAW_COMPOSIO_TOOLKITS=gmail,"
            "slack,... to expose concrete tools to the agent. Agent will run with no "
            "Composio tools this turn."
        )
        return []

    # ``composio.tools.get`` paginates alphabetically across the
    # combined toolkit set, so a single call with ``toolkits=[a,b,c]``
    # can return ~200 entries from the first letter alone. Fetch
    # per-toolkit so every requested integration is represented.
    # 50 tools/toolkit keeps multi-toolkit setups under the typical
    # LLM tool-schema budget (Claude historically caps ~128). Users
    # who need the full surface of a single toolkit should narrow
    # ``composio_toolkits`` to that one toolkit.
    per_toolkit_limit = 50
    combined: list[Any] = []
    seen_names: set[str] = set()
    for toolkit in toolkits:
        try:
            tk_tools = composio_client.tools.get(
                user_id=namespaced_user_id, toolkits=[toolkit], limit=per_toolkit_limit
            )
        except Exception:  # noqa: BLE001
            logger.warning("Composio: failed to fetch toolkit %r — skipping", toolkit)
            continue
        for t in tk_tools or []:
            name = getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)
            if name and name in seen_names:
                continue
            if name:
                seen_names.add(name)
            combined.append(t)

    # Append Composio's search-flow meta-tools as a discovery fallback.
    # The direct-tools surface above caps at ``per_toolkit_limit`` per
    # toolkit and paginates alphabetically — for big toolkits (github
    # has 50+ actions) the cap can hide a specific action the agent
    # needs (the bug: agent couldn't find GITHUB_SEARCH_ISSUES_AND_PULL_REQUESTS
    # because the harness deferred the per-letter pagination batch).
    # Giving the agent SEARCH / GET_SCHEMAS / MULTI_EXECUTE means when
    # the direct tool index misses, it can ask Composio's own (more
    # reliable) search to find and load the right action. Connection
    # management is intentionally NOT included — we own that flow via
    # ``connection_tool.py`` and don't want the model fragmenting between
    # two auth paths.
    meta_slugs = [
        "COMPOSIO_SEARCH_TOOLS",
        "COMPOSIO_GET_TOOL_SCHEMAS",
        "COMPOSIO_MULTI_EXECUTE_TOOL",
    ]
    try:
        meta_tools = composio_client.tools.get(user_id=namespaced_user_id, tools=meta_slugs)
    except Exception:  # noqa: BLE001
        # Meta-tool fetch failure is non-fatal: direct tools still work,
        # we just lose the discovery fallback for this turn.
        logger.warning(
            "Composio: meta-tools fetch failed — agent runs without search fallback this turn",
            exc_info=True,
        )
        meta_tools = []
    for t in meta_tools or []:
        name = getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)
        if name and name in seen_names:
            continue
        if name:
            seen_names.add(name)
        combined.append(t)

    # Append the connection-initiation tool. ``composio.tools.get``
    # returns executable tools for already-connected accounts; it does
    # not include a way to start the OAuth flow for a NEW account. Without
    # this addition, when Composio returns ``ConnectedAccountNotFound``
    # the agent has no tool to call next — it would just tell the user
    # "I can't authorize" (the bug we just shipped on top of).
    connection_tool = _build_initiate_connection_tool(backend_kind, composio_client)
    if connection_tool is not None:
        combined.append(connection_tool)

    # Append the identity-verification tool. Composio's connected_accounts
    # API doesn't expose the underlying OAuth identity (GitHub login,
    # Gmail address) in a uniform way — the documented pattern is a
    # per-toolkit "who am I" probe. After ``initiate_connection`` and
    # the user authorizes, the agent calls ``verify_connection(toolkit)``
    # to learn whose account was actually authorized + surface a
    # confirmation back to the user. Tripwire on re-authorization.
    verify_tool = _build_verify_connection_tool(backend_kind, composio_client)
    if verify_tool is not None:
        combined.append(verify_tool)

    return combined


def _build_initiate_connection_tool(backend_kind: str, composio_client: Any) -> Any | None:
    """Build the per-backend wrapper for ``initiate_connection_sync``.

    Returns a tool object shaped for the target backend's tool surface,
    or ``None`` if we don't have a wrapper for that backend yet (the
    agent will still run with its concrete-tool surface; it just can't
    initiate new connections from chat).
    """
    from pocketpaw_ee.cloud.composio.connection_tool import initiate_connection_sync

    if backend_kind == BACKEND_CLAUDE_SDK:
        try:
            from claude_agent_sdk import tool
        except ImportError:
            return None

        @tool(
            "initiate_connection",
            (
                "Start a fresh OAuth/Connect flow for a Composio toolkit "
                "(gmail, slack, github, googlecalendar, googledrive, …). Call "
                "this when a Composio tool returned ConnectedAccountNotFound "
                "or any 'needs auth' / 'not connected' error. Returns a "
                "redirect_url the user opens in a new browser tab to authorize. "
                "After the user authorizes, retry the original tool call."
            ),
            {
                "type": "object",
                "properties": {
                    "toolkit": {
                        "type": "string",
                        "description": (
                            "Toolkit slug to authorize (lowercase). Examples: "
                            "'gmail', 'slack', 'github', 'googlecalendar', "
                            "'googledrive'."
                        ),
                    },
                },
                "required": ["toolkit"],
                "additionalProperties": False,
            },
        )
        async def initiate_connection(args: dict[str, Any]) -> dict[str, Any]:
            import asyncio
            import json

            ctx = _resolve_ctx()
            if ctx is None:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": "initiate_connection requires an active chat stream.",
                        }
                    ],
                    "is_error": True,
                }
            from pocketpaw_ee.cloud.composio import service as svc

            try:
                user_id = str(svc.composio_user_id(ctx))
            except Exception as exc:  # noqa: BLE001
                return {
                    "content": [{"type": "text", "text": f"user id resolution failed: {exc}"}],
                    "is_error": True,
                }

            toolkit_slug = str(args.get("toolkit", "")).strip().lower()
            if not toolkit_slug:
                return {
                    "content": [{"type": "text", "text": "toolkit argument is required"}],
                    "is_error": True,
                }

            result = await asyncio.to_thread(
                initiate_connection_sync,
                composio_client,
                user_id=user_id,
                toolkit_slug=toolkit_slug,
            )
            return {"content": [{"type": "text", "text": json.dumps(result)}]}

        return initiate_connection

    # TODO: deep_agents / google_adk / openai_agents wrappers. For now
    # other backends just don't get the initiate-connection tool —
    # the agent runs without auth-initiation; admin can fall back to
    # connecting accounts via the Composio dashboard. The cloud chat
    # default is claude_agent_sdk per the agent doc, so this covers
    # the common case.
    return None


def _build_verify_connection_tool(backend_kind: str, composio_client: Any) -> Any | None:
    """Build the per-backend wrapper for the identity-verification flow.

    The agent calls this after ``initiate_connection`` succeeds and the
    user returns from authorizing. Probes the toolkit's "who am I"
    action and upserts the result, with tripwire on identity change.
    Returns ``None`` for backends we don't have a wrapper for yet.
    """
    from pocketpaw_ee.cloud.composio.identity import probe_identity_sync

    if backend_kind == BACKEND_CLAUDE_SDK:
        try:
            from claude_agent_sdk import tool
        except ImportError:
            return None

        @tool(
            "verify_connection",
            (
                "Verify which external account a user authorized via Composio. "
                "Call IMMEDIATELY after the user clicks the redirect URL from "
                "initiate_connection and returns to chat. Returns the connected "
                "account's external identity (GitHub login, Gmail address, "
                "etc.). Surface the result to the user verbatim — they need "
                "to know which account they connected, especially if they "
                "have multiple. If status is 'mismatch', the user "
                "re-authorized as a DIFFERENT account than the one previously "
                "stored — ask them to confirm before retrying the original tool."
            ),
            {
                "type": "object",
                "properties": {
                    "toolkit": {
                        "type": "string",
                        "description": (
                            "Toolkit slug whose identity to probe. Same slug "
                            "passed to initiate_connection (e.g. 'gmail', 'github')."
                        ),
                    },
                },
                "required": ["toolkit"],
                "additionalProperties": False,
            },
        )
        async def verify_connection(args: dict[str, Any]) -> dict[str, Any]:
            import asyncio
            import json
            from dataclasses import asdict

            ctx = _resolve_ctx()
            if ctx is None:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": "verify_connection requires an active chat stream.",
                        }
                    ],
                    "is_error": True,
                }
            from pocketpaw_ee.cloud.composio import service as svc

            try:
                user_id = str(svc.composio_user_id(ctx))
            except Exception as exc:  # noqa: BLE001
                return {
                    "content": [{"type": "text", "text": f"user id resolution failed: {exc}"}],
                    "is_error": True,
                }

            toolkit_slug = str(args.get("toolkit", "")).strip().lower()
            if not toolkit_slug:
                return {
                    "content": [{"type": "text", "text": "toolkit argument is required"}],
                    "is_error": True,
                }

            try:
                external_identity = await asyncio.to_thread(
                    probe_identity_sync,
                    composio_client,
                    user_id=user_id,
                    toolkit=toolkit_slug,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("composio.verify_connection: probe failed")
                return {
                    "content": [{"type": "text", "text": f"identity probe failed: {exc}"}],
                    "is_error": True,
                }

            try:
                record = await svc.record_connection(
                    ctx, toolkit=toolkit_slug, external_identity=external_identity
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("composio.verify_connection: record_connection failed")
                return {
                    "content": [{"type": "text", "text": f"record_connection failed: {exc}"}],
                    "is_error": True,
                }

            return {"content": [{"type": "text", "text": json.dumps(asdict(record))}]}

        return verify_connection

    return None


def _provider_class_for(backend_kind: str) -> tuple[Any, Any]:
    """Resolve (Composio class, Provider class) for a backend kind.

    Imports are local so a missing provider package only breaks its
    own backend, not the rest of ``pocketpaw_ee.cloud.composio``.
    """
    from composio import Composio  # type: ignore[import-not-found]

    if backend_kind == BACKEND_CLAUDE_SDK:
        from composio_claude_agent_sdk import (  # type: ignore[import-not-found]
            ClaudeAgentSDKProvider,
        )

        return Composio, ClaudeAgentSDKProvider
    if backend_kind == BACKEND_OPENAI_AGENTS:
        from composio_openai_agents import (  # type: ignore[import-not-found]
            OpenAIAgentsProvider,
        )

        return Composio, OpenAIAgentsProvider
    if backend_kind == BACKEND_GOOGLE_ADK:
        from composio_google_adk import (  # type: ignore[import-not-found]
            GoogleAdkProvider,
        )

        return Composio, GoogleAdkProvider
    if backend_kind == BACKEND_DEEP_AGENTS:
        from composio_langgraph import (  # type: ignore[import-not-found]
            LanggraphProvider,
        )

        return Composio, LanggraphProvider
    raise ValueError(f"Composio: no provider mapping for backend kind {backend_kind!r}")


__all__ = [
    "BACKEND_CLAUDE_SDK",
    "BACKEND_DEEP_AGENTS",
    "BACKEND_GOOGLE_ADK",
    "BACKEND_OPENAI_AGENTS",
    "SUPPORTED_BACKENDS",
    "build_tools_for_backend",
    "reset_cache_for_tests",
]
