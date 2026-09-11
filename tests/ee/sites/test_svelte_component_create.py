# tests/ee/sites/test_svelte_component_create.py — the svelte-track CREATE lane
# (sites_service.edit_svelte_component(..., create=True)). Created: 2026-09-11
# (feat/sites-svelte-edit-create, SC-1).
#
# WHAT WAS MISSING. ``edit_svelte_component`` shipped first of the three edit lanes
# and could only ever REWRITE a file that already existed: both the sites service and
# ``set_svelte_source_file`` raised NotFound on an unknown ``component_path``,
# described there as "not a silent create". React (RX-3) and html (HE-10) each grew a
# ``create`` flag afterwards for exactly this reason; svelte never did. So "add an
# about page" to a LIVE svelte site was unanswerable — and because the svelte tool
# description also lacked the "NEVER call create_svelte_site again" warning its two
# siblings carry, the agent's remaining move was a second ``create_svelte_site``: a
# second pocket at a second url, leaving the site the user was looking at untouched.
#
# These tests live in their own file rather than in test_component_edit.py because
# that module is about the EDIT + republish contract; this is a new capability with
# its own guard, its own inversion and its own rollback shape. Same split react and
# html already have.
#
# FOUR PROPERTIES ARE LOAD BEARING and get their own sections below:
#   (a) THE HEADLINE CASE — a published svelte site can gain a second ROUTE. On this
#       track a page is TWO files (``+page.svelte`` plus the ``+page.ts`` carrying
#       its own prerender flag, because the root page's flag is page-level and does
#       not cascade), so adding one is three calls counting the nav link.
#   (b) THE PATH GUARD — new with ``create``. While the lane could only overwrite
#       existing keys, every writable path had been vetted when create_svelte_site
#       landed the map; a minted path has not been. And a reserved path does not
#       merely fail, it fails in the worst place: svelte-scaffold.ts THROWS at
#       materialize time, which is NOT a SmokeGateFailed, so it would escape the
#       rollback and leave the pocket permanently unpublishable.
#   (c) THE CREATE/EXISTS INVERSION — ``create=False`` demands the path exist (a typo
#       is never a silent create); ``create=True`` demands it not (an accidental
#       overwrite of a real component is worse than a rejected call).
#   (d) THE ROLLBACK SHAPE — a create has no previous contents, so a failed one must
#       REMOVE the key. Restoring "" would leave an empty file at a real route.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core.errors import CloudError, NotFound
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.generator_client import SmokeGateFailed

_HERO_V1 = "<section class='hero'><h1>Bright Smile</h1></section>"
_HERO_V2 = "<section class='hero'><h1>Brighter Smiles, Whiter Teeth</h1></section>"
_SVELTE_SOURCE = {
    "src/routes/+page.svelte": (
        "<script>import Hero from '$lib/components/Hero.svelte'</script><Hero/>"
    ),
    "src/routes/+layout.svelte": "<script>import '../app.css'</script><slot/>",
    "src/routes/+page.ts": "export const prerender = true",
    "src/app.css": ":root{--brand:#0A84FF}",
    "src/lib/components/Hero.svelte": _HERO_V1,
}


class _FakeGenerator:
    """Records the source map it was asked to build so a test can assert the created
    file reached the regenerated build."""

    def __init__(self):
        self.built = None

    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        self.built = kw
        return BuildResult(project_dir="/tmp/site", ripple_version=None)


class _SmokeFailGenerator:
    """Stands in for a generator whose workerd smoke render fails — how
    ``GeneratorClient.build`` signals a broken site (it raises BEFORE any deploy)."""

    async def build(self, **kw):
        raise SmokeGateFailed("workerd SSR failure: document is not defined")


class _FakeCF:
    def __init__(self):
        self.put_calls = []

    async def put_worker(self, *, script_name, bundle, bindings=None):
        self.put_calls.append(script_name)
        return True


def _fake_local_deploy(site_id: str, project_dir: str) -> str:
    return f"http://127.0.0.1:9999/{site_id}/"


