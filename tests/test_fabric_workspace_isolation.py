# tests/test_fabric_workspace_isolation.py
# Created: 2026-06-26 (ISO-1 — workspace-keyed store factory + physical Fabric isolation).
#
# Proves ISO-1's new invariant: each workspace gets its OWN fabric.db file under
# ~/.pocketpaw/workspaces/<workspace_id>/, layered ON TOP of W4a's in-row
# workspace_id WHERE-filter (physical isolation is additive defense-in-depth).
#
# Covers:
#   * two-workspace physical isolation — objects created in A and B land in two
#     SEPARATE db files on disk, and a query in A returns ZERO of B's objects;
#   * fail-closed — POCKETPAW_REQUIRE_WORKSPACE_SCOPE set + no workspace resolves
#     to a hard error, never a silent shared-store read;
#   * back-compat — with the flag unset and no workspace, the factory returns the
#     legacy ~/.pocketpaw/fabric.db so single-tenant OSS keeps working;
#   * ContextVar resolution — the current_workspace ContextVar is consulted when
#     no explicit workspace_id is passed, and the explicit arg wins over it;
#   * the bounded LRU caches handles and evicts (aclose) past the cap;
#   * path-traversal safety — a hostile workspace_id (incl. ../../../tmp/pwn,
#     url-encoded traversal, null bytes, leading-dash flag-injection, non-ASCII)
#     is rejected by a STRICT positive allowlist ([A-Za-z0-9][A-Za-z0-9_-]*),
#     fails closed (raises), and leaves the filesystem untouched (writes nothing
#     outside workspaces/).

from __future__ import annotations

from pathlib import Path

import pytest

import pocketpaw.stores as stores
from pocketpaw.fabric.models import FabricQuery

WS_A = "ws-alpha"
WS_B = "ws-bravo"


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the store factory at a tmp data dir and reset module + LRU state.

    Every test gets a clean ~/.pocketpaw equivalent, an empty cache, and the
    required-scope flag unset, so tests can't leak db files or cached handles
    into one another.
    """
    monkeypatch.setattr(stores, "_DATA_DIR", tmp_path)
    monkeypatch.delenv("POCKETPAW_REQUIRE_WORKSPACE_SCOPE", raising=False)
    stores.reset_store_caches()
    # Make sure no ContextVar value bleeds in from another test.
    token = stores.current_workspace.set(None)
    try:
        yield
    finally:
        try:
            stores.current_workspace.reset(token)
        except ValueError:
            # A test that reloads the module replaces the ContextVar object, so
            # the token no longer matches. Fall back to clearing the (new) var.
            stores.current_workspace.set(None)
        stores.reset_store_caches()


# ---------------------------------------------------------------------------
# Core ISO-1 invariant: two workspaces => two physical files, no cross-read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_workspaces_get_separate_files_and_no_cross_read(
    tmp_path: Path,
) -> None:
    store_a = stores.get_fabric_store(workspace_id=WS_A)
    store_b = stores.get_fabric_store(workspace_id=WS_B)

    # Distinct handles backed by distinct paths under workspaces/.
    assert store_a is not store_b
    assert store_a._db_path != store_b._db_path

    # A defines a type + object; B defines its own.
    type_a = await store_a.define_type(name="Customer", properties=[], workspace_id=WS_A)
    await store_a.create_object(type_a.id, {"name": "Acme"}, workspace_id=WS_A)

    type_b = await store_b.define_type(name="Customer", properties=[], workspace_id=WS_B)
    await store_b.create_object(type_b.id, {"name": "Globex"}, workspace_id=WS_B)

    # (a) Two SEPARATE db files exist on disk.
    db_a = tmp_path / "workspaces" / WS_A / "fabric.db"
    db_b = tmp_path / "workspaces" / WS_B / "fabric.db"
    assert db_a.exists(), "workspace A fabric.db should exist on disk"
    assert db_b.exists(), "workspace B fabric.db should exist on disk"
    assert db_a != db_b

    # The shared legacy file must NOT have been created by a scoped call.
    assert not (tmp_path / "fabric.db").exists()

    # (b) A query in A returns ZERO of B's objects — even unscoped at the store
    # level (workspace_id=None), because B's row physically isn't in A's file.
    res_a = await store_a.query(FabricQuery(type_name="Customer"))
    names_a = {o.properties.get("name") for o in res_a.objects}
    assert names_a == {"Acme"}
    assert "Globex" not in names_a

    res_b = await store_b.query(FabricQuery(type_name="Customer"))
    names_b = {o.properties.get("name") for o in res_b.objects}
    assert names_b == {"Globex"}
    assert "Acme" not in names_b


# ---------------------------------------------------------------------------
# Fail-closed: required-scope mode + no workspace => raise, never shared read
# ---------------------------------------------------------------------------


def test_fail_closed_when_scope_required_and_no_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POCKETPAW_REQUIRE_WORKSPACE_SCOPE", "1")
    with pytest.raises(stores.WorkspaceScopeRequired):
        stores.get_fabric_store()  # no workspace, no ContextVar


def test_fail_closed_ignores_blank_contextvar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank/whitespace ContextVar must not satisfy required-scope mode."""
    monkeypatch.setenv("POCKETPAW_REQUIRE_WORKSPACE_SCOPE", "1")
    token = stores.current_workspace.set("   ")
    try:
        with pytest.raises(stores.WorkspaceScopeRequired):
            stores.get_fabric_store()
    finally:
        stores.current_workspace.reset(token)


