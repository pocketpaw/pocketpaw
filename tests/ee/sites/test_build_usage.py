# Tests for what a site/project build sandbox really uses and what it asks for:
# the end-of-build usage probe (``sites/build_usage.py``: peak memory from cgroup v2,
# v1 or /proc, ``du -sb`` of the workdir, node_modules and build output, one INFO line
# per build) and the env-configurable build sandbox size (``PAW_SITES_BUILD_*``) in
# ``daytona_runner.run_build``. The probe must never change a build's verdict.
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from pocketpaw_ee.sites import build_usage
from pocketpaw_ee.sites import daytona_runner as dr

from tests.ee.sites.faults import FaultyDaytonaClient

REACT_FILES = {"src/App.tsx": "export default function App() { return <p>hi</p>; }"}

V2_OUTPUT = (
    "mem_peak_bytes=1073741824\n"
    "mem_source=cgroup_v2\n"
    "disk_workdir_bytes=524288000\n"
    "disk_node_modules_bytes=419430400\n"
    "disk_output_bytes=2097152\n"
    "disk_fs_used_bytes=3221225472\n"
)

_ENVS = (dr.BUILD_CPU_ENV, dr.BUILD_MEMORY_ENV, dr.BUILD_DISK_ENV)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENVS:
        monkeypatch.delenv(name, raising=False)


def _usage_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith("sites.build_usage:")]


# ---------------------------------------------------------------------------
# The probe command and its parser
# ---------------------------------------------------------------------------


class TestProbe:
    def test_command_reads_every_memory_source_and_sizes_the_dirs(self) -> None:
        cmd = build_usage.usage_probe_command(
            "/home/daytona/paw-build", "/home/daytona/paw-build/dist"
        )
        assert build_usage.USAGE_PROBE_MARKER in cmd
        assert "/sys/fs/cgroup/memory.peak" in cmd
        assert "/sys/fs/cgroup/memory/memory.max_usage_in_bytes" in cmd
        assert "/proc/meminfo" in cmd
        assert "du -sb /home/daytona/paw-build " in cmd or "du -sb /home/daytona/paw-build\n" in cmd
        assert "/home/daytona/paw-build/node_modules" in cmd
        assert "/home/daytona/paw-build/dist" in cmd
        # Every step is bounded, so a huge tree cannot hold the sandbox open.
        assert cmd.count("timeout ") >= 4

    def test_parse_reads_the_numbers(self) -> None:
        got = build_usage.parse_usage(V2_OUTPUT)
        assert got == {
            "mem_peak_bytes": 1073741824,
            "mem_source": "cgroup_v2",
            "disk_workdir_bytes": 524288000,
            "disk_node_modules_bytes": 419430400,
            "disk_output_bytes": 2097152,
            "disk_fs_used_bytes": 3221225472,
        }

    @pytest.mark.parametrize(
        "raw", ["", None, "garbage\n=\nmem_peak_bytes=\n", "mem_peak_bytes=abc"]
    )
    def test_parse_turns_missing_values_into_none(self, raw: Any) -> None:
        got = build_usage.parse_usage(raw)
        assert got["mem_peak_bytes"] is None
        assert got["disk_workdir_bytes"] is None
        assert got["mem_source"] is None


# ---------------------------------------------------------------------------
# The probe inside run_build
# ---------------------------------------------------------------------------


