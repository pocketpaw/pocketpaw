# tests/ee/sites/faults.py — reusable FAULT INJECTORS for the sites build + deploy lane.
#
# Created 2026-08-10 (SG-7, the fault ladder).
#
# WHY THIS IS A MODULE AND NOT A FIXTURE FILE OR A BLOCK INSIDE ONE TEST. The two faults
# the rest of the program keeps needing — "Daytona is unavailable" and "the deploy
# answered 5xx" — are each injected at a boundary that took reading three modules to
# locate. A sibling task re-deriving those patch points is not just slow, it is how two
# tests end up injecting at DIFFERENT depths and disagreeing about what the lane does.
# Import from here instead; if a patch point moves, it moves once.
#
# Deliberately a plain importable module (``from tests.ee.sites.faults import ...``,
# which the tree already does in test_pipeline_regression_gate.py) rather than fixtures
# in conftest.py. Fixtures are autouse-shaped and per-test; these are constructors a test
# picks up when it wants one, and several tests need TWO of them in one call.
#
# WHAT EACH INJECTOR PROMISES: it fails at a real seam the production code actually
# calls, with the exception type that seam actually raises. Where the real exception
# comes from a third-party SDK whose type is not part of our contract (Daytona's 5xx),
# the injector raises a documented stand-in and says so — every caller in this lane
# treats sandbox exceptions uniformly, so the type is not what is under test.

from __future__ import annotations

import io
import json
import tarfile
from dataclasses import dataclass, field
from typing import Any

from pocketpaw_ee.sites import daytona_build as db

# ---------------------------------------------------------------------------
# Sentinels — the evidence a build writes to prove it completed
# ---------------------------------------------------------------------------

#: Exit codes that must classify as infrastructure, never as the user's broken build.
#: Re-exported from the module under test on purpose: hard-coding 137 here would let a
#: rename in production drift past these tests silently.
EXIT_TIMEOUT = 124
EXIT_SIGKILL = 137
EXIT_SIGTERM = 143


