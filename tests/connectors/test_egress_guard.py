# Tests for the AW-1 connector egress guard.
# Added: 2026-06-28 (AW-1) — proves the SSRF egress primitive in
#   ``pocketpaw.security.url_validators`` and its wiring into the DirectREST
#   connector engine (``yaml_engine.DirectRESTAdapter``):
#     * an allow-listed public host passes ``assert_egress_allowed`` and yields
#       a pinned EgressTarget;
#     * a non-allow-listed host, an http (non-https) URL, userinfo / fragment,
#       and a loopback/internal-resolving host are all rejected;
#     * ``PinnedTransport`` repoints the connect at the resolved IP while
#       preserving the Host header + sni_hostname (no second DNS lookup);
#     * a host that is allow-listed but RESOLVES to an internal IP is still
#       blocked at the check (DNS-rebind defense);
#     * an end-to-end connector smoke (mocked httpx transport) shows a
#       non-allow-listed host blocked and an allowed host succeeding, gated by
#       the POCKETPAW_CONNECTOR_EGRESS_GUARD flag.
# DNS is mocked throughout (no network); ``POCKETPAW_ALLOW_INTERNAL_URLS`` is
# pinned per-test so the dev escape never leaks across cases.
# Updated: 2026-06-28 (AW-2 multi-host allow-list + concern fixes) — added
#   ``TestEffectiveAllowList`` and ``TestConnectorMultiHost`` proving:
#     * a two-host connector (auth host on auth.auth_url ≠ the API host) calls
#       BOTH hosts successfully under the guard — both are auto-seeded;
#     * a templated base URL ({FOO}.api.example.com) resolves at call time and
#       is checked by its RESOLVED host (not the template string);
#     * a request to a host outside the connector's declared topology is
#       blocked with a clean "egress guard" error even though it resolves to a
#       public IP (the allow-list is now meaningful, not tautological);
#     * a cookie/session-auth connector keeps its cookie jar across two calls
#       under the guard (the pinned client is cached per host, concern fix 2);
#     * ``_egress_guard_enabled`` FAILS CLOSED (returns True) on a settings-load
#       error instead of silently disabling the guard (concern fix 1);
#     * explicit ``allowed_hosts:`` (YAML) and per-workspace allowed_hosts (via
#       connect() config) are layered on top of the auto-seeded hosts.
# Updated: 2026-07-02 (AW-3 egress default-close) — added
#   ``test_internal_rejected_when_flag_unset``: with POCKETPAW_ALLOW_INTERNAL_URLS
#   UNSET (delenv, not just "false"), an allow-listed host resolving to a
#   metadata IP is still rejected — proves the guard is default-closed. Also
#   dropped the dead ``yaml_engine.get_settings`` no-op patch in
#   ``TestEgressGuardFailsClosed`` (the method imports get_settings locally from
#   pocketpaw.config, so only the config_mod patch is load-bearing).

from __future__ import annotations

import socket

import httpx
import pytest

from pocketpaw.security.url_validators import (
    EgressError,
    PinnedTransport,
    assert_egress_allowed,
)


def _fake_getaddrinfo(mapping: dict[str, list[str]]):
    """Build a ``socket.getaddrinfo`` stub returning the IPs in ``mapping``.

    ``mapping`` is host -> list of IP strings. An unmapped host raises
    ``socket.gaierror`` (matches a real resolution failure).
    """

    def _stub(host, *args, **kwargs):
        ips = mapping.get(host)
        if not ips:
            raise socket.gaierror(f"name resolution disabled in test: {host}")
        # getaddrinfo returns 5-tuples; only info[4][0] (the sockaddr host) is read.
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]

    return _stub


@pytest.fixture(autouse=True)
def _block_internal(monkeypatch):
    """Default every test to the strict posture (internal hosts blocked)."""
    monkeypatch.setenv("POCKETPAW_ALLOW_INTERNAL_URLS", "false")