async def _make_svelte_pocket(workspace_id: str, user_id: str) -> str:
    """Persist a real svelte-engine Pocket via the pockets service and return its id.
    Mirrors how ``create_svelte_site`` lands a pocket."""
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id=workspace_id,
        owner_id=user_id,
        name="Bright Smile",
        type_="site",
        pattern="landing",
        ripple_spec=None,
        engine="svelte",
        source=dict(_SVELTE_SOURCE),
        trusted=True,
    )
    assert err is None, err
    assert pocket_id is not None
    return pocket_id


async def _edit(pocket_id: str, **kw):
    """Call the lane with the build seams stubbed, so no Bun / workerd / Cloudflare
    is touched. ``_generator`` may be overridden per-test."""
    kw.setdefault("_generator", _FakeGenerator())
    kw.setdefault("_cloudflare", _FakeCF())
    kw.setdefault("_local_deploy", _fake_local_deploy)
    return await sites_service.edit_svelte_component(
        workspace_id="w1", user_id="u1", pocket_id=pocket_id, **kw
    )


# ── (a) the headline case ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_adds_a_new_page_to_a_live_svelte_site(beanie_test_db):
    """The repro: a published svelte site gains a second ROUTE.

    Before SC-1 the first call raised NotFound("site_component") and there was no
    other tool on the sites server that would accept it.
    """
    pocket_id = await _make_svelte_pocket("w1", "u1")

    await _edit(
        pocket_id,
        component_path="src/routes/about/+page.svelte",
        new_source="<h1>About us</h1>",
        create=True,
    )
    await _edit(
        pocket_id,
        component_path="src/routes/about/+page.ts",
        new_source="export const prerender = true",
        create=True,
    )

    pocket = await pockets_service.get(pocket_id, "u1")
    assert pocket["source"]["src/routes/about/+page.svelte"] == "<h1>About us</h1>"
    assert "prerender" in pocket["source"]["src/routes/about/+page.ts"]
    # The pre-existing files are untouched — a create adds, it does not rewrite.
    assert pocket["source"]["src/lib/components/Hero.svelte"] == _HERO_V1


@pytest.mark.asyncio
async def test_the_created_file_reaches_the_regenerated_build(beanie_test_db):
    """Persisting is not enough — the new route has to be in the map the generator
    is handed, or the page exists on the pocket and nowhere else."""
    pocket_id = await _make_svelte_pocket("w1", "u1")
    gen = _FakeGenerator()

    await _edit(
        pocket_id,
        component_path="src/routes/about/+page.svelte",
        new_source="<h1>About us</h1>",
        create=True,
        _generator=gen,
    )

    assert gen.built is not None
    built_source = gen.built.get("source") or {}
    assert built_source.get("src/routes/about/+page.svelte") == "<h1>About us</h1>"


@pytest.mark.asyncio
async def test_create_adds_a_new_section_component(beanie_test_db):
    """The other shape of the two-call contract: a component, wired in by an import
    rather than a link."""
    pocket_id = await _make_svelte_pocket("w1", "u1")

    await _edit(
        pocket_id,
        component_path="src/lib/components/Testimonials.svelte",
        new_source="<section>Lovely people</section>",
        create=True,
    )
    await _edit(
        pocket_id,
        component_path="src/routes/+page.svelte",
        edits=[
            {
                "old_string": "<Hero/>",
                "new_string": (
                    "<Hero/>\n<script>import Testimonials from "
                    "'$lib/components/Testimonials.svelte'</script>\n<Testimonials/>"
                ),
            }
        ],
    )

    pocket = await pockets_service.get(pocket_id, "u1")
    assert "Testimonials" in pocket["source"]["src/routes/+page.svelte"]


# ── (b) the path guard ──────────────────────────────────────────────────────

_BS = chr(92)  # a literal backslash, built rather than written so no quoting layer
# between the editor and the file can eat it.

_RESERVED_SPELLINGS = [
    "src/lib/paw/helpers.ts",
    "src/lib/paw/../paw/helpers.ts",
    _BS.join(["src", "lib", "paw", "helpers.ts"]),
    "./src/lib/paw/helpers.ts",
    "src/hooks.server.ts",
    "src/lib/auth.ts",
    "src/app.d.ts",
    "package.json",
    "./package.json",
    "src/../package.json",
    "vite.config.ts",
    "svelte.config.js",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _RESERVED_SPELLINGS)
