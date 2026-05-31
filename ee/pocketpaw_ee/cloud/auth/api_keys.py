"""Workspace-scoped API key service.

Issue/list/revoke API keys and resolve ``paw_<prefix><secret>`` bearer
tokens to ``(user_id, workspace_id, scopes)``.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta

from beanie import PydanticObjectId
from pwdlib import PasswordHash

from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.models.api_key import APIKey

logger = logging.getLogger(__name__)

_password_hash = PasswordHash.recommended()

_KEY_BYTES = 16  # 32 hex chars
_PREFIX_LEN = 8

_LAST_USED_WRITE_INTERVAL = 60.0
_LAST_USED_LRU_MAX = 1000
_last_used_writes: OrderedDict[str, float] = OrderedDict()


def generate_key() -> tuple[str, str, str]:
    """Mint a key. Returns ``(full_key, prefix, hashed_secret)``."""
    secret = secrets.token_hex(_KEY_BYTES)
    prefix = secret[:_PREFIX_LEN]
    full_key = f"paw_{secret}"
    hashed = _password_hash.hash(secret)
    return full_key, prefix, hashed


async def create_api_key(
    *,
    workspace_id: str,
    owner_user_id: str,
    name: str,
    scopes: list[str],
    expires_at: datetime | None = None,
) -> tuple[APIKey, str]:
    """Insert a new API key. Returns the doc and the plaintext (shown once)."""
    full_key, prefix, hashed = generate_key()
    doc = APIKey(
        workspace=workspace_id,
        owner_user_id=owner_user_id,
        name=name,
        prefix=prefix,
        hashed_secret=hashed,
        scopes=list(scopes),
        expires_at=expires_at,
    )
    await doc.insert()
    return doc, full_key


async def list_api_keys(workspace_id: str) -> list[APIKey]:
    rows = await APIKey.find(
        APIKey.workspace == workspace_id,
        APIKey.revoked == False,  # noqa: E712
    ).to_list()
    rows.sort(key=lambda r: r.created_at, reverse=True)
    return rows


async def revoke_api_key(key_id: str, workspace_id: str) -> APIKey:
    try:
        doc = await APIKey.get(PydanticObjectId(key_id))
    except Exception as exc:
        raise NotFound("api_key", key_id) from exc
    if doc is None or doc.workspace != workspace_id:
        raise NotFound("api_key", key_id)
    if not doc.revoked:
        doc.revoked = True
        await doc.save()
    return doc


async def revoke_keys_for_user_in_workspace(user_id: str, workspace_id: str) -> int:
    """Revoke every active API key owned by ``user_id`` in ``workspace_id``.

    Returns the number of keys flipped. Already-revoked keys are not
    counted. Used by the member-removal cascade.
    """
    rows = await APIKey.find(
        APIKey.owner_user_id == user_id,
        APIKey.workspace == workspace_id,
        APIKey.revoked == False,  # noqa: E712
    ).to_list()
    count = 0
    for doc in rows:
        doc.revoked = True
        await doc.save()
        count += 1
    return count


def _expires_in_days(days: int | None) -> datetime | None:
    if days is None:
        return None
    return datetime.now(UTC) + timedelta(days=days)


def _should_write_last_used(key_id: str, now_monotonic: float) -> bool:
    prev = _last_used_writes.get(key_id)
    if prev is not None and now_monotonic - prev < _LAST_USED_WRITE_INTERVAL:
        return False
    _last_used_writes[key_id] = now_monotonic
    _last_used_writes.move_to_end(key_id)
    while len(_last_used_writes) > _LAST_USED_LRU_MAX:
        _last_used_writes.popitem(last=False)
    return True


def _reset_caches_for_tests() -> None:
    _last_used_writes.clear()


async def resolve_bearer(token: str) -> tuple[str, str, list[str]] | None:
    """Resolve ``paw_<prefix><secret>``.

    Returns ``(owner_user_id, workspace_id, scopes)`` or ``None``.
    """
    if not token.startswith("paw_"):
        return None
    body = token[4:]
    if len(body) < _PREFIX_LEN + 1:
        return None
    prefix = body[:_PREFIX_LEN]
    secret = body

    doc = await APIKey.find_one(
        APIKey.prefix == prefix,
        APIKey.revoked == False,  # noqa: E712
    )
    if doc is None:
        return None

    # Cheap expiry check first — skips the ~30ms argon2 verify when the
    # key is already dead.
    now = datetime.now(UTC)
    if doc.expires_at is not None:
        exp = doc.expires_at if doc.expires_at.tzinfo else doc.expires_at.replace(tzinfo=UTC)
        if exp <= now:
            return None

    # Why: argon2 verify is ~30ms, acceptable for API-key auth path.
    try:
        result = _password_hash.verify(secret, doc.hashed_secret)
    except Exception:
        return None
    if isinstance(result, tuple):
        valid = bool(result[0])
    else:
        valid = bool(result)
    if not valid:
        return None

    if _should_write_last_used(str(doc.id), time.monotonic()):
        try:
            doc.last_used_at = now
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            logger.debug("last_used_at update failed: %s", exc)

    return doc.owner_user_id, doc.workspace, list(doc.scopes)


__all__ = [
    "create_api_key",
    "generate_key",
    "list_api_keys",
    "resolve_bearer",
    "revoke_api_key",
    "revoke_keys_for_user_in_workspace",
    "_expires_in_days",
    "_reset_caches_for_tests",
]
