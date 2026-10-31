# tests/ee/agent/test_sites_mcp_server/test_play_test_site.py — the agent PLAYS its
# own template-built draft.
#
# ``play_test_site`` drives Cloudflare's remote browser over CDP. Playwright is faked
# at ``async_playwright`` so nothing reaches the network: the fake records the CDP
# url and headers, every page call, and emits console / pageerror / failed-request
# events on navigation. These tests pin step validation, that the page is always
# the pocket's preview_url (plus a validated path), the bearer header, the capture
# lists (deduped, capped), the blank-canvas flag, fps, that the session is closed
# even when a step raises, the honest "unavailable" errors, the rate limit,
# the allow-list registration, and ``exercise_forms`` (failures recorded for verify and
# the publish gate; never run on the live site or a draft without server routes).
from __future__ import annotations

import io
from contextlib import ExitStack, asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")

from PIL import Image  # noqa: E402

_PREVIEW = "https://abc123.preview.example.com/"


def _png(width: int = 1280, height: int = 800, *, canvas: str = "varied") -> bytes:
    """A viewport screenshot. The canvas box (100,100)-(600,500) is one flat colour
    when ``canvas`` is ``flat``, noise otherwise."""
    img = Image.new("RGB", (width, height), (240, 240, 240))
    if canvas == "varied":
        for x in range(100, 600, 10):
            for y in range(100, 500, 10):
                shade = (x * 7 + y * 13) % 256
                img.paste((shade, 255 - shade, (shade * 3) % 256), (x, y, x + 10, y + 10))
    else:
        img.paste((0, 0, 0), (100, 100, 600, 500))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


_CANVAS_RECT = {"x": 100, "y": 100, "w": 500, "h": 400}


class _Msg:
    def __init__(self, kind: str, text: str) -> None:
        self.type = kind
        self.text = text


class _Err:
    def __init__(self, message: str) -> None:
        self.message = message


class _Req:
    def __init__(self, url: str, failure: str) -> None:
        self.url = url
        self.failure = failure


class _Resp:
    def __init__(self, url: str, status: int) -> None:
        self.url = url
        self.status = status


class _Recorder:
    def __init__(self, page: _FakePage, name: str) -> None:
        self._page = page
        self._name = name

    def __getattr__(self, method: str):
        async def call(*args: Any, **kwargs: Any) -> None:
            self._page.calls.append((f"{self._name}.{method}", args, kwargs))

        return call


class _Locator:
    def __init__(self, page: _FakePage, selector: str) -> None:
        self._page = page
        self._selector = selector
        self.first = self

    async def click(self, **kwargs: Any) -> None:
        self._page.calls.append(("locator.click", (self._selector,), kwargs))

    async def press_sequentially(self, text: str, **kwargs: Any) -> None:
        self._page.calls.append(("locator.press_sequentially", (self._selector, text), kwargs))


class _FakePage:
    def __init__(self, world: _World) -> None:
        self.world = world
        self.calls: list[tuple[str, tuple, dict]] = []
        self.handlers: dict[str, list] = {}
        self.keyboard = _Recorder(self, "keyboard")
        self.mouse = _Recorder(self, "mouse")
        self.touchscreen = _Recorder(self, "touchscreen")
        self.closed = False

    def on(self, event: str, handler: Any) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def _emit(self, event: str, arg: Any) -> None:
        for handler in self.handlers.get(event, []):
            handler(arg)

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.calls.append(("goto", (url,), kwargs))
        if self.world.nav_error:
            raise RuntimeError(self.world.nav_error)
        for kind, arg in self.world.events:
            self._emit(kind, arg)

    async def wait_for_timeout(self, ms: int) -> None:
        self.calls.append(("wait_for_timeout", (ms,), {}))

    async def click(self, selector: str, **kwargs: Any) -> None:
        self.calls.append(("click", (selector,), kwargs))
        if selector in self.world.missing:
            raise RuntimeError(f"Timeout 3000ms exceeded waiting for locator({selector!r})")
        if selector in self.world.closes:
            self.closed = True

    async def tap(self, selector: str, **kwargs: Any) -> None:
        self.calls.append(("tap", (selector,), kwargs))

    def locator(self, selector: str) -> _Locator:
        return _Locator(self, selector)

    async def screenshot(self, **kwargs: Any) -> bytes:
        self.calls.append(("screenshot", (), kwargs))
        return self.world.image

    async def evaluate(self, js: str, arg: Any = None) -> Any:
        if self.world.evaluate_hook is not None:
            handled, value = self.world.evaluate_hook(js, arg)
            if handled:
                return value
        if "__pawFrames" in js:
            return {"frames": 120, "ms": 2000.0}
        if "canvas" in js:
            return self.world.canvas_rect
        return None

    def is_closed(self) -> bool:
        return self.closed

    async def route(self, pattern: str, handler: Any) -> None:
        self.calls.append(("route", (pattern,), {}))

    async def wait_for_load_state(self, state: str, **kwargs: Any) -> None:
        self.calls.append(("wait_for_load_state", (state,), kwargs))


