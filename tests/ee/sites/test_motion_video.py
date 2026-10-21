# tests/ee/sites/test_motion_video.py — the HyperFrames + anime.js video engine.
#
# Unit tests run with the LLM (``motion_video._chat``) and the CLI
# (``motion_video._run`` / ``lint_project`` / ``render_project``) doubled, so
# they need no node, Chrome or proxy. They pin:
#   * the composition contract the renderer depends on (static checks),
#   * the ``motion_safety`` allow-list rejects every SSRF / exfiltration probe a
#     reviewer found, and the renderer env carries no server secrets,
#   * an unguarded render is discarded; busy slots and the deadline fail fast,
#   * exactly ONE repair round-trip on lint/static problems, then a failure,
#   * a hung subprocess is killed as a tree and surfaces as a failure,
#   * a missing CLI is ``unavailable`` (→ 503) BEFORE any LLM spend,
#   * engine auto-selection (fal key present/absent) and the history model id,
#   * the MCP tool schema advertises ``engine`` and ``palette``.
#
# Opt-in integration tests (POCKETPAW_HYPERFRAMES_IT=1) really render through
# the CLI: a 2s MP4 + JPEG poster, and a render whose (unvalidated) composition
# tries to load a local HTTP server, which must receive NO request.

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest
from pocketpaw_ee.sites import motion_video
from pocketpaw_ee.sites.motion_video import MotionBrief, MotionVideoError

_MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
_JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


def _good_html(*, w: int = 1920, h: int = 1080, d: int = 5, image: str | None = None) -> str:
    img = f'<img id="photo" src="{image}" alt="" />' if image else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<script src="anime.min.js"></script>
