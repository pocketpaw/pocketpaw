# tests/ee/sites/test_react_component_edit.py — the react-track EDIT lane
# (sites_service.edit_react_component). Created: 2026-08-11 (feat/sites-react-edit-lane,
# RX-3).
#
# WHAT WAS MISSING. ``create_react_site`` shipped in RX-2 and worked; nothing could
# change a react site afterwards. The only edit entry point was svelte-gated at three
# layers, so the chat agent's only available move on "shorten the hero headline" was to
# call ``create_react_site`` a second time — minting a SECOND site pocket and leaving
# the one the user was looking at untouched. These tests pin the lane that closes it.
#
# They use the shared ``beanie_test_db`` fixture (an in-memory Mongo) so the pockets
# service persists a REAL react Pocket doc and the guards run against real state. There
# is no generator, no Cloudflare and no bundle reader to inject, because there is no
# build: the whole contract is "resolve the edit and persist the draft".
#
# THREE PROPERTIES ARE LOAD BEARING and get their own sections below:
#   (a) DRAFT-ONLY — the edit must not publish and must not enqueue a build. Asserted
#       both by spying on the publish/enqueue seams AND by the observable that no Site
#       doc exists afterwards. This is NOT a copy of the svelte contract: svelte
#       republishes and rolls back on a smoke failure, which react cannot do because
#       ``build_runs_async("react")`` is True and a react publish returns before any
#       build outcome exists.
#   (b) THE RESERVED-PATH GUARD — an edit must not be a way around the create tool's
#       allowlist. ``create=True`` on ``package.json`` would write the dependency
#       manifest, defeating the generator's dependency allowlist and with it the
#       supply-chain release-age floor that manifest is what enforces. Every spelling
#       the normalizer is supposed to collapse is tested, because a guard a trivial
#       path spelling defeats is not a guard.
#   (c) THE CREATE/EXISTS INVERSION — ``create=False`` demands the path exist (a typo
#       is never a silent create); ``create=True`` demands it not (an accidental
#       overwrite of a real component is worse than a rejected call).
#
# Mutation coverage: tests/mutations/react_edit_lane.json.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
from pocketpaw_ee.cloud.models.site import Site as _SiteDoc
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.sites import service as sites_service

_HERO_V1 = (
    "export default function Hero() {\n"
    "  return (\n"
    "    <section className='hero'>\n"
    "      <h1>Bright Smile Dental</h1>\n"
    "      <p>Gentle care in the heart of town.</p>\n"
    "    </section>\n"
    "  );\n"
    "}\n"
)
_APP = (
    "import Hero from './components/Hero';\n\n"
    "export default function App() {\n"
    "  return (\n    <main>\n      <Hero />\n    </main>\n  );\n}\n"
)
_REACT_SOURCE = {
    "src/App.tsx": _APP,
    "src/components/Hero.tsx": _HERO_V1,
    "src/index.css": ":root{--brand:#0A84FF}",
    "public/favicon.svg": "<svg/>",
}


async def _make_react_pocket(workspace_id: str, user_id: str) -> str:
    """Persist a real react-engine Pocket via the pockets service and return its id.

    Mirrors how ``create_react_site`` lands one: type='site', pattern='landing',
    engine='react', source=<map>, trusted=True."""
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id=workspace_id,
        owner_id=user_id,
        name="Bright Smile",
        type_="site",
        pattern="landing",
        ripple_spec=None,
        engine="react",
        source=dict(_REACT_SOURCE),
        trusted=True,
    )
    assert err is None, err
    assert pocket_id is not None
    return pocket_id


async def _source_of(pocket_id: str, user_id: str = "u1") -> dict:
    wire = await pockets_service.get(pocket_id, user_id)
    return wire["source"]


# ---------------------------------------------------------------------------
# The happy paths — a targeted diff and a full rewrite
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_targeted_edits_are_applied_and_persisted(beanie_test_db):
    """The dominant case: the agent sends only the diff and the file changes.

    THE MUTATION THAT BREAKS THIS: make the persist a no-op (drop the
    ``set_react_source_file`` call). Run: the re-read still had the old headline.
    """
    pocket_id = await _make_react_pocket("ws1", "u1")

    out = await sites_service.edit_react_component(
        user_id="u1",
        pocket_id=pocket_id,
        component_path="src/components/Hero.tsx",
        edits=[{"old_string": "Bright Smile Dental", "new_string": "Bright Smile"}],
    )

    assert out == {
        "pocket_id": pocket_id,
        "component_path": "src/components/Hero.tsx",
        "created": False,
    }
    source = await _source_of(pocket_id)
    assert "<h1>Bright Smile</h1>" in source["src/components/Hero.tsx"]
    # Only the targeted file moved — the rest of the map came through verbatim.
    assert source["src/App.tsx"] == _APP
    assert source["src/index.css"] == ":root{--brand:#0A84FF}"


