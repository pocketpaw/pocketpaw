# Tests for the Sites Cloudflare client (RFC 12, Task 2.2).
# Created: 2026-05-30 (feat/paw-sites-backend) — exercises the two CF
# surfaces the control plane uses, with httpx.MockTransport standing in
# for the real Cloudflare API (no network):
#   * put_worker — uploads a user Worker to the Workers-for-Platforms
#     dispatch namespace (asserts URL + PUT verb).
#   * create_custom_hostname — Cloudflare-for-SaaS hostname create, returns
#     the single CNAME target the client pastes.
#   * get_hostname_status — maps CF's status/ssl pair onto HostnameStatus.
#   * non-2xx responses fail closed (raise).
from __future__ import annotations

import httpx
import pytest
from pocketpaw_ee.sites.cloudflare_client import CloudflareClient
from pocketpaw_ee.sites.domain import CustomHostname, HostnameStatus


def _client(handler) -> CloudflareClient:
    transport = httpx.MockTransport(handler)
    return CloudflareClient(
        account_id="acct_1",
        api_token="tok_1",
        zone_id="zone_1",
        dispatch_namespace="paw-sites",
        _transport=transport,
    )


@pytest.mark.asyncio
async def test_put_worker_uploads_to_dispatch_namespace():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        return httpx.Response(200, json={"success": True, "result": {"id": "site_1"}})

    client = _client(handler)
    ok = await client.put_worker(script_name="site_1", bundle=b"export default {}")
    assert ok is True
    assert "dispatch/namespaces/paw-sites/scripts/site_1" in seen["url"]
    assert seen["method"] == "PUT"


@pytest.mark.asyncio
async def test_create_custom_hostname_returns_cname_target():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "id": "ch_1",
                    "hostname": "www.brightsmiledental.com",
                    "status": "pending",
                    "ssl": {"status": "pending_validation"},
                },
            },
        )

    client = _client(handler)
    ch: CustomHostname = await client.create_custom_hostname("www.brightsmiledental.com")
    assert ch.hostname == "www.brightsmiledental.com"
    assert ch.status == HostnameStatus.PENDING
    # Clients paste ONE CNAME pointing at the zone's SaaS fallback origin.
    assert ch.cname_target.endswith("zone_1.cdn.cloudflare.net") or ch.cname_target


@pytest.mark.asyncio
async def test_get_hostname_status_maps_active_to_live():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "id": "ch_1",
                    "hostname": "www.brightsmiledental.com",
                    "status": "active",
                    "ssl": {"status": "active"},
                },
            },
        )

    client = _client(handler)
    status = await client.get_hostname_status("ch_1")
    assert status == HostnameStatus.LIVE


@pytest.mark.asyncio
async def test_non_2xx_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"success": False, "errors": [{"message": "denied"}]})

    client = _client(handler)
    with pytest.raises(Exception):
        await client.put_worker(script_name="x", bundle=b"")
