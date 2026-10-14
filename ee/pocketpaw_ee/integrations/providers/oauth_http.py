# ee/pocketpaw_ee/integrations/providers/oauth_http.py: outbound HTTP for OAuth providers.
#
# The small shared layer the real connect providers (google_sheets, hubspot,
# zoho_crm) use:
#   * ``client()``          one ``httpx.AsyncClient`` per call; tests swap
#                           ``_transport`` for an ``httpx.MockTransport``
#   * ``token_request()``   form POST to a token endpoint; a dead grant
#                           (``invalid_grant`` / ``BAD_REFRESH_TOKEN`` /
#                           ``invalid_code``) raises ``InvalidGrant`` so the vault
#                           flips needs_reconnect. An ``error`` body is a failure
#                           even on a 200 (Zoho answers errors that way)
#   * ``authed()``          an API call with the site's token (``auth_scheme``
#                           defaults to ``Bearer``; Zoho wants ``Zoho-oauthtoken``);
#                           a 401 refreshes once through ``get_token(rejected=...)``
#                           and retries
#   * ``s256()`` / ``client_credentials()`` PKCE challenge and env-read app config
#   * ``untraced()``        suppresses the OpenTelemetry httpx instrumentation
#                           (Logfire) for a request whose URL carries a secret, so
#                           the URL never lands in a span (HubSpot token info)
#
# Nothing here logs or raises with a token or a response body in the message.

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import httpx

from pocketpaw_ee.integrations.providers.base import InvalidGrant, ProviderError

TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_DEAD_GRANT = {"invalid_grant", "bad_refresh_token", "bad_auth_code", "invalid_code"}

# Tests replace this with ``httpx.MockTransport(handler)``.
_transport: httpx.AsyncBaseTransport | None = None


@contextlib.contextmanager
def untraced() -> Iterator[None]:
    """No httpx span inside: Logfire's ``instrument_httpx`` wraps the transport
    class globally, so a per-client opt-out is impossible; the OTel suppress key
    is the seam it honours (``is_http_instrumentation_enabled``)."""
    try:
        from opentelemetry.instrumentation.utils import suppress_http_instrumentation
    except ImportError:  # no OTel installed: nothing traces httpx anyway
        yield
        return
    with suppress_http_instrumentation():
        yield


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, transport=_transport)


def client_credentials(prefix: str) -> tuple[str, str]:
    """``(<prefix>_CLIENT_ID, <prefix>_CLIENT_SECRET)`` from the env, stripped."""
    return (
        os.environ.get(f"{prefix}_CLIENT_ID", "").strip(),
        os.environ.get(f"{prefix}_CLIENT_SECRET", "").strip(),
    )


def s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


async def token_request(url: str, data: dict[str, str], *, provider: str) -> dict[str, Any]:
    """POST a token request; the JSON answer, or InvalidGrant / ProviderError."""
    try:
        async with client() as http:
            resp = await http.post(url, data=data, headers={"accept": "application/json"})
    except httpx.HTTPError as exc:
        raise ProviderError(
            f"{provider}: token endpoint unreachable ({type(exc).__name__})"
        ) from None
    body = _json(resp)
    if resp.status_code >= 400 or (body.get("error") and not body.get("access_token")):
        reason = str(body.get("error") or body.get("status") or "").lower()
        if reason in _DEAD_GRANT:
            raise InvalidGrant(f"{provider}: {reason}")
        raise ProviderError(f"{provider}: token endpoint answered {resp.status_code}")
    if not body.get("access_token"):
        raise ProviderError(f"{provider}: token response had no access token")
    return body


async def authed(
    get_token: Callable[..., Awaitable[str]] | None,
    method: str,
    url: str,
    *,
    provider: str,
    auth_scheme: str = "Bearer",
    **kwargs: Any,
) -> httpx.Response:
    """Call ``url`` with a bearer token; on a 401 refresh once and retry."""
    if get_token is None:
        raise ProviderError(f"{provider}: no connection")
    base_headers = dict(kwargs.pop("headers", None) or {})
    token = await get_token()
    async with client() as http:
        for attempt in (0, 1):
            headers = {**base_headers, "authorization": f"{auth_scheme} {token}"}
            try:
                resp = await http.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                raise ProviderError(f"{provider}: API unreachable ({type(exc).__name__})") from None
            if resp.status_code != 401 or attempt:
                return resp
            token = await get_token(rejected=token)
    raise ProviderError(f"{provider}: unreachable")  # pragma: no cover - loop returns


def body_of(resp: httpx.Response) -> dict[str, Any]:
    return _json(resp)