@pytest.mark.asyncio
async def test_new_source_replaces_the_whole_file(beanie_test_db):
    """The large-rewrite form: ``new_source`` is used as-is, not merged."""
    pocket_id = await _make_react_pocket("ws1", "u1")
    rewritten = "export default function Hero() {\n  return <section>New</section>;\n}\n"

    await sites_service.edit_react_component(
        user_id="u1",
        pocket_id=pocket_id,
        component_path="src/components/Hero.tsx",
        new_source=rewritten,
    )

    source = await _source_of(pocket_id)
    assert source["src/components/Hero.tsx"] == rewritten


@pytest.mark.asyncio
async def test_edit_writes_a_reviewable_draft_version(beanie_test_db):
    """The edit leaves a Branch draft snapshotting the FULL edited map, so a later
    publish has something to promote. This is what makes "draft-only" a draft rather
    than an unreviewable in-place mutation."""
    pocket_id = await _make_react_pocket("ws1", "u1")

    await sites_service.edit_react_component(
        user_id="u1",
        pocket_id=pocket_id,
        component_path="src/components/Hero.tsx",
        edits=[{"old_string": "Gentle care", "new_string": "Gentle, unhurried care"}],
    )

    from pocketpaw_ee.versions import service as versions

    draft = await versions.get_draft(scope_type="pocket", scope_id=pocket_id)
    assert draft is not None
    assert "Gentle, unhurried care" in draft.content["src/components/Hero.tsx"]
    # The snapshot is the whole map, not just the edited file.
    assert draft.content["src/App.tsx"] == _APP


# ---------------------------------------------------------------------------
# (a) DRAFT-ONLY — no publish, no build enqueued
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_does_not_publish_and_does_not_enqueue_a_build(beanie_test_db, monkeypatch):
    """The contract that separates this lane from ``edit_svelte_component``.

    A react publish ENQUEUES a Daytona build and returns before any outcome exists,
    so there is nothing synchronous to roll back from and a republish here would
    spend a sandbox on every keystroke-sized edit. Persisting the draft is the whole
    job.

    Asserted two ways on purpose: spies prove the seams were never called, and the
    empty Site collection proves it at the level of observable state — a publish, by
    any route, upserts a Site doc.

    THE MUTATION THAT BREAKS THIS: append a ``publish_pocket(...)`` call to
    ``edit_react_component``. Run: the spy fired and this failed.
    """
    pocket_id = await _make_react_pocket("ws1", "u1")

    called: list[str] = []

    async def _no_publish(**_kw):
        called.append("publish_pocket")
        raise AssertionError("edit_react_component must not publish")

    async def _no_enqueue(**_kw):
        called.append("_enqueue_static_build")
        raise AssertionError("edit_react_component must not enqueue a build")

    def _no_prewarm(**_kw):
        called.append("_schedule_native_prewarm")
        raise AssertionError("edit_react_component must not pre-warm a svelte artifact")

    monkeypatch.setattr(sites_service, "publish_pocket", _no_publish)
    monkeypatch.setattr(sites_service, "_enqueue_static_build", _no_enqueue)
    monkeypatch.setattr(sites_service, "_schedule_native_prewarm", _no_prewarm)

    await sites_service.edit_react_component(
        user_id="u1",
        pocket_id=pocket_id,
        component_path="src/components/Hero.tsx",
        new_source="export default function Hero() { return <section/>; }\n",
    )

    assert called == []
    # No Site doc was minted or touched — a publish (live or preview) upserts one.
    assert await _SiteDoc.find_all().to_list() == []


# ---------------------------------------------------------------------------
# (b) The reserved-path guard
# ---------------------------------------------------------------------------

# Every spelling the normalizer must collapse. The plain names are the four files
# the generator owns; the rest are the evasions — a leading ``./``, Windows
# separators, and a ``..`` round trip through the reserved namespace. If any of
# these lands, the create tool's allowlist has a back door.
_RESERVED_SPELLINGS = [
    "package.json",
    "./package.json",
    "index.html",
    "vite.config.ts",
    "paw-prerender.mjs",
    "src/paw/entry.tsx",
    "src\\paw\\entry.tsx",
    "src/paw/../paw/entry.tsx",
    "src/components/../paw/entry-client.tsx",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _RESERVED_SPELLINGS)