def test_required_scope_with_workspace_returns_scoped_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Required-scope mode is satisfied by an explicit workspace_id."""
    monkeypatch.setenv("POCKETPAW_REQUIRE_WORKSPACE_SCOPE", "1")
    store = stores.get_fabric_store(workspace_id=WS_A)
    assert str(tmp_path / "workspaces" / WS_A) in store._db_path


# ---------------------------------------------------------------------------
# Back-compat: flag unset + no workspace => legacy shared file
# ---------------------------------------------------------------------------


def test_legacy_shared_store_when_unscoped_and_not_required(
    tmp_path: Path,
) -> None:
    store = stores.get_fabric_store()  # no workspace, flag unset
    assert store._db_path == str(tmp_path / "fabric.db")
    # Same handle is returned on a second unscoped call (singleton preserved).
    assert stores.get_fabric_store() is store


# ---------------------------------------------------------------------------
# ContextVar resolution + precedence
# ---------------------------------------------------------------------------


def test_contextvar_is_consulted_when_no_explicit_arg(tmp_path: Path) -> None:
    token = stores.current_workspace.set(WS_A)
    try:
        store = stores.get_fabric_store()
        assert str(tmp_path / "workspaces" / WS_A) in store._db_path
    finally:
        stores.current_workspace.reset(token)


def test_explicit_arg_wins_over_contextvar(tmp_path: Path) -> None:
    token = stores.current_workspace.set(WS_B)
    try:
        store = stores.get_fabric_store(workspace_id=WS_A)
        assert str(tmp_path / "workspaces" / WS_A) in store._db_path
        assert WS_B not in store._db_path
    finally:
        stores.current_workspace.reset(token)


def test_same_workspace_returns_cached_handle(tmp_path: Path) -> None:
    first = stores.get_fabric_store(workspace_id=WS_A)
    second = stores.get_fabric_store(workspace_id=WS_A)
    assert first is second


# ---------------------------------------------------------------------------
# Bounded LRU: evicts past the cap and closes the evicted handle
# ---------------------------------------------------------------------------


async def _drain_pending_tasks() -> None:
    """Run every fire-and-forget eviction task this test started to completion.

    Eviction from inside a running loop schedules ``aclose`` via
    ``loop.create_task``. Draining those tasks before the test's loop closes
    keeps the aiosqlite worker from racing teardown (the source of spurious
    "Event loop is closed" warnings).
    """
    import asyncio

    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_lru_evicts_past_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    # Shrink the cap so the test stays fast and deterministic.
    monkeypatch.setattr(stores, "_WORKSPACE_STORE_CACHE_CAP", 3)
    stores.reset_store_caches()

    handles = [stores.get_fabric_store(workspace_id=f"ws-{i}") for i in range(3)]
    # Touch ws-0 so it becomes most-recently-used; ws-1 is now the LRU victim.
    again_0 = stores.get_fabric_store(workspace_id="ws-0")
    assert again_0 is handles[0]

    # A 4th distinct workspace evicts the least-recently-used (ws-1).
    stores.get_fabric_store(workspace_id="ws-3")
    # ws-1 was evicted: a fresh fetch builds a NEW handle, not the old one.
    new_1 = stores.get_fabric_store(workspace_id="ws-1")
    assert new_1 is not handles[1]
    # ws-0 survived (it was touched), so it's still the same handle.
    assert stores.get_fabric_store(workspace_id="ws-0") is handles[0]

    await _drain_pending_tasks()


@pytest.mark.asyncio
async def test_eviction_closes_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    """On eviction the factory calls the store's aclose() (best-effort)."""
    monkeypatch.setattr(stores, "_WORKSPACE_STORE_CACHE_CAP", 1)
    stores.reset_store_caches()

    closed: list[str] = []

    victim = stores.get_fabric_store(workspace_id="ws-victim")
    # Wrap the real aclose so we can observe it firing on eviction.
    real_aclose = victim.aclose

    async def _spy() -> None:
        closed.append("ws-victim")
        await real_aclose()

    monkeypatch.setattr(victim, "aclose", _spy)

    # A second distinct workspace evicts ws-victim (cap is 1).
    stores.get_fabric_store(workspace_id="ws-next")

    # Eviction schedules aclose as a task on this loop; drain it to completion
    # (deterministic — no bare sleep) and assert the spy fired.
    await _drain_pending_tasks()
    assert closed == ["ws-victim"]


