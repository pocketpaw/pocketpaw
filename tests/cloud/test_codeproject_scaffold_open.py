# test_codeproject_scaffold_open.py — B3: a SCAFFOLD project opens on the Daytona
# VM runtime.
#
# Created 2026-07-25 (B3, feat/code-scaffold-on-vm).
#
# What was broken: ``open_project`` provisioned every project through
# ``open_sandbox``, which runs ``_validate_repo_url`` and fail-closes on anything
# that isn't a clean http(s) URL. A scaffold project's ``repo`` is a starter
# TEMPLATE id ("react"), so the open was rejected before a VM existed — scaffold
# projects were in-tab only.
#
# The fix is a second door, not a weaker lock: ``open_bare_sandbox`` provisions an
# EMPTY VM (no clone, no remote, no branch) and ``scaffold_into_sandbox``
# materializes the starter into it. These tests hold that line explicitly — the
# clone validator's rules are asserted here, unchanged, so a future refactor that
# quietly loosens them fails a test instead of shipping.
#
# What is proved, on real Beanie over mongomock-motor (the ``mongo_db`` fixture)
# with a FAKE Daytona client, a FAKE ``bring_up``, and a FAKE blob store injected
# through the existing DI seams — no VM, no npm, no network:
#   1. a scaffold open provisions a VM and materializes the starter, and the git
#      clone path is NOT taken;
#   2. a repo open is byte-for-byte the old behaviour (regression);
#   3. the durable state restores ON TOP of the scaffold, in that order;
#   4. a reopen does not re-scaffold over the user's work — neither when the VM is
#      still live, nor when it died and a fresh one is provisioned;
#   5. ``_validate_repo_url`` still rejects file:// / git@ / ssh:// / bare paths /
#      credential-embedding URLs.
from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from pocketpaw_ee.cloud._core.errors import BadRequest
from pocketpaw_ee.cloud.codeproject import lifecycle
from pocketpaw_ee.cloud.codeproject import service as codeproject_service
from pocketpaw_ee.cloud.codescaffold import registry as codescaffold_registry
from pocketpaw_ee.cloud.codescaffold.registry import Template
from pocketpaw_ee.cloud.websandbox import durability, provision, scaffold
from pocketpaw_ee.cloud.websandbox import service as sandbox_service

from tests.cloud.test_codeproject_durability import _FakeUploads
from tests.cloud.test_websandbox_provision import _FakeDaytonaClient as _FakeProvisionClient

pytestmark = pytest.mark.usefixtures("mongo_db")

_WS = "ws-1"
_USER = "user-1"
_REPO = "https://github.com/acme/widgets.git"
_STARTER = "react"
_WORKDIR = "/home/daytona"

#: What the fake npm registry hands back for the starter.
_TEMPLATE_FILES = {
    "package.json": '{"name":"template-default"}',
    "src/app.tsx": "TEMPLATE-BASELINE",
}


# ---------------------------------------------------------------------------
# Fakes — every one of them closes a DI seam that already existed.
# ---------------------------------------------------------------------------


@dataclass
class _TracingClient(_FakeProvisionClient):
    """The provisioning fake, plus an ORDER trace.

    ``upload_bytes`` is how a restore lands bytes in the VM, and the fake
    ``bring_up`` never calls it, so appending here gives an unambiguous signal for
    "the restore ran" that can be compared against "the scaffold ran".
    """

    trace: list[str] = field(default_factory=list)

    async def upload_bytes(self, sandbox_id, data, remote_path):  # noqa: ANN001
        self.trace.append("restore")
        await super().upload_bytes(sandbox_id, data, remote_path)


class _RecordingBringUp:
    """Stands in for ``scaffold.bring_up`` — records, touches nothing.

    Returns a clean ``BringUp`` so ``scaffold_into_sandbox`` maps a real response;
    what a real bring-up does to a VM (tar upload, npm install, dev server) is
    ``test_websandbox_scaffold``'s job, not this file's.
    """

    def __init__(self, trace: list[str] | None = None) -> None:
        self.calls: list[dict] = []
        self._trace = trace if trace is not None else []

    async def __call__(  # noqa: ANN204
        self,
        daytona,  # noqa: ANN001
        sandbox_id,  # noqa: ANN001
        files,  # noqa: ANN001
        project_dir,  # noqa: ANN001
        *,
        port=None,  # noqa: ANN001
        assets=None,  # noqa: ANN001
        run_migrations=True,  # noqa: ANN001
    ):
        self._trace.append("scaffold")
        self.calls.append(
            {
                "sandbox_id": sandbox_id,
                "files": dict(files),
                "project_dir": project_dir,
                "port": port,
            }
        )
        return scaffold.BringUp(
            steps=[scaffold.Step(name="materialize", ok=True, exitCode=0)],
            running=True,
            port=port or 5173,
        )


