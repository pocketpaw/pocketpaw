# tests/ee/sites/test_figma_call_budget.py: how many tier-1 Figma reads an import costs.
#
# The real ``RestFigmaSource`` serves the real tools against a call-counting fake of
# the REST API (behind ``oauth_http._transport``) and of Figma's image hosts (behind
# ``sites_reference._TRANSPORT``), with the private body store swapped for an
# in-memory one. Each tool call gets a fresh source, as in production, so whatever
# a later call reuses came through the persistent cache.
#
# Pins: a link with a node-id costs exactly 2 tier-1 reads (one ``/nodes``, one
# batched PNG render), 3 with icons (one SVG call), a bare file link 1 more (the
# index), and a second import of an unchanged version 0. The cache keys on the
# ``/meta`` version, not ``last_touched_at``, and is shared across members only
# after the caller's own ``/meta`` succeeds. A metered account is told what the
# import used and roughly what is left this month; the month count resets.
from __future__ import annotations

import io
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from PIL import Image

pytest.importorskip("pocketpaw_ee")

KEY = "AbCdEf1234567890"
RENDER_HOST = "figma-alpha-api.s3.us-west-2.amazonaws.com"
FILL_HOST = "s3-alpha-sig.figma.com"
REF = "a1b2c3d4e5f60718293a"
TIER1 = {f"/v1/files/{KEY}", f"/v1/files/{KEY}/nodes", f"/v1/images/{KEY}"}
FRAME_URL = f"https://www.figma.com/design/{KEY}/Site?node-id=1-2"
BARE_URL = f"https://www.figma.com/design/{KEY}/Site"


def _png(width: int = 1440, height: int = 900) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (20, 120, 200)).save(out, format="PNG")
    return out.getvalue()


# ------------------------------------------------------------ the fakes


def _frame(node_id: str, name: str, width: int, kids: list) -> dict:
    return {
        "id": node_id,
        "type": "FRAME",
        "name": name,
        "absoluteBoundingBox": {"x": 0, "y": 0, "width": width, "height": 900},
        "fills": [{"type": "SOLID", "color": {"r": 1, "g": 1, "b": 1, "a": 1}}],
        "children": kids,
    }


def _text(node_id: str, chars: str) -> dict:
    style = {"fontFamily": "Inter", "fontSize": 16, "fontWeight": 400}
    return {"id": node_id, "type": "TEXT", "name": "copy", "characters": chars, "style": style}


def _photo(node_id: str) -> dict:
    return {
        "id": node_id,
        "type": "RECTANGLE",
        "name": "photo",
        "fills": [{"type": "IMAGE", "imageRef": REF, "scaleMode": "FILL"}],
    }


TREE = {
    "id": "0:1",
    "type": "CANVAS",
    "name": "Page 1",
    "children": [
        _frame(
            "1:2",
            "Home/Desktop",
            1440,
            [
                _text("2:1", "Bread worth the queue"),
                _photo("2:2"),
                {
                    "id": "2:4",
                    "type": "FRAME",
                    "name": "Row",
                    "children": [{"id": "2:3", "type": "VECTOR", "name": "icon"}],
                },
            ],
        ),
        _frame("1:3", "Home/Mobile", 390, [_text("3:1", "Bread"), _photo("3:2")]),
    ],
}


def _find(node: dict, node_id: str) -> dict | None:
    if node.get("id") == node_id:
        return node
    for kid in node.get("children") or []:
        hit = _find(kid, node_id)
        if hit:
            return hit
    return None


def _trim(node: dict, depth: int) -> dict:
    out = {k: v for k, v in node.items() if k != "children"}
    if depth > 0 and node.get("children"):
        out["children"] = [_trim(c, depth - 1) for c in node["children"]]
    return out


