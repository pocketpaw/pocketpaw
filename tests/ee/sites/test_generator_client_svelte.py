# tests/ee/sites/test_generator_client_svelte.py
# Created: 2026-06-04 (feat/sites-svelte-engine) — pins the §4.2 integration
# contract GeneratorClient.build() emits to paw-sites-gen, the surface
# pocketpaw and paw-sites MUST agree on byte-for-byte:
#   * svelte: { "engine": "svelte", "source": {<path>: <contents>, ...}, ... }
#             — ``source`` PRESENT, ``rippleSpec`` ABSENT.
#   * ripple: { "engine": "ripple", "rippleSpec": {...}, ... }
#             — ``rippleSpec`` PRESENT, ``source`` ABSENT (engine tag added).
# The fake runner captures the exact input_json so we assert the dict the real
# generator would parse, without spawning bun/node/workerd. siteConfig + theme
# ride both tracks unchanged; install + smoke stay track-agnostic.

from __future__ import annotations

import pytest
from pocketpaw_ee.sites.generator_client import GeneratorClient

_SOURCE_MAP = {
    "src/routes/+page.svelte": (
        "<script>import Hero from '$lib/components/Hero.svelte';</script><Hero />"
    ),
    "src/routes/+layout.svelte": "<script>import '../app.css';</script>",
    "src/routes/+page.ts": "export const prerender = true;",
    "src/app.css": ":root{}",
    "src/lib/components/Hero.svelte": "<section><h1>hi</h1></section>",
}


class _CapturingRunner:
    """Fake runner that records the input_json build() hands generate(), so the
    §4.2 wire contract can be asserted without spawning bun/workerd."""

    def __init__(self) -> None:
        self.input_json: dict | None = None
        self.calls: list[str] = []

    async def generate(self, input_json: dict, out_dir: str) -> dict:
        self.calls.append("generate")
        self.input_json = input_json
        return {"projectDir": "/tmp/site", "rippleVersion": "0.2.0"}

    async def install(self, project_dir: str) -> tuple[bool, str]:
        self.calls.append("install")
        return True, "ok"

    async def smoke(self, project_dir: str) -> tuple[bool, str]:
        self.calls.append("smoke")
        return True, "ok"


@pytest.mark.asyncio
async def test_svelte_build_sends_source_not_ripple_spec() -> None:
    """§4.2 svelte payload: engine='svelte', source present, rippleSpec ABSENT."""
    runner = _CapturingRunner()
    client = GeneratorClient(_runner=runner)
    await client.build(
        engine="svelte",
        source=_SOURCE_MAP,
        ripple_spec=None,
        theme={"primary": "#0A84FF"},
        site_id="site_sv",
        title="Tally",
        capture_api_base="https://api.paw.example",
        capture_signed_key="pp_tok_x",
    )
    sent = runner.input_json
    assert sent is not None
    # The fork: engine tag + source, and rippleSpec must NOT be in the payload.
    assert sent["engine"] == "svelte"
    assert sent["source"] == _SOURCE_MAP
    assert "rippleSpec" not in sent
    # siteConfig rides the svelte track unchanged.
    assert sent["siteConfig"]["siteId"] == "site_sv"
    assert sent["siteConfig"]["title"] == "Tally"
    assert sent["siteConfig"]["captureApiBase"] == "https://api.paw.example"
    # Generation still runs install + smoke (track-agnostic, fail-closed gate).
    assert runner.calls == ["generate", "install", "smoke"]


@pytest.mark.asyncio
async def test_ripple_build_sends_ripple_spec_with_engine_tag_no_source() -> None:
    """§4.2 ripple payload: engine='ripple', rippleSpec present, source ABSENT.
    The ripple path 'just gains engine:ripple and is otherwise unchanged'."""
    runner = _CapturingRunner()
    client = GeneratorClient(_runner=runner)
    await client.build(
        ripple_spec={"version": "1.0", "ui": {"type": "flex"}},
        theme={"primary": "#0A84FF"},
        site_id="site_rp",
        title="Dashboard",
        capture_api_base="https://api.paw.example",
        capture_signed_key="pp_tok_y",
    )
    sent = runner.input_json
    assert sent is not None
    assert sent["engine"] == "ripple"
    assert sent["rippleSpec"] == {"version": "1.0", "ui": {"type": "flex"}}
    assert "source" not in sent
    assert sent["siteConfig"]["siteId"] == "site_rp"


@pytest.mark.asyncio
async def test_engine_defaults_to_ripple_when_unspecified() -> None:
    """build() with no engine kwarg keeps the legacy ripple behaviour: the
    payload carries engine='ripple' + rippleSpec, never source."""
    runner = _CapturingRunner()
    client = GeneratorClient(_runner=runner)
    await client.build(
        ripple_spec={"version": "1.0"},
        theme={},
        site_id="s",
        title="t",
        capture_api_base="x",
        capture_signed_key="k",
    )
    sent = runner.input_json
    assert sent is not None
    assert sent["engine"] == "ripple"
    assert "rippleSpec" in sent
    assert "source" not in sent
