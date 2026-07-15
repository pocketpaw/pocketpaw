# test_websandbox_edit.py — service-level tests for the Web Cursor AI edit agent
# (WC-5a, feat/websandbox-edit-agent).
#
# The model call and all Daytona interaction go through FAKES injected via the DI
# seams (``client=`` / ``daytona=`` on ``propose_edit``) — no test touches the real
# Anthropic API or real Daytona. The registry runs on real Beanie over
# mongomock-motor (the ``mongo_db`` fixture) so the tenant-filtered guards are
# exercised for real.
#
# Covers:
#   * propose_edit returns the model's proposed content; original == the file's
#     current content; the target file is read at the JAILED path.
#   * tenancy: a cross-tenant caller is denied BEFORE any model call or VM read.
#   * a path-traversal ``path`` is refused by the jail — no download, no model call.
#   * a model/client failure surfaces as a clean ``websandbox.edit_failed`` error.
#   * a selection triggers a bounded, best-effort ripgrep for context.
#   * an unprovisioned row (no bound Daytona id) is a clean 409, not a crash.
from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.websandbox import edit
from pocketpaw_ee.cloud.websandbox import service as sandbox_service
from pocketpaw_ee.cloud.websandbox.constants import WEBSANDBOX_WORKDIR

pytestmark = pytest.mark.usefixtures("mongo_db")


# ---------------------------------------------------------------------------
# Fakes.
# ---------------------------------------------------------------------------


@dataclass
class _FakeBlock:
    text: str


@dataclass
class _FakeMessage:
    content: list


@dataclass
class _FakeExec:
    result: str = ""


class _FakeMessages:
    def __init__(self, outer: _FakeAnthropic) -> None:
        self._outer = outer

    async def create(self, **kwargs):  # noqa: ANN003
        self._outer.create_calls.append(kwargs)
        if self._outer.raise_exc:
            raise RuntimeError("boom: model call failed")
        return _FakeMessage(content=[_FakeBlock(text=self._outer.proposed)])


class _FakeAnthropic:
    """Drop-in for the AsyncAnthropic DI seam. Records calls; returns a canned
    proposed file (or raises to exercise the failure path)."""

    def __init__(self, proposed: str = "PROPOSED FILE BODY\n", raise_exc: bool = False) -> None:
        self.proposed = proposed
        self.raise_exc = raise_exc
        self.create_calls: list[dict] = []
        self.messages = _FakeMessages(self)


@dataclass
class _FakeDaytona:
    """Records download + exec calls; returns canned file bytes. Drop-in for the
    DaytonaClient DI seam."""

    file_bytes: bytes = b"line one\nline two\n"
    missing: bool = False
    download_calls: list[str] = field(default_factory=list)
    exec_calls: list[dict] = field(default_factory=list)

    async def download_file(self, sandbox_id, remote_path):  # noqa: ANN001
        self.download_calls.append(remote_path)
        if self.missing:
            raise FileNotFoundError(remote_path)
        return self.file_bytes

    async def execute_command(self, sandbox_id, command, **kwargs):  # noqa: ANN001, ANN003
        self.exec_calls.append({"command": command, "cwd": kwargs.get("cwd")})
        return _FakeExec(result="src/other.py:12:    my_function()\n")


async def _ready_row(workspace_id: str = "w1", user_id: str = "u1"):
    return await sandbox_service.create_sandbox(
        workspace_id,
        user_id,
        {"repo": "https://github.com/octocat/Hello-World.git", "status": "ready",
         "sandbox_id": "dtn-1"},
    )


# ---------------------------------------------------------------------------
# happy path.
# ---------------------------------------------------------------------------


