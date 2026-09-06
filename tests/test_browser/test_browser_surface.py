# test_browser_surface.py — the BR-1 trust boundaries, against a REAL browser.
# Created: 2026-09-06 (BR-1, feat/browser-surface-server).
#
# WHY A REAL BROWSER. The pre-existing driver tests mock ``page`` wholesale, and
# that is precisely how ``page.accessibility.snapshot()`` stayed green for months
# after Playwright deleted the API — the mock had the attribute, the real Page
# did not. The two claims this slice actually makes (a request to a private
# address never leaves; a credential field is never typed into) are claims about
# Chromium's behavior, so they are tested against Chromium. When no browser is
# installed the tests SKIP rather than fail, so CI without one stays green.
#
# The surface-scoping test needs no browser and always runs.

from __future__ import annotations

import asyncio
import socket

import pytest

from pocketpaw.browser import BrowserDriver

pytestmark = pytest.mark.asyncio

# 169.254.169.254 is the cloud metadata endpoint — the single highest-value SSRF
# target on any hosted deploy.
METADATA_URL = "http://169.254.169.254/latest/meta-data/"


@pytest.fixture
async def driver():
    d = BrowserDriver()

    # ``launch`` auto-downloads Chromium (~150MB) when none is installed. On a CI
    # box without one that is a slow surprise, not a test — so refuse the install
    # and let the skip below fire instead.
    async def _no_install():
        raise RuntimeError("browser auto-install disabled under test")

    d._install_chromium = _no_install  # type: ignore[method-assign]

    try:
        await d.launch()
    except Exception as exc:  # noqa: BLE001 — no Chrome/Chromium on this box
        pytest.skip(f"no browser available: {exc}")
    try:
        yield d
    finally:
        await d.close()


@pytest.fixture
async def egress():
    """Skip when the box cannot reach the public internet.

    The "a public host IS allowed" half of the SSRF contract cannot be tested
    against a local fixture — loopback is precisely what the guard blocks — so
    those two tests need real egress. Everything else here runs offline.
    """
    try:
        await asyncio.get_running_loop().getaddrinfo("example.com", 443)
    except (socket.gaierror, OSError) as exc:
        pytest.skip(f"no egress: {exc}")


class TestRequestLevelSsrf:
    async def test_subresource_to_metadata_ip_is_aborted(self, driver):
        """A page that EMBEDS the metadata endpoint must not reach it.

        Subresources are the case a navigate-only URL check misses entirely: the
        agent never types this URL, the page does.

        THE MUTATION THAT BREAKS THIS: drop the ``context.route`` registration in
        ``BrowserDriver.launch`` — the img request succeeds and blocked_hosts
        stays empty.
        """
        page = driver._page
        failures: list[str] = []
        page.on("requestfailed", lambda r: failures.append(r.url))

        await page.set_content(f'<html><body><img src="{METADATA_URL}"></body></html>')
        await page.wait_for_timeout(1500)

        assert "169.254.169.254" in driver.blocked_hosts
        assert any("169.254.169.254" in u for u in failures)

    async def test_navigate_to_loopback_is_blocked(self, driver):
        """A top-level navigation to loopback is blocked too — the guard is on
        the request, not on one entry point."""
        with pytest.raises(Exception):  # noqa: B017 — Playwright's net::ERR_FAILED
            await driver.navigate("http://127.0.0.1:9/")
        assert any(h.startswith("127.0.0.1") for h in driver.blocked_hosts)

    async def test_session_survives_a_block(self, driver, egress):
        """One blocked URL must not brick the browser for the next one.

        THE MUTATION THAT BREAKS THIS: remove the about:blank reset from
        ``BrowserDriver.navigate``'s except branch — Chromium's own error page
        lands mid-flight and interrupts the following goto with
        ``interrupted by another navigation to "chrome-error://chromewebdata/"``.
        """
        with pytest.raises(Exception):  # noqa: B017
            await driver.navigate(METADATA_URL)
        result = await driver.navigate("https://example.com")
        assert "Example Domain" in result.snapshot

    async def test_public_host_is_allowed(self, driver, egress):
        """The guard must not block the ordinary case."""
        result = await driver.navigate("https://example.com")
        assert "Example Domain" in result.snapshot
        assert driver.blocked_hosts == []