async def test_every_reserved_path_spelling_is_rejected(beanie_test_db, path: str):
    """A generator-owned path is refused however it is spelled.

    A guard a trivial path spelling defeats is not a guard, so each spelling the
    normalizer is supposed to collapse gets its own case.
    """
    pocket_id = await _make_svelte_pocket("w1", "u1")

    with pytest.raises(CloudError) as exc:
        await _edit(pocket_id, component_path=path, new_source="whatever", create=True)
    assert exc.value.code == "site_edit.reserved_path"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["README.md", "../etc/passwd", "src/../../escape.ts", "/abs/path.ts"]
)
async def test_path_outside_src_is_rejected(beanie_test_db, path: str):
    pocket_id = await _make_svelte_pocket("w1", "u1")

    with pytest.raises(CloudError) as exc:
        await _edit(pocket_id, component_path=path, new_source="whatever", create=True)
    assert exc.value.code == "site_edit.path_outside_source"


@pytest.mark.asyncio
async def test_the_guard_runs_before_the_pocket_is_read(beanie_test_db):
    """A reserved path is rejected on the path ALONE, so no pocket state can make it
    land — asserted by the verdict coming back for a pocket that does not exist."""
    with pytest.raises(CloudError) as exc:
        await _edit(
            "pocket-that-does-not-exist",
            component_path="package.json",
            new_source="{}",
            create=True,
        )
    assert exc.value.code == "site_edit.reserved_path"


@pytest.mark.asyncio
async def test_a_reserved_path_writes_nothing(beanie_test_db):
    """The rejection is not merely an error code — the map must be untouched, which
    is the property that keeps the scaffold's materialize-time throw unreachable."""
    pocket_id = await _make_svelte_pocket("w1", "u1")

    with pytest.raises(CloudError):
        await _edit(
            pocket_id,
            component_path="src/lib/paw/evil.ts",
            new_source="export const x = 1",
            create=True,
        )

    pocket = await pockets_service.get(pocket_id, "u1")
    assert set(pocket["source"]) == set(_SVELTE_SOURCE)


# ── (c) the create/exists inversion ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_onto_an_existing_path_is_rejected(beanie_test_db):
    """Silently overwriting a real component the agent thought it was adding is
    worse than a rejected call it can retry."""
    pocket_id = await _make_svelte_pocket("w1", "u1")

    with pytest.raises(CloudError) as exc:
        await _edit(
            pocket_id,
            component_path="src/lib/components/Hero.svelte",
            new_source="<section>clobbered</section>",
            create=True,
        )
    assert exc.value.code == "pocket.svelte_component_exists"

    pocket = await pockets_service.get(pocket_id, "u1")
    assert pocket["source"]["src/lib/components/Hero.svelte"] == _HERO_V1


@pytest.mark.asyncio
async def test_a_missing_path_without_create_is_still_not_found(beanie_test_db):
    """The other half of the inversion, and the pre-SC-1 contract: a typo'd path is
    an error, never a stray new file."""
    pocket_id = await _make_svelte_pocket("w1", "u1")

    with pytest.raises(NotFound):
        await _edit(
            pocket_id,
            component_path="src/lib/components/Heroo.svelte",
            new_source="<p/>",
        )


@pytest.mark.asyncio
async def test_a_missing_path_with_edits_is_not_found_not_a_key_error(beanie_test_db):
    """The sites service keeps its OWN existence check, separate from the write
    chokepoint's, because the ``edits`` branch indexes the source map.

    Worth its own case next to the ``new_source`` one above: there, removing the
    service-level check changes nothing observable (the pockets service still raises
    NotFound at the write). Here it turns a relayable NotFound into a bare KeyError,
    which the agent cannot act on.
    """
    pocket_id = await _make_svelte_pocket("w1", "u1")

    with pytest.raises(NotFound):
        await _edit(
            pocket_id,
            component_path="src/lib/components/Heroo.svelte",
            edits=[{"old_string": "Bright", "new_string": "Brighter"}],
        )