class Figma:
    """The REST API: logs every call; ``tok-b`` cannot open the file."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, str]] = []
        self.version = "101"
        self.touched = "2026-11-01T00:00:00Z"
        self.too_big_at_2x: set[str] = set()

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.figma.com"
        path, params = request.url.path, dict(request.url.params)
        token = request.headers["authorization"].removeprefix("Bearer ")
        self.calls.append((path, params, token))
        if token == "tok-b":
            return httpx.Response(404, json={"status": 404, "err": "Not found"})
        if path == f"/v1/files/{KEY}/meta":
            file = {"name": "Site", "version": self.version, "last_touched_at": self.touched}
            return httpx.Response(200, json={"file": file})
        if path == f"/v1/files/{KEY}":
            assert params == {"depth": "2"}
            return httpx.Response(200, json={"name": "Site", "document": _trim(TREE_DOC, 2)})
        if path == f"/v1/files/{KEY}/nodes":
            depth = int(params["depth"])
            nodes = {}
            for node_id in params["ids"].split(","):
                hit = _find(TREE, node_id)
                nodes[node_id] = (
                    {"document": _trim(hit, depth), "styles": {}, "components": {}} if hit else None
                )
            return httpx.Response(200, json={"name": "Site", "nodes": nodes})
        if path == f"/v1/images/{KEY}":
            fmt = params["format"]
            images = {
                i: None
                if params.get("scale") == "2" and i in self.too_big_at_2x
                else f"https://{RENDER_HOST}/{i}.{fmt}"
                for i in params["ids"].split(",")
            }
            return httpx.Response(200, json={"images": images})
        if path == f"/v1/files/{KEY}/images":
            images = {REF: f"https://{FILL_HOST}/fills/{REF}.png"}
            return httpx.Response(200, json={"meta": {"images": images}})
        return httpx.Response(404, json={"status": 404, "err": "Not found"})

    def tier1(self) -> int:
        return sum(1 for path, _, _ in self.calls if path in TIER1)

    def count(self, path: str) -> int:
        return sum(1 for p, _, _ in self.calls if p == path)


TREE_DOC = {"id": "0:0", "type": "DOCUMENT", "children": [TREE]}


class MemStore:
    """The private upload adapter, in memory; logs every read."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.reads: list[str] = []

    async def put(self, key, stream, mime):
        self.files[key] = b"".join([c async for c in stream])

    async def open(self, key):
        from pocketpaw.uploads.errors import NotFound

        self.reads.append(key)
        if key not in self.files:
            raise NotFound(key)
        yield self.files[key]

    async def delete(self, key):
        self.files.pop(key, None)

    async def browse(self, prefix):
        names: dict[str, bool] = {}
        for key in self.files:
            if key.startswith(prefix):
                head, sep, _ = key[len(prefix) :].partition("/")
                names[head] = bool(sep)
        return [SimpleNamespace(name=n, is_dir=d) for n, d in names.items()]


def _images(request: httpx.Request) -> httpx.Response:
    if str(request.url).endswith(".svg"):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h4"/></svg>'
        return httpx.Response(200, headers={"content-type": "image/svg+xml"}, content=svg)
    return httpx.Response(200, headers={"content-type": "image/png"}, content=_png())


