# scaffold.py — Materialize a composed source map into a VM and bring it up (CS-2).
#
# Created 2026-07-22 (feat/codescaffold-daytona). Takes the `{path: contents}`
# map `codescaffold.compose` produced and turns it into a running dev server.
#
# ── Why this lives in websandbox, not codescaffold ──────────────────────────
# An import-linter contract forbids `codescaffold` from importing `daytona` or
# `websandbox`, and this is the file that makes that contract mean something.
# Composition emits a source map and stops; MATERIALIZING one is runtime work, so
# it belongs on the runtime side. The direction of the dependency is the design:
# websandbox knows about source maps, codescaffold knows nothing about VMs, and a
# WebContainer materializes the same map in CS-3 with none of this code.
#
# ── The vehicle ─────────────────────────────────────────────────────────────
# One in-memory tarball, `upload_bytes`, one `tar -xzf`. Identical to
# `broker.clone_into_vm` and `durability.restore_workspace` — the file-RPC would
# need a round trip per file, and a composed project is ~50 of them.
#
# ── Bring-up reports steps, not a boolean ───────────────────────────────────
# CS-2's stated acceptance is that a failed `npm install` shows a VISIBLE FAILED
# STATE, not a spinner. So every stage returns its exit code and the tail of its
# output, and the caller renders them. A dependency install failing on a fresh
# project is not an exotic case — it is Tuesday — and "still working…" forever is
# the worst possible answer.
from __future__ import annotations

import io
import logging
import os
import posixpath
import tarfile
import time
from dataclasses import dataclass, field

from pocketpaw_ee.cloud._core.errors import CloudError, with_cause
from pocketpaw_ee.cloud.daytona.client import DaytonaClient

logger = logging.getLogger(__name__)

# Where the tarball lands in the VM before extraction. Same convention as the
# broker's staging path; removed immediately after extraction.
_STAGING_TAR = "/tmp/paw-scaffold.tar.gz"  # noqa: S108 — a path in the VM, not this host

# Ceiling on the packed project. A composed project is ~220 KB; this is three
# orders of magnitude of headroom and exists only to bound a pathological map.
MAX_PACKED_BYTES = 64 * 1024 * 1024

# Individual step budgets, in seconds. `install` is the outlier by a wide margin:
# a cold npm install of the SvelteKit + Cloudflare toolchain pulls several
# hundred packages and compiles nothing, but the registry round trips dominate.
INSTALL_TIMEOUT = 600
MIGRATE_TIMEOUT = 120
QUICK_TIMEOUT = 60

#: Default dev-server port. Vite's own default, so a user who reads the template
#: README and expects 5173 is not surprised.
DEFAULT_DEV_PORT = 5173

#: How much of a failing command's output to carry back. Enough to show the npm
#: error block that actually names the problem, bounded so a 50k-line log does
#: not become a response body.
MAX_OUTPUT_TAIL = 4000


@dataclass
class Step:
    """One stage of bring-up, with the evidence of how it went."""

    name: str
    ok: bool
    exitCode: int | None = None
    #: Tail of combined output. Empty on success — nobody reads a successful
    #: install log, and carrying it would dwarf the rest of the response.
    output: str = ""
    durationMs: int = 0


@dataclass
class BringUp:
    """The outcome of materializing and starting a project."""

    steps: list[Step] = field(default_factory=list)
    running: bool = False
    port: int = DEFAULT_DEV_PORT

    @property
    def failed_step(self) -> Step | None:
        return next((s for s in self.steps if not s.ok), None)


# ── Packing ─────────────────────────────────────────────────────────────────