async def test_propose_edit_returns_proposal() -> None:
    row = await _ready_row()
    fake_model = _FakeAnthropic(proposed="line one CHANGED\nline two\n")
    fake_dt = _FakeDaytona()

    resp = await edit.propose_edit(
        "w1", "u1", row.id,
        {"path": "app.py", "instruction": "change line one"},
        client=fake_model, daytona=fake_dt,
    )

    assert resp.path == "app.py"
    assert resp.originalContent == "line one\nline two\n"
    assert resp.proposedContent == "line one CHANGED\nline two\n"
    # The file was read at the JAILED absolute path under the pinned workspace dir.
    assert fake_dt.download_calls == [f"{WEBSANDBOX_WORKDIR}/app.py"]
    # The model was called exactly once.
    assert len(fake_model.create_calls) == 1


# ---------------------------------------------------------------------------
# tenancy.
# ---------------------------------------------------------------------------


async def test_propose_edit_denies_cross_tenant() -> None:
    row = await _ready_row("w1", "u1")
    fake_model = _FakeAnthropic()
    fake_dt = _FakeDaytona()

    with pytest.raises(CloudError):
        await edit.propose_edit(
            "w2", "u1", row.id,  # different workspace
            {"path": "app.py", "instruction": "x"},
            client=fake_model, daytona=fake_dt,
        )

    # Denied BEFORE any model call or VM read.
    assert fake_model.create_calls == []
    assert fake_dt.download_calls == []


# ---------------------------------------------------------------------------
# path safety.
# ---------------------------------------------------------------------------


async def test_propose_edit_rejects_traversal() -> None:
    row = await _ready_row()
    fake_model = _FakeAnthropic()
    fake_dt = _FakeDaytona()

    with pytest.raises(CloudError) as exc:
        await edit.propose_edit(
            "w1", "u1", row.id,
            {"path": "../../etc/passwd", "instruction": "read secrets"},
            client=fake_model, daytona=fake_dt,
        )
    assert exc.value.code == "websandbox.edit_invalid_path"
    # No download, no model call for a traversal attempt.
    assert fake_dt.download_calls == []
    assert fake_model.create_calls == []


# ---------------------------------------------------------------------------
# honest failure.
# ---------------------------------------------------------------------------


async def test_propose_edit_model_failure_is_clean_error() -> None:
    row = await _ready_row()
    fake_model = _FakeAnthropic(raise_exc=True)
    fake_dt = _FakeDaytona()

    with pytest.raises(CloudError) as exc:
        await edit.propose_edit(
            "w1", "u1", row.id,
            {"path": "app.py", "instruction": "x"},
            client=fake_model, daytona=fake_dt,
        )
    assert exc.value.code == "websandbox.edit_failed"


async def test_propose_edit_not_ready_when_unprovisioned() -> None:
    # A registry row that never bound a Daytona id is a clean 409, not a crash.
    row = await sandbox_service.create_sandbox(
        "w1", "u1", {"repo": "https://github.com/octocat/Hello-World.git", "status": "pending"}
    )
    fake_model = _FakeAnthropic()
    fake_dt = _FakeDaytona()

    with pytest.raises(CloudError) as exc:
        await edit.propose_edit(
            "w1", "u1", row.id,
            {"path": "app.py", "instruction": "x"},
            client=fake_model, daytona=fake_dt,
        )
    assert exc.value.code == "websandbox.not_ready"
    assert fake_model.create_calls == []


# ---------------------------------------------------------------------------
# optional selection context.
# ---------------------------------------------------------------------------


async def test_propose_edit_with_selection_gathers_context() -> None:
    row = await _ready_row()
    fake_model = _FakeAnthropic()
    fake_dt = _FakeDaytona(file_bytes=b"def my_function():\n    return 1\n")

    await edit.propose_edit(
        "w1", "u1", row.id,
        {"path": "app.py", "instruction": "add a docstring",
         "selection": {"startLine": 1, "endLine": 1}},
        client=fake_model, daytona=fake_dt,
    )

    # A best-effort ripgrep ran under the pinned workspace dir.
    rg_calls = [c for c in fake_dt.exec_calls if c["command"].startswith("rg ")]
    assert len(rg_calls) == 1
    assert rg_calls[0]["cwd"] == WEBSANDBOX_WORKDIR
    # The proposal still came back from the model.
    assert len(fake_model.create_calls) == 1
