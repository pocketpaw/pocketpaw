# tests/ee/sites/test_keeps_client_bundle.py — MT-1: an interactive site keeps its
# own JavaScript. Created: 2026-08-07 (feat/sites-keep-client-bundle).
#
# A published Paw Site is generated with ``csr = false``, and on the ripple track the
# post-build prune then deletes the emitted hydration dirs — so an author's own client
# JS never runs in the browser: no ``onMount``, no ``use:`` action, no
# IntersectionObserver scroll-reveal, no WebGL canvas. The paw-sites generator (this
# slice's sibling change) reads ``siteConfig.keepsClientBundle`` to emit ``csr = true``
# and mark the route manifest.
#
# These tests pin the pocketpaw HALF of that cross-repo contract: the exact wire shape
# ``GeneratorClient.build()`` hands ``paw-sites-gen``. The fake runner captures the
# input_json so the payload is asserted without spawning bun/node/workerd (the
# _CapturingRunner pattern from test_generator_client_svelte.py).
#
# The load-bearing test here is the REGRESSION one: a build that does not declare the
# flag must emit a byte-identical siteConfig — the key is OMITTED, never sent as
# False. That follows SE-2b's ``builderOrigin`` (omit-when-unset) rather than DP0-1's
# ``d1DatabaseId`` (always-present), because the generator treats an absent flag as
# false and every existing static site must keep its exact prior payload.

from __future__ import annotations

import pytest
from pocketpaw_ee.sites.generator_client import GeneratorClient

_SOURCE_MAP = {
    "src/routes/+page.svelte": "<script>import { reveal } from '$lib/reveal.js';</script>",
    "src/routes/+layout.svelte": "<script>import '../app.css';</script>",
    "src/routes/+page.ts": "export const prerender = true;",
    "src/app.css": ":root{}",
    "src/lib/reveal.js": "export function reveal(node) { new IntersectionObserver(() => {}); }",
}

# The siteConfig a build emitted BEFORE MT-1. A site that declares nothing must still
# send exactly these keys — no keepsClientBundle, in any form.
_PRE_MT1_SITE_CONFIG_KEYS = {
    "siteId",
    "title",
    "captureApiBase",
    "captureSignedKey",
    "d1DatabaseId",
}


class _CapturingRunner:
    """Records the input_json build() hands generate(), so the wire contract can be
    asserted without spawning bun/workerd."""

    def __init__(self) -> None:
        self.input_json: dict | None = None

    async def generate(self, input_json: dict, out_dir: str) -> dict:
        self.input_json = input_json
        return {"projectDir": "/tmp/site", "rippleVersion": "0.2.0"}

    async def install(self, project_dir: str) -> tuple[bool, str]:
        return True, "ok"

    async def smoke(self, project_dir: str) -> tuple[bool, str]:
        return True, "ok"


async def _build(**kw) -> dict:
    """Run a build through a capturing runner and return the sent payload."""
    runner = _CapturingRunner()
    client = GeneratorClient(_runner=runner)
    await client.build(
        theme={},
        site_id="site_mt1",
        title="Motion",
        capture_api_base="https://api.paw.example",
        capture_signed_key="pp_tok_x",
        **kw,
    )
    assert runner.input_json is not None
    return runner.input_json


@pytest.mark.asyncio
async def test_declared_flag_rides_site_config_on_svelte() -> None:
    """build(keeps_client_bundle=True) puts keepsClientBundle on siteConfig so the
    generator emits csr=true and the author's own JS actually runs."""
    sent = await _build(
        engine="svelte", source=_SOURCE_MAP, ripple_spec=None, keeps_client_bundle=True
    )
    assert sent["siteConfig"]["keepsClientBundle"] is True


@pytest.mark.asyncio
async def test_declared_flag_rides_site_config_on_ripple() -> None:
    """The flag is a per-SITE fact, not an engine one — it rides the ripple payload
    the same way. (On ripple it is also what stops the post-build prune from deleting
    the hydration bundle.)"""
    sent = await _build(ripple_spec={"type": "container"}, keeps_client_bundle=True)
    assert sent["engine"] == "ripple"
    assert sent["siteConfig"]["keepsClientBundle"] is True


@pytest.mark.asyncio
async def test_omitted_flag_leaves_the_payload_byte_identical() -> None:
    """THE REGRESSION GUARD: a site that declares nothing sends the exact pre-MT-1
    siteConfig — the key is absent, not present-and-False. A False would still be a
    changed payload for every existing static site."""
    sent = await _build(ripple_spec={"type": "container"})
    site_config = sent["siteConfig"]
    assert "keepsClientBundle" not in site_config
    assert set(site_config) == _PRE_MT1_SITE_CONFIG_KEYS


@pytest.mark.asyncio
async def test_explicit_false_is_also_omitted() -> None:
    """Passing the flag explicitly False is the same as not passing it — nothing is
    added to the payload, so a caller that always forwards a stored bool cannot
    accidentally change the wire bytes of every static site."""
    sent = await _build(
        engine="svelte", source=_SOURCE_MAP, ripple_spec=None, keeps_client_bundle=False
    )
    assert "keepsClientBundle" not in sent["siteConfig"]
    assert set(sent["siteConfig"]) == _PRE_MT1_SITE_CONFIG_KEYS


@pytest.mark.asyncio
async def test_flag_composes_with_the_other_site_config_fields() -> None:
    """The flag is additive: it does not disturb the builderOrigin (SE-2b) or
    d1DatabaseId (DP0-1) fields that share siteConfig."""
    sent = await _build(
        engine="svelte",
        source=_SOURCE_MAP,
        ripple_spec=None,
        keeps_client_bundle=True,
        builder_origin="https://app.paw.example",
        d1_database_id="abc123",
    )
    site_config = sent["siteConfig"]
    assert site_config["keepsClientBundle"] is True
    assert site_config["builderOrigin"] == "https://app.paw.example"
    assert site_config["d1DatabaseId"] == "abc123"
    # The source map still rides the svelte track untouched.
    assert sent["source"] == _SOURCE_MAP
