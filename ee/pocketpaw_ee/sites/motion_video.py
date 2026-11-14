# ee/pocketpaw_ee/sites/motion_video.py — the "motion" engine for site video.
#
# Renders a short MP4 from HTML instead of a generative model: an LLM writes ONE
# HyperFrames composition (index.html, anime.js v3 animations), the HyperFrames
# CLI lints it and renders it frame by frame in headless Chrome, and ffmpeg pulls
# a poster frame. Deterministic, no fal key, good for animated typography, shapes
# and brand graphics behind a site's hero. ``media_generation`` picks this engine
# (``engine="motion"``, or ``auto`` with no fal key) and owns storage + history.
#
# Three steps, each a seam tests replace:
#   compose  ``_chat`` — one completion through the LiteLLM proxy with the
#            workspace's own key (studio ``_resolve_auth_key``). Model:
#            POCKETPAW_SITES_MOTION_MODEL, else the deployment default.
#   validate ``composition_problems`` (contract + the ``motion_safety`` allow-list)
#            and ``hyperframes lint``. ONE repair round-trip to the LLM, then fail.
#   render   ``hyperframes render`` → MP4, remuxed with +faststart, poster at 0.5s.
#
# INVARIANTS (the HTML is untrusted: LLM-written from a user brief):
#   * The render browser is OFF THE NETWORK. ``vendor/hf-network-guard.cjs`` is
#     preloaded into the CLI (NODE_OPTIONS), wraps every child_process launcher,
#     and gives every Chrome a dead proxy that bypasses only the CLI's own project
#     file server, and no DNS. It logs each Chrome launch to a guard file outside
#     the project; output is discarded unless there is a PID and no UNGUARDED
#     line. (Upstream: that file server binds all interfaces during a render.)
#   * The CLI sees an allow-listed environment (``motion_safety.renderer_env``),
#     never the server's secrets.
#   * anime.js is VENDORED (vendor/anime-3.2.2.min.js, MIT) and copied next to
#     index.html as ``anime.min.js``. v3, not v4: the HyperFrames adapter seeks
#     every instance in ``window.__hfAnime``.
#   * The CLI comes from POCKETPAW_HYPERFRAMES_CMD, default a PINNED npx spec.
#   * Bounded: one motion job per workspace, 1-2 per process
#     (POCKETPAW_HYPERFRAMES_CONCURRENCY) with a short wait for a slot ("busy"),
#     and the whole compose+lint+render under OVERALL_DEADLINE_S. Every subprocess
#     is killed on timeout or cancellation, and so is every guarded Chrome (puppeteer
#     launches it detached, outside the CLI's process group). The temp dir is
#     always removed.
#   * ``reason`` is ``unavailable`` (renderer missing), ``busy`` (no slot) or
#     ``failed``; callers map the first two to 503.

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import re
import shlex
import shutil
import signal
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

ENGINE_ID = "hyperframes-animejs"
HYPERFRAMES_VERSION = "0.8.181"
DEFAULT_HYPERFRAMES_CMD = f"npx --yes hyperframes@{HYPERFRAMES_VERSION}"
HYPERFRAMES_CMD_ENV = "POCKETPAW_HYPERFRAMES_CMD"
MOTION_MODEL_ENV = "POCKETPAW_SITES_MOTION_MODEL"
CONCURRENCY_ENV = "POCKETPAW_HYPERFRAMES_CONCURRENCY"

ANIME_VERSION = "3.2.2"
ANIME_FILENAME = "anime.min.js"
_ANIME_VENDOR = Path(__file__).parent / "vendor" / f"anime-{ANIME_VERSION}.min.js"
_NETWORK_GUARD = Path(__file__).parent / "vendor" / "hf-network-guard.cjs"
GUARD_FILE_ENV = "POCKETPAW_HF_GUARD_FILE"

COMPOSITION_ID = "hero"
RENDER_TIMEOUT_S = 180
LINT_TIMEOUT_S = 60
FFMPEG_TIMEOUT_S = 30
_CHAT_TIMEOUT_S = 90.0
#: The whole job (compose, lint, repair, render, poster). Keeps one HTTP request
#: under ~5 minutes; a background job + polling is the real fix (follow-up).
OVERALL_DEADLINE_S = 280
#: How long a job waits for a render slot before answering "busy".
SLOT_WAIT_S = 15
_MAX_HTML_BYTES = 200_000