@pytest.fixture
def world(monkeypatch):
    from pocketpaw_ee.agent.mcp_servers import figma as mcp
    from pocketpaw_ee.agent.mcp_servers import sites_reference
    from pocketpaw_ee.integrations.providers import oauth_http
    from pocketpaw_ee.sites.figma import body_cache, client, session_keys

    api = Figma()
    store = MemStore()
    state = SimpleNamespace(user="a")
    members = {
        user: SimpleNamespace(token=f"tok-{user}", conn=SimpleNamespace(meta={}, scopes=[]))
        for user in ("a", "b", "c")
    }

    def source_for(workspace_id, user_id):
        member = members[user_id]

        async def token(rejected: str = "") -> str:
            return member.token

        async def on_read(n: int) -> None:
            member.conn.meta = client.count_read(member.conn.meta, n)

        source = client.RestFigmaSource(
            token, scope=f"conn-{user_id}", workspace=workspace_id, on_read=on_read
        )
        return source, member.conn

    client.clear_cache()
    session_keys.clear_memory()
    monkeypatch.setattr(oauth_http, "_transport", httpx.MockTransport(api.handle))
    monkeypatch.setattr(sites_reference, "_TRANSPORT", httpx.MockTransport(_images))
    monkeypatch.setattr(body_cache, "_store", lambda: store)
    monkeypatch.setattr(mcp, "source_for", AsyncMock(side_effect=source_for))
    asset_store = SimpleNamespace(
        put=AsyncMock(return_value=SimpleNamespace(url="https://assets.test/a.webp"))
    )
    with (
        patch("pocketpaw_ee.sites.figma.client._redis", return_value=None),
        patch("pocketpaw_ee.sites.figma.session_keys._redis", return_value=None),
        patch("pocketpaw_ee.cloud.chat.agent_service.current_workspace_id", return_value="w"),
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_user_id", side_effect=lambda: state.user
        ),
        patch("pocketpaw_ee.cloud.chat.agent_service.current_session_mongo_id", return_value="s"),
        patch(
            "pocketpaw_ee.cloud.workspace.service.get_workspace_plan",
            new=AsyncMock(return_value="go"),
        ),
        patch(
            "pocketpaw_ee.cloud.pockets.service.get",
            new=AsyncMock(return_value={"id": "p1", "workspace": "w"}),
        ),
        patch(
            "pocketpaw_ee.cloud.pockets.service.get_pocket_workspace",
            new=AsyncMock(return_value="w"),
        ),
        patch("pocketpaw_ee.sites.media_generation._require_site", new=AsyncMock()),
        patch("pocketpaw_ee.sites.public_assets.public_asset_store", return_value=asset_store),
    ):
        yield SimpleNamespace(
            mcp=mcp, api=api, store=store, state=state, members=members, client=client
        )
    client.clear_cache()
    session_keys.clear_memory()


async def _as(world, user: str, *links: str) -> None:
    from pocketpaw_ee.sites.figma import session_keys

    world.state.user = user
    for link in links:
        await session_keys.note_message("w", "s", user, link)


def _ok(out: dict) -> dict:
    assert out.get("is_error") is not True, out
    return out


async def _import(world, url: str, *, icons: bool = False, also: tuple[str, ...] = ()) -> list:
    """What the skill teaches: inspect, tokens, screenshot, assets."""
    mcp = world.mcp
    outs = [_ok(await mcp._inspect_handler({"url": url}))]
    for node in also:
        outs.append(_ok(await mcp._inspect_handler({"url": url, "node": node})))
    node = "1:2"
    outs.append(_ok(await mcp._tokens_handler({"url": url, "node": node})))
    for frame in dict.fromkeys((node, *also)):
        outs.append(_ok(await mcp._screenshot_handler({"url": url, "node": frame})))
    nodes = [{"id": "2:2", "format": "webp", "name": "photo"}]
    if icons:
        nodes.append({"id": "2:3", "format": "svg", "name": "icon"})
    out = _ok(await mcp._assets_handler({"url": url, "pocket_id": "p1", "nodes": nodes}))
    assets = json.loads(out["content"][0]["text"])["assets"]
    assert all(a["ok"] for a in assets), assets
    return [*outs, out]


# --------------------------------------------------------------- the counts


async def test_a_node_id_link_without_icons_costs_two_tier1_reads(world):
    await _as(world, "a", FRAME_URL)
    await _import(world, FRAME_URL)
    assert world.api.tier1() == 2
    assert world.api.count(f"/v1/files/{KEY}") == 0  # no index for a node-id link
    assert world.api.count(f"/v1/files/{KEY}/images") == 1  # the photo: tier 2


async def test_icons_add_one_svg_call(world):
    await _as(world, "a", FRAME_URL)
    await _import(world, FRAME_URL, icons=True)
    assert world.api.tier1() == 3
    svg = [q for p, q, _ in world.api.calls if p == f"/v1/images/{KEY}" and q["format"] == "svg"]
    assert svg == [
        {"ids": "2:3", "format": "svg", "svg_outline_text": "true", "svg_include_id": "false"}
    ]


async def test_a_bare_link_adds_only_the_index(world):
    await _as(world, "a", BARE_URL)
    outs = await _import(world, BARE_URL, also=("1:2", "1:3"))
    assert world.api.tier1() == 3
    assert world.api.count(f"/v1/files/{KEY}") == 1
    # Both viewports came in one /nodes call and one batched 2x render.
    nodes = [q for p, q, _ in world.api.calls if p == f"/v1/files/{KEY}/nodes"]
    assert nodes == [{"ids": "1:2,1:3", "depth": "6"}]
    renders = [q for p, q, _ in world.api.calls if p == f"/v1/images/{KEY}"]
    assert renders == [{"ids": "1:2,1:3", "format": "png", "scale": "2"}]
    head = json.loads(outs[0]["content"][0]["text"].split("\n", 1)[0])
    assert head["mode"] == "frames"


