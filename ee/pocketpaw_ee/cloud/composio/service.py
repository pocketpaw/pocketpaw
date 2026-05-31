"""Composio service — session factory, user_id namespacing, toolkit discovery.

Module-level ``async def`` functions per the ee/pocketpaw_ee/cloud entity convention
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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pocketpaw.config import Settings
from pocketpaw_ee.cloud._core.context import RequestContext
from pocketpaw_ee.cloud._core.errors import Internal, ValidationError
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import (
    ComposioConnectionMismatch,
    ComposioConnectionVerified,
)
from pocketpaw_ee.cloud.composio.domain import ComposioUserId

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


@dataclass(frozen=True, slots=True)
class ConnectionRecord:
    """Result of ``record_connection`` — what the caller surfaces to the agent.

    ``status`` is one of:
        * ``"verified"`` — first time recording this identity, or a fresh
          probe matched the previously stored one. Caller renders
          "Connected as X. Continue?".
        * ``"mismatch"`` — probe returned a different identity than the
          stored one. Caller renders "Connected as X — this differs from
          previously verified Y. Confirm?" and waits for the user to
          explicitly accept the change before retrying the original tool.
        * ``"unverified"`` — probe returned ``None`` (toolkit lacks a
          registered probe, or the probe call failed). Caller renders
          "Connected to <toolkit> — identity verification unavailable".
    """

    status: str  # "verified" | "mismatch" | "unverified"
    toolkit: str
    external_identity: str | None
    previous_identity: str | None = None


async def record_connection(
    ctx: RequestContext,
    *,
    toolkit: str,
    external_identity: str | None,
) -> ConnectionRecord:
    """Upsert the verified identity for ``(workspace, user, toolkit)``.

    ``external_identity=None`` means the probe couldn't resolve an
    identity (no probe registered, call failed). We still bump
    ``last_verified_at`` so the chat can confirm something was probed,
    but we don't store a value for tripwire comparison.

    Tripwire: when the new identity is non-empty and differs from the
    stored one, we DO NOT overwrite. The mismatch is recorded
    (``mismatch_count``, ``last_mismatch_identity``, ``last_mismatch_at``)
    and surfaced via ``ComposioConnectionMismatch``. The agent layer
    blocks until the user explicitly confirms the change, at which
    point a second call (with ``external_identity`` matching) succeeds.

    Multi-tenancy: ``workspace_id`` is required on ``ctx``; the find
    filters on it. Two users in different workspaces can each have their
    own connected GitHub without collision.
    """
    if not ctx.workspace_id:
        raise ValidationError("composio.workspace_required", "RequestContext.workspace_id is empty")
    if not ctx.user_id:
        raise ValidationError("composio.user_id_missing", "RequestContext.user_id is empty")
    toolkit_slug = toolkit.strip().lower()
    if not toolkit_slug:
        raise ValidationError("composio.toolkit_required", "toolkit slug is required")

    from pocketpaw_ee.cloud.models.composio_connection import ComposioConnection

    now = datetime.now(UTC)
    doc = await ComposioConnection.find_one(
        ComposioConnection.workspace == ctx.workspace_id,
        ComposioConnection.paw_user_id == ctx.user_id,
        ComposioConnection.toolkit == toolkit_slug,
    )

    if doc is None:
        doc = ComposioConnection(
            workspace=ctx.workspace_id,
            paw_user_id=ctx.user_id,
            toolkit=toolkit_slug,
            external_identity=external_identity,
            last_verified_at=now,
        )
        await doc.insert()
        status = "verified" if external_identity else "unverified"
        await emit(
            ComposioConnectionVerified(
                data={
                    "workspace_id": ctx.workspace_id,
                    "user_id": ctx.user_id,
                    "toolkit": toolkit_slug,
                    "external_identity": external_identity,
                    "first_time": True,
                }
            )
        )
        return ConnectionRecord(
            status=status,
            toolkit=toolkit_slug,
            external_identity=external_identity,
        )

    stored = doc.external_identity
    if external_identity is None:
        # Probe couldn't resolve — bump verified_at as a heartbeat but
        # don't pretend we re-confirmed the identity.
        doc.last_verified_at = now
        await doc.save()
        return ConnectionRecord(
            status="unverified",
            toolkit=toolkit_slug,
            external_identity=stored,
        )

    if stored is None or stored == external_identity:
        # First-time confirmation OR a matching re-probe. Safe to update.
        doc.external_identity = external_identity
        doc.last_verified_at = now
        await doc.save()
        await emit(
            ComposioConnectionVerified(
                data={
                    "workspace_id": ctx.workspace_id,
                    "user_id": ctx.user_id,
                    "toolkit": toolkit_slug,
                    "external_identity": external_identity,
                    "first_time": stored is None,
                }
            )
        )
        return ConnectionRecord(
            status="verified",
            toolkit=toolkit_slug,
            external_identity=external_identity,
        )

    # Tripwire: identity changed. Record the mismatch without
    # overwriting the stored external_identity — the user must confirm.
    doc.mismatch_count += 1
    doc.last_mismatch_identity = external_identity
    doc.last_mismatch_at = now
    await doc.save()
    await emit(
        ComposioConnectionMismatch(
            data={
                "workspace_id": ctx.workspace_id,
                "user_id": ctx.user_id,
                "toolkit": toolkit_slug,
                "stored_identity": stored,
                "probed_identity": external_identity,
                "mismatch_count": doc.mismatch_count,
            }
        )
    )
    return ConnectionRecord(
        status="mismatch",
        toolkit=toolkit_slug,
        external_identity=external_identity,
        previous_identity=stored,
    )


async def confirm_identity_change(
    ctx: RequestContext,
    *,
    toolkit: str,
    external_identity: str,
) -> ConnectionRecord:
    """Accept an identity change the user explicitly confirmed.

    After a ``"mismatch"`` result from ``record_connection``, the chat
    asks the user to confirm. On "yes", this overwrites the stored
    ``external_identity`` so future probes match and re-verify cleanly.
    """
    if not ctx.workspace_id or not ctx.user_id:
        raise ValidationError(
            "composio.workspace_or_user_missing", "RequestContext.workspace_id/user_id is empty"
        )
    toolkit_slug = toolkit.strip().lower()

    from pocketpaw_ee.cloud.models.composio_connection import ComposioConnection

    doc = await ComposioConnection.find_one(
        ComposioConnection.workspace == ctx.workspace_id,
        ComposioConnection.paw_user_id == ctx.user_id,
        ComposioConnection.toolkit == toolkit_slug,
    )
    if doc is None:
        raise ValidationError(
            "composio.connection_not_found",
            f"No prior connection record for toolkit {toolkit_slug!r} — cannot confirm change",
        )

    previous = doc.external_identity
    doc.external_identity = external_identity
    doc.last_verified_at = datetime.now(UTC)
    doc.last_mismatch_identity = None
    doc.last_mismatch_at = None
    await doc.save()
    await emit(
        ComposioConnectionVerified(
            data={
                "workspace_id": ctx.workspace_id,
                "user_id": ctx.user_id,
                "toolkit": toolkit_slug,
                "external_identity": external_identity,
                "first_time": False,
                "confirmed_change_from": previous,
            }
        )
    )
    return ConnectionRecord(
        status="verified",
        toolkit=toolkit_slug,
        external_identity=external_identity,
        previous_identity=previous,
    )


def reset_client_cache_for_tests() -> None:
    """Reset the process-global client cache. ONLY for tests.

    The pool is fine to share across a real process lifetime, but tests
    that swap settings between cases need to invalidate it to avoid the
    second test seeing the first test's mocked client.
    """
    global _client
    _client = None
