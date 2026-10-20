# tests/ee/sites/test_motion_video.py — the HyperFrames + anime.js video engine.
#
# Unit tests run with the LLM (``motion_video._chat``) and the CLI
# (``motion_video._run`` / ``lint_project`` / ``render_project``) doubled, so
# they need no node, Chrome or proxy. They pin:
#   * the composition contract the renderer depends on (static checks),
#   * exactly ONE repair round-trip on lint/static problems, then a failure,
#   * a hung subprocess is killed as a tree and surfaces as a failure,
#   * a missing CLI is ``unavailable`` (→ 503) BEFORE any LLM spend,
#   * engine auto-selection (fal key present/absent) and the history model id,
#   * the MCP tool schema advertises ``engine`` and ``palette``.
#
# One opt-in integration test (POCKETPAW_HYPERFRAMES_IT=1) really renders a 2s
# composition through the CLI and checks a playable MP4 and a JPEG poster.

from __future__ import annotations

import asyncio
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
            "stylesheet",
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

    async def _render(project_dir: Path, out: Path):
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
    async def _run(argv, *, cwd, timeout_s, label):
        return 1, "", "Error: Could not find Chrome (ver. 131). Run hyperframes browser ensure"

    monkeypatch.setattr(motion_video, "_run", _run)
    with pytest.raises(MotionVideoError) as exc:
        await motion_video.render_project(tmp_path, tmp_path / "out.mp4")
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

    with pytest.raises(MotionVideoError) as exc:
        await motion_video.render_project(tmp_path, tmp_path / "out.mp4")

    assert exc.value.reason == "failed" and "timed out" in exc.value.message
    assert killed and killed[0] == 4242


@pytest.mark.asyncio
async def test_lint_reads_error_findings_from_json(monkeypatch, tmp_path) -> None:
    report = (
        '{"ok": false, "findings": ['
        '{"code": "missing_timeline_registry", "severity": "error",'
        ' "message": "m", "fixHint": "f"},'
        '{"code": "studio_missing_editable_id", "severity": "warning", "message": "w"}]}'
    )

    async def _run(argv, *, cwd, timeout_s, label):
        assert "lint" in argv and "--json" in argv
        return 1, report, ""

    monkeypatch.setattr(motion_video, "_run", _run)
    assert await motion_video.lint_project(tmp_path) == ["missing_timeline_registry: m Fix: f"]


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
