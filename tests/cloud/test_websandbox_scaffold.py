# test_websandbox_scaffold.py — materializing a composed project into a VM (CS-2).
#
# Created 2026-07-22 (feat/codescaffold-daytona).
#
# Two testable layers, and no VM in either:
#
#   * `pack_source_map` is pure. The tarball it builds is the thing that gets
#     extracted inside a sandbox, so its path handling is a security boundary and
#     is tested as one.
#   * `bring_up` drives a fake DaytonaClient. What matters is the SEQUENCING —
#     stop at the first failure, report what happened, never raise — and a fake
#     exercises exactly that. What it deliberately does NOT prove is that npm
#     install succeeds in a real image; that is CV-1's job.
from __future__ import annotations

import io
import tarfile

import pytest
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.websandbox import scaffold

PROJECT_DIR = "/home/daytona"
FILES = {
    "package.json": '{"name":"demo"}',
    "src/routes/+page.svelte": "<h1>hi</h1>",
    "migrations/0001_init.sql": "CREATE TABLE t (id integer);",
}


# ── Fakes ───────────────────────────────────────────────────────────────────


class _Exec:
    def __init__(self, exit_code: int, result: str = "") -> None:
        self.exit_code = exit_code
        self.result = result


class _FakeDaytona:
    """Records what would have been done to a VM.

    `failures` maps a substring of a command to the response it should get, so a
    test can fail exactly one stage without scripting the whole sequence.
    """

    def __init__(self, failures: dict[str, _Exec] | None = None) -> None:
        self.uploads: list[tuple[bytes, str]] = []
        self.commands: list[tuple[str, str | None]] = []
        self._failures = failures or {}
        self.upload_error: Exception | None = None
        self.exec_error: Exception | None = None

    async def upload_bytes(self, sandbox_id: str, data: bytes, remote_path: str) -> None:
        if self.upload_error:
            raise self.upload_error
        self.uploads.append((data, remote_path))

    async def execute_command(self, sandbox_id, command, cwd=None, timeout=30):  # noqa: ANN001
        self.commands.append((command, cwd))
        if self.exec_error:
            raise self.exec_error
        for needle, response in self._failures.items():
            if needle in command:
                return response
        return _Exec(0, "ok")


def _members(packed: bytes) -> dict[str, str]:
    with tarfile.open(fileobj=io.BytesIO(packed), mode="r:gz") as tar:
        return {
            m.name: tar.extractfile(m).read().decode("utf-8")  # type: ignore[union-attr]
            for m in tar.getmembers()
            if m.isfile()
        }


# ── Packing ─────────────────────────────────────────────────────────────────


def test_pack_round_trips_every_file() -> None:
    assert _members(scaffold.pack_source_map(FILES)) == FILES


def test_pack_is_deterministic() -> None:
    """Same project, same bytes. Makes a scaffold reproducible from a bug report,
    and lets a future caller cache on the digest. A wall-clock mtime is the one
    thing that would silently break this."""
    assert scaffold.pack_source_map(FILES) == scaffold.pack_source_map(FILES)


def test_pack_does_not_depend_on_key_order() -> None:
    reversed_map = dict(reversed(list(FILES.items())))

    assert scaffold.pack_source_map(FILES) == scaffold.pack_source_map(reversed_map)


def test_an_empty_project_is_refused() -> None:
    with pytest.raises(CloudError) as exc:
        scaffold.pack_source_map({})

    assert exc.value.status_code == 400


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        "src/../../outside.txt",
        "/etc/passwd",
        "C:/Windows/system32/evil.dll",
        "",
    ],
)
def test_a_path_that_escapes_the_project_is_refused(path: str) -> None:
    """This tarball is extracted with `tar -xzf` inside a VM. A `..` component or
    an absolute path writes outside the project directory.

    The map comes from our own engine, so this is defence in depth rather than a
    trust boundary — but the cost of checking is nothing and the cost of not
    checking is arbitrary file write.
    """
    with pytest.raises(CloudError) as exc:
        scaffold.pack_source_map({path: "payload"})

    assert exc.value.status_code == 400


def test_windows_separators_are_normalised() -> None:
    """This backend runs on Windows in development, and a tar member named
    `src\\routes\\x` extracts as one flat file with backslashes in its name."""
    packed = scaffold.pack_source_map({"src\\lib\\a.ts": "x"})

    assert "src/lib/a.ts" in _members(packed)


def test_a_pathological_project_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scaffold, "MAX_PACKED_BYTES", 10)

    with pytest.raises(CloudError) as exc:
        scaffold.pack_source_map({"big.txt": "x" * 100_000})

    assert exc.value.status_code == 413


# ── Materialize ─────────────────────────────────────────────────────────────


async def test_materialize_uploads_once_and_extracts() -> None:
    """One tarball, one extraction — the broker's vehicle. The file-RPC would
    need a round trip per file and a composed project is ~50 of them."""
    daytona = _FakeDaytona()

    step = await scaffold.materialize(daytona, "sb-1", FILES, PROJECT_DIR)

    assert step.ok
    assert len(daytona.uploads) == 1
    assert len(daytona.commands) == 1
    command = daytona.commands[0][0]
    assert "tar -xzf" in command
    assert PROJECT_DIR in command


async def test_materialize_removes_the_staging_tarball() -> None:
    """Left behind, it would be picked up by the next snapshot and shipped around
    as part of the user's workspace."""
    daytona = _FakeDaytona()

    await scaffold.materialize(daytona, "sb-1", FILES, PROJECT_DIR)

    assert "rm -f" in daytona.commands[0][0]


