# tests/ee/sites/test_daytona_runner.py — the ephemeral Daytona round-trip driver
# (ee/pocketpaw_ee/sites/daytona_runner.py), exercised against a recording fake client.
#
# Created 2026-08-09 (SG-9i slice 1).
#
# WHY A FAKE AND NOT A MOCK OF THE SDK: the contract this module has to hold is about
# ORDER and CLEANUP, not about return values. A fake that records the call sequence lets
# the tests assert the two properties that actually matter and cannot be checked by
# inspection — that the sentinel is read BEFORE teardown, and that teardown happens on
# every path including the ones where the build blew up.
#
# The real round-trip against live Daytona lives in
# scripts/sg9_daytona_roundtrip.py, because it costs sandbox time and needs
# credentials. These tests are not a substitute for it and do not claim to be: a fake
# passing is not evidence the lane works, only that the driver sequences correctly.

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest
from pocketpaw_ee.sites import daytona_build as db
from pocketpaw_ee.sites import daytona_runner as dr

from tests.ee.sites.faults import clean_artifact

REACT_FILES = {"src/App.tsx": "export default function App() { return <p>hi</p>; }"}


@dataclass
class _SandboxInfo:
    id: str


class FakeClient:
    """Records every call in order so the tests can assert sequencing.

    ``sentinel`` is what ``download_file`` returns for the result path: bytes for a
    readable sentinel, ``None`` to simulate one that cannot be read (a dead or already
    reaped sandbox).
    """

    def __init__(
        self,
        *,
        sentinel: dict[str, Any] | None,
        exec_raises: bool = False,
        artifact: bytes | None = None,
        delete_raises: bool = False,
    ) -> None:
        # Updated 2026-08-10 (SG-7): the default was the placeholder ``b"tgz-bytes"``, which
        # is not a readable tar and does not match the size ``_ok_sentinel`` promises. A
        # TRUTHFULNESS fix — nothing reads either value today — but a fake asserting against
        # a fiction is how a real mismatch later goes unnoticed.
        artifact = clean_artifact() if artifact is None else artifact
        self.calls: list[str] = []
        self.create_kwargs: dict[str, Any] = {}
        self.exec_timeout: int | None = None
        self._sentinel = sentinel
        self._exec_raises = exec_raises
        self._artifact = artifact
        self._delete_raises = delete_raises

    async def create_sandbox(self, **kwargs: Any) -> _SandboxInfo:
        self.calls.append("create")
        self.create_kwargs = kwargs
        return _SandboxInfo(id="sb-1")

    async def wait_for_sandbox(self, sandbox_id: str, target_state: str = "started") -> None:
        self.calls.append("wait")

    async def bulk_upload(self, sandbox_id: str, files: list[tuple[Any, str]]) -> None:
        self.calls.append("upload")
        self.uploaded = files

    async def execute_command(self, sandbox_id: str, command: str, timeout: int = 30) -> Any:
        self.calls.append("exec")
        self.exec_timeout = timeout
        if self._exec_raises:
            raise RuntimeError("sandbox went away mid-build")
        return object()

    async def download_file(self, sandbox_id: str, remote_path: str) -> bytes:
        if remote_path.endswith(db.BUILD_RESULT_FILENAME):
            self.calls.append("read_sentinel")
            if self._sentinel is None:
                raise FileNotFoundError(remote_path)
            return json.dumps(self._sentinel).encode()
        self.calls.append("download_artifact")
        return self._artifact

    async def delete_sandbox(self, sandbox_id: str) -> None:
        self.calls.append("delete")
        if self._delete_raises:
            raise RuntimeError("delete failed")


def _ok_sentinel(**over: Any) -> dict[str, Any]:
    base = {
        "schema": db.SENTINEL_SCHEMA,
        "engine": "react",
        "install_exit": 0,
        "build_exit": 0,
        "artifact_rel": "dist",
        # Equals what FakeClient returns — see its __init__ comment.
        "artifact_bytes": len(clean_artifact()),
        "stderr_tail": "",
    }
    base.update(over)
    return base


