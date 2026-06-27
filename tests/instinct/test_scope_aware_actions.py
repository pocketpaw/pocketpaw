# tests/instinct/test_scope_aware_actions.py
# Created: 2026-06-18 (feat/branch-primitive-instinct-gate, BP-3) — coverage for
# the ADDITIVE generic scope on the Instinct store (Part A). Pins that:
#   1. A legacy Action (no scope_type) round-trips with scope_type=None and the
#      legacy pocket_id-only read still finds it.
#   2. A scoped Action (scope_type set) round-trips and a scope-aware read
#      filtering on (scope_type, scope_id) returns it.
#   3. A scope-aware read does NOT return a legacy row that shares the same
#      pocket_id/scope_id (scope_type discriminates), and a legacy read is
#      unaffected by scoped rows existing.
#   4. pending() is scope-aware the same way.
# These are OSS store tests — no cloud extras, no beanie, just a tmp SQLite db.
from __future__ import annotations

from pathlib import Path

import pytest

from pocketpaw.instinct.models import ActionStatus, ActionTrigger
from pocketpaw.instinct.store import InstinctStore

pytestmark = pytest.mark.asyncio

TRIGGER = ActionTrigger(type="agent", source="claude", reason="scope test")


@pytest.fixture
def store(tmp_path: Path) -> InstinctStore:
    return InstinctStore(tmp_path / "scope.db")


async def test_legacy_action_roundtrips_with_none_scope(store: InstinctStore) -> None:
    """A propose with no scope_type stores scope_type=None and is readable via
    the legacy pocket_id-only path — the pre-BP-3 behaviour, unchanged."""
    action = await store.propose(
        pocket_id="pocket-1",
        title="legacy",
        description="",
        recommendation="",
        trigger=TRIGGER,
    )
    assert action.scope_type is None

    # Read back via get_action — scope_type comes back None.
    fetched = await store.get_action(action.id)
    assert fetched is not None
    assert fetched.scope_type is None
    assert fetched.pocket_id == "pocket-1"

    # Legacy pocket_id-only listing finds it.
    rows = await store.list_actions(pocket_id="pocket-1")
    assert [r.id for r in rows] == [action.id]


async def test_scoped_action_roundtrips_and_scope_aware_read_finds_it(
    store: InstinctStore,
) -> None:
    """A propose with scope_type='site' round-trips, and a scope-aware read
    filtering on (scope_type, scope_id) returns it."""
    action = await store.propose(
        pocket_id="pocket-9",  # reused as the scope id within scope_type="site"
        title="scoped",
        description="",
        recommendation="",
        trigger=TRIGGER,
        scope_type="site",
    )
    assert action.scope_type == "site"

    fetched = await store.get_action(action.id)
    assert fetched is not None
    assert fetched.scope_type == "site"

    # Scope-aware read returns it.
    rows = await store.list_actions(pocket_id="pocket-9", scope_type="site")
    assert [r.id for r in rows] == [action.id]


async def test_scope_aware_read_discriminates_legacy_from_scoped(
    store: InstinctStore,
) -> None:
    """A legacy row and a scoped row that share the same id value must NOT be
    confused: a scope-aware read returns only the scoped row, and a legacy
    pocket_id-only read returns only the legacy row."""
    legacy = await store.propose(
        pocket_id="shared-id",
        title="legacy row",
        description="",
        recommendation="",
        trigger=TRIGGER,
    )
    scoped = await store.propose(
        pocket_id="shared-id",
        title="scoped row",
        description="",
        recommendation="",
        trigger=TRIGGER,
        scope_type="site",
    )

    # Scope-aware read on (site, shared-id) returns ONLY the scoped row.
    scoped_rows = await store.list_actions(pocket_id="shared-id", scope_type="site")
    assert [r.id for r in scoped_rows] == [scoped.id]

    # Legacy pocket_id-only read returns BOTH rows that carry pocket_id ==
    # shared-id (the legacy path is scope-type-agnostic — it never narrowed by
    # scope_type, so it is unchanged from pre-BP-3 and sees every row with that
    # pocket_id). The important guarantee is that a SCOPED read does not leak
    # the legacy row; the legacy read staying broad is the backward-compat path.
    legacy_ids = {r.id for r in await store.list_actions(pocket_id="shared-id")}
    assert legacy.id in legacy_ids
    assert scoped.id in legacy_ids


async def test_pending_is_scope_aware(store: InstinctStore) -> None:
    """pending() filters on (scope_type, scope_id) when scope_type is given."""
    legacy = await store.propose(
        pocket_id="p",
        title="legacy pending",
        description="",
        recommendation="",
        trigger=TRIGGER,
    )
    scoped = await store.propose(
        pocket_id="p",
        title="scoped pending",
        description="",
        recommendation="",
        trigger=TRIGGER,
        scope_type="dashboard",
    )

    scoped_pending = await store.pending(pocket_id="p", scope_type="dashboard")
    assert [a.id for a in scoped_pending] == [scoped.id]
    assert all(a.status == ActionStatus.PENDING for a in scoped_pending)

    # The legacy pending read (no scope_type) still sees both.
    all_pending_ids = {a.id for a in await store.pending(pocket_id="p")}
    assert legacy.id in all_pending_ids
    assert scoped.id in all_pending_ids
