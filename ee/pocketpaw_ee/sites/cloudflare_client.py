# ee/pocketpaw_ee/sites/cloudflare_client.py — async Cloudflare API client for
# the Sites control plane. Two surfaces:
#   * Workers for Platforms — PUT a user Worker into our dispatch namespace
#     (one synchronous call per site; live on 200; no per-account script cap).
#   * Cloudflare for SaaS — create a custom hostname, return the single CNAME
#     the client pastes, poll validation + TLS status.
# httpx-based; account id + token come from settings (env), not per-tenant rows
# in v1. Non-2xx raises a CloudError so the standard envelope applies.
#
# Secret handling: the CF API token lives only in the in-memory Authorization
# header — never logged, never written to disk. All errors fail closed (raise
# ValidationError), so a failed CF call never silently reports success.
# Created: 2026-05-30 (feat/paw-sites-backend, Task 2.2).

from __future__ import annotations

import httpx

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites.domain import CustomHostname, HostnameStatus

_CF_API = "https://api.cloudflare.com/client/v4"


def _map_status(cf_status: str, ssl_status: str) -> HostnameStatus:
    if cf_status == "active" and ssl_status == "active":
        return HostnameStatus.LIVE
    if cf_status in {"pending", "pending_deletion"}:
        return HostnameStatus.PENDING
    if cf_status in {"active"} or ssl_status in {"pending_validation", "initializing"}:
        return HostnameStatus.VERIFYING
    return HostnameStatus.ERROR


class CloudflareClient:
    def __init__(
        self,
        *,
        account_id: str,
        api_token: str,
        zone_id: str,
        dispatch_namespace: str,
        _transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._account_id = account_id
        self._zone_id = zone_id
        self._namespace = dispatch_namespace
        self._headers = {"Authorization": f"Bearer {api_token}"}
        self._transport = _transport  # tests inject a MockTransport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers=self._headers, transport=self._transport, timeout=30.0)

    @staticmethod
    def _unwrap(resp: httpx.Response) -> dict:
        if resp.status_code // 100 != 2:
            raise ValidationError("sites.cloudflare_error", f"Cloudflare API {resp.status_code}")
        body = resp.json()
        if not body.get("success", False):
            errs = body.get("errors") or [{"message": "unknown"}]
            raise ValidationError("sites.cloudflare_error", str(errs[0].get("message")))
        return body.get("result", {})

    async def put_worker(self, *, script_name: str, bundle: bytes) -> bool:
        """Upload a user Worker into the dispatch namespace. Live on 200."""
        url = (
            f"{_CF_API}/accounts/{self._account_id}"
            f"/workers/dispatch/namespaces/{self._namespace}/scripts/{script_name}"
        )
        async with self._client() as client:
            resp = await client.put(
                url,
                content=bundle,
                headers={"Content-Type": "application/javascript+module"},
            )
        self._unwrap(resp)
        return True

    async def create_custom_hostname(self, hostname: str) -> CustomHostname:
        url = f"{_CF_API}/zones/{self._zone_id}/custom_hostnames"
        async with self._client() as client:
            resp = await client.post(
                url,
                json={"hostname": hostname, "ssl": {"method": "http", "type": "dv"}},
            )
        result = self._unwrap(resp)
        return CustomHostname(
            id=result["id"],
            hostname=result["hostname"],
            status=_map_status(
                result.get("status", ""), (result.get("ssl") or {}).get("status", "")
            ),
            cname_target=f"{self._zone_id}.cdn.cloudflare.net",
        )

    async def get_hostname_status(self, hostname_id: str) -> HostnameStatus:
        url = f"{_CF_API}/zones/{self._zone_id}/custom_hostnames/{hostname_id}"
        async with self._client() as client:
            resp = await client.get(url)
        result = self._unwrap(resp)
        return _map_status(result.get("status", ""), (result.get("ssl") or {}).get("status", ""))