DIMENSIONS: dict[str, tuple[int, int]] = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
}

#: Families HyperFrames bundles and inlines at render time (no @font-face needed).
BUNDLED_FONTS = (
    "Inter",
    "Montserrat",
    "Outfit",
    "Nunito",
    "Oswald",
    "League Gothic",
    "Archivo Black",
    "Poppins",
    "Roboto",
    "Open Sans",
    "Lato",
    "Playfair Display",
    "EB Garamond",
    "Space Mono",
    "IBM Plex Mono",
    "JetBrains Mono",
    "Source Code Pro",
)

_IMAGE_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}


class MotionVideoError(Exception):
    """``reason`` is ``unavailable`` (renderer missing on this host), ``busy``
    (no render slot free) or ``failed``."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def _busy() -> MotionVideoError:
    return MotionVideoError("busy", "Motion video rendering is busy right now; try again shortly.")


def _unavailable(detail: str) -> MotionVideoError:
    return MotionVideoError(
        "unavailable", f"Motion video needs the HyperFrames renderer on the server: {detail}"
    )


@dataclass(frozen=True)
class MotionBrief:
    prompt: str
    aspect_ratio: str = "16:9"
    duration_sec: int = 5
    palette: list[str] = field(default_factory=list)
    image_filename: str | None = None

    @property
    def size(self) -> tuple[int, int]:
        return DIMENSIONS.get(self.aspect_ratio, DIMENSIONS["16:9"])


@dataclass(frozen=True)
class MotionRender:
    video: bytes
    poster: bytes | None
    html: str


# ── Compose ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a motion designer who writes HyperFrames compositions: one standalone HTML file that a headless browser renders frame by frame into an MP4. The clip plays muted, looped, behind a website's own headline and buttons. It must look like a premium, intentional hero background, not a demo.

OUTPUT: one complete HTML document, nothing else. No prose, no markdown fences.

HARD CONTRACT (the renderer rejects violations):
1. Start from the SKELETON in the request and keep its root <div> attributes exactly (data-composition-id, data-width, data-height, data-duration, data-no-timeline). #root is width:100%; height:100%; never hardcode pixel width/height on it.
2. Animation library: anime.js v3 (global `anime`), loaded ONLY by <script src="anime.min.js"></script>. No other script src, no GSAP, no CSS @keyframes, no CSS transitions.
3. Every animation is created with autoplay:false and pushed to window.__hfAnime, e.g. window.__hfAnime.push(anime({targets:'#a', translateX:[0,40], duration:5000, easing:'easeInOutSine', autoplay:false})); or a timeline: const tl = anime.timeline({autoplay:false, easing:'easeInOutSine'}); ...; window.__hfAnime.push(tl). The renderer SEEKS these (milliseconds); never call play(), restart() or rely on wall-clock time.
4. Times are milliseconds. Every animation, including loops and delays, ends at or before the clip length (data-duration x 1000). loop:true is forbidden; use a finite loop count where count x duration <= clip length.
5. Determinism: no Math.random (use the seeded rand() in the skeleton), no Date, no performance.now, no setTimeout/setInterval/requestAnimationFrame, no fetch, no input/hover/scroll state.
6. No network: no http(s) URLs anywhere (no <link>, no @import, no web fonts, no remote images). Inline SVG is fine (its xmlns attribute is fine).
7. Fonts: use only these families by name, with no @font-face: {fonts}; or system-ui / serif / sans-serif / monospace.
8. Never give an element a CSS transform AND animate its transform in anime; set the start state with anime from/to arrays (translateX:[-40,0]). Elements you transform are display:block or inline-block with a real width and height.
9. No <video>, <audio>, <canvas>, <iframe>, <object>, <embed>, <link>, <use>, <image>, <foreignObject>, <a>, <form>. No <br> inside text. No event-handler attributes.
10. The only URLs allowed anywhere are url(#id) / href="#id" fragments and the provided local image filename. In JS: create elements only with literal tag names (document.createElementNS('http://www.w3.org/2000/svg', ...) is NOT allowed; create SVG shapes in the markup instead, or use divs), setAttribute only with literal, non-URL, non-style attribute names, no innerHTML, no string concatenation of two literals, no .join()/.concat()/.repeat(), no Object.assign/defineProperty, no window[...] / globalThis / eval / Function / constructor, no assigning .src or .href, no style.cssText or setProperty. Build repeated elements with document.createElement('div') and el.style.<property> = value.

DESIGN (this is what makes it good):
- ONE idea, executed well: e.g. slow aurora gradients, drifting geometric shapes, a field of seeded dots breathing, giant outlined letterforms sliding past, light sweeping across a surface. Not a slideshow, not a sequence of scenes.
- It sits BEHIND the site's headline and call to action. Keep the area where a headline sits (center, or the left half on wide frames) calm: low contrast, no small detail, no readable words there. Put energy at the edges and in the depth.
- Words only if the brief asks for them. Kinetic type here means huge, cropped, low-contrast letterforms as texture (outline strokes, 8-20vw), never a readable tagline competing with the page.
- Use the brand palette: one dominant background tone, one or two accents, tints and shades of them. Gradients, soft blurred shapes (filter: blur), subtle grain (an SVG feTurbulence overlay at low opacity) and layering give depth. Avoid pure black or pure white fills unless the palette is monochrome.
- Motion is slow, continuous and eased (easeInOutSine, easeInOutQuad). Everything drifts a little all the time; nothing flashes, strobes, pops in or snaps. Amplitude is modest: translations under 10% of the frame, rotations slow, scale 0.9-1.15.
- SEAMLESS LOOP: the frame at the last moment must match the first frame exactly, because the site plays it on loop. Build every animation as a full cycle that returns to its start values exactly at the clip length: keyframes [a, b, a]; direction:'alternate' with an even loop count; full 360-degree rotations; or a translation by exactly one pattern period. No fade-in from black, no entrance, no ending.
- Fill the whole frame edge to edge at every moment; oversize drifting layers so their edges never show.
- Compose for the aspect ratio: on 9:16 stack vertically; on 1:1 centre the mass."""

