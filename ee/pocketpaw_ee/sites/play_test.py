# play_test.py — drive a site draft in Cloudflare's remote browser and report what
# happened: the engine behind the ``play_test_site`` agent tool.
#
# The agent hands over a short list of steps (click, press, type, mouse_move,
# scroll, wait, screenshot). ``validate_steps`` checks them strictly before any
# browser exists. ``run_play_test`` connects to Browser Run over CDP
# (``CloudflareClient.browser_run_cdp``), opens the draft, plays the steps and
# returns a ``PlayResult``: a step log, console errors and warnings, uncaught page
# errors, failed requests, an approximate frame rate and viewport screenshots with
# a blank-canvas flag each.
#
# Invariants a reader must not break:
#   * Chromium never runs in our process. The draft's JS is untrusted and our
#     container passes ``--no-sandbox``; the browser is Cloudflare's, reached over
#     ``connect_over_cdp``. ``sites/screenshot.py`` documents the same rule.
#   * The URL is the caller's (the pocket's own preview URL); nothing here takes
#     an address from a step.
#   * The remote session is closed in a ``finally`` (CDP ``Browser.close``, then
#     ``browser.close()``), and ``keep_alive`` is the run budget plus a margin, so a
#     lost close still frees the session soon.
#   * Everything the page says (console, errors, URLs) is untrusted site output:
#     deduped, capped per list and truncated here before it reaches the model.
#   * A failed step is logged and the run goes on; only a closed page stops it.
#   * Screenshots stay in memory and are never logged.
from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

MAX_STEPS = 25
MAX_SCREENSHOTS = 4
MAX_HOLD_MS = 3_000
MAX_WAIT_MS = 5_000
#: Waits and holds together; the rest of the minute is connect, load and actions.
MAX_TOTAL_PAUSE_MS = 40_000
MAX_TYPE_CHARS = 200
MAX_SELECTOR_CHARS = 300
MAX_MOVE_PX = 2_000
MAX_SCROLL_PX = 5_000
MAX_LABEL_CHARS = 40
#: Wall-clock budget for one run, connect included.
RUN_BUDGET_S = 60.0
#: ``keep_alive`` for the remote session: the budget plus a margin (Cloudflare's
#: floor is 10000 ms).
KEEP_ALIVE_MS = 75_000
CONNECT_TIMEOUT_MS = 15_000
NAV_TIMEOUT_MS = 20_000
SETTLE_MS = 1_000
ACTION_TIMEOUT_MS = 3_000
#: Each captured list keeps this many distinct entries, each cut to the char cap.
MAX_EVENTS = 20
MAX_EVENT_CHARS = 300
#: A crop whose pixel variance is under this (per band, 0-255 scale) is one flat
#: colour: nothing was drawn into the canvas.
BLANK_VARIANCE = 4.0
#: Canvases smaller than this (CSS px, both sides) are not judged.
MIN_CANVAS_PX = 48

ACTIONS = ("click", "press", "type", "mouse_move", "scroll", "wait", "screenshot")
_KEY_RE = re.compile(r"[A-Za-z0-9]+(\+[A-Za-z0-9]+)*")
_LABEL_RE = re.compile(r"[\w .:-]{1,40}")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

#: Counts animation frames: one per distinct rAF timestamp, so several loops in the
#: same frame count once. Installed before any page script runs.
RAF_COUNTER_JS = """
(() => {
  if (window.__pawFrames) return;
  const s = { frames: 0, last: -1, t0: performance.now() };
  window.__pawFrames = s;
  const raf = window.requestAnimationFrame.bind(window);
  window.requestAnimationFrame = function (cb) {
    return raf(function (ts) {
      if (ts !== s.last) { s.last = ts; s.frames++; }
      return cb(ts);
    });
  };
})();
"""
_RAF_READ_JS = (
    "() => { const s = window.__pawFrames;"
    " return s ? { frames: s.frames, ms: performance.now() - s.t0 } : null; }"
)
#: The largest canvas's visible box inside the viewport, in CSS px, or null.
_CANVAS_RECT_JS = """
() => {
  let best = null, area = 0;
  for (const c of document.querySelectorAll('canvas')) {
    const r = c.getBoundingClientRect();
    const x = Math.max(0, r.left), y = Math.max(0, r.top);
    const x2 = Math.min(innerWidth, r.right), y2 = Math.min(innerHeight, r.bottom);
    const a = (x2 - x) * (y2 - y);
    if (x2 > x && y2 > y && a > area) { area = a; best = { x, y, w: x2 - x, h: y2 - y }; }
  }
  return best;
}
"""