async def test_a_page_link_plans_the_site_from_one_nodes_call(world):
    url = f"https://www.figma.com/design/{KEY}/Site?node-id=0-1"
    await _as(world, "a", url)
    out = _ok(await world.mcp._inspect_handler({"url": url}))
    body = out["content"][0]["text"].split("\n<untrusted_design_content>\n", 1)[1]
    payload = json.loads(body.rsplit("\n</untrusted_design_content>", 1)[0])
    [page] = payload["site_pages"]
    assert page["route"] == "/" and [v["node"] for v in page["viewports"]] == ["1:2", "1:3"]
    for node in ("1:2", "1:3"):
        _ok(await world.mcp._inspect_handler({"url": url, "node": node}))
        _ok(await world.mcp._tokens_handler({"url": url, "node": node}))
    assert world.api.tier1() == 1


async def test_a_second_import_of_an_unchanged_version_costs_nothing(world):
    await _as(world, "a", FRAME_URL)
    await _import(world, FRAME_URL, icons=True)
    before, metas = world.api.tier1(), world.api.count(f"/v1/files/{KEY}/meta")
    world.client.clear_cache()  # a later session: the 10-minute meta entry is gone
    await _import(world, FRAME_URL, icons=True)
    assert world.api.tier1() == before
    assert world.api.count(f"/v1/files/{KEY}/meta") == metas + 1  # still gated by /meta
    assert world.api.count(f"/v1/files/{KEY}/images") == 1  # the photo bytes were kept


async def test_the_cache_keys_on_the_version_not_last_touched_at(world):
    await _as(world, "a", FRAME_URL)
    await world.mcp._inspect_handler({"url": FRAME_URL})
    world.api.touched = "2026-11-02T00:00:00Z"  # a comment or a view, same version
    world.client.clear_cache()
    await world.mcp._inspect_handler({"url": FRAME_URL})
    assert world.api.tier1() == 1
    world.api.version = "102"  # an edit
    world.client.clear_cache()
    await world.mcp._inspect_handler({"url": FRAME_URL})
    assert world.api.tier1() == 2
    folders = {k.split("/")[3] for k in world.store.files}
    assert folders == {"102"}  # the superseded version was dropped


async def test_a_frame_too_big_at_2x_is_rendered_once_at_1x(world):
    world.api.too_big_at_2x = {"1:2"}
    await _as(world, "a", FRAME_URL)
    await _import(world, FRAME_URL)
    renders = [q for p, q, _ in world.api.calls if p == f"/v1/images/{KEY}"]
    assert renders == [
        {"ids": "1:2", "format": "png", "scale": "2"},
        {"ids": "1:2", "format": "png", "scale": "1"},
    ]
    world.client.clear_cache()
    out = _ok(await world.mcp._screenshot_handler({"url": FRAME_URL, "node": "1:2"}))
    assert out["content"][0]["type"] == "image"
    assert world.api.tier1() == 3  # the 1x render was kept too


# ------------------------------------------------------------ isolation


async def test_the_cache_is_shared_only_after_the_callers_own_meta(world):
    await _as(world, "a", FRAME_URL)
    await _import(world, FRAME_URL)
    assert world.store.files

    # b cannot open the file: refused at /meta, before any cached body is read.
    await _as(world, "b", FRAME_URL)
    reads = len(world.store.reads)
    out = await world.mcp._inspect_handler({"url": FRAME_URL})
    assert "figma_not_shared" in out["content"][0]["text"]
    out = await world.mcp._screenshot_handler({"url": FRAME_URL})
    assert "figma_not_shared" in out["content"][0]["text"]
    assert len(world.store.reads) == reads
    assert [p for p, _, t in world.api.calls if t == "tok-b"] == [f"/v1/files/{KEY}/meta"] * 2

    # c can: its own /meta passes, then a's import serves it with no tier-1 read.
    await _as(world, "c", FRAME_URL)
    before = world.api.tier1()
    await _import(world, FRAME_URL)
    assert world.api.tier1() == before
    assert [p for p, _, t in world.api.calls if t == "tok-c"] == [f"/v1/files/{KEY}/meta"]