class _FakeContext:
    def __init__(self, world: _World, kwargs: dict) -> None:
        self.world = world
        self.kwargs = kwargs
        self.init_scripts: list[str] = []
        self.closed = False

    async def add_init_script(self, script: str) -> None:
        self.init_scripts.append(script)

    async def new_page(self) -> _FakePage:
        page = _FakePage(self.world)
        self.world.page = page
        return page

    async def close(self) -> None:
        self.closed = True


class _FakeCDP:
    def __init__(self, world: _World) -> None:
        self.world = world

    async def send(self, method: str) -> None:
        self.world.cdp_sent.append(method)


class _FakeBrowser:
    def __init__(self, world: _World) -> None:
        self.world = world

    async def new_context(self, **kwargs: Any) -> _FakeContext:
        ctx = _FakeContext(self.world, kwargs)
        self.world.context = ctx
        return ctx

    async def new_browser_cdp_session(self) -> _FakeCDP:
        return _FakeCDP(self.world)

    async def close(self) -> None:
        self.world.browser_closed = True


class _World:
    """Everything the fake Playwright saw and will do."""

    def __init__(self) -> None:
        self.connects: list[tuple[str, dict]] = []
        self.connect_error: Exception | None = None
        self.events: list[tuple[str, Any]] = []
        self.missing: set[str] = set()
        self.closes: set[str] = set()
        self.nav_error = ""
        self.image = _png()
        self.canvas_rect: dict | None = _CANVAS_RECT
        self.page: _FakePage | None = None
        self.context: _FakeContext | None = None
        self.cdp_sent: list[str] = []
        self.browser_closed = False
        #: ``(js, arg) -> (handled, value)`` for the form-exercise scripts.
        self.evaluate_hook: Any = None

    def async_playwright(self):
        world = self

        class _Chromium:
            async def connect_over_cdp(self, url: str, **kwargs: Any) -> _FakeBrowser:
                world.connects.append((url, kwargs))
                if world.connect_error is not None:
                    raise world.connect_error
                return _FakeBrowser(world)

        class _PW:
            chromium = _Chromium()

        @asynccontextmanager
        async def _cm():
            yield _PW()

        return _cm()


@pytest.fixture
def world():
    w = _World()
    with patch("playwright.async_api.async_playwright", w.async_playwright):
        yield w


@pytest.fixture(autouse=True)
def _fresh_limits():
    from pocketpaw_ee.agent.mcp_servers import sites_play_test

    sites_play_test._runs.clear()
    sites_play_test._slots = None
    yield
    sites_play_test._runs.clear()


def _real_cf():
    from pocketpaw_ee.sites.cloudflare_client import CloudflareClient

    return CloudflareClient(
        account_id="acct1", api_token="tok-secret", zone_id="z", dispatch_namespace="n"
    )


@pytest.fixture
def ctx():
    """Identity, an open plan gate, a project pocket with a built draft, and a real
    CloudflareClient (no network: only ``browser_run_cdp`` is used)."""
    with ExitStack() as stack:
        stack.enter_context(
            patch("pocketpaw_ee.cloud.chat.agent_service.current_workspace_id", return_value="w")
        )
        stack.enter_context(
            patch("pocketpaw_ee.cloud.chat.agent_service.current_user_id", return_value="u")
        )
        stack.enter_context(
            patch(
                "pocketpaw_ee.cloud.workspace.service.get_workspace_plan",
                new=AsyncMock(return_value="go"),
            )
        )
        stack.enter_context(
            patch(
                "pocketpaw_ee.cloud.pockets.service.get",
                new=AsyncMock(return_value={"id": "p1", "engine": "project", "source": {}}),
            )
        )
        stack.enter_context(
            patch(
                "pocketpaw_ee.sites.service.get_native_artifact",
                new=AsyncMock(
                    return_value={
                        "body_html": "",
                        "build_status": "built",
                        "preview_url": _PREVIEW,
                        "preview_mode": "full",
                    }
                ),
            )
        )
        stack.enter_context(patch("pocketpaw_ee.sites.service._cf_client", side_effect=_real_cf))
        yield