@pytest.fixture()
def uploads(monkeypatch) -> _FakeUploads:  # noqa: ANN001
    """A fake blob store wired in as the module default.

    ``put_project_file`` and ``restore_project`` each build their own uploads
    service, so the seam is closed at module level and ONE instance carries the
    blob — bytes written by a seed are readable by the later restore.
    """
    fake = _FakeUploads()
    monkeypatch.setattr(durability, "build_uploads", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def offline_starter(monkeypatch) -> None:  # noqa: ANN001
    """No npm registry in a unit test.

    Faked at ``fetch_template`` rather than at ``compose`` deliberately: compose
    still runs for real, so the catalog check and the project-name stamp into
    package.json are exercised — those are the parts the open path depends on.
    """

    async def _fetch(starter):  # noqa: ANN001, ANN202
        return Template(files=dict(_TEMPLATE_FILES), assets={})

    monkeypatch.setattr(codescaffold_registry, "fetch_template", _fetch)


async def _scaffold_project(name: str = "booking"):  # noqa: ANN202
    return await codeproject_service.create_project(
        _WS, _USER, {"repo": _STARTER, "provider": "starter", "name": name}
    )


# ---------------------------------------------------------------------------
# Proof 1 — a scaffold project opens on a VM, and never touches the clone path.
# ---------------------------------------------------------------------------


async def test_scaffold_open_provisions_a_bare_vm_and_materializes_the_starter() -> None:
    project = await _scaffold_project()
    client = _TracingClient()
    bring_up = _RecordingBringUp()

    sandbox = await lifecycle.open_project(_WS, _USER, project.id, client=client, bring_up=bring_up)

    # A real VM was provisioned and the row is ready with its Daytona id bound.
    assert len(client.create_calls) == 1
    assert sandbox.status == "ready"
    assert sandbox.sandbox_id == "dtn-1"

    # The starter was materialized INTO that VM, at the pinned workspace dir.
    assert len(bring_up.calls) == 1
    assert bring_up.calls[0]["sandbox_id"] == "dtn-1"
    assert bring_up.calls[0]["project_dir"] == _WORKDIR
    assert bring_up.calls[0]["files"]["src/app.tsx"] == "TEMPLATE-BASELINE"
    # compose ran for real, so the project's own name is in its package.json.
    assert '"name": "booking"' in bring_up.calls[0]["files"]["package.json"]

    # And the git clone path was NOT taken: no clone, no broker upload, no
    # ``git checkout -b``, and no branch on the row (there is no repository).
    assert client.clone_calls == []
    assert not any("git checkout" in c for c in client.exec_calls)
    assert sandbox.branch is None

    # The project is bound to the row it just opened.
    stored = await codeproject_service.get_project(_WS, _USER, project.id)
    assert stored.current_sandbox_id == sandbox.id


async def test_a_template_id_is_never_validated_as_a_repo_url() -> None:
    """The clone validator would reject "react" — and never sees it.

    This is the whole shape of the fix in one test: the rule that made a scaffold
    open impossible is still in force for clones, and the scaffold open succeeds
    anyway because it does not go through the clone.
    """
    with pytest.raises(BadRequest):
        provision._validate_repo_url(_STARTER)

    project = await _scaffold_project()
    sandbox = await lifecycle.open_project(
        _WS, _USER, project.id, client=_TracingClient(), bring_up=_RecordingBringUp()
    )

    assert sandbox.status == "ready"


async def test_the_sandbox_row_key_is_scoped_to_the_project() -> None:
    """Two projects on one starter get two rows, not one shared one.

    A row is unique per (workspace, user, key) and it is what binds a project to a
    VM. Keying on the bare template id would make opening the second project
    rebind — and tear down — the first one's VM, and then hand the first project
    the second's files on its next open.
    """
    first = await _scaffold_project("alpha")
    second = await _scaffold_project("beta")
    client = _TracingClient()

    a = await lifecycle.open_project(
        _WS, _USER, first.id, client=client, bring_up=_RecordingBringUp()
    )
    b = await lifecycle.open_project(
        _WS, _USER, second.id, client=client, bring_up=_RecordingBringUp()
    )

    assert a.id != b.id
    assert a.sandbox_id != b.sandbox_id
    row_a = await sandbox_service.get_sandbox(_WS, _USER, a.id)
    row_b = await sandbox_service.get_sandbox(_WS, _USER, b.id)
    assert row_a.repo == f"starter:{_STARTER}:{first.id}"
    assert row_b.repo == f"starter:{_STARTER}:{second.id}"
    # Opening the second project did not tear the first one's VM down.
    assert client.delete_calls == []


# ---------------------------------------------------------------------------
# Proof 2 — a repo project is completely unchanged.
# ---------------------------------------------------------------------------


async def test_a_repo_project_open_is_unchanged() -> None:
    project = await codeproject_service.create_project(_WS, _USER, {"repo": _REPO})
    client = _TracingClient()
    bring_up = _RecordingBringUp()

    sandbox = await lifecycle.open_project(_WS, _USER, project.id, client=client, bring_up=bring_up)

    # Cloned, branched, and keyed on the repo URL exactly as before.
    assert len(client.clone_calls) == 1
    assert client.clone_calls[0]["repo_url"] == _REPO
    assert client.clone_calls[0]["path"] == _WORKDIR
    assert sandbox.branch is not None and sandbox.branch.startswith("paw/edit-")
    assert any(f"git checkout -b {sandbox.branch}" == c for c in client.exec_calls)
    row = await sandbox_service.get_sandbox(_WS, _USER, sandbox.id)
    assert row.repo == _REPO

    # And nothing was scaffolded into it.
    assert bring_up.calls == []


# ---------------------------------------------------------------------------
# Proof 3 — the durable state restores ON TOP of the scaffold, in that order.
# ---------------------------------------------------------------------------


async def test_durable_state_restores_on_top_of_the_scaffold(uploads) -> None:  # noqa: ANN001
    """The template is the baseline; the user's work wins.

    Order is the assertion. Restoring first would let the template overwrite the
    edits it is supposed to sit under.
    """
    project = await _scaffold_project()
    # A file the user saved in a previous session, on the SAME path the starter
    # ships — the collision that makes ordering observable.
    await durability.put_project_file(_WS, _USER, project.id, "src/app.tsx", "USER-EDIT")

    trace: list[str] = []
    client = _TracingClient(trace=trace)
    bring_up = _RecordingBringUp(trace)

    await lifecycle.open_project(_WS, _USER, project.id, client=client, bring_up=bring_up)

    assert trace == ["scaffold", "restore"], "the restore must land AFTER the scaffold"
    # The bytes that landed last are the user's, at the same path the starter used.
    assert client.upload_calls[-1]["data"] == b"USER-EDIT"
    assert client.upload_calls[-1]["path"] == f"{_WORKDIR}/src/app.tsx"
    assert bring_up.calls[0]["files"]["src/app.tsx"] == "TEMPLATE-BASELINE"


# ---------------------------------------------------------------------------
# Proof 4 — reopen safety. The dangerous edge: re-materializing a template over
# work the user has already done.
# ---------------------------------------------------------------------------


async def test_reopening_a_live_scaffold_project_does_not_re_scaffold() -> None:
    project = await _scaffold_project()
    client = _TracingClient()
    bring_up = _RecordingBringUp()

    first = await lifecycle.open_project(_WS, _USER, project.id, client=client, bring_up=bring_up)
    second = await lifecycle.open_project(_WS, _USER, project.id, client=client, bring_up=bring_up)

    # Same live VM reused — no new VM, and crucially NO second scaffold over the
    # files the user has been editing in it.
    assert second.sandbox_id == first.sandbox_id
    assert len(client.create_calls) == 1
    assert len(bring_up.calls) == 1


async def test_reopening_after_the_vm_died_rescaffolds_only_the_empty_baseline(uploads) -> None:  # noqa: ANN001
    """A fresh VM is empty, so the template is again the correct baseline.

    Daytona's own lifecycle deletes an idle Code Mode VM in minutes, so this is the
    ordinary returning-user path, not an edge. What must hold is that the user's
    work still lands last.
    """
    project = await _scaffold_project()
    trace: list[str] = []
    client = _TracingClient(trace=trace)
    bring_up = _RecordingBringUp(trace)

    first = await lifecycle.open_project(_WS, _USER, project.id, client=client, bring_up=bring_up)
    # The user edits a file; it is mirrored to the durable project store.
    await durability.put_project_file(_WS, _USER, project.id, "src/app.tsx", "USER-EDIT")
    # Daytona reclaims the idle VM out of band; the Mongo row still says ready.
    client.deleted_out_of_band.add(first.sandbox_id)
    trace.clear()

    second = await lifecycle.open_project(_WS, _USER, project.id, client=client, bring_up=bring_up)

    assert second.sandbox_id != first.sandbox_id
    assert len(bring_up.calls) == 2, "a brand-new empty VM does need the baseline"
    assert trace == ["scaffold", "restore"], "and the user's work still lands last"
    assert client.upload_calls[-1]["data"] == b"USER-EDIT"


# ---------------------------------------------------------------------------
# Proof 5 — the clone validator is unchanged. Asserted explicitly so a later
# refactor cannot quietly loosen it to "make scaffolds work".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "repo",
    [
        "file:///etc/passwd",
        "file://../../secrets",
        "git@github.com:acme/widgets.git",
        "ssh://git@github.com/acme/widgets.git",
        "/var/lib/secrets",
        "../../etc",
        "acme/widgets",
        _STARTER,
        "https://user:token@github.com/acme/widgets.git",
        "https://x-access-token:ghp_deadbeef@github.com/acme/widgets.git",
        "",
        "   ",
    ],
)
def test_validate_repo_url_still_fails_closed(repo: str) -> None:
    with pytest.raises(BadRequest) as exc:
        provision._validate_repo_url(repo)

    assert exc.value.status_code == 400


def test_validate_repo_url_still_accepts_a_clean_https_url() -> None:
    assert provision._validate_repo_url(f"  {_REPO}  ") == _REPO
