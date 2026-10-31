# play_test.py — drive a site draft in Cloudflare's remote browser and report what
# happened: the engine behind the ``play_test_site`` agent tool.
#
# The agent hands over a short list of steps (click, press, type, mouse_move,
# scroll, wait, screenshot). ``validate_steps`` checks them strictly before any
# browser exists. ``run_play_test`` connects to Browser Run over CDP
# (``CloudflareClient.browser_run_cdp``), opens the draft, plays the steps and
# returns a ``PlayResult``: a step log, console errors and warnings, uncaught page
# errors, failed requests, an approximate frame rate and viewport screenshots with
# a blank-canvas flag each. With ``exercise_forms`` it then submits each form on the
# page with obviously fake data (``exercise_page_forms``) and ``judge_form`` says
# whether each one was sent AND confirmed (``FormOutcome``).
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
#   * A form test never reaches the owner's real data: same-origin submits go to the
#     draft (draft-only storage), gateway posts are stubbed, other hosts are aborted.
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
from urllib.parse import parse_qs, urlsplit

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


#: Forms one run submits, each on a fresh load of the page.
MAX_FORMS = 4
#: How long a submit gets to answer and the page to show its result.
FORM_SETTLE_MS = 2_500
#: A form is not started with less than this left in the run budget.
FORM_MIN_S = 8.0
#: Obviously fake test data: example.com never delivers, 555-01xx rings nobody.
TEST_DATA = {
    "name": "Paw Draft Test",
    "email": "paw.draft.test@example.com",
    "text": "Paw draft test, please ignore",
    "phones": ["5550100123", "+15550100123", "07700900123", "+447700900123", "9000000000"],
}
#: The Paw integration gateway paw.js posts to (``/sites/<id>/x/<integration>/<action>``):
#: a test submit is answered with a stub there, so no real subscriber or order is made.
_GATEWAY_RE = re.compile(r"/sites/[^/]+/x/[^/]+/[^/]+$")
#: Words a page shows once an entry went through. Counted before and after the submit.
_SUCCESS_RE = re.compile(
    r"thank|success|received|confirmed|you'?re (?:on|in|booked|all set)|booked|subscribed"
    r"|\bsent\b|see you|be in touch|got it|on the list|added",
    re.IGNORECASE,
)

