# ee/pocketpaw_ee/integrations/vault.py: encrypted workspace connections.
#
# An ``IntegrationConnection`` is a workspace's grant for one provider account. The
# token bundle is Fernet-encrypted with ``cloud._core.crypto`` (the same helper
# ``sites.site_secrets`` uses) and is only ever decrypted inside this module.
#
# ``get_valid_token`` is THE one refresh path. When the access token is near
# expiry it takes a per-connection lease with one atomic ``find_one_and_update``
# (lease owner + expiry, the outbox claim pattern), re-checks the row (a sibling
# may have just refreshed), refreshes, and writes the new bundle only while it
# still holds the lease. A caller that loses the race waits for the holder's
# result instead of refreshing with a refresh token that is about to be spent.
#
# INVARIANTS A READER MUST NOT BREAK:
#   * The newest refresh token is always persisted: providers that rotate
#     single-use refresh tokens (Airtable, Shopify, Notion, ...) lock the account
#     out if an old one is reused. A provider that does not rotate keeps the old one.
#   * ``InvalidGrant`` flips the connection to ``needs_reconnect`` and notifies the
#     bound sites. Nothing retries a dead grant.
#   * No token in a view, an event, an exception message or a log line.
#
# ``store_grant`` saves a finished connect or resume flow. A resume names the
# connection it continues and updates that row in place, refusing a different
# provider account. Non-secret provider state rides ``TokenGrant.meta`` into
# ``connection.meta``; the view's ``attention`` is the provider's
# ``connection_attention`` hook over the stored row.

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pocketpaw_ee.cloud._core import crypto
from pocketpaw_ee.cloud._core.errors import ConflictError, NotFound
from pocketpaw_ee.integrations.dto import ConnectionView
from pocketpaw_ee.integrations.providers import get_provider
from pocketpaw_ee.integrations.providers.base import InvalidGrant, ProviderError, TokenGrant

logger = logging.getLogger(__name__)

LEASE_SECONDS = 30
# Refresh this long before the provider's stated expiry.
EXPIRY_SKEW = timedelta(seconds=60)
# How long a caller that lost the lease race waits for the holder.
WAIT_SECONDS = 10.0
_POLL_SECONDS = 0.1


class NotConnected(Exception):
    """The connection is missing, revoked or needs a reconnect."""


def _doc():
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection

    return IntegrationConnection


def _utc(value: datetime | None) -> datetime | None:
    """Mongo hands datetimes back naive; they were written as UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _now() -> datetime:
    return datetime.now(UTC)


def _expires_at(grant: TokenGrant, now: datetime) -> datetime | None:
    return now + timedelta(seconds=grant.expires_in) if grant.expires_in else None


def _bundle(grant: TokenGrant, previous_refresh: str = "") -> str:
    return crypto.encrypt_json(
        {
            "access_token": grant.access_token,
            # Rotation: keep whatever the provider sent last; reuse the old one only
            # when this response carried none (the provider does not rotate).
            "refresh_token": grant.refresh_token or previous_refresh,
            "token_type": grant.token_type,
        }
    )


def _tokens(conn: Any) -> dict[str, str]:
    return crypto.decrypt_json(conn.token_ciphertext) if conn.token_ciphertext else {}


def _fresh(conn: Any, now: datetime) -> bool:
    expires = _utc(conn.expires_at)
    return expires is None or expires - EXPIRY_SKEW > now


# ------------------------------------------------------------------ views


def attention(conn: Any) -> str | None:
    """The provider's owner-facing hint for ``conn``; a broken hook reads as none."""
    if conn.status == "revoked":
        return None
    provider = get_provider(conn.provider)
    hook = getattr(provider, "connection_attention", None)
    if hook is None:
        return None
    try:
        text = hook(conn)
    except Exception:  # noqa: BLE001 - a hint must never break the list
        logger.debug("integrations: attention hook failed for %s", conn.provider, exc_info=True)
        return None
    return str(text)[:200] if text else None


def to_view(conn: Any, site_count: int = 0) -> ConnectionView:
    return ConnectionView(
        id=str(conn.id),
        provider=conn.provider,
        account_label=conn.account_label or "",
        status=conn.status if conn.status in ("active", "needs_reconnect") else "revoked",
        scopes=list(conn.scopes or []),
        created_at=_utc(conn.createdAt),
        site_count=site_count,
        attention=attention(conn),
    )


async def get_connection(workspace_id: str, connection_id: str):
    """The connection, or NotFound when missing or in another workspace."""
    from beanie import PydanticObjectId

    try:
        conn = await _doc().get(PydanticObjectId(connection_id))
    except Exception:  # noqa: BLE001 - a malformed id is a missing connection
        conn = None
    if conn is None or not workspace_id or conn.workspace != workspace_id:
        raise NotFound("integration_connection", connection_id)
    return conn


async def list_connections(workspace_id: str) -> list[Any]:
    doc = _doc()
    rows = await doc.find(doc.workspace == workspace_id, doc.status != "revoked").to_list()
    return sorted(rows, key=lambda r: (r.provider, str(r.id)))


# ----------------------------------------------------------------- writes


