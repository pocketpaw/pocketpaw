# tests/cloud/test_instinct_compensation_registry.py
# Created: 2026-06-18 (feat/instinct-gate-foundation, T4) — tests for the
# in-memory OptimisticCompensationRegistry that backs the OPTIMISTIC lane's
# rollback safety net (2026-06-18 gate-layered-learning design). No DB.
# Time is injected (now=...) so TTL expiry is deterministic. Audit + home
# dir are redirected to test doubles / tmp_path so the real ~/.pocketpaw
# and audit log are never touched. Pins case T-36: TTL expiry fires an
# ALERT audit event, persists the expired handle to the expired-
# compensations JSONL, and purges the registry entry.

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from pocketpaw_ee.cloud.pockets import instinct_compensation_registry as reg_mod
from pocketpaw_ee.cloud.pockets.action_executor import CompensateSpec
from pocketpaw_ee.cloud.pockets.instinct_compensation_registry import (
    OptimisticCompensationRegistry,
)

from pocketpaw.security.audit import AuditSeverity


class _FakeAuditLogger:
    """Captures logged events instead of writing the real audit JSONL."""

    def __init__(self) -> None:
        self.events: list = []

    def log(self, event) -> None:
        self.events.append(event)


@pytest.fixture
def fake_audit(monkeypatch):
    logger = _FakeAuditLogger()
    monkeypatch.setattr(reg_mod, "get_audit_logger", lambda: logger)
    return logger


@pytest.fixture(autouse=True)
def _tmp_home(monkeypatch, tmp_path):
    monkeypatch.setattr(reg_mod.Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _compensate() -> CompensateSpec:
    return CompensateSpec(method="POST", path="/refund", params={"id": "x"})


def _expired_path(home, workspace_id: str):
    return home / ".pocketpaw" / "instinct" / "expired_compensations" / f"{workspace_id}.jsonl"


def test_register_returns_id_and_is_retrievable() -> None:
    r = OptimisticCompensationRegistry(ttl_seconds=300)
    now = datetime.now(UTC)
    cid = r.register(
        workspace_id="w1",
        pocket_id="p1",
        action="charge",
        compensate=_compensate(),
        now=now,
    )
    assert isinstance(cid, str) and cid
    handle = r.get(cid)
    assert handle is not None
    assert handle.workspace_id == "w1"
    assert handle.action == "charge"


def test_rollback_removes_entry() -> None:
    r = OptimisticCompensationRegistry(ttl_seconds=300)
    now = datetime.now(UTC)
    cid = r.register(
        workspace_id="w1",
        pocket_id="p1",
        action="charge",
        compensate=_compensate(),
        now=now,
    )
    popped = r.pop(cid)
    assert popped is not None
    assert r.get(cid) is None


# T-36: TTL expiry fires ALERT audit event + writes expired JSONL + purges.
def test_t36_ttl_expiry_alerts_persists_and_purges(fake_audit, _tmp_home) -> None:
    r = OptimisticCompensationRegistry(ttl_seconds=300)
    t0 = datetime.now(UTC)
    cid = r.register(
        workspace_id="w1",
        pocket_id="p1",
        action="charge",
        compensate=_compensate(),
        now=t0,
    )

    # Before TTL: sweep finds nothing expired.
    expired_early = r.sweep_expired(now=t0 + timedelta(seconds=299))
    assert expired_early == []
    assert r.get(cid) is not None
    assert fake_audit.events == []

    # After TTL: sweep expires the entry.
    expired = r.sweep_expired(now=t0 + timedelta(seconds=301))
    assert len(expired) == 1
    assert expired[0].compensation_id == cid

    # registry entry purged
    assert r.get(cid) is None

    # ALERT audit event fired
    assert len(fake_audit.events) == 1
    evt = fake_audit.events[0]
    assert evt.severity == AuditSeverity.ALERT
    assert "w1" in (evt.context.get("workspace_id"), evt.target)

    # expired-compensation JSONL written
    path = _expired_path(_tmp_home, "w1")
    assert path.exists()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["compensation_id"] == cid
    assert row["pocket_id"] == "p1"
    assert row["action"] == "charge"


def test_expiry_jsonl_permissions(fake_audit, _tmp_home) -> None:
    import stat

    r = OptimisticCompensationRegistry(ttl_seconds=10)
    t0 = datetime.now(UTC)
    r.register(
        workspace_id="w1",
        pocket_id="p1",
        action="charge",
        compensate=_compensate(),
        now=t0,
    )
    r.sweep_expired(now=t0 + timedelta(seconds=11))

    path = _expired_path(_tmp_home, "w1")
    dir_mode = stat.S_IMODE(path.parent.stat().st_mode)
    file_mode = stat.S_IMODE(path.stat().st_mode)
    assert dir_mode == 0o700
    assert file_mode == 0o600


def test_sweep_purges_even_if_jsonl_write_fails(fake_audit, monkeypatch) -> None:
    # design MF-8: if the JSONL persist fails, log + still purge to avoid a
    # memory leak. Force the persist to raise and assert the entry is gone.
    r = OptimisticCompensationRegistry(ttl_seconds=10)
    t0 = datetime.now(UTC)
    cid = r.register(
        workspace_id="w1",
        pocket_id="p1",
        action="charge",
        compensate=_compensate(),
        now=t0,
    )

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(r, "_persist_expired", _boom)
    expired = r.sweep_expired(now=t0 + timedelta(seconds=11))
    assert len(expired) == 1
    assert r.get(cid) is None  # purged despite persist failure
