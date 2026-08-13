# tests/ee/sites/test_static_form_capture.py — a hand-authored contact form on a
# track with no server route had nowhere to post, and the failure was silent.
#
# Created 2026-08-13. THE BUG THIS COVERS: the react and raw-html tracks have no
# ``/api/submit`` endpoint (no server runtime), and ``rewireForms`` runs ONLY on
# the import path — ``src/cli.ts`` says so in as many words: "the ONLY caller of
# rewireForms in the deployed backend path". So on those two tracks the authoring
# prompt asked for "a native ``<form>`` with flat named fields" and never said
# where it posts. A ``<form>`` with no action posts to itself: the visitor fills
# it in, presses send, the page reloads, and the lead is gone. Nothing errors,
# nothing is logged, and the site looks completely fine.
#
# THE FIX has two halves that must ship together. The prompt tells the agent to
# author the full native-form contract using the ``__CAPTURE_API_BASE__`` /
# ``__SITE_ID__`` / ``__CAPTURE_SIGNED_KEY__`` placeholders (the same tokens
# ``scaffoldProject`` already substitutes into the svelte templates), and
# ``build_generator_input`` resolves them on the way to the generator. Splitting
# those across repos would mean a window where the prompt is live and the
# substitution is not, publishing literal ``__CAPTURE_API_BASE__`` into the action
# of every react/html site — so they are one commit, and this file pins both ends.
#
# What is pinned here:
#   * a public deploy resolves all three tokens, on both source-map tracks;
#   * an ARMED (editable) build does NOT — see the byteSpan note on that test,
#     which is the subtle one and the reason the gate exists at all;
#   * the caller's map is never mutated, so the POCKET keeps placeholders and a
#     rotated signed key is picked up by the next publish with nothing to migrate;
#   * non-string entries survive (a dynamic svelte envelope carries binding keys
#     alongside its files);
#   * the prompt actually names the endpoint and the canonical field names, rather
#     than the two drifting the way the assembler and the mapping did.
from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.sites.generator_client import build_generator_input  # noqa: E402

from pocketpaw.sites_capture import contact_form  # noqa: E402

SITE_ID = "6d4a1f2b3c8e9a0f1b2c3d4e"
CAPTURE_BASE = "https://api.pocketpaw.test/api/v1"
SIGNED_KEY = "site_key_bTdSb2hpdEs4d2FoYQ"

# The form an authoring agent is now told to write. Tokens, not values — the agent
# has neither at authoring time (on a create the site does not exist yet and the
# key is minted at publish).
AUTHORED_FORM = (
    '<form method="POST" action="__CAPTURE_API_BASE__/capture/form">'
    '<input type="hidden" name="paw_site_id" value="__SITE_ID__">'
    '<input type="hidden" name="paw_key" value="__CAPTURE_SIGNED_KEY__">'
    '<input type="hidden" name="paw_redirect" value="/thank-you">'
    '<input name="full_name" required><input type="email" name="email" required>'
    '<button type="submit">Send</button></form>'
)


def _input(engine: str, source: dict, **over):
    return build_generator_input(
        engine=engine,
        theme={},
        site_id=SITE_ID,
        title="Bright Smile Dental",
        capture_api_base=CAPTURE_BASE,
        capture_signed_key=SIGNED_KEY,
        source=source,
        **over,
    )


# --------------------------------------------------------------------------- #
# A public deploy resolves the tokens
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("engine", "path"), [("html", "index.html"), ("react", "src/App.tsx")])
def test_a_published_form_posts_to_a_real_address(engine, path):
    """THE FIX. Before it, whatever the agent wrote in ``action`` was whatever the
    agent guessed, and on these tracks that was nothing at all."""
    out = _input(engine, {path: AUTHORED_FORM})
    emitted = out["source"][path]

    assert f'action="{CAPTURE_BASE}/capture/form"' in emitted
    assert f'value="{SITE_ID}"' in emitted
    assert f'value="{SIGNED_KEY}"' in emitted
    # No token may survive into the deployed page.
    for token in ("__CAPTURE_API_BASE__", "__SITE_ID__", "__CAPTURE_SIGNED_KEY__"):
        assert token not in emitted, f"{token} shipped to the live site"


