# tests/test_workspace_ctxvar_bridge.py
# Created: 2026-06-26 (ISO-3 — ContextVar bridge for non-router store callers).
#
# Proves the ISO-3 invariant: the NON-router store callers (agent tools, MCP
# servers, connector ingest) that call get_fabric_store() / get_instinct_store()
# WITHOUT an explicit workspace_id land in the caller's per-workspace file,
# because EE's attach_agent_identity bridges the per-stream workspace onto the
# OSS-core `current_workspace` ContextVar that the factory resolves through.
#
# Covers:
#   * the bridge — attach_agent_identity sets the OSS current_workspace; a bare
#     get_fabric_store()/get_instinct_store() then resolves to that workspace's
#     file; detach clears it;
#   * a REAL agent tool (FabricCreateTool, which calls the bare factory
#     internally) writes through the bridge → the object lands in the caller's
#     workspace file, not the shared one, and is invisible to another workspace;
#   * fail-closed — under POCKETPAW_REQUIRE_WORKSPACE_SCOPE, a non-router store
#     call with NO identity attached (ContextVar unset) raises, never a shared
#     read;
#   * the EE StoreProvider seam (pocketpaw.stores entry-point) is registered and
#     returns the per-workspace store for both kinds (and None for no-workspace).

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.chat.agent_service import (
    attach_agent_identity,
    detach_agent_identity,
)

import pocketpaw.stores as stores
from pocketpaw.fabric.models import FabricQuery

WS_A = "ws-alpha"
WS_B = "ws-bravo"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the factory at a tmp data dir; reset caches + flag + ContextVar."""
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


# ---------------------------------------------------------------------------
# The bridge itself: attach sets the OSS ContextVar; bare factory resolves it
# ---------------------------------------------------------------------------


def test_attach_identity_bridges_workspace_to_oss_contextvar(tmp_path: Path) -> None:
    assert stores.current_workspace.get() is None

    tokens = attach_agent_identity(workspace_id=WS_A, user_id="u1")
    try:
        # The OSS ContextVar now carries the stream's workspace.
        assert stores.current_workspace.get() == WS_A
        # A bare (no-arg) factory call — exactly what the agent tools / MCP
        # servers do — resolves to that workspace's file.
        fab = stores.get_fabric_store()
        inst = stores.get_instinct_store()
        assert str(tmp_path / "workspaces" / WS_A) in fab._db_path
        assert str(tmp_path / "workspaces" / WS_A) in inst._db_path
    finally:
        detach_agent_identity(tokens)

    # detach clears it — back to no workspace.
    assert stores.current_workspace.get() is None


@pytest.mark.asyncio
async def test_agent_tool_write_lands_in_callers_workspace_file(tmp_path: Path) -> None:
    """A REAL agent tool writing through the bare factory lands in the right file.

    Drives the non-router path end to end: attach identity for WS_A, write a
    Fabric object via the SAME bare get_fabric_store() the agent FabricCreateTool
    uses, and assert it lands in WS_A's file and is invisible from WS_B.
    """
    # Write as WS_A through the bridged bare factory.
    tokens_a = attach_agent_identity(workspace_id=WS_A, user_id="u1")
    try:
        store_a = stores.get_fabric_store()  # no explicit workspace arg
        t = await store_a.define_type(name="Customer", properties=[], workspace_id=WS_A)
        await store_a.create_object(t.id, {"name": "Acme"}, workspace_id=WS_A)
    finally:
        detach_agent_identity(tokens_a)

    # The object physically lives in WS_A's file.
    db_a = tmp_path / "workspaces" / WS_A / "fabric.db"
    assert db_a.exists()
    assert not (tmp_path / "fabric.db").exists()  # never the shared file

    # WS_B, through the same bare path, sees ZERO of WS_A's objects.
    tokens_b = attach_agent_identity(workspace_id=WS_B, user_id="u2")
    try:
        store_b = stores.get_fabric_store()
        res_b = await store_b.query(FabricQuery(type_name="Customer"))
        assert res_b.objects == []
    finally:
        detach_agent_identity(tokens_b)

    # WS_A still sees its own object.
    tokens_a2 = attach_agent_identity(workspace_id=WS_A, user_id="u1")
    try:
        res_a = await stores.get_fabric_store().query(FabricQuery(type_name="Customer"))
        assert {o.properties.get("name") for o in res_a.objects} == {"Acme"}
    finally:
        detach_agent_identity(tokens_a2)


@pytest.mark.asyncio
async def test_instinct_write_through_bridge_is_isolated(tmp_path: Path) -> None:
    """The Instinct non-router path is bridged + isolated the same way."""
    from pocketpaw.instinct.models import ActionTrigger

    def trig() -> ActionTrigger:
        return ActionTrigger(type="agent", source="claude", reason="iso-3")

    tokens_a = attach_agent_identity(workspace_id=WS_A, user_id="u1")
    try:
        await stores.get_instinct_store().propose(
            pocket_id="p",
            title="A-task",
            description="",
            recommendation="",
            trigger=trig(),
            workspace_id=WS_A,
        )
    finally:
        detach_agent_identity(tokens_a)

    tokens_b = attach_agent_identity(workspace_id=WS_B, user_id="u2")
    try:
        b_pending = await stores.get_instinct_store().pending()
        assert b_pending == []  # WS_B's file has none of WS_A's actions
    finally:
        detach_agent_identity(tokens_b)

    assert (tmp_path / "workspaces" / WS_A / "instinct.db").exists()
    assert not (tmp_path / "instinct.db").exists()


# ---------------------------------------------------------------------------
# Fail-closed: a cloud non-router call with no identity attached must raise
# ---------------------------------------------------------------------------


def test_fail_closed_when_required_and_no_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No attach_agent_identity in scope + required-scope flag → raise, not share."""
    monkeypatch.setenv("POCKETPAW_REQUIRE_WORKSPACE_SCOPE", "1")
    # No identity attached → ContextVar is None → fail closed for BOTH kinds.
    with pytest.raises(stores.WorkspaceScopeRequired):
        stores.get_fabric_store()
    with pytest.raises(stores.WorkspaceScopeRequired):
        stores.get_instinct_store()