class TestCredentialRefusal:
    async def test_type_into_password_field_is_refused_and_types_nothing(self, driver, monkeypatch):
        """The hard boundary: the agent may fill a search box, never a password.

        THE MUTATION THAT BREAKS THIS: delete the ``_is_credential_field`` guard
        in ``_type_handler`` — the field ends up holding "hunter2".
        """
        from pocketpaw_ee.agent.mcp_servers import browser as browser_mcp

        page = driver._page
        await page.set_content(
            '<html><body><input type="password" id="pw">'
            '<input type="text" id="q" name="query"></body></html>'
        )
        await driver.snapshot()

        # Pin identity + the shared driver so the handler runs the real path.
        monkeypatch.setattr(browser_mcp, "_identity", lambda: ("ws-test", "user-test"))

        async def _fake_driver(_ws):
            return driver

        monkeypatch.setattr(browser_mcp, "_driver", _fake_driver)

        # The password input is the first interactive element, so it is ref 1.
        out = await browser_mcp._type_handler({"ref": 1, "text": "hunter2"})

        assert out.get("is_error") is True
        assert "Refused" in out["content"][0]["text"]
        assert await page.locator("#pw").input_value() == ""

    async def test_type_into_a_plain_text_field_is_allowed(self, driver, monkeypatch):
        """The refusal must be a scalpel, not a ban on typing."""
        from pocketpaw_ee.agent.mcp_servers import browser as browser_mcp

        page = driver._page
        await page.set_content('<html><body><input type="text" id="q" name="query"></body></html>')
        await driver.snapshot()

        monkeypatch.setattr(browser_mcp, "_identity", lambda: ("ws-test", "user-test"))

        async def _fake_driver(_ws):
            return driver

        monkeypatch.setattr(browser_mcp, "_driver", _fake_driver)

        out = await browser_mcp._type_handler({"ref": 1, "text": "kettles"})
        assert out.get("is_error") is not True
        assert await page.locator("#q").input_value() == "kettles"


class TestCredentialClassifier:
    """The classifier on its own — no browser needed."""

    @pytest.mark.parametrize(
        "info",
        [
            {"type": "password"},
            {"type": "text", "autocomplete": "one-time-code"},
            {"type": "text", "autocomplete": "cc-number"},
            {"type": "text", "name": "user_password"},
            {"type": "text", "id": "otp-input"},
            {"type": "text", "name": "cardNumber"},
        ],
    )
    async def test_credential_shapes_are_refused(self, info):
        from pocketpaw_ee.agent.mcp_servers.browser import _is_credential_field

        assert _is_credential_field({"type": "", "autocomplete": "", "name": "", "id": "", **info})

    @pytest.mark.parametrize(
        "info",
        [
            {"type": "text", "name": "q"},
            {"type": "email", "name": "email", "autocomplete": "email"},
            {"type": "text", "name": "street-address"},
        ],
    )
    async def test_ordinary_fields_are_allowed(self, info):
        from pocketpaw_ee.agent.mcp_servers.browser import _is_credential_field

        assert not _is_credential_field(
            {"type": "", "autocomplete": "", "name": "", "id": "", **info}
        )