def test_source_with_no_tokens_is_passed_through_unchanged():
    """Ripple and virtually every svelte site carry no tokens. Substitution must be
    incapable of changing their emitted bytes."""
    untouched = {"src/routes/+page.svelte": "<h1>Brew and Co</h1>"}
    assert _input("svelte", untouched)["source"] == untouched


def test_resolution_is_idempotent():
    """A republish re-runs this over source that already looks resolved (it does
    not — the pocket keeps tokens — but the property costs nothing and a future
    caching layer could hand us a resolved map)."""
    once = _input("html", {"index.html": AUTHORED_FORM})["source"]["index.html"]
    twice = _input("html", {"index.html": once})["source"]["index.html"]
    assert once == twice


# --------------------------------------------------------------------------- #
# An ARMED build must NOT resolve them — the subtle one
# --------------------------------------------------------------------------- #


def test_an_editable_build_keeps_the_placeholders():
    """THE OFFSET TRAP. The html edit lane records ``byteSpan`` offsets in its edit
    manifest, computed over the bytes the generator was handed. A real signed key is
    about twice the length of its placeholder, so arming a build with SUBSTITUTED
    source and later splicing an edit into the STORED (placeholder) source would put
    every span after the form out by the difference — edits landing mid-tag, silently.

    Keeping armed builds on placeholders makes the two byte-identical. The form does
    not submit inside the builder preview, which is the right behaviour anyway: an
    edit session should not be able to mint live leads.

    Mutation: drop ``and not builder_origin`` from the condition in
    ``build_generator_input`` and this fails."""
    out = _input("html", {"index.html": AUTHORED_FORM}, builder_origin="http://localhost:8888")
    emitted = out["source"]["index.html"]

    assert "__CAPTURE_API_BASE__" in emitted
    assert "__CAPTURE_SIGNED_KEY__" in emitted
    assert SIGNED_KEY not in emitted


# --------------------------------------------------------------------------- #
# The stored pocket keeps its placeholders
# --------------------------------------------------------------------------- #


def test_the_callers_source_map_is_never_mutated():
    """The map handed in IS the stored pocket source. Mutating it would bake a real
    signed key into the pocket, and ``rotate_signed_key`` would then leave every
    published page authenticating with the leaked key it was rotated away from."""
    stored = {"index.html": AUTHORED_FORM}
    _input("html", stored)
    assert stored == {"index.html": AUTHORED_FORM}


def test_non_string_entries_ride_through_untouched():
    """A DYNAMIC svelte pocket carries live-data binding keys on the same envelope
    as its files (see ``_split_svelte_source``). Those are dicts and lists; running
    a string replace over them would raise."""
    out = _input(
        "svelte",
        {
            "src/routes/+page.svelte": "<h1>__SITE_ID__</h1>",
            "objects": [{"name": "Booking", "fields": []}],
            "auth": {"mode": "none"},
        },
    )
    assert out["source"]["src/routes/+page.svelte"] == f"<h1>{SITE_ID}</h1>"
    assert out["objects"] == [{"name": "Booking", "fields": []}]


# --------------------------------------------------------------------------- #
# The prompt half — the tokens are worthless if nothing tells the agent to write them
# --------------------------------------------------------------------------- #


def test_the_static_track_prompt_teaches_the_whole_native_form_contract():
    """The substitution above only ever runs on source an agent actually authored
    that way. This asserts the instruction exists and is complete — endpoint, both
    credential fields, and the relative-redirect rule the endpoint 400s without."""
    from pocketpaw_ee.cloud.surface.handlers.sites import native_form_contract

    block = native_form_contract()

    assert "__CAPTURE_API_BASE__/capture/form" in block
    assert "paw_site_id" in block and "__SITE_ID__" in block
    assert "paw_key" in block and "__CAPTURE_SIGNED_KEY__" in block
    # The open-redirect guard 400s an absolute path, so the agent has to know.
    assert "paw_redirect" in block
    assert "relative" in block.lower()


def test_the_prompt_names_the_canonical_field_names_rather_than_its_own():
    """The whole reason the contact schema exists is that a hand-written field list
    drifted from the mapping that reads it. A prompt is another hand-written field
    list, so it renders from the same declaration."""
    from pocketpaw_ee.cloud.surface.handlers.sites import native_form_contract

    block = native_form_contract()
    for field in contact_form.CONTACT_FIELD_NAMES:
        assert field in block, f"the prompt never tells the agent to emit {field!r}"
