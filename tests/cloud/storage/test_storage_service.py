# tests/cloud/storage/test_storage_service.py — the workspace S3 storage usage
# resolver + cap gate (feat/billing-storage-caps).
#
# ``storage.service`` owns two jobs:
#   * MEASURE — ``workspace_storage_usage`` sums the workspace's live
#     (non-deleted) ``FileUpload`` blob sizes in the DB (a server-side $sum, so
#     deleting a file frees its bytes). This is the S3 usage backing the
#     Files → Knowledge Base store.
#   * GATE — ``storage_cap_exceeded`` / ``assert_storage_available`` decide
#     whether ``incoming_bytes`` of NEW uploads would push the workspace over its
#     plan's ``max_storage_bytes`` (Free = 5 GB, Go = 15 GB, Pro = 50 GB, Pro
#     Max = 100 GB, Enterprise = None). GATED on ``billing_enforced``: OSS /
#     self-host (billing off) always pass through with no cap. The upload seam
#     raises ``StorageLimitError`` (402, ``billing.storage_limit``) when over.
#   * READ — ``resolve_storage_usage`` pairs used bytes with the plan cap for the
#     Settings storage page (used / max / remaining / percent). The READ is NOT
#     gated on ``billing_enforced`` (it is informational — a Go workspace shows
#     "15 GB" whether or not enforcement is on); only the GATE is.
#
# DB-backed (mongo_db): real Workspace + FileUpload docs drive the sums and the
# entitlement resolution. billing_enforced is patched per-test for the GATE only.
#
# Created 2026-08-08 (feat/billing-storage-caps).

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from pocketpaw_ee.cloud._core.errors import StorageLimitError
from pocketpaw_ee.cloud.storage import service as storage_service
from pocketpaw_ee.cloud.uploads.models import FileUpload


async def _make_workspace(plan: str, slug: str | None = None) -> str:
    from pocketpaw_ee.cloud.models.workspace import Workspace

    slug = slug or f"acme-{uuid.uuid4().hex[:8]}"
    ws = Workspace(name="Acme", slug=slug, owner="u-owner", plan=plan)
    await ws.insert()
    return str(ws.id)


async def _seed_file(workspace_id: str, size: int, *, deleted: bool = False) -> None:
    """Insert one FileUpload row of ``size`` bytes (optionally soft-deleted)."""
    from datetime import UTC, datetime

    await FileUpload(
        file_id=uuid.uuid4().hex,
        storage_key=f"u/{uuid.uuid4().hex}",
        filename="doc.pdf",
        mime="application/pdf",
        size=size,
        workspace=workspace_id,
        owner="u-owner",
        deleted_at=datetime.now(UTC) if deleted else None,
    ).insert()


@pytest.fixture
def billing_on(monkeypatch: pytest.MonkeyPatch):
    """Turn billing_enforced on for the storage gate + read.

    The service lazily imports ``get_settings`` from ``pocketpaw.config`` inside
    each function, so the patch targets the CONFIG module (mirroring
    ``tests/cloud/pockets/test_pocket_cap.py``).
    """
    import pocketpaw.config as ppconfig

    monkeypatch.setattr(
        ppconfig,
        "get_settings",
        lambda: SimpleNamespace(billing_enforced=True, dodo_plan_products=None),
    )
    yield


# ---------------------------------------------------------------------------
# workspace_storage_usage — the measure
# ---------------------------------------------------------------------------


async def test_usage_sums_live_files_per_workspace(mongo_db) -> None:
    ws_a = await _make_workspace("free")
    ws_b = await _make_workspace("free")
    await _seed_file(ws_a, 1_000)
    await _seed_file(ws_a, 2_000)
    await _seed_file(ws_b, 999_999)  # other tenant must not leak in

    assert await storage_service.workspace_storage_usage(ws_a) == 3_000
    assert await storage_service.workspace_storage_usage(ws_b) == 999_999


async def test_usage_excludes_soft_deleted_files(mongo_db) -> None:
    ws = await _make_workspace("free")
    await _seed_file(ws, 5_000)
    await _seed_file(ws, 7_000, deleted=True)  # deleted → frees its bytes

    assert await storage_service.workspace_storage_usage(ws) == 5_000


async def test_usage_is_zero_for_empty_workspace(mongo_db) -> None:
    ws = await _make_workspace("free")
    assert await storage_service.workspace_storage_usage(ws) == 0


# ---------------------------------------------------------------------------
# storage_cap_exceeded — the gate (billing on)
# ---------------------------------------------------------------------------