class PlayTestUnavailable(RuntimeError):
    """The remote browser could not be reached (not configured, not installed, or
    the connect failed). Never a statement about the site."""


@dataclass
class Shot:
    label: str
    image: bytes  # PNG, viewport-sized
    blank_canvas: bool | None  # None: no canvas big enough to judge


@dataclass
class PlayResult:
    url: str
    steps: list[str] = field(default_factory=list)
    console: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    shots: list[Shot] = field(default_factory=list)
    navigation_error: str = ""
    fps: float | None = None
    frames: int | None = None
    timed_out: bool = False
    page_closed: bool = False
    elapsed_s: float = 0.0


# ── validation ──────────────────────────────────────────────────────────────


def _whole(raw: Any) -> bool:
    return isinstance(raw, int) and not isinstance(raw, bool)


def _validate_one(i: int, step: Any) -> str:
    where = f"step {i + 1}"
    if not isinstance(step, dict) or len(step) != 1:
        return f"{where} must be an object with exactly one action ({', '.join(ACTIONS)})"
    ((action, value),) = step.items()
    if action not in ACTIONS:
        return f"{where}: unknown action {action!r}; use one of {', '.join(ACTIONS)}"
    if action == "click":
        if isinstance(value, str):
            if not value.strip() or len(value) > MAX_SELECTOR_CHARS:
                return f"{where}: `click` selector must be 1-{MAX_SELECTOR_CHARS} chars"
            return ""
        if (
            isinstance(value, dict)
            and set(value) == {"x", "y"}
            and _whole(value["x"])
            and _whole(value["y"])
            and value["x"] >= 0
            and value["y"] >= 0
        ):
            return ""
        return f"{where}: `click` takes a CSS selector or {{x, y}} whole, non-negative pixels"
    if action == "type":
        if not (isinstance(value, dict) and set(value) == {"selector", "text"}):
            return f"{where}: `type` takes {{selector, text}}"
        sel, text = value["selector"], value["text"]
        if not isinstance(sel, str) or not sel.strip() or len(sel) > MAX_SELECTOR_CHARS:
            return f"{where}: `type.selector` must be 1-{MAX_SELECTOR_CHARS} chars"
        if not isinstance(text, str) or len(text) > MAX_TYPE_CHARS:
            return f"{where}: `type.text` must be a string of at most {MAX_TYPE_CHARS} chars"
        return ""
    if action == "mouse_move":
        if not (isinstance(value, dict) and set(value) == {"dx", "dy"}):
            return f"{where}: `mouse_move` takes {{dx, dy}} (relative pixels)"
        if not all(_whole(value[k]) and abs(value[k]) <= MAX_MOVE_PX for k in ("dx", "dy")):
            return f"{where}: `mouse_move` dx and dy must be whole numbers within ±{MAX_MOVE_PX}"
        return ""
    if action == "scroll":
        if not (isinstance(value, dict) and set(value) == {"dy"} and _whole(value["dy"])):
            return f"{where}: `scroll` takes {{dy}} (whole pixels, positive scrolls down)"
        if abs(value["dy"]) > MAX_SCROLL_PX:
            return f"{where}: `scroll.dy` must be within ±{MAX_SCROLL_PX}"
        return ""
    if action == "wait":
        if not _whole(value) or not 0 <= value <= MAX_WAIT_MS:
            return f"{where}: `wait` takes whole milliseconds, 0-{MAX_WAIT_MS}"
        return ""
    if action == "screenshot":
        if not isinstance(value, str) or not _LABEL_RE.fullmatch(value):
            return (
                f"{where}: `screenshot` takes a short label (1-{MAX_LABEL_CHARS} letters, "
                "digits, spaces, `.`, `:`, `-` or `_`)"
            )
        return ""
    return ""


def _validate_press(i: int, step: dict) -> str:
    where = f"step {i + 1}"
    key = step.get("press")
    if not isinstance(key, str) or not (
        (len(key) == 1 and key.isprintable()) or (len(key) <= 32 and _KEY_RE.fullmatch(key))
    ):
        return (
            f"{where}: `press` takes a Playwright key name such as `ArrowUp`, `w`, "
            f"`Space`, `Enter` or `Shift+W`; got {key!r}"
        )
    if "hold_ms" in step:
        hold = step["hold_ms"]
        if not _whole(hold) or not 0 <= hold <= MAX_HOLD_MS:
            return f"{where}: `hold_ms` takes whole milliseconds, 0-{MAX_HOLD_MS}"
    return ""


