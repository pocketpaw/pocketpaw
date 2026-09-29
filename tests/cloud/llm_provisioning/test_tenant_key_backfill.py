# tests/cloud/llm_provisioning/test_tenant_key_backfill.py — the retry for a
# failed tenant-key mint, and the shutdown drain for background LiteLLM work.
#
# Workspace create mints in the background and never retries, so
# ``backfill_tenant_keys`` (run from the 5-minute sweep loop) is what gets a
# workspace off the master key after a failed mint. These tests pin who it mints
# for (unkeyed and keyless-row workspaces, not keyed or soft-deleted ones), the
# per-pass cap, that one failure does not stop the pass, and that an unreachable
# proxy ends the pass early. The last group pins the cloud shutdown drain: it
# calls both drains and survives one that raises or hangs.

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime

import httpx
import pocketpaw_ee.cloud.llm_provisioning.service as svc
import pytest
from pocketpaw_ee.cloud.models.litellm_key import LiteLLMTenantKey
from pocketpaw_ee.cloud.models.workspace import Workspace

pytestmark = pytest.mark.usefixtures("mongo_db")


class _Admin:
    """Records every mint; raises for workspaces listed in ``fail``."""

    def __init__(self, fail: dict[str, Exception] | None = None) -> None:
        self.minted: list[str] = []
        self.fail = fail or {}

    async def generate_key(self, **kwargs):
        ws = kwargs["metadata"]["workspace_id"]
        if ws in self.fail:
            raise self.fail[ws]
        self.minted.append(ws)
        return {"key": f"sk-{ws}"}


async def _ws(slug: str, *, deleted: bool = False) -> str:
    doc = Workspace(
        name=slug, slug=slug, owner="u1", deleted_at=datetime.now(UTC) if deleted else None
    )
    await doc.insert()
    return str(doc.id)


async def test_backfill_mints_only_for_live_unkeyed_workspaces() -> None:
    unkeyed = await _ws("unkeyed")
    keyless_row = await _ws("keyless-row")
    keyed = await _ws("keyed")
    deleted = await _ws("deleted", deleted=True)
    await LiteLLMTenantKey(workspace=keyed, litellm_key="sk-old").insert()
    # A spend-sweep bookkeeping row with no key still counts as unkeyed.
    await LiteLLMTenantKey(workspace=keyless_row).insert()

    admin = _Admin()
    summary = await svc.backfill_tenant_keys(admin_client=admin)

    assert sorted(admin.minted) == sorted([unkeyed, keyless_row])
    assert summary == {"candidates": 2, "minted": 2, "failed": 0}
    for ws in (unkeyed, keyless_row):
        row = await LiteLLMTenantKey.find_one(LiteLLMTenantKey.workspace == ws)
        assert row is not None and row.litellm_key == f"sk-{ws}"
    kept = await LiteLLMTenantKey.find_one(LiteLLMTenantKey.workspace == keyed)
    assert kept is not None and kept.litellm_key == "sk-old"
    assert await LiteLLMTenantKey.find_one(LiteLLMTenantKey.workspace == deleted) is None

    # A second pass has nothing left to do.
    admin2 = _Admin()
    assert (await svc.backfill_tenant_keys(admin_client=admin2))["candidates"] == 0
    assert admin2.minted == []


async def test_backfill_respects_the_per_pass_cap() -> None:
    for i in range(5):
        await _ws(f"w{i}")

    admin = _Admin()
    summary = await svc.backfill_tenant_keys(limit=3, admin_client=admin)
    assert len(admin.minted) == 3
    assert summary["candidates"] == 3

    # The next pass picks up the rest.
    admin2 = _Admin()
    await svc.backfill_tenant_keys(limit=3, admin_client=admin2)
    assert len(admin2.minted) == 2
    assert not set(admin.minted) & set(admin2.minted)


async def test_one_failing_mint_is_logged_and_the_pass_continues(caplog) -> None:
    a = await _ws("a")
    b = await _ws("b")
    c = await _ws("c")
    admin = _Admin(fail={b: svc.LiteLLMAdminError("proxy said 500")})

    with caplog.at_level(logging.INFO, logger=svc.__name__):
        summary = await svc.backfill_tenant_keys(admin_client=admin)

    assert sorted(admin.minted) == sorted([a, c])
    assert summary == {"candidates": 3, "minted": 2, "failed": 1}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("mint failed" in r.getMessage() and b in r.getMessage() for r in warnings)
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("backfill_tenant_keys: minted" in m and a in m for m in infos)
    assert await LiteLLMTenantKey.find_one(LiteLLMTenantKey.workspace == b) is None