async def test_another_workspace_never_shares_the_cache(world):
    from pocketpaw_ee.sites.figma import body_cache

    await _as(world, "a", FRAME_URL)
    await world.mcp._inspect_handler({"url": FRAME_URL})
    assert all(k.startswith(f"{body_cache.PREFIX}/w/{KEY}/101/") for k in world.store.files)
    assert body_cache.for_version("../w", KEY, "101", store=world.store) is None
    assert body_cache.for_version("w", KEY, "../101", store=world.store) is None


# ---------------------------------------------------------------- budget


async def test_a_metered_account_hears_what_the_import_used(world):
    world.members["a"].conn.meta = {"rate_limit_type": "low"}
    await _as(world, "a", FRAME_URL)
    outs = await _import(world, FRAME_URL)
    note = outs[2]["content"][-1]["text"]  # the screenshot, after its render
    assert "This import used 2 Figma reads; about 4 left this month (estimate)." in note
    assets = json.loads(outs[-1]["content"][0]["text"])
    assert assets["budget"].startswith("This import used 2 Figma reads")
    assert world.members["a"].conn.meta["tier1_reads"] == 2


async def test_an_unmetered_account_gets_no_budget_note(world):
    await _as(world, "a", FRAME_URL)
    outs = await _import(world, FRAME_URL)
    assert "budget" not in json.loads(outs[0]["content"][0]["text"].split("\n", 1)[0])
    assert "Figma read" not in outs[2]["content"][-1]["text"]


async def test_the_starter_plan_is_warned_too(world):
    world.members["a"].conn.meta = {"plan_tier": "starter"}
    await _as(world, "a", FRAME_URL)
    out = _ok(await world.mcp._inspect_handler({"url": FRAME_URL}))
    head = json.loads(out["content"][0]["text"].split("\n", 1)[0])
    assert "Starter plan" in head["warning"]
    assert head["budget"].startswith("This import used 1 Figma read;")


def test_the_month_count_starts_over_each_month():
    from pocketpaw_ee.sites.figma import client

    stale = {"tier1_month": "2020-01", "tier1_reads": 5, "handle": "dana"}
    assert client.monthly_reads(stale) == 0
    fresh = client.count_read(stale)
    assert fresh == {"tier1_month": client.month_now(), "tier1_reads": 1, "handle": "dana"}
    assert client.monthly_reads(client.count_read(fresh, 2)) == 3


# ---------------------------------------------------------------- the store


async def test_a_version_folder_expires_after_thirty_days():
    from pocketpaw_ee.sites.figma import body_cache

    store = MemStore()
    bodies = body_cache.for_version("w", KEY, "101", store=store)
    await bodies.put_json("index.json", {"a": 1})
    again = body_cache.for_version("w", KEY, "101", store=store)
    assert await again.get_json("index.json") == {"a": 1}
    old = time.time() - body_cache.TTL_SECONDS - 60
    store.files[f"{bodies.prefix}_at.json"] = json.dumps({"at": old}).encode()
    late = body_cache.for_version("w", KEY, "101", store=store)
    assert await late.get_json("index.json") is None
    assert not store.files  # the expired folder was deleted


async def test_large_bodies_are_kept(world):
    """No 2 MB cap: a big /nodes answer is cached like any other."""
    big = {"id": "9:9", "type": "FRAME", "name": "big", "children": []}
    big["children"] = [_text(f"9:{n}", "x" * 3000) for n in range(1000)]
    TREE["children"].append(big)
    try:
        url = f"https://www.figma.com/design/{KEY}/Site?node-id=9-9"
        await _as(world, "a", url)
        await world.mcp._inspect_handler({"url": url})
        world.client.clear_cache()
        await world.mcp._inspect_handler({"url": url})
        assert world.api.tier1() == 1
        assert max(len(v) for v in world.store.files.values()) > 2 * 1024 * 1024
    finally:
        TREE["children"].remove(big)
