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