def validate_steps(raw: Any) -> tuple[list[dict], str]:
    """``(steps, "")`` for a usable step list, or ``([], reason)``. The reason names
    the first bad step and what it should look like."""
    if raw is None:
        return [], ""
    if not isinstance(raw, list):
        return [], "`steps` must be a list of step objects."
    if len(raw) > MAX_STEPS:
        return [], f"`steps` has {len(raw)} steps; the most one run takes is {MAX_STEPS}."
    shots = 0
    pause = 0
    for i, step in enumerate(raw):
        if isinstance(step, dict) and "press" in step:
            extra = set(step) - {"press", "hold_ms"}
            if extra:
                return [], (
                    f"step {i + 1} must be an object with exactly one action "
                    f"({', '.join(ACTIONS)}); `press` only allows `hold_ms` beside it"
                )
            bad = _validate_press(i, step)
            pause += step.get("hold_ms", 0)
        else:
            bad = _validate_one(i, step)
            if not bad and "wait" in step:
                pause += step["wait"]
            if not bad and "screenshot" in step:
                shots += 1
        if bad:
            return [], bad + "."
    if shots > MAX_SCREENSHOTS:
        return [], (
            f"`steps` asks for {shots} screenshots; the most one run takes is {MAX_SCREENSHOTS}."
        )
    if pause > MAX_TOTAL_PAUSE_MS:
        return [], (
            f"`steps` waits and holds for {pause} ms in total; keep it under "
            f"{MAX_TOTAL_PAUSE_MS} ms so the run fits its {int(RUN_BUDGET_S)}s budget."
        )
    return list(raw), ""


# ── capture helpers ─────────────────────────────────────────────────────────


def clean_text(text: Any, limit: int = MAX_EVENT_CHARS) -> str:
    """One line of untrusted page text: control characters dropped, whitespace
    collapsed, cut to ``limit``."""
    out = " ".join(_CONTROL_RE.sub(" ", str(text)).split())
    return out if len(out) <= limit else out[: limit - 1] + "…"


def _push(bucket: list[str], seen: set[str], text: Any) -> None:
    line = clean_text(text)
    if not line or line in seen or len(bucket) >= MAX_EVENTS:
        return
    seen.add(line)
    bucket.append(line)


def blank_canvas(image: bytes, rect: dict | None) -> bool | None:
    """True when the ``rect`` crop of ``image`` is one flat colour, False when it has
    content, None when there is no canvas big enough to judge. WebGL canvases
    cannot be read back with ``toDataURL``, so the screenshot is the only honest
    view of what they drew."""
    if not rect:
        return None
    try:
        w, h = float(rect["w"]), float(rect["h"])
        x, y = float(rect["x"]), float(rect["y"])
    except (KeyError, TypeError, ValueError):
        return None
    if w < MIN_CANVAS_PX or h < MIN_CANVAS_PX:
        return None
    from PIL import Image, ImageStat

    with Image.open(io.BytesIO(image)) as img:
        crop = img.convert("RGB").crop((int(x), int(y), int(x + w), int(y + h)))
        if crop.width < MIN_CANVAS_PX or crop.height < MIN_CANVAS_PX:
            return None
        return max(ImageStat.Stat(crop).var) < BLANK_VARIANCE