async def store_grant(
    *,
    workspace_id: str,
    provider: str,
    grant: TokenGrant,
    user_id: str,
    connection_id: str = "",
):
    """Save a fresh grant. Reconnecting the same account updates its row; a resume
    (``connection_id``) updates that row and refuses another account."""
    doc = _doc()
    now = _now()
    conn = None
    if connection_id:
        conn = await get_connection(workspace_id, connection_id)
        if conn.provider != provider or conn.status == "revoked":
            raise ConflictError("integrations.resume_mismatch", "That connection can't resume.")
        if conn.account_ref and conn.account_ref != grant.account_ref:
            raise ConflictError(
                "integrations.resume_mismatch", "That is a different account. Connect it instead."
            )
    elif grant.account_ref:
        conn = await doc.find_one(
            doc.workspace == workspace_id,
            doc.provider == provider,
            doc.account_ref == grant.account_ref,
            doc.status != "revoked",
        )
    if conn is None:
        conn = doc(workspace=workspace_id, provider=provider, created_by=user_id)
    conn.account_label = grant.account_label or conn.account_label or provider
    conn.account_ref = grant.account_ref or conn.account_ref
    conn.scopes = list(grant.scopes)
    conn.token_ciphertext = _bundle(grant)
    conn.expires_at = _expires_at(grant, now)
    conn.meta = {**(getattr(conn, "meta", None) or {}), **dict(grant.meta or {})}
    conn.status = "active"
    conn.last_error = ""
    conn.refresh_lease_owner = None
    conn.refresh_lease_until = None
    await conn.save()
    return conn


async def mark_revoked(conn: Any) -> None:
    conn.status = "revoked"
    conn.token_ciphertext = ""
    conn.expires_at = None
    await conn.save()


# ------------------------------------------------------------ the token path


async def _claim(conn_id: Any, owner: str, now: datetime) -> dict | None:
    coll = _doc().get_pymongo_collection()
    return await coll.find_one_and_update(
        {
            "_id": conn_id,
            "status": "active",
            "$or": [
                {"refresh_lease_until": None},
                {"refresh_lease_until": {"$lte": now}},
            ],
        },
        {
            "$set": {
                "refresh_lease_owner": owner,
                "refresh_lease_until": now + timedelta(seconds=LEASE_SECONDS),
            }
        },
        return_document=True,
    )


async def _finish(conn_id: Any, owner: str, update: dict) -> bool:
    coll = _doc().get_pymongo_collection()
    result = await coll.update_one(
        {"_id": conn_id, "refresh_lease_owner": owner},
        {"$set": {**update, "refresh_lease_owner": None, "refresh_lease_until": None}},
    )
    return getattr(result, "matched_count", 1) > 0


async def _reload(conn_id: Any):
    return await _doc().get(conn_id)


async def get_valid_token(conn: Any) -> str:
    """A usable access token for ``conn``, refreshing it under the lease if needed.

    Raises ``NotConnected`` for a revoked / needs_reconnect / missing connection,
    ``ProviderError`` when the provider fails or the lease holder never finishes."""
    if conn is None or conn.status != "active":
        raise NotConnected("connection is not active")
    now = _now()
    if _fresh(conn, now):
        return _tokens(conn).get("access_token", "")

    owner = uuid.uuid4().hex
    claimed = await _claim(conn.id, owner, now)
    if claimed is None:
        return await _wait_for_holder(conn.id)

    current = await _reload(conn.id)
    if current is None or current.status != "active":
        raise NotConnected("connection is not active")
    if _fresh(current, _now()):
        # A sibling refreshed between our read and our claim.
        await _finish(conn.id, owner, {})
        return _tokens(current).get("access_token", "")

    provider = get_provider(current.provider)
    previous_refresh = _tokens(current).get("refresh_token", "")
    try:
        if provider is None:
            raise ProviderError(f"provider {current.provider} is not available")
        grant = await provider.refresh(previous_refresh)
    except InvalidGrant:
        await _finish(
            conn.id,
            owner,
            {"status": "needs_reconnect", "last_error": "invalid_grant"},
        )
        logger.info("integrations: connection %s needs a reconnect", conn.id)
        await _notify_needs_reconnect(current)
        raise NotConnected("connection needs a reconnect") from None
    except Exception as exc:
        await _finish(conn.id, owner, {"last_error": type(exc).__name__})
        logger.warning("integrations: refresh failed for %s: %s", conn.id, type(exc).__name__)
        raise ProviderError("token refresh failed") from None

    stored = await _finish(
        conn.id,
        owner,
        {
            "token_ciphertext": _bundle(grant, previous_refresh),
            "expires_at": _expires_at(grant, _now()),
            "last_error": "",
            "updatedAt": _now(),
        },
    )
    if not stored:
        # Our lease lapsed mid-refresh and someone else holds it now. The provider
        # already spent the old refresh token, so keep what we got rather than lose
        # it: write it unconditionally. The newest token wins either way.
        coll = _doc().get_pymongo_collection()
        await coll.update_one(
            {"_id": conn.id},
            {
                "$set": {
                    "token_ciphertext": _bundle(grant, previous_refresh),
                    "expires_at": _expires_at(grant, _now()),
                }
            },
        )
    return grant.access_token


async def _wait_for_holder(conn_id: Any) -> str:
    deadline = asyncio.get_running_loop().time() + WAIT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(_POLL_SECONDS)
        current = await _reload(conn_id)
        if current is None or current.status != "active":
            raise NotConnected("connection is not active")
        if current.refresh_lease_owner is None and _fresh(current, _now()):
            return _tokens(current).get("access_token", "")
    raise ProviderError("token refresh is taking too long")


async def _notify_needs_reconnect(conn: Any) -> None:
    try:
        from pocketpaw_ee.integrations import bindings

        await bindings.notify_connection_status(conn, "needs_reconnect")
    except Exception:  # noqa: BLE001 - a missed card update must not mask the error
        logger.debug("integrations: needs_reconnect notify failed", exc_info=True)


def ensure_not_revoked(conn: Any) -> None:
    if conn.status == "revoked":
        raise ConflictError(
            "integrations.connection_revoked", "That connection was removed. Connect again."
        )
