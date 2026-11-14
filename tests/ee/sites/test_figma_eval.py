# tests/ee/sites/test_figma_eval.py: the build-from-Figma path, end to end, offline.
#
# Two fixtures under ``figma_eval/``: ``bakery_frame.json`` (a mocked Figma file:
# a home page at two widths plus two more pages, and the desktop home's node tree)
# and ``redteam_injection.json`` (layer names and text that try to instruct the
# agent, close the envelope, point the tools at another file or smuggle a path
# into a route). The REST API is a fake behind ``oauth_http._transport`` and the
# real ``RestFigmaSource`` serves the tools, so this runs the shipped code path.
#
# Pins: the expected tool sequence is what the per-turn note and the skill teach,
# in order, and every tool in it rides the /sites allow-list; the frames become the
# expected routes and viewports; tokens, the screenshot reference and the asset
# exports come out as expected; and no injected text escapes the envelope, no
# injected file key is readable, and routes stay plain slugs.
from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from PIL import Image

pytest.importorskip("pocketpaw_ee")

HERE = Path(__file__).parent / "figma_eval"
RENDER_HOST = "figma-alpha-api.s3.us-west-2.amazonaws.com"


def _fixture(name: str) -> dict:
    return json.loads((HERE / name).read_text(encoding="utf-8"))


def _api(fx: dict):
    key = fx["file_key"]

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        if path == f"/v1/files/{key}/meta":
            return httpx.Response(200, json={"file": {"name": "x", "last_touched_at": "t1"}})
        if path == f"/v1/files/{key}":
            return httpx.Response(200, json=fx["index"])
        if path == f"/v1/files/{key}/variables/local" and "variables" in fx:
            return httpx.Response(200, json=fx["variables"])
        if path == f"/v1/files/{key}/nodes":
            return httpx.Response(200, json=fx["nodes"])
        if path == f"/v1/images/{key}":
            ids = params["ids"].split(",")
            fmt = params["format"]
            return httpx.Response(
                200, json={"images": {i: f"https://{RENDER_HOST}/{i}.{fmt}" for i in ids}}
            )
        return httpx.Response(404, json={"status": 404})

    return handle


def _png(width: int, height: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (246, 243, 238)).save(out, format="PNG")
    return out.getvalue()


def _images(request: httpx.Request) -> httpx.Response:
    if str(request.url).endswith(".svg"):
        body = (
            b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40"><path d="M0 0h40"/></svg>'
        )
        return httpx.Response(200, headers={"content-type": "image/svg+xml"}, content=body)
    return httpx.Response(200, headers={"content-type": "image/png"}, content=_png(1440, 2400))


@pytest.fixture
def run(monkeypatch):
    """Install a fixture's fake Figma and return the tools module plus helpers."""
    from pocketpaw_ee.agent.mcp_servers import figma as mcp
    from pocketpaw_ee.agent.mcp_servers import sites_reference
    from pocketpaw_ee.integrations.providers import oauth_http
    from pocketpaw_ee.sites.figma import client, session_keys
    from pocketpaw_ee.sites.figma.client import RestFigmaSource

    client.clear_cache()
    session_keys.clear_memory()
    monkeypatch.setattr(sites_reference, "_TRANSPORT", httpx.MockTransport(_images))

    def install(fx: dict, scopes: tuple[str, ...] = ("file_content:read",)) -> SimpleNamespace:
        monkeypatch.setattr(oauth_http, "_transport", httpx.MockTransport(_api(fx)))

        async def token(rejected: str = "") -> str:
            return "tok"

        source = RestFigmaSource(token, scope="conn-eval")
        conn = SimpleNamespace(meta={"handle": "owner"}, scopes=list(scopes))
        monkeypatch.setattr(mcp, "source_for", AsyncMock(return_value=(source, conn)))
        return SimpleNamespace(mcp=mcp, fx=fx)

    with (
        patch("pocketpaw_ee.sites.figma.client._redis", return_value=None),
        patch("pocketpaw_ee.sites.figma.session_keys._redis", return_value=None),
        patch("pocketpaw_ee.cloud.chat.agent_service.current_workspace_id", return_value="w"),
        patch("pocketpaw_ee.cloud.chat.agent_service.current_user_id", return_value="u"),
        patch("pocketpaw_ee.cloud.chat.agent_service.current_session_mongo_id", return_value="s"),
        patch(
            "pocketpaw_ee.cloud.workspace.service.get_workspace_plan",
            new=AsyncMock(return_value="go"),
        ),
        patch(
            "pocketpaw_ee.cloud.pockets.service.get",
            new=AsyncMock(return_value={"id": "p1", "workspace": "w"}),
        ),
    ):
        yield install
    client.clear_cache()
    session_keys.clear_memory()