async def _play(args: dict) -> dict:
    from pocketpaw_ee.agent.mcp_servers import sites_play_test

    return await sites_play_test._play_test_site_handler({"pocket_id": "p1", **args})


def _text(out: dict) -> str:
    return "\n".join(p["text"] for p in out["content"] if p["type"] == "text")


# ── step validation ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "step",
    [
        {"click": "#start"},
        {"click": {"x": 10, "y": 20}},
        {"press": "ArrowUp"},
        {"press": "w", "hold_ms": 3000},
        {"press": "Shift+W"},
        {"press": " "},
        {"type": {"selector": "#name", "text": "Ada"}},
        {"mouse_move": {"dx": -200, "dy": 15}},
        {"scroll": {"dy": 600}},
        {"wait": 5000},
        {"screenshot": "after jump"},
    ],
)
def test_each_action_validates(step) -> None:
    from pocketpaw_ee.sites.play_test import validate_steps

    steps, bad = validate_steps([step])
    assert bad == "" and steps == [step]


@pytest.mark.parametrize(
    ("step", "needle"),
    [
        ({"jump": True}, "unknown action"),
        ({"click": "#a", "press": "w"}, "exactly one action"),
        ({"press": "w", "wait": 10}, "exactly one action"),
        ({"click": ""}, "`click` selector"),
        ({"click": {"x": -1, "y": 2}}, "`click` takes"),
        ({"click": {"x": 1.5, "y": 2}}, "`click` takes"),
        ({"press": "Arrow Up!"}, "Playwright key name"),
        ({"press": "w", "hold_ms": 3001}, "`hold_ms`"),
        ({"type": {"selector": "#a", "text": "x" * 201}}, "at most 200"),
        ({"type": {"selector": "#a"}}, "`type` takes"),
        ({"mouse_move": {"dx": 2001, "dy": 0}}, "±2000"),
        ({"mouse_move": {"x": 1, "y": 0}}, "`mouse_move` takes"),
        ({"scroll": {"dy": 9000}}, "±5000"),
        ({"wait": 5001}, "`wait`"),
        ({"wait": True}, "`wait`"),
        ({"screenshot": ""}, "`screenshot`"),
        ({"screenshot": "a/b"}, "`screenshot`"),
        ("click #a", "exactly one action"),
    ],
)
def test_a_bad_step_is_named(step, needle) -> None:
    from pocketpaw_ee.sites.play_test import validate_steps

    steps, bad = validate_steps([{"wait": 1}, step])
    assert steps == []
    assert bad.startswith("step 2") or "step 2" in bad, bad
    assert needle in bad, bad


def test_step_and_screenshot_counts_and_pause_are_capped() -> None:
    from pocketpaw_ee.sites.play_test import validate_steps

    assert "most one run takes is 25" in validate_steps([{"wait": 0}] * 26)[1]
    assert "5 screenshots" in validate_steps([{"screenshot": "s"}] * 5)[1]
    assert "waits and holds" in validate_steps([{"wait": 5000}] * 9)[1]
    assert validate_steps("nope")[1].startswith("`steps` must be a list")
    assert validate_steps(None) == ([], "")


async def test_a_bad_step_errors_before_connecting(ctx, world) -> None:
    out = await _play({"steps": [{"press": "w"}, {"fly": 1}]})
    assert out.get("is_error") is True
    assert "step 2" in _text(out) and "Nothing was played" in _text(out)
    assert world.connects == []


async def test_steps_sent_as_a_json_string_are_decoded(ctx, world) -> None:
    out = await _play({"steps": '[{"press": "ArrowUp"}]'})
    assert out.get("is_error") is not True, out
    assert any(c[0] == "keyboard.press" for c in world.page.calls)


# ── target, auth, session ───────────────────────────────────────────────────


