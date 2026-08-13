# tests/ee/sites/test_html_file_edit.py — the html-track EDIT lane
# (sites_service.edit_html_file). Created: 2026-08-13 (feat/sites-html-edit-lane,
# HE-10).
#
# WHAT WAS MISSING, and it is the RX-3 hole one engine over. ``create_html_site``
# shipped in HE-6 and the URL importer (SI-5) mints html pockets too, so html sites
# existed in quantity — and NOTHING could change one from chat. Both existing edit
# entry points reject an html pocket by design (``edit_svelte_component`` raises
# ``pocket.not_svelte_site``, ``edit_react_component`` raises
# ``pocket.not_react_site``), and the only html-engine writer in the pockets service
# was ``set_imported_source``, which replaces the WHOLE map and takes a
# ``workspace_id`` with no viewer check because the crawler calls it off-request.
# So "change the phone number in the footer" had no tool that would take it, and the
# agent's only available move was a second ``create_html_site`` — a SECOND pocket at
# a SECOND url, leaving the site the user was looking at untouched.
#
# These use the shared ``beanie_test_db`` fixture (an in-memory Mongo) so the pockets
# service persists a REAL html Pocket doc and the guards run against real state.
# There is no generator, no Cloudflare and no bundle reader to inject, because there
# is no build — html is the one engine where ``needs_node_build`` is False.
#
# THREE PROPERTIES ARE LOAD BEARING and get their own sections:
#   (a) DRAFT-ONLY — the edit must not publish. The REASON is not react's and the
#       distinction matters: react cannot roll back because its build is async,
#       whereas html has no build to gate on AT ALL, so a republish here would push
#       unvalidated markup straight to a live customer site with nothing in between.
#       Asserted by spying on the publish seam AND by the observable that no Site doc
#       exists afterwards.
#   (b) THE PATH GUARD, WHICH IS HTML'S OWN. The temptation was to reuse
#       ``react_paths``; doing so would have rejected ``index.html`` — every html
#       site's entry document — because react requires ``src/`` or ``public/``. So
#       the positive rule is ABSENT here on purpose, and there is a test that fails
#       if someone "restores" it. What IS reserved is ``_paw/``, where
#       ``_paw/edit-manifest.json`` maps each editable element to a byte range; an
#       author who could shadow it would not break anything loudly, they would make
#       the NEXT native-editor edit splice at wrong offsets.
#   (c) THE CREATE/EXISTS INVERSION — ``create=False`` demands the path exist (a typo
#       is never a silent create); ``create=True`` demands it not.
#
# Mutation coverage: tests/mutations/html_edit_lane.json.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
from pocketpaw_ee.cloud.models.site import Site as _SiteDoc
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.sites import service as sites_service

_INDEX_V1 = (
    "<!doctype html>\n"
    '<html lang="en">\n'
    "<head><title>Bright Smile Dental</title>\n"
    '<link rel="stylesheet" href="styles.css"></head>\n'
    "<body>\n"
    "  <h1>Bright Smile Dental</h1>\n"
    "  <p>Gentle care in the heart of town.</p>\n"
    '  <a href="tel:5550100">555-0100</a>\n'
    "</body>\n"
    "</html>\n"
)
_STYLES = ":root{--brand:#0A84FF}\nbody{font-family:system-ui}\n"
_HTML_SOURCE = {
    "index.html": _INDEX_V1,
    "styles.css": _STYLES,
    "img/logo.svg": "<svg/>",
}


async def _make_html_pocket(workspace_id: str, user_id: str) -> str:
    """Persist a real html-engine Pocket via the pockets service and return its id.

    Mirrors how ``create_html_site`` lands one: type='site', pattern='landing',
    engine='html', source=<map>, ripple_spec=None, trusted=True."""
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id=workspace_id,
        owner_id=user_id,
        name="Bright Smile",
        type_="site",
        pattern="landing",
        ripple_spec=None,
        engine="html",
        source=dict(_HTML_SOURCE),
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
    """The dominant case, and the one the whole lane exists for: the agent sends
    only the diff and the file changes.

    THE MUTATION THAT BREAKS THIS: make the persist a no-op (drop the
    ``set_html_source_file`` call) — the re-read still carries the old number."""
    pocket_id = await _make_html_pocket("ws1", "u1")

    out = await sites_service.edit_html_file(
        user_id="u1",
        pocket_id=pocket_id,
        file_path="index.html",
        edits=[{"old_string": "555-0100", "new_string": "555-0199"}],
    )

    assert out == {"pocket_id": pocket_id, "file_path": "index.html", "created": False}
    source = await _source_of(pocket_id)
    assert "555-0199" in source["index.html"]
    assert "555-0100" not in source["index.html"]
    # Only the targeted file moved — the rest of the map came through verbatim.
    assert source["styles.css"] == _STYLES
    assert source["img/logo.svg"] == "<svg/>"


