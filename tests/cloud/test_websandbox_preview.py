# test_websandbox_preview.py — service-level tests for the Web Cursor live
# dev-server preview endpoint (WC-8/P3b, feat/code-mode).
#
# All Daytona interaction goes through a FAKE injected via the DI seam
# (``client=`` on ``preview.get_preview``) — no test touches real Daytona. The
# registry runs on real Beanie over mongomock-motor (the ``mongo_db`` fixture) so
# the tenant-filtered guards are exercised for real.
#
# Covers:
#   * happy path returns {url, port}; the preview URL is resolved on the row's
#     bound Daytona id + the requested port, AFTER a fail-closed authorize.
#   * a not-owned row is a NotFound (cross-tenant is indistinguishable from
#     missing) — resolved before any VM op.
#   * a not-ready row (no bound Daytona id) is a clean 409, not a crash.
#   * an out-of-range port and the reserved terminal port (22222) are rejected
#     with a clean ValidationError before any VM op.
from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.websandbox import preview
from pocketpaw_ee.cloud.websandbox import service as sandbox_service

pytestmark = pytest.mark.usefixtures("mongo_db")


# ---------------------------------------------------------------------------
# Fakes.
# ---------------------------------------------------------------------------


@dataclass
class _FakeDaytona:
    """Records the (sandbox_id, port) it was asked to preview and returns a canned
    iframe-embeddable URL. Drop-in for the DaytonaClient DI seam."""

    url: str = "https://3000-dtn-1.h.daytona.app/?token=abc123"
    calls: list[tuple[str, int]] = field(default_factory=list)

    async def get_port_preview_url(self, sandbox_id, port):  # noqa: ANN001
        self.calls.append((sandbox_id, port))
        return self.url


async def _ready_row(workspace_id: str = "w1", user_id: str = "u1", sandbox_id: str = "dtn-1"):
    return await sandbox_service.create_sandbox(
        workspace_id,
        user_id,
        {
            "repo": "https://github.com/octocat/Hello-World.git",
            "status": "ready",
            "sandbox_id": sandbox_id,
        },
    )


# ---------------------------------------------------------------------------
# happy path.
# ---------------------------------------------------------------------------


async def test_get_preview_returns_url_and_port() -> None:
    row = await _ready_row()
    fake_dt = _FakeDaytona(url="https://3000-dtn-1.h.daytona.app/?token=xyz")

    resp = await preview.get_preview("w1", "u1", row.id, 3000, client=fake_dt)

    assert resp.url == "https://3000-dtn-1.h.daytona.app/?token=xyz"
    assert resp.port == 3000
    # The preview was resolved on the row's bound Daytona id + the requested port.
    assert fake_dt.calls == [("dtn-1", 3000)]


# ---------------------------------------------------------------------------
# tenancy.
# ---------------------------------------------------------------------------


async def test_get_preview_denies_not_owned_row() -> None:
    row = await _ready_row("w1", "u1")
    fake_dt = _FakeDaytona()

    # A caller in a different workspace can't resolve the row at all — NotFound,
    # and no VM preview op runs.
    with pytest.raises(CloudError) as exc:
        await preview.get_preview("w2", "u1", row.id, 3000, client=fake_dt)
    assert exc.value.status_code == 404
    assert fake_dt.calls == []


# ---------------------------------------------------------------------------
# not-ready.
# ---------------------------------------------------------------------------


async def test_get_preview_not_ready_when_unprovisioned() -> None:
    # A registry row that never bound a Daytona id is a clean 409, not a crash.
    row = await sandbox_service.create_sandbox(
        "w1", "u1", {"repo": "https://github.com/octocat/Hello-World.git", "status": "pending"}
    )
    fake_dt = _FakeDaytona()

    with pytest.raises(CloudError) as exc:
        await preview.get_preview("w1", "u1", row.id, 3000, client=fake_dt)
    assert exc.value.code == "websandbox.not_ready"
    assert fake_dt.calls == []


# ---------------------------------------------------------------------------
# port validation — rejected before any VM op.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_port", [0, -1, 65536, 99999])
async def test_get_preview_rejects_out_of_range_port(bad_port) -> None:  # noqa: ANN001
    row = await _ready_row()
    fake_dt = _FakeDaytona()

    with pytest.raises(CloudError) as exc:
        await preview.get_preview("w1", "u1", row.id, bad_port, client=fake_dt)
    assert exc.value.status_code == 422
    assert fake_dt.calls == []


async def test_get_preview_rejects_reserved_terminal_port() -> None:
    row = await _ready_row()
    fake_dt = _FakeDaytona()

    # 22222 is Daytona's built-in web terminal — never previewable.
    with pytest.raises(CloudError) as exc:
        await preview.get_preview("w1", "u1", row.id, 22222, client=fake_dt)
    assert exc.value.status_code == 422
    assert exc.value.code == "websandbox.reserved_port"
    assert fake_dt.calls == []
