# tests/cloud/pockets/test_svelte_engine_field.py
# Created: 2026-06-04 (feat/sites-svelte-engine) — pins that the Paw Sites
# "Svelte track" fields persist end-to-end as first-class Pocket fields:
#   * ``engine`` ("ripple" default | "svelte") — the generation track
#   * ``source`` ({relative_path: file_contents} | None) — the svelte source map
# Threaded the same 5 layers ``pattern`` used (model -> domain -> dto wire ->
# service read/create/agent_create), so the round-trip is asserted on BOTH the
# REST create() path and the agent_create() path the create_svelte_site tool
# uses. The fields are additive: a ripple pocket reads back engine="ripple",
# source=None with NO Mongo migration.
#
# Mirrors test_pattern_field.py / test_specialist_create_stamps_type_pattern.py:
# the shared ``mongo_db`` fixture is Beanie over an in-memory mongomock-motor DB
# with ALL_DOCUMENTS registered + an autouse RecordingBus so emit() succeeds —
# no live Mongo required.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets.dto import CreatePocketRequest

pytestmark = pytest.mark.usefixtures("mongo_db")

_WS = "ws_svelte_engine"
_USER = "user_svelte_engine"

# A representative §4.3 source map (paths -> file contents). Small but real.
_SOURCE_MAP = {
    "src/routes/+page.svelte": (
        "<script>\n  import Hero from '$lib/components/Hero.svelte';\n</script>\n<Hero />\n"
    ),
    "src/routes/+layout.svelte": (
        "<script>\n  import '../app.css';\n  let { children } = $props();\n</script>\n"
        "{@render children()}\n"
    ),
    "src/routes/+page.ts": "export const prerender = true;\n",
    "src/app.css": ":root { --ink: #17130f; }\n",
    "src/lib/components/Hero.svelte": '<section class="hero"><h1>Get paid faster</h1></section>\n',
}


# ── REST create() path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_persists_engine_and_source() -> None:
    """A svelte-track pocket created via REST reads engine + source back."""
    wire = await pockets_service.create(
        _WS,
        _USER,
        CreatePocketRequest(
            name="Tally svelte site",
            type="site",
            pattern="landing",
            engine="svelte",
            source=_SOURCE_MAP,
        ),
    )
    assert wire["engine"] == "svelte"
    assert wire["source"] == _SOURCE_MAP
    assert wire["source"]["src/routes/+page.ts"] == "export const prerender = true;\n"


@pytest.mark.asyncio
async def test_engine_defaults_ripple_source_none_backcompat() -> None:
    """A pocket created without engine/source reads back engine="ripple",
    source=None — proves the change is additive (no migration)."""
    wire = await pockets_service.create(_WS, _USER, CreatePocketRequest(name="plain ripple"))
    assert wire["engine"] == "ripple"
    assert wire["source"] is None


@pytest.mark.asyncio
async def test_engine_source_survive_get_roundtrip() -> None:
    """engine + source are STORED on the doc, not just echoed from the create
    body: fetch a fresh wire dict by id and both are still present."""
    created = await pockets_service.create(
        _WS,
        _USER,
        CreatePocketRequest(
            name="Bakery svelte",
            type="site",
            pattern="landing",
            engine="svelte",
            source=_SOURCE_MAP,
        ),
    )
    fetched = await pockets_service.get(created["_id"], _USER)
    assert fetched["engine"] == "svelte"
    assert fetched["source"] == _SOURCE_MAP


# ── agent_create() path (the create_svelte_site tool's persistence) ──────────


@pytest.mark.asyncio
async def test_agent_create_stamps_engine_and_source() -> None:
    """agent_create (the path create_svelte_site uses) persists engine="svelte"
    + the source map, with ripple_spec=None and trusted=True."""
    view, pocket_id, err = await pockets_service.agent_create(
        workspace_id=_WS,
        owner_id=_USER,
        name="Bright Smile Svelte",
        type_="site",
        pattern="landing",
        ripple_spec=None,
        engine="svelte",
        source=_SOURCE_MAP,
        trusted=True,
    )
    assert err is None
    assert pocket_id is not None
    assert view is not None
    assert view["type"] == "site"
    assert view["pattern"] == "landing"
    assert view["engine"] == "svelte"
    assert view["source"] == _SOURCE_MAP


@pytest.mark.asyncio
async def test_agent_create_engine_source_survive_get_roundtrip() -> None:
    """The svelte fields are stored on the doc via agent_create: fetch a fresh
    wire dict by id and both are still present."""
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id=_WS,
        owner_id=_USER,
        name="Salon Svelte",
        type_="site",
        pattern="landing",
        ripple_spec=None,
        engine="svelte",
        source=_SOURCE_MAP,
        trusted=True,
    )
    assert err is None
    assert pocket_id is not None
    fetched = await pockets_service.get(pocket_id, _USER)
    assert fetched["engine"] == "svelte"
    assert fetched["source"]["src/routes/+layout.svelte"].startswith("<script>")


@pytest.mark.asyncio
async def test_agent_create_engine_defaults_ripple_backcompat() -> None:
    """An agent_create with no engine/source keeps today's behaviour:
    engine="ripple", source=None. Additive."""
    _min_spec = {
        "version": "1.0",
        "state": {},
        "ui": {"id": "n_root0001", "type": "flex", "props": {}, "children": []},
    }
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id=_WS,
        owner_id=_USER,
        name="Plain ripple pocket",
        ripple_spec=_min_spec,
    )
    assert err is None
    assert pocket_id is not None
    fetched = await pockets_service.get(pocket_id, _USER)
    assert fetched["engine"] == "ripple"
    assert fetched["source"] is None