async def test_every_reserved_path_spelling_is_rejected(beanie_test_db, path: str):
    """An edit may not write a generator-owned path, however it is spelled.

    ``package.json`` is the one that matters most: it is the dependency manifest, so
    writing it defeats the generator's dependency allowlist and with it the
    supply-chain release-age floor. The others carry the prerender contract — remove
    ``paw-prerender.mjs`` and the page ships blank without JavaScript, which is the
    thing this engine exists to refuse.

    ``create=True`` is used deliberately: it is the STRONGER attack (an
    existence-checked edit would also have to guess a path already in the map), so
    proving it fails proves the weaker one does.

    THE MUTATION THAT BREAKS THIS: drop the ``react_path_rejection`` call from
    ``edit_react_component``. Run: the write landed and this failed.
    """
    pocket_id = await _make_react_pocket("ws1", "u1")

    with pytest.raises(ValidationError) as exc:
        await sites_service.edit_react_component(
            user_id="u1",
            pocket_id=pocket_id,
            component_path=path,
            new_source='{"dependencies": {"evil": "*"}}',
            create=True,
        )
    assert exc.value.code == "site_edit.reserved_path", exc.value.code
    # Nothing was written under any spelling of the key.
    source = await _source_of(pocket_id)
    assert set(source) == set(_REACT_SOURCE)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "styles.css",
        "../../etc/passwd",
        "/etc/passwd",
        "src/../secrets.env",
        "lib/components/Hero.tsx",
    ],
)
async def test_path_outside_src_and_public_is_rejected(beanie_test_db, path: str):
    """The positive half of the policy: authored files live under ``src/`` or
    ``public/``. Everything else at the project root belongs to the generated shell,
    and a ``..`` escape or an absolute path is not a project path at all.

    THE MUTATION THAT BREAKS THIS: widen ``REACT_AUTHORABLE_PREFIXES`` to ``("",)``.
    Run: every path became authorable and this failed.
    """
    pocket_id = await _make_react_pocket("ws1", "u1")

    with pytest.raises(ValidationError) as exc:
        await sites_service.edit_react_component(
            user_id="u1",
            pocket_id=pocket_id,
            component_path=path,
            new_source="nope",
            create=True,
        )
    assert exc.value.code == "site_edit.path_outside_source", exc.value.code
    assert set(await _source_of(pocket_id)) == set(_REACT_SOURCE)


@pytest.mark.asyncio
async def test_the_guard_runs_before_the_pocket_is_read(beanie_test_db):
    """A reserved path is rejected on the path ALONE — no pocket state can make it
    land. Proven with a pocket id that does not exist: a guard that ran after the
    read would raise NotFound instead."""
    with pytest.raises(ValidationError) as exc:
        await sites_service.edit_react_component(
            user_id="u1",
            pocket_id="0123456789abcdef01234567",
            component_path="package.json",
            new_source="{}",
            create=True,
        )
    assert exc.value.code == "site_edit.reserved_path"


# ---------------------------------------------------------------------------
# (c) The create/exists inversion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_adds_a_new_component_file(beanie_test_db):
    """The reason ``create`` exists: "add a testimonials section" needs a NEW file
    plus an ``src/App.tsx`` edit, and the write chokepoint refuses any path not
    already in the map. Without this the agent cannot add a section at all."""
    pocket_id = await _make_react_pocket("ws1", "u1")
    testimonials = "export default function Testimonials() { return <section/>; }\n"

    out = await sites_service.edit_react_component(
        user_id="u1",
        pocket_id=pocket_id,
        component_path="src/components/Testimonials.tsx",
        new_source=testimonials,
        create=True,
    )

    assert out["created"] is True
    source = await _source_of(pocket_id)
    assert source["src/components/Testimonials.tsx"] == testimonials
    # The rest of the map is intact — a create is additive.
    assert source["src/components/Hero.tsx"] == _HERO_V1


@pytest.mark.asyncio
async def test_missing_path_is_not_found_when_create_is_false(beanie_test_db):
    """A typo must never be a silent create. This is the default mode, so it is the
    one a mistyped path actually hits.

    THE MUTATION THAT BREAKS THIS: make ``create`` default to True. Run: the typo'd
    path was created and no NotFound was raised.
    """
    pocket_id = await _make_react_pocket("ws1", "u1")

    with pytest.raises(NotFound):
        await sites_service.edit_react_component(
            user_id="u1",
            pocket_id=pocket_id,
            component_path="src/components/Heroo.tsx",
            new_source="export default function Heroo() { return null; }\n",
        )
    assert "src/components/Heroo.tsx" not in await _source_of(pocket_id)