class TestAssertEgressAllowed:
    async def test_allowed_host_passes(self, monkeypatch):
        monkeypatch.setattr(
            socket, "getaddrinfo", _fake_getaddrinfo({"api.example.com": ["93.184.216.34"]})
        )
        target = await assert_egress_allowed(
            "https://api.example.com/v1/orders", {"api.example.com"}
        )
        assert target.host == "api.example.com"
        assert target.port == 443
        assert target.pinned_ip == "93.184.216.34"
        assert target.url == "https://api.example.com/v1/orders"

    async def test_non_allowlisted_host_rejected(self, monkeypatch):
        monkeypatch.setattr(
            socket, "getaddrinfo", _fake_getaddrinfo({"evil.com": ["93.184.216.34"]})
        )
        with pytest.raises(EgressError, match="allow-list"):
            await assert_egress_allowed("https://evil.com/steal", {"api.example.com"})

    async def test_http_scheme_rejected(self, monkeypatch):
        monkeypatch.setattr(
            socket, "getaddrinfo", _fake_getaddrinfo({"api.example.com": ["93.184.216.34"]})
        )
        with pytest.raises(EgressError, match="https-only"):
            await assert_egress_allowed("http://api.example.com/v1", {"api.example.com"})

    async def test_userinfo_rejected(self):
        # userinfo lets the real host hide after the '@'; reject before DNS.
        with pytest.raises(EgressError, match="userinfo"):
            await assert_egress_allowed("https://api.example.com@evil.com/", {"api.example.com"})

    async def test_fragment_rejected(self):
        with pytest.raises(EgressError, match="fragment"):
            await assert_egress_allowed("https://api.example.com/v1#frag", {"api.example.com"})

    async def test_loopback_host_rejected(self, monkeypatch):
        # An allow-listed host name that resolves to loopback is still blocked.
        monkeypatch.setattr(
            socket, "getaddrinfo", _fake_getaddrinfo({"localish.example.com": ["127.0.0.1"]})
        )
        with pytest.raises(EgressError, match="internal address"):
            await assert_egress_allowed("https://localish.example.com/", {"localish.example.com"})

    async def test_metadata_ip_rejected(self, monkeypatch):
        # AWS metadata endpoint via a public-looking name — classic SSRF target.
        monkeypatch.setattr(
            socket, "getaddrinfo", _fake_getaddrinfo({"metadata.example.com": ["169.254.169.254"]})
        )
        with pytest.raises(EgressError, match="internal address"):
            await assert_egress_allowed(
                "https://metadata.example.com/latest/meta-data", {"metadata.example.com"}
            )

    async def test_dns_rebind_after_allowlist_still_blocked(self, monkeypatch):
        """The core TOCTOU case: the host IS allow-listed by name, but DNS now
        returns an internal IP. ``assert_egress_allowed`` re-resolves at call
        time and rejects, so the pin can never point at the internal address."""
        monkeypatch.setattr(
            socket, "getaddrinfo", _fake_getaddrinfo({"rebind.example.com": ["10.0.0.7"]})
        )
        with pytest.raises(EgressError, match="internal address"):
            await assert_egress_allowed("https://rebind.example.com/", {"rebind.example.com"})

    async def test_internal_rejected_when_flag_unset(self, monkeypatch):
        """AW-3 default-close: with POCKETPAW_ALLOW_INTERNAL_URLS UNSET (the
        realistic prod state), an allow-listed host that resolves to a metadata
        IP must STILL be rejected. Before the fix the egress guard reused the
        permissive ``_allow_internal()`` (unset ⇒ True), so this pinned the
        internal address — default-OPEN SSRF. The guard now defaults to reject."""
        # The autouse fixture pins the flag to "false"; delete it entirely so we
        # exercise the genuine unset path, not the explicit-false path.
        monkeypatch.delenv("POCKETPAW_ALLOW_INTERNAL_URLS", raising=False)
        monkeypatch.setattr(
            socket, "getaddrinfo", _fake_getaddrinfo({"metadata.example.com": ["169.254.169.254"]})
        )
        with pytest.raises(EgressError, match="internal address"):
            await assert_egress_allowed(
                "https://metadata.example.com/latest/meta-data", {"metadata.example.com"}
            )

    async def test_internal_allowed_with_dev_escape(self, monkeypatch):
        # POCKETPAW_ALLOW_INTERNAL_URLS=true is the documented dev escape.
        monkeypatch.setenv("POCKETPAW_ALLOW_INTERNAL_URLS", "true")
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"dev.local": ["127.0.0.1"]}))
        target = await assert_egress_allowed("https://dev.local/api", {"dev.local"})
        assert target.pinned_ip == "127.0.0.1"


