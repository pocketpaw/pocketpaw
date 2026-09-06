# Playwright browser driver wrapper
# Changes: 2026-09-06 (BR-5, feat/browser-surface-profile) — ``launch()`` takes an
#   optional ``user_data_dir``. With it set, the browser starts via
#   ``chromium.launch_persistent_context(...)``, which returns a BrowserContext
#   and NO Browser — so ``self._context`` is now the object the driver keys on
#   (``is_launched``, ``close``), and ``self._browser`` stays None on that path.
#   The SSRF ``context.route`` guard and ``service_workers="block"`` are applied
#   to the context in ONE place for both paths: losing either on the persistent
#   path would silently delete a security control. Default stays None, so every
#   existing caller and test behaves exactly as before.
#   Also ``apply_storage_state()``: installs an imported (user-exported) session
#   — cookies via ``add_cookies``, localStorage via an init script. The driver
#   never reads that state from disk; ``session.py`` hands it in.
# Changes: 2026-09-06 (BR-4, feat/browser-surface-extract) — added
#   ``content_html()``: the page's rendered HTML, which the EE ``extract`` tool
#   converts to markdown for READING. Snapshots stay the acting surface.
# Changes: 2026-09-06 (BR-1, feat/browser-surface-server) — three changes:
#   1. ``_take_snapshot`` runs ``page.evaluate(SNAPSHOT_JS)`` instead of
#      ``page.accessibility.snapshot()``. Playwright REMOVED ``page.accessibility``
#      (1.58 raises ``AttributeError``), so every snapshot call was failing; the
#      DOM-walk replacement lives in ``snapshot.py``. click/type now resolve
#      through the ``[data-paw-ref="N"]`` selectors that walk stamps.
#   2. REQUEST-LEVEL SSRF. ``context.route("**/*", ...)`` aborts any request whose
#      host does not resolve to a public address, reusing
#      ``pocketpaw.security.safe_fetch.assert_public_url`` (one IP-rule
#      implementation for the whole codebase). This catches clicks, redirects,
#      subresources and JS fetches — not just the top-level navigate.
#   3. ``--no-sandbox`` is passed ONLY inside a container (``/.dockerenv`` or
#      ``POCKETPAW_IN_CONTAINER``); never on a dev machine.
#   ``navigate`` raises ``BlockedURLError`` (not a raw Chromium net::ERR_FAILED)
#   when OUR guard is what aborted the request, so the agent is told the address
#   is unreachable instead of being handed a transient-looking error to retry.
#   ``navigate`` also waits for the page to SETTLE when a goto fails: the call
#   raises while its navigation is still in flight, Chromium lands its own
#   ``chrome-error://chromewebdata/`` page, and that interrupts the NEXT goto —
#   so one blocked URL used to brick the session for every URL after it.
#   Also added ``screenshot_png()`` (bytes, no disk) and ``field_info()`` (live
#   attributes of a ref'd element) for the EE browser MCP server's credential
#   refusal — the driver reports, the tool decides.
#
# Wraps Playwright browser automation with methods for navigate, click,
# type, scroll, snapshot, and screenshot.
# Uses system Chrome if available, auto-installs Chromium if needed.
"""Playwright browser driver wrapper."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from pocketpaw.security.safe_fetch import assert_public_url

from .snapshot import SNAPSHOT_JS, RefMap, render_snapshot

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page, Playwright, Route


def _in_container() -> bool:
    """True when this process is running inside a container.

    Chromium needs ``--no-sandbox`` there (no user namespaces); passing it on a
    dev machine would needlessly drop the sandbox, so it is container-only.
    """
    return (
        os.environ.get("POCKETPAW_IN_CONTAINER", "").lower() in {"1", "true", "yes"}
        or Path("/.dockerenv").exists()
    )


logger = logging.getLogger(__name__)


@dataclass
class NavigationResult:
    """Result of a navigation or interaction that returns page state."""

    snapshot: str
    refmap: RefMap


class BrowserDriver:
    """Playwright browser driver wrapper.

    Provides a simplified async interface for browser automation
    with accessibility tree snapshots for LLM control.

    Usage:
        async with BrowserDriver() as driver:
            result = await driver.navigate("https://example.com")
            print(result.snapshot)
            await driver.click(ref=1)
    """

    # Default viewport size
    DEFAULT_VIEWPORT = {"width": 1280, "height": 720}

    # Scroll amount in pixels
    SCROLL_AMOUNT = 500

    def __init__(self, headless: bool = True) -> None:
        """Initialize the browser driver.

        Args:
            headless: Whether to run browser in headless mode (default True)
        """
        self.headless = headless
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        # The context is the object BOTH launch paths produce; a persistent
        # profile yields a context with no Browser behind it at all.
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._refmap: RefMap = RefMap()
        # host -> allowed? Memoized per driver so a page with dozens of
        # subresources does not re-run getaddrinfo per request.
        self._host_verdicts: dict[str, bool] = {}
        # Hosts this driver actually blocked, for the caller's audit trail.
        self.blocked_hosts: list[str] = []

        # Verify playwright is installed early (fail fast with helpful message)
        try:
            import playwright  # noqa: F401
        except ImportError:
            from pocketpaw._compat import require_extra

            require_extra("playwright", "browser")

    async def __aenter__(self) -> BrowserDriver:
        """Async context manager entry - launches browser."""
        await self.launch()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit - closes browser."""
        await self.close()

    @property
    def is_launched(self) -> bool:
        """Check if browser is launched and ready."""
        return self._context is not None and self._page is not None

    @property
    def current_url(self) -> str | None:
        """Get current page URL or None if not launched."""
        if self._page is None:
            return None
        return self._page.url

    # Options every context gets, whichever way it was launched. Kept in one
    # place so the persistent path cannot quietly lose the worker block:
    # ``context.route`` does NOT intercept requests made from inside a Service
    # Worker, so a page that registers one could fetch a private address
    # straight past the SSRF guard. (WebSocket traffic is still uncovered —
    # that needs ``route_web_socket``; a known gap, not fixed here.)
    CONTEXT_OPTIONS = {
        "viewport": DEFAULT_VIEWPORT,
        "service_workers": "block",
    }

    async def launch(self, user_data_dir: Path | None = None) -> None:
        """Launch the browser.

        Tries in order:
        1. System Chrome (no download needed)
        2. Playwright's bundled Chromium (auto-installs if missing)

        Args:
            user_data_dir: When set, launch a PERSISTENT context rooted at this
                directory, so cookies and localStorage survive an idle close and
                a container restart. That call returns a BrowserContext and no
                Browser, which is why the driver keys on ``self._context``.
        """
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()

        # Containers have no user namespaces, so Chromium's sandbox cannot
        # start there. Dev machines keep the sandbox.
        launch_args = ["--no-sandbox"] if _in_container() else []

        if user_data_dir is not None:
            # A container killed mid-run leaves Chromium's singleton locks
            # behind and the next launch refuses with "profile in use". They are
            # stale by definition here — one browser per profile.
            for lock in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
                Path(user_data_dir, lock).unlink(missing_ok=True)

            def _launch(**kw):
                return self._playwright.chromium.launch_persistent_context(
                    str(user_data_dir), **kw, **self.CONTEXT_OPTIONS
                )

            self._context = await self._launch_with_fallback(_launch, launch_args)
        else:

            def _launch(**kw):
                return self._playwright.chromium.launch(**kw)

            self._browser = await self._launch_with_fallback(_launch, launch_args)
            self._context = await self._browser.new_context(**self.CONTEXT_OPTIONS)

        # SSRF at the REQUEST level: every request Chromium makes — top-level
        # navigation, redirect hop, subresource, JS fetch — is checked before it
        # leaves. A URL that resolves to a private / loopback / link-local /
        # metadata address is aborted.
        await self._context.route("**/*", self._ssrf_route)
        # A persistent context opens with a blank page already; reuse it rather
        # than leaving an orphan tab behind.
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()

    async def _launch_with_fallback(self, launch, launch_args: list[str]):
        """Run ``launch`` against system Chrome, then bundled Chromium, then a
        freshly installed Chromium. One ladder, both launch modes."""
        try:
            result = await launch(headless=self.headless, channel="chrome", args=launch_args)
            logger.info("Using system Chrome")
            return result
        except Exception as e:  # noqa: BLE001
            logger.debug(f"System Chrome not available: {e}")

        try:
            result = await launch(headless=self.headless, args=launch_args)
            logger.info("Using Playwright Chromium")
            return result
        except Exception as install_error:
            if "Executable doesn't exist" not in str(install_error):
                raise
        logger.info("Installing Chromium browser (one-time download)...")
        await self._install_chromium()
        result = await launch(headless=self.headless, args=launch_args)
        logger.info("Using Playwright Chromium (freshly installed)")
        return result

    async def apply_storage_state(self, state: dict) -> None:
        """Install an imported, already-authenticated session on this context.

        Cookies go in directly. localStorage cannot be written without a page on
        the origin, so it is replayed by an init script that runs before any
        page script on a matching origin.

        The argument is a CREDENTIAL. It is never logged here and never leaves
        this call.
        """
        if self._context is None:
            raise RuntimeError("Browser not launched. Call launch() first.")
        cookies = state.get("cookies") or []
        if cookies:
            await self._context.add_cookies(cookies)
        origins = state.get("origins") or []
        if origins:
            by_origin = json.dumps({o["origin"]: o["localStorage"] for o in origins})
            script = (
                f"(() => {{ const byOrigin = {by_origin};"
                " const items = byOrigin[window.location.origin]; if (!items) return;"
                " try { for (const it of items) window.localStorage.setItem(it.name, it.value); }"
                " catch (e) {} })()"
            )
            await self._context.add_init_script(script)

    async def _ssrf_route(self, route: Route) -> None:
        """Abort any request whose host does not resolve to a public address."""
        from pocketpaw.security.safe_fetch import SafeFetchError, assert_public_url

        url = route.request.url
        host = ""
        try:
            from urllib.parse import urlparse

            host = urlparse(url).netloc
        except Exception:  # noqa: BLE001
            host = url

        verdict = self._host_verdicts.get(host)
        if verdict is None:
            try:
                await assert_public_url(url)
                verdict = True
            except SafeFetchError:
                verdict = False
            except Exception:  # noqa: BLE001 — never let the guard crash the page
                logger.warning("SSRF check errored for %s; blocking", host, exc_info=True)
                verdict = False
            self._host_verdicts[host] = verdict

        if not verdict:
            if host not in self.blocked_hosts:
                self.blocked_hosts.append(host)
            logger.info("browser SSRF: blocked request to %s", host)
            await route.abort()
            return
        await route.continue_()

    async def _install_chromium(self) -> None:
        """Auto-install Playwright's Chromium browser."""
        logger.info("Downloading Chromium browser (~150MB)...")

        # Run playwright install chromium
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "playwright",
            "install",
            "chromium",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            error_msg = stderr.decode() if stderr else "Unknown error"
            raise RuntimeError(f"Failed to install Chromium: {error_msg}")

        logger.info("Chromium installed successfully")

    async def close(self) -> None:
        """Close the browser and cleanup resources."""
        # Persistent path: the context IS the browser, and closing it is what
        # flushes cookies to the profile directory.
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:  # noqa: BLE001 — already gone is fine
                logger.debug("browser context close failed", exc_info=True)
            self._context = None
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        self._page = None
        self._refmap = RefMap()
        self._host_verdicts.clear()

    def _require_page(self) -> Page:
        """Get page or raise if not launched."""
        if self._page is None:
            raise RuntimeError("Browser not launched. Call launch() first.")
        return self._page

    async def _take_snapshot(self) -> NavigationResult:
        """Snapshot the current page by walking the visible DOM.

        ``page.accessibility`` no longer exists in Playwright, so this evaluates
        ``SNAPSHOT_JS`` instead: it stamps ``data-paw-ref="N"`` on interactive
        elements and returns the semantic text alongside the ref count.
        """
        page = self._require_page()

        result = await page.evaluate(SNAPSHOT_JS)
        snapshot_text, refmap = render_snapshot(result or {})

        # Store refmap for future interactions
        self._refmap = refmap

        return NavigationResult(snapshot=snapshot_text, refmap=refmap)

    async def field_info(self, ref: int) -> dict[str, str]:
        """Report the live attributes of a ref'd element.

        Read at ACTION time, not snapshot time — the DOM can change between the
        two, and a credential check on stale attributes is not a check. Callers
        (the EE browser MCP server) decide policy; the driver only reports.
        """
        page = self._require_page()
        selector = self._refmap.get_selector(ref)
        if selector is None:
            raise ValueError(f"Invalid ref: {ref}. Element not found in current snapshot.")
        info = await page.locator(selector).evaluate(
            "el => ({tag: el.tagName || '', type: el.type || '', "
            "autocomplete: el.getAttribute('autocomplete') || '', "
            "name: el.getAttribute('name') || '', id: el.id || ''})"
        )
        return {k: str(v) for k, v in (info or {}).items()}

    async def navigate(self, url: str) -> NavigationResult:
        """Navigate to a URL and return page snapshot.

        Args:
            url: The URL to navigate to

        Returns:
            NavigationResult with snapshot text and refmap
        """
        page = self._require_page()

        # Check BEFORE handing the URL to Chromium. The ``context.route`` guard
        # is still the real boundary — it is the only thing that sees redirect
        # hops, subresources and JS fetches — but letting it abort a top-level
        # navigation is self-harm: the abort makes Chromium load its own
        # ``chrome-error://chromewebdata/`` page, and that navigation is still
        # committing when the NEXT goto starts, which Playwright reports as
        # "interrupted by another navigation". One blocked address then bricked
        # the session for every URL after it. Settling with
        # ``wait_for_load_state`` does NOT fix it (the error page has already
        # reached domcontentloaded, so the wait returns while the commit is
        # in flight) — verified by live smoke, one block was enough.
        # Refusing here means the common case never reaches the browser at all.
        from pocketpaw.security.safe_fetch import BlockedURLError, SafeFetchError

        try:
            await assert_public_url(url)
        except SafeFetchError as exc:
            # Record it exactly as ``_ssrf_route`` would have. The pre-check
            # short-circuits that handler, so without this a caller-visible
            # block would be missing from ``blocked_hosts`` and the verdict
            # cache — the same block reported two different ways depending on
            # which layer caught it.
            host = urlparse(url).netloc
            self._host_verdicts[host] = False
            if host not in self.blocked_hosts:
                self.blocked_hosts.append(host)
            raise BlockedURLError(f"Blocked URL: {exc}") from None

        try:
            await page.goto(url, wait_until="domcontentloaded")
        except Exception:
            # A failed goto RAISES while its navigation is still in flight —
            # aborted by the SSRF guard, Chromium then loads its own
            # ``chrome-error://`` page. That in-flight navigation interrupts
            # whatever goto comes next, so one blocked URL bricked the session
            # for every URL after it. Let the error page land before re-raising.
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:  # noqa: BLE001 — best-effort settle
                logger.debug("page did not settle after a failed navigation", exc_info=True)
            # Chromium reports an aborted request as a generic net::ERR_FAILED,
            # which reads to an agent like a transient error worth retrying. If
            # OUR guard is what aborted it, say so — and say so on the SECOND
            # attempt at the same host too, which is why this reads the verdict
            # cache rather than watching ``blocked_hosts`` grow.
            # Still reachable when a PUBLIC url redirects to a private one: the
            # pre-check above passed, the route guard aborted the redirect hop.
            # Park on about:blank so the dead error page is not left in flight
            # to interrupt the next call, then report our own verdict.
            try:
                await page.goto("about:blank", wait_until="domcontentloaded", timeout=5000)
            except Exception:  # noqa: BLE001 — best-effort reset
                logger.debug("could not reset page after a failed navigation", exc_info=True)

            if self._host_verdicts.get(urlparse(url).netloc) is False:
                raise BlockedURLError("Blocked URL: resolved to non-public IP address.") from None
            raise

        return await self._take_snapshot()

    async def click(self, ref: int) -> NavigationResult:
        """Click an element by its reference number.

        Args:
            ref: The reference number from the snapshot

        Returns:
            NavigationResult with updated page state
        """
        page = self._require_page()

        selector = self._refmap.get_selector(ref)
        if selector is None:
            raise ValueError(f"Invalid ref: {ref}. Element not found in current snapshot.")

        locator = page.locator(selector)
        await locator.click()

        # Return updated snapshot
        return await self._take_snapshot()

    async def type_text(self, ref: int, text: str) -> str:
        """Type text into an element by its reference number.

        Uses fill() which replaces any existing content.

        Args:
            ref: The reference number from the snapshot
            text: The text to type

        Returns:
            Confirmation message
        """
        page = self._require_page()

        selector = self._refmap.get_selector(ref)
        if selector is None:
            raise ValueError(f"Invalid ref: {ref}. Element not found in current snapshot.")

        locator = page.locator(selector)
        await locator.fill(text)

        return f"Typed text into element [ref={ref}]"

    async def scroll(self, direction: str = "down") -> NavigationResult:
        """Scroll the page.

        Args:
            direction: "up" or "down"

        Returns:
            NavigationResult with updated page state
        """
        page = self._require_page()

        if direction not in ("up", "down"):
            raise ValueError(f"Invalid direction: {direction}. Must be 'up' or 'down'.")

        amount = self.SCROLL_AMOUNT if direction == "down" else -self.SCROLL_AMOUNT

        await page.evaluate(f"window.scrollBy(0, {amount})")

        return await self._take_snapshot()

    async def snapshot(self) -> NavigationResult:
        """Get current page snapshot without any interaction.

        Returns:
            NavigationResult with current page state
        """
        return await self._take_snapshot()

    async def screenshot(self, path: str | None = None) -> str:
        """Take a screenshot of the current page.

        Args:
            path: Path to save screenshot. If None, uses default timestamped name.

        Returns:
            Path where screenshot was saved
        """
        page = self._require_page()

        if path is None:
            timestamp = datetime.now(tz=UTC).strftime("%Y%m%d_%H%M%S")
            path = f"screenshot_{timestamp}.png"

        # Ensure path is absolute
        path_obj = Path(path)
        if not path_obj.is_absolute():
            path_obj = Path.cwd() / path_obj

        await page.screenshot(path=str(path_obj))

        return str(path_obj)

    async def screenshot_png(self) -> bytes:
        """Screenshot the current page and return the PNG bytes.

        No disk write — the cloud surface hands the image straight back to the
        agent, and writing into a shared server's cwd is not a thing to do.
        """
        return await self._require_page().screenshot()

    async def content_html(self) -> str:
        """The current page's rendered HTML (post-JS), for READING.

        A snapshot is for clicking — it carries ``[ref=N]`` markers and the
        structural noise that goes with them. The EE ``extract`` tool converts
        this HTML to markdown instead, which is far cheaper per page of prose.
        """
        return await self._require_page().content()


__all__ = ["BrowserDriver", "NavigationResult"]