@pytest.mark.asyncio
async def test_missing_path_with_edits_is_not_found_not_a_key_error(beanie_test_db):
    """The ``edits`` branch INDEXES the source map to read the current contents, so
    the existence check has to run in the sites service and not only at the write
    chokepoint. Without it a typo'd path is a bare ``KeyError`` that the MCP handler
    can only report as "edit failed: 'src/...'" — the agent cannot tell a missing
    component from a broken backend, so it cannot fix the path and retry.

    This is the test the ``new_source`` case does NOT cover: with a full rewrite the
    chokepoint's own NotFound arrives first, so the sites-service check looks
    redundant right up until the diff path needs it.

    THE MUTATION THAT BREAKS THIS: neuter the ``not create and component_path not in
    source_map`` check in ``edit_react_component``. Run: a KeyError surfaced instead
    of NotFound and this failed.
    """
    pocket_id = await _make_react_pocket("ws1", "u1")

    with pytest.raises(NotFound):
        await sites_service.edit_react_component(
            user_id="u1",
            pocket_id=pocket_id,
            component_path="src/components/Heroo.tsx",
            edits=[{"old_string": "Bright", "new_string": "Brighter"}],
        )


@pytest.mark.asyncio
async def test_existing_path_is_rejected_when_create_is_true(beanie_test_db):
    """``create`` INVERTS the existence check rather than relaxing it: an accidental
    overwrite of a real component is worse than a rejected call the agent can retry
    without the flag.

    THE MUTATION THAT BREAKS THIS: drop the ``create and exists`` branch. Run: the
    existing Hero was silently overwritten and no error was raised.
    """
    pocket_id = await _make_react_pocket("ws1", "u1")

    with pytest.raises(ValidationError) as exc:
        await sites_service.edit_react_component(
            user_id="u1",
            pocket_id=pocket_id,
            component_path="src/components/Hero.tsx",
            new_source="clobbered",
            create=True,
        )
    assert exc.value.code == "pocket.react_component_exists"
    assert (await _source_of(pocket_id))["src/components/Hero.tsx"] == _HERO_V1


@pytest.mark.asyncio
async def test_create_without_new_source_is_rejected(beanie_test_db):
    """There is nothing for ``edits`` to search against in a file that does not
    exist yet, so ``create`` requires the full contents."""
    pocket_id = await _make_react_pocket("ws1", "u1")

    with pytest.raises(ValidationError) as exc:
        await sites_service.edit_react_component(
            user_id="u1",
            pocket_id=pocket_id,
            component_path="src/components/New.tsx",
            edits=[{"old_string": "a", "new_string": "b"}],
            create=True,
        )
    assert exc.value.code == "site_edit.create_needs_source"


# ---------------------------------------------------------------------------
# Argument + engine guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_both_edits_and_new_source_is_rejected(beanie_test_db):
    """Exactly one edit shape. Accepting both would leave which one wins to
    whichever branch happened to run last."""
    pocket_id = await _make_react_pocket("ws1", "u1")

    with pytest.raises(ValidationError) as exc:
        await sites_service.edit_react_component(
            user_id="u1",
            pocket_id=pocket_id,
            component_path="src/components/Hero.tsx",
            new_source="whole file",
            edits=[{"old_string": "Bright", "new_string": "Brighter"}],
        )
    assert exc.value.code == "site_edit.invalid_args"
    assert (await _source_of(pocket_id))["src/components/Hero.tsx"] == _HERO_V1


@pytest.mark.asyncio
async def test_neither_edits_nor_new_source_is_rejected(beanie_test_db):
    """A call with no change in it is a bug in the caller, not an empty success."""
    pocket_id = await _make_react_pocket("ws1", "u1")

    with pytest.raises(ValidationError) as exc:
        await sites_service.edit_react_component(
            user_id="u1",
            pocket_id=pocket_id,
            component_path="src/components/Hero.tsx",
        )
    assert exc.value.code == "site_edit.invalid_args"


@pytest.mark.asyncio
async def test_ambiguous_old_string_is_rejected_and_nothing_is_written(beanie_test_db):
    """The uniqueness contract comes from the SHARED ``apply_edits`` (there is
    deliberately no second copy of that logic), and it fires BEFORE the persist —
    an ambiguous diff leaves the file byte-identical rather than replacing an
    arbitrary one of the matches."""
    pocket_id = await _make_react_pocket("ws1", "u1")

    with pytest.raises(ValidationError) as exc:
        await sites_service.edit_react_component(
            user_id="u1",
            pocket_id=pocket_id,
            # "section" appears in both the opening and closing tag.
            component_path="src/components/Hero.tsx",
            edits=[{"old_string": "section", "new_string": "div"}],
        )
    assert exc.value.code == "site_edit.ambiguous_match"
    assert (await _source_of(pocket_id))["src/components/Hero.tsx"] == _HERO_V1


