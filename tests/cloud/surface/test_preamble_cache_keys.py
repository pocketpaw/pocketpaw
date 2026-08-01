# tests/cloud/surface/test_preamble_cache_keys.py
# Created: 2026-08-02 (PA-2, feat/prompt-assembler-seam) — pins the cache key
# every surface handler now returns, and the dispatcher that carries it out.
#
# The key exists because the preamble became a prompt LAYER: a backend caching
# an agent object with the prompt baked in folds the assembled digest into its
# own key, and this is the part of that digest that knows where the user is.
# The end-to-end digest properties live in ``tests/test_prompt_surface_layer.py``
# (OSS, no EE import); what is held HERE is the half that only the EE side can
# answer — whether the keys the handlers produce actually track what they read.
#
# The centre of the file is ``test_an_edit_past_the_widget_cut_still_moves_the_key``.
# It is the test for the design decision this task turned on: the obvious key is
# ``f"{kind}:{meta.pocket_id}:{meta.intent}"``, computed centrally in the
# dispatcher with zero handler changes — and it cannot see a pocket being
# edited, because none of those three move when it is. Neither can a digest of
# the rendered TEXT, once the edit lands past the 12-widget cut. Nor, it turns
# out, can the pocket's ``updatedAt``, which never moves at all under beanie 2
# (see the test's own docstring). Only a fingerprint of what the handler read
# sees it, and only the handler has that.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.models.user import User as _UserDoc
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets.dto import AddWidgetRequest, CreatePocketRequest
from pocketpaw_ee.cloud.surface.domain import SurfaceKind, SurfaceMeta, SurfacePreamble
from pocketpaw_ee.cloud.surface.handlers import pocket as pocket_handler
from pocketpaw_ee.cloud.surface.service import resolve_surface_context

pytestmark = pytest.mark.usefixtures("mongo_db")

WORKSPACE = "ws-surface-keys"

# The pocket handler lists this many widgets and collapses the rest into a
# "... (+N more)" line. Mirrored from the handler rather than imported: if the
# handler's cut moves, the test that depends on rendering past it should fail
# loudly rather than silently follow along and stop testing anything.
WIDGET_LIST_LIMIT = 12


async def _seed_user(email: str) -> str:
    doc = _UserDoc(
        email=email,
        hashed_password="x",
        is_active=True,
        is_verified=True,
        full_name="Key Owner",
        active_workspace=WORKSPACE,
    )
    await doc.insert()
    return str(doc.id)


async def _pocket_with_widgets(user_id: str, name: str, count: int) -> str:
    pocket = await pockets_service.create(WORKSPACE, user_id, CreatePocketRequest(name=name))
    for i in range(count):
        await pockets_service.add_widget(
            pocket["_id"],
            user_id,
            AddWidgetRequest(name=f"Widget {i:02d}", type="native"),
        )
    return str(pocket["_id"])


async def _preamble(user_id: str, pocket_id: str | None) -> SurfacePreamble:
    return await pocket_handler.build_preamble(WORKSPACE, user_id, SurfaceMeta(pocket_id=pocket_id))


# ---------------------------------------------------------------------------
# The handler answers a key at all, and it tracks the pocket
# ---------------------------------------------------------------------------


async def test_two_pockets_never_share_a_key() -> None:
    """Navigation, at the source. The digest property this feeds is in
    ``test_prompt_surface_layer``; here we prove the input to it moves."""
    user_id = await _seed_user("two@keys.test")
    a = await _pocket_with_widgets(user_id, "Pocket A", 1)
    b = await _pocket_with_widgets(user_id, "Pocket B", 1)

    assert (await _preamble(user_id, a)).cache_key != (await _preamble(user_id, b)).cache_key


async def test_an_unchanged_pocket_keeps_one_key_across_turns() -> None:
    """The cache-protecting half: reading the same pocket twice, with nothing
    written in between, must not move the key. A key that folded in a timestamp
    or a random id would pass every other test in this file and fail here."""
    user_id = await _seed_user("stable@keys.test")
    pocket_id = await _pocket_with_widgets(user_id, "Stable", 3)

    first = await _preamble(user_id, pocket_id)
    second = await _preamble(user_id, pocket_id)

    assert first.text == second.text
    assert first.cache_key == second.cache_key


async def test_editing_a_pocket_moves_the_key() -> None:
    """The visible case: a widget is added, the rendered text changes, and the
    key changes with it."""
    user_id = await _seed_user("edit@keys.test")
    pocket_id = await _pocket_with_widgets(user_id, "Edited", 2)

    before = await _preamble(user_id, pocket_id)
    await pockets_service.add_widget(
        pocket_id, user_id, AddWidgetRequest(name="Widget 99", type="native")
    )
    after = await _preamble(user_id, pocket_id)

    assert before.text != after.text
    assert before.cache_key != after.cache_key