class TestPinnedTransport:
    async def test_connect_target_is_pinned_ip(self, monkeypatch):
        """The transport must dial the pinned IP while keeping the Host header
        and sni_hostname on the original hostname (so TLS verifies the cert and
        no second DNS lookup happens)."""
        captured: dict[str, object] = {}

        async def _fake_inner(self, request):  # noqa: ANN001
            captured["connect_host"] = request.url.host
            captured["host_header"] = request.headers.get("Host")
            captured["sni"] = request.extensions.get("sni_hostname")
            return httpx.Response(200, request=request)

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _fake_inner)

        transport = PinnedTransport("93.184.216.34")
        request = httpx.Request("GET", "https://api.example.com/v1/orders")
        resp = await transport.handle_async_request(request)
        await transport.aclose()

        assert resp.status_code == 200
        # Connection went to the pinned IP, not a re-resolved hostname.
        assert captured["connect_host"] == "93.184.216.34"
        # Host header + TLS SNI preserved the original name.
        assert captured["host_header"] == "api.example.com"
        assert captured["sni"] == "api.example.com"


class TestConnectorEgressSmoke:
    """End-to-end-ish: drive ``DirectRESTAdapter.execute`` with the guard on,
    using a mocked httpx transport so no real socket is opened."""

    def _adapter(self, monkeypatch, *, base_url: str):
        from pocketpaw.connectors.yaml_engine import ConnectorDef, DirectRESTAdapter

        cdef = ConnectorDef(
            name="demo",
            display_name="Demo",
            auth={"method": "none"},
            actions=[{"name": "fetch", "method": "GET", "content_type": ""}],
        )
        adapter = DirectRESTAdapter(cdef)
        adapter._connected = True
        adapter._credentials = {"BASE_URL": base_url}
        return adapter

    def _force_guard(self, monkeypatch, enabled: bool) -> None:
        """Force the egress-guard flag without a full ``Settings.load()``.

        ``DirectRESTAdapter._egress_guard_enabled`` reads ``get_settings()``,
        but reloading settings here would re-validate the developer's workspace
        ``.env`` (which legitimately points several URL fields at localhost) and
        fail. Patching the classmethod keeps this test about the guard wiring,
        not the settings loader — the flag<->settings binding is covered by the
        config test suite.
        """
        from pocketpaw.connectors.yaml_engine import DirectRESTAdapter

        monkeypatch.setattr(
            DirectRESTAdapter, "_egress_guard_enabled", staticmethod(lambda: enabled)
        )

    async def test_blocked_host_returns_egress_error(self, monkeypatch):
        # Guard ON. The connector's declared base-URL host is the only
        # allow-listed host; here that host resolves to an internal IP, so the
        # check blocks it (DNS pre-resolve + internal-range reject).
        self._force_guard(monkeypatch, True)
        monkeypatch.setenv("POCKETPAW_ALLOW_INTERNAL_URLS", "false")
        monkeypatch.setattr(
            socket, "getaddrinfo", _fake_getaddrinfo({"internal.example.com": ["10.1.2.3"]})
        )

        adapter = self._adapter(monkeypatch, base_url="https://internal.example.com")
        result = await adapter.execute("fetch", {"path": "data"})
        assert result.success is False
        assert "egress guard" in result.error.lower()

    async def test_allowed_host_succeeds_through_pinned_transport(self, monkeypatch):
        # Guard ON, public host, mocked inner transport returns 200 JSON.
        self._force_guard(monkeypatch, True)
        monkeypatch.setenv("POCKETPAW_ALLOW_INTERNAL_URLS", "false")
        monkeypatch.setattr(
            socket, "getaddrinfo", _fake_getaddrinfo({"api.example.com": ["93.184.216.34"]})
        )

        seen: dict[str, object] = {}

        async def _fake_inner(self, request):  # noqa: ANN001
            seen["connect_host"] = request.url.host
            seen["host_header"] = request.headers.get("Host")
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "application/json"},
                json={"ok": True, "data": [1, 2, 3]},
            )

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _fake_inner)

        adapter = self._adapter(monkeypatch, base_url="https://api.example.com")
        result = await adapter.execute("fetch", {"path": "orders"})

        assert result.success is True, result.error
        assert result.data == {"ok": True, "data": [1, 2, 3]}
        # Proof the bytes went to the pinned IP through the client transport,
        # with the Host header preserved as the original hostname.
        assert seen["connect_host"] == "93.184.216.34"
        assert seen["host_header"] == "api.example.com"

    async def test_guard_off_uses_pooled_client_unchanged(self, monkeypatch):
        # Guard OFF (default): no allow-list, no pin — the legacy pooled path.
        self._force_guard(monkeypatch, False)

        async def _fake_inner(self, request):  # noqa: ANN001
            # Pooled path leaves the URL host as the hostname (no pin).
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "application/json"},
                json={"pooled": True},
            )

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _fake_inner)

        adapter = self._adapter(monkeypatch, base_url="https://api.example.com")
        result = await adapter.execute("fetch", {"path": "orders"})
        assert result.success is True, result.error
        assert result.data == {"pooled": True}


