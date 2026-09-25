# tests/ee/sites/test_author_dependencies_sandbox_only.py — author npm packages never
# install on the API host, and ``set_site_dependencies`` writes the manifest (PP-1).
#
# Created: 2026-09-24 (feat/sites-author-dependencies). Three layers of the
# sandbox-only rule, outermost first:
#   1. ``build_runs_async`` sends a static svelte pocket with a manifest to the
#      sandbox lane even with the SL-4 flag OFF, and ``_deploy_site_doc`` follows it;
#   2. ``edit_svelte_component`` skips its local preview build for such a pocket;
#   3. ``GeneratorClient`` refuses the host build outright (``HostInstallRefused``),
#      and ``_build_or_cloud_error`` maps that to a 422, not a toolchain 500.
# Then the writer: resolve → manifest → draft version, removes, and refusals.
# The mutations that break these tests are in tests/mutations/sites_author_dependencies.json.

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.sites import dependency_manifest as dm
from pocketpaw_ee.sites import dependency_resolver as dr
from pocketpaw_ee.sites import generator_client as gc
from pocketpaw_ee.sites import service as sites_service

_MANIFEST = dm.render_manifest({"three": {"version": "0.170.0"}})

_SVELTE_SOURCE = {
    "src/routes/+page.svelte": (
        "<script>import Hero from '$lib/components/Hero.svelte'</script><Hero/>"
    ),
    "src/routes/+layout.svelte": "<script>import '../app.css'</script><slot/>",
    "src/routes/+page.ts": "export const prerender = true",
    "src/app.css": ":root{--brand:#0A84FF}",
    "src/lib/components/Hero.svelte": "<h1>Hi</h1>",
}


def _with_manifest(source: dict) -> dict:
    return {**source, dm.DEPENDENCY_MANIFEST_PATH: _MANIFEST}


# ---------------------------------------------------------------------------
# 3. The chokepoint: the host generator client refuses
# ---------------------------------------------------------------------------


class _RecordingRunner:
    def __init__(self, project_dir: str) -> None:
        self.project_dir = project_dir
        self.calls: list[str] = []

    async def generate(self, input_json, out_dir):
        self.calls.append("generate")
        return {"projectDir": self.project_dir}

    async def install(self, project_dir):
        self.calls.append("install")
        return True, ""

    async def build_static(self, project_dir, *, gate):
        self.calls.append("build_static")
        return True, ""

    async def smoke(self, project_dir):
        self.calls.append("smoke")
        return True, ""


def _build_kwargs(engine: str, source: dict) -> dict:
    return dict(
        theme={},
        site_id="s1",
        title="t",
        capture_api_base="http://x",
        capture_signed_key="k",
        engine=engine,
        source=source,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["svelte", "react"])
async def test_the_host_client_refuses_to_install_author_packages(tmp_path, engine):
    """Nothing runs — not even generate — for a node build that declares packages.

    Mutation: delete the ``HostInstallRefused`` raise in ``_build_one``.
    """
    runner = _RecordingRunner(str(tmp_path))
    client = gc.GeneratorClient(_runner=runner)
    with pytest.raises(gc.HostInstallRefused):
        await client.build(**_build_kwargs(engine, _with_manifest({"src/App.tsx": "x"})))
    assert runner.calls == []


@pytest.mark.asyncio
async def test_an_unreadable_manifest_is_refused_too(tmp_path):
    runner = _RecordingRunner(str(tmp_path))
    client = gc.GeneratorClient(_runner=runner)
    with pytest.raises(gc.HostInstallRefused):
        await client.build(**_build_kwargs("svelte", {"paw.dependencies.json": "{oops"}))
    assert "install" not in runner.calls


@pytest.mark.asyncio
async def test_a_source_without_packages_still_builds_on_the_host(tmp_path):
    """Control: the guard is keyed on the manifest, not on the engine."""
    runner = _RecordingRunner(str(tmp_path))
    client = gc.GeneratorClient(_runner=runner)
    await client.build(**_build_kwargs("svelte", dict(_SVELTE_SOURCE)))
    assert runner.calls == ["generate", "install", "build_static"]


@pytest.mark.asyncio
async def test_an_empty_manifest_is_not_a_declaration(tmp_path):
    runner = _RecordingRunner(str(tmp_path))
    client = gc.GeneratorClient(_runner=runner)
    source = {**_SVELTE_SOURCE, "paw.dependencies.json": dm.render_manifest({})}
    await client.build(**_build_kwargs("svelte", source))
    assert "install" in runner.calls


def test_host_install_refused_is_not_a_smoke_failure():
    """The edit lane rolls back on SmokeGateFailed; a routing refusal is not a bad edit."""
    assert not issubclass(gc.HostInstallRefused, gc.SmokeGateFailed)


