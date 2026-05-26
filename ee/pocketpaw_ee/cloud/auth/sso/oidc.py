"""OIDC primitives — discovery, token exchange, userinfo, id_token verify.

Raw httpx + PyJWT, no authlib. The discovery cache is in-process (1h)
and keyed by issuer; tests reset it via ``_clear_discovery_cache``.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import jwt as pyjwt

PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "okta": {
        "discovery_suffix": "/.well-known/openid-configuration",
        "scopes": ["openid", "email", "profile"],
    },
    "google": {
        "issuer": "https://accounts.google.com",
        "discovery_suffix": "/.well-known/openid-configuration",
        "scopes": ["openid", "email", "profile"],
    },
    "azure": {
        "discovery_suffix": "/v2.0/.well-known/openid-configuration",
        "scopes": ["openid", "email", "profile"],
    },
    "generic_oidc": {
        "discovery_suffix": "/.well-known/openid-configuration",
        "scopes": ["openid", "email", "profile"],
    },
}

_DISCOVERY_TTL_SECONDS = 3600
_discovery_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _discovery_url(issuer: str, provider_key: str) -> str:
    preset = PROVIDER_PRESETS.get(provider_key, PROVIDER_PRESETS["generic_oidc"])
    suffix = preset["discovery_suffix"]
    base = issuer.rstrip("/")
    return f"{base}{suffix}"


async def discover(issuer: str, provider_key: str) -> dict[str, Any]:
    cache_key = f"{provider_key}|{issuer}"
    now = time.monotonic()
    cached = _discovery_cache.get(cache_key)
    if cached and (now - cached[0]) < _DISCOVERY_TTL_SECONDS:
        return cached[1]
    url = _discovery_url(issuer, provider_key)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        doc = resp.json()
    _discovery_cache[cache_key] = (now, doc)
    return doc


async def exchange_code(
    token_endpoint: str,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    *,
    code_verifier: str | None = None,
) -> dict[str, Any]:
    data: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }
    if code_verifier:
        data["code_verifier"] = code_verifier
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(token_endpoint, data=data)
        resp.raise_for_status()
        return resp.json()


async def fetch_userinfo(userinfo_endpoint: str, access_token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            userinfo_endpoint,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()


def parse_id_token(id_token: str, jwks_uri: str, audience: str) -> dict[str, Any]:
    """Verify RS256 signature + audience + expiry, return claims dict."""
    jwks_client = pyjwt.PyJWKClient(jwks_uri)
    signing_key = jwks_client.get_signing_key_from_jwt(id_token).key
    return pyjwt.decode(
        id_token,
        signing_key,
        algorithms=["RS256"],
        audience=audience,
    )


def _clear_discovery_cache() -> None:
    _discovery_cache.clear()