# ---------------------------------------------------------------------------
# AW-2 — multi-host allow-list, edge cases, and the two AW-1 concern fixes.
# ---------------------------------------------------------------------------


def _force_guard(monkeypatch, enabled: bool) -> None:
    """Force the egress-guard flag without a full ``Settings.load()``.

    Same rationale as ``TestConnectorEgressSmoke._force_guard`` — patching the
    classmethod keeps these tests about guard behavior, not the settings loader
    (the workspace ``.env`` legitimately points URL fields at localhost).
    """
    from pocketpaw.connectors.yaml_engine import DirectRESTAdapter

    monkeypatch.setattr(DirectRESTAdapter, "_egress_guard_enabled", staticmethod(lambda: enabled))


class TestEffectiveAllowList:
    """Unit-test the allow-list builder: auto-seed from declared base-URL host
    + auth-endpoint host + explicit additions, with template resolution."""

    def _adapter(self, **kw):
        from pocketpaw.connectors.yaml_engine import ConnectorDef, DirectRESTAdapter

        cdef = ConnectorDef(name="demo", display_name="Demo", **kw)
        return DirectRESTAdapter(cdef)

    def test_seeds_from_declared_action_hosts(self):
        adapter = self._adapter(
            actions=[
                {"name": "a", "url": "https://api.example.com/v1/x"},
                {"name": "b", "url": "https://api.example.com/v1/y"},
            ]
        )
        assert adapter._effective_allowed_hosts() == {"api.example.com"}

    def test_seeds_auth_endpoint_host_distinct_from_api(self):
        # Two-host connector: API on api.example.com, auth on auth.example.com.
        adapter = self._adapter(
            auth={"method": "bearer", "auth_url": "https://auth.example.com/oauth/token"},
            actions=[{"name": "a", "url": "https://api.example.com/v1/x"}],
        )
        assert adapter._effective_allowed_hosts() == {"api.example.com", "auth.example.com"}

    def test_resolves_templated_host_from_credentials(self):
        # A templated base URL resolves through the SAME substitution execute()
        # applies — the allow-list carries the REAL host, not "{FOO}...".
        adapter = self._adapter(
            actions=[{"name": "a", "url": "https://{REGION}.freshdesk.com/api/v2/tickets"}]
        )
        adapter._credentials = {"REGION": "acme"}
        assert adapter._effective_allowed_hosts() == {"acme.freshdesk.com"}

    def test_unresolved_template_is_dropped_not_allowlisted(self):
        # No credential to fill {REGION} → the template host is dropped, never
        # added as a literal "{region}.freshdesk.com".
        adapter = self._adapter(actions=[{"name": "a", "url": "https://{REGION}.freshdesk.com/x"}])
        assert adapter._effective_allowed_hosts() == set()

    def test_base_url_credential_host_is_seeded(self):
        # The build-from-BASE_URL path host is allow-listed too.
        adapter = self._adapter(actions=[{"name": "a"}])
        adapter._credentials = {"BASE_URL": "https://gitlab.example.com"}
        assert adapter._effective_allowed_hosts() == {"gitlab.example.com"}

    def test_explicit_yaml_allowed_hosts_layered_on_top(self):
        adapter = self._adapter(
            actions=[{"name": "a", "url": "https://api.example.com/x"}],
            allowed_hosts=["cdn.example.com", "Mirror.Example.COM"],
        )
        assert adapter._effective_allowed_hosts() == {
            "api.example.com",
            "cdn.example.com",
            "mirror.example.com",  # normalized lowercase
        }

    def test_workspace_allowed_hosts_from_connect_config(self):
        adapter = self._adapter(actions=[{"name": "a", "url": "https://api.example.com/x"}])
        # Simulate what connect() captures from the WorkspaceConnector config.
        adapter._ws_allowed_hosts = ["extra.example.com"]
        assert adapter._effective_allowed_hosts() == {"api.example.com", "extra.example.com"}

    def test_ipv6_literal_base_url_host_normalized(self):
        # An IPv6-literal base URL keeps its host (brackets stripped) so a
        # request to that literal is allow-listed; the resolved-IP internal
        # check still applies at request time.
        adapter = self._adapter(actions=[{"name": "a", "url": "https://[2606:2800:220:1::1]/x"}])
        assert adapter._effective_allowed_hosts() == {"2606:2800:220:1::1"}


