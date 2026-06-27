# tests/cloud/runs/test_run_core_jail_quota.py
# Created 2026-06-26 (ART-3) — locks the run-start jail-quota gate in
# run_core._reject_if_over_jail_quota:
#   * over-quota -> the run is rejected CLEANLY (terminal `failed` doc + a
#     terminal `error` stream frame), the call returns True, and it never raises
#     (the worker survives — no OOM/abort)
#   * under-quota -> returns False and touches nothing (the run proceeds)
"""execute_run's ART-3 jail-quota gate."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.asyncio


class _FakeTransport:
    """Records the terminal frames the gate writes, without a real Redis."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []
        self.ttls: list[tuple[str, int]] = []

    async def append_event(self, run_id: str, event: str, data: dict) -> None:
        self.events.append((run_id, event, data))

    async def set_ttl(self, run_id: str, ttl_seconds: int) -> None:
        self.ttls.append((run_id, ttl_seconds))


def _spec(run_id: str, workspace_id: str):
    from pocketpaw_ee.cloud.chat.runs.domain import RunSpec

    return RunSpec(
        run_id=run_id,
        workspace_id=workspace_id,
        context_type="session",
        scope_id="s1",
        session_key="session:s1",
        group=None,
        user_id="u1",
        agent_id="a1",
        client_message_id=f"c-{run_id}",
        user_message_id="m1",
        content="hi",
        history=[],
        intent=None,
    )


async def test_over_quota_rejects_cleanly(mongo_db, tmp_path, monkeypatch):  # noqa: ARG001
    monkeypatch.setenv("POCKETPAW_WORKSPACE_JAIL_ROOT", str(tmp_path / "jail"))
    monkeypatch.setenv("POCKETPAW_AGENT_JAIL_QUOTA_MB", "1")
    # Build w1's jail at 2 MB — over the 1 MB cap.
    jail = Path(tmp_path) / "jail" / "w1" / "agent" / "s1"
    jail.mkdir(parents=True)
    (jail / "big.bin").write_bytes(b"x" * (2 * 1024 * 1024))

    from pocketpaw_ee.cloud.chat.runs import run_core
    from pocketpaw_ee.cloud.chat.runs import service as run_service

    spec = _spec("r1", "w1")
    await run_service.create_run(spec)
    transport = _FakeTransport()
    ctx = SimpleNamespace(workspace_id="w1")

    # Must NOT raise — a clean rejection, not a crash.
    rejected = await run_core._reject_if_over_jail_quota(spec, ctx, transport)

    assert rejected is True
    doc = await run_service.get_run("r1")
    assert doc.status == "failed"
    assert "full" in (doc.error or "").lower()
    # A terminal error frame went to the stream, with a TTL bound.
    assert transport.events and transport.events[-1][1] == "error"
    assert transport.events[-1][2]["code"] == "agent.jail_over_quota"
    assert transport.ttls and transport.ttls[-1][0] == "r1"


async def test_under_quota_proceeds_untouched(mongo_db, tmp_path, monkeypatch):  # noqa: ARG001
    monkeypatch.setenv("POCKETPAW_WORKSPACE_JAIL_ROOT", str(tmp_path / "jail"))
    monkeypatch.setenv("POCKETPAW_AGENT_JAIL_QUOTA_MB", "10")
    jail = Path(tmp_path) / "jail" / "w1" / "agent" / "s1"
    jail.mkdir(parents=True)
    (jail / "small.bin").write_bytes(b"x" * 1024)

    from pocketpaw_ee.cloud.chat.runs import run_core
    from pocketpaw_ee.cloud.chat.runs import service as run_service

    spec = _spec("r2", "w1")
    await run_service.create_run(spec)
    transport = _FakeTransport()
    ctx = SimpleNamespace(workspace_id="w1")

    rejected = await run_core._reject_if_over_jail_quota(spec, ctx, transport)

    assert rejected is False
    doc = await run_service.get_run("r2")
    assert doc.status == "queued"  # untouched
    assert transport.events == [] and transport.ttls == []
