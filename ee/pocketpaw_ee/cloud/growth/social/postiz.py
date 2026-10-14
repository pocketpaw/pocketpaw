# ee/pocketpaw_ee/cloud/growth/social/postiz.py — the HTTP client for the
# self-hosted Postiz instance that holds each brand's connected social accounts.
# Enterprise routes (org creation, connect start) are signed with an HS256 JWT
# over Postiz's JWT_SECRET; public routes take the org's API key as a bare
# ``Authorization`` header. Neither secret may reach a log line or an error
# message: ``__repr__`` hides them and every error text is scrubbed.
# ``resolve_postiz`` returns the installed seam, else a client built from
# settings, else None (connections are off on this deployment).
#
# Telegram has no OAuth: ``connect_state`` returns a bare state code the user
# posts as ``/connect <code>`` where our bot sits, ``telegram_updates`` reads
# those messages straight from the Bot API (token scrubbed from every error), and
# ``complete_connect`` hands Postiz the state plus the chat id. Tests replace the
# Bot API read through ``set_production_telegram_updates``.

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable
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

    async def _enterprise_url(
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
        return resp.text.strip().strip('"')

    async def connect_url(
        self, api_key: str, platform: str, redirect_url: str, refresh_id: str | None = None
    ) -> str:
        url = await self._enterprise_url(api_key, platform, redirect_url, refresh_id)
        if not url.startswith("https://"):
            raise PostizPlatformUnavailable(
                "postiz.platform_unavailable", f"Postiz returned no auth URL for {platform}."
            )
        return url

    async def connect_state(self, api_key: str, platform: str, redirect_url: str) -> str:
        """The bare state code Postiz returns for a provider without OAuth (Telegram)."""
        state = await self._enterprise_url(api_key, platform, redirect_url)
        if not state or state.startswith("https://"):
            raise PostizPlatformUnavailable(
                "postiz.platform_unavailable", f"Postiz returned no connect code for {platform}."
            )
        return state

    async def complete_connect(self, platform: str, state: str, code: str) -> dict[str, Any]:
        resp = await self._request(
            "POST",
            f"/integrations/social-connect/{quote(platform, safe='')}",
            body={"state": state, "code": code, "timezone": "0"},
        )
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not (isinstance(data, dict) and data.get("id") and not data.get("error")):
            raise PostizError(
                "postiz.connect_failed", f"Postiz did not connect the {platform} account."
            )
        return data

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


TelegramUpdatesFn = Callable[..., Awaitable[list[dict[str, Any]]]]

TELEGRAM_API = "https://api.telegram.org"


async def telegram_updates(
    token: str, offset: int | None = None, client: httpx.AsyncClient | None = None
) -> list[dict[str, Any]]:
    """Pending bot updates (messages and channel posts); ``offset`` confirms older ones."""
    params: dict[str, Any] = {"allowed_updates": json.dumps(["message", "channel_post"])}
    if offset is not None:
        params["offset"] = offset
    http = client or httpx.AsyncClient(timeout=20.0)
    try:
        resp = await http.get(f"{TELEGRAM_API}/bot{token}/getUpdates", params=params)
    except httpx.HTTPError as exc:
        raise PostizError(
            "telegram.unreachable", f"Telegram request failed: {exc!r}".replace(token, "***")
        ) from None
    finally:
        if client is None:
            await http.aclose()
    try:
        data = resp.json()
    except ValueError:
        data = None
    if resp.status_code != 200 or not isinstance(data, dict) or not data.get("ok"):
        raise PostizError(
            "telegram.http_error",
            f"Telegram answered {resp.status_code}: {resp.text.replace(token, '***')[:300]}",
        )
    result = data.get("result")
    return [u for u in result if isinstance(u, dict)] if isinstance(result, list) else []


_PRODUCTION: PostizClient | None = None
_TELEGRAM_UPDATES: TelegramUpdatesFn | None = None


def set_production_postiz(client: PostizClient | None) -> None:
    global _PRODUCTION
    _PRODUCTION = client


def set_production_telegram_updates(fn: TelegramUpdatesFn | None) -> None:
    global _TELEGRAM_UPDATES
    _TELEGRAM_UPDATES = fn


def resolve_telegram_updates() -> TelegramUpdatesFn:
    return _TELEGRAM_UPDATES or telegram_updates


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
    "TelegramUpdatesFn",
    "resolve_postiz",
    "resolve_telegram_updates",
    "set_production_postiz",
    "set_production_telegram_updates",
    "sign_jwt",
    "telegram_updates",
]