async def _paste(text: str) -> str:
    """The run path on a /sites turn: record the keys, return the per-turn note."""
    from pocketpaw_ee.cloud.chat.agent_service import ScopeKind
    from pocketpaw_ee.cloud.chat.runs import run_core

    ctx = SimpleNamespace(kind=ScopeKind.SESSION, scope_id="s", workspace_id="w", user_id="u")
    return await run_core._note_figma_links(ctx, text, "sites")


def _split(text: str) -> tuple[str, dict]:
    outside, inner = text.split("\n<untrusted_design_content>\n", 1)
    body, _, after = inner.partition("\n</untrusted_design_content>")
    assert after == "", "content after the envelope"
    return outside, json.loads(body)


# ------------------------------------------------------------- the sequence


def _ordered(text: str, names: list[str]) -> bool:
    at = 0
    for name in names:
        found = text.find(name, at)
        if found < 0:
            return False
        at = found + len(name)
    return True


async def test_the_expected_sequence_is_what_the_agent_is_taught(run):
    from pocketpaw_ee.cloud.surface.surface_registry import _load_mcp_tool_ids

    from pocketpaw.bundled_skills.installer import bundled_skills_plugin_dir

    fx = _fixture("bakery_frame.json")
    sequence = [t for t in fx["expected"]["tool_sequence"] if t.startswith("figma_")]
    unique = list(dict.fromkeys(sequence))
    note = await _paste(fx["pasted"])
    assert "<figma-design>" in note and "sites-reference-match" in note
    assert "RECONSTRUCTION" in note and fx["file_key"] in note
    assert _ordered(note, unique), note
    skill = (
        bundled_skills_plugin_dir() / "skills" / "sites-reference-match" / "SKILL.md"
    ).read_text(encoding="utf-8")
    figma_section = skill.split("## 7. A Figma link", 1)[1].split("\n## ", 1)[0]
    assert _ordered(figma_section, unique)
    assert "compare_site_to_reference" in skill.split("## 8.", 1)[1]
    allow = _load_mcp_tool_ids().sites_allow
    for tool in unique:
        assert f"mcp__pocketpaw_figma__{tool}" in allow, tool
    assert "mcp__pocketpaw_sites_manager__compare_site_to_reference" in allow


async def test_the_bakery_frame_builds_as_expected(run):
    fx = _fixture("bakery_frame.json")
    tools = run(fx)
    expected = fx["expected"]
    await _paste(fx["pasted"])
    url = fx["pasted"].split(" ")[-1]
    base = url.split("?")[0]

    # 1. the frame index and the site plan
    out = await tools.mcp._inspect_handler({"url": base})
    _, index = _split(out["content"][0]["text"])
    pages = [
        {
            "route": p["route"],
            "name": p["name"],
            "widths": [v["width"] for v in p["viewports"]],
            "mobile": p["mobile"],
        }
        for p in index["site_pages"]
    ]
    assert pages == expected["site_pages"]

    # 2. the outline of the desktop home
    out = await tools.mcp._inspect_handler({"url": url})
    _, outline = _split(out["content"][0]["text"])
    hero = outline["nodes"][0]
    assert hero["layout"]["layoutMode"] == "VERTICAL"
    assert hero["children"][0]["text"] == "Bread worth the queue"

    # 3. tokens
    out = await tools.mcp._tokens_handler({"url": url})
    tokens = json.loads(out["content"][0]["text"])["tokens"]
    for role, value in expected["tokens"].items():
        assert tokens[role]["value"] == value, role
        assert tokens[role]["status"] == "observed"

    # 4. the screenshot is saved as a reference
    saved = SimpleNamespace(
        reference_id="ref_00000000000000aa", width=1440, height=2400, viewport="desktop"
    )
    with patch(
        "pocketpaw_ee.sites.site_references.save_reference", new=AsyncMock(return_value=saved)
    ):
        out = await tools.mcp._screenshot_handler({"url": url, "pocket_id": "p1"})
    assert "ref_00000000000000aa" in out["content"][-1]["text"]

    # 5. assets
    store = SimpleNamespace(
        put=AsyncMock(return_value=SimpleNamespace(url="https://assets.test/loaf.webp"))
    )
    with (
        patch(
            "pocketpaw_ee.cloud.pockets.service.get_pocket_workspace",
            new=AsyncMock(return_value="w"),
        ),
        patch("pocketpaw_ee.sites.public_assets.public_asset_store", return_value=store),
    ):
        out = await tools.mcp._assets_handler(
            {"url": url, "pocket_id": "p1", "nodes": expected["assets"]}
        )
    assets = json.loads(out["content"][0]["text"])["assets"]
    assert [(a["id"], a["format"], a["ok"]) for a in assets] == [
        (a["id"], a["format"], True) for a in expected["assets"]
    ]