#: Fill form ``i`` (document order, dialogs skipped) with the test data through the
#: native value setters, so framework-controlled inputs see it, and mark it for the
#: submit. Returns what the judge needs, or null when there is no form ``i``.
_FILL_FORM_JS = """
(args) => {
  const [i, data] = args;
  const forms = [...document.querySelectorAll('form')]
    .filter(f => (f.getAttribute('method') || '').toLowerCase() !== 'dialog');
  const f = forms[i];
  if (!f) return null;
  const name = f.id || f.getAttribute('name') || f.getAttribute('aria-label') || `form ${i + 1}`;
  const set = (el, v) => {
    const desc = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value');
    if (desc && desc.set) desc.set.call(el, v); else el.value = v;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  };
  const fit = (el, v) => (el.maxLength > 0 ? String(v).slice(0, el.maxLength) : String(v));
  const day = new Date(Date.now() + 7 * 864e5).toISOString().slice(0, 10);
  const skip = ['hidden', 'submit', 'button', 'reset', 'image', 'file', 'range', 'color'];
  const radios = new Set();
  const fields = [];
  for (const el of f.elements) {
    const t = (el.type || '').toLowerCase();
    const n = (el.name || el.id || '').toLowerCase();
    if (el.disabled || el.readOnly || skip.includes(t)) continue;
    if (el.name) fields.push(el.name);
    if (el.tagName === 'SELECT') {
      const opt = [...el.options].find(o => o.value && !o.disabled);
      if (opt) set(el, opt.value);
    } else if (t === 'checkbox') {
      if (el.required && !el.checked) el.click();
    } else if (t === 'radio') {
      if (!radios.has(el.name)) { radios.add(el.name); if (!el.checked) el.click(); }
    } else if (t === 'email') set(el, fit(el, data.email));
    else if (t === 'tel' || /phone|mobile|whatsapp/.test(n)) {
      let re = null;
      try { re = el.pattern ? new RegExp(`^(?:${el.pattern})$`) : null; } catch (e) {}
      set(el, fit(el, data.phones.find(p => !re || re.test(p)) || data.phones[0]));
    } else if (t === 'number') set(el, el.min || '1');
    else if (t === 'date') set(el, el.min && el.min > day ? el.min : day);
    else if (t === 'time') set(el, '12:00');
    else if (t === 'datetime-local') set(el, `${day}T12:00`);
    else if (t === 'month') set(el, day.slice(0, 7));
    else if (t === 'url') set(el, 'https://example.com');
    else if (t === 'password') set(el, 'PawDraftTest-123!');
    else if (/name/.test(n)) set(el, fit(el, data.name));
    else set(el, fit(el, data.text));
  }
  f.setAttribute('data-paw-exercise', '1');
  let external = '';
  try {
    const u = new URL(f.getAttribute('action') || location.href, location.href);
    if (u.origin !== location.origin) external = u.host;
  } catch (e) {}
  const invalid = f.checkValidity() ? [] : [...f.elements]
    .filter(e => e.willValidate && !e.validity.valid).slice(0, 3)
    .map(e => `${e.name || e.id || e.type}: ${e.validationMessage}`);
  const search = (f.getAttribute('role') || '').toLowerCase() === 'search'
    || [...f.elements].some(e => (e.type || '').toLowerCase() === 'search');
  return {
    name, count: forms.length, fields, invalid, external, search,
    text: document.body ? document.body.innerText.slice(0, 20000) : '',
  };
}
"""
_SUBMIT_FORM_JS = """
() => {
  const f = document.querySelector('form[data-paw-exercise]');
  if (!f) return false;
  if (f.requestSubmit) f.requestSubmit(); else f.submit();
  return true;
}
"""
_FORM_STATE_JS = """
() => {
  const f = document.querySelector('form[data-paw-exercise]');
  return {
    state: f ? f.getAttribute('data-paw-state') : null,
    url: location.href,
    text: document.body ? document.body.innerText.slice(0, 20000) : '',
  };
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
    forms: list[FormOutcome] = field(default_factory=list)
    forms_note: str = ""


@dataclass
class FormOutcome:
    """One form's test submit. ``status``: ``ok`` (sent and confirmed), ``failed`` (a
    defect a visitor hits) or ``unchecked`` (the test could not tell: the browser's own
    validation refused the test entry, or it posts to another service the test does not
    contact). ``outcome`` is safe to show the owner; ``detail`` is for the agent."""

    name: str
    status: str
    outcome: str
    detail: str = ""


def _same_page(a: str, b: str) -> bool:
    ua, ub = urlsplit(a), urlsplit(b)
    return (ua.netloc, ua.path.rstrip("/")) == (ub.netloc, ub.path.rstrip("/"))


def judge_form(
    info: dict[str, Any],
    state: dict[str, Any] | None,
    sent: list[dict[str, Any]],
    base_url: str,
    new_errors: list[str] | None = None,
) -> FormOutcome:
    """Decide what one test submit showed. ``info`` is the fill result, ``state`` the
    page after the submit (``None`` when it could not be read), ``sent`` every non-GET
    request and navigation the submit caused (``{kind, method, url, status}``; ``kind``
    is ``site``, ``gateway``, ``external`` or ``navigation``)."""
    name = clean_text(info.get("name") or "form", 40)
    lines = [f"{s['kind']} {s.get('method', '')} {s['url']} -> {s.get('status')}" for s in sent]
    if new_errors:
        lines += [f"page error: {e}" for e in new_errors]
    detail = clean_text("; ".join(lines), MAX_EVENT_CHARS) if lines else "no request was sent"
    if info.get("invalid"):
        why = clean_text(", ".join(info["invalid"]), 160)
        return FormOutcome(
            name, "unchecked", "the form's own checks refused the test entry", f"invalid: {why}"
        )
    external = [s for s in sent if s["kind"] == "external"]
    posted = [s for s in sent if s["kind"] in ("site", "gateway")]
    if info.get("external") or (external and not posted):
        host = info.get("external") or urlsplit(external[0]["url"]).netloc
        return FormOutcome(
            name,
            "unchecked",
            f"it sends to {host}, which the test does not contact",
            "submit blocked so no real email or record was made at the other service",
        )
    errors = [s for s in posted if (s.get("status") or 0) >= 400]
    navs = [s for s in sent if s["kind"] == "navigation"]
    bad_nav = [s for s in navs if (s.get("status") or 0) >= 400]
    if errors or bad_nav:
        code = (errors or bad_nav)[0].get("status")
        return FormOutcome(name, "failed", f"the site answered with an error (HTTP {code})", detail)
    fields = [f for f in info.get("fields") or () if f]
    for nav in navs:
        query = parse_qs(urlsplit(nav["url"]).query)
        if _same_page(nav["url"], base_url) and any(f in query for f in fields):
            return FormOutcome(
                name,
                "failed",
                "nothing was saved: what was typed ended up in the address bar",
                detail,
            )
    page = state or {}
    before = len(_SUCCESS_RE.findall(info.get("text") or ""))
    after = len(_SUCCESS_RE.findall(page.get("text") or ""))
    confirmed = page.get("state") == "success" or after > before
    moved = any(not _same_page(n["url"], base_url) for n in navs)
    if page.get("state") == "error":
        return FormOutcome(name, "failed", "the form showed an error", detail)
    if not posted and not navs:
        return FormOutcome(name, "failed", "nothing was sent and the page did not change", detail)
    if confirmed or moved:
        return FormOutcome(name, "ok", "sent and confirmed", detail)
    if new_errors:
        return FormOutcome(name, "failed", "the page hit a script error", detail)
    return FormOutcome(
        name, "failed", "it was sent, but the page showed no thank-you or confirmation", detail
    )


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


async def exercise_page_forms(page: Any, url: str, result: PlayResult, deadline: float) -> None:
    """Submit each form on ``url`` (up to :data:`MAX_FORMS`, a fresh load each) with
    :data:`TEST_DATA` and judge what happened (:func:`judge_form`).

    Where the entries go: a same-origin submit reaches the draft itself, whose server
    (a project draft Worker) has draft-only D1 / KV / R2 that publish purges. A post to
    the Paw integration gateway is answered with a stub, so no real subscriber, lead or
    order is made. A non-GET to any other host is aborted, so no email is sent and no
    record is made elsewhere. Search boxes are not submitted."""
    origin = urlsplit(url)._replace(path="", query="", fragment="").geturl()
    sent: list[dict[str, Any]] = []
    armed = [False]

    async def route(r: Any) -> None:
        req = r.request
        method = (getattr(req, "method", "GET") or "GET").upper()
        if not armed[0] or method in ("GET", "HEAD", "OPTIONS"):
            await r.continue_()
            return
        target = req.url
        if _GATEWAY_RE.search(urlsplit(target).path):
            sent.append({"kind": "gateway", "method": method, "url": target, "status": 200})
            await r.fulfill(status=200, content_type="application/json", body='{"ok":true}')
            return
        if urlsplit(target)._replace(path="", query="", fragment="").geturl() != origin:
            sent.append({"kind": "external", "method": method, "url": target, "status": None})
            await r.abort()
            return
        entry = {"kind": "site", "method": method, "url": target, "status": None}
        sent.append(entry)
        await r.continue_()

    def on_response(resp: Any) -> None:
        if not armed[0]:
            return
        req = resp.request
        method = (getattr(req, "method", "GET") or "GET").upper()
        for entry in sent:
            if entry["kind"] == "site" and entry["url"] == resp.url and entry["status"] is None:
                entry["status"] = resp.status
                return
        if method == "GET" and req.is_navigation_request():
            sent.append({"kind": "navigation", "method": method, "url": resp.url})
            sent[-1]["status"] = resp.status

    try:
        await page.route("**/*", route)
    except Exception as exc:  # noqa: BLE001
        result.forms_note = f"forms were not exercised: {clean_text(exc, 120)}"
        return
    page.on("response", on_response)
    for i in range(MAX_FORMS):
        if deadline - time.monotonic() < FORM_MIN_S:
            result.forms_note = "the run budget ran out before every form was tried"
            result.timed_out = True
            break
        armed[0] = False
        sent.clear()
        try:
            await page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
            await page.wait_for_timeout(SETTLE_MS)
            info = await page.evaluate(_FILL_FORM_JS, [i, TEST_DATA])
        except Exception as exc:  # noqa: BLE001
            result.forms_note = f"the page did not load for form {i + 1}: {clean_text(exc, 120)}"
            break
        if not info:
            if i == 0:
                result.forms_note = "this page has no forms"
            break
        if i == 0 and int(info.get("count") or 0) > MAX_FORMS:
            result.forms_note = f"only the first {MAX_FORMS} of {info['count']} forms were tried"
        if info.get("search"):
            name = clean_text(info.get("name") or "form", 40)
            result.forms.append(FormOutcome(name, "unchecked", "a search box, not submitted"))
            continue
        errors_before = len(result.page_errors)
        state = None
        if not info.get("invalid"):
            armed[0] = True
            try:
                await page.evaluate(_SUBMIT_FORM_JS)
            except Exception:  # noqa: BLE001 — a navigating submit tears the context down
                logger.debug("play_test: submit evaluate interrupted", exc_info=True)
            try:
                await page.wait_for_load_state("load", timeout=FORM_SETTLE_MS)
            except Exception:  # noqa: BLE001
                pass
            await page.wait_for_timeout(FORM_SETTLE_MS)
            armed[0] = False
            try:
                state = await page.evaluate(_FORM_STATE_JS)
            except Exception:  # noqa: BLE001
                state = None
        new_errors = result.page_errors[errors_before:]
        result.forms.append(judge_form(info, state, list(sent), url, new_errors))


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
    exercise_forms: bool = False,
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
        if exercise_forms and not result.page_closed:
            await exercise_page_forms(page, url, result, deadline)
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
    exercise_forms: bool = False,
) -> PlayResult:
    """Play ``steps`` on ``url`` in the remote browser at ``ws_url``, then, with
    ``exercise_forms``, submit each form on the page with test data
    (:func:`exercise_page_forms`). Raises ``PlayTestUnavailable`` when the browser
    cannot be reached; otherwise returns whatever was observed, partial when the
    budget ran out."""
    started = time.monotonic()
    deadline = started + budget_s
    result = PlayResult(url=url)
    open_browser = opener or open_cdp_browser
    try:
        async with asyncio.timeout(budget_s):
            async with open_browser(ws_url, headers) as browser:
                await _play(browser, url, viewport, mobile, steps, result, deadline, exercise_forms)
    except TimeoutError:
        result.timed_out = True
    result.elapsed_s = round(time.monotonic() - started, 1)
    return result


__all__ = [
    "FormOutcome",
    "MAX_FORMS",
    "MAX_SCREENSHOTS",
    "MAX_STEPS",
    "KEEP_ALIVE_MS",
    "PlayResult",
    "PlayTestUnavailable",
    "RAF_COUNTER_JS",
    "Shot",
    "blank_canvas",
    "clean_text",
    "exercise_page_forms",
    "judge_form",
    "open_cdp_browser",
    "run_play_test",
    "to_jpeg",
    "validate_steps",
]