def to_jpeg(image: bytes, budget_b64: int) -> bytes:
    """A viewport screenshot as a JPEG whose base64 fits ``budget_b64``: quality 70,
    then 55, then downscaled until it fits (never below 256px on the long edge)."""
    from PIL import Image

    def enc(img: Any, quality: int) -> bytes:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()

    def b64(data: bytes) -> int:
        return 4 * ((len(data) + 2) // 3)

    with Image.open(io.BytesIO(image)) as src:
        img = src.convert("RGB")
    if max(img.size) > 1568:
        img.thumbnail((1568, 1568), Image.Resampling.LANCZOS)
    out = enc(img, 70)
    if b64(out) > budget_b64:
        out = enc(img, 55)
    while b64(out) > budget_b64 and max(img.size) > 256:
        img = img.resize((max(1, round(img.width * 0.8)), max(1, round(img.height * 0.8))))
        out = enc(img, 55)
    return out


# ── the remote browser ──────────────────────────────────────────────────────


@asynccontextmanager
async def open_cdp_browser(ws_url: str, headers: dict[str, str]) -> AsyncIterator[Any]:
    """Connect to a remote Chromium over CDP and close the session on the way out,
    whatever happened inside. Raises ``PlayTestUnavailable`` when Playwright is not
    installed or the connect fails."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise PlayTestUnavailable("the Playwright client is not installed") from exc

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(
                ws_url, headers=headers, timeout=CONNECT_TIMEOUT_MS
            )
        except Exception as exc:  # noqa: BLE001 — every connect failure means "unavailable"
            raise PlayTestUnavailable(f"could not connect to the remote browser: {exc}") from exc
        try:
            yield browser
        finally:
            await _end_session(browser)


async def _end_session(browser: Any) -> None:
    """Close the remote browser, which ends the Browser Run session. Best effort:
    a failure here is logged, never raised, and ``keep_alive`` is the backstop."""
    try:
        cdp = await asyncio.wait_for(browser.new_browser_cdp_session(), 5)
        await asyncio.wait_for(cdp.send("Browser.close"), 5)
    except Exception:  # noqa: BLE001
        logger.debug("play_test: CDP Browser.close failed", exc_info=True)
    try:
        await asyncio.wait_for(browser.close(), 5)
    except Exception:  # noqa: BLE001
        logger.debug("play_test: browser.close failed", exc_info=True)


# ── the run ─────────────────────────────────────────────────────────────────


def _attach_listeners(page: Any, result: PlayResult) -> None:
    seen: dict[str, set[str]] = {"c": set(), "e": set(), "r": set()}

    def on_console(msg: Any) -> None:
        kind = getattr(msg, "type", "")
        if kind in ("error", "warning"):
            _push(result.console, seen["c"], f"[{kind}] {getattr(msg, 'text', '')}")

    def on_page_error(err: Any) -> None:
        _push(result.page_errors, seen["e"], getattr(err, "message", None) or err)

    def on_request_failed(req: Any) -> None:
        failure = getattr(req, "failure", None)
        _push(result.failed_requests, seen["r"], f"{req.url} ({failure or 'failed'})")

    def on_response(resp: Any) -> None:
        if getattr(resp, "status", 0) >= 400:
            _push(result.failed_requests, seen["r"], f"HTTP {resp.status} {resp.url}")

    page.on("console", on_console)
    page.on("pageerror", on_page_error)
    page.on("requestfailed", on_request_failed)
    page.on("response", on_response)


async def _shoot(page: Any, label: str, result: PlayResult) -> str:
    image = await page.screenshot(type="png", full_page=False, scale="css", timeout=10_000)
    rect = None
    try:
        rect = await page.evaluate(_CANVAS_RECT_JS)
    except Exception:  # noqa: BLE001 — no rect just means no blank check
        rect = None
    flag = blank_canvas(image, rect)
    result.shots.append(Shot(label=label, image=image, blank_canvas=flag))
    return "ok (canvas looks blank)" if flag else "ok"


async def _do(page: Any, step: dict, mobile: bool, mouse: list[float], result: PlayResult) -> str:
    """Run one validated step; return its outcome line (``ok`` or what went wrong
    is raised by Playwright and caught by the caller)."""
    if "press" in step:
        key = step["press"]
        hold = step.get("hold_ms")
        if hold:
            keys = key.split("+") if len(key) > 1 else [key]
            for k in keys:
                await page.keyboard.down(k)
            await page.wait_for_timeout(hold)
            for k in reversed(keys):
                await page.keyboard.up(k)
        else:
            await page.keyboard.press(key)
        return "ok"
    ((action, value),) = step.items()
    if action == "click":
        if isinstance(value, str):
            if mobile:
                await page.tap(value, timeout=ACTION_TIMEOUT_MS)
            else:
                await page.click(value, timeout=ACTION_TIMEOUT_MS)
        else:
            mouse[0], mouse[1] = float(value["x"]), float(value["y"])
            if mobile:
                await page.touchscreen.tap(value["x"], value["y"])
            else:
                await page.mouse.click(value["x"], value["y"])
        return "ok"
    if action == "type":
        locator = page.locator(value["selector"]).first
        await locator.click(timeout=ACTION_TIMEOUT_MS)
        await locator.press_sequentially(value["text"], delay=10, timeout=ACTION_TIMEOUT_MS * 3)
        return "ok"
    if action == "mouse_move":
        mouse[0] += value["dx"]
        mouse[1] += value["dy"]
        await page.mouse.move(mouse[0], mouse[1], steps=8)
        return "ok"
    if action == "scroll":
        await page.mouse.wheel(0, value["dy"])
        return "ok"
    if action == "wait":
        await page.wait_for_timeout(value)
        return "ok"
    if action == "screenshot":
        return await _shoot(page, value, result)
    return "skipped (unknown action)"


def _describe(step: dict) -> str:
    return clean_text(", ".join(f"{k}={v!r}" for k, v in step.items()), 120)


async def _play(
    browser: Any,
    url: str,
    viewport: dict[str, int],
    mobile: bool,
    steps: list[dict],
    result: PlayResult,
    deadline: float,
) -> None:
    context = await browser.new_context(viewport=dict(viewport), has_touch=mobile, is_mobile=mobile)
    try:
        await context.add_init_script(RAF_COUNTER_JS)
        page = await context.new_page()
        _attach_listeners(page, result)
        try:
            await page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
            await page.wait_for_timeout(SETTLE_MS)
        except Exception as exc:  # noqa: BLE001
            result.navigation_error = clean_text(exc)
            return
        mouse = [viewport["width"] / 2, viewport["height"] / 2]
        for i, step in enumerate(steps):
            line = f"{i + 1}. {_describe(step)}: "
            remaining = deadline - time.monotonic()
            if remaining <= 1:
                result.timed_out = True
                result.steps.append(line + "skipped (run budget spent)")
                continue
            try:
                outcome = await asyncio.wait_for(_do(page, step, mobile, mouse, result), remaining)
            except TimeoutError:
                result.timed_out = True
                outcome = "stopped (run budget spent)"
            except Exception as exc:  # noqa: BLE001 — a failed step is logged, not fatal
                outcome = f"failed: {clean_text(exc, 200)}"
            result.steps.append(line + outcome)
            if page.is_closed():
                result.page_closed = True
                break
        if not result.page_closed and not result.shots:
            if deadline - time.monotonic() > 2:
                try:
                    await _shoot(page, "final", result)
                except Exception:  # noqa: BLE001
                    logger.debug("play_test: final screenshot failed", exc_info=True)
        if not result.page_closed:
            try:
                frames = await page.evaluate(_RAF_READ_JS)
            except Exception:  # noqa: BLE001
                frames = None
            if isinstance(frames, dict) and frames.get("ms"):
                result.frames = int(frames.get("frames") or 0)
                result.fps = round(result.frames / (float(frames["ms"]) / 1000), 1)
    finally:
        try:
            await asyncio.wait_for(context.close(), 5)
        except Exception:  # noqa: BLE001
            logger.debug("play_test: context.close failed", exc_info=True)


async def run_play_test(
    *,
    url: str,
    viewport: dict[str, int],
    mobile: bool,
    steps: list[dict],
    ws_url: str,
    headers: dict[str, str],
    opener: Callable[[str, dict[str, str]], Any] | None = None,
    budget_s: float = RUN_BUDGET_S,
) -> PlayResult:
    """Play ``steps`` on ``url`` in the remote browser at ``ws_url``. Raises
    ``PlayTestUnavailable`` when the browser cannot be reached; otherwise returns
    whatever was observed, partial when the budget ran out."""
    started = time.monotonic()
    deadline = started + budget_s
    result = PlayResult(url=url)
    open_browser = opener or open_cdp_browser
    try:
        async with asyncio.timeout(budget_s):
            async with open_browser(ws_url, headers) as browser:
                await _play(browser, url, viewport, mobile, steps, result, deadline)
    except TimeoutError:
        result.timed_out = True
    result.elapsed_s = round(time.monotonic() - started, 1)
    return result


__all__ = [
    "MAX_SCREENSHOTS",
    "MAX_STEPS",
    "KEEP_ALIVE_MS",
    "PlayResult",
    "PlayTestUnavailable",
    "RAF_COUNTER_JS",
    "Shot",
    "blank_canvas",
    "clean_text",
    "open_cdp_browser",
    "run_play_test",
    "to_jpeg",
    "validate_steps",
]
