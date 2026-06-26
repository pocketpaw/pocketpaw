# tests/cloud/test_executor_workspace_iso.py
# Created: 2026-06-26 (fix/cloud-iso-executor-scope) — C1 regression.
# Updated: 2026-06-26 (FU-1) — parametrized across EVERY approve-path executor
# (and the discovery propose flow) that was threaded, not just Belt. No
# executor's per-workspace isolation may rest on an over-mocked store anymore.
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
# to the SAME per-workspace file the router wrote the action to (terminal status
# lands there; the legacy shared file is never created), and (b) the executor
# does not crash under the fail-closed flag. They FAIL on the pre-fix code (bare
# call) and PASS after threading the blob's workspace_id.
#
# Coverage map (executor → blob kind → store(s) scoped):
#   belt/executor.execute_approved_change            _code_change      instinct
#   external_actions/…execute_approved_external_action _external_action instinct
#   pocket_proposals/…execute_approved_pocket_create  _pocket_create    instinct
#   versions/instinct_executor.execute_approved_change _artifact_change instinct
#   fabric_proposals/…execute_approved_fabric_objects _fabric_objects  instinct+fabric
#   discovery/orchestrate.run_discovery_and_propose   (propose flow)   instinct
#
# Most cases drive the executor's EARLY validation-fail path (a correct
# workspace_id but an intentionally-bad blob), which still calls
# ``store.mark_failed`` on the resolved store — enough to prove WHICH file it
# opened without standing up connectors/git/beanie. fabric_objects uses a VALID
# blob so the SECOND scoped store (get_fabric_store) is exercised too.
from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import pocketpaw.stores as stores
from pocketpaw.instinct.models import ActionTrigger

pytestmark = pytest.mark.asyncio

