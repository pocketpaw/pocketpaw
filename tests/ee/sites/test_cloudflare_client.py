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


def _client(handler, *, cname_target: str = "sites.pawzone.test") -> CloudflareClient:
    transport = httpx.MockTransport(handler)
    return CloudflareClient(
        account_id="acct_1",
        api_token="tok_1",
        zone_id="zone_1",
        dispatch_namespace="paw-sites",
        cname_target=cname_target,
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

    client = _client(handler, cname_target="sites.pawzone.test")
    ch: CustomHostname = await client.create_custom_hostname("www.brightsmiledental.com")
    assert ch.hostname == "www.brightsmiledental.com"
    assert ch.status == HostnameStatus.PENDING
    # Clients paste ONE CNAME, and it is the CONFIGURED target verbatim.
    #
    # This assertion used to read ``endswith("zone_1.cdn.cloudflare.net") or
    # ch.cname_target`` — a trailing truthy `or` that passed for any non-empty
    # string, so it never checked anything. Under it the client shipped
    # ``{zone_id}.cdn.cloudflare.net``, a name with no DNS records at all, as the
    # one instruction the whole flow asks a customer to follow.
    #
    # MUTATION THAT BREAKS THIS: returning ``f"{self._zone_id}.cdn.cloudflare.net"``.
    assert ch.cname_target == "sites.pawzone.test"


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


# ── The error body on a non-2xx is the diagnosis, and it was being discarded ──
# Added 2026-08-12, from a live report: adding a custom domain answered 422 with
# ``{"code": "sites.cloudflare_error", "message": "Cloudflare API 403"}``.
#
# A bare status code is not a diagnosis. Cloudflare 403s the custom-hostname
# endpoint for several unrelated reasons — a token missing the SSL-and-Certificates
# edit scope, a token with no access to that zone, a zone without Cloudflare for
# SaaS enabled — and it names which one in the response body every time. ``_unwrap``
# read that body only on the ``success: false`` branch, which cannot run on a
# non-2xx, so the one useful sentence was dropped on exactly the responses where
# somebody needed it.


@pytest.mark.asyncio
async def test_non_2xx_surfaces_cloudflares_own_error_message():
    """MUTATION THAT BREAKS THIS: reverting _unwrap's non-2xx branch to
    ``f"Cloudflare API {resp.status_code}"``. Everything still fails closed — the
    loss is purely diagnostic, which is why it survived: no test asserted on the
    message, only on the code."""
    from pocketpaw_ee.cloud._core.errors import ValidationError

    def handler(request: httpx.Request) -> httpx.Response:
        # The shape Cloudflare actually returns for a token that cannot edit
        # custom hostnames on this zone.
        return httpx.Response(
            403,
            json={
                "success": False,
                "errors": [{"code": 9109, "message": "Unauthorized to access requested resource"}],
            },
        )

    client = _client(handler)
    with pytest.raises(ValidationError) as exc:
        await client.create_custom_hostname("www.example.com")
    assert exc.value.code == "sites.cloudflare_error"
    assert "Unauthorized to access requested resource" in str(exc.value)
    # The status is still worth carrying — it is how an operator tells a refusal
    # (403) apart from an outage (5xx) at a glance.
    assert "403" in str(exc.value)


@pytest.mark.asyncio
async def test_non_2xx_with_an_unparseable_body_still_reports_the_status():
    """Cloudflare's edge can answer a non-2xx with an HTML error page rather than
    the JSON envelope. Parsing must not become the new failure mode — the status
    code is the floor, not the ceiling."""
    from pocketpaw_ee.cloud._core.errors import ValidationError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html><body>Bad gateway</body></html>")

    client = _client(handler)
    with pytest.raises(ValidationError) as exc:
        await client.create_custom_hostname("www.example.com")
    assert exc.value.code == "sites.cloudflare_error"
    assert "502" in str(exc.value)


@pytest.mark.asyncio
async def test_non_2xx_reports_every_error_cloudflare_listed():
    """Cloudflare returns an ARRAY. The ``success: false`` branch already only read
    errors[0]; on a refusal the second entry is often the actionable one (the first
    names the endpoint, the second names the missing permission)."""
    from pocketpaw_ee.cloud._core.errors import ValidationError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "success": False,
                "errors": [
                    {"code": 1109, "message": "Invalid access token"},
                    {"code": 1049, "message": "zone is not entitled to Cloudflare for SaaS"},
                ],
            },
        )

    client = _client(handler)
    with pytest.raises(ValidationError) as exc:
        await client.create_custom_hostname("www.example.com")
    assert "Invalid access token" in str(exc.value)
    assert "not entitled to Cloudflare for SaaS" in str(exc.value)


# ── The routing half: a hostname Cloudflare accepts still has to reach a site ──
# Added 2026-08-12 (the custom-domain routing lane). ``create_custom_hostname``
# only makes Cloudflare willing to terminate TLS for the domain. A Worker route
# scoped to ``<hostname>/*`` is what decides which site answers it, and without
# one the domain validates, shows Live, and serves an error from the fallback
# origin — a failure where every signal reports success.


