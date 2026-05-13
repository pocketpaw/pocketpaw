"""Composio service — session factory, user_id namespacing, toolkit discovery.

Module-level ``async def`` functions per the ee/cloud entity convention
(Rule 5). State (the process-global Composio client) lives behind a
lazy-init helper, not on a class.

Boundary: the upstream ``composio`` SDK is imported lazily inside
``_get_client`` so this module is importable in environments that don't
have ``pocketpaw[enterprise]`` installed (test collection in OSS-only
checkouts, doc builds, etc.). Callers should gate on ``is_enabled()``
before invoking any function that touches the SDK.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ee.cloud._core.context import RequestContext
from ee.cloud._core.errors import Internal, ValidationError
from ee.cloud.composio.domain import ComposioUserId
from pocketpaw.config import Settings

logger = logging.getLogger(__name__)


# Process-global Composio client cache. The client holds the api_key and
# is safe to share across requests (it does not carry per-user state —
# per-user identity is the ``user_id`` passed at session-create time).
# An ``asyncio.Lock`` guards against the thundering-herd at first use,
# where N concurrent requests would otherwise each call the SDK init.
_client: object | None = None
_client_lock: asyncio.Lock = asyncio.Lock()


def is_enabled(settings: Settings | None = None) -> bool:
    """True when Composio is fully configured (api_key + enterprise_id).

    The ``Settings`` validator already enforces ``api_key →
    enterprise_id``; this is a cheap helper for the call sites that
    just want a yes/no before deciding whether to inject the MCP
    server. Accepts an optional ``Settings`` so callers that already
    have one don't pay the ``Settings.load()`` cost twice.
    """
    s = settings or Settings.load()
    return bool(s.composio_api_key and s.composio_enterprise_id)


def composio_user_id(ctx: RequestContext, settings: Settings | None = None) -> ComposioUserId:
    """Build the namespaced Composio user_id for the request.

    Format: ``f"{enterprise_id}:{user_id}"``. Constructed via the
    ``ComposioUserId`` value object so the tenancy invariants live
    in one place (domain), not scattered across f-strings.
    """
    s = settings or Settings.load()
    if not s.composio_enterprise_id:
        raise ValidationError(
            "composio.disabled",
            "Composio is not configured (composio_enterprise_id missing)",
        )
    if not ctx.user_id:
        raise ValidationError("composio.user_id_missing", "RequestContext.user_id is empty")
    return ComposioUserId(enterprise_id=s.composio_enterprise_id, user_id=ctx.user_id)


async def _get_client(settings: Settings | None = None) -> object:
    """Lazy-init the process-global Composio client.

    Cached because the client init touches network / FS in some SDK
    versions and per-request setup would dominate latency for cheap
    tool calls. The client is a-tenant-agnostic singleton — per-user
    identity is supplied at session-create time, not client-init time.
    """
    global _client
    if _client is not None:
        return _client

    s = settings or Settings.load()
    if not is_enabled(s):
        raise ValidationError(
            "composio.disabled",
            "Composio is not configured (composio_api_key + composio_enterprise_id required)",
        )

    async with _client_lock:
        if _client is not None:  # double-checked locking
            return _client
        try:
            from composio import Composio  # type: ignore[import-not-found]
        except ImportError as exc:
            raise Internal(
                "composio.sdk_missing",
                "composio SDK not installed (pip install 'pocketpaw[enterprise]')",
            ) from exc
        # ``Composio()`` can perform blocking I/O on init in some SDK
        # versions; run it on the default executor so we don't block
        # the event loop. ``base_url`` is None for Composio cloud.
        client = await asyncio.to_thread(
            _build_client_sync, Composio, s.composio_api_key, s.composio_base_url
        )
        _client = client
        return _client


def _build_client_sync(composio_cls: Any, api_key: str | None, base_url: str | None) -> object:
    """Sync Composio() constructor — separated for ``asyncio.to_thread``."""
    if base_url:
        return composio_cls(api_key=api_key, base_url=base_url)
    return composio_cls(api_key=api_key)


async def list_available_toolkits(settings: Settings | None = None) -> list[str]:
    """Return the full list of toolkit slugs available on the Composio account.

    Admin-discovery helper for the fail-closed allow-list — operators
    inspect this to decide what to put in ``POCKETPAW_COMPOSIO_TOOLKITS``
    rather than spelunking the Composio docs. Returns slugs (e.g.
    ``"gmail"``, ``"slack"``), not display names.

    NOT cached: toolkit availability changes when Composio adds new
    integrations or an admin disables one upstream. Callers that
    want caching should wrap this themselves.
    """
    s = settings or Settings.load()
    client = await _get_client(s)
    try:
        toolkits = await asyncio.to_thread(_list_toolkits_sync, client)
    except Exception as exc:  # noqa: BLE001
        raise Internal("composio.toolkit_list_failed", "Failed to list Composio toolkits") from exc
    return toolkits


def _list_toolkits_sync(client: Any) -> list[str]:
    """Sync toolkit-list call — separated for ``asyncio.to_thread``.

    ``client.toolkits.list()`` returns either a dict
    (``{"items": [...]}``) or a pydantic model with an ``items``
    attribute depending on minor SDK version. We extract defensively
    so ``dict.items`` (the builtin method) doesn't get mistaken for
    the list. Each item has a ``slug`` (e.g. ``"gmail"``).

    The response is paginated; for v1 we surface only the first page —
    admins can call upstream directly if they need the full catalog.
    """
    response = client.toolkits.list()
    items: Any
    if isinstance(response, dict):
        items = response.get("items") or []
    else:
        items = getattr(response, "items", None) or []
    slugs: list[str] = []
    for tk in items:
        slug = (tk.get("slug") if isinstance(tk, dict) else getattr(tk, "slug", None)) or (
            tk.get("name") if isinstance(tk, dict) else getattr(tk, "name", None)
        )
        if slug:
            slugs.append(str(slug))
    return slugs


def reset_client_cache_for_tests() -> None:
    """Reset the process-global client cache. ONLY for tests.

    The pool is fine to share across a real process lifetime, but tests
    that swap settings between cases need to invalidate it to avoid the
    second test seeing the first test's mocked client.
    """
    global _client
    _client = None
