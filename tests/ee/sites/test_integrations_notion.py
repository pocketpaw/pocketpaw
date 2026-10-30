# tests/ee/sites/test_integrations_notion.py: the notion CMS provider.
#
# Runs through the real routes (connect -> /start -> Notion -> callback -> site
# link -> PUT database_id -> public gateway) against an in-memory fake of Notion's
# OAuth + REST API (2026-03-11, data sources), swapped in as
# ``oauth_http._transport``. No network. Covers: Basic-auth JSON token requests and
# the grant mapping, refresh-token rotation + invalid_grant, the remote database
# options, property projection per type, the Published-checkbox convention,
# get_entry refusing pages of other databases, block flattening + caps, the cache
# (hits and isolation between two sites) and error mapping.
from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from tests.ee.sites.test_integrations import (  # noqa: F401 - _env is an autouse fixture
    ORIGIN,
    OWNER,
    SITE_HOST,
    WS,
    _client,
    _code,
    _env,
    _gw,
    _message,
    _pocket,
    _site,
)

CLIENT_ID = "notion-client"
SECRET = "notion-secret"
NOTION_WS = "4f1c0b52-0000-4000-8000-0000000000aa"


def uid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-000000000000"


BLOG = uid(1)
NEWS = uid(2)
OTHER = uid(3)
DUAL = uid(4)


def rt(text: str) -> list[dict]:
    return [{"type": "text", "plain_text": text, "href": "https://x.test"}]


def page(n: int, source: str, title: str, *, published: bool | None = True, **extra) -> dict:
    props: dict = {"Name": {"type": "title", "title": rt(title)}}
    if published is not None:
        props["Published"] = {"type": "checkbox", "checkbox": published}
    props.update(extra)
    return {
        "object": "page",
        "id": uid(n),
        "parent": {"type": "data_source_id", "data_source_id": source, "database_id": uid(900)},
        "in_trash": False,
        "properties": props,
        "cover": None,
    }


def block(n: int, kind: str, text: str = "", **data) -> dict:
    body = {"rich_text": rt(text), **data} if kind not in ("divider", "image") else data
    return {
        "object": "block",
        "id": uid(n),
        "type": kind,
        kind: body,
        "has_children": False,
    }


