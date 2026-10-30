# tests/test_instinct_workspace_isolation.py
# Created: 2026-06-26 (ISO-2 — Instinct physically isolated + per-workspace audit chain).
#
# Proves ISO-2's invariant: each workspace gets its OWN instinct.db file under
# ~/.pocketpaw/workspaces/<workspace_id>/, with its OWN W2b audit hash-chain
# (genesis→…→head). Physical isolation is layered ON TOP of the W4a in-row
# workspace_id read-filter (additive defense-in-depth), and reuses the generic
# workspace-keyed factory ISO-1 built (so the path-traversal guard + fail-closed
# + bounded LRU are inherited).
#
# Covers:
#   * two-workspace physical isolation — actions proposed in A and B land in two
#     SEPARATE db files on disk; pending() and query_audit() in A return ZERO of
#     B's rows (even unscoped at the store level, because B's rows physically
#     aren't in A's file);
#   * per-workspace audit chain — verify_audit_chain() passes INDEPENDENTLY for
#     each workspace's file, each with its own genesis (prev_hash="");
#   * fail-closed — POCKETPAW_REQUIRE_WORKSPACE_SCOPE + no workspace → raises,
#     never a silent shared-store read;
#   * back-compat — flag unset + no workspace → legacy ~/.pocketpaw/instinct.db;
#   * inherited hostile-id guard — a traversal workspace_id is rejected by the
#     same strict allowlist Fabric uses, and writes nothing outside workspaces/.

from __future__ import annotations

from pathlib import Path

import pytest

import pocketpaw.stores as stores
from pocketpaw.instinct.models import ActionTrigger

WS_A = "ws-alpha"
WS_B = "ws-bravo"