WS = "ws-tenant-a"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the store data dir at a tmp path, clear the fail-closed flag and the
    ContextVar, and reset the bounded LRU between tests so a per-workspace store
    from one test never leaks into the next.

    NOTE the flag is cleared even when the flag-mode CI lane exports it process
    wide — these resolution tests set/clear it per-case so they assert the same
    way under either lane."""
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


class _NoopOpener:
    """A PrOpener that never touches git/GitHub — the belt executor bails before
    it on the no-diff blob, but the signature must be satisfied."""

    async def open_pr(self, **_kwargs) -> str:  # pragma: no cover - not reached
        return "https://example.invalid/pr/0"


# ---------------------------------------------------------------------------
# Per-executor specs. Each builds the Action ``parameters`` (CORRECT
# workspace_id, kind-specific blob) and an awaitable that runs the executor
# OUTSIDE any identity. ``terminal`` is True when the executor marks the Action
# executed/failed on the resolved instinct store (everything except the
# discovery propose flow).
# ---------------------------------------------------------------------------


def _belt_params(ws: str) -> dict:
    # Valid schema but no diff/repo → the executor closes the chain and bails
    # AFTER opening the instinct store; the action ends terminal on it.
    return {
        "_code_change": {
            "schema": 2,
            "kind": "code_change",
            "workspace_id": ws,
            "requested_by": "u1",
            "base_branch": "main",
            "correlation_id": None,
        }
    }


async def _run_belt(action: Any) -> None:
    from pocketpaw_ee.cloud.belt import executor as belt_executor

    await belt_executor.execute_approved_change(action, pr_opener=_NoopOpener())


def _external_params(ws: str) -> dict:
    # Wrong schema → the executor's schema guard marks_failed on the instinct
    # store it just resolved.
    return {
        "_external_action": {
            "schema": 999,  # mismatch → early _fail (mark_failed)
            "workspace_id": ws,
            "connector_name": "crm",
            "action": "noop",
            "params": {},
            "requested_by": "u1",
            "correlation_id": None,
        }
    }


async def _run_external(action: Any) -> None:
    from pocketpaw_ee.cloud.external_actions import executor as ext_executor

    await ext_executor.execute_approved_external_action(action)


def _pocket_create_params(ws: str) -> dict:
    return {
        "_pocket_create": {
            "schema": 999,  # mismatch → early _fail (mark_failed)
            "workspace_id": ws,
            "user_id": "u1",
            "pocket_spec": {"name": "x"},
            "correlation_id": None,
        }
    }


async def _run_pocket_create(action: Any) -> None:
    from pocketpaw_ee.cloud.pocket_proposals import executor as pc_executor

    await pc_executor.execute_approved_pocket_create(action)


def _artifact_change_params(ws: str) -> dict:
    # ``workspace`` set (the executor's tenant key) but ``to_version_id`` missing
    # → mark_failed on the resolved instinct store, before any beanie work.
    return {
        "_artifact_change": {
            "schema": 1,
            "scope_type": "pocket",
            "scope_id": "pocket-art",
            "branch": "main",
            "from_version_id": "ver-from",
            "to_version_id": "",  # missing → mark_failed
            "workspace": ws,
            "user_id": "u1",
        }
    }


async def _run_artifact_change(action: Any) -> None:
    from pocketpaw_ee.versions import instinct_executor as ver_executor

    await ver_executor.execute_approved_change(action)


def _fabric_objects_params(ws: str) -> dict:
    # VALID blob (schema 1, one type + one object) so the executor passes its
    # guards and reaches the SECOND scoped store, get_fabric_store(workspace_id).
    return {
        "_fabric_objects": {
            "schema": 1,
            "workspace_id": ws,
            "object_types": [
                {
                    "type_name": "Customer",
                    "properties": [{"name": "name", "type": "string"}],
                }
            ],
            "objects": [
                {
                    "type_name": "Customer",
                    "properties": {"name": "Acme Inc"},
                    "source_connector": "crm",
                    "source_id": "cust-1",
                }
            ],
            "links": [],
            "requested_by": "u1",
            "correlation_id": None,
        }
    }


async def _run_fabric_objects(action: Any) -> None:
    from pocketpaw_ee.cloud.fabric_proposals import executor as fo_executor

    await fo_executor.execute_approved_fabric_objects(action)


# (id, blob_params_builder, run_coro, asserts_instinct_terminal)
_EXECUTOR_CASES: list[tuple[str, Callable[[str], dict], Callable[[Any], Awaitable[None]], bool]] = [
    ("belt", _belt_params, _run_belt, True),
    ("external_action", _external_params, _run_external, True),
    ("pocket_create", _pocket_create_params, _run_pocket_create, True),
    ("artifact_change", _artifact_change_params, _run_artifact_change, True),
    ("fabric_objects", _fabric_objects_params, _run_fabric_objects, True),
]


async def _propose_and_approve(params: dict) -> Any:
    """ROUTER PATH — open the scoped instinct store exactly as
    ee/.../instinct/router.py::_store(ws) does, file the action carrying the
    blob, and approve it. Returns the approved Action."""
    router_store = stores.get_instinct_store(workspace_id=WS)
    act = await router_store.propose(
        pocket_id=WS,
        title="iso-case",
        description="",
        recommendation="",
        trigger=_trig(),
        workspace_id=WS,
        parameters=params,
    )
    await router_store.approve(act.id, approver="u1")
    return act


@pytest.mark.parametrize("case_id, build_params, run_executor, terminal", _EXECUTOR_CASES)
async def test_executor_resolves_to_per_workspace_file(
    case_id: str,
    build_params: Callable[[str], dict],
    run_executor: Callable[[Any], Awaitable[None]],
    terminal: bool,
    tmp_path: Path,
) -> None:
    """The router proposes+approves on the per-workspace instinct file; the
    executor (HTTP approve path, no ContextVar) must mark that SAME action
    terminal on the SAME file — never the shared one."""
    params = build_params(WS)
    act = await _propose_and_approve(params)

    # EXECUTOR PATH — run OUTSIDE any identity (no ContextVar). With the fix the
    # executor threads the blob's workspace, so it opens the SAME file.
    await run_executor(SimpleNamespace(id=act.id, pocket_id=WS, parameters=params))

    if terminal:
        refreshed = await stores.get_instinct_store(workspace_id=WS).get_action(act.id)
        assert refreshed is not None
        assert refreshed.status.value in {"executed", "failed"}, (
            f"[{case_id}] executor left the action {refreshed.status.value!r} on the "
            "per-workspace file — its store resolved elsewhere"
        )

    # The executor must NOT have created the legacy SHARED instinct file.
    assert not (tmp_path / "instinct.db").exists(), (
        f"[{case_id}] executor opened the SHARED instinct.db instead of the per-workspace file"
    )
    # fabric_objects also opens a fabric store — it must be the per-workspace one,
    # never the legacy shared fabric.db.
    if case_id == "fabric_objects":
        assert not (tmp_path / "fabric.db").exists(), (
            "fabric_objects executor opened the SHARED fabric.db instead of the "
            "per-workspace fabric file"
        )
        assert (tmp_path / "workspaces" / WS / "fabric.db").exists(), (
            "fabric_objects executor never opened the per-workspace fabric file"
        )


@pytest.mark.parametrize("case_id, build_params, run_executor, terminal", _EXECUTOR_CASES)
async def test_executor_does_not_crash_under_failclosed_flag(
    case_id: str,
    build_params: Callable[[str], dict],
    run_executor: Callable[[Any], Awaitable[None]],
    terminal: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under the cloud fail-closed flag the executor must still resolve the
    tenant's file from the blob (not raise WorkspaceScopeRequired and leave the
    action approved-but-never-executed)."""
    params = build_params(WS)
    act = await _propose_and_approve(params)

    monkeypatch.setenv("POCKETPAW_REQUIRE_WORKSPACE_SCOPE", "1")
    stores.reset_store_caches()  # force re-resolution under the flag

    # Must NOT raise — the executors never re-raise into the router, but pre-fix
    # the bare store call raised internally and the action stayed approved.
    await run_executor(SimpleNamespace(id=act.id, pocket_id=WS, parameters=params))

    if terminal:
        refreshed = await stores.get_instinct_store(workspace_id=WS).get_action(act.id)
        assert refreshed is not None
        assert refreshed.status.value in {"executed", "failed"}, (
            f"[{case_id}] under the fail-closed flag the executor left the action "
            f"{refreshed.status.value!r} — it could not resolve the tenant store"
        )


