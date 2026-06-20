# ee/pocketpaw_ee/sites/cloudflare_client.py — async Cloudflare API client for
# the Sites control plane. Three surfaces:
#   * Workers for Platforms — PUT a user Worker into our dispatch namespace
#     (one synchronous call per site; live on 200; no per-account script cap).
#   * Cloudflare for SaaS — create a custom hostname, return the single CNAME
#     the client pastes, poll validation + TLS status.
#   * D1 (DS-3) — query a dynamic site's per-tenant D1 over the HTTP API so the
#     control plane can READ its data (the operator data-view); see query_d1.
# httpx-based; account id + token come from settings (env), not per-tenant rows
# in v1. Non-2xx raises a CloudError so the standard envelope applies.
#
# Secret handling: the CF API token lives only in the in-memory Authorization
# header — never logged, never written to disk. All errors fail closed (raise
# ValidationError), so a failed CF call never silently reports success.
# Created: 2026-05-30 (feat/paw-sites-backend, Task 2.2).
#
# Updated 2026-06-20 (DS-3 — control-plane read of a dynamic site's D1): added
# query_d1(). It POSTs to the Cloudflare D1 query endpoint
# (POST /accounts/{acct}/d1/database/{db_id}/query) with a PARAMETERIZED
# {sql, params} body and returns the rows from the FIRST statement's
# ``result[0].results``. It mirrors the existing client style exactly: injectable
# transport, the CF token in the in-memory Authorization header only, and
# fail-closed via the shared ``_unwrap`` (a non-2xx or success:false raises
# ValidationError). It NEVER interpolates SQL — the table identifier is validated
# against the site's known objects by the service BEFORE it reaches here, and all
# values bind through ``params``. The service layer (DS-3) owns that validation;
# this method is a thin, SQL-agnostic transport.

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

    async def query_d1(
        self, *, database_id: str, sql: str, params: list | None = None
    ) -> list[dict]:
        """Run ONE parameterized SQL statement against a D1 database and return its
        rows (DS-3 — the control-plane read of a dynamic site's data).

        POSTs to the Cloudflare D1 query endpoint with a ``{sql, params}`` body.
        The D1 query response wraps each statement's output in a ``result`` ARRAY
        (one element per statement; a single ``sql`` returns one element), each
        carrying its own ``results`` rows + ``meta``. This sends a single
        statement and returns the FIRST element's ``results`` (the rows) as a list
        of dicts — empty when the table has no rows.

        SQL safety: this method NEVER builds SQL itself. The caller (the service)
        passes a fully-formed statement whose table identifier it has already
        validated against the site's declared ``objects`` (an unknown table is
        rejected BEFORE this is reached); every value rides ``params`` as a bound
        placeholder, never string-interpolated. ``params`` defaults to an empty
        list so a value-less listing query is sent cleanly.

        Fail-closed: a non-2xx, a ``success: false`` envelope, or a D1
        statement-level failure raises ValidationError via ``_unwrap`` /
        the per-statement ``success`` check, so a failed read never silently
        reports empty rows."""
        url = f"{_CF_API}/accounts/{self._account_id}/d1/database/{database_id}/query"
        async with self._client() as client:
            resp = await client.post(url, json={"sql": sql, "params": params or []})
        # ``_unwrap`` checks the outer envelope (HTTP status + top-level success);
        # for a query the ``result`` is an ARRAY of per-statement outcomes.
        result = self._unwrap(resp)
        statements = result if isinstance(result, list) else []
        if not statements:
            return []
        first = statements[0] if isinstance(statements[0], dict) else {}
        # A per-statement failure (e.g. malformed SQL) sets success=false on the
        # element even when the HTTP envelope is 200 — fail closed on it too.
        if first.get("success") is False:
            raise ValidationError("sites.cloudflare_error", "D1 query statement failed")
        rows = first.get("results")
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