def _safe_member_path(path: str) -> str:
    """Validate one source-map key and return it in POSIX form.

    The map comes from our own engine, so this is defence in depth rather than a
    boundary — but it is a tarball being extracted with `tar -xzf` in a VM, and
    a `..` component would write outside the project directory. The cost of
    checking is nothing and the cost of not checking is arbitrary file write.
    """
    normalized = path.replace("\\", "/").strip()
    if not normalized:
        raise CloudError(400, "websandbox.scaffold_bad_path", "Empty path in the source map")
    if normalized.startswith("/") or ":" in normalized.split("/")[0]:
        raise CloudError(
            400, "websandbox.scaffold_bad_path", f"Absolute path in the source map: {path}"
        )
    parts = normalized.split("/")
    if ".." in parts:
        raise CloudError(400, "websandbox.scaffold_bad_path", f"Path escapes the project: {path}")
    return posixpath.normpath(normalized)


def pack_source_map(files: dict[str, str]) -> bytes:
    """Tar+gzip a source map in memory.

    Deterministic on purpose: entries are emitted in sorted order with a fixed
    mtime and fixed ownership, so composing the same project twice produces the
    same bytes. That makes a scaffold reproducible from a bug report, and it lets
    a future caller cache on the digest instead of the inputs.
    """
    if not files:
        raise CloudError(400, "websandbox.scaffold_empty", "The project has no files")

    buffer = io.BytesIO()
    # mtime=0 rather than "now" — a timestamp is the one thing that would make
    # two identical projects produce different bytes.
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path in sorted(files):
            data = files[path].encode("utf-8")
            info = tarfile.TarInfo(_safe_member_path(path))
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))

    packed = buffer.getvalue()
    if len(packed) > MAX_PACKED_BYTES:
        raise CloudError(
            413,
            "websandbox.scaffold_too_large",
            f"The composed project is {len(packed) / 1024 / 1024:.1f} MB packed, "
            f"over the {MAX_PACKED_BYTES / 1024 / 1024:.0f} MB limit",
        )
    return packed


# ── Running commands ────────────────────────────────────────────────────────