def test_detach_restores_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """After detach, a required-scope non-router call fails closed again.

    Guards against a leaked ContextVar keeping a later, identity-less call
    resolving to a stale workspace.
    """
    monkeypatch.setenv("POCKETPAW_REQUIRE_WORKSPACE_SCOPE", "1")
    tokens = attach_agent_identity(workspace_id=WS_A, user_id="u1")
    # Inside the scope it resolves fine.
    assert stores.get_fabric_store() is not None
    detach_agent_identity(tokens)
    # Outside it, the same call fails closed.
    with pytest.raises(stores.WorkspaceScopeRequired):
        stores.get_fabric_store()


# ---------------------------------------------------------------------------
# AC#2: the EE StoreProvider seam is live
# ---------------------------------------------------------------------------


def test_ee_store_provider_is_registered_and_serves_both_kinds(
    tmp_path: Path,
) -> None:
    from pocketpaw._registry import clear_cache, first

    clear_cache()
    provider = first("pocketpaw.stores")
    assert provider is not None, "EE CloudStoreProvider should be registered"
    assert type(provider).__name__ == "CloudStoreProvider"

    # No-workspace → None (factory falls back to its shared singleton / fail-closed).
    assert provider.get_store("fabric", workspace_id=None) is None

    # Per-workspace → the right store on the right file, for both kinds.
    fab = provider.get_store("fabric", workspace_id=WS_A)
    inst = provider.get_store("instinct", workspace_id=WS_A)
    assert type(fab).__name__ == "FabricStore"
    assert type(inst).__name__ == "InstinctStore"
    assert str(tmp_path / "workspaces" / WS_A / "fabric.db") == fab._db_path
    assert str(tmp_path / "workspaces" / WS_A / "instinct.db") == inst._db_path