async def test_unreachable_proxy_ends_the_pass_early(caplog) -> None:
    ids = [await _ws(f"p{i}") for i in range(3)]
    # Newest first: the first mint attempted is the last workspace created.
    admin = _Admin(fail={ids[-1]: httpx.ConnectError("refused")})

    with caplog.at_level(logging.WARNING, logger=svc.__name__):
        summary = await svc.backfill_tenant_keys(admin_client=admin)

    assert admin.minted == []
    assert summary == {"candidates": 3, "minted": 0, "failed": 1}
    assert any("proxy unreachable" in r.getMessage() for r in caplog.records)


async def test_backfill_never_raises_when_listing_fails(monkeypatch) -> None:
    async def _boom(limit):
        raise RuntimeError("mongo down")

    monkeypatch.setattr(svc, "_unkeyed_workspace_ids", _boom)
    assert await svc.backfill_tenant_keys(admin_client=_Admin()) == {
        "candidates": 0,
        "minted": 0,
        "failed": 0,
    }


def test_the_sweep_loop_runs_the_backfill() -> None:
    """The backfill only helps if something calls it on a schedule, and it must
    stay off the boot pass, where a down proxy would hold startup."""
    from pocketpaw_ee import extensions

    cluster, _per_host = extensions._sweeps()
    assert "backfill_tenant_keys" in [fn.__name__ for fn in cluster]
    assert "backfill_tenant_keys" in extensions._TICK_ONLY_SWEEPS


async def test_the_boot_pass_skips_the_backfill_and_the_tick_runs_it(monkeypatch) -> None:
    from pocketpaw_ee import extensions

    calls: list[str] = []

    def _fn(name):
        async def fn():
            calls.append(name)

        fn.__name__ = name
        return fn

    monkeypatch.setattr(
        extensions,
        "_sweeps",
        lambda: ([_fn("sweep_stale_runs"), _fn("backfill_tenant_keys")], []),
    )
    await extensions._run_sweeps(boot=True)
    assert calls == ["sweep_stale_runs"]
    calls.clear()
    await extensions._run_sweeps()
    assert calls == ["sweep_stale_runs", "backfill_tenant_keys"]


# ---- shutdown drain ---------------------------------------------------------


def _patch_drains(monkeypatch, mints, ingests) -> None:
    import pocketpaw_ee.cloud.llm_provisioning.run_end_trigger as ret

    monkeypatch.setattr(svc, "drain_pending_mints", mints)
    monkeypatch.setattr(ret, "drain_pending", ingests)


async def test_shutdown_drain_calls_both_drains(monkeypatch) -> None:
    from pocketpaw_ee.cloud import _drain_llm_provisioning

    called: list[tuple[str, float]] = []

    async def _mints(timeout):
        called.append(("mints", timeout))

    async def _ingests(timeout):
        called.append(("ingests", timeout))

    _patch_drains(monkeypatch, _mints, _ingests)
    await _drain_llm_provisioning(timeout=2.0)
    assert sorted(called) == [("ingests", 2.0), ("mints", 2.0)]


async def test_shutdown_drain_survives_a_raising_drain(monkeypatch, caplog) -> None:
    from pocketpaw_ee.cloud import _drain_llm_provisioning

    ran: list[str] = []

    async def _mints(timeout):
        raise RuntimeError("drain exploded")

    async def _ingests(timeout):
        ran.append("ingests")

    _patch_drains(monkeypatch, _mints, _ingests)
    with caplog.at_level(logging.WARNING):
        await _drain_llm_provisioning(timeout=1.0)  # must not raise
    assert ran == ["ingests"]
    assert any("drain failed" in r.getMessage() for r in caplog.records)


async def test_shutdown_drain_survives_a_hanging_drain(monkeypatch) -> None:
    from pocketpaw_ee.cloud import _drain_llm_provisioning

    async def _hang(timeout):
        await asyncio.sleep(60)  # ignores its own timeout

    async def _ok(timeout):
        return None

    _patch_drains(monkeypatch, _hang, _ok)
    t0 = time.perf_counter()
    await _drain_llm_provisioning(timeout=0.2)  # must not raise
    assert time.perf_counter() - t0 < 2.0


def test_the_drain_is_a_cloud_shutdown_hook() -> None:
    import ast
    import inspect
    import textwrap

    from pocketpaw_ee import cloud

    tree = ast.parse(textwrap.dedent(inspect.getsource(cloud.mount_cloud)))
    hooks = [
        fn
        for fn in ast.walk(tree)
        if isinstance(fn, ast.AsyncFunctionDef)
        and any(isinstance(d, ast.Name) and d.id == "on_shutdown" for d in fn.decorator_list)
    ]
    # The hook's BODY must call the drain; its name alone contains the same text.
    bodies = [" ".join(ast.unparse(stmt) for stmt in fn.body) for fn in hooks]
    assert any("await _drain_llm_provisioning()" in body for body in bodies)
