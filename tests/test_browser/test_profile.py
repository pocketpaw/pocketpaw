# test_profile.py — per-workspace browser profile + imported storage state.
# Created: 2026-09-06 (BR-5, feat/browser-surface-profile).
#
# The imported state is a credential, so the assertions here are about the
# security properties, not the happy path: 0700/0600 modes, per-workspace
# isolation, a hostile import writing NOTHING, and the summary never carrying a
# cookie value. The persistent-context launch is asserted to still install BOTH
# the SSRF route guard and the service-worker block — losing either would delete
# a security control with no visible symptom.
"""Tests for the per-workspace browser profile and imported storage state."""

from __future__ import annotations

import json
import stat
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketpaw.browser import profile
from pocketpaw.browser.driver import BrowserDriver

SECRET = "secret-value-XYZ123"

VALID_STATE = {
    "cookies": [{"name": "session", "value": SECRET, "domain": "portal.example.test", "path": "/"}],
    "origins": [
        {
            "origin": "https://portal.example.test",
            "localStorage": [{"name": "tok", "value": SECRET}],
        }
    ],
}


# --- Directory + file modes ---------------------------------------------------


def test_profile_dir_is_private_and_per_workspace():
    a = profile.profile_dir("ws-a")
    b = profile.profile_dir("ws-b")

    assert a != b
    assert stat.S_IMODE(a.stat().st_mode) == 0o700
    assert stat.S_IMODE(a.parent.stat().st_mode) == 0o700


def test_state_file_is_owner_only():
    profile.write_state("ws-a", profile.validate_storage_state(VALID_STATE))
    assert stat.S_IMODE(profile.state_path("ws-a").stat().st_mode) == 0o600


def test_one_workspace_cannot_read_anothers_state():
    profile.write_state("ws-a", profile.validate_storage_state(VALID_STATE))

    assert profile.read_state("ws-b") is None
    assert profile.summarize("ws-b") is None
    assert profile.read_state("ws-a")["cookies"][0]["value"] == SECRET


@pytest.mark.parametrize("bad", ["../escape", "ws/../..", "a/b", "", "..", "."])
def test_a_traversal_shaped_workspace_id_is_refused(bad):
    with pytest.raises(profile.InvalidStorageState):
        profile.profile_dir(bad)


# --- Summary must never leak --------------------------------------------------


def test_summary_carries_counts_and_never_a_cookie_value():
    profile.write_state("ws-a", profile.validate_storage_state(VALID_STATE))

    summary = profile.summarize("ws-a")

    assert summary["cookie_count"] == 1
    assert summary["domains"] == ["portal.example.test"]
    assert summary["origin_count"] == 1
    assert summary["imported_at"]
    # Against the WHOLE serialized body, not just the fields we thought to check.
    assert SECRET not in json.dumps(summary)


# --- Validation ---------------------------------------------------------------


def test_a_bare_cookie_array_is_accepted():
    state = profile.validate_storage_state(
        [{"name": "a", "value": "b", "domain": ".portal.example.test", "path": "/"}]
    )
    assert state["cookies"][0]["domain"] == "portal.example.test"
    assert state["origins"] == []


def test_an_extension_export_is_normalized():
    state = profile.validate_storage_state(
        [
            {
                "name": "a",
                "value": "b",
                "domain": "portal.example.test",
                "path": "/",
                "expirationDate": 1893456000.5,
                "sameSite": "no_restriction",
                "hostOnly": True,
                "storeId": "0",
            }
        ]
    )
    cookie = state["cookies"][0]
    assert cookie["expires"] == 1893456000.5
    assert cookie["sameSite"] == "None"
    # Fields Playwright does not take are dropped rather than passed through.
    assert "hostOnly" not in cookie
    assert "storeId" not in cookie