SKELETON = """<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width={w}, height={h}" />
<script src="anime.min.js"></script>
<style>
  html, body {{ margin: 0; background: BACKGROUND; }}
  #root {{ position: relative; width: 100%; height: 100%; overflow: hidden; background: BACKGROUND; }}
  /* your styles */
</style>
</head>
<body>
<div id="root" data-composition-id="{cid}" data-start="0" data-width="{w}" data-height="{h}" data-duration="{d}" data-no-timeline>
  <!-- your layers -->
</div>
<script>
  window.__hfAnime = window.__hfAnime || [];
  const rand = (function (s) {{ return function () {{ s |= 0; s = (s + 0x6d2b79f5) | 0; let t = Math.imul(s ^ (s >>> 15), 1 | s); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }}; }})(20251021);
  const DURATION = {ms};
  // build layers with rand() if needed, then push every anime instance to window.__hfAnime
</script>
</body>
</html>"""


def system_prompt() -> str:
    return SYSTEM_PROMPT.replace("{fonts}", ", ".join(BUNDLED_FONTS))


def compose_request(brief: MotionBrief) -> str:
    """The user message: the brief, its constraints and the skeleton to fill."""
    w, h = brief.size
    skeleton = SKELETON.format(
        w=w, h=h, d=brief.duration_sec, ms=brief.duration_sec * 1000, cid=COMPOSITION_ID
    )
    lines = [
        f"BRIEF: {brief.prompt.strip()}",
        f"FRAME: {w}x{h} ({brief.aspect_ratio}), {brief.duration_sec} seconds "
        f"({brief.duration_sec * 1000} ms), seamless loop.",
    ]
    if brief.palette:
        lines.append(f"BRAND PALETTE (use these): {', '.join(brief.palette)}")
    else:
        lines.append(
            "BRAND PALETTE: none given. Pick a restrained palette that fits the brief "
            "(one dominant tone, one or two accents)."
        )
    if brief.image_filename:
        lines.append(
            f'IMAGE: the owner\'s image is the local file "{brief.image_filename}". Make it '
            "the full-bleed base layer (<img> with object-fit: cover, oversized ~115% so "
            "edges never show) with a slow Ken Burns move (scale and drift that return to "
            "the start by the end), a soft scrim gradient in the palette for headline "
            "legibility, and one or two light parallax layers of shapes or light over it."
        )
    else:
        lines.append("IMAGE: none. Do not reference any image file.")
    lines.append("SKELETON (fill it in; keep the root attributes exactly):")
    lines.append(skeleton)
    return "\n".join(lines)