async def test_a_failed_upload_is_a_clean_error() -> None:
    daytona = _FakeDaytona()
    daytona.upload_error = RuntimeError("connection reset")

    with pytest.raises(CloudError) as exc:
        await scaffold.materialize(daytona, "sb-1", FILES, PROJECT_DIR)

    assert exc.value.status_code == 502


async def test_a_failed_extraction_is_a_failed_step_not_a_raise() -> None:
    """Bring-up needs the record of what happened; a raise would lose it."""
    daytona = _FakeDaytona(failures={"tar -xzf": _Exec(2, "tar: unexpected EOF")})

    step = await scaffold.materialize(daytona, "sb-1", FILES, PROJECT_DIR)

    assert step.ok is False
    assert step.exitCode == 2
    assert "unexpected EOF" in step.output


# ── bring_up ────────────────────────────────────────────────────────────────


async def test_bring_up_runs_the_stages_in_order() -> None:
    daytona = _FakeDaytona()

    result = await scaffold.bring_up(daytona, "sb-1", FILES, PROJECT_DIR)

    assert [s.name for s in result.steps] == ["materialize", "install", "migrate", "dev-server"]
    assert result.running is True
    assert result.failed_step is None


async def test_a_failed_install_stops_and_reports(caplog: pytest.LogCaptureFixture) -> None:
    """CS-2's stated acceptance: a failed `npm install` is a VISIBLE FAILED
    STATE, not a spinner. npm's own error text has to survive to the caller."""
    daytona = _FakeDaytona(
        failures={"npm install": _Exec(1, "npm ERR! ERESOLVE could not resolve")}
    )

    result = await scaffold.bring_up(daytona, "sb-1", FILES, PROJECT_DIR)

    assert result.running is False
    assert result.failed_step is not None
    assert result.failed_step.name == "install"
    assert "ERESOLVE" in result.failed_step.output
    # And it stopped — no dev server was started on a project with no deps.
    assert [s.name for s in result.steps] == ["materialize", "install"]


async def test_a_failed_materialize_never_reaches_install() -> None:
    daytona = _FakeDaytona(failures={"tar -xzf": _Exec(2, "boom")})

    result = await scaffold.bring_up(daytona, "sb-1", FILES, PROJECT_DIR)

    assert [s.name for s in result.steps] == ["materialize"]
    assert not any("npm install" in c for c, _ in daytona.commands)


async def test_successful_steps_carry_no_output() -> None:
    """A clean npm install log is thousands of lines nobody reads, and it would
    dwarf everything else in the response body."""
    daytona = _FakeDaytona()

    result = await scaffold.bring_up(daytona, "sb-1", FILES, PROJECT_DIR)

    assert all(s.output == "" for s in result.steps)


async def test_output_is_tailed_not_dumped() -> None:
    daytona = _FakeDaytona(failures={"npm install": _Exec(1, "x" * 50_000)})

    result = await scaffold.bring_up(daytona, "sb-1", FILES, PROJECT_DIR)

    assert len(result.failed_step.output) < scaffold.MAX_OUTPUT_TAIL + 100


async def test_migrations_are_skipped_when_the_project_has_none() -> None:
    daytona = _FakeDaytona()

    result = await scaffold.bring_up(daytona, "sb-1", {"package.json": "{}"}, PROJECT_DIR)

    assert [s.name for s in result.steps] == ["materialize", "install", "dev-server"]


async def test_the_dev_server_binds_all_interfaces() -> None:
    """Vite binds loopback by default and Daytona's preview URL reaches the VM
    from OUTSIDE. Without an explicit host bind the server runs perfectly and the
    preview pane shows nothing — the worst kind of working."""
    daytona = _FakeDaytona()

    await scaffold.bring_up(daytona, "sb-1", FILES, PROJECT_DIR, port=4321)

    dev = next(c for c, _ in daytona.commands if "npm run dev" in c)
    assert "--host 0.0.0.0" in dev
    assert "--port 4321" in dev


async def test_the_dev_server_is_backgrounded_with_a_log() -> None:
    """`execute_command` waits for the process to exit and a dev server does not.
    The log file is the only record of why it died when it dies later."""
    daytona = _FakeDaytona()

    await scaffold.bring_up(daytona, "sb-1", FILES, PROJECT_DIR)

    dev = next(c for c, _ in daytona.commands if "npm run dev" in c)
    assert dev.rstrip().endswith("echo $!")
    assert "nohup" in dev
    assert ".paw-dev.log" in dev


async def test_running_is_false_when_the_start_command_fails() -> None:
    daytona = _FakeDaytona(failures={"npm run dev": _Exec(127, "npm: not found")})

    result = await scaffold.bring_up(daytona, "sb-1", FILES, PROJECT_DIR)

    assert result.running is False
    assert result.failed_step.name == "dev-server"


async def test_a_transport_failure_is_a_failed_step_not_a_crash() -> None:
    """The VM going away mid-bring-up is a normal Tuesday. It must read as a
    failed stage with the reason, not an unhandled exception."""
    daytona = _FakeDaytona()
    daytona.exec_error = RuntimeError("sandbox stopped")

    result = await scaffold.bring_up(daytona, "sb-1", FILES, PROJECT_DIR)

    assert result.running is False
    assert result.failed_step.exitCode is None
    assert "sandbox stopped" in result.failed_step.output


async def test_install_command_is_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same escape hatch as PAW_SITES_GEN_CMD — an image with a different package
    manager should not need a code change."""
    monkeypatch.setenv("PAW_SCAFFOLD_INSTALL_CMD", "pnpm install --frozen-lockfile")
    daytona = _FakeDaytona()

    await scaffold.bring_up(daytona, "sb-1", FILES, PROJECT_DIR)

    assert any("pnpm install --frozen-lockfile" in c for c, _ in daytona.commands)