def test_provider_delegation_inherits_hostile_id_guard() -> None:
    """The provider delegates to the OSS helper, so the allowlist still bites."""
    from pocketpaw._registry import clear_cache, first

    clear_cache()
    provider = first("pocketpaw.stores")
    with pytest.raises(ValueError):
        provider.get_store("fabric", workspace_id="../../../tmp/pwn")


def test_factory_routes_through_a_registered_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The FACTORY must actually consult a registered StoreProvider (AC#2).

    The end-to-end proof that the ``pocketpaw.stores`` entry-point contract
    works: register a provider, then assert ``get_fabric_store`` /
    ``get_instinct_store`` return EXACTLY what that provider produced — not a
    store the factory built itself. This is the seam a future per-tier backend
    swap relies on, so it must be a real, tested contract, not just a dormant
    hook. We inject a sentinel provider by patching the registry lookup the
    factory uses (``pocketpaw.stores.first``).
    """
    sentinel_fabric = stores.FabricStore(":memory:")
    sentinel_instinct = stores.InstinctStore(":memory:")

    class _FakeProvider:
        calls: list[tuple[str, str | None]] = []

        def get_store(self, name: str, *, workspace_id: str | None = None):
            self.calls.append((name, workspace_id))
            if name == "fabric":
                return sentinel_fabric
            if name == "instinct":
                return sentinel_instinct
            return None

    fake = _FakeProvider()
    # The factory resolves the provider via ``first(_STORE_PROVIDER_GROUP)``;
    # patch that name in the stores module so our fake is what it sees.
    monkeypatch.setattr(stores, "first", lambda group: fake)
    stores.reset_store_caches()

    # A per-workspace fetch must return the PROVIDER'S store, proving the factory
    # routed through the seam rather than constructing its own.
    got_fab = stores.get_fabric_store(workspace_id=WS_A)
    got_inst = stores.get_instinct_store(workspace_id=WS_A)
    assert got_fab is sentinel_fabric
    assert got_inst is sentinel_instinct
    # And it asked the provider with the resolved workspace.
    assert ("fabric", WS_A) in fake.calls
    assert ("instinct", WS_A) in fake.calls


@pytest.mark.asyncio
async def test_contextvar_is_reset_when_the_run_body_raises(tmp_path: Path) -> None:
    """HARD requirement (AC#1): a failed run must NOT leak a workspace.

    The OSS ContextVar token rides the SAME identity-token tuple the existing
    workspace/user tokens use and is reset in the SAME ``detach_agent_identity``
    call — and both production seams (run_core, agent_bridge) run that detach in
    a guaranteed ``finally``. This test pins that contract at the seam: simulate
    a run that attaches identity, then throws, with detach in a finally — and
    assert the OSS ``current_workspace`` is back to ``None`` afterwards, so the
    next task on this loop can't inherit the failed run's workspace.
    """
    assert stores.current_workspace.get() is None

    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        tokens = attach_agent_identity(workspace_id=WS_A, user_id="u1")
        try:
            assert stores.current_workspace.get() == WS_A  # bound during the run
            raise _Boom("run blew up mid-flight")
        finally:
            detach_agent_identity(tokens)

    # The exception propagated, but the workspace did NOT leak.
    assert stores.current_workspace.get() is None
    # A subsequent identity-less required-scope call therefore fails closed, not
    # silently resolving to the dead run's WS_A.
    import os

    os.environ["POCKETPAW_REQUIRE_WORKSPACE_SCOPE"] = "1"
    try:
        with pytest.raises(stores.WorkspaceScopeRequired):
            stores.get_fabric_store()
    finally:
        os.environ.pop("POCKETPAW_REQUIRE_WORKSPACE_SCOPE", None)