@pytest.mark.asyncio
async def test_editing_a_root_level_file_is_allowed(beanie_test_db):
    """THE TEST THAT FAILS IF SOMEONE PORTS REACT'S POSITIVE PATH RULE HERE.

    ``react_path_rejection`` requires a path to resolve under ``src/`` or
    ``public/``, because on that track everything at the project root belongs to a
    generated build shell. An html site has NO build shell — ``html-scaffold.ts``
    writes the author's map verbatim into the directory the edge serves — so its
    files legitimately live at the root, starting with the ``index.html`` the edge
    serves as the entry document. Reusing react's module here would have rejected
    the single most-edited file on the track.

    Mutation: swap ``html_path_rejection`` for ``react_path_rejection`` in
    ``edit_html_file`` and this fails on every file in the fixture."""
    pocket_id = await _make_html_pocket("ws1", "u1")

    for path in ("index.html", "styles.css"):
        await sites_service.edit_html_file(
            user_id="u1",
            pocket_id=pocket_id,
            file_path=path,
            new_source=f"/* {path} */",
        )

    source = await _source_of(pocket_id)
    assert source["index.html"] == "/* index.html */"
    assert source["styles.css"] == "/* styles.css */"


@pytest.mark.asyncio
async def test_new_source_replaces_the_whole_file(beanie_test_db):
    """The large-rewrite form: ``new_source`` is used as-is, not merged."""
    pocket_id = await _make_html_pocket("ws1", "u1")
    rewritten = "<!doctype html>\n<html><body><h1>New</h1></body></html>\n"

    await sites_service.edit_html_file(
        user_id="u1",
        pocket_id=pocket_id,
        file_path="index.html",
        new_source=rewritten,
    )

    assert (await _source_of(pocket_id))["index.html"] == rewritten


@pytest.mark.asyncio
async def test_a_second_edit_stacks_on_the_first(beanie_test_db):
    """Consecutive edits compose — the second reads what the first persisted, so a
    multi-turn conversation converges instead of each turn undoing the last."""
    pocket_id = await _make_html_pocket("ws1", "u1")

    await sites_service.edit_html_file(
        user_id="u1",
        pocket_id=pocket_id,
        file_path="index.html",
        edits=[{"old_string": "555-0100", "new_string": "555-0199"}],
    )
    await sites_service.edit_html_file(
        user_id="u1",
        pocket_id=pocket_id,
        file_path="index.html",
        edits=[{"old_string": "Gentle care", "new_string": "Expert care"}],
    )

    index = (await _source_of(pocket_id))["index.html"]
    assert "555-0199" in index and "Expert care" in index


# ---------------------------------------------------------------------------
# (a) DRAFT-ONLY — no publish, and nothing goes live
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_edit_does_not_publish(beanie_test_db, monkeypatch):
    """An html publish runs NO build and NO smoke gate, so there is nothing that
    could reject a broken edit before it reached the edge. Republishing from here
    would therefore push unvalidated markup straight to a live customer site — the
    opposite of the svelte lane, where the smoke gate is exactly what makes an
    automatic republish safe.

    Mutation: add a ``publish_pocket`` call to ``edit_html_file`` and this fails."""
    pocket_id = await _make_html_pocket("ws1", "u1")
    calls: list = []
    monkeypatch.setattr(
        sites_service,
        "publish_pocket",
        lambda *a, **k: calls.append(k) or (_ for _ in ()).throw(AssertionError("published")),
    )

    await sites_service.edit_html_file(
        user_id="u1",
        pocket_id=pocket_id,
        file_path="index.html",
        edits=[{"old_string": "555-0100", "new_string": "555-0199"}],
    )

    assert calls == []


@pytest.mark.asyncio
async def test_no_site_doc_is_created_by_an_edit(beanie_test_db):
    """The observable half of the same contract, independent of any seam: a Site doc
    is what makes a site exist at a url, and an edit must not mint one."""
    pocket_id = await _make_html_pocket("ws1", "u1")

    await sites_service.edit_html_file(
        user_id="u1",
        pocket_id=pocket_id,
        file_path="index.html",
        new_source="<html></html>",
    )

    assert await _SiteDoc.find({"pocket_id": pocket_id}).count() == 0