@pytest.mark.asyncio
async def test_create_custom_hostname_refuses_without_a_cname_target():
    """An unconfigured target is refused rather than handed out.

    The value a customer pastes is the only instruction in the whole flow, and a
    wrong one produces a hostname that can never validate with nothing able to say
    why. Failing at create time turns a silent customer-side dead end into an
    operator-side message naming the variable to set.

    MUTATION THAT BREAKS THIS: dropping the ``if not self._cname_target`` guard, or
    defaulting the constructor arg to any non-empty string.
    """
    from pocketpaw_ee.cloud._core.errors import ValidationError

    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"success": True, "result": {}})

    client = _client(handler, cname_target="")
    with pytest.raises(ValidationError) as exc:
        await client.create_custom_hostname("www.example.com")
    assert exc.value.code == "sites.cloudflare_unconfigured"
    # And it refuses BEFORE spending a Cloudflare call — a hostname created against
    # a target nobody configured is one somebody has to delete by hand later.
    assert called is False


@pytest.mark.asyncio
async def test_paid_tier_features_no_longer_send_custom_metadata():
    """Per-hostname ``custom_metadata`` is not generally available — Cloudflare's
    doc says "only certain customers have access to this feature". BC-10 attached it
    for any tier declaring ``cloudflare_features``, which meant custom domains worked
    on FREE sites and 403'd on PAID ones.

    The ``ssl.settings`` map stays: it is not entitlement-gated, and it is the part
    that actually provisions anything.

    MUTATION THAT BREAKS THIS: restoring the ``ssl["custom_metadata"] = ...`` line in
    ``_ssl_for_features``.
    """
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "id": "ch_1",
                    "hostname": "www.example.com",
                    "status": "pending",
                    "ssl": {"status": "pending_validation"},
                },
            },
        )

    client = _client(handler)
    await client.create_custom_hostname("www.example.com", features={"waf", "edge_cache"})
    ssl = seen["body"]["ssl"]
    assert "custom_metadata" not in ssl
    # The entitlement-free half is untouched: both feature fragments still merge.
    assert ssl["settings"] == {"min_tls_version": "1.2", "tls_1_3": "on", "http2": "on"}
    assert ssl["method"] == "http" and ssl["type"] == "dv"


@pytest.mark.asyncio
async def test_create_worker_route_posts_the_pattern_and_returns_the_id():
    """POST, not PUT: PUT on this collection is update-by-id and needs a route id we
    do not have yet. The returned id is what makes teardown possible — a route nobody
    recorded is an orphan nobody can delete.

    MUTATION THAT BREAKS THIS: switching the verb to ``client.put``, or returning
    ``True``/the whole result instead of ``result["id"]``.
    """
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"success": True, "result": {"id": "route_9"}})

    client = _client(handler)
    route_id = await client.create_worker_route(
        pattern="www.example.com/*", script="paw-site-abc"
    )
    assert route_id == "route_9"
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/zones/zone_1/workers/routes")
    assert seen["body"] == {"pattern": "www.example.com/*", "script": "paw-site-abc"}


@pytest.mark.asyncio
async def test_create_worker_route_fails_closed_on_a_cloudflare_refusal():
    """A route naming a script Cloudflare cannot find is rejected, and that must
    surface — the caller rolls the custom hostname back on this exact signal."""
    from pocketpaw_ee.cloud._core.errors import ValidationError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "success": False,
                "errors": [{"code": 10007, "message": "workers.api.error.script_not_found"}],
            },
        )

    client = _client(handler)
    with pytest.raises(ValidationError) as exc:
        await client.create_worker_route(pattern="www.example.com/*", script="missing")
    assert exc.value.code == "sites.cloudflare_error"
    assert "script_not_found" in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "arg"),
    [("delete_worker_route", "route_9"), ("delete_custom_hostname", "ch_1")],
)
async def test_deletes_treat_a_404_as_already_done(method: str, arg: str):
    """Teardown targets a STATE, not an event: "this is not on the zone" is already
    satisfied by something that was never there. Raising on 404 would make a
    half-finished teardown permanently un-finishable — which recreates the exact
    orphan accumulation these methods exist to stop.

    MUTATION THAT BREAKS THIS: deleting the ``if resp.status_code == 404: return``
    guard, so ``_unwrap`` raises on the 404.
    """
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(404, json={"success": False, "errors": [{"message": "not found"}]})

    client = _client(handler)
    await getattr(client, method)(arg)  # must not raise
    assert seen["method"] == "DELETE"
    assert seen["url"].endswith(arg)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "arg"),
    [("delete_worker_route", "route_9"), ("delete_custom_hostname", "ch_1")],
)
async def test_deletes_still_fail_closed_on_a_real_error(method: str, arg: str):
    """404-tolerance is not error-tolerance. A 403 means we could not delete it and
    it is still there; reporting success would strand the orphan silently."""
    from pocketpaw_ee.cloud._core.errors import ValidationError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"success": False, "errors": [{"message": "denied"}]})

    client = _client(handler)
    with pytest.raises(ValidationError) as exc:
        await getattr(client, method)(arg)
    assert exc.value.code == "sites.cloudflare_error"
