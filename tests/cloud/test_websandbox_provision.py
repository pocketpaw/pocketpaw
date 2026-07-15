# test_websandbox_provision.py — service-level tests for the Web Cursor
# cold-provision + file-tree + idle-reaper slice (WC-2).
# Created 2026-07-15 (feat/websandbox-vm-provision).
#
# All Daytona interaction goes through a FAKE DaytonaClient injected via the DI
# seam (``client=`` on every provisioning fn) — no test touches real Daytona.
# The registry itself runs on real Beanie over mongomock-motor (the ``mongo_db``
# fixture) so the tenant-filtered query paths and the reaper's global-read are
# exercised for real.
#
# Covers:
#   * open provisions (create called with the aggressive lifecycle: stop 5 /
#     archive 5 / delete-on-stop), clones (the
#     repo URL reaches git_clone), and the row ends ``ready`` with the Daytona
#     sandbox_id bound.
#   * open mid-flight failure marks the row ``stopped`` (never stuck ``opening``)
#     and tears down the half-created VM.
#   * open with no Daytona configured → clean CloudError, not a crash.
#   * tree returns the fake listing AND enforces ``authorize_sandbox`` (a
#     cross-tenant caller is Forbidden).
#   * the reaper reclaims an idle VM (stop+delete, row -> reaped) and leaves a
#     fresh row untouched.
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.daytona.client import SandboxInfo
from pocketpaw_ee.cloud.websandbox import provision
from pocketpaw_ee.cloud.websandbox import service as sandbox_service

pytestmark = pytest.mark.usefixtures("mongo_db")


# ---------------------------------------------------------------------------
# Fakes.
# ---------------------------------------------------------------------------


@dataclass
class _FakeFileInfo:
    """Minimal stand-in for daytona's FileInfo (name / is_dir / size)."""

    name: str
    is_dir: bool = False
    size: int = 0


@dataclass
class _FakeDaytonaClient:
    """Records calls and returns canned data. Drop-in for DaytonaClient in the
    DI seam. ``clone_fails`` flips ``git_clone`` into a raise to exercise the
    provisioning-failure path."""

    project_dir: str = "/home/daytona"  # matches WEBSANDBOX_WORKDIR (clone target + jail root)
    files: list[_FakeFileInfo] = field(
        default_factory=lambda: [
            _FakeFileInfo("README.md", is_dir=False, size=42),
            _FakeFileInfo("src", is_dir=True, size=0),
        ]
    )
    clone_fails: bool = False

    create_calls: list[dict] = field(default_factory=list)
    wait_calls: list[dict] = field(default_factory=list)
    clone_calls: list[dict] = field(default_factory=list)
    exec_calls: list[str] = field(default_factory=list)
    # Full ``execute_command`` invocations (command + cwd + timeout) so tests can
    # assert the auto-branch ``git checkout -b`` ran with cwd=WEBSANDBOX_WORKDIR.
    exec_invocations: list[dict] = field(default_factory=list)
    stop_calls: list[str] = field(default_factory=list)
    delete_calls: list[str] = field(default_factory=list)
    _counter: int = 0

    async def create_sandbox(self, name, auto_stop_interval=3600, **kwargs):  # noqa: ANN001
        self._counter += 1
        sid = f"dtn-{self._counter}"
        self.create_calls.append(
            {
                "name": name,
                "auto_stop_interval": auto_stop_interval,
                "auto_archive_interval": kwargs.get("auto_archive_interval"),
                "auto_delete_interval": kwargs.get("auto_delete_interval"),
                "id": sid,
            }
        )
        return SandboxInfo(id=sid, name=name, state="creating")

    async def wait_for_sandbox(self, sandbox_id, target_state="started", timeout=120.0):  # noqa: ANN001
        self.wait_calls.append(
            {"id": sandbox_id, "target_state": target_state, "timeout": timeout}
        )
        return SandboxInfo(id=sandbox_id, name="", state="started")

    async def get_project_dir(self, sandbox_id):  # noqa: ANN001
        return self.project_dir

    async def execute_command(self, sandbox_id, command, **kwargs):  # noqa: ANN001
        # open_sandbox runs ``mkdir -p <workdir>`` before cloning, then
        # ``git checkout -b paw/edit-<hex>`` after (the WC-5a auto-branch).
        self.exec_calls.append(command)
        self.exec_invocations.append(
            {"command": command, "cwd": kwargs.get("cwd"), "timeout": kwargs.get("timeout")}
        )
        return None

    async def git_clone(self, sandbox_id, repo_url, path, branch=None, commit_id=None):  # noqa: ANN001
        if self.clone_fails:
            raise RuntimeError("boom: clone failed")
        self.clone_calls.append(
            {"id": sandbox_id, "repo_url": repo_url, "path": path, "branch": branch}
        )

    async def list_files(self, sandbox_id, path="."):  # noqa: ANN001
        return list(self.files)

    async def stop_sandbox(self, sandbox_id):  # noqa: ANN001
        self.stop_calls.append(sandbox_id)

    async def delete_sandbox(self, sandbox_id):  # noqa: ANN001
        self.delete_calls.append(sandbox_id)