def make_trigger() -> ActionTrigger:
    """Minimal ActionTrigger for a proposed action (matches test_ee_instinct)."""
    return ActionTrigger(type="agent", source="claude", reason="iso-2 unit test")


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the store factory at a tmp data dir; reset module + LRU state.

    Every test gets a clean ~/.pocketpaw equivalent, empty caches, the
    required-scope flag unset, and a cleared ContextVar so nothing leaks between
    tests.
    """
    monkeypatch.setattr(stores, "_DATA_DIR", tmp_path)
    monkeypatch.delenv("POCKETPAW_REQUIRE_WORKSPACE_SCOPE", raising=False)
    stores.reset_store_caches()
    token = stores.current_workspace.set(None)
    try:
        yield
    finally:
        try:
            stores.current_workspace.reset(token)
        except ValueError:
            stores.current_workspace.set(None)
        stores.reset_store_caches()


async def _propose(store, *, pocket: str, title: str, workspace_id: str):
    return await store.propose(
        pocket_id=pocket,
        title=title,
        description="",
        recommendation="",
        trigger=make_trigger(),
        workspace_id=workspace_id,
    )


# ---------------------------------------------------------------------------
# Core ISO-2 invariant: two workspaces => two files, no cross-read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_workspaces_get_separate_files_and_no_cross_read(
    tmp_path: Path,
) -> None:
    store_a = stores.get_instinct_store(workspace_id=WS_A)
    store_b = stores.get_instinct_store(workspace_id=WS_B)

    assert store_a is not store_b
    assert store_a._db_path != store_b._db_path

    await _propose(store_a, pocket="p", title="A-task", workspace_id=WS_A)
    await _propose(store_b, pocket="p", title="B-task", workspace_id=WS_B)

    # (a) Two SEPARATE db files exist on disk under workspaces/.
    db_a = tmp_path / "workspaces" / WS_A / "instinct.db"
    db_b = tmp_path / "workspaces" / WS_B / "instinct.db"
    assert db_a.exists(), "workspace A instinct.db should exist"
    assert db_b.exists(), "workspace B instinct.db should exist"
    # The shared legacy file must NOT have been created by a scoped call.
    assert not (tmp_path / "instinct.db").exists()

    # (b) pending() in A returns ZERO of B's actions — even UNSCOPED at the store
    # level (workspace_id=None), because B's row physically isn't in A's file.
    a_pending = await store_a.pending()
    b_pending = await store_b.pending()
    assert {a.title for a in a_pending} == {"A-task"}
    assert {a.title for a in b_pending} == {"B-task"}

    # (c) query_audit() is likewise physically isolated.
    a_audit = await store_a.query_audit(pocket_id="p")
    b_audit = await store_b.query_audit(pocket_id="p")
    a_titles = {e.description for e in a_audit}
    # B's "Proposed: B-task" audit line can't appear in A's file.
    assert not any("B-task" in d for d in a_titles)
    assert any("A-task" in (e.description or "") for e in a_audit)
    assert any("B-task" in (e.description or "") for e in b_audit)


# ---------------------------------------------------------------------------
# Per-workspace audit hash chain: each file verifies independently
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_workspace_has_its_own_verifiable_chain(tmp_path: Path) -> None:
    store_a = stores.get_instinct_store(workspace_id=WS_A)
    store_b = stores.get_instinct_store(workspace_id=WS_B)

    # Build a few hashed rows in each tenant's file (propose + approve both write
    # audit rows, extending that file's chain).
    for i in range(3):
        act = await _propose(store_a, pocket="p", title=f"a{i}", workspace_id=WS_A)
        await store_a.approve(act.id)
    for i in range(2):
        act = await _propose(store_b, pocket="p", title=f"b{i}", workspace_id=WS_B)
        await store_b.approve(act.id)

    # Each chain verifies INDEPENDENTLY and intact — its own genesis→…→head.
    verdict_a = await store_a.verify_audit_chain()
    verdict_b = await store_b.verify_audit_chain()

    assert verdict_a["intact"] is True
    assert verdict_a["broken_at"] is None
    assert verdict_a["hashed"] == verdict_a["checked"]
    assert verdict_a["hashed"] > 0

    assert verdict_b["intact"] is True
    assert verdict_b["broken_at"] is None
    assert verdict_b["hashed"] == verdict_b["checked"]
    assert verdict_b["hashed"] > 0

    # The chains are independent: A has more hashed rows than B (3 vs 2 actions,
    # each ~2 audit rows), so they are NOT one shared global chain.
    assert verdict_a["hashed"] != verdict_b["hashed"]


@pytest.mark.asyncio
async def test_tampering_one_workspace_does_not_break_the_other(tmp_path: Path) -> None:
    """A tampered row in A's file must not flip B's verdict (independent chains)."""
    import sqlite3

    store_a = stores.get_instinct_store(workspace_id=WS_A)
    store_b = stores.get_instinct_store(workspace_id=WS_B)

    a_act = await _propose(store_a, pocket="p", title="a", workspace_id=WS_A)
    await store_a.approve(a_act.id)
    b_act = await _propose(store_b, pocket="p", title="b", workspace_id=WS_B)
    await store_b.approve(b_act.id)

    # Both intact to start.
    assert (await store_a.verify_audit_chain())["intact"] is True
    assert (await store_b.verify_audit_chain())["intact"] is True

    # Tamper with A's ledger directly on disk (mutate a hashed row's content).
    # Target the lowest-rowid hashed row via a subquery — this SQLite build does
    # not support UPDATE ... ORDER BY ... LIMIT (needs a compile-time flag).
    with sqlite3.connect(store_a._db_path) as conn:
        conn.execute(
            "UPDATE instinct_audit SET description = 'TAMPERED' WHERE rowid = ("
            "SELECT rowid FROM instinct_audit WHERE entry_hash IS NOT NULL "
            "ORDER BY rowid LIMIT 1)"
        )
        conn.commit()

    # A is now broken; B is untouched and still intact — proof the chains are
    # per-tenant, not one global chain whose break would implicate every tenant.
    assert (await store_a.verify_audit_chain())["intact"] is False
    assert (await store_b.verify_audit_chain())["intact"] is True


# ---------------------------------------------------------------------------
# Fail-closed + back-compat + ContextVar (mirrors the ISO-1 guards)
# ---------------------------------------------------------------------------


def test_fail_closed_when_scope_required_and_no_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POCKETPAW_REQUIRE_WORKSPACE_SCOPE", "1")
    with pytest.raises(stores.WorkspaceScopeRequired):
        stores.get_instinct_store()  # no workspace, no ContextVar


def test_legacy_shared_store_when_unscoped_and_not_required(tmp_path: Path) -> None:
    store = stores.get_instinct_store()  # no workspace, flag unset
    assert store._db_path == str(tmp_path / "instinct.db")
    assert stores.get_instinct_store() is store  # singleton preserved


def test_contextvar_is_consulted_and_explicit_arg_wins(tmp_path: Path) -> None:
    token = stores.current_workspace.set(WS_B)
    try:
        # No explicit arg → ContextVar (WS_B).
        from_ctx = stores.get_instinct_store()
        assert str(tmp_path / "workspaces" / WS_B) in from_ctx._db_path
        # Explicit arg wins over the ContextVar.
        explicit = stores.get_instinct_store(workspace_id=WS_A)
        assert str(tmp_path / "workspaces" / WS_A) in explicit._db_path
        assert WS_B not in explicit._db_path
    finally:
        stores.current_workspace.reset(token)


def test_same_workspace_returns_cached_handle(tmp_path: Path) -> None:
    first = stores.get_instinct_store(workspace_id=WS_A)
    second = stores.get_instinct_store(workspace_id=WS_A)
    assert first is second


def test_fabric_and_instinct_caches_are_independent(tmp_path: Path) -> None:
    """The two store kinds keep separate LRUs — same workspace, different files."""
    fab = stores.get_fabric_store(workspace_id=WS_A)
    inst = stores.get_instinct_store(workspace_id=WS_A)
    assert fab is not inst
    assert fab._db_path.endswith("fabric.db")
    assert inst._db_path.endswith("instinct.db")
    # Both under the SAME workspace dir.
    assert str(tmp_path / "workspaces" / WS_A) in fab._db_path
    assert str(tmp_path / "workspaces" / WS_A) in inst._db_path


# ---------------------------------------------------------------------------
# Inherited hostile-id guard (the generic factory governs Instinct too)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "../../../tmp/pwn",
        "..",
        "a/b",
        "/abs/path",
        "-rf",
        "x\x00y",
        ".",
    ],
)
def test_hostile_workspace_id_rejected_and_writes_nothing(tmp_path: Path, hostile: str) -> None:
    """Instinct inherits ISO-1's strict-allowlist guard: reject + FS untouched."""

    def _snapshot() -> set[Path]:
        return set(tmp_path.rglob("*")) if tmp_path.exists() else set()

    before = _snapshot()
    with pytest.raises(ValueError):
        stores.get_instinct_store(workspace_id=hostile)
    assert _snapshot() == before
    assert not Path("/tmp/pwn").exists()
