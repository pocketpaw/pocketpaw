# ee/pocketpaw_ee/catalog/admin_client.py — async ADMIN client over the LiteLLM
# proxy's management API (MCG-8). The read-only catalog client lives in
# ``litellm_client.py``; this is its MUTATING sibling — it mints per-tenant
# virtual keys and reads per-key spend, authorized by the proxy MASTER key.
#
# Reuses the catalog's proxy base/key resolution (``catalog.config``) so the whole
# deployment has ONE proxy config path — the admin key is the same
# ``POCKETPAW_LITELLM_API_KEY`` (the master key) the read client already uses; it
# is the only key authorized for ``/key/*`` and ``/spend/*``.
#
# Endpoints wrapped:
#   * POST /key/generate     — mint a virtual key with max_budget / budget_duration
#                              / rpm_limit / tpm_limit / models / metadata. Returns
#                              the new ``key`` string (+ echoed fields).
#   * GET  /key/info?key=    — read back a key's live config + spend.
#   * GET  /spend/logs?api_key=<key> — the per-key spend rows (cost + token counts,
#                              incl. cached tokens). MUST pass ``api_key`` — an
#                              unfiltered /spend/logs scans the whole proxy and
#                              times out (noted in the MCG-8 task brief).
#   * POST /key/delete       — revoke keys (used by the live-check teardown so a
#                              throwaway probe key never lingers on the proxy).
#
# httpx-based with an injectable ``_transport`` so tests stand in an
# httpx.MockTransport (no live proxy) — the same seam ``LiteLLMClient`` exposes.
# Fail-LOUD: a non-2xx raises ``LiteLLMAdminError`` so a broken provision surfaces
# rather than silently minting nothing / billing nothing.
#
# Created 2026-06-26 (integration/model-catalog-v2, MCG-8): the proxy admin client.

from __future__ import annotations

import logging
from typing import Any

import httpx

from pocketpaw_ee.catalog import config

logger = logging.getLogger(__name__)


class LiteLLMAdminError(Exception):
    """A LiteLLM proxy management call failed (non-2xx or malformed body). Raised
    so provisioning / spend ingestion fail loud rather than silently no-op."""


class LiteLLMAdminClient:
    """Thin async client over the LiteLLM proxy MANAGEMENT API (key + spend).

    Authorized by the proxy master key (``catalog.config.litellm_proxy_api_key``).
    Mutating sibling of the read-only ``LiteLLMClient`` — same base-URL + header +
    transport shape, different (privileged) endpoints.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        _transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = (base_url or config.litellm_proxy_url()).rstrip("/")
        # api_key explicitly passed wins; otherwise resolve the master key from env.
        self._api_key = api_key if api_key is not None else config.litellm_proxy_api_key()
        self._transport = _transport  # tests inject httpx.MockTransport
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers=self._headers(),
            transport=self._transport,
            timeout=self._timeout,
        )

    @staticmethod
    def _json_or_raise(resp: httpx.Response, what: str) -> dict[str, Any]:
        """Return the JSON body of a 2xx response, or raise ``LiteLLMAdminError``.
        A non-2xx surfaces the status + (when present) the proxy error message so a
        bad-budget / bad-key / no-permission response is legible."""
        if resp.status_code // 100 != 2:
            detail = ""
            try:
                body = resp.json()
                if isinstance(body, dict):
                    err = body.get("error")
                    if isinstance(err, dict):
                        detail = str(err.get("message") or "")
                    elif isinstance(err, str):
                        detail = err
                    detail = detail or str(body.get("detail") or body.get("message") or "")
            except Exception:  # noqa: BLE001
                detail = ""
            raise LiteLLMAdminError(
                f"LiteLLM admin {what} returned {resp.status_code}"
                + (f" — {detail}" if detail else "")
            )
        try:
            body = resp.json()
        except ValueError as exc:
            raise LiteLLMAdminError(f"LiteLLM admin {what} returned non-JSON") from exc
        return body if isinstance(body, dict) else {"data": body}

    async def generate_key(
        self,
        *,
        key_alias: str | None = None,
        max_budget: float | None = None,
        budget_duration: str | None = None,
        rpm_limit: int | None = None,
        tpm_limit: int | None = None,
        models: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST /key/generate — mint a virtual key. Returns the proxy's JSON body
        (carries the new ``key`` string and echoes the budget / limit fields).

        Only non-None fields are sent so the proxy applies its own defaults for
        anything we leave unset. ``models=[]`` is sent as an explicit empty
        allowlist (all models) only when ``models`` is the empty list, not None.
        """
        payload: dict[str, Any] = {}
        if key_alias is not None:
            payload["key_alias"] = key_alias
        if max_budget is not None:
            payload["max_budget"] = max_budget
        if budget_duration is not None:
            payload["budget_duration"] = budget_duration
        if rpm_limit is not None:
            payload["rpm_limit"] = rpm_limit
        if tpm_limit is not None:
            payload["tpm_limit"] = tpm_limit
        if models is not None:
            payload["models"] = models
        if metadata is not None:
            payload["metadata"] = metadata

        async with self._client() as client:
            resp = await client.post(f"{self._base_url}/key/generate", json=payload)
        return self._json_or_raise(resp, "/key/generate")

    async def key_info(self, key: str) -> dict[str, Any]:
        """GET /key/info?key=<key> — read back a key's live config + spend."""
        async with self._client() as client:
            resp = await client.get(f"{self._base_url}/key/info", params={"key": key})
        return self._json_or_raise(resp, "/key/info")

    async def spend_logs(self, *, api_key: str) -> list[dict[str, Any]]:
        """GET /spend/logs?api_key=<key> — the spend rows for ONE virtual key.

        ``api_key`` is REQUIRED: an unfiltered /spend/logs scans the proxy's whole
        spend table and times out (per the MCG-8 brief). Returns the list of rows
        (each carries ``spend``, ``startTime``, token counts incl. cached, and a
        ``request_id``). A proxy that returns a bare list yields it directly; a
        dict-wrapped ``{"data": [...]}`` is unwrapped.
        """
        if not api_key:
            raise LiteLLMAdminError("spend_logs requires api_key (unfiltered scan times out)")
        async with self._client() as client:
            resp = await client.get(f"{self._base_url}/spend/logs", params={"api_key": api_key})
        body = self._json_or_raise(resp, "/spend/logs")
        # /spend/logs returns a top-level JSON array; _json_or_raise wraps a bare
        # list as {"data": [...]}. A dict response with a "data" list is handled
        # the same way. Keep only dict rows.
        data = body.get("data")
        rows = data if isinstance(data, list) else []
        return [r for r in rows if isinstance(r, dict)]

    async def delete_keys(self, keys: list[str]) -> dict[str, Any]:
        """POST /key/delete — revoke the given virtual keys. Used by the live-check
        teardown so a throwaway probe key is never left on the proxy."""
        async with self._client() as client:
            resp = await client.post(f"{self._base_url}/key/delete", json={"keys": keys})
        return self._json_or_raise(resp, "/key/delete")


__all__ = [
    "LiteLLMAdminClient",
    "LiteLLMAdminError",
]