@pytest.mark.asyncio
async def test_create_without_new_source_is_rejected(beanie_test_db):
    """``edits`` has nothing to search against in a file that does not exist yet."""
    pocket_id = await _make_svelte_pocket("w1", "u1")

    with pytest.raises(CloudError) as exc:
        await _edit(
            pocket_id,
            component_path="src/routes/about/+page.svelte",
            edits=[{"old_string": "x", "new_string": "y"}],
            create=True,
        )
    assert exc.value.code == "site_edit.create_needs_source"


@pytest.mark.asyncio
async def test_pockets_service_enforces_the_inversion_itself(beanie_test_db):
    """The rule lives at the write chokepoint, so it holds for EVERY caller — not
    only the ones that come through the sites service."""
    pocket_id = await _make_svelte_pocket("w1", "u1")

    with pytest.raises(NotFound):
        await pockets_service.set_svelte_source_file(
            pocket_id,
            "u1",
            component_path="src/lib/components/Nope.svelte",
            new_source="<p/>",
        )
    with pytest.raises(CloudError) as exc:
        await pockets_service.set_svelte_source_file(
            pocket_id,
            "u1",
            component_path="src/lib/components/Hero.svelte",
            new_source="<p/>",
            create=True,
        )
    assert exc.value.code == "pocket.svelte_component_exists"


@pytest.mark.asyncio
async def test_create_returns_no_previous_source_to_roll_back_to(beanie_test_db):
    pocket_id = await _make_svelte_pocket("w1", "u1")

    _wire, previous = await pockets_service.set_svelte_source_file(
        pocket_id,
        "u1",
        component_path="src/lib/components/New.svelte",
        new_source="<p>new</p>",
        create=True,
    )
    assert previous is None


# ── (d) the rollback shape ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_failed_create_removes_the_key_rather_than_blanking_it(beanie_test_db):
    """A create that fails the smoke gate must leave the map exactly as it was.

    The pre-existing rollback restores ``previous_source``; a create HAS none, and
    writing an empty string back would leave an empty file at a real route — a blank
    page the next publish still has to serve, which is the state the rollback exists
    to prevent rather than a recovery from it.
    """
    pocket_id = await _make_svelte_pocket("w1", "u1")

    with pytest.raises(SmokeGateFailed):
        await _edit(
            pocket_id,
            component_path="src/routes/about/+page.svelte",
            new_source="<h1>boom</h1>",
            create=True,
            _generator=_SmokeFailGenerator(),
        )

    pocket = await pockets_service.get(pocket_id, "u1")
    assert "src/routes/about/+page.svelte" not in pocket["source"], (
        "a rolled-back create must REMOVE the key, not leave an empty file behind"
    )
    assert set(pocket["source"]) == set(_SVELTE_SOURCE)


@pytest.mark.asyncio
async def test_a_failed_ordinary_edit_still_restores_the_prior_contents(beanie_test_db):
    """The create branch must not have broken the rollback it forked from."""
    pocket_id = await _make_svelte_pocket("w1", "u1")

    with pytest.raises(SmokeGateFailed):
        await _edit(
            pocket_id,
            component_path="src/lib/components/Hero.svelte",
            new_source=_HERO_V2,
            _generator=_SmokeFailGenerator(),
        )

    pocket = await pockets_service.get(pocket_id, "u1")
    assert pocket["source"]["src/lib/components/Hero.svelte"] == _HERO_V1


@pytest.mark.asyncio
async def test_removing_an_absent_path_is_not_an_error(beanie_test_db):
    """The rollback must be safe to run against a write that never landed — raising
    there would replace a recoverable smoke failure with an unrecoverable one."""
    pocket_id = await _make_svelte_pocket("w1", "u1")

    await pockets_service.remove_svelte_source_file(
        pocket_id, "u1", component_path="src/routes/never/+page.svelte"
    )
    pocket = await pockets_service.get(pocket_id, "u1")
    assert set(pocket["source"]) == set(_SVELTE_SOURCE)


# ── the reachability advisory ───────────────────────────────────────────────
#
# A create is HALF of adding a page. The react and html lanes each grew this after a
# real orphan-create incident: call 1 landed, call 2 never came, and the flat success
# was read as "done" over a page that never changed. Svelte gets it at birth.