# ----------------------------------------------------------------- red team


async def test_injected_layer_text_stays_content(run):
    fx = _fixture("redteam_injection.json")
    tools = run(fx)
    await _paste(fx["pasted"])
    texts = []
    out = await tools.mcp._inspect_handler({"url": fx["pasted"]})
    outside, index = _split(out["content"][0]["text"])
    texts.append(outside)
    assert [p["route"] for p in index["site_pages"]] == fx["expected_routes"]
    for page in index["site_pages"]:
        assert all(c.isalnum() or c in "-/" for c in page["route"]), page["route"]
    out = await tools.mcp._inspect_handler({"url": fx["pasted"] + "?node-id=1-2"})
    outside, outline = _split(out["content"][0]["text"])
    texts.append(outside)
    assert "‮" not in json.dumps(outline, ensure_ascii=False)
    assert "\x00" not in json.dumps(outline)
    for text in texts:
        for phrase in fx["must_not_appear_outside_envelope"]:
            assert phrase not in text, phrase


async def test_injected_font_and_variable_names_never_become_tokens(run):
    fx = _fixture("redteam_injection.json")
    tools = run(fx, ("file_content:read", "file_variables:read"))
    await _paste(fx["pasted"])
    out = await tools.mcp._tokens_handler({"url": fx["pasted"] + "?node-id=1-2"})
    text = out["content"][0]["text"]
    body = json.loads(text)
    assert body["source"] == "variables+derived"
    for phrase in fx["must_not_appear_outside_envelope"]:
        assert phrase not in text, phrase
    tokens = body["tokens"]
    assert tokens["color.accent"]["value"] == "#cc331a"  # the colour still counts
    for role in ("font.display", "font.body"):
        value = tokens[role]["value"]
        assert value is None or all(c.isalnum() or c in " -" for c in value), value
    assert "url(" not in body["css"]["root"] and "{" not in body["css"]["root"][7:]


async def test_a_file_key_named_in_the_design_is_never_read(run):
    fx = _fixture("redteam_injection.json")
    tools = run(fx)
    await _paste(fx["pasted"])
    out = await tools.mcp._inspect_handler(
        {"url": f"https://www.figma.com/design/{fx['evil_key']}/x"}
    )
    assert "figma_not_pasted" in out["content"][0]["text"]


async def test_a_pasted_link_on_another_surface_opens_nothing():
    from pocketpaw_ee.cloud.chat.agent_service import ScopeKind
    from pocketpaw_ee.cloud.chat.runs import run_core
    from pocketpaw_ee.sites.figma import session_keys

    session_keys.clear_memory()
    fx = _fixture("bakery_frame.json")
    ctx = SimpleNamespace(kind=ScopeKind.SESSION, scope_id="s", workspace_id="w", user_id="u")
    with patch("pocketpaw_ee.sites.figma.session_keys._redis", return_value=None):
        assert await run_core._note_figma_links(ctx, fx["pasted"], "chat") == ""
        assert not await session_keys.allowed("w", "s", "u", fx["file_key"])