class TestTheFourRowsThroughTheDriver:
    """All four outcomes, driven end to end. Two of them (``timed_out``,
    ``infra_lost``) can only be reached by inducing them, which is the point of the
    fake."""

    async def test_row1_completed_ok_returns_the_artifact(self) -> None:
        c = FakeClient(sentinel=_ok_sentinel())
        got = await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=c)
        assert got.classification.outcome == "completed_ok"
        assert got.ok is True
        assert got.artifact_bytes == len(clean_artifact())

    async def test_row2_build_failed_surfaces_stderr_and_no_artifact(self) -> None:
        c = FakeClient(sentinel=_ok_sentinel(build_exit=1, stderr_tail="TS2304"))
        got = await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=c)
        assert got.classification.outcome == "build_failed"
        assert got.classification.blames_user is True
        assert "TS2304" in got.classification.stderr_tail
        assert got.artifact is None
        assert "download_artifact" not in c.calls

    async def test_row3_timed_out_when_no_sentinel_and_clock_expired(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Induced: no readable sentinel, and our own clock past the budget. A tiny
        timeout makes any real elapsed time exceed it."""
        c = FakeClient(sentinel=None, exec_raises=True)
        got = await dr.run_build(REACT_FILES, engine="react", timeout_seconds=0, client=c)
        assert got.classification.outcome == "timed_out"
        assert got.classification.retryable is True
        assert got.classification.blames_user is False

    async def test_row4_infra_lost_when_no_sentinel_before_the_budget(self) -> None:
        """Induced: the exec blows up and no sentinel survives, well inside the budget.
        This is the row that must NOT reach the user as a build failure.

        Corrected 2026-08-10 (SG-7): this used to end "if it does, the local-builder
        fallback never fires", which describes a fallback the captain overrode. The
        ruling is Daytona-only. What actually goes wrong when this row is misclassified
        is that the user is told their site is broken when we lost the container — the
        mis-report the whole sentinel design exists to prevent."""
        c = FakeClient(sentinel=None, exec_raises=True)
        got = await dr.run_build(REACT_FILES, engine="react", timeout_seconds=100_000, client=c)
        assert got.classification.outcome == "infra_lost"
        assert got.classification.retryable is True
        assert got.classification.blames_user is False

    async def test_oom_killed_build_is_infra_lost_not_build_failed(self) -> None:
        """The residual gap: a signalled process still runs the trap, so this arrives
        WITH a sentinel and a non-zero exit and looks exactly like a real failure."""
        c = FakeClient(sentinel=_ok_sentinel(build_exit=137))
        got = await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=c)
        assert got.classification.outcome == "infra_lost"
        assert got.classification.blames_user is False


class TestSequencingIsTheContract:
    async def test_sentinel_is_read_before_teardown(self) -> None:
        """The single most important ordering in the module. Delete first and the
        evidence is gone, and "not found" can no longer distinguish a normal delete
        from a death."""
        c = FakeClient(sentinel=_ok_sentinel())
        await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=c)
        assert c.calls.index("read_sentinel") < c.calls.index("delete")

    async def test_artifact_is_downloaded_before_teardown(self) -> None:
        c = FakeClient(sentinel=_ok_sentinel())
        await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=c)
        assert c.calls.index("download_artifact") < c.calls.index("delete")

    async def test_full_happy_path_order(self) -> None:
        c = FakeClient(sentinel=_ok_sentinel())
        await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=c)
        assert c.calls == [
            "create",
            "wait",
            "upload",
            "exec",
            "read_sentinel",
            "download_artifact",
            "delete",
        ]

    async def test_the_wrapper_is_uploaded_alongside_the_project(self) -> None:
        c = FakeClient(sentinel=_ok_sentinel())
        await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=c)
        dests = [dest for _, dest in c.uploaded]
        assert dr.SANDBOX_WRAPPER_PATH in dests
        assert f"{dr.SANDBOX_PROJECT_DIR}/src/App.tsx" in dests


class TestTeardownAlwaysHappens:
    async def test_delete_runs_when_the_build_fails(self) -> None:
        c = FakeClient(sentinel=_ok_sentinel(build_exit=1))
        got = await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=c)
        assert "delete" in c.calls
        assert got.sandbox_deleted is True

    async def test_delete_runs_when_the_exec_raises(self) -> None:
        c = FakeClient(sentinel=None, exec_raises=True)
        await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=c)
        assert "delete" in c.calls

    async def test_a_failing_delete_does_not_mask_the_build_result(self) -> None:
        """A cleanup error must not turn a successful build into an exception — the
        auto-delete backstop still reaps the sandbox."""
        c = FakeClient(sentinel=_ok_sentinel(), delete_raises=True)
        got = await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=c)
        assert got.classification.outcome == "completed_ok"
        assert got.sandbox_deleted is False

    async def test_no_snapshot_is_ever_taken(self) -> None:
        """A stated invariant, not an accident: the SR-3 decision that a per-site signed
        key in the build inputs is acceptable rests on the key living only in a
        container that is destroyed. A snapshot would make it durable."""
        c = FakeClient(sentinel=_ok_sentinel())
        await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=c)
        assert not any("snapshot" in call for call in c.calls)


class TestLifecycleAndTimeoutWiring:
    async def test_idle_auto_stop_exceeds_the_build_timeout(self) -> None:
        """The bug this guards is subtle and expensive: Daytona counts INACTIVITY, and a
        long cold install makes no API calls. An auto-stop shorter than the build would
        kill healthy builds — and it would arrive as a mid-build death, which is the
        hardest failure to diagnose."""
        c = FakeClient(sentinel=_ok_sentinel())
        await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=c)
        idle_minutes = c.create_kwargs["auto_stop_interval"]
        assert idle_minutes * 60 > 600

    async def test_auto_delete_is_the_backstop(self) -> None:
        """0 = delete immediately on stop, so a sandbox orphaned by OUR process dying is
        still reaped without human action."""
        c = FakeClient(sentinel=_ok_sentinel())
        await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=c)
        assert c.create_kwargs["auto_delete_interval"] == 0

    async def test_our_exec_budget_is_looser_than_the_in_sandbox_one(self) -> None:
        """The inner ``timeout(1)`` must win the race, because that path still runs the
        trap and produces a sentinel. If ours fired first we would lose the evidence."""
        c = FakeClient(sentinel=_ok_sentinel())
        await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=c)
        assert c.exec_timeout is not None and c.exec_timeout > 600

    async def test_timeout_is_not_hardcoded(self) -> None:
        c = FakeClient(sentinel=_ok_sentinel())
        await dr.run_build(REACT_FILES, engine="react", timeout_seconds=1234, client=c)
        assert c.exec_timeout == 1234 + dr.EXEC_TIMEOUT_SLACK_SECONDS

    async def test_timings_are_recorded_for_every_phase(self) -> None:
        c = FakeClient(sentinel=_ok_sentinel())
        got = await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=c)
        d = got.timings.as_dict()
        assert set(d) == {"S_create", "U_upload", "IB_exec", "D_extract", "total"}
        assert all(v >= 0 for v in d.values())


class TestFailsBeforeSpendingMoney:
    async def test_an_engine_whose_output_is_the_project_root_is_refused_pre_create(
        self,
    ) -> None:
        """html's static output is ``.``, so an include-list cannot exclude
        node_modules. Refusing BEFORE create_sandbox means a routing bug costs nothing
        instead of a billed sandbox."""
        c = FakeClient(sentinel=_ok_sentinel())
        with pytest.raises(ValueError, match="project root"):
            await dr.run_build(REACT_FILES, engine="html", timeout_seconds=600, client=c)
        assert c.calls == []


class TestTheSupplyChainFloorReachesTheSandbox:
    """SL-3. The workspace's install protections live in the DEVELOPER'S HOME DIR
    (``~/.npmrc``, ``~/.bunfig.toml``) and are in no repo, so a fresh container inherits
    none of them. Once Daytona is the only build host, that makes the build box weaker
    than the runtime image beside it — these tests are what stop the floor being dropped
    by a refactor nobody connects to supply chain.
    """

    async def test_a_bunfig_is_uploaded_into_the_project_dir(self) -> None:
        c = FakeClient(sentinel=_ok_sentinel())
        await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=c)
        dests = [dest for _, dest in c.uploaded]
        assert f"{dr.SANDBOX_PROJECT_DIR}/{dr.SANDBOX_BUNFIG_REL}" in dests

    async def test_the_floor_carries_the_seven_day_release_age_and_no_scripts(self) -> None:
        """The two controls, asserted on the BYTES that actually get uploaded rather
        than on the constant, so a broken encode or a wrong destination still fails."""
        c = FakeClient(sentinel=_ok_sentinel())
        await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=c)
        dst = f"{dr.SANDBOX_PROJECT_DIR}/{dr.SANDBOX_BUNFIG_REL}"
        body = next(src for src, dest in c.uploaded if dest == dst)
        text = body.decode() if isinstance(body, bytes) else body
        # 604800 = 7 days in seconds, bun's unit. The number is asserted literally: a
        # plausible-looking wrong value (a day, or minutes) is the failure mode here.
        assert "minimumReleaseAge = 604800" in text
        assert "ignoreScripts = true" in text
        assert "[install]" in text

    async def test_a_project_supplied_bunfig_is_dropped_so_the_floor_wins(self) -> None:
        """A floor a built project can override is not a floor.

        Resolved in our own code, NOT by upload ordering: bulk_upload hands the whole
        list to the SDK in one batch, so which write survives a duplicate destination is
        not ours to guarantee. Asserting exactly ONE bunfig destination is what proves
        the resolution happened here.
        """
        hostile = dict(REACT_FILES)
        hostile["bunfig.toml"] = "[install]\nminimumReleaseAge = 0\nignoreScripts = false\n"
        c = FakeClient(sentinel=_ok_sentinel())
        await dr.run_build(hostile, engine="react", timeout_seconds=600, client=c)

        dst = f"{dr.SANDBOX_PROJECT_DIR}/{dr.SANDBOX_BUNFIG_REL}"
        matching = [src for src, dest in c.uploaded if dest == dst]
        assert len(matching) == 1, "the project's bunfig must be dropped, not merely lost a race"
        text = matching[0].decode() if isinstance(matching[0], bytes) else matching[0]
        assert "minimumReleaseAge = 604800" in text
        assert "minimumReleaseAge = 0" not in text
        assert "ignoreScripts = false" not in text

    async def test_dropping_a_project_bunfig_is_logged_not_silent(self, caplog) -> None:
        """The trade this makes is discarding a possibly-legitimate project setting, so
        it must be visible. A silent override is the version of this that wastes an
        afternoon when someone's bun config appears not to apply."""
        import logging

        hostile = dict(REACT_FILES)
        hostile["bunfig.toml"] = "[install]\nminimumReleaseAge = 0\n"
        c = FakeClient(sentinel=_ok_sentinel())
        with caplog.at_level(logging.WARNING):
            await dr.run_build(hostile, engine="react", timeout_seconds=600, client=c)
        # getMessage(), not .message: the latter only exists once a formatter has run,
        # so reading it here raises rather than failing the assertion it looks like.
        assert any("bunfig.toml" in r.getMessage() for r in caplog.records)

    async def test_the_floor_does_not_displace_the_project_or_the_wrapper(self) -> None:
        """Adding an upload must not cost an existing one."""
        c = FakeClient(sentinel=_ok_sentinel())
        await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=c)
        dests = [dest for _, dest in c.uploaded]
        assert dr.SANDBOX_WRAPPER_PATH in dests
        for rel in REACT_FILES:
            assert f"{dr.SANDBOX_PROJECT_DIR}/{rel}" in dests
        # No duplicate destinations at all — a batch upload with two writes to one path
        # is ambiguous by construction.
        assert len(dests) == len(set(dests))
