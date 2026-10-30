# tests/ee/sites/test_native_artifact_keeps_client_bundle.py
# Created 2026-08-26 (fix/sites-preview-ships-client-js).
#
# THE BUG, reported as "the site's CSS looks broken in preview — maybe because the JS
# isn't shipped in the native artifact". It is the JS, and the cause is a dropped
# argument.
#
# `keeps_client_bundle` is the per-site declaration that a site's own client JavaScript
# is load-bearing. It is a TRI-STATE on the pocket: an explicit True/False is authorial,
# and `None` — every legacy pocket — resolves to `sites_keep_client_bundle_default`,
# which is True. Sites ship their JavaScript unless told otherwise.
#
# `publish_pocket` resolves that tri-state and threads it to the generator. The PREVIEW
# lane never asked the question at all, so its build fell to `build_generator_input`'s
# `keeps_client_bundle=False` default.
#
# Downstream that is not subtle. paw-sites' `reactPrerenderScript` branches on the flag:
# with it False it STRIPS Vite's module script and the modulepreload hints from
# dist/index.html, leaving the emitted chunk on disk unreferenced. Observed on a real
# react site: a 215KB index-*.js in dist/assets that index.html never referenced. So the
# preview rendered a JavaScript-less variant of a site that ships JavaScript when
# published — no hydration, no scroll reveals, no menu toggle — and the operator sees a
# broken page and reasonably blames the CSS.
#
# SP-2 moved the preview build out of this process into the ephemeral Daytona lane, and
# the flag did not come along, so the divergence survived that rewrite. These tests pin
# it where it now lives: on the generator_input the JOB carries, not an inline build.
#
# The flag also has to ride the artifact CONTENT HASH. Without that, flipping the
# declaration would keep serving the previously-cached variant forever — the same class
# of staleness that made this hard to see in the first place.
#
# Engine-agnostic on purpose: the lane is shared, so svelte sites that declare a client
# bundle were mis-built the same way. React is simply where it was caught.

from __future__ import annotations

from typing import Any

import pytest
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.sites import service as sites_service

_REACT_SOURCE = {
    "src/App.tsx": "export default function App() { return <main><h1>Hi</h1></main>; }\n",
    "src/index.css": ":root{--brand:#0A84FF}",
}


class _RecordingPool:
    """An arq pool that records the enqueue instead of performing it."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def enqueue_job(self, function: str, *args, _job_id: str | None = None, **kw):
        self.calls.append({"function": function, "args": args, "job_id": _job_id})
        return object()


async def _make_pocket(keeps_client_bundle: bool | None) -> str:
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id="ws1",
        owner_id="u1",
        name="Bright Smile",
        type_="site",
        pattern="landing",
        ripple_spec=None,
        engine="react",
        source=dict(_REACT_SOURCE),
        keeps_client_bundle=keeps_client_bundle,
        trusted=True,
    )
    assert err is None, err
    assert pocket_id is not None
    return pocket_id


async def _queued_site_config(pocket_id: str) -> dict[str, Any]:
    """Drive a cache MISS and return the siteConfig the QUEUED job carries.

    The generator input is positional arg 2 of the enqueue — see
    ``build_job.enqueue_preview_build``.
    """
    pool = _RecordingPool()
    await sites_service.get_native_artifact(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        builder_origin="https://dash.paw.example",
        _pool=pool,
    )
    assert pool.calls, "a cache miss must queue exactly one build"
    generator_input = pool.calls[0]["args"][2]
    return generator_input.get("siteConfig") or {}


@pytest.mark.asyncio
async def test_an_undeclared_pocket_keeps_its_client_bundle(beanie_test_db):
    """The reported bug. An undeclared pocket resolves to the settings default — True —
    exactly as publish resolves it. Before the fix nothing was passed, so the sandbox
    built with generator_client's False and stripped the module script the PUBLISHED
    site keeps."""
    pocket_id = await _make_pocket(None)

    assert (await _queued_site_config(pocket_id)).get("keepsClientBundle") is True


@pytest.mark.asyncio
async def test_an_explicit_false_still_drops_the_bundle(beanie_test_db):
    """The other direction, and it must not be lost in the fix: a site that DECLARES it
    ships no JavaScript still gets none. Passing a hardcoded True would be as wrong as
    the hardcoded False, just less visibly. The key is omitted rather than set False —
    build_generator_input only writes it when True."""
    pocket_id = await _make_pocket(False)

    assert not (await _queued_site_config(pocket_id)).get("keepsClientBundle")


@pytest.mark.asyncio
async def test_an_explicit_true_keeps_the_bundle(beanie_test_db):
    pocket_id = await _make_pocket(True)

    assert (await _queued_site_config(pocket_id)).get("keepsClientBundle") is True


def test_the_flag_rides_the_artifact_cache_key():
    """Flipping the declaration must invalidate the cached render. The two variants are
    materially different HTML — one references the module script, one has it stripped —
    so a hash blind to the flag would serve whichever was built first and make the fix
    appear not to work."""
    from pocketpaw_ee.sites.service import _artifact_content_hash

    common = {
        "source": {"src/App.tsx": "export default () => <div>hi</div>;"},
        "theme": {},
        "builder_origin": "https://dash.paw.example",
        "gen_version": "v1|abc|0.2.0|^12.40.0",
        "engine": "react",
    }

    with_js = _artifact_content_hash(keeps_client_bundle=True, **common)
    without_js = _artifact_content_hash(keeps_client_bundle=False, **common)

    assert with_js != without_js