def ok_sentinel(**over: Any) -> dict[str, Any]:
    """A sentinel describing a clean react build. ``over`` mutates any field.

    The base case is deliberately the HAPPY one so every degraded sentinel in the suite
    reads as a one-field delta from success — which is what the classifier's callers care
    about, and it keeps a test's intent visible on one line
    (``ok_sentinel(build_exit=EXIT_SIGKILL)``).
    """
    base = {
        "schema": db.SENTINEL_SCHEMA,
        "engine": "react",
        "install_exit": 0,
        "build_exit": 0,
        "artifact_rel": "dist",
        # The promised size MUST agree with what the fake actually hands back, because
        # ``verify_artifact`` compares the two (2026-08-11): a sentinel promising 4096 bytes
        # beside a ~170-byte artifact is a TRUNCATED transfer, so a hardcoded number here
        # would make every happy-path test in the ladder fail for the wrong reason. It
        # started as a truthfulness fix while nothing compared them; it is now load-bearing.
        "artifact_bytes": len(clean_artifact()),
        "stderr_tail": "",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Artifacts — real gzip tars, so a verification gate is tested against real bytes
# ---------------------------------------------------------------------------


def tar_bytes(members: dict[str, bytes]) -> bytes:
    """Pack ``{arcname: contents}`` into a real ``.tar.gz``.

    A real tar rather than a sentinel string because the thing under test reads the
    archive index. A fake byte-blob would prove only that the gate rejects garbage, which
    is the easy half; the interesting half is that it accepts a well-formed artifact and
    rejects a well-formed one carrying the wrong member.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, contents in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(contents)
            tar.addfile(info, io.BytesIO(contents))
    return buf.getvalue()


def clean_artifact() -> bytes:
    """A static output tree as the include-list tar would produce it."""
    return tar_bytes(
        {
            "./index.html": b"<!doctype html><title>hi</title>",
            "./assets/app.js": b"console.log(1)",
        }
    )


def artifact_with_node_modules() -> bytes:
    """A well-formed artifact that nonetheless carries ``node_modules``.

    The shape a leaked exclusion would produce: ``dist/node_modules/`` becomes
    ``./node_modules/`` once packed with ``-C dist``. It cannot be produced by putting a
    tree on disk and running the real command — that is what the include-list tests do, and
    they now pass — so a hand-built tar is the only way to exercise what the byte gate does
    when the exclusion does NOT hold, which is the case it exists for.

    Deliberately a VALID tar: rejecting garbage is the easy half, and the interesting half
    is rejecting something that opens cleanly.
    """
    return tar_bytes(
        {
            "./index.html": b"<!doctype html>",
            "./node_modules/.bin/vite": b"#!/usr/bin/env node",
            "./node_modules/react/index.js": b"module.exports = {}",
        }
    )


def truncated_artifact() -> bytes:
    """The first 40 bytes of a healthy artifact — a transfer that died part-way."""
    return clean_artifact()[:40]


def garbage_artifact(size: int) -> bytes:
    """``size`` bytes that are not a tar at all.

    Used with a sentinel promising exactly ``size``, so the FULL promised payload arrives
    and is still unreadable. That combination is what distinguishes "the build produced
    garbage" (not retryable) from "the transfer lost bytes" (retryable), and it is the only
    way to exercise the non-retryable branch.
    """
    return b"x" * size


# ---------------------------------------------------------------------------
# F5 — exercising the include-list itself, with a real tar
# ---------------------------------------------------------------------------
#
# WHY A REAL TAR AND NOT A HAND-BUILT ONE. The property under test is that
# ``artifact_tar_command``'s include-list CANNOT pick up ``node_modules`` — and that is a
# property of the COMMAND, not of any bytes we could assemble ourselves. Packing a fake
# artifact and scanning it would test the scanner. Putting node_modules on disk and
# running the real command is the only way the construction is the thing being exercised,
# and it fails loudly if anyone ever regresses the include-list into an exclude-list.
#
# The fake Daytona client cannot do this job: it records ``execute_command`` and never
# runs the wrapper, so no tar ever executes inside it.

#: A project tree with ``node_modules`` in BOTH places it can occur: as a sibling of the
#: build output (what ``bun install`` actually produces) and nested INSIDE the output dir
#: (what a build that copies dependencies into ``dist`` would produce). The two are not
#: the same test — the include-list excludes the first by construction and, as SG-7
#: measured, does NOT exclude the second.
NODE_MODULES_PROJECT: dict[str, bytes] = {
    "dist/index.html": b"<!doctype html><title>hi</title>",
    "dist/assets/app.js": b"console.log(1)",
    "node_modules/react/index.js": b"module.exports = {}",
    "node_modules/.bin/vite": b"#!/usr/bin/env node",
}


def write_project_tree(root: Any, files: dict[str, bytes]) -> str:
    """Materialise ``{relpath: contents}`` under ``root``; return a POSIX-style path.

    Forward slashes on purpose. ``artifact_tar_command`` renders a POSIX shell command
    with ``shlex.quote``, and a Windows path full of backslashes would either be quoted
    into something tar cannot open or have its separators eaten as escapes. The real
    sandbox path is POSIX anyway, so this keeps the command under test identical to the
    one production renders.
    """
    from pathlib import Path

    base = Path(root)
    for rel, contents in files.items():
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
    return str(base).replace("\\", "/")


def pack_with_real_tar(engine: str, project_dir: str, dest_path: str) -> list[str]:
    """Run ``artifact_tar_command`` for real and return the member names it packed.

    Executes the command with the actual ``tar`` binary rather than through a shell, so
    the test does not depend on which ``bash`` a Windows Python resolves (``/tmp`` means
    different things to git-bash and WSL, which is a real trap here). ``shlex.split`` is
    faithful because the command is rendered with ``shlex.quote`` in the first place.

    Raises ``RuntimeError`` when tar fails, rather than returning an empty list — an
    empty member list is exactly what a passing "no node_modules" assertion looks like,
    so a silent tar failure would turn this into a test that cannot fail.
    """
    import shlex
    import subprocess
    import tarfile

    from pocketpaw_ee.sites.daytona_build import artifact_tar_command

    command = artifact_tar_command(engine, project_dir, dest_path)
    proc = subprocess.run(shlex.split(command), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"tar failed ({proc.returncode}): {proc.stderr.strip()}")
    with tarfile.open(dest_path) as tar:
        return sorted(tar.getnames())


def tar_is_available() -> bool:
    """Whether a real ``tar`` exists to run. The include-list tests skip without one
    rather than silently degrading into an assertion about nothing."""
    import shutil

    return shutil.which("tar") is not None


# ---------------------------------------------------------------------------
# F1 / F2 — Daytona faults
# ---------------------------------------------------------------------------


class DaytonaUnavailable(RuntimeError):
    """Stand-in for what the Daytona SDK raises on a 5xx or a create failure.

    The SDK's own exception type is not part of this lane's contract: ``run_build``
    catches ``Exception`` at the exec seam and lets create/upload failures propagate, so
    every caller is type-agnostic by design. A named local exception makes a test's
    intent legible and carries ``status`` for the cases that want to assert on it.
    """

    def __init__(self, message: str = "daytona unavailable", *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def daytona_unconfigured(monkeypatch: Any) -> None:
    """Make Daytona genuinely unconfigured for the duration of a test (F1).

    Injects at the ENV, which is the real boundary: ``config.daytona_enabled()`` reads
    ``DAYTONA_API_URL`` / ``DAYTONA_API_KEY`` on every call, and
    ``client.get_daytona_client()`` returns ``None`` when it is False. Patching
    ``get_daytona_client`` directly would test the mock instead — and would miss the
    cached singleton, which is why that gets cleared too: a client built by an earlier
    test in the same process would otherwise be handed out to a caller that must see
    "unavailable".
    """
    from pocketpaw_ee.cloud.daytona import client as client_mod

    monkeypatch.delenv("DAYTONA_API_URL", raising=False)
    monkeypatch.delenv("DAYTONA_API_KEY", raising=False)
    monkeypatch.setattr(client_mod, "_client", None, raising=False)


@dataclass
class _SandboxInfo:
    id: str


class _Unset:
    """Distinguishes "the caller said nothing" from "the caller said None".

    Needed because ``None`` is MEANINGFUL for both of this fake's payloads: a sentinel of
    ``None`` is the central fault of the whole lane (no proof of completion), and an
    artifact of ``None`` is an empty download. Defaulting those parameters to ``None``
    would make the most important fault in the ladder unexpressible — and, worse, would
    silently hand back a healthy build instead, so the test would pass while injecting
    nothing.
    """


_UNSET = _Unset()


class FaultyDaytonaClient:
    """A Daytona client fake that fails at ONE chosen phase.

    Phases, in the order ``run_build`` calls them: ``create``, ``wait``, ``upload``,
    ``exec``, ``sentinel``, ``artifact``, ``delete``. ``fail_at=None`` is the happy path.

    ``fail_times`` exists for a caller that RETRIES: the phase fails that many times and
    then succeeds, so a retry-with-backoff test can prove it recovered on attempt N
    rather than merely that it survived. Nothing in the lane retries today (see this
    task's report), so the default is "fail every time" — the honest default, since a
    helper that quietly healed on attempt two would make a non-existent retry look real.

    Records every call in ``calls`` so a test can assert ORDER, which is most of what
    this lane's contract actually is.
    """

    def __init__(
        self,
        *,
        fail_at: str | None = None,
        error: BaseException | None = None,
        fail_times: int | None = None,
        sentinel: dict[str, Any] | None | _Unset = _UNSET,
        artifact: bytes | None | _Unset = _UNSET,
    ) -> None:
        self.fail_at = fail_at
        self.error = error or DaytonaUnavailable(status=503)
        self.fail_times = fail_times
        self.calls: list[str] = []
        self.create_kwargs: dict[str, Any] = {}
        self.exec_timeout: int | None = None
        self.uploaded: list[tuple[Any, str]] = []
        self._failures: dict[str, int] = {}
        # ``sentinel=None`` means the build left NO proof; omitting it means a healthy
        # one. Same split for the artifact, where None means the download came back
        # empty. See ``_Unset``.
        self._sentinel = ok_sentinel() if isinstance(sentinel, _Unset) else sentinel
        self._artifact = clean_artifact() if isinstance(artifact, _Unset) else artifact

    # -- internals ---------------------------------------------------------

    def _maybe_fail(self, phase: str) -> None:
        """Raise if ``phase`` is the injected one and its failure budget is unspent."""
        if phase != self.fail_at:
            return
        seen = self._failures.get(phase, 0)
        if self.fail_times is not None and seen >= self.fail_times:
            return
        self._failures[phase] = seen + 1
        raise self.error

    @property
    def failures(self) -> dict[str, int]:
        """How many times each phase actually failed. A test asserting "it retried
        three times" needs this, not a call count — ``calls`` also grows on success."""
        return dict(self._failures)

    # -- the client surface run_build uses ---------------------------------

    async def create_sandbox(self, **kwargs: Any) -> _SandboxInfo:
        self.calls.append("create")
        self.create_kwargs = kwargs
        self._maybe_fail("create")
        return _SandboxInfo(id="sb-fault")

    async def wait_for_sandbox(self, sandbox_id: str, target_state: str = "started") -> None:
        self.calls.append("wait")
        self._maybe_fail("wait")

    async def bulk_upload(self, sandbox_id: str, files: list[tuple[Any, str]]) -> None:
        self.calls.append("upload")
        self.uploaded = files
        self._maybe_fail("upload")

    async def execute_command(self, sandbox_id: str, command: str, timeout: int = 30) -> Any:
        self.calls.append("exec")
        self.exec_timeout = timeout
        self._maybe_fail("exec")
        return object()

    async def download_file(self, sandbox_id: str, remote_path: str) -> bytes:
        if remote_path.endswith(db.BUILD_RESULT_FILENAME):
            self.calls.append("read_sentinel")
            self._maybe_fail("sentinel")
            if self._sentinel is None:
                raise FileNotFoundError(remote_path)
            return json.dumps(self._sentinel).encode()
        self.calls.append("download_artifact")
        self._maybe_fail("artifact")
        return b"" if self._artifact is None else self._artifact

    async def delete_sandbox(self, sandbox_id: str) -> None:
        self.calls.append("delete")
        self._maybe_fail("delete")


def sandbox_create_fails(**kw: Any) -> FaultyDaytonaClient:
    """Daytona answers 5xx to ``create_sandbox`` — nothing has run yet (F2)."""
    return FaultyDaytonaClient(fail_at="create", **kw)


def sandbox_dies_mid_build(**kw: Any) -> FaultyDaytonaClient:
    """The container goes away during the build: the exec raises AND no sentinel
    survives. The pair matters — an exec failure with a readable sentinel is a build
    failure, and only the absence of evidence makes it infrastructure loss."""
    kw.setdefault("sentinel", None)
    return FaultyDaytonaClient(fail_at="exec", **kw)


# ---------------------------------------------------------------------------
# F4 — deploy faults
# ---------------------------------------------------------------------------


def cloudflare_error(status: int = 503) -> BaseException:
    """The exception the REAL Cloudflare client raises for a non-2xx.

    ``cloudflare_client._unwrap`` maps any non-2xx to
    ``ValidationError("sites.cloudflare_error", f"Cloudflare API {status}")`` — including
    5xx, which is worth noticing: a Cloudflare outage surfaces through the lane as a
    VALIDATION error. Reproducing the real type here rather than raising a generic
    ``RuntimeError`` is the difference between testing the publish path's handling and
    testing an exception nothing throws.
    """
    from pocketpaw_ee.cloud._core.errors import ValidationError

    return ValidationError("sites.cloudflare_error", f"Cloudflare API {status}")


@dataclass
class FailingCloudflare:
    """A CF client whose ``put_worker`` always fails (the WfP deploy target).

    Counts attempts, so a test can state plainly whether the lane retried. It does not
    today; the count is what makes that a measurement instead of a belief.
    """

    status: int = 503
    attempts: int = 0
    put_calls: list[str] = field(default_factory=list)

    async def put_worker(
        self, *, script_name: str, bundle: bytes, bindings: Any | None = None
    ) -> bool:
        self.attempts += 1
        self.put_calls.append(script_name)
        raise cloudflare_error(self.status)


@dataclass
class FailingWorkersDeploy:
    """The ``workers`` deploy target failing — the ``_workers_deploy`` publish seam.

    Kept separate from :class:`FailingCloudflare` because they are different targets on
    different code paths (``deploy_workers`` vs ``cf.put_worker``), and a rung proven on
    one is not proven on the other.
    """

    status: int = 503
    attempts: int = 0

    async def __call__(self, site_id: str, project_dir: str, **kw: Any) -> str:
        self.attempts += 1
        raise cloudflare_error(self.status)


# ---------------------------------------------------------------------------
# F6 — screenshot faults
# ---------------------------------------------------------------------------


def screenshot_always_fails(monkeypatch: Any) -> dict[str, int]:
    """Make the post-deploy screenshot raise, and count the attempts (F6).

    Patched on ``sites.screenshot`` rather than on the service, because the service
    imports the function INSIDE ``_schedule_site_screenshot`` — so a module-level patch of
    the service attribute would never be consulted. Returns a counter dict a test asserts
    on; a rung claiming "the screenshot failed and the publish survived" is worthless
    unless the screenshot was actually reached.
    """
    from pocketpaw_ee.sites import screenshot as screenshot_mod

    counter = {"attempts": 0}

    def _boom(*_args: Any, **_kw: Any) -> None:
        counter["attempts"] += 1
        raise RuntimeError("browser rendering quota exceeded")

    monkeypatch.setattr(screenshot_mod, "schedule_site_screenshot", _boom, raising=False)
    monkeypatch.setattr(screenshot_mod, "schedule_draft_screenshot", _boom, raising=False)
    return counter


__all__ = [
    "EXIT_SIGKILL",
    "EXIT_SIGTERM",
    "EXIT_TIMEOUT",
    "NODE_MODULES_PROJECT",
    "DaytonaUnavailable",
    "FailingCloudflare",
    "FailingWorkersDeploy",
    "FaultyDaytonaClient",
    "artifact_with_node_modules",
    "clean_artifact",
    "cloudflare_error",
    "daytona_unconfigured",
    "garbage_artifact",
    "ok_sentinel",
    "pack_with_real_tar",
    "sandbox_create_fails",
    "sandbox_dies_mid_build",
    "screenshot_always_fails",
    "tar_bytes",
    "tar_is_available",
    "truncated_artifact",
    "write_project_tree",
]