class FakeNotion:
    def __init__(self) -> None:
        self.valid: set[str] = set()
        self.live_refresh: set[str] = set()
        self.issued = 0
        self.token_calls: list[dict] = []
        self.calls: list[tuple[str, str]] = []
        self.rate_limited = False
        self.sources: dict[str, dict] = {
            BLOG: {
                "title": "Blog",
                "schema": {
                    "Name": {"type": "title"},
                    "Published": {"type": "checkbox"},
                    "Publish date": {"type": "date"},
                },
            },
            NEWS: {"title": "News", "schema": {"Name": {"type": "title"}}},
            DUAL: {
                "title": "Dual",
                "schema": {
                    "Name": {"type": "title"},
                    "Published": {"type": "checkbox"},
                    "PUBLIC": {"type": "checkbox"},
                },
            },
        }
        self.pages: list[dict] = [
            page(
                10,
                BLOG,
                "First post",
                **{"Publish date": {"type": "date", "date": {"start": "2026-01-02"}}},
            ),
            page(11, BLOG, "Draft", published=False),
            page(12, BLOG, "Second post"),
            page(20, NEWS, "News item", published=None),
            page(30, OTHER, "Elsewhere", published=None),
            page(40, DUAL, "Both", PUBLIC={"type": "checkbox", "checkbox": True}),
            page(41, DUAL, "Only one", PUBLIC={"type": "checkbox", "checkbox": False}),
            page(42, DUAL, "No public prop"),
        ]
        # Parent database matches BLOG's id but the data source is another one.
        self.pages.append(
            {
                **page(50, OTHER, "Sibling source"),
                "parent": {"type": "data_source_id", "data_source_id": OTHER, "database_id": BLOG},
            }
        )
        self.blocks: dict[str, list[dict]] = {}

    # -- token endpoint
    def _token(self, request: httpx.Request) -> httpx.Response:
        basic = base64.b64encode(f"{CLIENT_ID}:{SECRET}".encode()).decode()
        assert request.headers["authorization"] == f"Basic {basic}"
        assert request.headers["content-type"] == "application/json"
        assert request.headers["notion-version"] == "2026-03-11"
        body = json.loads(request.content)
        self.token_calls.append(body)
        if body["grant_type"] == "refresh_token":
            if body["refresh_token"] not in self.live_refresh:
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.live_refresh.discard(body["refresh_token"])  # single use
        elif body["code"] != "good":
            return httpx.Response(400, json={"error": "invalid_grant"})
        self.issued += 1
        access, refresh = f"n-access-{self.issued}", f"n-refresh-{self.issued}"
        self.valid.add(access)
        self.live_refresh.add(refresh)
        return httpx.Response(
            200,
            json={
                "access_token": access,
                "token_type": "bearer",
                "refresh_token": refresh,
                "bot_id": "bot-1",
                "workspace_id": NOTION_WS,
                "workspace_name": "Acme Notion",
                "owner": {"type": "workspace", "workspace": True},
            },
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.notion.com"
        path = request.url.path
        if path == "/v1/oauth/token":
            return self._token(request)
        assert request.headers["notion-version"] == "2026-03-11"
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token not in self.valid:
            return httpx.Response(401, json={"object": "error", "code": "unauthorized"})
        self.calls.append((request.method, path))
        if self.rate_limited:
            return httpx.Response(429, json={"object": "error", "code": "rate_limited"})
        body = json.loads(request.content) if request.content else {}
        params = dict(request.url.params)
        if path == "/v1/search":
            return self._search(body)
        parts = path.split("/")
        if parts[2] == "data_sources":
            source = self.sources.get(parts[3])
            if source is None:
                return httpx.Response(404, json={"object": "error", "code": "object_not_found"})
            if len(parts) == 4:
                return httpx.Response(
                    200, json={"object": "data_source", "properties": source["schema"]}
                )
            return self._query(parts[3], body)
        if parts[2] == "pages":
            found = next(
                (p for p in self.pages if p["id"].replace("-", "") == parts[3].replace("-", "")),
                None,
            )
            if found is None:
                return httpx.Response(404, json={"object": "error", "code": "object_not_found"})
            return httpx.Response(200, json=found)
        if parts[2] == "blocks":
            items = self.blocks.get(parts[3], [])
            start = int(params.get("start_cursor") or 0)
            size = int(params["page_size"])
            chunk = items[start : start + size]
            more = start + size < len(items)
            return httpx.Response(
                200,
                json={
                    "results": chunk,
                    "has_more": more,
                    "next_cursor": str(start + size) if more else None,
                },
            )
        return httpx.Response(404)

    def _search(self, body: dict) -> httpx.Response:
        assert body["filter"] == {"property": "object", "value": "data_source"}
        results = [
            {"object": "data_source", "id": sid, "title": rt(s["title"]), "in_trash": False}
            for sid, s in self.sources.items()
        ]
        results.append({"object": "data_source", "id": uid(77), "properties": {}})  # partial
        results.append(
            {"object": "data_source", "id": uid(78), "title": rt("Old"), "in_trash": True}
        )
        if body.get("start_cursor"):
            return httpx.Response(
                200,
                json={
                    "results": [{"object": "data_source", "id": uid(79), "title": rt("Page two")}],
                    "has_more": False,
                },
            )
        return httpx.Response(
            200, json={"results": results, "has_more": True, "next_cursor": uid(5)}
        )

    def _query(self, source: str, body: dict) -> httpx.Response:
        rows = [p for p in self.pages if p["parent"]["data_source_id"] == source]
        flt = body.get("filter")
        if flt:
            checks = flt.get("and", [flt])
            rows = [
                r
                for r in rows
                if all(
                    r["properties"].get(c["property"], {}).get("checkbox")
                    is c["checkbox"]["equals"]
                    for c in checks
                )
            ]
        cursor = body.get("start_cursor")
        if cursor and cursor != "cur-1":
            return httpx.Response(400, json={"object": "error", "code": "validation_error"})
        start = 1 if cursor else 0
        size = body["page_size"]
        chunk = rows[start : start + size]
        more = start + size < len(rows)
        self.last_query = body
        return httpx.Response(
            200,
            json={
                "object": "list",
                "results": chunk,
                "has_more": more,
                "next_cursor": "cur-1" if more else None,
            },
        )


@pytest.fixture
def notion(monkeypatch):
    from pocketpaw_ee.integrations import field_options
    from pocketpaw_ee.integrations.providers import notion as notion_mod
    from pocketpaw_ee.integrations.providers import oauth_http

    fake = FakeNotion()
    monkeypatch.setattr(oauth_http, "_transport", httpx.MockTransport(fake.handle))
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_NOTION_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_NOTION_CLIENT_SECRET", SECRET)
    notion_mod.clear_cache()
    field_options.clear_cache()
    yield fake
    notion_mod.clear_cache()
    field_options.clear_cache()


async def _connect(c, pid: str, code: str = "good") -> dict:
    started = await c.post("/api/v1/integrations/notion/connect", json={"pocket_id": pid})
    assert started.status_code == 200, started.text
    split = urlsplit(started.json()["authorize_url"])
    start = await c.get(split.path, params=parse_qs(split.query))
    assert start.status_code == 302, start.text
    location = urlsplit(start.headers["location"])
    assert (location.netloc, location.path) == ("api.notion.com", "/v1/oauth/authorize")
    query = {k: v[0] for k, v in parse_qs(location.query).items()}
    cb = await c.get(
        "/api/v1/integrations/notion/callback", params={"code": code, "state": query["state"]}
    )
    assert cb.status_code == 200
    return _message(cb.text)


async def _put_db(c, pid: str, db: str):
    return await c.put(
        f"/api/v1/sites/by-pocket/{pid}/integrations/notion", json={"settings": {"database_id": db}}
    )


async def _linked(c, db: str = BLOG) -> tuple[str, str]:
    pid = await _pocket()
    site = await _site(pid)
    msg = await _connect(c, pid)
    assert msg["status"] == "active", msg
    resp = await _put_db(c, pid, db)
    assert resp.status_code == 200, resp.text
    return str(site.id), pid


async def _conn():
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection

    return await IntegrationConnection.find_one(IntegrationConnection.provider == "notion")


# ----------------------------------------------------------------- unit


def test_registry_authorize_url_and_availability(monkeypatch, notion):
    from pocketpaw_ee.integrations.providers import get_provider

    provider = get_provider("notion")
    assert (provider.kind, provider.category, provider.plan) == ("connect", "cms", "site")
    field, note = provider.settings_fields
    assert (note.key, note.type, note.readonly) == ("publishing", "text", True)
    assert (field.key, field.type, field.options_source, field.required) == (
        "database_id",
        "select",
        "remote",
        True,
    )
    for spec in provider.actions.values():
        assert spec.public and not spec.write
    assert set(provider.actions) == {"list_entries", "get_entry"}
    split = urlsplit(provider.authorize_url("st-1", "https://connect.example.com/cb"))
    q = {k: v[0] for k, v in parse_qs(split.query).items()}
    assert (split.netloc, split.path) == ("api.notion.com", "/v1/oauth/authorize")
    assert q == {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "owner": "user",
        "redirect_uri": "https://connect.example.com/cb",
        "state": "st-1",
    }
    assert provider.availability() == "available"
    monkeypatch.delenv("POCKETPAW_INTEGRATIONS_NOTION_CLIENT_SECRET")
    assert provider.availability() == "unconfigured"


def test_property_projection_for_each_type():
    from pocketpaw_ee.integrations.providers.notion import _entry

    long_text = "word " * 100
    entry = _entry(
        {
            "id": uid(1),
            "cover": {"type": "external", "external": {"url": "https://img.test/c.png"}},
            "properties": {
                "Name": {"type": "title", "title": rt("Hello\x00 world")},
                "Body": {"type": "rich_text", "rich_text": rt(long_text)},
                "Kind": {"type": "select", "select": {"name": "Guide"}},
                "State": {"type": "status", "status": {"name": "Done"}},
                "Tags": {"type": "multi_select", "multi_select": [{"name": "a"}, {"name": "b"}]},
                "Date": {"type": "date", "date": {"start": "2026-03-04", "end": None}},
                "Count": {"type": "number", "number": 3},
                "Ratio": {"type": "number", "number": 0.5},
                "Done": {"type": "checkbox", "checkbox": True},
                "Link": {"type": "url", "url": "https://a.test"},
                "Mail": {"type": "email", "email": "a@b.co"},
                "Phone": {"type": "phone_number", "phone_number": None},
                "Tel": {"type": "phone_number", "phone_number": "+1 (555) 010-0000"},
                "JsLink": {"type": "url", "url": "javascript:alert(1)"},
                "DataLink": {"type": "url", "url": "data:text/html,<script>x</script>"},
                "BareLink": {"type": "url", "url": "example.com"},
                "BadMail": {"type": "email", "email": "javascript:alert(1)//@x"},
                "BadTel": {"type": "phone_number", "phone_number": "tel:javascript:1"},
                "Slug": {"type": "rich_text", "rich_text": rt("hello-world")},
                "Calc": {"type": "formula", "formula": {"type": "string", "string": "x"}},
                "People": {"type": "people", "people": [{"id": "u"}]},
                "Rel": {"type": "relation", "relation": []},
            },
        }
    )
    assert entry["properties"] == {
        "Name": "Hello world",
        "Body": long_text,
        "Kind": "Guide",
        "State": "Done",
        "Tags": "a, b",
        "Date": "2026-03-04",
        "Count": "3",
        "Ratio": "0.5",
        "Done": "true",
        "Link": "https://a.test",
        "Mail": "a@b.co",
        "Phone": "",
        "Tel": "+1 (555) 010-0000",
        "JsLink": "",
        "DataLink": "",
        "BareLink": "",
        "BadMail": "",
        "BadTel": "",
        "Slug": "hello-world",
        "Calc": "x",
    }
    assert entry["title"] == "Hello world" and entry["slug"] == "hello-world"
    assert entry["published_at"] == "2026-03-04"
    assert entry["cover_url"] == "https://img.test/c.png"
    assert len(entry["excerpt"]) <= 201 and entry["excerpt"].endswith("…")
    insecure = _entry(
        {
            "id": "x",
            "properties": {},
            "cover": {"type": "external", "external": {"url": "http://a"}},
        }
    )
    assert "cover_url" not in insecure and insecure["excerpt"] == ""


@pytest.mark.asyncio
async def test_refresh_rotates_and_invalid_grant(notion):
    from pocketpaw_ee.integrations.providers import get_provider
    from pocketpaw_ee.integrations.providers.base import InvalidGrant

    provider = get_provider("notion")
    notion.live_refresh.add("rt-0")
    grant = await provider.refresh("rt-0")
    assert grant.refresh_token == "n-refresh-1" and grant.expires_in is None
    assert notion.token_calls == [{"grant_type": "refresh_token", "refresh_token": "rt-0"}]
    with pytest.raises(InvalidGrant):
        await provider.refresh("rt-0")  # spent


# ------------------------------------------------------------ end to end


@pytest.mark.asyncio
async def test_connect_maps_the_grant(beanie_test_db, notion):
    async with _client() as c:
        await _linked(c)
    conn = await _conn()
    assert (conn.account_ref, conn.account_label) == (NOTION_WS, "Acme Notion")
    assert conn.meta["bot_id"] == "bot-1" and conn.meta["workspace_id"] == NOTION_WS
    assert conn.expires_at is None
    assert notion.token_calls[0]["grant_type"] == "authorization_code"
    assert notion.token_calls[0]["code"] == "good"
    assert notion.token_calls[0]["redirect_uri"].endswith("/api/v1/integrations/notion/callback")


@pytest.mark.asyncio
async def test_remote_database_options_and_validation(beanie_test_db, notion):
    async with _client() as c:
        pid = await _pocket()
        await _site(pid)
        await _connect(c, pid)
        resp = await c.get(
            f"/api/v1/sites/by-pocket/{pid}/integrations/notion/fields/database_id/options"
        )
        bad = await _put_db(c, pid, OTHER)
    assert resp.status_code == 200, resp.text
    options = resp.json()
    options = options.get("options", options)
    assert [(o["value"], o["label"]) for o in options] == [
        (BLOG, "Blog"),
        (NEWS, "News"),
        (DUAL, "Dual"),
        (uid(77), "Untitled database"),
        (uid(79), "Page two"),
    ]
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_list_entries_published_filter_order_and_cache(beanie_test_db, notion):
    async with _client() as c:
        sid, _ = await _linked(c)
        first = await _gw(c, sid, "notion", "list_entries", args={"limit": 1})
        again = await _gw(c, sid, "notion", "list_entries", args={"limit": 1})
        nxt = await _gw(c, sid, "notion", "list_entries", args={"limit": 1, "cursor": "cur-1"})
        bad = await _gw(c, sid, "notion", "list_entries", args={"limit": 1, "cursor": "nope"})
        big = await _gw(c, sid, "notion", "list_entries", args={"limit": 51})
    result = first.json()["result"]
    assert [e["title"] for e in result["entries"]] == ["First post"], first.text
    assert result["next_cursor"] == "cur-1"
    assert result["entries"][0]["published_at"] == "2026-01-02"
    assert notion.last_query["filter"] == {"property": "Published", "checkbox": {"equals": True}}
    assert notion.last_query["sorts"][0] == {"property": "Publish date", "direction": "descending"}
    assert again.json() == first.json()
    queries = [call for call in notion.calls if call[1].endswith("/query")]
    assert len(queries) == 3  # first, cursor page, bad cursor; `again` came from the cache
    assert [e["title"] for e in nxt.json()["result"]["entries"]] == ["Second post"]
    assert nxt.json()["result"]["next_cursor"] is None
    assert _code(bad) == "invalid_args" and _code(big) == "invalid_args"


@pytest.mark.asyncio
async def test_database_without_flag_returns_every_row(beanie_test_db, notion):
    async with _client() as c:
        sid, _ = await _linked(c, NEWS)
        resp = await _gw(c, sid, "notion", "list_entries", args={})
    assert [e["title"] for e in resp.json()["result"]["entries"]] == ["News item"]
    assert "filter" not in notion.last_query
    assert notion.last_query["sorts"] == [{"timestamp": "created_time", "direction": "descending"}]


@pytest.mark.asyncio
async def test_get_entry_refuses_foreign_unpublished_and_missing(beanie_test_db, notion):
    async with _client() as c:
        sid, _ = await _linked(c)
        foreign = await _gw(c, sid, "notion", "get_entry", args={"id": uid(30)})
        other_db = await _gw(c, sid, "notion", "get_entry", args={"id": uid(20)})
        draft = await _gw(c, sid, "notion", "get_entry", args={"id": uid(11)})
        missing = await _gw(c, sid, "notion", "get_entry", args={"id": uid(99)})
        junk = await _gw(c, sid, "notion", "get_entry", args={"id": "../users/me"})
    for resp in (foreign, other_db, draft, missing, junk):
        assert resp.status_code == 422 and _code(resp) == "invalid_args", resp.text
    # A visitor's bad id never marks the owner's binding.
    from pocketpaw_ee.cloud.models.integration import SiteIntegration

    row = await SiteIntegration.find_one(SiteIntegration.provider == "notion")
    assert not row.setup_error
    # No block was read for a refused page.
    assert not [call for call in notion.calls if call[1].startswith("/v1/blocks")]


@pytest.mark.asyncio
async def test_get_entry_flattens_blocks(beanie_test_db, notion):
    notion.blocks[uid(10)] = [
        block(100, "heading_1", "Intro"),
        block(101, "paragraph", "Hello <b>there</b>"),
        {**block(102, "bulleted_list_item", "one"), "has_children": True},
        block(103, "bulleted_list_item", "two"),
        block(104, "numbered_list_item", "first"),
        block(105, "quote", "Said"),
        block(106, "code", "print(1)", language="python"),
        block(
            107,
            "image",
            file={"url": "https://s3.test/i.png", "expiry_time": "2026-10-14T10:00:00Z"},
            type="file",
            caption=rt("Cap"),
        ),
        block(108, "image", external={"url": "http://insecure.test/i.png"}, type="external"),
        block(109, "divider"),
        block(110, "embed", url="https://evil.test"),
        block(111, "heading_3", "Deep"),
    ]
    notion.blocks[uid(102)] = [block(120, "bulleted_list_item", "one.a")]
    async with _client() as c:
        sid, _ = await _linked(c)
        resp = await _gw(c, sid, "notion", "get_entry", args={"id": uid(10).replace("-", "")})
    result = resp.json()["result"]
    assert result["id"] == uid(10) and result["title"] == "First post"
    assert result["blocks"] == [
        {"type": "heading", "level": 1, "text": "Intro"},
        {"type": "paragraph", "text": "Hello <b>there</b>"},
        {"type": "bulleted_list", "items": ["one", "one.a", "two"]},
        {"type": "numbered_list", "items": ["first"]},
        {"type": "quote", "text": "Said"},
        {"type": "code", "text": "print(1)"},
        {
            "type": "image",
            "url": "https://s3.test/i.png",
            "text": "Cap",
            "expires_at": "2026-10-14T10:00:00Z",
        },
        {"type": "divider"},
        {"type": "heading", "level": 3, "text": "Deep"},
    ]
    assert "truncated" not in result


@pytest.mark.asyncio
async def test_get_entry_caps_blocks(beanie_test_db, notion):
    notion.blocks[uid(12)] = [block(1000 + i, "paragraph", f"p{i}") for i in range(250)]
    notion.blocks[uid(12)][0]["has_children"] = True
    async with _client() as c:
        sid, _ = await _linked(c)
        resp = await _gw(c, sid, "notion", "get_entry", args={"id": uid(12)})
    result = resp.json()["result"]
    assert len(result["blocks"]) == 200 and result["truncated"] is True
    block_calls = [call for call in notion.calls if call[1].startswith("/v1/blocks")]
    assert len(block_calls) == 2  # 100 + 100 top level; no budget left for children


@pytest.mark.asyncio
async def test_cache_is_isolated_between_two_sites(beanie_test_db, notion):
    from pocketpaw_ee.cloud.models.site import Site

    async with _client() as c:
        sid_a, _ = await _linked(c, BLOG)
        pid_b = await _pocket()
        site_b = Site(
            workspace=WS,
            pocket_id=pid_b,
            owner=OWNER,
            script_name="news-abc",
            signed_key="site_key_" + "n" * 32,
            allowed_origins=[SITE_HOST],
        )
        await site_b.insert()
        assert (await _connect(c, pid_b))["status"] == "active"
        assert (await _put_db(c, pid_b, NEWS)).status_code == 200
        a = await _gw(c, sid_a, "notion", "list_entries", args={"limit": 5})
        b = await _gw(
            c,
            str(site_b.id),
            "notion",
            "list_entries",
            args={"limit": 5},
            headers={"x-paw-site-key": "site_key_" + "n" * 32, "origin": ORIGIN},
        )
        b_entry = await _gw(
            c,
            str(site_b.id),
            "notion",
            "get_entry",
            args={"id": uid(10)},
            headers={"x-paw-site-key": "site_key_" + "n" * 32, "origin": ORIGIN},
        )
    assert [e["title"] for e in a.json()["result"]["entries"]] == ["First post", "Second post"]
    assert [e["title"] for e in b.json()["result"]["entries"]] == ["News item"], b.text
    assert _code(b_entry) == "invalid_args"  # BLOG's page is not site B's entry


@pytest.mark.asyncio
async def test_a_401_refreshes_once_and_rotates(beanie_test_db, notion):
    from pocketpaw_ee.cloud._core import crypto

    async with _client() as c:
        sid, _ = await _linked(c)
        notion.valid.clear()  # access token revoked early
        resp = await _gw(c, sid, "notion", "list_entries", args={})
    assert resp.status_code == 200, resp.text
    refreshes = [b for b in notion.token_calls if b["grant_type"] == "refresh_token"]
    assert [b["refresh_token"] for b in refreshes] == ["n-refresh-1"]
    stored = crypto.decrypt_json((await _conn()).token_ciphertext)
    assert stored["refresh_token"] == "n-refresh-2" and stored["access_token"] == "n-access-2"


@pytest.mark.asyncio
async def test_dead_refresh_token_needs_reconnect(beanie_test_db, notion):
    async with _client() as c:
        sid, _ = await _linked(c)
        notion.valid.clear()
        notion.live_refresh.clear()
        resp = await _gw(c, sid, "notion", "list_entries", args={})
    assert resp.status_code == 409 and _code(resp) == "not_connected", resp.text
    assert (await _conn()).status == "needs_reconnect"


@pytest.mark.asyncio
async def test_error_mapping(beanie_test_db, notion):
    from pocketpaw_ee.cloud.models.integration import SiteIntegration

    async with _client() as c:
        sid, _ = await _linked(c)
        notion.rate_limited = True
        limited = await _gw(c, sid, "notion", "list_entries", args={})
        notion.rate_limited = False
        del notion.sources[BLOG]  # unshared from the integration
        gone = await _gw(c, sid, "notion", "list_entries", args={"limit": 3})
    assert limited.status_code == 429 and _code(limited) == "rate_limited"
    assert gone.status_code == 409 and _code(gone) == "setup_required"
    row = await SiteIntegration.find_one(SiteIntegration.provider == "notion")
    assert row.setup_error == "notion_database_unavailable"


def test_get_entry_caps_are_tighter():
    from pocketpaw_ee.integrations.providers import get_provider

    actions = get_provider("notion").actions
    assert actions["get_entry"].caps() == (20, 60)
    assert actions["list_entries"].caps() == (60, 300)


@pytest.mark.asyncio
async def test_every_publish_checkbox_must_be_checked(beanie_test_db, notion):
    async with _client() as c:
        sid, pid = await _linked(c, DUAL)
        listed = await _gw(c, sid, "notion", "list_entries", args={})
        both = await _gw(c, sid, "notion", "get_entry", args={"id": uid(40)})
        one = await _gw(c, sid, "notion", "get_entry", args={"id": uid(41)})
        absent = await _gw(c, sid, "notion", "get_entry", args={"id": uid(42)})
        view = await c.get(f"/api/v1/sites/by-pocket/{pid}/integrations")
    assert [e["title"] for e in listed.json()["result"]["entries"]] == ["Both"], listed.text
    assert notion.last_query["filter"] == {
        "and": [
            {"property": "Published", "checkbox": {"equals": True}},
            {"property": "PUBLIC", "checkbox": {"equals": True}},
        ]
    }
    assert both.json()["result"]["title"] == "Both"
    assert _code(one) == "invalid_args" and _code(absent) == "invalid_args"
    [row] = view.json()
    assert row["settings"]["publishing"] == (
        'Only rows with "Published" and "PUBLIC" checked are public.'
    )


@pytest.mark.asyncio
async def test_publishing_note_tells_the_owner(beanie_test_db, notion):
    from pocketpaw_ee.cloud.models.integration import SiteIntegration
    from pocketpaw_ee.integrations.providers.notion import NOTE_ALL_PUBLIC

    async with _client() as c:
        _, pid = await _linked(c, NEWS)
        row = await SiteIntegration.find_one(SiteIntegration.provider == "notion")
        assert row.settings["publishing"] == NOTE_ALL_PUBLIC
        assert (await _put_db(c, pid, BLOG)).status_code == 200
        ignored = await c.put(
            f"/api/v1/sites/by-pocket/{pid}/integrations/notion",
            json={"settings": {"publishing": "everything is fine"}},
        )
    row = await SiteIntegration.find_one(SiteIntegration.provider == "notion")
    assert row.settings["publishing"] == 'Only rows with "Published" checked are public.'
    assert ignored.status_code in (200, 422)


@pytest.mark.asyncio
async def test_parent_database_id_alone_is_not_enough(beanie_test_db, notion):
    async with _client() as c:
        sid, _ = await _linked(c)
        resp = await _gw(c, sid, "notion", "get_entry", args={"id": uid(50)})
    assert _code(resp) == "invalid_args"


@pytest.mark.asyncio
async def test_get_entry_refusals_are_cached(beanie_test_db, notion):
    async with _client() as c:
        sid, _ = await _linked(c)
        for _ in range(3):
            assert (
                _code(await _gw(c, sid, "notion", "get_entry", args={"id": uid(99)}))
                == "invalid_args"
            )
            assert (
                _code(await _gw(c, sid, "notion", "get_entry", args={"id": uid(11)}))
                == "invalid_args"
            )
    page_calls = [call for call in notion.calls if call[1].startswith("/v1/pages/")]
    assert sorted(page_calls) == sorted(
        [("GET", f"/v1/pages/{uid(99)}"), ("GET", f"/v1/pages/{uid(11)}")]
    )
