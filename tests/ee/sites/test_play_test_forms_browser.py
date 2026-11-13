# tests/ee/sites/test_play_test_forms_browser.py — the form exercise against a REAL
# Chromium, on a local two-origin harness: origin A is the "draft", origin B is "the
# rest of the internet" and records every request it gets.
#
# The fakes in test_play_test_forms.py cannot show what Chromium actually does, and the
# re-review proved three leaks there: Playwright does not route redirect hops (a 307
# re-POSTs the form to B), WebSockets bypass context.route, and an off-site image or
# JSONP script GET carries the test entry in its query string. With a form armed, B must
# receive NOTHING. Skipped when no local Chromium can be launched (CI without browsers).
from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

pytest.importorskip("playwright")
aiohttp_web = pytest.importorskip("aiohttp.web")

from pocketpaw_ee.sites import play_test  # noqa: E402

_EMAIL_FORM = (
    '<form id="join" {attrs}><input type="email" name="email" required>'
    '<button>Join</button></form><p id="out"></p>'
)


def _page(attrs: str, script: str = "") -> str:
    body = _EMAIL_FORM.format(attrs=attrs)
    return f"<!doctype html><html><body>{body}<script>{script}</script></body></html>"


class _Harness:
    def __init__(self) -> None:
        self.hits: list[str] = []
        self.runners: list[Any] = []
        self.a = self.b = ""

    async def start(self) -> None:
        web = aiohttp_web

        async def b_any(request: Any) -> Any:
            self.hits.append(f"{request.method} {request.path_qs}")
            if request.path == "/ws":
                ws = web.WebSocketResponse()
                await ws.prepare(request)
                await ws.close()
                return ws
            return web.Response(text="ok", content_type="text/plain")

        b_app = web.Application()
        b_app.router.add_route("*", "/{tail:.*}", b_any)
        b_port = await self._serve(b_app)
        self.b = f"http://127.0.0.1:{b_port}"
        b = self.b
        ws = b.replace("http", "ws")

        pages = {
            "/redirect": _page('method="post" action="/submit-307"'),
            "/pixel": _page(
                "",
                "document.getElementById('join').addEventListener('submit', e => {"
                " e.preventDefault(); const v = e.target.email.value;"
                f" new Image().src = '{b}/pixel?e=' + encodeURIComponent(v);"
                " const s = document.createElement('script');"
                f" s.src = '{b}/jsonp?e=' + encodeURIComponent(v); document.body.appendChild(s);"
                " document.getElementById('out').textContent = 'Thanks!'; });",
            ),
            "/socket": _page(
                "",
                "document.getElementById('join').addEventListener('submit', e => {"
                " e.preventDefault(); const v = e.target.email.value;"
                f" try {{ new WebSocket('{ws}/ws?e=' + encodeURIComponent(v)); }}"
                " catch (err) {}"
                " document.getElementById('out').textContent = 'Thanks!'; });",
            ),
        }

        async def a_any(request: Any) -> Any:
            if request.path == "/submit-307":
                raise web.HTTPTemporaryRedirect(f"{b}/collect")
            html = pages.get(request.path)
            if html is None:
                return web.Response(status=404)
            return web.Response(text=html, content_type="text/html")

        a_app = web.Application()
        a_app.router.add_route("*", "/{tail:.*}", a_any)
        self.a = f"http://127.0.0.1:{await self._serve(a_app)}"

    async def _serve(self, app: Any) -> int:
        runner = aiohttp_web.AppRunner(app)
        await runner.setup()
        site = aiohttp_web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.runners.append(runner)
        return site._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for runner in self.runners:
            await runner.cleanup()


@pytest.fixture
async def harness():
    h = _Harness()
    await h.start()
    yield h
    await h.stop()


async def _exercise(path: str, harness: _Harness) -> play_test.PlayResult:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no local Chromium: {exc}")
        try:
            context = await browser.new_context(**play_test.CONTEXT_OPTIONS)
            page = await context.new_page()
            result = play_test.PlayResult(url=harness.a + path)
            await play_test.exercise_page_forms(
                page, harness.a + path, result, time.monotonic() + 60
            )
            await asyncio.sleep(0.5)
            return result
        finally:
            await browser.close()


async def test_a_307_to_another_host_does_not_repost_the_form(harness) -> None:
    result = await _exercise("/redirect", harness)
    assert harness.hits == []
    (form,) = result.forms
    assert form.status == "unchecked" and "redirects off-site" in form.outcome


async def test_off_site_images_and_scripts_never_carry_the_entry(harness) -> None:
    await _exercise("/pixel", harness)
    assert harness.hits == []


async def test_a_websocket_to_another_host_is_closed(harness) -> None:
    await _exercise("/socket", harness)
    assert harness.hits == []


def test_the_test_context_blocks_service_workers() -> None:
    assert play_test.CONTEXT_OPTIONS["service_workers"] == "block"