async def test_it_opens_the_pockets_preview_url_with_the_bearer_token(ctx, world) -> None:
    out = await _play({"path": "/play", "steps": [{"press": "Space"}]})
    assert out.get("is_error") is not True, out
    url, kwargs = world.connects[0]
    assert url.startswith(
        "wss://api.cloudflare.com/client/v4/accounts/acct1/browser-run/devtools/browser"
    )
    assert "keep_alive=75000" in url
    assert kwargs["headers"] == {"Authorization": "Bearer tok-secret"}
    goto = next(c for c in world.page.calls if c[0] == "goto")
    assert goto[1] == ("https://abc123.preview.example.com/play",)
    assert goto[2]["wait_until"] == "load"
    assert "tok-secret" not in _text(out)


async def test_the_agent_cannot_pass_a_url(ctx, world) -> None:
    out = await _play({"url": "https://evil.example.com"})
    goto = next(c for c in world.page.calls if c[0] == "goto")
    assert goto[1] == (_PREVIEW,)
    assert out.get("is_error") is not True


@pytest.mark.parametrize("path", ["//evil.example.com", "https://evil.example.com", "/../x"])
async def test_a_path_off_the_draft_origin_is_refused(ctx, world, path) -> None:
    out = await _play({"path": path})
    assert out.get("is_error") is True
    assert world.connects == []


async def test_the_session_is_closed_even_when_a_step_fails(ctx, world) -> None:
    world.missing = {"#nope"}
    out = await _play({"steps": [{"click": "#nope"}, {"press": "Enter"}]})
    assert out.get("is_error") is not True, out
    text = _text(out)
    assert "1. click='#nope': failed:" in text
    assert "2. press='Enter': ok" in text
    assert world.cdp_sent == ["Browser.close"] and world.browser_closed
    assert world.context.closed


async def test_the_session_is_closed_when_the_run_blows_up(ctx, world) -> None:
    async def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("context exploded")

    with patch.object(_FakeBrowser, "new_context", boom):
        out = await _play({"steps": [{"press": "w"}]})
    assert out.get("is_error") is True
    assert "does not mean the site is broken" in _text(out)
    assert "context exploded" not in _text(out)
    assert world.cdp_sent == ["Browser.close"] and world.browser_closed


async def test_a_page_that_closes_itself_stops_the_run(ctx, world) -> None:
    world.closes = {"#quit"}
    out = await _play({"steps": [{"click": "#quit"}, {"press": "w"}]})
    text = _text(out)
    assert "closed itself" in text
    assert "2. press" not in text


# ── actions ─────────────────────────────────────────────────────────────────


async def test_each_action_drives_the_page(ctx, world) -> None:
    steps = [
        {"click": "#start"},
        {"click": {"x": 10, "y": 20}},
        {"press": "w", "hold_ms": 400},
        {"press": "Shift+W", "hold_ms": 100},
        {"type": {"selector": "#name", "text": "Ada"}},
        {"mouse_move": {"dx": 50, "dy": -5}},
        {"scroll": {"dy": 300}},
        {"wait": 250},
    ]
    out = await _play({"steps": steps})
    assert out.get("is_error") is not True, out
    names = [(c[0], c[1]) for c in world.page.calls]
    assert ("click", ("#start",)) in names
    assert ("mouse.click", (10, 20)) in names
    assert names.index(("keyboard.down", ("w",))) < names.index(("wait_for_timeout", (400,)))
    assert names.index(("wait_for_timeout", (400,))) < names.index(("keyboard.up", ("w",)))
    assert ("keyboard.down", ("Shift",)) in names and ("keyboard.up", ("W",)) in names
    assert ("locator.press_sequentially", ("#name", "Ada")) in names
    # Relative to where the last click left the pointer.
    assert ("mouse.move", (60.0, 15.0)) in names
    assert ("mouse.wheel", (0, 300)) in names
    assert ("wait_for_timeout", (250,)) in names


async def test_mobile_uses_touch(ctx, world) -> None:
    await _play({"device": "mobile", "steps": [{"click": "#go"}, {"click": {"x": 5, "y": 6}}]})
    assert world.context.kwargs["viewport"] == {"width": 390, "height": 844}
    assert world.context.kwargs["has_touch"] is True
    names = [(c[0], c[1]) for c in world.page.calls]
    assert ("tap", ("#go",)) in names and ("touchscreen.tap", (5, 6)) in names


