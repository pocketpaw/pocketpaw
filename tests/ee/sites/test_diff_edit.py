# tests/ee/sites/test_diff_edit.py — exercises the TARGETED / DIFF edit path for a
# svelte Paw Site component (P3 — sites edit-perf). Created: 2026-06-18
# (feat/sites-diff-edit, P3).
#
# Motivation (the latency cause P3 addresses): the SE-2 edit tool takes the FULL
# ``new_source`` for a file, so for a one-line change ("add a bg color to the
# nav") the agent must read AND regenerate the whole component — lots of tokens in
# and out, slow, error-prone. P3 adds an ALTERNATIVE ``edits`` surface: a list of
# search/replace blocks ``[{old_string, new_string}, ...]`` (like the built-in
# Edit tool) the agent emits INSTEAD of the whole file. The blocks are applied to
# the pocket's CURRENT component source to produce the new source, which then
# reuses the unchanged SE-2 persist + preview/republish + rollback path.
#
# Two layers under test:
#   1. ``apply_edits`` — the PURE, I/O-free search/replace function. A block whose
#      ``old_string`` matches exactly once is applied; 0 matches or >1 matches
#      error with a clear, retry-able message; blocks apply sequentially so a
#      later block can target text an earlier block produced.
#   2. ``edit_svelte_component(..., edits=[...])`` — the diff path end to end:
#      it reads the pocket's current Hero source, applies the blocks, persists the
#      computed new source, and reaches the regenerated PREVIEW build with the
#      edited component (same downstream contract as the full-``new_source`` path).
#      The full-``new_source`` path is re-asserted here to prove it still works.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.sites import service as sites_service

# A minimal but §4.3-complete svelte source map (mirrors test_component_edit.py).
# The component under edit is Hero.svelte.
_HERO_V1 = "<section class='hero'><h1>Bright Smile</h1></section>"
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
    """Records the source map it was asked to build so a test can assert the
    diff-edited component reached the regenerated build."""

    def __init__(self):
        self.built = None

    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        self.built = kw
        return BuildResult(project_dir="/tmp/site", ripple_version=None)


class _FakeCF:
    def __init__(self):
        self.put_calls = []

    async def put_worker(self, *, script_name, bundle):
        self.put_calls.append(script_name)
        return True


def _fake_local_deploy(site_id: str, project_dir: str) -> str:
    return f"http://127.0.0.1:9999/{site_id}/"


@pytest.fixture(autouse=True)
def recording_bus():
    """Install a recording EventBus so the pockets service's ``emit`` calls don't
    raise (the real bus is only wired at boot). Mirrors test_component_edit.py."""
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod
    from pocketpaw_ee.cloud._core.realtime.events import Event

    class _RecordingBus:
        def __init__(self) -> None:
            self.events: list[Event] = []

        async def publish(self, event: Event) -> None:
            self.events.append(event)

        def subscribe(self, event_type: str, handler) -> None:  # noqa: ARG002
            return

    rec = _RecordingBus()
    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus_mod._bus = rec  # type: ignore[attr-defined]
    yield rec
    bus_mod._bus = prev  # type: ignore[attr-defined]


async def _make_svelte_pocket(workspace_id: str, user_id: str) -> str:
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


# ---------------------------------------------------------------------------
# 1. apply_edits — the PURE search/replace function
# ---------------------------------------------------------------------------


class TestApplyEdits:
    def test_single_block_applies_unique_match(self):
        out = sites_service.apply_edits(
            "<section class='hero'><h1>Bright Smile</h1></section>",
            [{"old_string": "Bright Smile", "new_string": "Brighter Smiles"}],
        )
        assert out == "<section class='hero'><h1>Brighter Smiles</h1></section>"

    def test_multiple_blocks_apply_in_order(self):
        """Blocks apply sequentially; a later block can target text an earlier
        block produced."""
        out = sites_service.apply_edits(
            "<section class='hero'><h1>Bright Smile</h1></section>",
            [
                # First add the class hook ...
                {
                    "old_string": "<section class='hero'>",
                    "new_string": "<section class='hero' style='background:#000'>",
                },
                # ... then a block that matches text the first block just wrote.
                {
                    "old_string": "background:#000",
                    "new_string": "background:#0A84FF",
                },
            ],
        )
        assert out == (
            "<section class='hero' style='background:#0A84FF'><h1>Bright Smile</h1></section>"
        )

    def test_zero_match_raises_clear_error(self):
        with pytest.raises(ValidationError) as ei:
            sites_service.apply_edits(
                "<section class='hero'><h1>Bright Smile</h1></section>",
                [{"old_string": "Not Present", "new_string": "x"}],
            )
        msg = ei.value.message.lower()
        assert "not found" in msg or "0 time" in msg or "did not match" in msg

    def test_multiple_match_raises_clear_error(self):
        """An ``old_string`` that matches more than once errors so the agent makes
        it unique (same contract as the built-in Edit tool)."""
        with pytest.raises(ValidationError) as ei:
            sites_service.apply_edits(
                "<div>x</div><div>x</div>",
                [{"old_string": "<div>x</div>", "new_string": "<div>y</div>"}],
            )
        msg = ei.value.message.lower()
        assert "2 time" in msg or "more than once" in msg or "unique" in msg

    def test_empty_edits_list_raises(self):
        with pytest.raises(ValidationError):
            sites_service.apply_edits("abc", [])

    def test_malformed_block_missing_keys_raises(self):
        with pytest.raises(ValidationError):
            sites_service.apply_edits("abc", [{"old_string": "a"}])

    def test_noop_block_old_equals_new_raises(self):
        """old_string == new_string is a no-op the agent did not intend — reject it
        with a clear message rather than silently doing nothing."""
        with pytest.raises(ValidationError):
            sites_service.apply_edits("abc", [{"old_string": "a", "new_string": "a"}])


