# ee/pocketpaw_ee/cloud/growth/social/postiz.py — the HTTP client for the
# self-hosted Postiz instance that holds each brand's connected social accounts.
# Enterprise routes (org creation, OAuth start) are signed with an HS256 JWT over
# Postiz's JWT_SECRET; public routes take the org's API key as a bare
# ``Authorization`` header. Neither secret may reach a log line or an error
# message: ``__repr__`` hides them and every error text is scrubbed.
# ``resolve_postiz`` returns the installed seam, else a client built from
# settings, else None (connections are off on this deployment).

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any
from urllib.parse import quote

import httpx

SAAS_NAME = "PocketPaw"


class PostizError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class PostizPlatformUnavailable(PostizError):
    """Postiz has no app configured for the platform, so it returned no auth URL."""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def sign_jwt(payload: dict[str, Any], secret: str) -> str:
    """An HS256 JWT with no expiry, the shape Postiz's enterprise routes verify."""
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header}.{body}".encode()
    return f"{header}.{body}.{_b64(hmac.digest(secret.encode(), signing_input, hashlib.sha256))}"


class PostizClient:
    def __init__(
        self, base_url: str, jwt_secret: str, client: httpx.AsyncClient | None = None
    ) -> None:
        self._api = f"{base_url.rstrip('/')}/api"
        self._secret = jwt_secret
        self._client = client

    def __repr__(self) -> str:
        return f"PostizClient(api={self._api!r}, jwt_secret='***redacted***')"

    def _scrub(self, text: str, api_key: str | None) -> str:
        for secret in (self._secret, api_key):
            if secret:
                text = text.replace(secret, "***")
        return text

    async def _request(
        self, method: str, path: str, *, body: Any = None, api_key: str | None = None
    ) -> httpx.Response:
        client = self._client or httpx.AsyncClient(timeout=20.0)
        headers = {"Authorization": api_key} if api_key else {}
        try:
            resp = await client.request(method, f"{self._api}{path}", json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise PostizError(
                "postiz.unreachable", self._scrub(f"Postiz request failed: {exc!r}", api_key)
            ) from None
        finally:
            if self._client is None:
                await client.aclose()
        if resp.status_code >= 300:
            raise PostizError(
                "postiz.http_error",
                f"Postiz answered {resp.status_code}: {self._scrub(resp.text, api_key)[:300]}",
            )
        return resp

    def _signed(self, payload: dict[str, Any]) -> dict[str, str]:
        return {"params": sign_jwt(payload, self._secret)}

    async def create_org(self, external_id: str, name: str) -> tuple[str, str]:
        """Create a Postiz org; returns ``(org_id, api_key)``. Not idempotent."""
        resp = await self._request(
            "POST",
            "/enterprise/create-user",
            body=self._signed(
                {"id": external_id, "name": name, "saasName": SAAS_NAME, "email": ""}
            ),
        )
        try:
            data = resp.json()
        except ValueError:
            data = None
        org_id = data.get("id") if isinstance(data, dict) else None
        api_key = data.get("apiKey") if isinstance(data, dict) else None
        if not (isinstance(org_id, str) and org_id and isinstance(api_key, str) and api_key):
            raise PostizError("postiz.create_failed", "Postiz did not create the organisation.")
        return org_id, api_key

    async def connect_url(
        self, api_key: str, platform: str, redirect_url: str, refresh_id: str | None = None
    ) -> str:
        payload: dict[str, Any] = {
            "redirectUrl": redirect_url,
            "apiKey": api_key,
            "provider": platform,
            "webhookUrl": "",
        }
        if refresh_id:
            payload["refreshId"] = refresh_id
        resp = await self._request("POST", "/enterprise/url", body=self._signed(payload))
        url = resp.text.strip().strip('"')
        if not url.startswith("https://"):
            raise PostizPlatformUnavailable(
                "postiz.platform_unavailable", f"Postiz returned no auth URL for {platform}."
            )
        return url

    async def list_integrations(self, api_key: str) -> list[dict[str, Any]]:
        resp = await self._request("GET", "/public/v1/integrations", api_key=api_key)
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, list):
            raise PostizError("postiz.bad_response", "Postiz returned an unexpected account list.")
        return [item for item in data if isinstance(item, dict)]

    async def delete_integration(self, api_key: str, integration_id: str) -> None:
        await self._request(
            "DELETE", f"/public/v1/integrations/{quote(integration_id, safe='')}", api_key=api_key
        )


_PRODUCTION: PostizClient | None = None


def set_production_postiz(client: PostizClient | None) -> None:
    global _PRODUCTION
    _PRODUCTION = client


def resolve_postiz() -> PostizClient | None:
    if _PRODUCTION is not None:
        return _PRODUCTION
    from pocketpaw.config import get_settings

    settings = get_settings()
    url = (settings.postiz_url or "").strip()
    secret = (settings.postiz_jwt_secret or "").strip()
    if not (url and secret):
        return None
    return PostizClient(url, secret)


__all__ = [
    "PostizClient",
    "PostizError",
    "PostizPlatformUnavailable",
    "resolve_postiz",
    "set_production_postiz",
    "sign_jwt",
]