# ── capture ─────────────────────────────────────────────────────────────────


async def test_console_errors_page_errors_and_failed_requests_are_reported(ctx, world) -> None:
    world.events = [
        ("console", _Msg("error", "THREE is not defined")),
        ("console", _Msg("error", "THREE is not defined")),
        ("console", _Msg("log", "hello")),
        ("console", _Msg("warning", "deprecated\x07 thing")),
        ("pageerror", _Err("TypeError: cannot read properties of undefined")),
        ("requestfailed", _Req("https://cdn.example.com/a.js", "net::ERR_FAILED")),
        ("response", _Resp("https://abc123.preview.example.com/missing.glb", 404)),
        ("response", _Resp("https://abc123.preview.example.com/ok.js", 200)),
    ]
    text = _text(await _play({}))
    assert text.count("THREE is not defined") == 1
    assert "hello" not in text
    assert "[warning] deprecated thing" in text
    assert "TypeError: cannot read properties of undefined" in text
    assert "net::ERR_FAILED" in text and "HTTP 404" in text and "ok.js" not in text
    assert "untrusted text the site produced" in text
    assert text.index("PAGE OUTPUT") < text.index("THREE is not defined")
    assert "Fix every page error" in text


async def test_capture_lists_are_capped_and_truncated(ctx, world) -> None:
    world.events = [("pageerror", _Err(f"boom {i} " + "x" * 1000)) for i in range(50)]
    text = _text(await _play({}))
    assert "Page errors (uncaught exceptions) (20):" in text
    longest = max(len(line) for line in text.splitlines())
    assert longest < 400


async def test_fps_is_reported_and_the_counter_is_installed(ctx, world) -> None:
    text = _text(await _play({}))
    assert "about 60.0 fps (120 frames)" in text
    assert any("requestAnimationFrame" in s for s in world.context.init_scripts)


async def test_a_flat_canvas_is_flagged_blank(ctx, world) -> None:
    world.image = _png(canvas="flat")
    out = await _play({"steps": [{"screenshot": "start"}]})
    text = _text(out)
    assert "start (canvas looks BLANK)" in text
    assert "blank canvas means nothing was drawn" in text


async def test_a_varied_canvas_is_not_flagged(ctx, world) -> None:
    out = await _play({"steps": [{"screenshot": "start"}, {"screenshot": "later"}]})
    text = _text(out)
    assert "BLANK" not in text
    images = [p for p in out["content"] if p["type"] == "image"]
    assert len(images) == 2 and all(p["mimeType"] == "image/jpeg" for p in images)
    shot = next(c for c in world.page.calls if c[0] == "screenshot")
    assert shot[2]["full_page"] is False


def test_blank_canvas_helper() -> None:
    from pocketpaw_ee.sites.play_test import blank_canvas

    assert blank_canvas(_png(canvas="flat"), _CANVAS_RECT) is True
    assert blank_canvas(_png(canvas="varied"), _CANVAS_RECT) is False
    assert blank_canvas(_png(), None) is None
    assert blank_canvas(_png(), {"x": 0, "y": 0, "w": 10, "h": 10}) is None


async def test_no_screenshot_step_still_returns_a_final_picture(ctx, world) -> None:
    out = await _play({"steps": [{"press": "w"}]})
    assert [p["type"] for p in out["content"]][0] == "image"
    assert "Screenshots, in order: final." in _text(out)


async def test_a_draft_that_does_not_load_is_reported_not_called_broken(ctx, world) -> None:
    world.nav_error = "Timeout 20000ms exceeded"
    out = await _play({"steps": [{"press": "w"}]})
    text = _text(out)
    assert "did not finish loading" in text and "Timeout 20000ms" in text
    assert not any(c[0] == "keyboard.press" for c in world.page.calls)
    assert world.browser_closed


# ── honest failures, limits ─────────────────────────────────────────────────


async def test_cloudflare_unconfigured_is_an_honest_error(ctx, world) -> None:
    from pocketpaw_ee.cloud._core.errors import ValidationError

    def _unconfigured():
        raise ValidationError("sites.cloudflare_unconfigured", "Cloudflare is not configured")

    with patch("pocketpaw_ee.sites.service._cf_client", side_effect=_unconfigured):
        out = await _play({})
    text = _text(out)
    assert out.get("is_error") is True
    assert "play-testing is unavailable on this deployment" in text
    assert "gameplay was not play-tested" in text and "does not mean the site is broken" in text
    assert world.connects == []


