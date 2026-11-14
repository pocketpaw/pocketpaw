# tests/ee/sites/test_integrations_remote_options.py: remote select options (Wave 2 seam).
#
# Covers ``SettingsField.options_source="remote"`` on the provider view, the
# ``field_options`` provider hook behind
# ``GET /sites/by-pocket/{pocket_id}/integrations/{provider}/fields/{key}/options``
# (authz, 404 / 409 not_connected / 502 provider_error, normalising + the 500 cap,
# the 60s per-binding cache), and PUT validation of a remote select (membership
# from the same cached list, 502 rather than an unvalidated value, 409 without a
# connection, an unchanged value never re-checked, every item of a multi select
# re-checked), a relink (PUT or OAuth callback) dropping stored remote values,
# single-flight loads, a builtin with no binding, and draft ``depends_on`` parents
# as query params (validated, never saved, cached apart). The ``demo`` provider's
# ``board`` field is the fixture; helpers come from test_integrations.py.
from __future__ import annotations

import pytest

from tests.ee.sites.test_integrations import (  # noqa: F401 - _env is an autouse fixture
    OTHER_WS,
    OWNER,
    WS,
    _client,
    _connect_demo,
    _env,
    _message,
    _pocket,
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    from pocketpaw_ee.integrations import field_options

    field_options.clear_cache()
    yield
    field_options.clear_cache()


def _options_url(pid: str, provider: str = "demo", key: str = "board") -> str:
    return f"/api/v1/sites/by-pocket/{pid}/integrations/{provider}/fields/{key}/options"


def _put_url(pid: str, provider: str = "demo") -> str:
    return f"/api/v1/sites/by-pocket/{pid}/integrations/{provider}"


def _counting(monkeypatch, result=None, error: Exception | None = None) -> dict:
    """Replace the demo hook; ``state`` counts calls and can switch to failing."""
    from pocketpaw_ee.integrations.providers.demo import DEMO_BOARDS, DemoProvider

    state = {"calls": 0, "error": error, "result": result}

    async def _hook(self, binding, ctx, key):
        state["calls"] += 1
        if state["error"] is not None:
            raise state["error"]
        return state["result"] if state["result"] is not None else list(DEMO_BOARDS)

    monkeypatch.setattr(DemoProvider, "field_options", _hook)
    return state


async def _bound(c, pid: str) -> str:
    page, _ = await _connect_demo(c, pid)
    return _message(page)["connection_id"]


# ------------------------------------------------------------------- view


@pytest.mark.asyncio
async def test_remote_select_is_marked_on_the_provider_view(beanie_test_db):
    async with _client() as c:
        providers = {p["id"]: p for p in (await c.get("/api/v1/integrations/providers")).json()}
    fields = {f["key"]: f for f in providers["demo"]["settings_fields"]}
    board = fields["board"]
    assert (board["type"], board["options_source"], board["options"]) == (
        "select",
        "remote",
        None,
    )
    assert fields["note"]["options_source"] == "static"


def test_check_value_for_a_remote_select():
    from pocketpaw_ee.integrations.providers.base import SettingsField
    from pocketpaw_ee.integrations.settings import check_value, merge_put

    spec = SettingsField("board", "Board", "select", options_source="remote")
    # Stored values are only normalised (membership was checked when stored).
    assert check_value(spec, " brd_x ") == "brd_x"
    assert check_value(spec, "brd_a", {"brd_a"}) == "brd_a"
    with pytest.raises(ValueError):
        check_value(spec, "brd_x", {"brd_a"})

    from pocketpaw_ee.cloud._core.errors import ValidationError

    provider = type("P", (), {"name": "P", "settings_fields": (spec,)})()
    # A changed remote value with no loaded list is refused (fail closed).
    with pytest.raises(ValidationError):
        merge_put(provider, {"board": "brd_a"}, {})
    assert merge_put(provider, {"board": "brd_a"}, {}, {"board": {"brd_a"}}) == {"board": "brd_a"}
    # Unchanged needs no list.
    assert merge_put(provider, {"board": "brd_a"}, {"board": "brd_a"}) == {"board": "brd_a"}


# ------------------------------------------------------------------ route


@pytest.mark.asyncio
async def test_options_route_loads_the_provider_list_and_caches_it(beanie_test_db, monkeypatch):
    state = _counting(monkeypatch)
    pid = await _pocket()
    async with _client() as c:
        await _bound(c, pid)
        first = await c.get(_options_url(pid))
        second = await c.get(_options_url(pid))
    assert first.status_code == 200, first.text
    assert first.json() == {
        "options": [
            {"value": "brd_launch", "label": "Launch plan"},
            {"value": "brd_roadmap", "label": "Roadmap"},
            {"value": "brd_support", "label": "Support queue"},
        ]
    }
    assert second.json() == first.json()
    assert state["calls"] == 1


@pytest.mark.asyncio
async def test_the_real_demo_hook_reaches_a_token(beanie_test_db):
    pid = await _pocket()
    async with _client() as c:
        await _bound(c, pid)
        resp = await c.get(_options_url(pid))
    assert resp.status_code == 200
    assert [o["value"] for o in resp.json()["options"]] == [
        "brd_launch",
        "brd_roadmap",
        "brd_support",
    ]


@pytest.mark.asyncio
async def test_cache_expires_and_is_per_connection(beanie_test_db, monkeypatch):
    from pocketpaw_ee.integrations import field_options

    state = _counting(monkeypatch)
    pid = await _pocket()
    async with _client() as c:
        conn_id = await _bound(c, pid)
        await c.get(_options_url(pid))
        # Keyed per binding (workspace, pocket, provider, connection) + field.
        assert list(field_options._cache) == [(WS, pid, "demo", conn_id, "board")]
        for key, (_, options) in list(field_options._cache.items()):
            field_options._cache[key] = (0.0, options)
        await c.get(_options_url(pid))
    assert state["calls"] == 2


@pytest.mark.asyncio
async def test_unknown_provider_or_field_is_404(beanie_test_db, monkeypatch):
    _counting(monkeypatch)
    pid = await _pocket()
    async with _client() as c:
        await _bound(c, pid)
        provider = await c.get(_options_url(pid, provider="nope"))
        field = await c.get(_options_url(pid, key="nope"))
        text = await c.get(_options_url(pid, key="note"))
        link = await c.get(_options_url(pid, key="board_url"))
    assert [r.status_code for r in (provider, field, text, link)] == [404, 404, 404, 404]


@pytest.mark.asyncio
async def test_static_select_answers_its_declared_options(beanie_test_db, monkeypatch):
    from pocketpaw_ee.integrations.providers.base import SettingOption, SettingsField
    from pocketpaw_ee.integrations.providers.demo import DemoProvider

    state = _counting(monkeypatch)
    monkeypatch.setattr(
        DemoProvider,
        "settings_fields",
        (
            *DemoProvider.settings_fields,
            SettingsField("size", "Size", "select", options=(SettingOption("s", "Small"),)),
        ),
    )
    pid = await _pocket()
    async with _client() as c:
        resp = await c.get(_options_url(pid, key="size"))
    assert resp.json() == {"options": [{"value": "s", "label": "Small"}]}
    assert state["calls"] == 0


@pytest.mark.asyncio
async def test_options_need_a_live_connection(beanie_test_db, monkeypatch):
    from beanie import PydanticObjectId
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection

    state = _counting(monkeypatch)
    pid = await _pocket()
    async with _client() as c:
        unbound = await c.get(_options_url(pid))
        conn_id = await _bound(c, pid)
        conn = await IntegrationConnection.get(PydanticObjectId(conn_id))
        conn.status = "needs_reconnect"
        await conn.save()
        stale = await c.get(_options_url(pid))
    for resp in (unbound, stale):
        assert (resp.status_code, resp.json()["error"]["code"]) == (409, "not_connected")
    assert state["calls"] == 0


@pytest.mark.asyncio
async def test_provider_failure_is_a_generic_502_and_is_not_cached(beanie_test_db, monkeypatch):
    from pocketpaw_ee.integrations.providers.base import ProviderError

    state = _counting(monkeypatch, error=ProviderError("upstream said secret-detail 500"))
    pid = await _pocket()
    async with _client() as c:
        await _bound(c, pid)
        failed = await c.get(_options_url(pid))
        state["error"] = RuntimeError("boom")
        crashed = await c.get(_options_url(pid))
        state["error"] = None
        recovered = await c.get(_options_url(pid))
    for resp in (failed, crashed):
        assert (resp.status_code, resp.json()["error"]["code"]) == (502, "provider_error")
        assert "secret-detail" not in resp.text and "boom" not in resp.text
    assert recovered.status_code == 200 and state["calls"] == 3


@pytest.mark.asyncio
async def test_options_are_normalised_and_capped(beanie_test_db, monkeypatch):
    from pocketpaw_ee.integrations import field_options
    from pocketpaw_ee.integrations.providers.base import SettingOption

    raw = [
        {"value": " a ", "label": " Alpha "},
        {"value": "a", "label": "dup"},
        {"value": "", "label": "blank"},
        {"value": 7, "label": "not a string"},
        "junk",
        {"value": "b"},
        {"value": "x" * 501, "label": "too long"},
        *[SettingOption(value=f"v{i}", label=f"V{i}") for i in range(600)],
    ]
    _counting(monkeypatch, result=raw)
    pid = await _pocket()
    async with _client() as c:
        await _bound(c, pid)
        options = (await c.get(_options_url(pid))).json()["options"]
    assert len(options) == field_options.MAX_OPTIONS
    assert options[:3] == [
        {"value": "a", "label": "Alpha"},
        {"value": "b", "label": "b"},
        {"value": "v0", "label": "V0"},
    ]


@pytest.mark.asyncio
async def test_options_route_authz(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    state = _counting(monkeypatch)
    pid = await _pocket()
    async with _client() as c:
        await _bound(c, pid)
    async with _client(OTHER_WS, OWNER) as c:
        other = await c.get(_options_url(pid))

    async def _no_edit(*_a, **_k):
        return False

    monkeypatch.setattr(pockets_service, "has_edit_access", _no_edit)
    async with _client() as c:
        denied = await c.get(_options_url(pid))
    assert other.status_code == 404
    assert denied.status_code == 403
    assert state["calls"] == 0


# -------------------------------------------------------------------- PUT


@pytest.mark.asyncio
async def test_put_requires_a_value_from_the_remote_list(beanie_test_db, monkeypatch):
    state = _counting(monkeypatch)
    pid = await _pocket()
    async with _client() as c:
        await _bound(c, pid)
        await c.get(_options_url(pid))
        bad = await c.put(_put_url(pid), json={"settings": {"board": "brd_other"}})
        ok = await c.put(_put_url(pid), json={"settings": {"board": "brd_roadmap"}})
    assert (bad.status_code, bad.json()["error"]["code"]) == (422, "invalid_settings")
    assert ok.status_code == 200 and ok.json()["settings"]["board"] == "brd_roadmap"
    # The route and both PUTs shared one cached list.
    assert state["calls"] == 1


@pytest.mark.asyncio
async def test_put_refuses_when_the_provider_is_unreachable(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.models.integration import SiteIntegration
    from pocketpaw_ee.integrations import field_options
    from pocketpaw_ee.integrations.providers.base import ProviderError

    state = _counting(monkeypatch)
    pid = await _pocket()
    async with _client() as c:
        await _bound(c, pid)
        saved = await c.put(_put_url(pid), json={"settings": {"board": "brd_launch"}})
        field_options.clear_cache()
        state["error"] = ProviderError("down")
        changed = await c.put(_put_url(pid), json={"settings": {"board": "brd_support"}})
        # Re-sending the stored value while changing another field is not re-checked.
        same = await c.put(_put_url(pid), json={"settings": {"board": "brd_launch", "note": "hi"}})
        cleared = await c.put(_put_url(pid), json={"settings": {"board": ""}})
    assert saved.status_code == 200
    assert (changed.status_code, changed.json()["error"]["code"]) == (502, "provider_error")
    assert same.status_code == 200
    assert same.json()["settings"] == {"board": "brd_launch", "note": "hi"}
    assert cleared.status_code == 200 and "board" not in cleared.json()["settings"]
    row = await SiteIntegration.find_one(SiteIntegration.pocket_id == pid)
    assert "board" not in row.settings


@pytest.mark.asyncio
async def test_put_of_a_remote_value_without_a_connection_is_409(beanie_test_db, monkeypatch):
    state = _counting(monkeypatch)
    pid = await _pocket()
    async with _client() as c:
        resp = await c.put(_put_url(pid), json={"settings": {"board": "brd_launch"}})
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "not_connected")
    assert state["calls"] == 0


# ----------------------------------------------------------- relink, flights


async def _second_connection() -> str:
    from pocketpaw_ee.integrations import vault
    from pocketpaw_ee.integrations.providers.base import TokenGrant

    grant = TokenGrant(
        access_token="AT-2",
        refresh_token="RT-2",
        expires_in=3600,
        account_label="Other account",
        account_ref="demo-account-2",
    )
    conn = await vault.store_grant(workspace_id=WS, provider="demo", grant=grant, user_id=OWNER)
    return str(conn.id)


def _per_connection(monkeypatch, lists: dict[str, list[str]]) -> dict:
    """Each connection's account lists different boards."""
    from pocketpaw_ee.integrations.providers.base import SettingOption
    from pocketpaw_ee.integrations.providers.demo import DemoProvider

    state = {"calls": 0}

    async def _hook(self, binding, ctx, key):
        state["calls"] += 1
        return [SettingOption(v, v) for v in lists[str(ctx.connection.id)]]

    monkeypatch.setattr(DemoProvider, "field_options", _hook)
    return state


@pytest.mark.asyncio
async def test_relinking_another_connection_drops_and_rechecks_remote_values(
    beanie_test_db, monkeypatch
):
    from pocketpaw_ee.cloud.models.integration import SiteIntegration

    pid = await _pocket()
    async with _client() as c:
        first = await _bound(c, pid)
        second = await _second_connection()
        _per_connection(monkeypatch, {first: ["brd_old"], second: ["brd_new"]})
        saved = await c.put(_put_url(pid), json={"settings": {"board": "brd_old"}})
        # Same PUT moves the binding and re-sends the old account's value: refused.
        stale = await c.put(
            _put_url(pid), json={"connection_id": second, "settings": {"board": "brd_old"}}
        )
        moved = await c.put(_put_url(pid), json={"connection_id": second})
        repicked = await c.put(_put_url(pid), json={"settings": {"board": "brd_new"}})
    assert saved.json()["settings"]["board"] == "brd_old"
    assert (stale.status_code, stale.json()["error"]["code"]) == (422, "invalid_settings")
    assert moved.status_code == 200 and "board" not in moved.json()["settings"]
    row = await SiteIntegration.find_one(SiteIntegration.pocket_id == pid)
    assert repicked.status_code == 200 and row.settings["board"] == "brd_new"


@pytest.mark.asyncio
async def test_relink_in_the_oauth_callback_drops_remote_values(beanie_test_db, monkeypatch):
    from beanie import PydanticObjectId
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection, SiteIntegration
    from pocketpaw_ee.integrations import bindings

    pid = await _pocket()
    async with _client() as c:
        first = await _bound(c, pid)
        second = await _second_connection()
        _per_connection(monkeypatch, {first: ["brd_old"], second: ["brd_new"]})
        await c.put(_put_url(pid), json={"settings": {"board": "brd_old", "note": "kept"}})
    # Re-linking the same connection keeps the pick.
    conn1 = await IntegrationConnection.get(PydanticObjectId(first))
    await bindings.link_connection(workspace_id=WS, user_id=OWNER, pocket_id=pid, conn=conn1)
    row = await SiteIntegration.find_one(SiteIntegration.pocket_id == pid)
    assert row.settings["board"] == "brd_old"
    conn2 = await IntegrationConnection.get(PydanticObjectId(second))
    await bindings.link_connection(workspace_id=WS, user_id=OWNER, pocket_id=pid, conn=conn2)
    row = await SiteIntegration.find_one(SiteIntegration.pocket_id == pid)
    assert "board" not in row.settings and row.settings["note"] == "kept"


@pytest.mark.asyncio
async def test_a_required_remote_value_reads_missing_after_relink(beanie_test_db, monkeypatch):
    from pocketpaw_ee.integrations.providers.base import SettingsField
    from pocketpaw_ee.integrations.providers.demo import DemoProvider

    monkeypatch.setattr(
        DemoProvider,
        "settings_fields",
        (
            SettingsField("note", "Note", "text"),
            SettingsField("board", "Board", "select", required=True, options_source="remote"),
        ),
    )
    pid = await _pocket()
    async with _client() as c:
        first = await _bound(c, pid)
        second = await _second_connection()
        _per_connection(monkeypatch, {first: ["brd_old"], second: ["brd_new"]})
        ok = await c.put(_put_url(pid), json={"settings": {"board": "brd_old"}})
        moved = await c.put(_put_url(pid), json={"connection_id": second})
    assert ok.json()["status"] == "connected"
    assert moved.json()["status"] == "setup_required"


@pytest.mark.asyncio
async def test_concurrent_misses_share_one_provider_call(beanie_test_db, monkeypatch):
    import asyncio

    from pocketpaw_ee.integrations import bindings, field_options
    from pocketpaw_ee.integrations.providers import get_provider
    from pocketpaw_ee.integrations.providers.demo import DEMO_BOARDS, DemoProvider

    state = {"calls": 0}
    gate = asyncio.Event()

    async def _slow(self, binding, ctx, key):
        state["calls"] += 1
        await gate.wait()
        return list(DEMO_BOARDS)

    monkeypatch.setattr(DemoProvider, "field_options", _slow)
    pid = await _pocket()
    async with _client() as c:
        await _bound(c, pid)
    provider = get_provider("demo")
    row = await bindings._row(WS, pid, "demo")
    conn = await bindings.connection_for(row)
    spec = next(f for f in provider.settings_fields if f.key == "board")

    def _load():
        return field_options.load(
            provider, spec, workspace_id=WS, pocket_id=pid, user_id=OWNER, binding=row, conn=conn
        )

    waiters = [asyncio.ensure_future(_load()) for _ in range(5)]
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*waiters)
    assert state["calls"] == 1
    assert all(r == list(DEMO_BOARDS) for r in results)
    assert field_options._inflight == {}


@pytest.mark.asyncio
async def test_a_multi_remote_select_rechecks_every_item(beanie_test_db, monkeypatch):
    from pocketpaw_ee.integrations import field_options
    from pocketpaw_ee.integrations.providers.base import SettingOption, SettingsField
    from pocketpaw_ee.integrations.providers.demo import DemoProvider

    monkeypatch.setattr(
        DemoProvider,
        "settings_fields",
        (SettingsField("boards", "Boards", "select", multi=True, options_source="remote"),),
    )
    state = _counting(monkeypatch, result=[SettingOption("a", "A"), SettingOption("b", "B")])
    pid = await _pocket()
    async with _client() as c:
        await _bound(c, pid)
        saved = await c.put(_put_url(pid), json={"settings": {"boards": "a"}})
        # "a" left the account; adding "b" re-checks "a" too.
        field_options.clear_cache()
        state["result"] = [SettingOption("b", "B")]
        partial = await c.put(_put_url(pid), json={"settings": {"boards": "a,b"}})
        replaced = await c.put(_put_url(pid), json={"settings": {"boards": "b"}})
    assert saved.json()["settings"]["boards"] == "a"
    assert (partial.status_code, partial.json()["error"]["code"]) == (422, "invalid_settings")
    assert replaced.status_code == 200 and replaced.json()["settings"]["boards"] == "b"


@pytest.mark.asyncio
async def test_a_builtin_remote_select_loads_with_no_binding(beanie_test_db, monkeypatch):
    from pocketpaw_ee.integrations.providers.base import SettingOption, SettingsField
    from pocketpaw_ee.integrations.providers.newsletter import NewsletterProvider

    seen: dict = {}

    async def _hook(self, binding, ctx, key):
        seen.update(binding=binding, get_token=ctx.get_token, connection=ctx.connection, key=key)
        return [SettingOption("weekly", "Weekly")]

    monkeypatch.setattr(
        NewsletterProvider,
        "settings_fields",
        (SettingsField("cadence", "Cadence", "select", options_source="remote"),),
    )
    monkeypatch.setattr(NewsletterProvider, "field_options", _hook, raising=False)
    pid = await _pocket()
    async with _client() as c:
        resp = await c.get(_options_url(pid, provider="newsletter", key="cadence"))
        put = await c.put(_put_url(pid, "newsletter"), json={"settings": {"cadence": "weekly"}})
    assert resp.json() == {"options": [{"value": "weekly", "label": "Weekly"}]}
    assert seen == {"binding": None, "get_token": None, "connection": None, "key": "cadence"}
    assert put.status_code == 200 and put.json()["settings"] == {"cadence": "weekly"}


# ------------------------------------------------------------- draft parents


def _dependent(monkeypatch) -> dict:
    """Give demo a ``column`` select that depends on ``board``; the hook answers
    ``board`` from DEMO_BOARDS and ``column`` per the board the binding carries."""
    from pocketpaw_ee.integrations.providers.base import SettingOption, SettingsField
    from pocketpaw_ee.integrations.providers.demo import DEMO_BOARDS, DemoProvider

    state: dict = {"calls": [], "seen": []}
    monkeypatch.setattr(
        DemoProvider,
        "settings_fields",
        (
            SettingsField(key="note", label="Note", type="text", max_length=120),
            SettingsField(key="board", label="Board", type="select", options_source="remote"),
            SettingsField(
                key="column",
                label="Column",
                type="select",
                options_source="remote",
                depends_on=("board",),
            ),
        ),
    )

    async def _hook(self, binding, ctx, key):
        state["calls"].append(key)
        if key == "board":
            return list(DEMO_BOARDS)
        board = (getattr(binding, "settings", None) or {}).get("board") or ""
        state["seen"].append(board)
        return [SettingOption(f"col_{board}", f"Column of {board}")] if board else []

    monkeypatch.setattr(DemoProvider, "field_options", _hook)
    return state


@pytest.mark.asyncio
async def test_a_draft_parent_loads_the_child_list_without_saving(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.models.integration import SiteIntegration

    state = _dependent(monkeypatch)
    pid = await _pocket()
    async with _client() as c:
        await _bound(c, pid)
        saved = await c.get(_options_url(pid, key="column"))
        draft = await c.get(_options_url(pid, key="column"), params={"board": "brd_roadmap"})
    assert saved.json() == {"options": []}
    assert draft.status_code == 200, draft.text
    assert draft.json() == {
        "options": [{"value": "col_brd_roadmap", "label": "Column of brd_roadmap"}]
    }
    # The draft board was checked against the board list first.
    assert state["calls"] == ["column", "board", "column"]
    row = await SiteIntegration.find_one(SiteIntegration.provider == "demo")
    assert "board" not in (row.settings or {})


@pytest.mark.asyncio
async def test_a_draft_parent_outside_its_own_list_is_422(beanie_test_db, monkeypatch):
    state = _dependent(monkeypatch)
    pid = await _pocket()
    async with _client() as c:
        await _bound(c, pid)
        resp = await c.get(_options_url(pid, key="column"), params={"board": "brd_secret"})
    assert (resp.status_code, resp.json()["error"]["code"]) == (422, "invalid_settings")
    # The provider was never asked for the child list of an unvetted id.
    assert state["calls"] == ["board"] and state["seen"] == []


@pytest.mark.asyncio
async def test_query_keys_outside_depends_on_are_ignored(beanie_test_db, monkeypatch):
    state = _dependent(monkeypatch)
    pid = await _pocket()
    async with _client() as c:
        await _bound(c, pid)
        put = await c.put(_put_url(pid), json={"settings": {"board": "brd_launch"}})
        assert put.status_code == 200, put.text
        child = await c.get(
            _options_url(pid, key="column"), params={"note": "x", "other": "brd_roadmap"}
        )
        # A non-dependent field ignores even a would-be parent key.
        board = await c.get(_options_url(pid), params={"board": "brd_secret"})
    assert child.json()["options"] == [{"value": "col_brd_launch", "label": "Column of brd_launch"}]
    assert board.status_code == 200 and len(board.json()["options"]) == 3
    assert state["seen"] == ["brd_launch"]


@pytest.mark.asyncio
async def test_draft_and_saved_parents_cache_apart(beanie_test_db, monkeypatch):
    from pocketpaw_ee.integrations import field_options

    state = _dependent(monkeypatch)
    pid = await _pocket()
    async with _client() as c:
        conn_id = await _bound(c, pid)
        await c.put(_put_url(pid), json={"settings": {"board": "brd_launch"}})
        saved = await c.get(_options_url(pid, key="column"))
        draft = await c.get(_options_url(pid, key="column"), params={"board": "brd_roadmap"})
        again = await c.get(_options_url(pid, key="column"), params={"board": "brd_roadmap"})
        # Drafting the saved value is the saved entry: no re-check, no new load.
        same = await c.get(_options_url(pid, key="column"), params={"board": "brd_launch"})
        # A blank draft clears the parent for this load.
        cleared = await c.get(_options_url(pid, key="column"), params={"board": ""})
        saved_again = await c.get(_options_url(pid, key="column"))
    assert saved.json()["options"][0]["value"] == "col_brd_launch"
    assert draft.json()["options"][0]["value"] == "col_brd_roadmap"
    assert again.json() == draft.json()
    assert same.json() == saved.json() == saved_again.json()
    assert cleared.json() == {"options": []}
    assert state["seen"] == ["brd_launch", "brd_roadmap", ""]
    column_keys = {k for k in field_options._cache if k[4] == "column"}
    assert column_keys == {
        (WS, pid, "demo", conn_id, "column", "brd_launch"),
        (WS, pid, "demo", conn_id, "column", "brd_roadmap"),
        (WS, pid, "demo", conn_id, "column", ""),
    }