def _tail(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT_TAIL else "…\n" + text[-MAX_OUTPUT_TAIL:]


async def _step(
    daytona: DaytonaClient,
    sandbox_id: str,
    name: str,
    command: str,
    *,
    cwd: str,
    timeout: int,
) -> Step:
    """Run one command and record how it went. NEVER raises.

    A raising step would abort bring-up and lose the record of the stages that
    DID succeed — which is exactly the information needed to explain the failure.
    The caller stops on the first `ok=False` and returns everything so far.
    """
    started = time.monotonic()
    try:
        result = await daytona.execute_command(sandbox_id, command, cwd=cwd, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — a transport failure is a failed step, not a crash
        logger.warning("scaffold: step %r failed to execute", name, exc_info=True)
        return Step(
            name=name,
            ok=False,
            exitCode=None,
            output=_tail(str(exc)),
            durationMs=int((time.monotonic() - started) * 1000),
        )

    # The SDK's ExecuteResponse exposes `exit_code` and `result`; both are read
    # defensively because a transport that returns neither must not read as
    # success. `None` exit code is treated as failure for the same reason.
    exit_code = getattr(result, "exit_code", None)
    output = str(getattr(result, "result", "") or "")
    ok = exit_code == 0
    return Step(
        name=name,
        ok=ok,
        exitCode=exit_code,
        # Successful output is dropped: nobody reads a clean install log, and it
        # would dwarf everything else in the response.
        output="" if ok else _tail(output),
        durationMs=int((time.monotonic() - started) * 1000),
    )


def _install_command() -> str:
    """The dependency install.

    `npm install` rather than pnpm: the composed project ships no lockfile (the
    engine's own ignore list excludes it), so there is nothing for a
    frozen-lockfile install to honour, and npm is present in the image already.
    Override with PAW_SCAFFOLD_INSTALL_CMD.
    """
    return os.environ.get("PAW_SCAFFOLD_INSTALL_CMD", "npm install --no-audit --no-fund")


async def materialize(
    daytona: DaytonaClient,
    sandbox_id: str,
    files: dict[str, str],
    project_dir: str,
) -> Step:
    """Ship a composed source map into the VM.

    Extracted OVER `project_dir` rather than into a fresh one: the sandbox is
    already provisioned with a workspace directory, and the tarball's members are
    all relative, so this fills it rather than nesting a project inside it.
    """
    started = time.monotonic()
    packed = pack_source_map(files)

    try:
        await daytona.upload_bytes(sandbox_id, packed, _STAGING_TAR)
    except Exception as exc:  # noqa: BLE001 — uniform failure surface
        raise with_cause(
            CloudError(
                502,
                "websandbox.scaffold_upload_failed",
                "Could not copy the project into the workspace",
            ),
            exc,
        ) from exc

    extract = (
        f"mkdir -p {project_dir} && tar -xzf {_STAGING_TAR} -C {project_dir} "
        f"&& rm -f {_STAGING_TAR}"
    )
    step = await _step(
        daytona,
        sandbox_id,
        "materialize",
        extract,
        cwd="/",
        timeout=QUICK_TIMEOUT,
    )
    step.durationMs = int((time.monotonic() - started) * 1000)
    logger.info(
        "scaffold.materialize: sandbox=%s files=%d packed=%d ok=%s",
        sandbox_id,
        len(files),
        len(packed),
        step.ok,
    )
    return step


def _dev_command(project_dir: str, port: int) -> str:
    """Start the dev server detached, with its output on disk.

    Three things are load-bearing here:
      * `--host 0.0.0.0` — Vite binds loopback by default, and Daytona's preview
        URL reaches the VM from outside. Without this the server runs perfectly
        and the preview pane shows nothing, which is the worst kind of working.
      * `nohup … &` — `execute_command` waits for the process to exit, and a dev
        server does not exit. Backgrounding is what makes this return.
      * the log file — when the server dies thirty seconds later, this is the
        only record of why. `bring_up` cannot wait around to find out.
    """
    log = posixpath.join(project_dir, ".paw-dev.log")
    return f"nohup npm run dev -- --host 0.0.0.0 --port {port} > {log} 2>&1 < /dev/null & echo $!"


async def bring_up(
    daytona: DaytonaClient,
    sandbox_id: str,
    files: dict[str, str],
    project_dir: str,
    *,
    port: int = DEFAULT_DEV_PORT,
    run_migrations: bool = True,
) -> BringUp:
    """Materialize a composed project and start its dev server.

    Stops at the FIRST failing step and returns everything up to it. The caller
    renders the steps, so a failed install shows as a failed install — with npm's
    own error text — rather than a preview pane that never loads.
    """
    result = BringUp(port=port)

    result.steps.append(await materialize(daytona, sandbox_id, files, project_dir))
    if not result.steps[-1].ok:
        return result

    result.steps.append(
        await _step(
            daytona,
            sandbox_id,
            "install",
            _install_command(),
            cwd=project_dir,
            timeout=INSTALL_TIMEOUT,
        )
    )
    if not result.steps[-1].ok:
        return result

    # Local D1 migrations. Only when the project actually has some — the base
    # ships 0001, but a caller composing a bare template should not fail on a
    # missing migrations directory.
    if run_migrations and any(p.startswith("migrations/") for p in files):
        result.steps.append(
            await _step(
                daytona,
                sandbox_id,
                "migrate",
                "npm run db:migrate:local",
                cwd=project_dir,
                timeout=MIGRATE_TIMEOUT,
            )
        )
        if not result.steps[-1].ok:
            return result

    result.steps.append(
        await _step(
            daytona,
            sandbox_id,
            "dev-server",
            _dev_command(project_dir, port),
            cwd=project_dir,
            timeout=QUICK_TIMEOUT,
        )
    )
    # "running" means the START command returned cleanly, NOT that the server is
    # serving — it was backgrounded, so nothing here could know that yet. The
    # preview pane fetching the port is what actually proves it, and naming the
    # field honestly keeps this from being read as a promise.
    result.running = result.steps[-1].ok
    return result


__all__ = [
    "DEFAULT_DEV_PORT",
    "MAX_PACKED_BYTES",
    "BringUp",
    "Step",
    "bring_up",
    "materialize",
    "pack_source_map",
]