def repair_request(html: str, problems: list[str]) -> str:
    listed = "\n".join(f"- {p}" for p in problems)
    return (
        "The composition below failed validation. Fix every problem, keep the design, "
        "and return the complete corrected HTML document only.\n\n"
        f"PROBLEMS:\n{listed}\n\nCOMPOSITION:\n{html}"
    )


async def _chat(*, workspace_id: str | None, system: str, user: str) -> str | None:
    """One completion through the LiteLLM proxy, billed to the workspace's own
    virtual key when it has one. Seam for tests."""
    from pocketpaw_ee.catalog import litellm_client
    from pocketpaw_ee.cloud.studio import service as studio

    model = (os.environ.get(MOTION_MODEL_ENV) or "").strip()
    if not model:
        from pocketpaw_ee.cloud.chat.side_completion import deployment_default_model

        model = deployment_default_model()
    extra = {"user": workspace_id} if workspace_id else {}
    return await litellm_client.chat_completion(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        api_key=await studio._resolve_auth_key(workspace_id),
        base_url=studio._proxy_base(),
        timeout=_CHAT_TIMEOUT_S,
        transport=studio._PROXY_TRANSPORT,
        temperature=0.7,
        max_tokens=12000,
        **extra,
    )


async def _ask(*, workspace_id: str | None, user: str) -> str:
    try:
        content = await _chat(workspace_id=workspace_id, system=system_prompt(), user=user)
    except Exception as exc:  # noqa: BLE001 — proxy/http/shape failures are one failure
        logger.warning("motion_video: compose call failed", exc_info=True)
        raise MotionVideoError(
            "failed", f"the motion composer could not be reached: {exc}"
        ) from exc
    html = extract_html(content or "")
    if not html:
        raise MotionVideoError("failed", "the motion composer returned no HTML")
    return html


_DOC_RE = re.compile(r"(<!doctype html.*?</html>|<html.*?</html>)", re.IGNORECASE | re.DOTALL)


def extract_html(content: str) -> str:
    """The HTML document inside a reply, without fences or chatter."""
    match = _DOC_RE.search(content or "")
    return match.group(1).strip() if match else ""


_ROOT_RE = re.compile(r"<div\b[^>]*\bdata-composition-id\s*=[^>]*>", re.IGNORECASE)


def normalize_composition(html: str) -> str:
    """Deterministic fixes that never need a model: make sure the root opts out
    of the GSAP timeline poll (``data-no-timeline``), which anime.js compositions
    must carry or the renderer waits 45s for a timeline that never comes."""
    root = _ROOT_RE.search(html)
    if root and "data-no-timeline" not in root.group(0):
        tag = root.group(0)
        html = html.replace(tag, tag[:-1].rstrip() + " data-no-timeline>", 1)
    return html


def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf'\b{name}\s*=\s*["\']?([^"\'\s>]+)', tag, re.IGNORECASE)
    return m.group(1) if m else None