@pytest.mark.asyncio
async def test_build_or_cloud_error_maps_the_refusal_to_a_422():
    class _Refusing:
        async def build(self, **_kw):
            raise gc.HostInstallRefused("sandbox only")

    with pytest.raises(CloudError) as info:
        await sites_service._build_or_cloud_error(_Refusing(), map_smoke_gate=False)
    assert info.value.code == "sites.author_dependencies_need_sandbox"
    assert info.value.status_code == 422


# ---------------------------------------------------------------------------
# 1. Routing: the sandbox lane, whatever the SL-4 flag says
# ---------------------------------------------------------------------------


@pytest.fixture
def lane_flag_off(monkeypatch):
    monkeypatch.delenv("PAW_SITES_SVELTE_ASYNC_BUILD", raising=False)


def test_static_svelte_with_packages_goes_to_the_sandbox_lane_flag_off(lane_flag_off):
    """Mutation: delete the PP-1 early return in ``build_runs_async``."""
    assert sites_service.build_runs_async("svelte", source=_with_manifest(_SVELTE_SOURCE)) is True
    # Control: the same pocket without packages stays inline with the flag off.
    assert sites_service.build_runs_async("svelte", source=dict(_SVELTE_SOURCE)) is False


def test_dynamic_svelte_and_html_are_not_rerouted(lane_flag_off):
    dynamic = {**_with_manifest(_SVELTE_SOURCE), "sources": [{"kind": "data", "object": "x"}]}
    assert sites_service.build_runs_async("svelte", source=dynamic) is False
    assert (
        sites_service.build_runs_async(
            "svelte", source=_with_manifest(_SVELTE_SOURCE), pattern="dynamic"
        )
        is False
    )
    assert (
        sites_service.build_runs_async("html", source=_with_manifest({"index.html": "x"})) is False
    )


@pytest.mark.asyncio
async def test_an_inline_publish_of_such_a_pocket_enqueues_instead_of_building(
    lane_flag_off, monkeypatch
):
    enqueue = AsyncMock(return_value="queued-doc")
    monkeypatch.setattr(sites_service, "_enqueue_static_build", enqueue)

    class _MustNotBuild:
        async def build(self, **_kw):
            raise AssertionError("the host build ran")

    out = await sites_service._deploy_site_doc(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="p1",
        site_id="s1",
        signed_key="k",
        site_name="n",
        ripple_spec=None,
        theme={},
        engine="svelte",
        source=_with_manifest(_SVELTE_SOURCE),
        pattern="landing",
        generator=_MustNotBuild(),
    )
    assert out == "queued-doc"
    enqueue.assert_awaited_once()


# ---------------------------------------------------------------------------
# 2. The edit lane's local preview build is skipped
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def recording_bus():
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod

    class _Bus:
        async def publish(self, event) -> None:
            return None

        def subscribe(self, event_type, handler) -> None:  # noqa: ARG002
            return None

    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus_mod._bus = _Bus()  # type: ignore[attr-defined]
    yield
    bus_mod._bus = prev  # type: ignore[attr-defined]


async def _make_pocket(engine: str, source: dict) -> str:
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id="ws1",
        owner_id="u1",
        name="Site",
        type_="site",
        pattern="landing",
        ripple_spec=None,
        engine=engine,
        source=source,
        trusted=True,
    )
    assert err is None and pocket_id
    return pocket_id


@pytest.mark.asyncio
async def test_edit_svelte_component_skips_the_local_build_for_a_pocket_with_packages(
    beanie_test_db,
):
    """Mutation: delete the PP-1 seam branch → the generator is called."""
    pocket_id = await _make_pocket("svelte", _with_manifest(_SVELTE_SOURCE))

    class _MustNotBuild:
        async def build(self, **_kw):
            raise AssertionError("the local preview build ran on the API host")

    doc, unreferenced = await sites_service.edit_svelte_component(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        component_path="src/lib/components/Hero.svelte",
        new_source="<h1>Hello</h1>",
        _generator=_MustNotBuild(),
    )
    assert doc is None
    assert unreferenced is False
    wire = await pockets_service.get(pocket_id, "u1")
    assert wire["source"]["src/lib/components/Hero.svelte"] == "<h1>Hello</h1>"


@pytest.mark.asyncio
async def test_edit_svelte_component_still_refuses_to_write_the_manifest(beanie_test_db):
    pocket_id = await _make_pocket("svelte", dict(_SVELTE_SOURCE))
    with pytest.raises(CloudError) as info:
        await sites_service.edit_svelte_component(
            workspace_id="ws1",
            user_id="u1",
            pocket_id=pocket_id,
            component_path="paw.dependencies.json",
            new_source=_MANIFEST,
            create=True,
        )
    assert info.value.code == "site_edit.reserved_path"


# ---------------------------------------------------------------------------
# The writer: set_site_dependencies
# ---------------------------------------------------------------------------


