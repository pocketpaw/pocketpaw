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
#
# Updated 2026-06-20 (DS-2 — dynamic-site D1 bindings): ``put_worker`` gained an
# optional ``bindings`` param. A DYNAMIC Paw Site is backed by a per-tenant
# Cloudflare D1; its deployed Worker needs a D1 binding to reach that DB. When
# bindings are supplied, the upload switches to the Workers multipart/form-data
# contract — a ``metadata`` JSON part (main_module + bindings + compatibility_date)
# plus the module file part referenced by its filename. When NO bindings are
# supplied (a static site), it keeps the prior single-module upload byte-for-byte,
# so the static path never regresses. The fail-closed + in-memory-token handling
# is identical on both paths.

from __future__ import annotations

import json

import httpx

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites.domain import CustomHostname, HostnameStatus

_CF_API = "https://api.cloudflare.com/client/v4"

# Filename the module part is uploaded under (and named by ``main_module`` in the
# metadata). The Workers multipart upload references modules by filename; the
# bundle our generator emits is a single ESM entry, so one ``*.mjs`` part suffices.
_MAIN_MODULE = "index.mjs"

# Workers compatibility date for the multipart upload's metadata. Matches the
# date the generator bakes into the (otherwise-ignored-on-direct-API-upload)
# wrangler.toml so the runtime semantics are identical to a wrangler deploy.
_COMPATIBILITY_DATE = "2024-09-23"


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

    async def put_worker(
        self,
        *,
        script_name: str,
        bundle: bytes,
        bindings: list[dict] | None = None,
    ) -> bool:
        """Upload a user Worker into the dispatch namespace. Live on 200.

        ``bindings`` (DS-2) carries the Worker's runtime bindings — for a dynamic
        Paw Site, a D1 binding ``{"type": "d1", "name": "DB", "id": <database_id>}``
        so the deployed Worker can reach its per-tenant D1. Each binding is a dict
        passed straight into the upload metadata's ``bindings`` array (the CF
        Workers contract), so future binding types (queues, KV, ...) ride the same
        param without a signature change.

        When ``bindings`` is None/empty (a STATIC site), the upload is the prior
        single-module PUT — ``content=bundle`` with a
        ``application/javascript+module`` Content-Type, byte-for-byte unchanged, so
        the static path never regresses. When bindings are supplied, the upload
        switches to the Workers multipart/form-data contract: a ``metadata`` JSON
        part naming ``main_module`` + the ``bindings`` array + ``compatibility_date``,
        plus the module file part referenced by that filename. The
        dispatch-namespace script upload uses the same multipart contract as a
        normal Worker upload (metadata part named ``metadata``, module parts keyed
        by filename)."""
        url = (
            f"{_CF_API}/accounts/{self._account_id}"
            f"/workers/dispatch/namespaces/{self._namespace}/scripts/{script_name}"
        )
        async with self._client() as client:
            if bindings:
                metadata = {
                    "main_module": _MAIN_MODULE,
                    "bindings": list(bindings),
                    "compatibility_date": _COMPATIBILITY_DATE,
                }
                # httpx builds the multipart/form-data body + boundary. The
                # ``metadata`` part is a JSON blob; the module part is named by its
                # filename (== main_module) and typed as an ES module so CF treats
                # it as the entrypoint, not a plain asset.
                files = {
                    "metadata": (None, json.dumps(metadata), "application/json"),
                    _MAIN_MODULE: (
                        _MAIN_MODULE,
                        bundle,
                        "application/javascript+module",
                    ),
                }
                resp = await client.put(url, files=files)
            else:
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
