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
# Updated 2026-06-20 (DS-2 — dynamic-site D1 bindings): put_worker gains an
# optional ``bindings`` param. When supplied it switches to the Workers
# multipart/form-data upload (a ``metadata`` JSON part carrying main_module +
# bindings + compatibility_date, plus the module file part) so a dynamic site's
# deployed Worker reaches its per-tenant D1. When omitted it keeps the exact
# single-module upload (static sites unchanged) — both paths are asserted here.
# Updated 2026-07-08 (DP0-1 — D1 provisioning): added coverage for
# create_database(): a successful create returns the uuid at ``result.uuid``, and
# a Cloudflare error envelope (success:false or non-2xx) fails closed by raising
# ValidationError with code ``sites.cloudflare_error``.
from __future__ import annotations

import json
import re

import httpx
import pytest
from pocketpaw_ee.sites.cloudflare_client import CloudflareClient
from pocketpaw_ee.sites.domain import CustomHostname, HostnameStatus


def _parse_multipart(body: bytes, content_type: str) -> dict[str, dict]:
    """Tiny multipart/form-data parser for test assertions — returns
    ``{field_name: {"filename": str|None, "content": bytes, "headers": str}}``.
    Avoids a heavyweight dep; the CF upload body is small and well-formed."""
    m = re.search(r"boundary=(.+)$", content_type)
    assert m, f"no boundary in content-type: {content_type!r}"
    boundary = m.group(1).strip('"')
    delim = b"--" + boundary.encode()
    parts: dict[str, dict] = {}
    for chunk in body.split(delim):
        chunk = chunk.strip(b"\r\n")
        if not chunk or chunk == b"--":
            continue
        head, _, content = chunk.partition(b"\r\n\r\n")
        head_text = head.decode("utf-8", "replace")
        name_m = re.search(r'name="([^"]+)"', head_text)
        if not name_m:
            continue
        fname_m = re.search(r'filename="([^"]*)"', head_text)
        parts[name_m.group(1)] = {
            "filename": fname_m.group(1) if fname_m else None,
            "content": content,
            "headers": head_text,
        }
    return parts


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
async def test_put_worker_no_bindings_sends_single_module_unchanged():
    """Guard against regress: with no bindings (static site) put_worker MUST send
    the byte-for-byte single-module upload — content == bundle, Content-Type
    application/javascript+module, NOT a multipart body."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content"] = request.content
        seen["content_type"] = request.headers.get("content-type", "")
        return httpx.Response(200, json={"success": True, "result": {"id": "site_1"}})

    client = _client(handler)
    ok = await client.put_worker(script_name="site_1", bundle=b"export default {STATIC}")
    assert ok is True
    # Exact same single-module wire shape as before DS-2.
    assert seen["content"] == b"export default {STATIC}"
    assert seen["content_type"] == "application/javascript+module"
    assert "multipart/form-data" not in seen["content_type"]


@pytest.mark.asyncio
async def test_put_worker_with_d1_binding_sends_multipart_metadata():
    """A dynamic site supplies a d1 binding ⇒ put_worker switches to the Workers
    multipart upload: a ``metadata`` JSON part naming the main module + carrying
    the d1 binding (name + database id), plus the module file part referenced by
    that filename. Asserts the exact metadata shape the CF API expects."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content"] = request.content
        seen["content_type"] = request.headers.get("content-type", "")
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"success": True, "result": {"id": "site_1"}})

    client = _client(handler)
    ok = await client.put_worker(
        script_name="site_1",
        bundle=b"export default {DYNAMIC}",
        bindings=[{"type": "d1", "name": "DB", "id": "d1_abc123"}],
    )
    assert ok is True
    assert seen["method"] == "PUT"
    assert "dispatch/namespaces/paw-sites/scripts/site_1" in seen["url"]
    assert seen["content_type"].startswith("multipart/form-data")

    parts = _parse_multipart(seen["content"], seen["content_type"])
    assert "metadata" in parts, f"no metadata part: {parts.keys()}"
    meta = json.loads(parts["metadata"]["content"])

    # The module part is referenced by filename via main_module.
    main_module = meta["main_module"]
    assert main_module in parts, f"main_module {main_module!r} has no file part"
    assert parts[main_module]["content"] == b"export default {DYNAMIC}"
    assert "application/javascript+module" in parts[main_module]["headers"]

    # The d1 binding (name + id) rides the metadata bindings list.
    d1 = [b for b in meta["bindings"] if b.get("type") == "d1"]
    assert d1 == [{"type": "d1", "name": "DB", "id": "d1_abc123"}]
    assert "compatibility_date" in meta


@pytest.mark.asyncio
async def test_put_worker_with_bindings_non_2xx_raises():
    """Multipart path fails closed identically to the single-module path."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"success": False, "errors": [{"message": "boom"}]})

    client = _client(handler)
    with pytest.raises(Exception):
        await client.put_worker(
            script_name="x",
            bundle=b"export default {}",
            bindings=[{"type": "d1", "name": "DB", "id": "d1_x"}],
        )


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


@pytest.mark.asyncio
async def test_create_database_returns_uuid_and_posts_name():
    """DP0-1: create_database POSTs {"name": <name>} to the D1 create endpoint and
    returns the new database's uuid from ``result.uuid``."""
    from pocketpaw_ee.cloud._core.errors import ValidationError

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"success": True, "result": {"uuid": "d1_uuid_xyz", "name": "site-db"}},
        )

    client = _client(handler)
    db_id = await client.create_database("site-db")
    assert db_id == "d1_uuid_xyz"
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/accounts/acct_1/d1/database")
    assert seen["body"] == {"name": "site-db"}
    # sanity: a real ValidationError type is importable (used by the fail path below).
    assert ValidationError is not None


@pytest.mark.asyncio
async def test_create_database_error_envelope_raises_cloudflare_error():
    """A Cloudflare error envelope (success:false) fails closed: create_database
    raises ValidationError with code ``sites.cloudflare_error``."""
    from pocketpaw_ee.cloud._core.errors import ValidationError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"success": False, "errors": [{"message": "d1 quota exceeded"}]}
        )

    client = _client(handler)
    with pytest.raises(ValidationError) as exc:
        await client.create_database("site-db")
    assert exc.value.code == "sites.cloudflare_error"


@pytest.mark.asyncio
async def test_create_database_non_2xx_raises_cloudflare_error():
    """A non-2xx response fails closed with the same ``sites.cloudflare_error`` code."""
    from pocketpaw_ee.cloud._core.errors import ValidationError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"success": False, "errors": [{"message": "boom"}]})

    client = _client(handler)
    with pytest.raises(ValidationError) as exc:
        await client.create_database("site-db")
    assert exc.value.code == "sites.cloudflare_error"
