# tests/cloud/test_executor_workspace_iso.py
# Created: 2026-06-26 (fix/cloud-iso-executor-scope) — C1 regression.
#
# Pins the per-workspace store isolation of the Instinct APPROVAL executors and
# the background/HTTP store paths that run OUTSIDE the agent chat stream (where
# the ``current_workspace`` ContextVar is NEVER set). Before the fix these all
# called BARE ``get_instinct_store()`` / ``get_fabric_store()`` with no
# workspace, so on CLOUD (``POCKETPAW_REQUIRE_WORKSPACE_SCOPE`` set) the bare
# call RAISED ``WorkspaceScopeRequired`` after the action was already flipped
# APPROVED (approved-but-never-executed), and on OSS (flag unset) it silently
# resolved to the legacy SHARED store while the proposal+approval lived in the
# per-workspace file → split-brain ledger.
#
# These tests DO NOT mock the store seam — they drive the REAL workspace-keyed
# factory against tmp store files and assert (a) the executor's store resolves
# to the SAME per-workspace file the router wrote the action to, and (b) the
# executor does not crash under the fail-closed flag. They FAIL on the pre-fix
# code (bare call) and PASS after threading the blob's workspace_id.
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import pocketpaw.stores as stores
from pocketpaw.instinct.models import ActionTrigger

pytestmark = pytest.mark.asyncio

WS = "ws-tenant-a"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the store data dir at a tmp path, clear the fail-closed flag and the
    ContextVar, and reset the bounded LRU between tests so a per-workspace store
    from one test never leaks into the next."""
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


def _trig() -> ActionTrigger:
    return ActionTrigger(type="agent", source="claude", reason="iso regression")


def _belt_action(action_id: str, *, workspace_id: str) -> SimpleNamespace:
    """An approved Belt code-change Action whose blob carries ``workspace_id`` —
    the exact shape ``belt/executor.execute_approved_change`` reads."""
    return SimpleNamespace(
        id=action_id,
        pocket_id=workspace_id,
        parameters={
            "_code_change": {
                "schema": 2,
                "kind": "code_change",
                "workspace_id": workspace_id,
                "requested_by": "u1",
                # No diff / repo: the executor bails after the chain bookkeeping,
                # which is all this test needs — the store resolution happens
                # BEFORE any git work.
                "base_branch": "main",
                "correlation_id": None,
            }
        },
    )


async def test_belt_executor_resolves_to_per_workspace_file(tmp_path: Path) -> None:
    """The router writes + approves on the per-workspace instinct file; the belt
    executor (HTTP approve path, no ContextVar) must mark that SAME action
    executed/failed on the SAME file — never the shared one."""
    from pocketpaw_ee.cloud.belt import executor as belt_executor

    # ROUTER PATH — scoped store, exactly as ee/.../instinct/router.py::_store(ws).
    router_store = stores.get_instinct_store(workspace_id=WS)
    act = await router_store.propose(
        pocket_id=WS,
        title="belt-change",
        description="",
        recommendation="",
        trigger=_trig(),
        workspace_id=WS,
        parameters=_belt_action("ignored", workspace_id=WS).parameters,
    )
    await router_store.approve(act.id, approver="u1")

    # EXECUTOR PATH — run OUTSIDE any identity (no ContextVar). With the fix the
    # executor threads the blob's workspace_id, so it opens the SAME file.
    await belt_executor.execute_approved_change(
        _belt_action(act.id, workspace_id=WS), pr_opener=_NoopOpener()
    )

    # The action the router approved is now terminal (executed OR failed) ON THE
    # PER-WORKSPACE FILE. Pre-fix the executor's bare store opened the SHARED
    # file, so this row stayed 'approved' here.
    refreshed = await router_store.get_action(act.id)
    assert refreshed is not None
    assert refreshed.status.value in {"executed", "failed"}, refreshed.status

    # And the executor must NOT have created the legacy shared file.
    assert not (tmp_path / "instinct.db").exists(), (
        "executor opened the SHARED instinct.db instead of the per-workspace file"
    )


async def test_belt_executor_does_not_crash_under_failclosed_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under the cloud fail-closed flag the executor must still resolve the
    tenant's file from the blob (not raise WorkspaceScopeRequired and leave the
    action approved-but-never-executed)."""
    from pocketpaw_ee.cloud.belt import executor as belt_executor

    router_store = stores.get_instinct_store(workspace_id=WS)
    act = await router_store.propose(
        pocket_id=WS,
        title="belt-change",
        description="",
        recommendation="",
        trigger=_trig(),
        workspace_id=WS,
        parameters=_belt_action("ignored", workspace_id=WS).parameters,
    )
    await router_store.approve(act.id, approver="u1")

    monkeypatch.setenv("POCKETPAW_REQUIRE_WORKSPACE_SCOPE", "1")
    stores.reset_store_caches()  # force re-resolution under the flag

    # Must NOT raise — the executor never re-raises into the router, but pre-fix
    # the bare store call raised internally and the action stayed approved.
    await belt_executor.execute_approved_change(
        _belt_action(act.id, workspace_id=WS), pr_opener=_NoopOpener()
    )

    # Re-read under the flag with the explicit workspace (the router's own path).
    refreshed = await stores.get_instinct_store(workspace_id=WS).get_action(act.id)
    assert refreshed is not None
    assert refreshed.status.value in {"executed", "failed"}, (
        "under the fail-closed flag the executor left the action "
        f"{refreshed.status.value!r} — it could not resolve the tenant store"
    )


class _NoopOpener:
    """A PrOpener that never touches git/GitHub — the executor bails before it
    on the no-diff blob, but the signature must be satisfied."""

    async def open_pr(self, **_kwargs) -> str:  # pragma: no cover - not reached
        return "https://example.invalid/pr/0"