class TestAudit:
    async def test_one_audit_row_per_action_including_refusals(self, monkeypatch):
        """Every browser action leaves exactly one audit row — refusals too.

        THE MUTATION THAT BREAKS THIS: move the ``_audit`` call in
        ``_type_handler`` below the credential ``return`` — the refusal (the row
        a security reviewer most wants) stops being recorded.
        """
        from pocketpaw_ee.agent.mcp_servers import browser as browser_mcp

        rows: list[dict] = []
        monkeypatch.setattr(
            browser_mcp, "record_tool_call", lambda **kw: rows.append(kw), raising=True
        )
        monkeypatch.setattr(browser_mcp, "_identity", lambda: ("ws-a", "user-a"))

        class _FakeDriver:
            current_url = "https://example.com/"
            blocked_hosts: list[str] = []

            async def field_info(self, ref):
                return {"type": "password", "autocomplete": "", "name": "", "id": ""}

            async def snapshot(self):
                class _R:
                    snapshot = "Page: x"

                return _R()

        async def _fake_driver(_ws):
            return _FakeDriver()

        monkeypatch.setattr(browser_mcp, "_driver", _fake_driver)

        await browser_mcp._snapshot_handler({})
        assert len(rows) == 1
        assert rows[0]["tool_name"] == "snapshot"
        assert rows[0]["ok"] is True

        out = await browser_mcp._type_handler({"ref": 1, "text": "hunter2"})
        assert out.get("is_error") is True
        assert len(rows) == 2
        assert rows[1]["tool_name"] == "type"
        assert rows[1]["ok"] is False
        assert rows[1]["metadata"]["reason"] == "credential_field_refused"
        # The typed value must never reach the audit log.
        assert "hunter2" not in str(rows[1])

    async def test_no_workspace_refuses_rather_than_sharing_a_browser(self, monkeypatch):
        """A run with no workspace must NOT fall back to a shared session key."""
        from pocketpaw_ee.agent.mcp_servers import browser as browser_mcp

        monkeypatch.setattr(browser_mcp, "_identity", lambda: (None, None))
        out = await browser_mcp._navigate_handler({"url": "https://example.com"})
        assert out["is_error"] is True
        assert "workspace" in out["content"][0]["text"].lower()


class TestAuditHygiene:
    async def test_query_string_is_stripped_before_auditing(self):
        """A magic-link URL must not deposit its token in the audit log.

        THE MUTATION THAT BREAKS THIS: drop the ``_safe_url`` call in ``_audit``
        — the token lands in Mongo verbatim.
        """
        from pocketpaw_ee.agent.mcp_servers.browser import _safe_url

        assert _safe_url("https://x.test/a/b?token=SECRET#frag") == "https://x.test/a/b"
        assert _safe_url(None) is None


class TestSurfaceScoping:
    """No browser needed — the profile table is pure data."""

    async def test_browser_surface_allows_the_tools(self):
        from pocketpaw_ee.agent.mcp_servers.browser import BROWSER_TOOL_IDS
        from pocketpaw_ee.cloud.surface import SurfaceKind, SurfaceMeta, resolve_profile

        profile = resolve_profile(SurfaceKind.BROWSER, SurfaceMeta())
        assert profile.allow_mcp_tool_ids is not None
        assert set(BROWSER_TOOL_IDS) <= profile.allow_mcp_tool_ids
        assert not (set(BROWSER_TOOL_IDS) & profile.deny_mcp_tool_ids)

    async def test_every_other_surface_denies_the_tools(self):
        """Including /chat and the unmapped default, which carry no allow-list
        and would otherwise reach the browser.

        THE MUTATION THAT BREAKS THIS: drop the ``_deny_browser_off_surface``
        call from ``resolve_profile`` — /chat's deny set comes back empty.
        """
        from pocketpaw_ee.agent.mcp_servers.browser import BROWSER_TOOL_IDS
        from pocketpaw_ee.cloud.surface import SurfaceKind, SurfaceMeta, resolve_profile

        for kind in SurfaceKind:
            if kind is SurfaceKind.BROWSER:
                continue
            profile = resolve_profile(kind, SurfaceMeta())
            assert set(BROWSER_TOOL_IDS) <= profile.deny_mcp_tool_ids, kind