<style>
  html, body {{ margin: 0; background: #0d1b2a; }}
  #root {{ position: relative; width: 100%; height: 100%; overflow: hidden; background: #0d1b2a; }}
  .blob {{ position: absolute; width: 900px; height: 900px; border-radius: 50%;
          background: #e07a5f; filter: blur(80px); opacity: .5; left: 55%; top: -10%; }}
  #photo {{ position: absolute; inset: -8%; width: 116%; height: 116%; object-fit: cover; }}
</style>
</head>
<body>
<div id="root" data-composition-id="hero" data-start="0"
     data-width="{w}" data-height="{h}" data-duration="{d}" data-no-timeline>
  {img}
  <div class="blob" id="blob"></div>
</div>
<script>
  window.__hfAnime = window.__hfAnime || [];
  window.__hfAnime.push(anime({{
    targets: '#blob', translateX: [0, -220, 0], scale: [1, 1.12, 1],
    duration: {d * 1000}, easing: 'easeInOutSine', autoplay: false
  }}));
</script>
</body>
</html>"""


# ── Static contract ──────────────────────────────────────────────────────


def test_a_good_composition_has_no_problems() -> None:
    assert motion_video.composition_problems(_good_html(), MotionBrief(prompt="x")) == []


def test_extract_html_strips_fences_and_chatter() -> None:
    reply = "Here you go:\n```html\n" + _good_html() + "\n```\nEnjoy!"
    html = motion_video.extract_html(reply)
    assert html.startswith("<!doctype html>") and html.endswith("</html>")


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda h: h.replace("autoplay: false", "autoplay: true"), "autoplay"),
        (
            lambda h: h.replace("scale: [1, 1.12, 1],", "scale: [1, 1 + Math.random(), 1],"),
            "Math.random",
        ),
        (lambda h: h.replace("'#blob',", "'#blob', loop: true,"), "loop:true"),
        (
            lambda h: h.replace(
                "<style>", '<link rel="stylesheet" href="https://fonts.x/a.css"><style>'
            ),
            "<link>",
        ),
        (
            lambda h: h.replace('src="anime.min.js"', 'src="https://cdn.jsdelivr.net/npm/animejs"'),
            "anime.min.js",
        ),
        (lambda h: h.replace('data-width="1920"', 'data-width="1280"'), "data-width"),
        (lambda h: h.replace('data-duration="5"', 'data-duration="9"'), "data-duration"),
        (lambda h: h.replace("window.__hfAnime.push(", "("), "__hfAnime"),
        (lambda h: h.replace("</script>\n</body>", "Date.now();</script>\n</body>"), "clock"),
        (lambda h: h.replace("<div class", "<video src='a.mp4'></video><div class"), "<video>"),
    ],
)
def test_contract_violations_are_reported(mutate, needle) -> None:
    problems = motion_video.composition_problems(mutate(_good_html()), MotionBrief(prompt="x"))
    assert problems, "the violation went unnoticed"
    assert any(needle in p for p in problems), problems


def test_svg_namespaces_are_not_network_urls() -> None:
    html = _good_html().replace(
        '<div class="blob"', '<svg xmlns="http://www.w3.org/2000/svg"></svg><div class="blob"'
    )
    assert motion_video.composition_problems(html, MotionBrief(prompt="x")) == []


def test_the_owner_image_must_be_used_and_dimensions_follow_the_aspect() -> None:
    brief = MotionBrief(prompt="x", aspect_ratio="9:16", image_filename="source-image.png")
    assert any(
        "source-image.png" in p
        for p in motion_video.composition_problems(_good_html(w=1080, h=1920), brief)
    )
    assert (
        motion_video.composition_problems(
            _good_html(w=1080, h=1920, image="source-image.png"), brief
        )
        == []
    )


def test_normalize_adds_data_no_timeline_once() -> None:
    html = _good_html().replace(" data-no-timeline>", ">")
    fixed = motion_video.normalize_composition(html)
    assert fixed.count("data-no-timeline") == 1
    assert motion_video.normalize_composition(fixed) == fixed


def test_the_compose_request_carries_frame_palette_and_skeleton() -> None:
    req = motion_video.compose_request(
        MotionBrief(prompt="aurora", aspect_ratio="1:1", duration_sec=10, palette=["#112233"])
    )
    assert 'data-width="1080" data-height="1080" data-duration="10"' in req
    assert "#112233" in req and "10000 ms" in req and "anime.min.js" in req


def test_the_cli_command_is_pinned_and_overridable(monkeypatch) -> None:
    monkeypatch.delenv(motion_video.HYPERFRAMES_CMD_ENV, raising=False)
    assert f"hyperframes@{motion_video.HYPERFRAMES_VERSION}" in motion_video.hyperframes_argv()
    assert "latest" not in motion_video.DEFAULT_HYPERFRAMES_CMD
    monkeypatch.setenv(motion_video.HYPERFRAMES_CMD_ENV, "hyperframes")
    assert Path(motion_video.hyperframes_argv()[0]).stem.lower() == "hyperframes"


# ── Orchestration: compose → lint → one repair → render ──────────────────


def _wire_pipeline(monkeypatch, *, replies: list[str], lint_rounds: list[list[str]]):
    """Double the LLM and the CLI. Returns the call log."""
    calls: dict[str, list] = {"chat": [], "lint": [], "render": []}
    monkeypatch.setattr(motion_video, "hyperframes_argv", lambda: [__file__])

    async def _chat(*, workspace_id, system, user):
        calls["chat"].append(user)
        return replies[len(calls["chat"]) - 1]

    async def _lint(project_dir: Path):
        calls["lint"].append((project_dir / "index.html").read_text(encoding="utf-8"))
        assert (project_dir / "anime.min.js").is_file(), "anime.js must be vendored in"
        return lint_rounds[len(calls["lint"]) - 1]

    async def _render(project_dir: Path, out: Path, guard_file: Path):
        calls["render"].append(project_dir)
        out.write_bytes(_MP4)

    async def _finish(project_dir: Path, rendered: Path):
        return rendered.read_bytes(), _JPG

    monkeypatch.setattr(motion_video, "_chat", _chat)
    monkeypatch.setattr(motion_video, "lint_project", _lint)
    monkeypatch.setattr(motion_video, "render_project", _render)
    monkeypatch.setattr(motion_video, "finish_video", _finish)
    return calls


@pytest.mark.asyncio
async def test_a_clean_composition_renders_without_a_repair(monkeypatch) -> None:
    calls = _wire_pipeline(monkeypatch, replies=[_good_html()], lint_rounds=[[]])

    clip = await motion_video.render_motion_video(prompt="aurora", workspace_id="ws1")

    assert clip.video == _MP4 and clip.poster == _JPG
    assert len(calls["chat"]) == 1 and len(calls["render"]) == 1
    assert not calls["render"][0].exists(), "the temp project dir must be removed"


@pytest.mark.asyncio
async def test_lint_errors_get_exactly_one_repair_round_trip(monkeypatch) -> None:
    calls = _wire_pipeline(
        monkeypatch,
        replies=[_good_html(), _good_html()],
        lint_rounds=[["gsap_css_transform_conflict: transform fights the tween"], []],
    )

    await motion_video.render_motion_video(prompt="aurora")

    assert len(calls["chat"]) == 2
    assert "gsap_css_transform_conflict" in calls["chat"][1], "the repair must carry lint output"
    assert len(calls["render"]) == 1


@pytest.mark.asyncio
async def test_still_broken_after_the_repair_fails_without_rendering(monkeypatch) -> None:
    bad = _good_html().replace("autoplay: false", "autoplay: true")
    calls = _wire_pipeline(monkeypatch, replies=[bad, bad], lint_rounds=[[], []])

    with pytest.raises(MotionVideoError) as exc:
        await motion_video.render_motion_video(prompt="aurora")

    assert exc.value.reason == "failed" and "autoplay" in exc.value.message
    assert len(calls["chat"]) == 2, "one repair, not a loop"
    assert calls["render"] == []


@pytest.mark.asyncio
async def test_a_missing_cli_is_unavailable_before_any_llm_spend(monkeypatch) -> None:
    monkeypatch.setenv(motion_video.HYPERFRAMES_CMD_ENV, "definitely-not-a-real-hf-binary")

    async def _chat(**_kw):
        raise AssertionError("no LLM call when the renderer is missing")

    monkeypatch.setattr(motion_video, "_chat", _chat)
    with pytest.raises(MotionVideoError) as exc:
        await motion_video.render_motion_video(prompt="aurora")

    assert exc.value.reason == "unavailable"
    assert exc.value.message.startswith("Motion video needs the HyperFrames renderer")


@pytest.mark.asyncio
async def test_a_render_failure_naming_chrome_is_unavailable(monkeypatch, tmp_path) -> None:
    async def _run(argv, *, cwd, timeout_s, label, **_kw):
        return 1, "", "Error: Could not find Chrome (ver. 131). Run hyperframes browser ensure"

    monkeypatch.setattr(motion_video, "_run", _run)
    with pytest.raises(MotionVideoError) as exc:
        await motion_video.render_project(tmp_path, tmp_path / "out.mp4", tmp_path / "g")
    assert exc.value.reason == "unavailable" and "Chrome" in exc.value.message


@pytest.mark.asyncio
async def test_a_hung_render_is_killed_on_timeout(monkeypatch, tmp_path) -> None:
    """The subprocess tree is killed and the caller gets a failure, not a hang."""
    from pocketpaw_ee.sites import generator_client

    killed: list[int] = []

    class _HungProc:
        pid = 4242
        returncode = None

        async def communicate(self):
            await asyncio.sleep(3600)

        async def wait(self):
            return -9

        def kill(self):
            killed.append(self.pid)

    async def _spawn(*_a, **_kw):
        return _HungProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    monkeypatch.setattr(generator_client, "_kill_process_group", lambda p: killed.append(p.pid))
    monkeypatch.setattr(motion_video, "RENDER_TIMEOUT_S", 0.05)

    guard = tmp_path / "guard.pids"
    guard.write_text("31337\n")
    browsers: list = []

    async def _kill_browsers(g):
        browsers.append(g)

    monkeypatch.setattr(motion_video, "_kill_guarded_browsers", _kill_browsers)
    with pytest.raises(MotionVideoError) as exc:
        await motion_video.render_project(tmp_path, tmp_path / "out.mp4", guard)

    assert exc.value.reason == "failed" and "timed out" in exc.value.message
    assert killed and killed[0] == 4242, "the CLI's process group is killed"
    assert browsers == [guard], "and so is every guarded (detached) Chrome"


@pytest.mark.asyncio
async def test_lint_reads_error_findings_from_json(monkeypatch, tmp_path) -> None:
    report = (
        '{"ok": false, "findings": ['
        '{"code": "missing_timeline_registry", "severity": "error",'
        ' "message": "m", "fixHint": "f"},'
        '{"code": "studio_missing_editable_id", "severity": "warning", "message": "w"}]}'
    )

    async def _run(argv, *, cwd, timeout_s, label, **_kw):
        assert "lint" in argv and "--json" in argv
        return 1, report, ""

    monkeypatch.setattr(motion_video, "_run", _run)
    assert await motion_video.lint_project(tmp_path) == ["missing_timeline_registry: m Fix: f"]


# ── Safety: the allow-list, the env, the guard, the limits ───────────────

_SCRIPT_TAIL = "</script>\n</body>"


def _with_body(markup: str) -> str:
    return _good_html().replace('<div class="blob"', markup + '<div class="blob"')


def _with_js(js: str) -> str:
    return _good_html().replace(_SCRIPT_TAIL, js + "\n" + _SCRIPT_TAIL)


def _with_css(css: str) -> str:
    return _good_html().replace("</style>", css + "\n</style>")


PROBES = {
    # the reviewer's probes
    "metadata ip": _with_body('<img src="http://169.254.169.254/latest/meta-data/">'),
    "object to api": _with_body('<object data="http://127.0.0.1:8888/api/v1/health"></object>'),
    "embed localhost": _with_body('<embed src="http://localhost:8888/">'),
    "file url": _with_body('<img src="file:///etc/passwd">'),
    "bracket fetch": _with_js('window["fe"+"tch"]("http://x/");'),
    "image src concat": _with_js('new Image().src="https://"+"evil.example/x";'),
    # more of the same family
    "protocol relative img": _with_body('<img src="//evil.example/x.png">'),
    "data img": _with_body('<img src="data:image/png;base64,AAAA">'),
    "img not provided": _with_body('<img src="secret.png">'),
    "link": _with_body('<link rel="stylesheet" href="x.css">'),
    "base": _with_body('<base href="http://evil.example/">'),
    "meta refresh": _with_body('<meta http-equiv="refresh" content="0;url=http://x">'),
    "iframe srcdoc": _with_body('<iframe srcdoc="<img src=x>"></iframe>'),
    "svg use": _with_body('<svg><use href="http://evil.example/s.svg#a"></use></svg>'),
    "svg image": _with_body('<svg><image href="http://evil.example/a.png"></image></svg>'),
    "foreignobject": _with_body("<svg><foreignObject><div></div></foreignObject></svg>"),
    "event handler": _with_body('<div onclick="x()"></div>'),
    "anchor": _with_body('<a href="http://x">x</a>'),
    "svg fill url": _with_body('<svg><rect fill="url(http://evil.example/p)"></rect></svg>'),
    "style attr url": _with_body('<div style="background:url(http://evil.example/a)"></div>'),
    "css url": _with_css(".x{background:url('https://evil.example/a.png')}"),
    "css protocol relative": _with_css(".x{background:url(//evil.example/a.png)}"),
    "css import": _with_css("@import 'x.css';"),
    "css font face": _with_css("@font-face{font-family:A;src:url(a.woff)}"),
    "css escape": _with_css(".x{background:\\75 rl(x)}"),
    "module script": _good_html().replace("<script>\n", '<script type="module">\n'),
    "second script": _good_html().replace(
        '<script src="anime.min.js"></script>',
        '<script src="anime.min.js"></script><script src="x.js"></script>',
    ),
    "self bracket": _with_js('self["fet"+"ch"]("x");'),
    "template access": _with_js("window[`fetch`]('x');"),
    "location": _with_js("location = 'x';"),
    "eval atob": _with_js("eval(atob('Zm9v'));"),
    "function ctor": _with_js("(0).constructor.constructor('return 1')();"),
    "cookie": _with_js("const c = document.cookie;"),
    "dynamic import": _with_js("import('x');"),
    "beacon": _with_js("navigator.sendBeacon('x', 'y');"),
    "websocket": _with_js("new WebSocket('ws://x');"),
    "xhr": _with_js("new XMLHttpRequest();"),
    "set src attr": _with_js("document.body.setAttribute('src', 'x');"),
    "dynamic set attr": _with_js("const k = 'sr'; document.body.setAttribute(k, 'x');"),
    "create script": _with_js("document.createElement('script');"),
    "dynamic create": _with_js("const t = 'img'; document.createElement(t);"),
    "inner html": _with_js("document.body.innerHTML = '<img>';"),
    "css text": _with_js("document.body.style.cssText = 'x';"),
    "literal concat": _with_js("const u = 'ht' + 'tp';"),
    "worker": _with_js("new Worker('w.js');"),
    "globalthis": _with_js("globalThis.x = 1;"),
    # second review: parser-differential and JS-shallowness probes
    "svg style img onerror": _with_body("<svg><style><img src=x onerror=fetch(1)></style></svg>"),
    "svg style script": _with_body("<svg><style><script>fetch(1)</script></style></svg>"),
    "svg script": _with_body("<svg><script>1</script></svg>"),
    "markup in css": _with_css(".x{color:red}</style><img src=x>"),
    "script double escape": _good_html().replace(
        _SCRIPT_TAIL, "</script><script><!--<script></script>\nfetch(1)\n" + _SCRIPT_TAIL
    ),
    "comment close in js": _with_js("let a = 1; -->"),
    "script text in body": _with_body("<div>&lt;script&gt;</div>"),
    "constructor bare": _with_js("const C = [].filter['constru' + 'ctor'];"),
    "constructor literal": _with_js("const F = (()=>1)['constructor'];"),
    "object assign": _with_js("Object.assign(document.body.style, {background: x});"),
    "define property": _with_js("Object.defineProperty(window, 'x', {});"),
    "join": _with_js("const n = ['fe', 'tch'].join('');"),
    "concat": _with_js("const n = 'fe'.concat('tch');"),
    "repeat": _with_js("const n = 'a'.repeat(3);"),
}


@pytest.mark.parametrize("name", sorted(PROBES))
def test_every_probe_is_rejected(name: str) -> None:
    problems = motion_video.composition_problems(PROBES[name], MotionBrief(prompt="x"))
    assert problems, f"{name}: passed the validator"


def test_what_a_good_composition_needs_still_passes() -> None:
    """The allow-list must not reject the normal toolbox: a provided image, SVG
    gradients by fragment, and building divs with literal tags and attributes."""
    html = _good_html(image="source-image.png").replace(
        '<div class="blob"',
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><defs>'
        '<linearGradient id="g"><stop offset="0" stop-color="#fff"/></linearGradient>'
        '</defs><rect width="10" height="10" fill="url(#g)"/></svg><div class="blob"',
    )
    html = html.replace(
        _SCRIPT_TAIL,
        "for (let i = 0; i < 20; i++) { const d = document.createElement('div');"
        " d.setAttribute('class', 'dot'); d.style.left = (rand() * 100) + '%';"
        " document.getElementById('root').appendChild(d); }\n" + _SCRIPT_TAIL,
    )
    brief = MotionBrief(prompt="x", image_filename="source-image.png")
    assert motion_video.composition_problems(html, brief) == []


def test_the_renderer_env_is_an_allow_list(monkeypatch) -> None:
    from pocketpaw_ee.sites.motion_safety import renderer_env

    env = renderer_env(
        {
            "Path": "/usr/bin",
            "HOME": "/home/p",
            "LANG": "C.UTF-8",
            "HYPERFRAMES_FFMPEG_PATH": "/ffmpeg",
            "HYPERFRAMES_BROWSER_PATH": "/attacker/chrome",
            "AWS_SECRET_ACCESS_KEY": "s1",
            "FAL_AI_API_KEY": "s2",
            "AUTH_SECRET": "s3",
            "LITELLM_MASTER_KEY": "s4",
            "MONGODB_URI": "mongodb://u:p@h",
            "NODE_OPTIONS": "--require /evil.js",
            "HYPERFRAMES_BROWSER_PATH_X": "kept, not an override",
            "PRODUCER_HEADLESS_SHELL_PATH": "/attacker/chrome",
        }
    )
    env.pop("HYPERFRAMES_BROWSER_PATH_X")
    assert env == {
        "Path": "/usr/bin",
        "HOME": "/home/p",
        "LANG": "C.UTF-8",
        "HYPERFRAMES_FFMPEG_PATH": "/ffmpeg",
    }, "secrets and browser-path overrides must not reach the CLI"

    monkeypatch.setenv("FAL_AI_API_KEY", "planted-secret")
    monkeypatch.setenv("NODE_OPTIONS", "--require /evil.js")
    cli = motion_video._cli_env(Path("/tmp/guard.pids"))
    assert "planted-secret" not in cli.values()
    assert "hf-network-guard.cjs" in cli["NODE_OPTIONS"] and "evil" not in cli["NODE_OPTIONS"]
    assert cli[motion_video.GUARD_FILE_ENV] == str(Path("/tmp/guard.pids"))


@pytest.mark.asyncio
async def test_an_unguarded_render_is_discarded(monkeypatch, tmp_path) -> None:
    """If the guard did not log a Chrome, the browser ran with network access:
    the output must not be published."""

    async def _run(argv, *, cwd, timeout_s, label, guard_file=None):
        Path(argv[argv.index("--output") + 1]).write_bytes(_MP4)
        return 0, "", ""

    monkeypatch.setattr(motion_video, "_run", _run)
    out = tmp_path / "out.mp4"
    with pytest.raises(MotionVideoError) as exc:
        await motion_video.render_project(tmp_path, out, tmp_path / "guard.pids")
    assert "network guard" in exc.value.message
    assert not out.exists()

    (tmp_path / "guard.pids").write_text("1234\nUNGUARDED chromium-browser\n")
    with pytest.raises(MotionVideoError) as exc:
        await motion_video.render_project(tmp_path, out, tmp_path / "guard.pids")
    assert "network guard" in exc.value.message, "ANY unguarded launch discards the output"

    (tmp_path / "guard.pids").write_text("1234\nSYNC chrome\n")
    await motion_video.render_project(tmp_path, out, tmp_path / "guard.pids")
    assert out.read_bytes() == _MP4


@pytest.mark.asyncio
async def test_no_free_slot_is_busy_fast(monkeypatch) -> None:
    calls = _wire_pipeline(monkeypatch, replies=[_good_html()], lint_rounds=[[]])
    slots = asyncio.Semaphore(1)
    await slots.acquire()  # someone else is rendering
    monkeypatch.setattr(motion_video, "_SLOTS", slots)
    monkeypatch.setattr(motion_video, "SLOT_WAIT_S", 0.05)

    with pytest.raises(MotionVideoError) as exc:
        await motion_video.render_motion_video(prompt="x", workspace_id="ws-busy")

    assert exc.value.reason == "busy" and "try again" in exc.value.message
    assert calls["chat"] == [], "no LLM spend while waiting for a slot"
    assert "ws-busy" not in motion_video._IN_FLIGHT


@pytest.mark.asyncio
async def test_one_motion_job_per_workspace(monkeypatch) -> None:
    _wire_pipeline(monkeypatch, replies=[_good_html()], lint_rounds=[[]])
    monkeypatch.setattr(motion_video, "_IN_FLIGHT", {"ws1"})
    with pytest.raises(MotionVideoError) as exc:
        await motion_video.render_motion_video(prompt="x", workspace_id="ws1")
    assert exc.value.reason == "busy"


@pytest.mark.asyncio
async def test_the_overall_deadline_stops_the_job(monkeypatch) -> None:
    _wire_pipeline(monkeypatch, replies=[_good_html()], lint_rounds=[[]])
    monkeypatch.setattr(motion_video, "_SLOTS", asyncio.Semaphore(1))

    async def _slow_chat(**_kw):
        await asyncio.sleep(3600)

    monkeypatch.setattr(motion_video, "_chat", _slow_chat)
    monkeypatch.setattr(motion_video, "OVERALL_DEADLINE_S", 0.05)
    with pytest.raises(MotionVideoError) as exc:
        await motion_video.render_motion_video(prompt="x", workspace_id="ws-slow")
    assert exc.value.reason == "failed" and "longer than" in exc.value.message
    assert motion_video._SLOTS._value == 1, "the slot is released"


def _fake_chrome(tmp_path: Path) -> Path | None:
    """A copy of node named like a browser. Callers end its argv with ``--`` so
    the guard's appended Chrome flags land in ``process.argv``."""
    node = shutil.which("node")
    if node is None:
        return None
    fake = tmp_path / ("chromium-browser.exe" if os.name == "nt" else "chromium-browser")
    shutil.copyfile(node, fake)
    fake.chmod(0o755)
    return fake


async def _node_with_guard(script: str, guard_file: Path) -> str:
    env = {
        **motion_video._cli_env(guard_file),
        "FAKE_CHROME": "",
    }
    proc = await asyncio.create_subprocess_exec(
        shutil.which("node"),
        "-e",
        script,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), 60)
    assert proc.returncode == 0, err.decode()
    return out.decode()


@pytest.mark.asyncio
async def test_the_guard_rewrites_browser_launches_and_tracks_live_ports(tmp_path) -> None:
    """Every browser launch gets the dead proxy; only ports still listening
    when Chrome starts are bypassed; the PID is logged."""
    fake = _fake_chrome(tmp_path)
    if fake is None:
        pytest.skip("node is not installed")
    guard = tmp_path / "guard.pids"
    script = f"""
const net = require('net'); const cp = require('child_process');
const closed = net.createServer(); const open = net.createServer();
closed.listen(0, '127.0.0.1', () => {{
  const closedPort = closed.address().port;
  closed.close(() => open.listen(0, '127.0.0.1', () => {{
    const child = cp.spawn({json.dumps(str(fake))},
      ['-e', 'process.stdout.write(JSON.stringify(process.argv))', '--', '--proxy-server=http://evil:1']);
    let out = ''; child.stdout.on('data', d => out += d);
    child.on('close', () => {{
      process.stdout.write(JSON.stringify({{argv: JSON.parse(out), closedPort,
        openPort: open.address().port}}));
      open.close();
    }});
  }}));
}});
"""
    report = json.loads(await _node_with_guard(script, guard))
    argv = report["argv"]
    assert "--proxy-server=http://127.0.0.1:9" in argv
    assert "--proxy-server=http://evil:1" not in argv, "a caller's proxy flag is stripped"
    bypass = next(a for a in argv if a.startswith("--proxy-bypass-list="))
    assert "<-loopback>" in bypass
    assert f"localhost:{report['openPort']}" in bypass
    assert f"localhost:{report['closedPort']}" not in bypass, "a closed port is not bypassed"
    assert any(a.startswith("--host-resolver-rules=MAP * ~NOTFOUND") for a in argv)
    pids, unguarded = motion_video.guard_report(guard)
    assert len(pids) == 1 and unguarded == []


@pytest.mark.asyncio
async def test_the_guard_refuses_a_launch_it_cannot_rewrite(tmp_path) -> None:
    """A browser launched without an argv array (or through a shell string) is
    refused and logged UNGUARDED, which discards the render."""
    fake = _fake_chrome(tmp_path)
    if fake is None:
        pytest.skip("node is not installed")
    guard = tmp_path / "guard.pids"
    script = f"""
const cp = require('child_process'); const results = [];
for (const launch of [
  () => cp.spawn({json.dumps(str(fake))}, {{stdio: 'ignore'}}),
  () => cp.execSync('"' + {json.dumps(str(fake))} + '" --version'),
  () => cp.execFileSync({json.dumps(str(fake))}, ['-e', '1', '--']),
]) {{
  try {{ launch(); results.push('ran'); }}
  catch (e) {{ results.push(String(e.message).slice(0, 40)); }}
}}
process.stdout.write(JSON.stringify(results));
"""
    results = json.loads(await _node_with_guard(script, guard))
    assert results[0].startswith("hf-network-guard") and results[1].startswith("hf-network-guard")
    assert results[2] == "ran", "a rewritable sync launch still runs"
    lines = guard.read_text().splitlines()
    assert sum(line.startswith("UNGUARDED") for line in lines) == 2
    assert "SYNC chromium-browser" in "\n".join(lines)


@pytest.mark.asyncio
async def test_a_reused_pid_is_not_killed(tmp_path) -> None:
    """The guard file may name a PID that has since been reused. Only a process
    that is still a browser is killed: this test's own PID must survive."""
    guard = tmp_path / "guard.pids"
    guard.write_text(f"{os.getpid()}\n")
    assert await motion_video._is_browser(os.getpid()) is False
    await motion_video._kill_guarded_browsers(guard)  # we are still alive to assert


# ── Engine selection in media_generation ─────────────────────────────────


def test_auto_picks_fal_with_a_key_and_motion_without(monkeypatch) -> None:
    from pocketpaw_ee.sites.media_generation import SiteMediaError, resolve_video_engine

    monkeypatch.setattr("pocketpaw_ee.cloud.studio.fal_edit.fal_api_key", lambda: "k")
    assert resolve_video_engine("auto") == "generative"
    assert resolve_video_engine(None) == "generative"
    assert resolve_video_engine("motion") == "motion"

    monkeypatch.setattr("pocketpaw_ee.cloud.studio.fal_edit.fal_api_key", lambda: None)
    assert resolve_video_engine("auto") == "motion"
    with pytest.raises(SiteMediaError) as exc:
        resolve_video_engine("generative")
    assert exc.value.reason == "unavailable"
    with pytest.raises(SiteMediaError):
        resolve_video_engine("sora")


@pytest.mark.asyncio
async def test_no_fal_key_routes_the_tool_to_motion_and_records_its_model(monkeypatch) -> None:
    """The deployment this exists for: no fal key, video still works, and the
    Studio row names the HyperFrames engine."""
    import json

    from pocketpaw_ee.agent.mcp_servers import site_media

    from tests.ee.sites.test_site_video_tool import FakeStore, _patch_history, _patch_owner

    store = FakeStore()
    monkeypatch.setattr(site_media, "_identity", lambda: ("ws1", "u1"))
    _patch_owner(monkeypatch, "ws1")
    monkeypatch.setattr(
        "pocketpaw_ee.sites.public_assets.public_asset_store", lambda: store, raising=False
    )
    monkeypatch.setattr("pocketpaw_ee.cloud.studio.fal_edit.fal_api_key", lambda: None)
    recorded = _patch_history(monkeypatch)

    async def _no_fal(**_kw):
        raise AssertionError("fal must not run without a key")

    monkeypatch.setattr("pocketpaw_ee.cloud.studio.fal_video.run_fal_video", _no_fal)
    seen: dict = {}

    async def _render(**kwargs):
        seen.update(kwargs)
        return motion_video.MotionRender(video=_MP4, poster=_JPG, html="")

    monkeypatch.setattr(motion_video, "render_motion_video", _render)

    resp = await site_media._generate_site_video_handler(
        {"pocket_id": "pk1", "prompt": "aurora", "palette": ["#0d1b2a", "#e07a5f"]}
    )
    body = json.loads(resp["content"][0]["text"])

    assert body["ok"] is True and body["engine"] == "motion"
    assert body["poster_url"] and "loop" in body["message"]
    assert seen["palette"] == ["#0d1b2a", "#e07a5f"]
    assert [r["status"] for r in recorded] == ["running", "succeeded"]
    assert recorded[-1]["gen"].model == motion_video.ENGINE_ID == "hyperframes-animejs"


@pytest.mark.asyncio
async def test_a_motion_failure_moves_the_row_and_keeps_its_reason(monkeypatch) -> None:
    from pocketpaw_ee.sites.media_generation import SiteMediaError, generate_site_video_asset

    from tests.ee.sites.test_site_video_tool import FakeStore, _patch_history, _patch_owner

    _patch_owner(monkeypatch, "ws1")
    monkeypatch.setattr(
        "pocketpaw_ee.sites.public_assets.public_asset_store", lambda: FakeStore(), raising=False
    )
    recorded = _patch_history(monkeypatch)

    async def _render(**_kw):
        raise MotionVideoError("unavailable", "Motion video needs the HyperFrames renderer: x")

    monkeypatch.setattr(motion_video, "render_motion_video", _render)
    with pytest.raises(SiteMediaError) as exc:
        await generate_site_video_asset(
            workspace_id="ws1", pocket_id="pk1", prompt="x", engine="motion"
        )
    assert exc.value.reason == "unavailable"
    assert [r["status"] for r in recorded] == ["running", "failed"]


# ── The MCP schema ───────────────────────────────────────────────────────


def test_the_mcp_tool_schema_offers_engine_and_palette(monkeypatch) -> None:
    pytest.importorskip("claude_agent_sdk")
    import claude_agent_sdk
    from pocketpaw_ee.agent.mcp_servers import site_media

    captured: dict = {}

    def _server(*, name, tools, **_kw):
        captured["tools"] = tools
        return object()

    monkeypatch.setattr(claude_agent_sdk, "create_sdk_mcp_server", _server)
    site_media.build_site_media_server()

    video = next(t for t in captured["tools"] if t.name == "generate_site_video")
    props = video.input_schema["properties"]
    assert props["engine"]["enum"] == ["auto", "motion", "generative"]
    assert "hero background" in props["engine"]["description"]
    assert props["palette"]["type"] == "array"
    assert video.input_schema["additionalProperties"] is False
    assert "motion" in video.description and "fal" in video.description


# ── Opt-in: a real render ────────────────────────────────────────────────


@pytest.mark.skipif(
    os.environ.get("POCKETPAW_HYPERFRAMES_IT") != "1",
    reason="real HyperFrames render; set POCKETPAW_HYPERFRAMES_IT=1 (needs node, Chrome, ffmpeg)",
)
@pytest.mark.asyncio
async def test_integration_renders_a_real_two_second_mp4(monkeypatch) -> None:
    """Everything real except the LLM: lint, render, remux and poster."""
    html = _good_html(d=2)

    async def _chat(**_kw):
        return html

    monkeypatch.setattr(motion_video, "_chat", _chat)
    clip = await motion_video.render_motion_video(prompt="test", duration_sec=2)

    assert clip.video[4:8] == b"ftyp", "not an MP4"
    assert clip.poster and clip.poster[:3] == b"\xff\xd8\xff", "poster is not a JPEG"

    out = (
        Path(os.environ.get("POCKETPAW_HYPERFRAMES_IT_OUT") or "")
        if os.environ.get("POCKETPAW_HYPERFRAMES_IT_OUT")
        else None
    )
    if out is not None:
        out.write_bytes(clip.video)
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        proc = await asyncio.create_subprocess_exec(
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate(clip.video)
        assert abs(float(stdout.decode().strip()) - 2.0) < 0.2


@pytest.mark.skipif(
    os.environ.get("POCKETPAW_HYPERFRAMES_IT") != "1",
    reason="real HyperFrames render; set POCKETPAW_HYPERFRAMES_IT=1 (needs node, Chrome, ffmpeg)",
)
@pytest.mark.asyncio
async def test_integration_the_render_browser_cannot_reach_the_network(
    monkeypatch, tmp_path
) -> None:
    """Defence in depth, layer 1 alone: the composition skips the validator and
    tries to load a local HTTP server by IP literal, by localhost, and from JS.
    The render must succeed (the CLI's own file server still works) and the
    server must see NO request."""
    import http.server
    import threading

    hits: list[str] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        html = _good_html(d=2).replace(
            '<div class="blob"',
            f'<img src="http://127.0.0.1:{port}/ip.png">'
            f'<img src="http://localhost:{port}/localhost.png">'
            f'<div style="background:url(http://127.0.0.1:{port}/css.png)"></div>'
            '<div class="blob"',
        )
        html = html.replace(
            _SCRIPT_TAIL,
            f"fetch('http://127.0.0.1:{port}/fetch').catch(function(){{}});\n" + _SCRIPT_TAIL,
        )
        project = tmp_path / "project"
        project.mkdir()
        (project / "index.html").write_text(html, encoding="utf-8")
        shutil.copyfile(motion_video._ANIME_VENDOR, project / motion_video.ANIME_FILENAME)
        guard = tmp_path / "guard.pids"
        out = tmp_path / "out.mp4"

        await motion_video.render_project(project, out, guard)

        assert out.stat().st_size > 0
        assert motion_video.guarded_pids(guard), "the guard must have wrapped Chrome"
        assert hits == [], f"the render browser reached the network: {hits}"
    finally:
        server.shutdown()