async def test_cap_exceeded_when_incoming_pushes_over(mongo_db, billing_on) -> None:
    """Free (5 GB) with 5 GB stored and a 1-byte new file IS over."""
    ws = await _make_workspace("free")
    await _seed_file(ws, 5_000_000_000)

    exceeded, used, limit = await storage_service.storage_cap_exceeded(ws, incoming_bytes=1)
    assert exceeded is True
    assert used == 5_000_000_000
    assert limit == 5_000_000_000


async def test_cap_allows_exactly_at_the_limit(mongo_db, billing_on) -> None:
    """Filling the cap EXACTLY is allowed — exceeded is strict >, never >=."""
    ws = await _make_workspace("go")
    await _seed_file(ws, 15_000_000_000)

    exceeded, _used, _limit = await storage_service.storage_cap_exceeded(ws, incoming_bytes=0)
    assert exceeded is False
    # …and adding even 1 byte crosses it.
    exceeded, _used, _limit = await storage_service.storage_cap_exceeded(ws, incoming_bytes=1)
    assert exceeded is True


async def test_cap_uncapped_enterprise_never_trips(mongo_db, billing_on) -> None:
    ws = await _make_workspace("enterprise")
    await _seed_file(ws, 10**12)  # a terabyte of "stored" bytes
    exceeded, used, limit = await storage_service.storage_cap_exceeded(ws, incoming_bytes=10**12)
    assert exceeded is False
    assert limit is None


async def test_gate_is_noop_when_billing_off(mongo_db, monkeypatch: pytest.MonkeyPatch) -> None:
    """billing_enforced off (OSS / self-host) → no cap, no extra DB read."""
    import pocketpaw.config as ppconfig

    monkeypatch.setattr(
        ppconfig,
        "get_settings",
        lambda: SimpleNamespace(billing_enforced=False, dodo_plan_products=None),
    )
    ws = await _make_workspace("free")
    await _seed_file(ws, 5_000_000_000)

    exceeded, used, limit = await storage_service.storage_cap_exceeded(ws, incoming_bytes=10**9)
    assert exceeded is False
    assert used == 0  # no usage was even summed — short-circuit
    assert limit is None


async def test_assert_storage_available_raises_only_when_exceeded(mongo_db, billing_on) -> None:
    ws = await _make_workspace("free")
    await _seed_file(ws, 5_000_000_000)

    with pytest.raises(StorageLimitError) as exc:
        await storage_service.assert_storage_available(ws, incoming_bytes=1)
    assert exc.value.status_code == 402
    assert exc.value.code == "billing.storage_limit"
    assert "5 GB" in exc.value.message

    # Within budget → no raise.
    await storage_service.assert_storage_available(ws, incoming_bytes=0)


# ---------------------------------------------------------------------------
# resolve_storage_usage — the read surface
# ---------------------------------------------------------------------------


async def test_resolve_reports_used_vs_cap(mongo_db, billing_on) -> None:
    ws = await _make_workspace("go")
    await _seed_file(ws, 1_500_000_000)  # 1.5 GB of Go's 15 GB

    usage = await storage_service.resolve_storage_usage(ws)
    assert usage.workspace_id == ws
    assert usage.used_bytes == 1_500_000_000
    assert usage.max_bytes == 15_000_000_000
    assert usage.remaining_bytes == 13_500_000_000
    assert usage.percent_used == 10.0


async def test_resolve_uncapped_enterprise_is_none(mongo_db, billing_on) -> None:
    ws = await _make_workspace("enterprise")
    await _seed_file(ws, 10**12)

    usage = await storage_service.resolve_storage_usage(ws)
    assert usage.max_bytes is None
    assert usage.remaining_bytes is None
    assert usage.percent_used is None


async def test_resolve_reads_plan_cap_regardless_of_billing(
    mongo_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """billing off → the read STILL reports the plan cap (informational, not gated).

    A Go workspace shows "15 GB" whether or not enforcement is active — the
    enforcement gate (``storage_cap_exceeded``) is what's no-op'd by billing off,
    not the read. Regression for the live "shows Unlimited on Go" bug: the read
    used to short-circuit to a None cap when ``billing_enforced`` was off.
    """
    import pocketpaw.config as ppconfig

    monkeypatch.setattr(
        ppconfig,
        "get_settings",
        lambda: SimpleNamespace(billing_enforced=False, dodo_plan_products=None),
    )
    ws = await _make_workspace("go")
    await _seed_file(ws, 1_500_000_000)

    usage = await storage_service.resolve_storage_usage(ws)
    assert usage.used_bytes == 1_500_000_000
    assert usage.max_bytes == 15_000_000_000
    assert usage.remaining_bytes == 13_500_000_000
    assert usage.percent_used == 10.0


async def test_resolve_rejects_empty_workspace_id() -> None:
    from pocketpaw_ee.cloud._core.errors import ValidationError

    with pytest.raises(ValidationError):
        await storage_service.resolve_storage_usage("")