class TestConnectorMultiHost:
    """End-to-end-ish connector.execute() with the guard on and DNS mocked."""

    def _adapter(self, monkeypatch, *, base_url="", actions=None, auth=None, **kw):
        from pocketpaw.connectors.yaml_engine import ConnectorDef, DirectRESTAdapter

        cdef = ConnectorDef(
            name="demo",
            display_name="Demo",
            auth=auth or {"method": "none"},
            actions=actions or [{"name": "fetch", "method": "GET"}],
            **kw,
        )
        adapter = DirectRESTAdapter(cdef)
        adapter._connected = True
        if base_url:
            adapter._credentials = {"BASE_URL": base_url}
        return adapter

    async def test_two_host_connector_both_hosts_succeed(self, monkeypatch):
        # auth host ≠ API host; both are seeded so a call to EITHER works.
        _force_guard(monkeypatch, True)
        monkeypatch.setenv("POCKETPAW_ALLOW_INTERNAL_URLS", "false")
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            _fake_getaddrinfo(
                {
                    "api.example.com": ["93.184.216.34"],
                    "auth.example.com": ["93.184.216.35"],
                }
            ),
        )

        async def _fake_inner(self, request):  # noqa: ANN001
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "application/json"},
                json={"host": request.headers.get("Host")},
            )

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _fake_inner)

        adapter = self._adapter(
            monkeypatch,
            auth={"method": "none", "auth_url": "https://auth.example.com/oauth/token"},
            actions=[
                {"name": "api_call", "method": "GET", "url": "https://api.example.com/v1/orders"},
                {
                    "name": "auth_call",
                    "method": "GET",
                    "url": "https://auth.example.com/oauth/token",
                },
            ],
        )

        api = await adapter.execute("api_call", {})
        assert api.success is True, api.error
        assert api.data == {"host": "api.example.com"}

        auth = await adapter.execute("auth_call", {})
        assert auth.success is True, auth.error
        assert auth.data == {"host": "auth.example.com"}

    async def test_templated_host_resolved_and_allowed(self, monkeypatch):
        # The connector's base URL is templated; it resolves to a concrete host
        # that IS its own declared host, so the guard passes by RESOLVED host.
        _force_guard(monkeypatch, True)
        monkeypatch.setenv("POCKETPAW_ALLOW_INTERNAL_URLS", "false")
        monkeypatch.setattr(
            socket, "getaddrinfo", _fake_getaddrinfo({"acme.freshdesk.com": ["93.184.216.34"]})
        )

        async def _fake_inner(self, request):  # noqa: ANN001
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "application/json"},
                json={"ok": True},
            )

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _fake_inner)

        adapter = self._adapter(
            monkeypatch,
            actions=[
                {
                    "name": "tickets",
                    "method": "GET",
                    "url": "https://{REGION}.freshdesk.com/api/v2/tickets",
                }
            ],
        )
        adapter._credentials = {"REGION": "acme"}
        result = await adapter.execute("tickets", {})
        assert result.success is True, result.error

    async def test_host_outside_declared_topology_blocked(self, monkeypatch):
        # The connector declares api.example.com, but the action URL carries a
        # PER-CALL {host} placeholder filled from params at call time (a classic
        # SSRF vector: caller-controlled host). The substituted host is NOT in
        # the connector's declared topology, so the allow-list rejects it BEFORE
        # any connect — proof the list is meaningful, not the request host echoed
        # back. evil.example.com resolves to a PUBLIC IP, so it is the allow-list
        # (not the internal-IP check) that does the blocking.
        _force_guard(monkeypatch, True)
        monkeypatch.setenv("POCKETPAW_ALLOW_INTERNAL_URLS", "false")
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            _fake_getaddrinfo(
                {"api.example.com": ["93.184.216.34"], "evil.example.com": ["93.184.216.99"]}
            ),
        )

        adapter = self._adapter(
            monkeypatch,
            actions=[
                # The declared host is api.example.com; {host} is a call-time
                # param, never part of the static allow-list seed.
                {"name": "fetch", "method": "GET", "url": "https://{host}/v1/x"},
            ],
        )
        # Seed a credential so the connector's OWN declared host is allow-listed,
        # then drive the request to a different, attacker-supplied host.
        adapter._credentials = {"BASE_URL": "https://api.example.com"}
        result = await adapter.execute("fetch", {"host": "evil.example.com"})
        assert result.success is False
        assert "egress guard" in result.error.lower()
        assert "allow-list" in result.error.lower()

    async def test_cookie_jar_persists_across_calls_under_guard(self, monkeypatch):
        # Concern fix 2: the pinned client is cached per host, so a Set-Cookie
        # from call 1 is sent back on call 2 (session/cookie-auth stays working).
        _force_guard(monkeypatch, True)
        monkeypatch.setenv("POCKETPAW_ALLOW_INTERNAL_URLS", "false")
        monkeypatch.setattr(
            socket, "getaddrinfo", _fake_getaddrinfo({"api.example.com": ["93.184.216.34"]})
        )

        seen_cookies: list[str | None] = []

        async def _fake_inner(self, request):  # noqa: ANN001
            seen_cookies.append(request.headers.get("Cookie"))
            # First response plants a session cookie; httpx stores it in the
            # client's cookie jar and replays it on the next request.
            return httpx.Response(
                200,
                request=request,
                headers={
                    "content-type": "application/json",
                    "set-cookie": "sessionid=abc123; Path=/",
                },
                json={"ok": True},
            )

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _fake_inner)

        adapter = self._adapter(
            monkeypatch,
            actions=[{"name": "fetch", "method": "GET", "url": "https://api.example.com/v1/x"}],
        )

        r1 = await adapter.execute("fetch", {})
        r2 = await adapter.execute("fetch", {})
        assert r1.success is True and r2.success is True

        # Call 1 sent no cookie; call 2 replayed the cookie the jar stored —
        # only possible if the SAME (cached) pinned client served both calls.
        assert seen_cookies[0] is None
        assert seen_cookies[1] == "sessionid=abc123"
        # And exactly one pinned client was cached for the host.
        assert list(adapter._pinned_clients.keys()) == ["api.example.com"]
        await adapter.disconnect("p")
        # disconnect tears the cache down.
        assert adapter._pinned_clients == {}

    async def test_workspace_allowed_hosts_via_connect_config(self, monkeypatch):
        # A host NOT in the YAML but added per-workspace (through connect config)
        # is allow-listed — proves the WorkspaceConnector.allowed_hosts wiring.
        _force_guard(monkeypatch, True)
        monkeypatch.setenv("POCKETPAW_ALLOW_INTERNAL_URLS", "false")
        monkeypatch.setattr(
            socket, "getaddrinfo", _fake_getaddrinfo({"mirror.example.com": ["93.184.216.34"]})
        )

        async def _fake_inner(self, request):  # noqa: ANN001
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "application/json"},
                json={"ok": True},
            )

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _fake_inner)

        from pocketpaw.connectors.yaml_engine import ConnectorDef, DirectRESTAdapter

        cdef = ConnectorDef(
            name="demo",
            display_name="Demo",
            auth={"method": "none"},
            actions=[{"name": "fetch", "method": "GET", "url": "https://mirror.example.com/x"}],
        )
        adapter = DirectRESTAdapter(cdef)
        # connect() captures allowed_hosts from the config blob the cloud layer
        # folds WorkspaceConnector.allowed_hosts into.
        await adapter.connect("p", {"allowed_hosts": ["mirror.example.com"]})
        result = await adapter.execute("fetch", {})
        assert result.success is True, result.error