# ---------------------------------------------------------------------------
# 2. edit_svelte_component(..., edits=[...]) — the diff path end to end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_targeted_edit_applies_and_reaches_build(beanie_test_db):
    """A targeted edit ([{old_string, new_string}]) is applied to the pocket's
    CURRENT Hero source, the computed new source persists, and the regenerated
    PREVIEW build materializes the edited component — without the agent ever
    sending the whole file."""
    pocket_id = await _make_svelte_pocket("ws1", "u1")
    gen, cf = _FakeGenerator(), _FakeCF()

    site = await sites_service.edit_svelte_component(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        component_path="src/lib/components/Hero.svelte",
        edits=[{"old_string": "Bright Smile", "new_string": "Brighter Smiles, Whiter Teeth"}],
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"export default {}",
        _local_deploy=_fake_local_deploy,
    )

    # Same preview contract as the full-new_source path: no live deploy.
    assert cf.put_calls == []
    assert site.deployed is False
    assert site.url.endswith(f"/preview-{pocket_id}/")

    expected = "<section class='hero'><h1>Brighter Smiles, Whiter Teeth</h1></section>"
    # The build materialized the diff-edited component.
    assert gen.built is not None
    assert gen.built["source"]["src/lib/components/Hero.svelte"] == expected
    # Untouched files came through verbatim.
    assert gen.built["source"]["src/routes/+page.ts"] == "export const prerender = true"

    # The computed new source persisted on the pocket.
    wire = await pockets_service.get(pocket_id, "u1")
    assert wire["source"]["src/lib/components/Hero.svelte"] == expected


@pytest.mark.asyncio
async def test_targeted_edit_non_unique_match_errors_without_persisting(beanie_test_db):
    """A targeted edit whose old_string does not match uniquely raises a clear
    ValidationError and does NOT mutate the pocket (the agent retries with a more
    specific old_string)."""
    pocket_id = await _make_svelte_pocket("ws1", "u1")
    gen, cf = _FakeGenerator(), _FakeCF()

    with pytest.raises(ValidationError):
        await sites_service.edit_svelte_component(
            workspace_id="ws1",
            user_id="u1",
            pocket_id=pocket_id,
            component_path="src/lib/components/Hero.svelte",
            edits=[{"old_string": "NOT IN THE FILE", "new_string": "x"}],
            _generator=gen,
            _cloudflare=cf,
            _bundle_reader=lambda d: b"x",
            _local_deploy=_fake_local_deploy,
        )

    # The bad edit never reached the generator and never mutated the pocket.
    assert gen.built is None
    wire = await pockets_service.get(pocket_id, "u1")
    assert wire["source"]["src/lib/components/Hero.svelte"] == _HERO_V1


@pytest.mark.asyncio
async def test_full_new_source_path_still_works(beanie_test_db):
    """Compat: the full-file ``new_source`` path is unchanged — it still rewrites
    the whole component and reaches the build."""
    pocket_id = await _make_svelte_pocket("ws1", "u1")
    gen, cf = _FakeGenerator(), _FakeCF()
    whole = "<section class='hero'><h1>Totally Rewritten</h1></section>"

    site = await sites_service.edit_svelte_component(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        component_path="src/lib/components/Hero.svelte",
        new_source=whole,
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"export default {}",
        _local_deploy=_fake_local_deploy,
    )

    assert site.deployed is False
    assert gen.built["source"]["src/lib/components/Hero.svelte"] == whole
    wire = await pockets_service.get(pocket_id, "u1")
    assert wire["source"]["src/lib/components/Hero.svelte"] == whole


@pytest.mark.asyncio
async def test_neither_edits_nor_new_source_raises(beanie_test_db):
    """Calling the edit with neither ``edits`` nor ``new_source`` is a programming
    error — raise a clear ValidationError, not a None-deref."""
    pocket_id = await _make_svelte_pocket("ws1", "u1")
    with pytest.raises(ValidationError):
        await sites_service.edit_svelte_component(
            workspace_id="ws1",
            user_id="u1",
            pocket_id=pocket_id,
            component_path="src/lib/components/Hero.svelte",
            _generator=_FakeGenerator(),
            _cloudflare=_FakeCF(),
            _bundle_reader=lambda d: b"x",
            _local_deploy=_fake_local_deploy,
        )