@pytest.mark.asyncio
async def test_a_new_page_nothing_links_to_reports_unreferenced(beanie_test_db):
    pocket_id = await _make_svelte_pocket("w1", "u1")

    _site, unreferenced = await _edit(
        pocket_id,
        component_path="src/routes/about/+page.svelte",
        new_source="<h1>About</h1>",
        create=True,
    )
    assert unreferenced is True


@pytest.mark.asyncio
async def test_a_new_page_is_referenced_once_something_links_it(beanie_test_db):
    """Call 2 of the two-call contract clears the advisory."""
    pocket_id = await _make_svelte_pocket("w1", "u1")

    await _edit(
        pocket_id,
        component_path="src/routes/+page.svelte",
        edits=[{"old_string": "<Hero/>", "new_string": "<Hero/><a href='/about'>About</a>"}],
    )
    _site, unreferenced = await _edit(
        pocket_id,
        component_path="src/routes/about/+page.svelte",
        new_source="<h1>About</h1>",
        create=True,
    )
    assert unreferenced is False


@pytest.mark.asyncio
async def test_a_near_miss_link_does_not_count_as_a_reference(beanie_test_db):
    """``/about-us`` must not satisfy ``/about`` — a false "it is referenced" is the
    silence the advisory exists to break."""
    pocket_id = await _make_svelte_pocket("w1", "u1")

    await _edit(
        pocket_id,
        component_path="src/routes/+page.svelte",
        edits=[
            {
                "old_string": "<Hero/>",
                "new_string": "<Hero/><a href='/about-us'>About us</a>",
            }
        ],
    )
    _site, unreferenced = await _edit(
        pocket_id,
        component_path="src/routes/about/+page.svelte",
        new_source="<h1>About</h1>",
        create=True,
    )
    assert unreferenced is True


@pytest.mark.asyncio
async def test_a_sibling_convention_file_is_reached_through_its_directory(
    beanie_test_db,
):
    """``+page.ts`` is claimed by the file-system router because of where it sits,
    not by an import — so it counts as reached once its ``+page.svelte`` exists."""
    pocket_id = await _make_svelte_pocket("w1", "u1")

    await _edit(
        pocket_id,
        component_path="src/routes/about/+page.svelte",
        new_source="<h1>About</h1>",
        create=True,
    )
    _site, unreferenced = await _edit(
        pocket_id,
        component_path="src/routes/about/+page.ts",
        new_source="export const prerender = true",
        create=True,
    )
    assert unreferenced is False


@pytest.mark.asyncio
async def test_a_new_component_nothing_imports_reports_unreferenced(beanie_test_db):
    pocket_id = await _make_svelte_pocket("w1", "u1")

    _site, unreferenced = await _edit(
        pocket_id,
        component_path="src/lib/components/Testimonials.svelte",
        new_source="<section>Lovely people</section>",
        create=True,
    )
    assert unreferenced is True


@pytest.mark.asyncio
async def test_a_new_component_is_referenced_once_imported(beanie_test_db):
    pocket_id = await _make_svelte_pocket("w1", "u1")

    await _edit(
        pocket_id,
        component_path="src/routes/+page.svelte",
        edits=[
            {
                "old_string": "<Hero/>",
                "new_string": (
                    "<Hero/><script>import T from '$lib/components/Testimonials.svelte'</script>"
                ),
            }
        ],
    )
    _site, unreferenced = await _edit(
        pocket_id,
        component_path="src/lib/components/Testimonials.svelte",
        new_source="<section>Lovely people</section>",
        create=True,
    )
    assert unreferenced is False


@pytest.mark.asyncio
async def test_an_ordinary_edit_never_reports_unreferenced(beanie_test_db):
    """Scoped to create deliberately — re-litigating wiring on every headline change
    is noise on the common path, which is how the signal on the rare path gets
    skimmed."""
    pocket_id = await _make_svelte_pocket("w1", "u1")

    _site, unreferenced = await _edit(
        pocket_id,
        component_path="src/lib/components/Hero.svelte",
        new_source=_HERO_V2,
    )
    assert unreferenced is False