_BANNED: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, re.IGNORECASE), why)
    for p, why in (
        (r"Math\.random\s*\(", "uses Math.random(); use the seeded rand() instead"),
        (r"\bDate\.now\s*\(|\bnew\s+Date\s*\(", "reads the clock (Date)"),
        (r"performance\.now\s*\(", "reads the clock (performance.now)"),
        (r"\bset(?:Timeout|Interval)\s*\(", "uses setTimeout/setInterval"),
        (r"requestAnimationFrame\s*\(", "uses requestAnimationFrame"),
        (r"\bloop\s*:\s*true", "uses loop:true (infinite); use a finite loop count"),
        (r"autoplay\s*:\s*true", "sets autoplay:true; every animation must be autoplay:false"),
        (r"@keyframes\b", "uses CSS @keyframes; animate with anime.js only"),
        (r"\bgsap\b", "uses GSAP; use anime.js only"),
    )
)


def composition_problems(html: str, brief: MotionBrief) -> list[str]:
    """The renderer contract plus the ``motion_safety`` allow-list. Empty means
    it may go to ``lint``."""
    from pocketpaw_ee.sites.motion_safety import unsafe_constructs

    problems: list[str] = []
    if len(html.encode()) > _MAX_HTML_BYTES:
        problems.append(f"the document is over {_MAX_HTML_BYTES // 1000} KB; simplify it")

    root = _ROOT_RE.search(html)
    if root is None:
        problems.append("no root <div> with data-composition-id")
    else:
        w, h = brief.size
        tag = root.group(0)
        if _attr(tag, "data-width") != str(w) or _attr(tag, "data-height") != str(h):
            problems.append(f'the root must carry data-width="{w}" data-height="{h}"')
        if _attr(tag, "data-duration") != str(brief.duration_sec):
            problems.append(f'the root must carry data-duration="{brief.duration_sec}"')

    if "__hfAnime" not in html or ".push(" not in html:
        problems.append("no animation is pushed to window.__hfAnime")
    if not re.search(r"autoplay\s*:\s*false", html):
        problems.append("animations must be created with autoplay:false")
    for pattern, why in _BANNED:
        if pattern.search(html):
            problems.append(f"the composition {why}")

    allowed = [brief.image_filename] if brief.image_filename else []
    problems.extend(unsafe_constructs(html, allowed_files=allowed, script_src=ANIME_FILENAME))
    if brief.image_filename and brief.image_filename not in html:
        problems.append(f'the owner\'s image "{brief.image_filename}" is not used')
    return problems


# ── The HyperFrames CLI ──────────────────────────────────────────────────


def hyperframes_argv() -> list[str]:
    """The CLI invocation. POCKETPAW_HYPERFRAMES_CMD wins (the image sets
    ``hyperframes``); otherwise a pinned npx spec. On Windows ``npx`` is a .cmd,
    so the executable is resolved through PATH first."""
    raw = (os.environ.get(HYPERFRAMES_CMD_ENV) or "").strip() or DEFAULT_HYPERFRAMES_CMD
    argv = shlex.split(raw, posix=sys.platform != "win32")
    if argv:
        argv[0] = shutil.which(argv[0]) or argv[0]
    return argv


def _cli_env(guard_file: Path | None = None) -> dict[str, str]:
    """An allow-listed environment (no server secrets) with the network guard
    preloaded into every Node process the CLI starts."""
    from pocketpaw_ee.sites.motion_safety import renderer_env

    env = renderer_env()
    env.setdefault("HYPERFRAMES_NO_TELEMETRY", "1")
    env.setdefault("HYPERFRAMES_NO_UPDATE_CHECK", "1")
    env["NO_COLOR"] = "1"
    env["NODE_OPTIONS"] = f'--require "{_NETWORK_GUARD.as_posix()}"'
    if guard_file is not None:
        env[GUARD_FILE_ENV] = str(guard_file)
    return env


def guard_report(guard_file: Path | None) -> tuple[list[int], list[str]]:
    """What the network guard logged: the PIDs of browsers it rewrote, and any
    ``UNGUARDED`` launches it refused."""
    if guard_file is None or not guard_file.is_file():
        return [], []
    pids: list[int] = []
    unguarded: list[str] = []
    for line in guard_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
        elif line.startswith("UNGUARDED"):
            unguarded.append(line)
    return pids, unguarded


def guarded_pids(guard_file: Path | None) -> list[int]:
    """The Chrome PIDs the network guard launched (and so guarded)."""
    return guard_report(guard_file)[0]