@pytest.mark.asyncio
async def test_svelte_pocket_is_rejected(beanie_test_db):
    """The react edit lane must not touch a svelte site's source map — the engines
    have different content models and different publish contracts (svelte
    republishes synchronously; react cannot)."""
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id="ws1",
        owner_id="u1",
        name="Svelte Site",
        type_="site",
        pattern="landing",
        ripple_spec=None,
        engine="svelte",
        source={"src/routes/+page.svelte": "<h1>hi</h1>"},
        trusted=True,
    )
    assert err is None, err

    with pytest.raises(ValidationError) as exc:
        await sites_service.edit_react_component(
            user_id="u1",
            pocket_id=pocket_id,
            component_path="src/routes/+page.svelte",
            new_source="<h1>changed</h1>",
        )
    assert exc.value.code == "pocket.not_react_site"


@pytest.mark.asyncio
async def test_ripple_pocket_is_rejected(beanie_test_db):
    """A ripple pocket has no source map at all — the guard must reject it rather
    than dereference ``None``."""
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id="ws1",
        owner_id="u1",
        name="Ripple Site",
        type_="site",
        pattern="landing",
        ripple_spec={"version": 1, "ui": {"type": "text", "props": {"text": "hi"}}},
        trusted=True,
    )
    assert err is None, err

    with pytest.raises(ValidationError) as exc:
        await sites_service.edit_react_component(
            user_id="u1",
            pocket_id=pocket_id,
            component_path="src/App.tsx",
            new_source="export default function App() { return null; }\n",
        )
    assert exc.value.code == "pocket.not_react_site"


@pytest.mark.asyncio
async def test_missing_pocket_raises_not_found(beanie_test_db):
    """A missing pocket id raises NotFound (the pockets service's public ``get``
    owns that), mapped by the MCP handler to an is_error."""
    with pytest.raises(NotFound):
        await sites_service.edit_react_component(
            user_id="u1",
            pocket_id="0123456789abcdef01234567",
            component_path="src/components/Hero.tsx",
            new_source="export default function Hero() { return null; }\n",
        )


# ---------------------------------------------------------------------------
# The write chokepoint enforces the same rules for every caller
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pockets_service_rejects_a_non_react_pocket(beanie_test_db):
    """``set_react_source_file`` is the chokepoint, so it owns the guards for ANY
    caller — not just for the one orchestration path above."""
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id="ws1",
        owner_id="u1",
        name="Svelte Site",
        type_="site",
        pattern="landing",
        ripple_spec=None,
        engine="svelte",
        source={"src/routes/+page.svelte": "<h1>hi</h1>"},
        trusted=True,
    )
    assert err is None, err

    with pytest.raises(ValidationError) as exc:
        await pockets_service.set_react_source_file(
            pocket_id,
            "u1",
            component_path="src/routes/+page.svelte",
            new_source="<h1>no</h1>",
        )
    assert exc.value.code == "pocket.not_react_site"


@pytest.mark.asyncio
async def test_pockets_service_enforces_the_create_inversion(beanie_test_db):
    """Both halves of the inversion, at the chokepoint rather than the orchestration
    layer, so a future second caller inherits them."""
    pocket_id = await _make_react_pocket("ws1", "u1")

    with pytest.raises(NotFound):
        await pockets_service.set_react_source_file(
            pocket_id, "u1", component_path="src/components/Nope.tsx", new_source="x"
        )
    with pytest.raises(ValidationError) as exc:
        await pockets_service.set_react_source_file(
            pocket_id,
            "u1",
            component_path="src/components/Hero.tsx",
            new_source="x",
            create=True,
        )
    assert exc.value.code == "pocket.react_component_exists"


@pytest.mark.asyncio
async def test_pockets_service_emits_pocket_updated(beanie_test_db, _recording_bus_for_sites):
    """Cloud rule 9 — a state-mutating service function emits. A silent source
    mutation desyncs the search index, soul memory and ripple invalidation."""
    pocket_id = await _make_react_pocket("ws1", "u1")
    _recording_bus_for_sites.events.clear()

    await pockets_service.set_react_source_file(
        pocket_id,
        "u1",
        component_path="src/components/Hero.tsx",
        new_source="export default function Hero() { return <section/>; }\n",
    )

    kinds = [type(e).__name__ for e in _recording_bus_for_sites.events]
    assert "PocketUpdated" in kinds, kinds
