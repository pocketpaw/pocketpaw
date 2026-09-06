"""Workspace-scoped API key service.

Issue/list/revoke API keys and resolve ``paw_<prefix><secret>`` bearer
tokens to ``(user_id, workspace_id, scopes)``.

Updated 2026-09-04 - a short-TTL cache in front of the argon2 verify.

Argon2id at pwdlib's recommended parameters is ~30ms of CPU and a 64 MB
allocation per verification, by design. Moving it to a thread stopped it
freezing the event loop, but it did NOT stop it being paid again on every
single call: a script polling one endpoint once a second re-derives the same
hash from the same token 3600 times an hour, and each derivation holds a
worker thread and 64 MB while it runs.

Caching an authentication decision buys that back at the cost of revocation
lag, so the cache is built so the lag applies to as little as possible:

  * Revocation through this module is NOT lagged at all. ``revoke_api_key``
    and ``revoke_keys_for_user_in_workspace`` evict the key's entries as they
    flip the flag, so a revoked key stops working on the next request.
  * Expiry is NOT lagged either. ``expires_at`` is re-checked against the
    clock on every cache hit, not just when the entry was stored, so a key
    that expires part-way through a cached window dies on time.
  * What IS lagged is a revocation performed OUT OF BAND - flipping
    ``revoked`` straight in the database, or in another process, since this
    cache is per-process. That window is ``_DEFAULT_VERIFY_TTL_SECONDS``, and
    ``POCKETPAW_API_KEY_VERIFY_TTL_SECONDS=0`` turns the cache off entirely
    for a deployment that will not accept it.

Only successful verifications are cached. Caching failures would buy nothing
against the attack it looks like it prevents - a brute-force sends a DIFFERENT
token every time, so it would never hit the cache - while adding a second way
for a key to stay dead after it should work again.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
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

#: How long a successful verification stays good without re-deriving argon2.
#: This is the ONLY window in which an out-of-band revocation is not honoured
#: (see the module docstring); revoking through this module evicts immediately.
_DEFAULT_VERIFY_TTL_SECONDS = 30.0
_VERIFY_TTL_ENV = "POCKETPAW_API_KEY_VERIFY_TTL_SECONDS"
_VERIFY_CACHE_MAX = 512
_verify_ttl_override: float | None = None


@dataclass(frozen=True, slots=True)
class _VerifiedKey:
    """One cached successful verification.

    ``expires_at`` rides along so a hit can re-check it against the clock -
    the entry proves the secret matched, not that the key is still in date.
    """

    key_id: str
    owner_user_id: str
    workspace: str
    scopes: tuple[str, ...]
    expires_at: datetime | None
    verified_at: float  # monotonic


#: token sha256 -> the verification it produced. Keyed on a digest of the
#: presented token rather than the token so the plaintext secret is not held
#: in a long-lived process map.
_verify_cache: OrderedDict[str, _VerifiedKey] = OrderedDict()


def generate_key() -> tuple[str, str, str]:
    """Mint a key. Returns ``(full_key, prefix, hashed_secret)``.

    Synchronous, and ``.hash()`` costs the same ~30ms of CPU and 64 MB that
    ``verify`` does. Anything on an async path wants ``generate_key_async``
    instead; this stays for sync callers and for the thread to run.
    """
    secret = secrets.token_hex(_KEY_BYTES)
    prefix = secret[:_PREFIX_LEN]
    full_key = f"paw_{secret}"
    hashed = _password_hash.hash(secret)
    return full_key, prefix, hashed


async def generate_key_async() -> tuple[str, str, str]:
    """``generate_key`` on a worker thread.

    Minting is rare, so this is not about throughput - it is that the hash is
    the same blocking 30ms burn as the verify, and one process serving every
    request cannot afford to take it on the loop just because it happens
    rarely. One admin creating a key should not stall every WebSocket frame
    and SSE chunk in flight.
    """
    return await asyncio.to_thread(generate_key)


async def create_api_key(
    *,
    workspace_id: str,
    owner_user_id: str,
    name: str,
    scopes: list[str],
    expires_at: datetime | None = None,
) -> tuple[APIKey, str]:
    """Insert a new API key. Returns the doc and the plaintext (shown once)."""
    full_key, prefix, hashed = await generate_key_async()
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
    # Evict BEFORE returning, and unconditionally: an already-revoked doc can
    # still have a live cache entry if the flag was flipped out of band, and a
    # revoke that leaves the key working for another 30 seconds is not a
    # revoke. This is what keeps the cache's staleness window off the path
    # that actually matters.
    _invalidate_cached_key(str(doc.id))
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
        _invalidate_cached_key(str(doc.id))
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


def _verify_ttl_seconds() -> float:
    """Cache lifetime in seconds. ``0`` disables the cache in both directions.

    Read from the environment once and memoised: this is on the hot path for
    every API-key request, and a deployment does not change its mind about the
    TTL while running.
    """
    global _verify_ttl_override
    if _verify_ttl_override is None:
        raw = os.getenv(_VERIFY_TTL_ENV, "").strip()
        if not raw:
            _verify_ttl_override = _DEFAULT_VERIFY_TTL_SECONDS
        else:
            try:
                _verify_ttl_override = max(0.0, float(raw))
            except ValueError:
                logger.warning(
                    "%s=%r is not a number; using the %.0fs default",
                    _VERIFY_TTL_ENV,
                    raw,
                    _DEFAULT_VERIFY_TTL_SECONDS,
                )
                _verify_ttl_override = _DEFAULT_VERIFY_TTL_SECONDS
    return _verify_ttl_override


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _cached_verification(digest: str, now_monotonic: float, now: datetime) -> _VerifiedKey | None:
    """Return a still-valid cached verification, or ``None``.

    Two independent reasons to miss, and they are not the same clock. The TTL
    is monotonic (how long ago we checked the secret); the expiry is wall time
    (whether the key is still in date). A long-lived cache entry for a key
    that expired ten seconds ago must not authenticate anybody, so the expiry
    is re-checked HERE rather than only at insert.
    """
    ttl = _verify_ttl_seconds()
    if ttl <= 0:
        return None
    entry = _verify_cache.get(digest)
    if entry is None:
        return None
    if now_monotonic - entry.verified_at >= ttl:
        _verify_cache.pop(digest, None)
        return None
    if entry.expires_at is not None:
        exp = entry.expires_at if entry.expires_at.tzinfo else entry.expires_at.replace(tzinfo=UTC)
        if exp <= now:
            _verify_cache.pop(digest, None)
            return None
    _verify_cache.move_to_end(digest)
    return entry


def _cache_verification(digest: str, doc: APIKey, now_monotonic: float) -> None:
    """Store a successful verification, evicting the least recently used."""
    if _verify_ttl_seconds() <= 0:
        return
    _verify_cache[digest] = _VerifiedKey(
        key_id=str(doc.id),
        owner_user_id=doc.owner_user_id,
        workspace=doc.workspace,
        scopes=tuple(doc.scopes),
        expires_at=doc.expires_at,
        verified_at=now_monotonic,
    )
    _verify_cache.move_to_end(digest)
    while len(_verify_cache) > _VERIFY_CACHE_MAX:
        _verify_cache.popitem(last=False)


def _invalidate_cached_key(key_id: str) -> int:
    """Drop every cached verification for one key. Returns how many went.

    Keyed by token digest, so finding a key's entries is a scan. That is the
    right way round: revocation happens a handful of times a day, verification
    thousands of times an hour, and the map is capped at a few hundred entries.
    """
    stale = [digest for digest, entry in _verify_cache.items() if entry.key_id == key_id]
    for digest in stale:
        _verify_cache.pop(digest, None)
    return len(stale)


def _reset_caches_for_tests() -> None:
    global _verify_ttl_override
    _last_used_writes.clear()
    _verify_cache.clear()
    _verify_ttl_override = None


async def resolve_bearer(token: str) -> tuple[str, str, list[str]] | None:
    """Resolve ``paw_<prefix><secret>``.

    Returns ``(owner_user_id, workspace_id, scopes)`` or ``None``.

    Backed by the short-TTL verification cache described in the module
    docstring. A repeat call with the same token inside the window costs one
    dict lookup instead of a Mongo read plus a 30ms argon2 derivation, and is
    still refused once the key expires or is revoked through this module.
    """
    if not token.startswith("paw_"):
        return None
    body = token[4:]
    if len(body) < _PREFIX_LEN + 1:
        return None
    prefix = body[:_PREFIX_LEN]
    secret = body

    now = datetime.now(UTC)
    now_monotonic = time.monotonic()

    # Cache hit: no Mongo round trip and no argon2 derivation. The entry only
    # ever records that THIS token verified against THIS key, so everything
    # that can change without the token changing - revocation, expiry - is
    # still enforced: expiry inside _cached_verification against the clock,
    # revocation by eviction at the point of revoke.
    cached = _cached_verification(_token_digest(token), now_monotonic, now)
    if cached is not None:
        return cached.owner_user_id, cached.workspace, list(cached.scopes)

    doc = await APIKey.find_one(
        APIKey.prefix == prefix,
        APIKey.revoked == False,  # noqa: E712
    )
    if doc is None:
        return None

    # Cheap expiry check first — skips the ~30ms argon2 verify when the
    # key is already dead.
    if doc.expires_at is not None:
        exp = doc.expires_at if doc.expires_at.tzinfo else doc.expires_at.replace(tzinfo=UTC)
        if exp <= now:
            return None

    # Argon2id at pwdlib's recommended parameters is ~30ms of CPU and a 64 MB
    # allocation, by design. That cost is fine; running it INLINE on the event
    # loop was not. This is one process serving every request, so a synchronous
    # 30ms burn here freezes every other in-flight request, WebSocket frame and
    # SSE chunk for those 30ms — not just the caller's. Sustained API-key
    # traffic therefore caps total server throughput around 1/0.03 ≈ 33 req/s
    # regardless of how trivial the endpoint being called is.
    #
    # to_thread hands it to the default executor so the loop keeps serving.
    # The verify stays exactly as strict; only where it runs has changed.
    try:
        result = await asyncio.to_thread(_password_hash.verify, secret, doc.hashed_secret)
    except Exception:
        return None
    if isinstance(result, tuple):
        valid = bool(result[0])
    else:
        valid = bool(result)
    if not valid:
        return None

    _cache_verification(_token_digest(token), doc, now_monotonic)

    if _should_write_last_used(str(doc.id), now_monotonic):
        try:
            doc.last_used_at = now
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            logger.debug("last_used_at update failed: %s", exc)

    return doc.owner_user_id, doc.workspace, list(doc.scopes)


__all__ = [
    "create_api_key",
    "generate_key",
    "generate_key_async",
    "list_api_keys",
    "resolve_bearer",
    "revoke_api_key",
    "revoke_keys_for_user_in_workspace",
    "_expires_in_days",
    "_reset_caches_for_tests",
]