def _fake_resolve(packages: dict[str, dict], rejected: list[dr.Rejection] | None = None):
    calls: list[tuple] = []

    async def resolve(requests, engine, *, already_declared=()):
        calls.append(([r.name for r in requests], engine, sorted(already_declared)))
        result = dr.ResolveResult()
        for req in requests:
            if req.name in packages:
                result.packages[req.name] = dr.ResolvedPackage(name=req.name, **packages[req.name])
        result.rejected = list(rejected or [])
        return result

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


@pytest.mark.asyncio
async def test_add_writes_a_canonical_manifest_and_a_draft_version(beanie_test_db):
    pocket_id = await _make_pocket("svelte", dict(_SVELTE_SOURCE))
    resolve = _fake_resolve({"three": {"version": "0.170.0"}})

    out = await sites_service.set_site_dependencies(
        user_id="u1", pocket_id=pocket_id, add=[{"name": "three"}], _resolve=resolve
    )

    assert out == {
        "pocket_id": pocket_id,
        "packages": {"three": {"version": "0.170.0"}},
        "rejected": [],
        "changed": True,
    }
    assert resolve.calls == [(["three"], "svelte", [])]
    wire = await pockets_service.get(pocket_id, "u1")
    assert json.loads(wire["source"]["paw.dependencies.json"]) == {
        "schema": 1,
        "packages": {"three": {"version": "0.170.0"}},
    }
    from pocketpaw_ee.versions import service as versions

    draft = await versions.get_draft(scope_type="pocket", scope_id=pocket_id)
    assert draft is not None and "paw.dependencies.json" in draft.content


@pytest.mark.asyncio
async def test_html_entries_keep_esm_and_integrity(beanie_test_db):
    pocket_id = await _make_pocket("html", {"index.html": "<p>hi</p>"})
    resolve = _fake_resolve(
        {"three": {"version": "0.170.0", "esm": "https://e/+esm", "integrity": "sha384-x"}}
    )
    await sites_service.set_site_dependencies(
        user_id="u1", pocket_id=pocket_id, add=["three"], _resolve=resolve
    )
    wire = await pockets_service.get(pocket_id, "u1")
    entry = json.loads(wire["source"]["paw.dependencies.json"])["packages"]["three"]
    assert entry == {"version": "0.170.0", "esm": "https://e/+esm", "integrity": "sha384-x"}


@pytest.mark.asyncio
async def test_removing_the_last_package_removes_the_file(beanie_test_db):
    pocket_id = await _make_pocket(
        "react", {"src/App.tsx": "x", **{dm.DEPENDENCY_MANIFEST_PATH: _MANIFEST}}
    )
    out = await sites_service.set_site_dependencies(
        user_id="u1", pocket_id=pocket_id, remove=["three"], _resolve=_fake_resolve({})
    )
    assert out["packages"] == {} and out["changed"] is True
    wire = await pockets_service.get(pocket_id, "u1")
    assert "paw.dependencies.json" not in wire["source"]


@pytest.mark.asyncio
async def test_rejections_pass_through_and_change_nothing(beanie_test_db):
    pocket_id = await _make_pocket("svelte", dict(_SVELTE_SOURCE))
    rej = dr.Rejection("svelte", dr.TOOLCHAIN_RESERVED, "provided already")
    out = await sites_service.set_site_dependencies(
        user_id="u1",
        pocket_id=pocket_id,
        add=[{"name": "svelte"}],
        remove=["nope"],
        _resolve=_fake_resolve({}, [rej]),
    )
    assert out["changed"] is False
    assert {r["code"] for r in out["rejected"]} == {"not_declared", dr.TOOLCHAIN_RESERVED}
    wire = await pockets_service.get(pocket_id, "u1")
    assert "paw.dependencies.json" not in wire["source"]


@pytest.mark.asyncio
async def test_existing_packages_count_toward_the_cap(beanie_test_db):
    pocket_id = await _make_pocket("svelte", _with_manifest(_SVELTE_SOURCE))
    resolve = _fake_resolve({"gsap": {"version": "3.12.5"}})
    out = await sites_service.set_site_dependencies(
        user_id="u1", pocket_id=pocket_id, add=[{"name": "gsap"}], _resolve=resolve
    )
    assert resolve.calls == [(["gsap"], "svelte", ["three"])]
    assert set(out["packages"]) == {"three", "gsap"}


@pytest.mark.asyncio
async def test_a_ripple_site_is_refused(beanie_test_db):
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id="ws1",
        owner_id="u1",
        name="Landing",
        type_="site",
        pattern="landing",
        ripple_spec=None,
        trusted=True,
    )
    assert err is None
    with pytest.raises(CloudError) as info:
        await sites_service.set_site_dependencies(
            user_id="u1", pocket_id=pocket_id, add=["three"], _resolve=_fake_resolve({})
        )
    assert info.value.code == "site_deps.engine_unsupported"
