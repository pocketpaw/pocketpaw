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
#
# Updated 2026-06-17 (feat/sites-svelte-component-edit, SE-2b): added coverage
# that build() threads an optional ``builder_origin`` into
# ``siteConfig.builderOrigin``. The paw-sites generator (SE-1) gates the
# editable section anchors + the postMessage edit-bridge on this field, so a
# site is only editable when it carries a builderOrigin. It is OMITTED from the
# payload when not set, so non-editable publishes keep the exact prior wire
# bytes.
#
# Updated 2026-06-04: added test_svelte_result_shape_no_ripple_version, a
# regression for an integration KeyError. A's real generator returns
# ``{"projectDir", "engine"}`` for svelte (NO ``rippleVersion`` — types.ts §4.2),
# but build() used to read ``gen["rippleVersion"]`` unconditionally, so every real
# svelte publish raised ``KeyError: 'rippleVersion'`` before deploy. The other
# fakes here returned a ripple-shaped result (with ``rippleVersion``) for BOTH
# tracks, which masked it. This test pins the REAL svelte output shape so the
# regression can't return.
#
# Updated 2026-07-08 (DP0-1 — per-tenant D1 id plumbing): build() now ALWAYS
# threads an optional ``d1_database_id`` onto ``siteConfig.d1DatabaseId`` (default
# "" for a static site), which the paw-sites generator bakes into the emitted
# wrangler.toml ``database_id``. Two tests pin it: a supplied id lands on
# ``siteConfig.d1DatabaseId``, and an omitted one defaults to "".
#
# Updated 2026-06-21 (feat/dsv-5-svelte-dynamic-brain): DSV-5 — a DYNAMIC svelte
# pocket carries its live-data bindings (objects/sources/actions/auth) as SIBLING
# keys on the same ``source`` envelope as the files. build() must SPLIT the
# envelope before sending: the file map goes on ``input.source`` and the bindings
# are spread as FLAT siblings on the GenerateInput (the exact shape DSV-1's
# parseBindings reads — ``input.objects`` / ``input.sources`` / ``input.actions`` /
# ``input.auth``, NOT nested inside ``source``). A STATIC svelte pocket has no
# binding keys, so the split is a no-op and ``input.source`` is the unchanged file
# map (the existing test_svelte_build_sends_source_not_ripple_spec still asserts
# ``sent["source"] == _SOURCE_MAP`` byte-for-byte). The added tests pin both the
# dynamic split and the static no-op.

from __future__ import annotations

import pytest
from pocketpaw_ee.sites.generator_client import BuildResult, GeneratorClient

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


@pytest.mark.asyncio
async def test_builder_origin_is_threaded_into_site_config() -> None:
    """SE-2b: build(builder_origin=...) puts it on siteConfig.builderOrigin so
    the paw-sites generator injects the gated edit-bridge. A site is only
    editable when this field is present."""
    runner = _CapturingRunner()
    client = GeneratorClient(_runner=runner)
    await client.build(
        engine="svelte",
        source=_SOURCE_MAP,
        ripple_spec=None,
        theme={},
        site_id="site_sv",
        title="Tally",
        capture_api_base="https://api.paw.example",
        capture_signed_key="pp_tok_x",
        builder_origin="https://app.paw.example",
    )
    sent = runner.input_json
    assert sent is not None
    assert sent["siteConfig"]["builderOrigin"] == "https://app.paw.example"


@pytest.mark.asyncio
async def test_d1_database_id_is_threaded_into_site_config() -> None:
    """DP0-1: build(d1_database_id=...) puts it on siteConfig.d1DatabaseId so the
    paw-sites generator threads it into the emitted wrangler.toml database_id."""
    runner = _CapturingRunner()
    client = GeneratorClient(_runner=runner)
    await client.build(
        engine="svelte",
        source=_SOURCE_MAP,
        ripple_spec=None,
        theme={},
        site_id="site_sv",
        title="Guestbook",
        capture_api_base="https://api.paw.example",
        capture_signed_key="pp_tok_x",
        d1_database_id="abc123",
    )
    sent = runner.input_json
    assert sent is not None
    assert sent["siteConfig"]["d1DatabaseId"] == "abc123"