# ---------------------------------------------------------------------------
# (b) THE PATH GUARD — the generator's namespace, and escapes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "_paw/edit-manifest.json",
        "_paw",
        "_paw/nested/deep.json",
        # Every spelling the normalizer is supposed to collapse. A guard a trivial
        # path spelling defeats is not a guard.
        "./_paw/edit-manifest.json",
        "_paw/./edit-manifest.json",
        "img/../_paw/edit-manifest.json",
        "_paw\\edit-manifest.json",
    ],
)
async def test_the_generator_namespace_is_not_writable(beanie_test_db, path):
    """``_paw/edit-manifest.json`` maps each editable element to a byte range in the
    author's source, and the native visual editor writes THROUGH it. Shadowing it
    does not fail loudly — it points valid uids at wrong offsets, so the next native
    edit splices into the middle of a tag.

    ``create=True`` is used because that is the dangerous direction: it is the only
    mode that can mint a path that does not exist yet."""
    pocket_id = await _make_html_pocket("ws1", "u1")

    with pytest.raises(ValidationError) as exc:
        await sites_service.edit_html_file(
            user_id="u1",
            pocket_id=pocket_id,
            file_path=path,
            new_source="{}",
            create=True,
        )
    assert exc.value.code == "site_edit.reserved_path"
    # Nothing was written under any spelling of the reserved path.
    assert not any(k.startswith("_paw") for k in await _source_of(pocket_id))


@pytest.mark.asyncio
async def test_a_file_merely_sharing_the_prefix_is_writable(beanie_test_db):
    """``_pawprint.html`` is an ordinary page name. Reserving the NAMESPACE means
    matching the directory, not any sibling whose name starts with the same
    characters — which is why the constant carries its trailing slash.

    Mutation: drop the trailing slash from ``HTML_RESERVED_PREFIX`` and this fails."""
    pocket_id = await _make_html_pocket("ws1", "u1")

    await sites_service.edit_html_file(
        user_id="u1",
        pocket_id=pocket_id,
        file_path="_pawprint.html",
        new_source="<h1>Our work</h1>",
        create=True,
    )

    assert (await _source_of(pocket_id))["_pawprint.html"] == "<h1>Our work</h1>"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["../secrets.env", "../../etc/passwd", "/etc/passwd", "img/../../out.html"],
)
async def test_a_path_that_escapes_the_site_is_rejected(beanie_test_db, path):
    """The generator asserts this too (``assertSafeRelPath``), but a build-time throw
    lands far from the authoring turn. Rejecting here names the problem while the
    agent can still act on it."""
    pocket_id = await _make_html_pocket("ws1", "u1")

    with pytest.raises(ValidationError) as exc:
        await sites_service.edit_html_file(
            user_id="u1",
            pocket_id=pocket_id,
            file_path=path,
            new_source="x",
            create=True,
        )
    assert exc.value.code == "site_edit.path_outside_source"


@pytest.mark.asyncio
async def test_a_path_with_an_absorbed_dotdot_is_allowed(beanie_test_db):
    """``img/../logo.svg`` normalizes to ``logo.svg`` and never leaves the project,
    so it is not an escape. The guard rejects paths that RESOLVE outside, not paths
    that merely contain ``..`` — over-rejecting here would fail legitimate edits."""
    pocket_id = await _make_html_pocket("ws1", "u1")

    await sites_service.edit_html_file(
        user_id="u1",
        pocket_id=pocket_id,
        file_path="img/../logo.svg",
        new_source="<svg id='new'/>",
        create=True,
    )

    assert "logo.svg" in await _source_of(pocket_id)


# ---------------------------------------------------------------------------
# (c) THE CREATE/EXISTS INVERSION
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_typo_is_not_a_silent_create(beanie_test_db):
    """Without ``create`` the path must already exist. The alternative — writing a
    new file at the misspelled path — leaves the real page unchanged while the tool
    reports success, so the user is told a change landed that nobody can see."""
    pocket_id = await _make_html_pocket("ws1", "u1")

    with pytest.raises(NotFound):
        await sites_service.edit_html_file(
            user_id="u1",
            pocket_id=pocket_id,
            file_path="indx.html",
            new_source="<h1>oops</h1>",
        )
    assert "indx.html" not in await _source_of(pocket_id)