@pytest.mark.parametrize(
    "hostile",
    [
        # A bare TLD would offer the cookie to every .com site the browser visits.
        [{"name": "a", "value": "b", "domain": ".com", "path": "/"}],
        [{"name": "a", "value": "b", "domain": "com", "path": "/"}],
        # Neither url nor domain.
        [{"name": "a", "value": "b"}],
        # A non-http scheme.
        [{"name": "a", "value": "b", "url": "file:///etc/passwd"}],
        # Wrong types, wrong shapes.
        [{"name": "a", "value": 1, "domain": "x.example.test"}],
        ["not-an-object"],
        {"cookies": "nope"},
        {"cookies": []},
        "a string",
        # Oversized collections.
        [{"name": "a", "value": "b", "domain": "x.example.test"}] * (profile.MAX_COOKIES + 1),
    ],
)
def test_a_hostile_import_is_refused(hostile):
    with pytest.raises(profile.InvalidStorageState):
        profile.validate_storage_state(hostile)


def test_a_refused_import_writes_nothing():
    with pytest.raises(profile.InvalidStorageState):
        state = profile.validate_storage_state([{"name": "a", "value": "b", "domain": ".com"}])
        profile.write_state("ws-a", state)

    assert not profile.state_path("ws-a").exists()


def test_a_validation_message_never_quotes_the_value():
    with pytest.raises(profile.InvalidStorageState) as exc:
        profile.validate_storage_state([{"name": "a", "value": SECRET, "domain": ".com"}])

    assert SECRET not in str(exc.value)
    assert "cookies[0].domain" in str(exc.value)


def test_a_corrupt_state_file_does_not_brick_the_browser():
    profile.state_path("ws-a").write_text("{ not json")
    assert profile.read_state("ws-a") is None
    assert profile.summarize("ws-a") is None


def test_delete_removes_the_whole_profile():
    profile.write_state("ws-a", profile.validate_storage_state(VALID_STATE))
    (profile.profile_dir("ws-a") / "Cookies").write_text("persisted-by-chromium")

    assert profile.delete_profile("ws-a") is True
    assert not (profile.profiles_root() / "ws-a").exists()
    assert profile.delete_profile("ws-a") is False


# --- Persistent-context launch keeps every security control -------------------


@pytest.mark.asyncio
async def test_persistent_launch_keeps_the_ssrf_guard_and_blocks_service_workers(tmp_path):
    """Mutation: drop ``service_workers`` or the ``context.route`` call from the
    persistent branch of ``launch`` and this fails."""
    context = MagicMock()
    context.route = AsyncMock()
    context.pages = []
    context.new_page = AsyncMock(return_value=MagicMock())
    chromium = MagicMock()
    chromium.launch_persistent_context = AsyncMock(return_value=context)
    pw = MagicMock(chromium=chromium)

    driver = BrowserDriver()
    with patch("playwright.async_api.async_playwright") as ap:
        ap.return_value.start = AsyncMock(return_value=pw)
        await driver.launch(user_data_dir=tmp_path / "prof")

    args, kwargs = chromium.launch_persistent_context.call_args
    assert args[0] == str(tmp_path / "prof")
    assert kwargs["service_workers"] == "block"
    context.route.assert_awaited_once()
    assert context.route.await_args.args[0] == "**/*"
    assert context.route.await_args.args[1] == driver._ssrf_route
    assert driver.is_launched


@pytest.mark.asyncio
async def test_persistent_launch_reuses_the_blank_page_the_profile_opens_with(tmp_path):
    page = MagicMock()
    context = MagicMock()
    context.route = AsyncMock()
    context.pages = [page]
    context.new_page = AsyncMock()
    chromium = MagicMock()
    chromium.launch_persistent_context = AsyncMock(return_value=context)

    driver = BrowserDriver()
    with patch("playwright.async_api.async_playwright") as ap:
        ap.return_value.start = AsyncMock(return_value=MagicMock(chromium=chromium))
        await driver.launch(user_data_dir=tmp_path / "prof")

    assert driver._page is page
    context.new_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_persistent_launch_is_unchanged(tmp_path):
    """Default stays ``None``, so the old Browser -> new_context path still runs."""
    context = MagicMock()
    context.route = AsyncMock()
    context.pages = []
    context.new_page = AsyncMock(return_value=MagicMock())
    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    chromium = MagicMock()
    chromium.launch = AsyncMock(return_value=browser)

    driver = BrowserDriver()
    with patch("playwright.async_api.async_playwright") as ap:
        ap.return_value.start = AsyncMock(return_value=MagicMock(chromium=chromium))
        await driver.launch()

    chromium.launch_persistent_context.assert_not_called()
    assert browser.new_context.await_args.kwargs["service_workers"] == "block"
    assert driver._browser is browser