@pytest.mark.asyncio
async def test_d1_database_id_defaults_to_empty_when_omitted() -> None:
    """A static site (no d1_database_id) still carries the key, defaulting to "" —
    the prior empty value the generator bakes into wrangler.toml database_id."""
    runner = _CapturingRunner()
    client = GeneratorClient(_runner=runner)
    await client.build(
        engine="svelte",
        source=_SOURCE_MAP,
        ripple_spec=None,
        theme={},
        site_id="site_sv",
        title="Tally",
        capture_api_base="https://api.paw.example",
        capture_signed_key="pp_tok_x",
        # d1_database_id omitted
    )
    sent = runner.input_json
    assert sent is not None
    assert sent["siteConfig"]["d1DatabaseId"] == ""


@pytest.mark.asyncio
async def test_builder_origin_omitted_when_not_set() -> None:
    """A normal (non-editable) publish carries NO builderOrigin key — the wire
    payload is byte-identical to before SE-2b, so the generator does not inject
    the bridge."""
    runner = _CapturingRunner()
    client = GeneratorClient(_runner=runner)
    await client.build(
        engine="svelte",
        source=_SOURCE_MAP,
        ripple_spec=None,
        theme={},
        site_id="site_sv",
        title="Tally",
        capture_api_base="https://api.paw.example",
        capture_signed_key="pp_tok_x",
        # builder_origin omitted
    )
    sent = runner.input_json
    assert sent is not None
    assert "builderOrigin" not in sent["siteConfig"]


class _SvelteShapeRunner:
    """Fake runner returning the EXACT shape A's generator emits for svelte:
    ``{"projectDir", "engine"}`` with NO ``rippleVersion`` (paw-sites types.ts
    §4.2 — no ripple runtime ships on this track). Distinct from
    ``_CapturingRunner``, which returns a ripple-shaped result for both tracks."""

    async def generate(self, input_json: dict, out_dir: str) -> dict:
        return {"projectDir": "/tmp/site_sv", "engine": "svelte"}

    async def install(self, project_dir: str) -> tuple[bool, str]:
        return True, "ok"

    async def smoke(self, project_dir: str) -> tuple[bool, str]:
        return True, "ok"


@pytest.mark.asyncio
async def test_svelte_result_shape_no_ripple_version() -> None:
    """Regression: build() must accept the real svelte GenerateResult, which
    omits ``rippleVersion``. The old ``gen["rippleVersion"]`` subscript raised
    ``KeyError`` here, crashing every svelte publish before deploy. build() now
    reads it defensively, so a svelte build returns a BuildResult with
    ``ripple_version=None`` instead of raising."""
    client = GeneratorClient(_runner=_SvelteShapeRunner())
    result = await client.build(
        engine="svelte",
        source=_SOURCE_MAP,
        ripple_spec=None,
        theme={},
        site_id="site_sv",
        title="Tally",
        capture_api_base="https://api.paw.example",
        capture_signed_key="pp_tok_x",
    )
    assert isinstance(result, BuildResult)
    assert result.project_dir == "/tmp/site_sv"
    assert result.ripple_version is None


# --------------------------------------------------------------------------- #
# DSV-5 — dynamic svelte pocket: bindings split out of source onto GenerateInput
# --------------------------------------------------------------------------- #

# A DYNAMIC svelte source envelope: the §4.3 files PLUS the live-data bindings as
# sibling keys on the SAME dict (the DSV-5 storage contract).
_DYNAMIC_OBJECTS = [
    {
        "name": "entry",
        "fields": {"id": "text", "name": "text", "message": "text"},
        "primaryKey": "id",
    }
]
_DYNAMIC_SOURCES = [
    {"name": "entries", "kind": "data", "object": "entry", "refresh": "pocket_open"}
]
_DYNAMIC_ACTIONS = [{"name": "sign", "object": "entry", "op": "insert"}]
_DYNAMIC_ENVELOPE = {
    **_SOURCE_MAP,
    "objects": _DYNAMIC_OBJECTS,
    "sources": _DYNAMIC_SOURCES,
    "actions": _DYNAMIC_ACTIONS,
    "auth": True,
}