@pytest.mark.asyncio
async def test_a_diff_against_a_missing_file_is_a_404_not_a_crash(beanie_test_db):
    """WHY THE SITES-SERVICE EXISTENCE CHECK EARNS ITS KEEP even though the pockets
    service also refuses a missing path.

    On the ``new_source`` path the two are indistinguishable — the chokepoint raises
    the same NotFound a moment later, which is exactly what a mutation proved by
    ESCAPING when this file only covered that path. The ``edits`` path is different:
    it INDEXES ``source_map[file_path]`` to compute the new source, so without the
    local check a typo'd path raises KeyError — a 500 the agent cannot act on —
    before the chokepoint is ever reached.

    Mutation: delete the ``if not create and file_path not in source_map`` guard in
    ``edit_html_file`` and this fails with KeyError instead of NotFound."""
    pocket_id = await _make_html_pocket("ws1", "u1")

    with pytest.raises(NotFound):
        await sites_service.edit_html_file(
            user_id="u1",
            pocket_id=pocket_id,
            file_path="indx.html",
            edits=[{"old_string": "555-0100", "new_string": "555-0199"}],
        )


@pytest.mark.asyncio
async def test_the_pockets_service_refuses_a_non_html_pocket_directly(beanie_test_db):
    """The write chokepoint carries its OWN engine gate, and this calls it directly
    because that is the only way to see it.

    Routed through ``edit_html_file`` the sites-service gate rejects first, so a
    mutation removing the chokepoint's gate ESCAPED — the test could not tell the
    two layers apart. ``set_html_source_file`` is a public service function
    (entity isolation: it is where EVERY html source write lands, not only this
    lane's), so a future caller reaching it without the sites-service preamble must
    still be refused rather than writing raw html into a react pocket's map."""
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id="ws1",
        owner_id="u1",
        name="React site",
        type_="site",
        pattern="landing",
        ripple_spec=None,
        engine="react",
        source={"src/App.tsx": "export default () => <main/>;"},
        trusted=True,
    )
    assert err is None, err

    with pytest.raises(ValidationError) as exc:
        await pockets_service.set_html_source_file(
            pocket_id,
            "u1",
            file_path="index.html",
            new_source="<h1>x</h1>",
            create=True,
        )
    assert exc.value.code == "pocket.not_html_site"


@pytest.mark.asyncio
async def test_create_mints_a_new_page(beanie_test_db):
    """The affirmative case ``create`` exists for: "add an about page" needs a new
    FILE, then an edit to ``index.html`` to link to it."""
    pocket_id = await _make_html_pocket("ws1", "u1")

    out = await sites_service.edit_html_file(
        user_id="u1",
        pocket_id=pocket_id,
        file_path="about.html",
        new_source="<!doctype html><h1>About us</h1>",
        create=True,
    )

    assert out["created"] is True
    source = await _source_of(pocket_id)
    assert source["about.html"] == "<!doctype html><h1>About us</h1>"
    # The rest of the site is untouched by an add.
    assert source["index.html"] == _INDEX_V1


@pytest.mark.asyncio
async def test_create_refuses_to_overwrite_an_existing_page(beanie_test_db):
    """The inversion's other half. Silently replacing the live home page with what
    the agent thought was a new file is worse than a rejected call it can retry."""
    pocket_id = await _make_html_pocket("ws1", "u1")

    with pytest.raises(ValidationError) as exc:
        await sites_service.edit_html_file(
            user_id="u1",
            pocket_id=pocket_id,
            file_path="index.html",
            new_source="<h1>clobbered</h1>",
            create=True,
        )
    assert exc.value.code == "pocket.html_file_exists"
    assert (await _source_of(pocket_id))["index.html"] == _INDEX_V1


@pytest.mark.asyncio
async def test_create_without_new_source_is_rejected(beanie_test_db):
    """There is nothing for ``edits`` to search against in a file that does not
    exist, so this is caught at the argument level rather than raising a KeyError
    deeper in."""
    pocket_id = await _make_html_pocket("ws1", "u1")

    with pytest.raises(ValidationError) as exc:
        await sites_service.edit_html_file(
            user_id="u1",
            pocket_id=pocket_id,
            file_path="about.html",
            edits=[{"old_string": "a", "new_string": "b"}],
            create=True,
        )
    assert exc.value.code == "site_edit.create_needs_source"


# ---------------------------------------------------------------------------
# Engine gating and argument shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_react_pocket_is_rejected(beanie_test_db):
    """The mirror of the rejections that created this hole. Each engine's edit tool
    accepts only its own content model — writing raw html into a react source map
    would persist a file the Vite build cannot compile."""
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id="ws1",
        owner_id="u1",
        name="React site",
        type_="site",
        pattern="landing",
        ripple_spec=None,
        engine="react",
        source={"src/App.tsx": "export default () => <main/>;"},
        trusted=True,
    )
    assert err is None, err

    with pytest.raises(ValidationError) as exc:
        await sites_service.edit_html_file(
            user_id="u1",
            pocket_id=pocket_id,
            file_path="index.html",
            new_source="<h1>x</h1>",
        )
    assert exc.value.code == "pocket.not_html_site"