class TestEgressGuardFailsClosed:
    """Concern fix 1: a settings-load error must NOT silently disable the guard."""

    def test_fails_closed_on_settings_error(self, monkeypatch, caplog):
        import logging

        from pocketpaw.connectors.yaml_engine import DirectRESTAdapter

        def _boom():
            raise RuntimeError("settings blew up")

        # ``_egress_guard_enabled`` imports get_settings locally from
        # pocketpaw.config, so patch it there — that binding is what the method
        # actually resolves.
        import pocketpaw.config as config_mod

        monkeypatch.setattr(config_mod, "get_settings", _boom)

        with caplog.at_level(logging.ERROR):
            enabled = DirectRESTAdapter._egress_guard_enabled()

        # FAIL CLOSED — guard runs (True) rather than silently turning off.
        assert enabled is True
        assert any("FAILING CLOSED" in rec.message for rec in caplog.records)

    def test_returns_flag_when_settings_ok(self, monkeypatch):
        from types import SimpleNamespace

        import pocketpaw.config as config_mod
        from pocketpaw.connectors.yaml_engine import DirectRESTAdapter

        monkeypatch.setattr(
            config_mod, "get_settings", lambda: SimpleNamespace(connector_egress_guard=True)
        )
        assert DirectRESTAdapter._egress_guard_enabled() is True
        monkeypatch.setattr(
            config_mod, "get_settings", lambda: SimpleNamespace(connector_egress_guard=False)
        )
        assert DirectRESTAdapter._egress_guard_enabled() is False