@pytest.mark.asyncio
async def test_apply_storage_state_adds_cookies_and_replays_local_storage():
    driver = BrowserDriver()
    driver._context = MagicMock(add_cookies=AsyncMock(), add_init_script=AsyncMock())

    await driver.apply_storage_state(profile.validate_storage_state(VALID_STATE))

    cookies = driver._context.add_cookies.await_args.args[0]
    assert cookies[0]["name"] == "session"
    script = driver._context.add_init_script.await_args.args[0]
    assert "https://portal.example.test" in script
    assert "localStorage" in script


# --- Session manager wiring ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_session_launches_on_its_profile_and_reapplies_the_import():
    """Chromium DROPS session cookies on exit, so the persistent profile alone
    is not enough — the import has to be re-applied on every launch. Test 3 of
    BR-5 (survive ``close_all`` then a new session) is this behaviour."""
    from pocketpaw.browser.session import BrowserSessionManager

    profile.write_state("ws-a", profile.validate_storage_state(VALID_STATE))
    manager = BrowserSessionManager()

    with patch("pocketpaw.browser.session.BrowserDriver") as MockDriver:
        driver = MockDriver.return_value
        driver.launch = AsyncMock()
        driver.close = AsyncMock()
        driver.apply_storage_state = AsyncMock()
        driver.is_launched = True

        await manager.get_or_create("ws-a")
        assert driver.launch.await_args.kwargs["user_data_dir"] == profile.profile_dir("ws-a")
        assert driver.apply_storage_state.await_count == 1

        await manager.close_all()
        await manager.get_or_create("ws-a")

        # Second launch: same profile directory, import applied again.
        assert driver.launch.await_args.kwargs["user_data_dir"] == profile.profile_dir("ws-a")
        assert driver.apply_storage_state.await_count == 2
        assert driver.apply_storage_state.await_args.args[0]["cookies"][0]["value"] == SECRET


@pytest.mark.asyncio
async def test_a_session_without_an_import_launches_clean():
    from pocketpaw.browser.session import BrowserSessionManager

    manager = BrowserSessionManager()
    with patch("pocketpaw.browser.session.BrowserDriver") as MockDriver:
        driver = MockDriver.return_value
        driver.launch = AsyncMock()
        driver.apply_storage_state = AsyncMock()
        driver.is_launched = True

        await manager.get_or_create("ws-fresh")

        driver.apply_storage_state.assert_not_awaited()


# --- Live browser: an imported cookie actually reaches the site ---------------


@pytest.mark.asyncio
async def test_an_imported_cookie_is_sent_on_the_next_navigate(tmp_path):
    """End to end through real Chromium: import a cookie, navigate, and read it
    back out of the request the page made.

    Served by a fulfilled Playwright route rather than a local HTTP server: the
    SSRF guard refuses 127.0.0.1 on purpose. ``example.com`` is used because
    ``navigate``'s pre-check resolves the host before Chromium sees it — the
    route then fulfills the request, so no traffic actually leaves.
    """
    from pocketpaw.security.safe_fetch import BlockedURLError

    driver = BrowserDriver()
    try:
        await driver.launch(user_data_dir=tmp_path / "prof")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no usable Chromium: {exc}")

    try:
        await driver.apply_storage_state(
            profile.validate_storage_state(
                [{"name": "session", "value": SECRET, "domain": "example.com", "path": "/"}]
            )
        )

        async def _fulfill(route):
            cookie = route.request.headers.get("cookie", "")
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=f"<html><body><p>cookie-header: {cookie}</p></body></html>",
            )

        # Registered on the PAGE, which is checked before the context's SSRF
        # route, so the fake response never needs to leave the machine.
        await driver._page.route("https://example.com/**", _fulfill)

        try:
            result = await driver.navigate("https://example.com/dashboard")
        except BlockedURLError:
            pytest.skip("no DNS — navigate's public-address pre-check cannot run")
        assert f"session={SECRET}" in result.snapshot
    finally:
        await driver.close()