@pytest.mark.asyncio
async def test_a_ripple_pocket_is_rejected(beanie_test_db):
    """A ripple pocket's content is a rippleSpec, not a source map — it has no
    ``source`` key to index, so the guard has to fire before the read."""
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id="ws1",
        owner_id="u1",
        name="Ripple site",
        type_="site",
        pattern="landing",
        ripple_spec={"type": "container", "children": []},
        trusted=True,
    )
    assert err is None, err

    with pytest.raises(ValidationError) as exc:
        await sites_service.edit_html_file(
            user_id="u1",
            pocket_id=pocket_id,
            file_path="index.html",
            new_source="<h1>x</h1>",
        )
    assert exc.value.code == "pocket.not_html_site"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {},  # neither
        {"new_source": "<h1/>", "edits": [{"old_string": "a", "new_string": "b"}]},  # both
    ],
)
async def test_exactly_one_edit_shape_is_required(beanie_test_db, kwargs):
    """Neither is a no-op call; both is ambiguous about which wins. Both are the
    caller's bug and are rejected before anything is read or written."""
    pocket_id = await _make_html_pocket("ws1", "u1")

    with pytest.raises(ValidationError) as exc:
        await sites_service.edit_html_file(
            user_id="u1", pocket_id=pocket_id, file_path="index.html", **kwargs
        )
    assert exc.value.code == "site_edit.invalid_args"


@pytest.mark.asyncio
async def test_an_ambiguous_old_string_is_rejected_and_nothing_is_written(beanie_test_db):
    """``apply_edits`` requires a unique match, and this pins that the rejection
    happens BEFORE the persist. "Bright Smile Dental" appears in both the <title>
    and the <h1>, so a replace-first implementation would change both and report
    success — the agent would never learn it had edited the wrong one too."""
    pocket_id = await _make_html_pocket("ws1", "u1")

    with pytest.raises(ValidationError) as exc:
        await sites_service.edit_html_file(
            user_id="u1",
            pocket_id=pocket_id,
            file_path="index.html",
            edits=[{"old_string": "Bright Smile Dental", "new_string": "Bright Smile"}],
        )
    assert exc.value.code == "site_edit.ambiguous_match"
    assert (await _source_of(pocket_id))["index.html"] == _INDEX_V1


@pytest.mark.asyncio
async def test_an_unknown_pocket_is_not_found(beanie_test_db):
    """Resolved by the pockets service's public ``get``, which owns the tenancy
    check — this lane never queries a Pocket doc itself."""
    with pytest.raises(NotFound):
        await sites_service.edit_html_file(
            user_id="u1",
            pocket_id="6d4a1f2b3c8e9a0f1b2c3d4e",
            file_path="index.html",
            new_source="<h1>x</h1>",
        )


# ---------------------------------------------------------------------------
# The lead-capture plumbing an edit must not quietly drop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_edit_elsewhere_leaves_the_capture_form_intact(beanie_test_db):
    """A targeted diff cannot touch what it does not name, and on this track that is
    load bearing: an html site's contact form carries its ``action`` plus hidden
    ``paw_site_id`` / ``paw_key`` / ``paw_redirect`` inputs, and those are the whole
    reason a submission arrives as a lead. Dropping them fails silently — the form
    still renders, still submits, and every enquiry goes nowhere.

    This is why the tool description tells the agent to prefer ``edits`` and to
    leave the form plumbing alone: a full-file rewrite is the shape that loses it,
    and on a one-document track a rewrite is what "change the headline" tempts."""
    form = (
        '<form method="POST" action="https://api.test/api/v1/capture/form">'
        '<input type="hidden" name="paw_site_id" value="site123">'
        '<input type="hidden" name="paw_key" value="k_abc">'
        '<input type="hidden" name="paw_redirect" value="/thank-you">'
        '<input name="full_name"><button>Send</button></form>'
    )
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id="ws1",
        owner_id="u1",
        name="With form",
        type_="site",
        pattern="landing",
        ripple_spec=None,
        engine="html",
        source={"index.html": f"<body><h1>Old headline</h1>{form}</body>"},
        trusted=True,
    )
    assert err is None, err

    await sites_service.edit_html_file(
        user_id="u1",
        pocket_id=pocket_id,
        file_path="index.html",
        edits=[{"old_string": "Old headline", "new_string": "New headline"}],
    )

    index = (await _source_of(pocket_id))["index.html"]
    assert "New headline" in index
    assert form in index, "the targeted edit disturbed the capture form"