async def test_an_edit_past_the_widget_cut_still_moves_the_key() -> None:
    """SAME TEXT, DIFFERENT KEY — the case a text digest gets wrong.

    The pocket carries more widgets than the preamble lists, so editing one
    past the cut renders byte-for-byte identically: same count, same first 12
    rows, same "+N more" tail. The pocket HAS changed, and a backend holding an
    agent built from the old prompt is holding a stale description of it. The
    handler fingerprints EVERY widget it read, not the twelve it printed, so
    the key moves even though nothing rendered did.

    This test is also what caught the first version of the key. That version
    used the pocket's ``updatedAt`` — the natural revision, claimed by
    ``TimestampedDocument`` and by the pocket service's own comments to be
    bumped on every write. It is not: beanie 2's ``init_actions`` skips
    ``_``-prefixed attributes, so the ``_set_updated`` hook is never registered
    and the timestamp keeps its creation value for life. The key looked right
    and reported every edit as "unchanged".
    """
    user_id = await _seed_user("cut@keys.test")
    pocket_id = await _pocket_with_widgets(user_id, "Deep", WIDGET_LIST_LIMIT + 2)

    before = await _preamble(user_id, pocket_id)

    pocket = await pockets_service.get(pocket_id, user_id)
    last_widget_id = pocket["widgets"][-1]["_id"]
    await pockets_service.update_widget(
        pocket_id,
        last_widget_id,
        user_id,
        _rename_request("Renamed past the cut"),
    )

    after = await _preamble(user_id, pocket_id)

    assert before.text == after.text, (
        "the edit must be INVISIBLE in the render for this test to mean anything — "
        "if the handler's list limit changed, this test needs rewriting, not relaxing"
    )
    assert before.cache_key != after.cache_key


def _rename_request(name: str):
    """Build an ``UpdateWidgetRequest`` naming only the field under test."""
    from pocketpaw_ee.cloud.pockets.dto import UpdateWidgetRequest

    return UpdateWidgetRequest(name=name)


# ---------------------------------------------------------------------------
# The branches that read nothing mutable
# ---------------------------------------------------------------------------


async def test_the_no_id_and_unavailable_branches_key_apart() -> None:
    """Three states of the pocket surface — no id, a dead id, a live pocket —
    render three different preambles and must key three different ways. They
    are all "pocket" to the dispatcher, which is why a key built from the kind
    would flatten them."""
    user_id = await _seed_user("branches@keys.test")
    live = await _pocket_with_widgets(user_id, "Live", 1)

    no_id = await _preamble(user_id, None)
    dead = await _preamble(user_id, "ffffffffffffffffffffffff")
    alive = await _preamble(user_id, live)

    keys = {no_id.cache_key, dead.cache_key, alive.cache_key}
    assert len(keys) == 3, keys


async def test_a_dead_pocket_id_keys_on_the_id_it_was_given() -> None:
    """Two different stale ids render different text (each names its own id),
    so they must not collapse to one "unavailable" key."""
    user_id = await _seed_user("dead@keys.test")
    one = await _preamble(user_id, "ffffffffffffffffffffffff")
    two = await _preamble(user_id, "eeeeeeeeeeeeeeeeeeeeeeee")
    assert one.text != two.text
    assert one.cache_key != two.cache_key


# ---------------------------------------------------------------------------
# The dispatcher carries the key out — and absorbs failure exactly as before
# ---------------------------------------------------------------------------


async def test_resolve_surface_context_carries_the_handlers_key() -> None:
    user_id = await _seed_user("carry@keys.test")
    pocket_id = await _pocket_with_widgets(user_id, "Carried", 1)

    ctx = await resolve_surface_context(
        WORKSPACE, user_id, {"surface": "pocket", "meta": {"pocket_id": pocket_id}}
    )
    direct = await _preamble(user_id, pocket_id)

    assert ctx.kind is SurfaceKind.POCKET
    assert ctx.preamble == direct.text
    assert ctx.preamble_cache_key == direct.cache_key
    assert ctx.preamble_cache_key, "a resolved pocket surface must carry a key"


async def test_a_raising_handler_still_yields_a_context_with_no_key(monkeypatch) -> None:
    """Criterion: a handler that raises degrades to a skipped layer and never
    fails the turn. The absorption is unchanged from before PA-2 — GENERIC kind,
    empty preamble — and the key that pairs with an empty preamble is ``None``,
    the same answer as having no surface at all. Both produce the same prompt,
    so both must hash alike."""
    from pocketpaw_ee.cloud.surface import service as surface_service

    async def _boom(workspace_id: str, user_id: str, meta):  # noqa: ARG001
        raise RuntimeError("handler exploded")

    monkeypatch.setattr(surface_service, "_HANDLERS", {SurfaceKind.POCKET: _boom})

    ctx = await resolve_surface_context(
        WORKSPACE, "u1", {"surface": "pocket", "meta": {"pocket_id": "p1"}}
    )

    assert ctx.kind is SurfaceKind.GENERIC
    assert ctx.preamble == ""
    assert ctx.preamble_cache_key is None


async def test_a_handler_that_returns_a_bare_string_claims_no_key(monkeypatch) -> None:
    """The liberal-in-what-we-accept path: a legacy or monkeypatched handler
    returning a plain ``str`` still renders, and is treated as VOLATILE. Text
    whose provenance nobody vouched for is exactly what must not be handed a
    key — least of all one the dispatcher invented for it."""
    from pocketpaw_ee.cloud.surface import service as surface_service

    async def _legacy(workspace_id: str, user_id: str, meta):  # noqa: ARG001
        return '<surface kind="pocket" route="/pockets/p1" />'

    monkeypatch.setattr(surface_service, "_HANDLERS", {SurfaceKind.POCKET: _legacy})

    ctx = await resolve_surface_context(
        WORKSPACE, "u1", {"surface": "pocket", "meta": {"pocket_id": "p1"}}
    )

    assert ctx.preamble.startswith("<surface")
    assert ctx.preamble_cache_key is None


async def test_an_empty_cache_key_is_refused() -> None:
    """``""`` reads as "stable forever" — the strongest claim a handler can
    make — and is also what someone types when they mean "nothing". It fails
    loudly instead, mirroring ``LayerOutput`` on the OSS side."""
    with pytest.raises(ValueError, match="cache_key"):
        SurfacePreamble(text="x", cache_key="")