class TestRunBuildMeasures:
    async def test_one_info_line_per_build_with_size_usage_and_duration(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = FaultyDaytonaClient()
        client.probe_output = V2_OUTPUT
        with caplog.at_level(logging.INFO, logger="pocketpaw_ee.sites.build_usage"):
            got = await dr.run_build(
                REACT_FILES,
                engine="react",
                timeout_seconds=600,
                client=client,
                labels={"paw_pocket": "pk1", "paw_job": "job1"},
            )
        assert got.ok
        assert client.calls.index("measure") < client.calls.index("delete")
        assert len(client.probe_commands) == 1
        records = _usage_records(caplog)
        assert len(records) == 1
        msg = records[0].getMessage()
        for part in (
            "engine=react",
            "lane=site-build",
            "pocket=pk1",
            "job=job1",
            "outcome=completed_ok",
            "cpu=2",
            "memory_gib=4",
            "disk_gib=10",
            "peak_memory_mib=1024",
            "memory_source=cgroup_v2",
            "workdir_mib=500",
            "node_modules_mib=400",
            "output_mib=2",
            "fs_used_mib=3072",
            "duration_s=",
        ):
            assert part in msg, (part, msg)
        assert records[0].levelno == logging.INFO
        usage = records[0].build_usage  # type: ignore[attr-defined]
        assert usage["mem_peak_bytes"] == 1073741824
        assert usage["memory_gib"] == 4

    async def test_a_failed_build_is_measured_too(self, caplog: pytest.LogCaptureFixture) -> None:
        """An OOM or a failing install is exactly the build worth sizing against."""
        from tests.ee.sites.faults import ok_sentinel

        client = FaultyDaytonaClient(sentinel=ok_sentinel(build_exit=1, stderr_tail="TS2304"))
        client.probe_output = V2_OUTPUT
        with caplog.at_level(logging.INFO, logger="pocketpaw_ee.sites.build_usage"):
            got = await dr.run_build(
                REACT_FILES, engine="react", timeout_seconds=600, client=client
            )
        assert got.classification.outcome == "build_failed"
        assert "outcome=build_failed" in _usage_records(caplog)[0].getMessage()

    async def test_a_probe_that_raises_never_fails_the_build(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        class _Raising(FaultyDaytonaClient):
            async def execute_command(
                self, sandbox_id: str, command: str, timeout: int = 30
            ) -> Any:
                if build_usage.USAGE_PROBE_MARKER in command:
                    self.calls.append("measure")
                    raise RuntimeError("exec transport died")
                return await super().execute_command(sandbox_id, command, timeout)

        client = _Raising()
        with caplog.at_level(logging.INFO, logger="pocketpaw_ee.sites.build_usage"):
            got = await dr.run_build(
                REACT_FILES, engine="react", timeout_seconds=600, client=client
            )
        assert got.ok and got.sandbox_deleted
        msg = _usage_records(caplog)[0].getMessage()
        assert "peak_memory_mib=unknown" in msg
        assert "cpu=2" in msg

    async def test_a_hung_probe_is_cut_off(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(build_usage, "PROBE_TIMEOUT_SECONDS", 0.05)

        class _Hanging(FaultyDaytonaClient):
            async def execute_command(
                self, sandbox_id: str, command: str, timeout: int = 30
            ) -> Any:
                if build_usage.USAGE_PROBE_MARKER in command:
                    self.calls.append("measure")
                    await asyncio.sleep(60)
                return await super().execute_command(sandbox_id, command, timeout)

        client = _Hanging()
        with caplog.at_level(logging.INFO, logger="pocketpaw_ee.sites.build_usage"):
            got = await asyncio.wait_for(
                dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=client),
                timeout=10,
            )
        assert got.ok and got.sandbox_deleted
        assert "peak_memory_mib=unknown" in _usage_records(caplog)[0].getMessage()

    async def test_missing_files_are_skipped_not_failed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = FaultyDaytonaClient()
        client.probe_output = "mem_peak_bytes=\nmem_source=none\ndisk_workdir_bytes=1048576\n"
        with caplog.at_level(logging.INFO, logger="pocketpaw_ee.sites.build_usage"):
            got = await dr.run_build(
                REACT_FILES, engine="react", timeout_seconds=600, client=client
            )
        assert got.ok
        msg = _usage_records(caplog)[0].getMessage()
        assert "peak_memory_mib=unknown" in msg
        assert "workdir_mib=1" in msg
        assert "node_modules_mib=unknown" in msg

    async def test_no_probe_when_the_sandbox_left_no_evidence(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No sentinel means the box is likely gone: an exec would only wait out its
        timeout. The line is still logged, with the usage unknown."""
        client = FaultyDaytonaClient(fail_at="exec", sentinel=None)
        with caplog.at_level(logging.INFO, logger="pocketpaw_ee.sites.build_usage"):
            got = await dr.run_build(
                REACT_FILES, engine="react", timeout_seconds=600, client=client
            )
        assert got.classification.outcome == "infra_lost"
        assert "measure" not in client.calls
        assert "peak_memory_mib=unknown" in _usage_records(caplog)[0].getMessage()

    async def test_the_probe_sizes_the_engines_output_dir(self) -> None:
        client = FaultyDaytonaClient()
        await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=client)
        assert f"{dr.SANDBOX_PROJECT_DIR}/dist" in client.probe_commands[0]
        assert f"{dr.SANDBOX_PROJECT_DIR}/node_modules" in client.probe_commands[0]


# ---------------------------------------------------------------------------
# Build sandbox size from the environment
# ---------------------------------------------------------------------------


class TestBuildResources:
    async def _create_kwargs(self, **kw: Any) -> dict[str, Any]:
        client = FaultyDaytonaClient(fail_at="create")
        with pytest.raises(Exception):  # noqa: B017 - the injected create failure
            await dr.run_build(
                REACT_FILES, engine="react", timeout_seconds=600, client=client, **kw
            )
        return client.create_kwargs

    async def test_defaults_are_todays_size(self) -> None:
        kw = await self._create_kwargs()
        assert (kw["cpu"], kw["memory"], kw["disk"]) == (2, 4, 10)

    async def test_env_overrides_each_dimension(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PAW_SITES_BUILD_CPU", "1")
        monkeypatch.setenv("PAW_SITES_BUILD_MEMORY_GIB", "3")
        monkeypatch.setenv("PAW_SITES_BUILD_DISK_GIB", " 6 ")
        kw = await self._create_kwargs()
        assert (kw["cpu"], kw["memory"], kw["disk"]) == (1, 3, 6)

    @pytest.mark.parametrize("raw", ["0", "-2", "two", "", "1.5"])
    async def test_a_bad_value_keeps_the_default(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv("PAW_SITES_BUILD_MEMORY_GIB", raw)
        kw = await self._create_kwargs()
        assert kw["memory"] == 4

    async def test_an_explicit_argument_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PAW_SITES_BUILD_MEMORY_GIB", "3")
        kw = await self._create_kwargs(memory_gb=8)
        assert kw["memory"] == 8

    async def test_the_admission_lease_asks_for_the_configured_memory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked: list[int] = []
        real = dr.memory_lease

        def _lease(gib: int, **kw: Any) -> Any:
            asked.append(gib)
            return real(gib, **kw)

        monkeypatch.setattr(dr, "memory_lease", _lease)
        monkeypatch.setenv("PAW_SITES_BUILD_MEMORY_GIB", "3")
        await self._create_kwargs()
        assert asked == [3]

    def test_the_resolver_reads_at_call_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert dr.build_resources() == (2, 4, 10)
        monkeypatch.setenv("PAW_SITES_BUILD_CPU", "4")
        assert dr.build_resources() == (4, 4, 10)


def test_the_fakes_know_the_probe_marker() -> None:
    from tests.ee.sites import faults

    assert faults.USAGE_PROBE_MARKER == build_usage.USAGE_PROBE_MARKER


def test_probe_response_shapes() -> None:
    """The SDK's ExecuteResponse carries stdout as ``result``; a bare string or None
    (a fake, an older SDK) must not raise."""
    assert build_usage.response_stdout(SimpleNamespace(result="a=1")) == "a=1"
    assert build_usage.response_stdout("a=1") == "a=1"
    assert build_usage.response_stdout(None) == ""