# ---------------------------------------------------------------------------
# discovery/orchestrate.run_discovery_and_propose — a PROPOSE flow (not an
# approve-time executor), but it opens the scoped instinct store on the same
# no-ContextVar path. A fake DiscoveryRun yields an EMPTY ontology so the run
# opens the per-workspace store, runs the supersede sweep against it, and
# returns cleanly — proving resolution without standing up connectors.
# ---------------------------------------------------------------------------


class _EmptyDiscoveryRun:
    async def run(self, *_a: Any, **_k: Any):
        from pocketpaw_ee.discovery.models import OntologyDraft

        return OntologyDraft()


async def test_discovery_propose_resolves_to_per_workspace_file(tmp_path: Path) -> None:
    from pocketpaw_ee.discovery.orchestrate import run_discovery_and_propose

    result = await run_discovery_and_propose(
        workspace_id=WS,
        user_id="u1",
        connector_ids=[],
        discovery_run=_EmptyDiscoveryRun(),
    )
    # Empty draft → no proposals filed, but the scoped store WAS opened + swept.
    assert result.fabric_objects_action_id is None
    assert result.pocket_action_id is None
    # The per-workspace instinct file was used; the legacy shared one was not.
    assert (tmp_path / "workspaces" / WS / "instinct.db").exists(), (
        "discovery never opened the per-workspace instinct file"
    )
    assert not (tmp_path / "instinct.db").exists(), (
        "discovery opened the SHARED instinct.db instead of the per-workspace file"
    )


async def test_discovery_propose_does_not_crash_under_failclosed_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pocketpaw_ee.discovery.orchestrate import run_discovery_and_propose

    monkeypatch.setenv("POCKETPAW_REQUIRE_WORKSPACE_SCOPE", "1")
    stores.reset_store_caches()

    # Must NOT raise WorkspaceScopeRequired — discovery threads its workspace_id.
    result = await run_discovery_and_propose(
        workspace_id=WS,
        user_id="u1",
        connector_ids=[],
        discovery_run=_EmptyDiscoveryRun(),
    )
    assert result.run_id  # the run completed and minted an id
    assert os.environ["POCKETPAW_REQUIRE_WORKSPACE_SCOPE"] == "1"  # flag was live