# ---------------------------------------------------------------------------
# open flow.
# ---------------------------------------------------------------------------


async def test_open_provisions_clones_and_marks_ready() -> None:
    fake = _FakeDaytonaClient()
    view = await provision.open_sandbox(
        "w1",
        "u1",
        {"repo": "https://github.com/octocat/Hello-World.git"},
        client=fake,
    )

    # Cold-provision fired with the aggressive Daytona lifecycle (all MINUTES):
    # stop after 5 idle, archive 5 after stop, delete immediately on stop (0).
    assert len(fake.create_calls) == 1
    assert fake.create_calls[0]["auto_stop_interval"] == 5
    assert fake.create_calls[0]["auto_archive_interval"] == 5
    assert fake.create_calls[0]["auto_delete_interval"] == 0

    # It waited for boot before cloning, then cloned the exact repo URL.
    assert len(fake.wait_calls) == 1
    assert len(fake.clone_calls) == 1
    assert fake.clone_calls[0]["repo_url"] == "https://github.com/octocat/Hello-World.git"
    assert fake.clone_calls[0]["path"] == fake.project_dir

    # The row ended ``ready`` with the Daytona id bound.
    assert view.status == "ready"
    assert view.sandbox_id == "dtn-1"

    # And the persisted row agrees.
    fetched = await sandbox_service.get_sandbox("w1", "u1", view.id)
    assert fetched.status == "ready"
    assert fetched.sandbox_id == "dtn-1"


async def test_open_creates_and_binds_edit_branch() -> None:
    # WC-5a: opening checks out a fresh ``paw/edit-<hex>`` branch IN the VM (via
    # execute_command with cwd=WEBSANDBOX_WORKDIR) and records it on the row.
    from pocketpaw_ee.cloud.websandbox.constants import WEBSANDBOX_WORKDIR

    fake = _FakeDaytonaClient()
    view = await provision.open_sandbox(
        "w1", "u1", {"repo": "https://github.com/octocat/Hello-World.git"}, client=fake
    )

    # The branch is minted, checked out in the VM, and stored on the ready row.
    assert view.branch is not None
    assert view.branch.startswith("paw/edit-")
    assert view.status == "ready"

    checkouts = [
        inv for inv in fake.exec_invocations if inv["command"].startswith("git checkout -b ")
    ]
    assert len(checkouts) == 1
    assert checkouts[0]["command"] == f"git checkout -b {view.branch}"
    assert checkouts[0]["cwd"] == WEBSANDBOX_WORKDIR

    # The persisted row agrees.
    fetched = await sandbox_service.get_sandbox("w1", "u1", view.id)
    assert fetched.branch == view.branch


async def test_open_failure_marks_stopped_and_tears_down_vm() -> None:
    fake = _FakeDaytonaClient(clone_fails=True)
    with pytest.raises(CloudError) as exc:
        await provision.open_sandbox(
            "w1", "u1", {"repo": "https://github.com/octocat/Hello-World.git"}, client=fake
        )
    assert exc.value.code == "websandbox.provision_failed"

    # The half-created VM was torn down.
    assert fake.delete_calls == ["dtn-1"]

    # The row is ``stopped`` — never left stuck in ``opening``.
    rows = await sandbox_service.list_sandboxes("w1", "u1")
    assert len(rows) == 1
    assert rows[0].status == "stopped"


async def test_open_rejects_non_http_repo() -> None:
    fake = _FakeDaytonaClient()
    with pytest.raises(CloudError) as exc:
        await provision.open_sandbox(
            "w1", "u1", {"repo": "git@github.com:acme/api.git"}, client=fake
        )
    assert exc.value.code == "websandbox.invalid_repo"
    # Nothing was provisioned.
    assert fake.create_calls == []


async def test_open_without_daytona_raises_clean_error(monkeypatch) -> None:
    # get_daytona_client() -> None when Daytona keys are unset; must be a clean
    # CloudError, not an AttributeError crash on a None client.
    monkeypatch.setattr(provision, "get_daytona_client", lambda: None)
    with pytest.raises(CloudError) as exc:
        await provision.open_sandbox(
            "w1", "u1", {"repo": "https://github.com/octocat/Hello-World.git"}, client=None
        )
    assert exc.value.code == "websandbox.daytona_unavailable"
    assert exc.value.status_code == 503