async def test_a_failed_connect_is_an_honest_error(ctx, world) -> None:
    world.connect_error = RuntimeError("WebSocket error: 401 Unauthorized")
    out = await _play({})
    text = _text(out)
    assert out.get("is_error") is True
    assert "play-testing is unavailable on this deployment" in text
    assert "401" not in text


async def test_no_preview_url_says_build_first(ctx, world) -> None:
    with patch(
        "pocketpaw_ee.sites.service.get_native_artifact",
        new=AsyncMock(return_value={"build_status": "building", "preview_mode": None}),
    ):
        out = await _play({})
    text = _text(out)
    assert out.get("is_error") is True
    assert "run_site_build" in text and "play_test_site again" in text
    assert world.connects == []


async def test_a_server_only_draft_says_so(ctx, world) -> None:
    with patch(
        "pocketpaw_ee.sites.service.get_native_artifact",
        new=AsyncMock(return_value={"preview_url": None, "preview_mode": "server_only"}),
    ):
        out = await _play({})
    assert out.get("is_error") is True and "server-only" in _text(out)


async def test_a_non_project_site_cannot_be_played(ctx, world) -> None:
    with patch(
        "pocketpaw_ee.cloud.pockets.service.get",
        new=AsyncMock(return_value={"id": "p1", "engine": "html", "source": {}}),
    ):
        out = await _play({})
    assert out.get("is_error") is True and "not play-tested" in _text(out)
    assert world.connects == []


async def test_the_rate_limit_refuses(ctx, world) -> None:
    from pocketpaw_ee.agent.mcp_servers import sites_play_test

    for _ in range(sites_play_test._RATE_LIMIT):
        sites_play_test._count_run("w")
    out = await _play({})
    assert out.get("is_error") is True
    assert "30 play-tests for the hour" in _text(out)
    assert world.connects == []


def test_the_rate_window_slides() -> None:
    from pocketpaw_ee.agent.mcp_servers import sites_play_test

    sites_play_test._runs["w2"] = __import__("collections").deque([0.0] * 30)
    assert sites_play_test._rate_limited("w2", now=100.0) is True
    assert sites_play_test._rate_limited("w2", now=3_700.0) is False


async def test_the_plan_gate_runs_first(ctx, world) -> None:
    with patch(
        "pocketpaw_ee.cloud.workspace.service.get_workspace_plan",
        new=AsyncMock(return_value="free"),
    ):
        out = await _play({})
    assert out.get("is_error") is True and "Nothing was" not in _text(out)
    assert world.connects == []


async def test_it_needs_identity_and_a_pocket_id(world) -> None:
    from pocketpaw_ee.agent.mcp_servers import sites_play_test

    out = await sites_play_test._play_test_site_handler({"pocket_id": "p1"})
    assert out.get("is_error") is True
    with (
        patch("pocketpaw_ee.cloud.chat.agent_service.current_workspace_id", return_value="w"),
        patch("pocketpaw_ee.cloud.chat.agent_service.current_user_id", return_value="u"),
    ):
        out = await sites_play_test._play_test_site_handler({})
    assert out.get("is_error") is True and "pocket_id" in _text(out)


async def test_the_run_budget_is_enforced(world) -> None:
    import asyncio

    from pocketpaw_ee.sites import play_test

    async def slow(self, ms: int) -> None:
        await asyncio.sleep(10)

    with patch.object(_FakePage, "wait_for_timeout", slow):
        result = await play_test.run_play_test(
            url=_PREVIEW,
            viewport={"width": 1280, "height": 800},
            mobile=False,
            steps=[{"wait": 10}],
            ws_url="wss://x",
            headers={},
            budget_s=0.3,
        )
    assert result.timed_out is True
    assert world.cdp_sent == ["Browser.close"] and world.browser_closed


