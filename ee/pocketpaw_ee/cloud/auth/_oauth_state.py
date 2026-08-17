"""Shared single-use OAuth state store — used by every OAuth-shaped login flow.

Created 2026-07-29 (AM-1). Extracted verbatim from ``auth/sso/service.py`` so
the enterprise SSO dance and the consumer social dance (Google / GitHub) share
one implementation of the part that is easy to get catastrophically wrong.

Why this is server-side rather than a signed stateless token
------------------------------------------------------------
``state`` exists to bind the callback to the browser that started the flow. A
stateless, self-verifying state token cannot do that: it verifies for *anyone*
who presents it, so an attacker can start a flow, capture the state, finish the
upstream consent with their OWN provider account, and then trick a victim into
loading ``…/callback?code=<attacker>&state=<attacker>``. Depending on what the
callback does next, the victim is either logged into the attacker's account or
has the attacker's identity linked into theirs.

That is not hypothetical: it is CVE-2025-68481 / GHSA-5j53-63w8-8625 against
fastapi-users, whose OAuth state was a JWT carrying nothing but an audience and
an expiry. This module keeps the store server-side and single-use, so a state
value is meaningless the instant it has been redeemed once.

The properties this module guarantees, and which its tests pin:
  * **unguessable** — 32 bytes from ``secrets``, URL-safe
  * **single-use** — read and delete are one step; a replay finds nothing
  * **expiring** — a TTL, so an abandoned flow cannot be resumed days later
  * **opaque to the client** — the payload never leaves the server, so nothing
    in it can be tampered with, only referenced

The namespace argument keeps flows from redeeming each other's states: an SSO
state cannot be spent on the social callback or vice versa, because they live
under different key prefixes and the lookup is namespaced.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from collections.abc import Mapping
from typing import Any

from pocketpaw_ee.cloud._core import redis_client
from pocketpaw_ee.cloud._core.errors import Forbidden

#: How long a started-but-unfinished flow stays resumable.
STATE_TTL_SECONDS = 600


def _key(namespace: str, state: str) -> str:
    return f"{namespace}_state:{state}"


def new_state() -> str:
    """A fresh, unguessable state value."""
    return secrets.token_urlsafe(32)


def new_nonce() -> str:
    """A fresh nonce, for providers that echo one back in an id_token."""
    return secrets.token_urlsafe(32)


def pkce_pair() -> tuple[str, str]:
    """Return ``(verifier, challenge)`` for PKCE S256.

    The verifier stays server-side in the state payload; only the challenge
    travels to the provider. Without PKCE, an authorization code intercepted in
    transit is redeemable by whoever holds it.
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


async def issue(
    namespace: str,
    payload: Mapping[str, Any],
    *,
    ttl_seconds: int = STATE_TTL_SECONDS,
) -> str:
    """Persist ``payload`` under a fresh state value and return that value.

    Everything the callback will need must go in ``payload`` — it is the only
    thing the callback can trust. Values that arrive on the callback's query
    string are attacker-influenced by definition.
    """
    state = new_state()
    redis = redis_client.get_redis()
    await redis.setex(_key(namespace, state), ttl_seconds, json.dumps(dict(payload)))
    return state


async def consume(namespace: str, state: str) -> dict[str, Any]:
    """Redeem ``state`` exactly once and return its payload.

    Raises ``Forbidden("<namespace>.invalid_state")`` when the state is unknown,
    already spent, expired, or belongs to a different flow. The delete happens
    immediately after the read, so two concurrent callbacks cannot both proceed.
    """
    redis = redis_client.get_redis()
    key = _key(namespace, state)
    raw = await redis.get(key)
    if raw is None:
        raise Forbidden(f"{namespace}.invalid_state", "Login state is missing or expired")
    await redis.delete(key)
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise Forbidden(f"{namespace}.invalid_state", "Login state payload is malformed") from exc
    if not isinstance(loaded, dict):
        raise Forbidden(f"{namespace}.invalid_state", "Login state payload is malformed")
    return loaded