# ---------------------------------------------------------------------------
# tree flow.
# ---------------------------------------------------------------------------


async def test_tree_returns_listing_for_owner() -> None:
    fake = _FakeDaytonaClient()
    view = await provision.open_sandbox(
        "w1", "u1", {"repo": "https://github.com/octocat/Hello-World.git"}, client=fake
    )
    tree = await provision.get_tree("w1", "u1", view.id, client=fake)

    assert tree.sandboxId == "dtn-1"
    assert tree.path == fake.project_dir
    names = {(e.name, e.isDir) for e in tree.entries}
    assert names == {("README.md", False), ("src", True)}


async def test_tree_denies_cross_tenant_caller() -> None:
    fake = _FakeDaytonaClient()
    view = await provision.open_sandbox(
        "w1", "u1", {"repo": "https://github.com/octocat/Hello-World.git"}, client=fake
    )
    # A caller in a DIFFERENT workspace must not resolve the row at all
    # (get_sandbox is NotFound before authorize even runs) — never the listing.
    with pytest.raises(CloudError):
        await provision.get_tree("w2", "u1", view.id, client=fake)


async def test_tree_not_ready_when_unprovisioned() -> None:
    # A registry row that never bound a Daytona id is a clean 409, not a crash.
    row = await sandbox_service.create_sandbox(
        "w1", "u1", {"repo": "https://github.com/octocat/Hello-World.git", "status": "pending"}
    )
    fake = _FakeDaytonaClient()
    with pytest.raises(CloudError) as exc:
        await provision.get_tree("w1", "u1", row.id, client=fake)
    assert exc.value.code == "websandbox.not_ready"


# ---------------------------------------------------------------------------
# idle-TTL reaper.
# ---------------------------------------------------------------------------


async def _age_row(row_id: str, *, seconds: int) -> None:
    """Push a row's ``updated_at`` into the past (test-only doc touch)."""
    from beanie import PydanticObjectId
    from pocketpaw_ee.cloud.models.web_sandbox import WebSandbox

    doc = await WebSandbox.find_one({"_id": PydanticObjectId(row_id)})
    assert doc is not None
    doc.updated_at = datetime.now(UTC) - timedelta(seconds=seconds)
    await doc.save()


async def test_reaper_reclaims_idle_and_spares_fresh(monkeypatch) -> None:
    monkeypatch.setenv("POCKETPAW_WEBSANDBOX_IDLE_TTL_SECONDS", "1800")

    idle = await sandbox_service.create_sandbox(
        "w1", "u1", {"repo": "r-idle", "status": "ready", "sandbox_id": "dtn-idle"}
    )
    fresh = await sandbox_service.create_sandbox(
        "w1", "u1", {"repo": "r-fresh", "status": "ready", "sandbox_id": "dtn-fresh"}
    )
    # Age the idle row well past the 1800s TTL; leave the fresh one at "now".
    await _age_row(idle.id, seconds=7200)

    fake = _FakeDaytonaClient()
    reaped = await provision.reap_idle_sandboxes(client=fake)

    assert reaped == 1
    # The idle VM was stopped then deleted; the fresh one was left alone.
    assert fake.delete_calls == ["dtn-idle"]
    assert "dtn-idle" in fake.stop_calls
    assert "dtn-fresh" not in fake.delete_calls

    assert (await sandbox_service.get_sandbox("w1", "u1", idle.id)).status == "reaped"
    assert (await sandbox_service.get_sandbox("w1", "u1", fresh.id)).status == "ready"


async def test_reaper_marks_unprovisioned_idle_row_reaped(monkeypatch) -> None:
    # An ``opening`` row that never bound a Daytona id is still reclaimable — no
    # VM to delete, just flip it to ``reaped`` so it stops looking in-flight.
    monkeypatch.setenv("POCKETPAW_WEBSANDBOX_IDLE_TTL_SECONDS", "1800")
    stuck = await sandbox_service.create_sandbox(
        "w1", "u1", {"repo": "r-stuck", "status": "opening"}
    )
    await _age_row(stuck.id, seconds=7200)

    fake = _FakeDaytonaClient()
    reaped = await provision.reap_idle_sandboxes(client=fake)

    assert reaped == 1
    assert fake.delete_calls == []  # nothing to delete
    assert (await sandbox_service.get_sandbox("w1", "u1", stuck.id)).status == "reaped"