def test_it_is_registered_on_the_allow_list() -> None:
    from pocketpaw_ee.agent.mcp_servers.sites import PLAY_TEST_SITE_TOOL_ID, SITES_TOOL_IDS
    from pocketpaw_ee.agent.mcp_servers.sites_create import SITES_CREATE_TOOL_IDS
    from pocketpaw_ee.cloud.surface import SurfaceKind, SurfaceMeta, resolve_profile

    assert PLAY_TEST_SITE_TOOL_ID == "mcp__pocketpaw_sites_manager__play_test_site"
    assert PLAY_TEST_SITE_TOOL_ID in SITES_TOOL_IDS
    assert PLAY_TEST_SITE_TOOL_ID in SITES_CREATE_TOOL_IDS
    for meta in (SurfaceMeta(), SurfaceMeta(pocket_id="p1")):
        profile = resolve_profile(SurfaceKind.SITES, meta)
        assert PLAY_TEST_SITE_TOOL_ID in (profile.allow_mcp_tool_ids or ())
        assert PLAY_TEST_SITE_TOOL_ID not in profile.deny_mcp_tool_ids


def test_the_tool_schema_offers_steps() -> None:
    from pocketpaw_ee.agent.mcp_servers.sites_play_test import make_play_test_site_tool

    captured: dict[str, Any] = {}

    def fake_tool(name, description, schema):
        captured.update(name=name, description=description, schema=schema)
        return lambda fn: fn

    make_play_test_site_tool(fake_tool)
    assert captured["name"] == "play_test_site"
    props = captured["schema"]["properties"]
    assert set(props) == {"pocket_id", "path", "device", "steps", "exercise_forms"}
    assert props["steps"]["maxItems"] == 25
    assert "never claim gameplay works" in captured["description"]


def test_the_app_plan_skill_plays_every_milestone() -> None:
    """The app-plan skill is where the milestone loop lives, so it is where the agent
    learns to play the game before it calls a milestone done."""
    from pathlib import Path

    import pocketpaw

    skill = (
        Path(pocketpaw.__file__).parent
        / "bundled_skills"
        / "_bundled"
        / "skills"
        / "sites-app-plan"
        / "SKILL.md"
    )
    text = " ".join(skill.read_text("utf-8").split())
    assert "play_test_site" in text
    assert "lose or win" in text and "play again" in text
    assert "Never claim gameplay works without a passing play-test" in text


_FORM_INFO = {"name": "join", "fields": ["email"], "invalid": [], "external": "", "count": 1}


class TestExerciseForms:
    async def test_failed_forms_are_recorded_for_verify_and_publish(
        self, ctx, world, monkeypatch
    ) -> None:
        from pocketpaw_ee.sites import play_test, verify

        recorded: list[tuple] = []
        monkeypatch.setattr(
            verify,
            "record_form_results",
            lambda pid, h, path, entries, **_k: recorded.append((pid, path, entries)),
        )

        def hook(js: str, arg: Any) -> tuple[bool, Any]:
            if js is play_test._FILL_FORM_JS:
                return True, (_FORM_INFO if arg[0] == 0 else None)
            if js is play_test._SUBMIT_FORM_JS:
                return True, True
            if js is play_test._FORM_STATE_JS:
                return True, {"text": "", "state": None}
            return False, None

        world.evaluate_hook = hook
        out = await _play({"exercise_forms": True, "path": "/join/index.html"})
        text = _text(out)
        assert "join: FAILED: nothing was sent and the page did not change" in text
        assert "blocks publishing" in text
        # Honest about what the test can promise.
        assert "server code may still call outside services" in text
        assert "nothing reached the owner's real data" not in text
        ((pid, path, entries),) = recorded
        # "/join/index.html" and "/join" are one page: one record key.
        assert (pid, path) == ("p1", "/join")
        assert entries[0]["id"] == "SC-FORM-01"
        assert entries[0]["page"] == "join/index.html"
        assert entries[0]["level"] == "error"

    @pytest.mark.parametrize("mode", ["published", "static", "server_only", None, "new-mode"])
    async def test_a_live_or_serverless_draft_is_never_exercised(
        self, ctx, world, monkeypatch, mode
    ) -> None:
        artifact = {"build_status": "built", "preview_url": _PREVIEW, "preview_mode": mode}
        with patch(
            "pocketpaw_ee.sites.service.get_native_artifact",
            new=AsyncMock(return_value=artifact),
        ):
            out = await _play({"exercise_forms": True})
        assert "Forms were NOT exercised" in _text(out)
        assert not any(c[0] == "route" for c in world.page.calls)