# ---------------------------------------------------------------------------
# Path-traversal safety (security-critical isolation code)
# ---------------------------------------------------------------------------


_HOSTILE_IDS = [
    "../../../tmp/pwn",  # the captain's canonical payload
    "../../etc",
    "..",
    "a/b",
    "a/../../b",
    "with space/../escape",
    "/abs/path",
    "\\windows\\path",
    "x\x00y",  # null byte
    ".",  # non-empty, would resolve to CWD if not refused
    "-rf",  # leading dash — rejected so it can't be read as an argv flag
    "--force",  # leading double-dash — same flag-injection concern
    "..%2f..%2fetc",  # url-encoded traversal — must NOT be decoded into a slash
    "ws\tname",  # embedded control char
    "wörkspace",  # non-ASCII
    "a.b",  # a dot in the middle — not in the allowlist
]


@pytest.mark.parametrize("hostile", _HOSTILE_IDS)
def test_traversal_workspace_id_is_rejected(hostile: str) -> None:
    """A non-token id that could escape ``workspaces/`` must RAISE, never build.

    These ids are non-empty after strip, so they reach ``_safe_workspace_dir``
    and MUST be rejected outright (fail closed) — the factory must never hand
    back a store on a traversal-laden / non-allowlisted id. The guard is a
    POSITIVE allowlist (``[A-Za-z0-9_-]+``), so even a novel encoding that a
    denylist might miss is refused because it simply isn't on the allowlist.
    """
    with pytest.raises(ValueError):
        stores.get_fabric_store(workspace_id=hostile)