@pytest.mark.asyncio
async def test_dynamic_svelte_splits_bindings_onto_generate_input() -> None:
    """DSV-5: a dynamic svelte pocket's bindings are spread as FLAT siblings on
    the GenerateInput (the DSV-1 parseBindings shape), and ``input.source`` carries
    ONLY the {path: contents} files — never the binding keys (materializeSource
    would otherwise try to write a binding key as a file)."""
    runner = _CapturingRunner()
    client = GeneratorClient(_runner=runner)
    await client.build(
        engine="svelte",
        source=_DYNAMIC_ENVELOPE,
        ripple_spec=None,
        theme={},
        site_id="site_dyn",
        title="Guestbook",
        capture_api_base="https://api.paw.example",
        capture_signed_key="pp_tok_x",
    )
    sent = runner.input_json
    assert sent is not None
    # The bindings ride as FLAT siblings on the input — exactly what parseBindings
    # reads (input.objects / input.sources / input.actions / input.auth).
    assert sent["objects"] == _DYNAMIC_OBJECTS
    assert sent["sources"] == _DYNAMIC_SOURCES
    assert sent["actions"] == _DYNAMIC_ACTIONS
    assert sent["auth"] is True
    # ``source`` carries ONLY the files — the binding keys are peeled OUT of it.
    assert sent["source"] == _SOURCE_MAP
    for k in ("objects", "sources", "actions", "auth"):
        assert k not in sent["source"]
    # Still the svelte fork — no rippleSpec.
    assert sent["engine"] == "svelte"
    assert "rippleSpec" not in sent


@pytest.mark.asyncio
async def test_static_svelte_passes_no_bindings() -> None:
    """A STATIC svelte pocket (no binding keys on source) sends NO binding siblings
    and ``input.source`` is the unchanged file map — the split is a no-op, so the
    wire bytes match the pre-DSV-5 payload exactly."""
    runner = _CapturingRunner()
    client = GeneratorClient(_runner=runner)
    await client.build(
        engine="svelte",
        source=_SOURCE_MAP,
        ripple_spec=None,
        theme={},
        site_id="site_static",
        title="Tally",
        capture_api_base="https://api.paw.example",
        capture_signed_key="pp_tok_x",
    )
    sent = runner.input_json
    assert sent is not None
    # No binding siblings added.
    for k in ("objects", "sources", "actions", "auth"):
        assert k not in sent
    # source is the unchanged file map.
    assert sent["source"] == _SOURCE_MAP


@pytest.mark.asyncio
async def test_partial_bindings_only_present_keys_are_passed() -> None:
    """A read-only dynamic site (objects + sources, no actions, no auth) passes
    ONLY the present binding keys — absent keys are not synthesized onto the input
    (so the generator's parseBindings sees exactly what was authored)."""
    runner = _CapturingRunner()
    client = GeneratorClient(_runner=runner)
    envelope = {**_SOURCE_MAP, "objects": _DYNAMIC_OBJECTS, "sources": _DYNAMIC_SOURCES}
    await client.build(
        engine="svelte",
        source=envelope,
        ripple_spec=None,
        theme={},
        site_id="site_ro",
        title="Read only",
        capture_api_base="https://api.paw.example",
        capture_signed_key="pp_tok_x",
    )
    sent = runner.input_json
    assert sent is not None
    assert sent["objects"] == _DYNAMIC_OBJECTS
    assert sent["sources"] == _DYNAMIC_SOURCES
    # Not authored → not on the input.
    assert "actions" not in sent
    assert "auth" not in sent
    assert sent["source"] == _SOURCE_MAP


def test_split_svelte_source_unit() -> None:
    """The pure splitter: files vs bindings, with an empty/None envelope handled."""
    from pocketpaw_ee.sites.generator_client import _split_svelte_source

    files, bindings = _split_svelte_source(_DYNAMIC_ENVELOPE)
    assert files == _SOURCE_MAP
    assert bindings == {
        "objects": _DYNAMIC_OBJECTS,
        "sources": _DYNAMIC_SOURCES,
        "actions": _DYNAMIC_ACTIONS,
        "auth": True,
    }
    # Static map → no bindings, files unchanged.
    f2, b2 = _split_svelte_source(_SOURCE_MAP)
    assert f2 == _SOURCE_MAP
    assert b2 == {}
    # None → empty, empty.
    f3, b3 = _split_svelte_source(None)
    assert f3 == {} and b3 == {}