_BROWSER_NAME_RE = re.compile(r"chrom|headless", re.IGNORECASE)


async def _capture(argv: list[str], timeout_s: float = 10) -> str:
    """Run a small helper (tasklist / ps / taskkill) without blocking the loop."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout_s)
    except Exception:  # noqa: BLE001 — a helper failing just means "don't know"
        return ""
    return out.decode("utf-8", errors="replace")


async def _is_browser(pid: int) -> bool:
    """Whether ``pid`` is still a Chrome process (guards against PID reuse)."""
    if sys.platform == "win32":
        out = await _capture(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"])
        first = out.strip().split(",", 1)[0].strip('"') if out.strip().startswith('"') else ""
        return bool(_BROWSER_NAME_RE.search(first))
    cmdline = Path(f"/proc/{pid}/cmdline")
    if cmdline.parent.is_dir():
        with contextlib.suppress(OSError):
            exe = cmdline.read_bytes().split(b"\0", 1)[0].decode("utf-8", errors="replace")
            return bool(_BROWSER_NAME_RE.search(Path(exe).name))
        return False
    return bool(_BROWSER_NAME_RE.search(await _capture(["ps", "-p", str(pid), "-o", "comm="])))


async def _kill_guarded_browsers(guard_file: Path | None) -> None:
    """Kill every Chrome the guard logged that is still a Chrome. Puppeteer spawns
    it ``detached`` on POSIX (its own process group), so killing the CLI's group
    misses it."""
    for pid in guarded_pids(guard_file):
        if not await _is_browser(pid):
            continue
        if sys.platform == "win32":
            await _capture(["taskkill", "/F", "/T", "/PID", str(pid)])
        else:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pid, signal.SIGKILL)


async def _run(
    argv: list[str],
    *,
    cwd: str,
    timeout_s: float,
    label: str,
    guard_file: Path | None = None,
) -> tuple[int, str, str]:
    """Run one bounded subprocess. On timeout OR cancellation (the overall
    deadline) kill its process group and any guarded Chrome. Seam for tests."""
    from pocketpaw_ee.sites import generator_client

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=_cli_env(guard_file),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError) as exc:
        raise _unavailable(
            f"`{argv[0]}` could not be started ({exc}). Install Node 22+ and "
            f"set {HYPERFRAMES_CMD_ENV}."
        ) from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
    except (TimeoutError, asyncio.CancelledError) as exc:
        generator_client._kill_process_group(proc)
        await _kill_guarded_browsers(guard_file)
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(proc.wait(), 5)
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise MotionVideoError(
            "failed", f"the {label} step timed out after {timeout_s}s and was stopped"
        ) from exc
    return (
        proc.returncode or 0,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )


_MISSING_TOOL_HINTS = ("chrome", "chromium", "browser", "ffmpeg", "ffprobe", "node.js")


def _tail(text: str, limit: int = 600) -> str:
    return text.strip()[-limit:]


def _parse_json_tail(stdout: str) -> dict | None:
    import json

    start = stdout.find("{")
    if start < 0:
        return None
    try:
        parsed = json.loads(stdout[start:])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


async def lint_project(project_dir: Path) -> list[str]:
    """``hyperframes lint --json``: the error findings, as fix-it lines."""
    rc, out, err = await _run(
        [*hyperframes_argv(), "lint", str(project_dir), "--json"],
        cwd=str(project_dir),
        timeout_s=LINT_TIMEOUT_S,
        label="lint",
    )
    report = _parse_json_tail(out)
    if report is None:
        detail = _tail(err or out) or f"exit {rc}"
        if rc != 0:
            raise _unavailable(f"`hyperframes lint` did not run: {detail}")
        return []
    problems = []
    for finding in report.get("findings") or []:
        if not isinstance(finding, dict) or finding.get("severity") != "error":
            continue
        line = f"{finding.get('code')}: {finding.get('message')}"
        if finding.get("fixHint"):
            line += f" Fix: {finding['fixHint']}"
        problems.append(line)
    return problems


async def render_project(project_dir: Path, out_path: Path, guard_file: Path) -> None:
    """``hyperframes render`` to ``out_path`` (bounded, single worker, guarded).
    Refuses the output when the guard file shows no guarded Chrome."""
    rc, out, err = await _run(
        [
            *hyperframes_argv(),
            "render",
            str(project_dir),
            "--output",
            str(out_path),
            "--fps",
            "30",
            "--crf",
            "20",
            "--workers",
            "1",
            "--quiet",
        ],
        cwd=str(project_dir),
        timeout_s=RENDER_TIMEOUT_S,
        label="render",
        guard_file=guard_file,
    )
    if rc == 0 and out_path.is_file() and out_path.stat().st_size > 0:
        pids, unguarded = guard_report(guard_file)
        if not pids or unguarded:
            out_path.unlink(missing_ok=True)
            raise MotionVideoError(
                "failed",
                "the render ran without the network guard, so its output was discarded",
            )
        return
    detail = _tail(err or out) or f"exit {rc}"
    if any(hint in detail.lower() for hint in _MISSING_TOOL_HINTS):
        raise _unavailable(detail)
    raise MotionVideoError("failed", f"the render failed: {detail}")


def _ffmpeg() -> str | None:
    return os.environ.get("HYPERFRAMES_FFMPEG_PATH") or shutil.which("ffmpeg")


async def _best_effort(argv: list[str], *, cwd: Path, label: str, out: Path) -> bool:
    try:
        rc, _o, _e = await _run(argv, cwd=str(cwd), timeout_s=FFMPEG_TIMEOUT_S, label=label)
    except MotionVideoError:
        logger.warning("motion_video: %s step failed", label, exc_info=True)
        return False
    return rc == 0 and out.is_file() and out.stat().st_size > 0


async def finish_video(project_dir: Path, rendered: Path) -> tuple[bytes, bytes | None]:
    """Remux for the web (+faststart, no audio track) and grab a poster at 0.5s.
    Both are best-effort: the raw render is already a valid MP4."""
    ffmpeg = _ffmpeg()
    video_path = rendered
    poster: bytes | None = None
    if ffmpeg:
        web = project_dir / "web.mp4"
        remux = [ffmpeg, "-y", "-loglevel", "error", "-i", str(rendered), "-an"]
        remux += ["-c:v", "copy", "-movflags", "+faststart", str(web)]
        if await _best_effort(remux, cwd=project_dir, label="remux", out=web):
            video_path = web
        jpg = project_dir / "poster.jpg"
        grab = [ffmpeg, "-y", "-loglevel", "error", "-ss", "0.5", "-i", str(rendered)]
        grab += ["-frames:v", "1", "-q:v", "3", str(jpg)]
        if await _best_effort(grab, cwd=project_dir, label="poster", out=jpg):
            poster = jpg.read_bytes()
    return video_path.read_bytes(), poster


# ── Orchestration ────────────────────────────────────────────────────────

_SLOTS: asyncio.Semaphore | None = None
_IN_FLIGHT: set[str] = set()


def _render_slots() -> asyncio.Semaphore:
    """1-2 concurrent motion jobs per process: each one is a Chrome plus an encoder."""
    global _SLOTS
    if _SLOTS is None:
        try:
            n = int(os.environ.get(CONCURRENCY_ENV) or 1)
        except ValueError:
            n = 1
        _SLOTS = asyncio.Semaphore(max(1, min(n, 2)))
    return _SLOTS


def _write_project(project_dir: Path, html: str) -> None:
    (project_dir / "index.html").write_text(html, encoding="utf-8", newline="\n")


async def render_motion_video(
    *,
    prompt: str,
    aspect_ratio: str = "16:9",
    duration_sec: int = 5,
    palette: list[str] | None = None,
    image: tuple[bytes, str] | None = None,
    workspace_id: str | None = None,
) -> MotionRender:
    """Compose, validate (one repair), and render a motion clip, within the
    per-workspace, per-process and overall limits. Raises ``MotionVideoError``."""
    for vendored in (_ANIME_VENDOR, _NETWORK_GUARD):
        if not vendored.is_file():
            raise _unavailable(f"the vendored {vendored.name} is missing")
    # Fail before the LLM spend when the CLI cannot even be found.
    exe = (hyperframes_argv() or [""])[0]
    if not exe or not (shutil.which(exe) or Path(exe).is_file()):
        raise _unavailable(
            f"`{exe or '(empty)'}` is not on PATH. Install Node 22+ and the pinned "
            f"hyperframes@{HYPERFRAMES_VERSION} CLI, or set {HYPERFRAMES_CMD_ENV}."
        )

    key = workspace_id or ""
    if key in _IN_FLIGHT:
        raise _busy()
    _IN_FLIGHT.add(key)
    try:
        slots = _render_slots()
        try:
            await asyncio.wait_for(slots.acquire(), SLOT_WAIT_S)
        except TimeoutError as exc:
            raise _busy() from exc
        try:
            async with asyncio.timeout(OVERALL_DEADLINE_S):
                return await _compose_and_render(
                    prompt=prompt,
                    aspect_ratio=aspect_ratio,
                    duration_sec=duration_sec,
                    palette=palette,
                    image=image,
                    workspace_id=workspace_id,
                )
        except TimeoutError as exc:
            raise MotionVideoError(
                "failed", f"motion video took longer than {OVERALL_DEADLINE_S}s and was stopped"
            ) from exc
        finally:
            slots.release()
    finally:
        _IN_FLIGHT.discard(key)


async def _compose_and_render(
    *,
    prompt: str,
    aspect_ratio: str,
    duration_sec: int,
    palette: list[str] | None,
    image: tuple[bytes, str] | None,
    workspace_id: str | None,
) -> MotionRender:
    image_filename = None
    if image is not None:
        image_filename = f"source-image.{_IMAGE_EXT.get(image[1], 'jpg')}"
    brief = MotionBrief(
        prompt=prompt,
        aspect_ratio=aspect_ratio if aspect_ratio in DIMENSIONS else "16:9",
        duration_sec=duration_sec,
        palette=[c.strip() for c in (palette or []) if c and c.strip()][:6],
        image_filename=image_filename,
    )

    # The CLI serves ``project/`` to Chrome; the guard file sits OUTSIDE it.
    work_dir = Path(tempfile.mkdtemp(prefix="paw-motion-"))
    project_dir = work_dir / "project"
    project_dir.mkdir()
    guard_file = work_dir / "guard.pids"
    try:
        shutil.copyfile(_ANIME_VENDOR, project_dir / ANIME_FILENAME)
        if image is not None and image_filename:
            (project_dir / image_filename).write_bytes(image[0])

        html = normalize_composition(
            await _ask(workspace_id=workspace_id, user=compose_request(brief))
        )
        _write_project(project_dir, html)
        problems = composition_problems(html, brief) + await lint_project(project_dir)
        if problems:
            logger.info("motion_video: repairing composition: %s", problems)
            html = normalize_composition(
                await _ask(workspace_id=workspace_id, user=repair_request(html, problems))
            )
            _write_project(project_dir, html)
            problems = composition_problems(html, brief) + await lint_project(project_dir)
            if problems:
                raise MotionVideoError(
                    "failed", "the composition did not validate: " + "; ".join(problems[:5])
                )

        rendered = work_dir / "render.mp4"
        await render_project(project_dir, rendered, guard_file)
        video, poster = await finish_video(work_dir, rendered)
        return MotionRender(video=video, poster=poster, html=html)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def decode_data_url(data_url: str) -> tuple[bytes, str]:
    """``data:<mime>;base64,<payload>`` → (bytes, mime)."""
    head, _, payload = data_url.partition(",")
    mime = head[5:].split(";", 1)[0] if head.startswith("data:") else "image/jpeg"
    return base64.b64decode(payload), mime or "image/jpeg"


__all__ = [
    "DEFAULT_HYPERFRAMES_CMD",
    "DIMENSIONS",
    "ENGINE_ID",
    "HYPERFRAMES_VERSION",
    "MotionBrief",
    "MotionRender",
    "MotionVideoError",
    "composition_problems",
    "decode_data_url",
    "extract_html",
    "hyperframes_argv",
    "lint_project",
    "normalize_composition",
    "render_motion_video",
]