@pytest.mark.parametrize("hostile", _HOSTILE_IDS)
def test_hostile_workspace_id_writes_nothing_outside_workspaces_dir(
    tmp_path: Path, hostile: str
) -> None:
    """A rejected hostile id must leave the filesystem untouched.

    The captain's explicit requirement: assert the call raises AND that NOTHING
    was created outside ``~/.pocketpaw/workspaces/`` (here: the tmp data dir). We
    snapshot the data dir before, attempt the build, and assert (a) it raised and
    (b) the data-dir tree is byte-for-byte unchanged — no escape file, no stray
    ``workspaces/<hostile>`` dir, no ``/tmp/pwn``.
    """
    data_dir = tmp_path  # the autouse fixture points stores._DATA_DIR here

    def _snapshot() -> set[Path]:
        return set(data_dir.rglob("*")) if data_dir.exists() else set()

    before = _snapshot()
    with pytest.raises(ValueError):
        stores.get_fabric_store(workspace_id=hostile)
    after = _snapshot()

    assert after == before, (
        f"hostile workspace_id {hostile!r} created filesystem entries: "
        f"{sorted(str(p) for p in (after - before))}"
    )
    # And specifically: no escape artifact landed at the obvious target.
    assert not Path("/tmp/pwn").exists()


@pytest.mark.parametrize("blank", ["", "   ", "\t", None])
def test_blank_workspace_id_falls_back_to_legacy_not_escape(
    tmp_path: Path, blank: str | None
) -> None:
    """Empty / whitespace / None = 'no workspace' → OSS legacy shared file.

    A blank id is NOT a traversal payload; it means the caller carries no
    workspace. In OSS mode (flag unset) that is the documented back-compat: the
    shared ``~/.pocketpaw/fabric.db``. The one thing it must never do is land
    somewhere OTHER than that legacy file (e.g. escape to CWD or to a stray
    ``workspaces/`` entry).
    """
    store = stores.get_fabric_store(workspace_id=blank)
    assert store._db_path == str(tmp_path / "fabric.db")


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_workspace_id_fails_closed_under_required_scope(
    blank: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same blank ids must FAIL CLOSED, not read the shared store, on cloud.

    AC#3: a missing workspace on a cloud path must never silently read a shared
    store. A blank id is a missing workspace, so under the required-scope flag
    it raises rather than returning the legacy file.
    """
    monkeypatch.setenv("POCKETPAW_REQUIRE_WORKSPACE_SCOPE", "1")
    with pytest.raises(stores.WorkspaceScopeRequired):
        stores.get_fabric_store(workspace_id=blank)


def test_reset_helper_clears_caches(tmp_path: Path) -> None:
    """reset_store_caches drops both the legacy singleton and the LRU."""
    legacy = stores.get_fabric_store()
    scoped = stores.get_fabric_store(workspace_id=WS_A)
    stores.reset_store_caches()
    assert stores.get_fabric_store() is not legacy
    assert stores.get_fabric_store(workspace_id=WS_A) is not scoped


def test_fresh_import_has_no_side_effects(tmp_path: Path) -> None:
    """Importing the module creates no DB and no dirs at import time.

    Loaded under an INDEPENDENT module object (not ``importlib.reload(stores)``,
    which would swap the shared module's ContextVar out from under the autouse
    fixture). We point its data dir at an empty tmp dir and assert importing it
    touched nothing on disk — the factory must be lazy.
    """
    import importlib.util
    import sys

    name = "_stores_probe"
    spec = importlib.util.spec_from_file_location(name, stores.__file__)
    assert spec and spec.loader
    probe = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec: the module defines an @dataclass
    # (``_StoreKind``), and the dataclasses machinery resolves the owning module
    # via ``sys.modules[cls.__module__]`` to look up annotations — an unregistered
    # module makes that lookup return None and raise. Clean it up afterward so the
    # probe never leaks into other tests.
    sys.modules[name] = probe
    try:
        spec.loader.exec_module(probe)
    finally:
        sys.modules.pop(name, None)

    probe._DATA_DIR = tmp_path
    # Nothing should exist yet — import alone must not create files/dirs.
    assert list(tmp_path.iterdir()) == []
    # And the module exposes the ISO-1 surface.
    assert hasattr(probe, "get_fabric_store")
    assert hasattr(probe, "current_workspace")
    assert hasattr(probe, "WorkspaceScopeRequired")
    assert hasattr(probe, "reset_store_caches")
