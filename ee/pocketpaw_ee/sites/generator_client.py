# ee/pocketpaw_ee/sites/generator_client.py — Python bridge to the Node/Bun
# generator (paw-sites-gen). build() runs the generate CLI, then `bun install`
# on the generated project, then `bun run build` to emit the deployable
# `.svelte-kit/cloudflare/` static output (with the data-paw-section anchors +
# the injected edit-bridge for an editable site); a LIVE publish additionally
# fail-gates on the workerd SSR render markers. If the gate fails the site does
# NOT proceed to deploy (Contract clause 4). The subprocess calls are isolated
# behind a _runner so the orchestration is unit-testable without Bun/workerd.
# Created: 2026-05-30 (feat/paw-sites-backend, Task 2.3).
#
# Updated 2026-07-09 (fix/sites-gen-windows-process-kill): ``_kill_process_group``
# was POSIX-only — it unconditionally called ``os.killpg(os.getpgid(pid), SIGKILL)``,
# neither of which exists on Windows. On a Windows host a build TIMEOUT therefore
# crashed inside ``_communicate_bounded``'s timeout handler with ``AttributeError:
# module 'os' has no attribute 'killpg'``, masking the real ``_BuildTimeout`` and
# escaping ``publish_pocket`` as an unhandled 500. ``_kill_process_group`` now
# branches on ``sys.platform``: POSIX keeps the process-group SIGKILL; Windows calls
# the new ``_kill_process_tree_windows`` (``taskkill /F /T`` to reap the leaked
# workerd child TREE, best-effort ``proc.kill()`` fallback if taskkill is missing).
#
# Updated 2026-07-02 (harden apply_leaf_edits temp-file cleanup): ``apply_leaf_edits``
# now assigns ``input_path = fh.name`` BEFORE ``json.dump`` inside the ``with`` and
# wraps creation + write + exec in ONE guarded try/finally, so a serialization
# failure mid-``json.dump`` no longer leaves ``input_path`` unbound (NameError in the
# cleanup) nor leaks the delete=False tempfile — the finally only unlinks a path that
# was assigned and still exists.
#
# Updated 2026-07-01 (NE-4b — native-editing leaf-edit bridge): added the
# module-level ``apply_leaf_edits(source, edits, *, _exec=None)`` coroutine — the
# Python bridge to the paw-sites ``apply-leaf-edit`` CLI (NE-4a). It shells out to
# the SAME tokenised generator command (``_gen_cmd_argv()``) but with the
# ``apply-leaf-edit`` subcommand: it writes ``{source, edits}`` to a tempfile
# ``--input`` and parses the single JSON stdout line
# ``{source, results:[{uid,applied,reason?}]}``. It reuses the build path's
# ``_communicate_bounded`` timeout guard (a timed-out splice raises RuntimeError,
# mirroring ``generate``); a non-zero exit raises RuntimeError with the CLI stderr.
# Unlike ``build`` it is a PURE transform — no install, no ``bun run build``, no
# workerd — so it is safe to call inline on the NE-4b persist path. ``_exec`` is an
# injectable subprocess-exec seam so the bridge is unit-testable without Bun.
#
# Updated 2026-06-26 (fix/sites-build-subprocess-timeout — stop-gap: bound the build
# subprocesses): all THREE _SubprocessRunner subprocesses (the generator, `bun
# install`, and the `bun run build` static build) ran a bare ``await
# proc.communicate()`` with NO timeout. When adapter-cloudflare's workerd prerender
# wedges (known upstream SvelteKit hang) the `bun run build` step ran forever, so
# /editable and publish hung unbounded (tens of minutes). The existing
# ``reap_build_workerd`` only runs AFTER communicate() returns, so it could not
# rescue a wedged build.
#   * Each subprocess is now launched with ``start_new_session=True`` (its own
#     process group) and run through ``_communicate_bounded(proc, timeout_s, label)``,
#     which ``asyncio.wait_for``-bounds communicate() and, on timeout, SIGKILLs the
#     whole process GROUP (``_kill_process_group`` → ``os.killpg`` — kills the leaked
#     workerd CHILD too, not just the bun parent), reaps the parent, and raises the
#     internal ``_BuildTimeout``.
#   * Timeout is ``PAW_SITES_BUILD_TIMEOUT_SEC`` (int, default 120s — a legit build is
#     ~45-60s, a wedged one runs for minutes, so 120s cleanly separates them); read
#     once per call via ``_build_timeout_sec()`` with an int-parse-or-fallback.
#   * Each call site converts ``_BuildTimeout`` into its EXISTING failure contract
#     (callers unchanged): ``install``/``build_static`` return ``(False, "<step> timed
#     out after Ns ...")`` — the same ``(ok, msg)`` shape the non-zero-exit path
#     returns, so build() raises SmokeGateFailed and the caller maps it to a CloudError
#     (FE degrades to view-only); ``generate`` raises ``RuntimeError`` (its existing
#     raise-on-failure). ``reap_build_workerd`` is kept (success-path + on a build
#     timeout, as defensive straggler cleanup).
#
# Updated 2026-06-21 (DSV-5 — dynamic svelte sites write-side): build() now SPLITS
# a svelte pocket's ``source`` content envelope before sending it to the generator.
# A DYNAMIC svelte pocket stores its live-data bindings
# (``objects``/``sources``/``actions``/``auth``) as SIBLING keys on the same
# ``source`` dict that holds the ``{path: contents}`` SvelteKit files (the contract
# DSV-2b's read assumes). The generator's DSV-1 ``GenerateInput`` expects the
# bindings as FLAT siblings alongside ``source`` (``input.objects`` /
# ``input.sources`` / ``input.actions`` / ``input.auth`` — what ``parseBindings``
# reads), NOT nested inside ``source`` (``materializeSource`` writes every
# ``source`` key to disk as a file, so a binding key mixed in would break the
# build). ``_split_svelte_source`` peels the binding keys out: the file map goes on
# ``input.source`` and the bindings spread as flat siblings. A STATIC svelte pocket
# carries no binding keys, so the split is a no-op and the wire bytes are unchanged.
# ``build``/``_build_one``'s ``source`` param is widened to ``dict[str, Any]`` to
# carry the (list/bool) binding values.
#
# Updated 2026-06-19 (fix/sites-preview-fresh-build, P0a + P1a — the #1 Paw Sites
# bug: the editable PREVIEW served STALE anchorless HTML so the hover edit-pill
# never appeared). ROOT CAUSE: PERF-4 overloaded the ``smoke`` flag — it gated
# ``_runner.smoke()``, the ONLY step that ran ``bun run build`` (the step that
# emits ``.svelte-kit/cloudflare/index.html`` with the section anchors + the
# injected ``id="paw-edit-bridge"``). A preview build (``smoke=False``) re-stamped
# the SOURCE with anchors but SKIPPED ``bun run build``, so ``persist_site`` copied
# whatever the LAST build left on disk — the stale, non-anchored LIVE build.
#   * P0a: SPLIT the two concerns the ``smoke`` flag conflated. ``bun run build``
#     (the static-output step — REQUIRED on any path that is served locally, i.e.
#     every preview that goes through deploy_local) now ALWAYS runs; the SSR
#     FAIL-gate (the actual "smoke" safety check) is the only thing the flag
#     toggles. The runner gained ``build_static(project_dir, gate)``: it always
#     runs ``bun run build`` (a non-zero exit always fails — a build that can't
#     produce output is never servable), and applies the workerd SSR-marker
#     fail-check ONLY when ``gate=True`` (the LIVE publish path). ``_build_one``
#     now ALWAYS calls it (``gate=smoke``) instead of calling ``smoke()`` only on
#     publish, so the preview/editable path produces FRESH anchored output and
#     ``persist_site`` copies it. Reintroduces per-edit build time — acceptable +
#     correct for now (instant-HMR via dev_server is a separate future change).
#   * P1a: REAP the build-path workerd. ``adapter-cloudflare`` prerenders by
#     booting a workerd that the bun build process never reaps (it reparents to
#     PID 1 and lives on), so they pile up and progressively slow the box (~60
#     observed). ``reap_build_workerd(project_dir)`` runs AFTER every static build
#     and terminates any workerd whose executable lives under this build's
#     ``project_dir`` (scoped so it never touches another build's / the dev
#     server's processes). The leak is independent of the smoke flag, so the reap
#     runs on BOTH the gated and ungated static build.
#   * The legacy ``smoke()`` runner method is preserved for the fake runners in the
#     existing generator/cache/smoke-gate unit tests (those fakes do not define
#     ``build_static``); ``_build_one`` falls back to the OLD behaviour (call
#     ``smoke()`` only when ``smoke=True``) for a runner without ``build_static``.
#     The REAL runner defines ``build_static`` and is always on the new path.
#
# Updated 2026-06-18 (feat/sites-smoke-at-publish, PERF-4 — smoke-gate only at
# publish): build() gained a ``smoke: bool = True`` flag. The workerd SMOKE render
# is per-edit overhead only needed before a LIVE deploy, so a PREVIEW/edit/arm
# build now passes ``smoke=False`` to SKIP it (the build runs generate + install
# only and never spawns the render). A LIVE publish keeps the default
# ``smoke=True`` so the gate — and the caller's rollback-on-SmokeGateFailed — is
# unchanged. service.publish() threads ``smoke=not preview`` from its existing
# ``preview`` flag. A preview build that WOULD fail smoke is no longer blocked;
# the live publish still gates + rolls back, so a broken edit can never go live.
#
# Updated 2026-06-18 (feat/sites-cached-build, PERF-3 — persistent build dir +
# cached node_modules): the single biggest per-edit cost was running `bun install`
# on a FRESH throwaway tempfile dir on EVERY build (preview AND publish). build()
# now materializes the generated source into a STABLE per-pocket working dir under
# build_home() (``<build_home>/<pocket_id>/``, configurable via PAW_SITES_BUILD_DIR
# — mirrors local_server.sites_home()), so node_modules persists across builds.
#   * `bun install` runs ONLY when the dependency set changed: build() fingerprints
#     the install inputs (the rewritten package.json + any lockfile) and compares
#     it to a sentinel (``.paw-install-hash``) recorded after the last successful
#     install in that dir. Hash match → SKIP install (reuse cached node_modules);
#     mismatch (or first build) → install, then record the new hash. This keeps
#     correctness: a stale node_modules can never serve old deps because the
#     dep-hash guard forces a reinstall whenever the inputs change.
#   * ``_rewrite_ripple_dep`` is preserved but now applied in build() (before the
#     hash is computed) so the fingerprint reflects the FINAL install inputs; the
#     runner's install() just runs `bun install` on the prepared dir.
#   * The real runner gained install_inputs_hash(project_dir). Fakes that don't
#     define it fall back to ALWAYS installing (legacy behaviour), so existing
#     generator/svelte tests are unaffected.
#   * Concurrent builds of the SAME pocket are serialized by a per-pocket asyncio
#     lock (last-writer wins on the shared dir). Per-process only; per-tenant few
#     editors makes cross-process contention a non-issue in practice. When no
#     pocket_id is passed, build() falls back to the old throwaway tempfile dir
#     (no caching) so legacy callers keep their prior behaviour.
#
# The smoke render stays as-is (PERF-4 gates it to publish-only — NOT here).
#
# Updated 2026-06-17 (feat/sites-svelte-component-edit, SE-2b): build() takes an
# optional ``builder_origin`` and, when set, sends it on
# ``siteConfig.builderOrigin``. The paw-sites generator (SE-1) gates the editable
# section anchors + the postMessage edit-bridge on that field, so a site is
# editable only when it carries a builderOrigin. The key is OMITTED when
# ``builder_origin`` is None, so a normal (non-editable) publish's wire payload is
# byte-identical to before this change.
#
# Updated 2026-06-04 (feat/sites-svelte-engine — Paw Sites "Svelte track"):
#   * FIX: BuildResult.ripple_version is now optional and build() reads it with
#     gen.get("rippleVersion") (was gen["rippleVersion"]). The svelte
#     GenerateResult omits rippleVersion entirely (paw-sites types.ts §4.2 — no
#     ripple runtime ships), so the old subscript raised KeyError on EVERY svelte
#     publish, crashing the chain before deploy. Ripple path is unchanged (it
#     still carries rippleVersion).
#   * build() is now engine-aware. It stamps ``engine`` ("ripple" | "svelte")
#     onto the generator input and forks the STAGE-2 payload on it (design spec
#     §4.2): the ripple path is unchanged except for the new ``engine: "ripple"``
#     tag and still sends ``rippleSpec``; the svelte path sends ``source`` (the
#     pocket's hand-written SvelteKit source map ``{path: contents}``) INSTEAD of
#     ``rippleSpec`` and omits ``rippleSpec`` entirely. ``engine`` defaults to
#     "ripple" so existing callers keep the old behaviour. ``ripple_spec`` is now
#     optional (the svelte path has none).
#   * Install + smoke (stages 1,3-8) stay track-agnostic. ``_rewrite_ripple_dep``
#     is a no-op on the svelte path because the svelte-variant package.json carries
#     no ``@ripple-ui/svelte`` dep (spec §5.1), so the shared install path is safe.
#
# Updated 2026-06-01 (Phase 3 Gap 1 — close the dep-install gap so a generated
# project actually builds in a fresh environment):
#   * The generate command is now overridable via PAW_SITES_GEN_CMD (default
#     "paw-sites-gen"). The value is shell-tokenised, so a dev with no installed
#     bin can point it at the built dist: PAW_SITES_GEN_CMD="node
#     /path/to/paw-sites/dist/cli.js". PROD TODO: publish/install the
#     paw-sites-gen bin on PATH and drop the override.
#   * The build/smoke step now runs `bun install` on the generated project
#     BEFORE `bun run build`. The generated package.json pins
#     "@ripple-ui/svelte": "0.2.0" (unpublished), so before install we rewrite
#     that one dep to a resolvable source read from PAW_SITES_RIPPLE_DEP
#     (default "0.2.0" — the registry version once ripple is published).
#     Locally set it to the tarball: PAW_SITES_RIPPLE_DEP="file:/tmp/
#     ripple-ui-svelte-0.2.0.tgz". PROD TODO: publish @ripple-ui/svelte, pin a
#     real registry version, and drop the tarball shim. The install is part of
#     the publish/build flow — never a manual step.

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shlex
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# Stop-gap timeout that bounds each build subprocess (the generator, `bun install`,
# and the `bun run build` static build) so a wedged step fails FAST instead of
# hanging the request unbounded. The static build runs adapter-cloudflare's workerd
# prerender, which can hang forever on a known upstream SvelteKit bug; without a
# timeout `/editable` and publish hang for tens of minutes. A legit build is
# ~45-60s and a wedged one runs for minutes, so the 120s default cleanly separates
# them. Override with PAW_SITES_BUILD_TIMEOUT_SEC (int seconds). Read once per call
# via _build_timeout_sec() with an int-parse-or-fallback so a malformed value can
# never crash the build.
_DEFAULT_BUILD_TIMEOUT_SEC = 120


def _build_timeout_sec() -> int:
    """Per-subprocess build timeout in seconds (PAW_SITES_BUILD_TIMEOUT_SEC, int,
    default 120). A malformed/empty value falls back to the default rather than
    raising — the timeout is a safety net and must never itself break a build."""
    raw = os.environ.get("PAW_SITES_BUILD_TIMEOUT_SEC")
    if raw is None:
        return _DEFAULT_BUILD_TIMEOUT_SEC
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "sites: ignoring non-int PAW_SITES_BUILD_TIMEOUT_SEC=%r, using default %ds",
            raw,
            _DEFAULT_BUILD_TIMEOUT_SEC,
        )
        return _DEFAULT_BUILD_TIMEOUT_SEC


class _BuildTimeout(RuntimeError):
    """Raised by ``_communicate_bounded`` when a build subprocess exceeds the
    timeout and is killed. Carries the step ``label`` and the elapsed ``timeout_s``
    so each call site can convert it into that step's existing failure contract
    (``install``/``smoke`` → a ``(False, msg)`` tuple; ``generate`` → a
    ``RuntimeError``). Internal — never surfaces to callers raw."""

    def __init__(self, label: str, timeout_s: int) -> None:
        self.label = label
        self.timeout_s = timeout_s
        super().__init__(f"{label} timed out after {timeout_s}s")


def _kill_process_tree_windows(proc: asyncio.subprocess.Process) -> None:
    """Windows has no ``setsid`` process groups, so kill the whole child TREE with
    ``taskkill /F /T`` — ``/T`` recurses into children, reaping the leaked workerd
    child the bun parent never cleans up (the POSIX ``killpg`` equivalent). If
    ``taskkill`` is missing or blocked, degrade to a best-effort parent kill so the
    exception never escapes the timeout handler."""
    pid = proc.pid
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    except Exception:
        # taskkill unavailable / blocked / timed out — best-effort parent kill.
        with contextlib.suppress(Exception):
            proc.kill()


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Kill a launched build subprocess AND its leaked children so a wedged workerd
    prerender dies too, not just the parent.

    ``adapter-cloudflare``'s prerender boots a workerd child that the bun parent
    never reaps; killing only the parent would leave that child wedged. The
    mechanism is platform-specific:

    * **POSIX** — the subprocesses are launched with ``start_new_session=True``, so
      each leads its own process group (pgid == pid); ``os.killpg(os.getpgid(pid),
      SIGKILL)`` takes out the whole group. Guards ``ProcessLookupError`` (already
      exited) and ``PermissionError`` (can't signal) so the best-effort kill never
      raises into the build path.
    * **Windows** — ``start_new_session`` is a no-op, and ``os.killpg`` /
      ``os.getpgid`` don't exist at all (referencing them raised ``AttributeError``
      inside the timeout handler, masking ``_BuildTimeout`` and escaping publish as
      an unhandled 500). Use ``taskkill /F /T`` to kill the child tree instead."""
    pid = proc.pid
    if pid is None:
        return
    if sys.platform == "win32":
        _kill_process_tree_windows(proc)
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # Already dead, or not allowed to signal it — nothing further to do.
        pass


async def _communicate_bounded(
    proc: asyncio.subprocess.Process, timeout_s: int, label: str
) -> tuple[bytes, bytes]:
    """Run ``proc.communicate()`` but bound it by ``timeout_s``. On timeout, kill
    the proc's whole process group (so leaked workerd children die too), reap the
    parent, and raise ``_BuildTimeout(label, timeout_s)``.

    Each build subprocess is launched with ``start_new_session=True`` so it leads
    its own process group; ``_kill_process_group`` SIGKILLs the group. After the
    kill we ``await proc.wait()`` to REAP the parent (avoid a zombie / a lingering
    transport) — suppressing any error there since the proc is being torn down
    anyway. The raised ``_BuildTimeout`` is caught at each call site and converted
    into that step's existing failure contract."""
    try:
        return await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError as exc:  # asyncio.TimeoutError IS builtin TimeoutError (3.11+)
        _kill_process_group(proc)
        with contextlib.suppress(Exception):
            await proc.wait()
        raise _BuildTimeout(label, timeout_s) from exc


# Sentinel file (in the per-pocket build dir) recording the install-input
# fingerprint of the last SUCCESSFUL `bun install`. build() skips install when the
# current fingerprint matches this; mismatch forces a reinstall (PERF-3).
_INSTALL_HASH_FILE = ".paw-install-hash"

# Files whose contents define the dependency set. If any change, node_modules is
# stale and must be reinstalled. The lockfile names cover bun's text + binary
# lockfiles and the npm fallback.
_INSTALL_INPUT_FILES = ("package.json", "bun.lock", "bun.lockb", "package-lock.json")

# Known workerd SSR-render failure markers (mirrors paw-sites/src/smoke.ts). A
# LIVE publish fail-gates on these; a preview build tolerates them (the live
# publish still gates + rolls back, so a broken edit can never go live).
_WORKERD_SSR_MARKERS = ("window is not defined", "document is not defined", "No such module")

# DSV-5: a DYNAMIC svelte pocket stores its live-data bindings as SIBLING keys on
# the same ``source`` content envelope that carries the ``{path: contents}``
# SvelteKit files (DSV-2b reads ``objects`` off exactly this envelope, and the
# publish/promote switch versions the whole ``source`` dict). These are the
# binding keys the generator's DSV-1 ``GenerateInput`` expects as FLAT siblings
# alongside ``source`` (not nested inside it): ``objects`` (the D1 table defs),
# ``sources`` (reads), ``actions`` (writes) — lists — and ``auth`` — a bool.
_SVELTE_BINDING_KEYS = ("objects", "sources", "actions", "auth")


def _split_svelte_source(
    source: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a svelte ``source`` content envelope into ``(files, bindings)``
    (DSV-5).

    A dynamic svelte pocket persists its live-data bindings
    (``objects``/``sources``/``actions``/``auth``) as SIBLING keys on the SAME
    dict that holds the ``{relative_path: file_contents}`` SvelteKit files. The
    generator can't receive them mixed together: ``materializeSource`` writes
    EVERY ``source`` key to disk as a file (``writeFile(path, contents)`` keyed on
    the key), so a binding key like ``objects`` (a list value) would be treated as
    a file path and break the build. DSV-1's ``parseBindings`` instead reads the
    bindings as FLAT siblings on ``GenerateInput`` (``input.objects`` /
    ``input.sources`` / ``input.actions`` / ``input.auth``), NOT nested under
    ``input.source``.

    So this peels the binding keys OUT of the envelope: it returns the file map
    (everything that is NOT a recognized binding key — written to ``input.source``)
    and the bindings dict (only the recognized keys that are present — spread as
    flat siblings on ``GenerateInput``). A STATIC svelte pocket carries no binding
    keys, so ``bindings`` is empty and ``files`` is the unchanged source map — the
    pre-DSV-5 wire bytes are preserved exactly."""
    src = source or {}
    files = {k: v for k, v in src.items() if k not in _SVELTE_BINDING_KEYS}
    bindings = {k: src[k] for k in _SVELTE_BINDING_KEYS if k in src}
    return files, bindings


def reap_build_workerd(project_dir: str) -> int:
    """Terminate any orphaned ``workerd`` process spawned by THIS build's
    ``bun run build`` (P1a). Returns the count reaped.

    ``adapter-cloudflare`` prerenders by booting a workerd that ``bun run build``
    does NOT reap: when the build process exits the workerd reparents to PID 1 and
    lives on, so on a per-edit rebuild loop they pile up and progressively slow the
    box (the reaper that already exists only covers ``dev_server``'s ``vite dev``).

    The reap is SCOPED to this build's ``project_dir``: each generated project
    installs its own ``@cloudflare/workerd-*`` binary under
    ``<project_dir>/node_modules/...``, and a prerender workerd runs THAT binary, so
    a process whose executable path is under ``project_dir`` is a leftover from this
    build — never another build's, never the dev server's. We match on the resolved
    project dir so the same pocket's persistent build dir (PERF-3) is handled too.

    Best-effort + never raises: a reap failure must not fail the build. If
    ``psutil`` is unavailable the reap is a logged no-op (the build still succeeds;
    the leak just isn't swept on that box)."""
    try:
        import psutil
    except Exception:  # noqa: BLE001 — reaping is best-effort; never fail the build
        logger.debug("sites: psutil unavailable, skipping build-workerd reap")
        return 0

    try:
        root = str(Path(project_dir).resolve())
    except Exception:  # noqa: BLE001
        root = project_dir
    victims = []
    for proc in psutil.process_iter(["name", "exe"]):
        try:
            name = (proc.info.get("name") or "").lower()
            exe = proc.info.get("exe") or ""
            # A prerender workerd for this build runs the workerd binary that lives
            # under this build's node_modules — match on the resolved project dir.
            if "workerd" in name and exe and root in exe:
                victims.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:  # noqa: BLE001 — never let one bad proc abort the sweep
            continue
    for proc in victims:
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:  # noqa: BLE001
            continue
    if victims:
        # Give SIGTERM a moment, then SIGKILL any survivor so the leak is real-swept.
        try:
            _gone, alive = psutil.wait_procs(victims, timeout=3)
            for proc in alive:
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            pass
        logger.info("sites: reaped %d build-path workerd process(es) under %s", len(victims), root)
    return len(victims)


class SmokeGateFailed(RuntimeError):
    """Raised when the workerd smoke render fails — the site is not deployed."""


@dataclass(frozen=True)
class BuildResult:
    project_dir: str
    # The pinned ripple version the generated app bundles. Echoed for audit on the
    # RIPPLE path only — the svelte generator ships no ripple runtime and its
    # GenerateResult omits ``rippleVersion`` entirely (paw-sites types.ts §4.2), so
    # this is ``None`` on the svelte path. Optional so reading the svelte result
    # does not KeyError.
    ripple_version: str | None = None


def build_home() -> Path:
    """Root dir for the persistent per-pocket build working dirs (PERF-3). Each
    pocket builds into ``build_home()/<pocket_id>/`` so node_modules persists
    across builds and `bun install` can be cached. ~/.pocketpaw/site-builds by
    default; override with PAW_SITES_BUILD_DIR (tests use a temp dir so they never
    write the real home). Mirrors local_server.sites_home()'s convention."""
    raw = os.environ.get("PAW_SITES_BUILD_DIR")
    base = Path(raw) if raw else Path.home() / ".pocketpaw" / "site-builds"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _install_inputs_hash(project_dir: str) -> str:
    """Fingerprint the install inputs (package.json + any lockfile) in a project
    dir. A stable hash for an unchanged dependency set; it changes the moment a dep
    or lockfile changes, which is exactly when node_modules goes stale and a
    reinstall is required (PERF-3 correctness guard). Computed AFTER
    ``_rewrite_ripple_dep`` so the @ripple-ui/svelte / motion rewrites are part of
    the fingerprint."""
    h = hashlib.sha256()
    for name in _INSTALL_INPUT_FILES:
        p = Path(project_dir, name)
        if p.is_file():
            h.update(name.encode())
            h.update(b"\0")
            h.update(p.read_bytes())
            h.update(b"\0")
    return h.hexdigest()


def _gen_cmd_argv() -> list[str]:
    """The generator invocation, tokenised. Default "paw-sites-gen" (the bin on
    PATH). Override with PAW_SITES_GEN_CMD to run an uninstalled build, e.g.
    "node /abs/path/paw-sites/dist/cli.js". PROD TODO: install the bin and drop
    the override."""
    return shlex.split(os.environ.get("PAW_SITES_GEN_CMD", "paw-sites-gen"))


def _ripple_dep_source() -> str:
    """The npm source spec the generated project's @ripple-ui/svelte dep is
    rewritten to before install. Default "0.2.0" (a registry version — valid
    once ripple is published). Locally override with PAW_SITES_RIPPLE_DEP set to
    a tarball, e.g. "file:/tmp/ripple-ui-svelte-0.2.0.tgz". PROD TODO: publish
    ripple, pin the registry version, drop the local tarball shim."""
    return os.environ.get("PAW_SITES_RIPPLE_DEP", "0.2.0")


def _ripple_motion_dep() -> str:
    """Version spec for motion.dev — the animation runtime ripple's dist
    lazy-loads via a client-only ``import('motion')``. ripple does NOT bundle
    motion (it stays out of the SSR/workerd pass), so the generated site has to
    declare it as a direct dep or `bun run build` can't resolve the dynamic
    import. Matters most on the local ``file:`` ripple path: a ``file:`` dep
    isn't hoisted, so ripple's own motion never reaches the consumer. Override
    with PAW_SITES_MOTION_DEP; keep it in lockstep with ripple's motion pin."""
    return os.environ.get("PAW_SITES_MOTION_DEP", "^12.40.0")


def _rewrite_ripple_dep(project_dir: str, source: str) -> None:
    """Make the generated package.json install: point @ripple-ui/svelte at a
    resolvable source AND ensure motion.dev is declared. The template pins the
    (unpublished) ripple version "0.2.0" and omits motion; we overwrite that one
    key and add motion if absent, leaving every other dep intact. Without motion
    the generator's `bun run build` smoke fails to resolve ripple's runtime
    ``import('motion')`` (the same break that hit paw-enterprise)."""
    pkg_path = Path(project_dir, "package.json")
    pkg = json.loads(pkg_path.read_text())
    deps = pkg.setdefault("dependencies", {})
    if "@ripple-ui/svelte" in deps:
        deps["@ripple-ui/svelte"] = source
        deps.setdefault("motion", _ripple_motion_dep())
        pkg_path.write_text(json.dumps(pkg, indent=2))


class _Runner(Protocol):
    async def generate(self, input_json: dict[str, Any], out_dir: str) -> dict[str, Any]: ...
    async def install(self, project_dir: str) -> tuple[bool, str]: ...
    # P0a: the static-output step — runs `bun run build` (emits
    # `.svelte-kit/cloudflare/` with the section anchors + injected edit-bridge),
    # reaps the build-path workerd, and applies the SSR fail-gate ONLY when
    # ``gate=True``. ALWAYS runs (preview + publish) so the served file is fresh.
    async def build_static(self, project_dir: str, *, gate: bool) -> tuple[bool, str]: ...
    # Legacy SSR fail-gate (== the old combined `bun run build` + marker check).
    # Kept for fake runners that predate build_static; the real runner routes the
    # gate through build_static. build() falls back to it when build_static is absent.
    async def smoke(self, project_dir: str) -> tuple[bool, str]: ...
    # PERF-3: fingerprint of the install inputs (package.json + lockfile). build()
    # uses it to decide whether `bun install` can be skipped. Optional on a runner —
    # build() falls back to always-install when a runner does not provide it.
    def install_inputs_hash(self, project_dir: str) -> str: ...


class _SubprocessRunner:
    """Real runner: shells out to the generator (Bun/Node), `bun install`, and
    the smoke render."""

    def __init__(self, gen_cmd: list[str] | None = None) -> None:
        self._gen_cmd = gen_cmd or _gen_cmd_argv()

    async def generate(self, input_json: dict[str, Any], out_dir: str) -> dict[str, Any]:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(input_json, fh)
            input_path = fh.name
        timeout_s = _build_timeout_sec()
        try:
            # start_new_session=True: own process group so a wedged generator (and
            # any children it leaked) can be killed as a group on timeout.
            proc = await asyncio.create_subprocess_exec(
                *self._gen_cmd,
                "build",
                "--input",
                input_path,
                "--out",
                out_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout, stderr = await _communicate_bounded(proc, timeout_s, "generator")
            except _BuildTimeout as exc:
                # Preserve generate()'s raise-on-failure contract: a timed-out
                # generator is a failed generate.
                raise RuntimeError(f"generator timed out after {exc.timeout_s}s") from exc
            if proc.returncode != 0:
                raise RuntimeError(f"generator failed: {stderr.decode()}")
            return json.loads(stdout.decode().strip().splitlines()[-1])
        finally:
            os.unlink(input_path)

    def install_inputs_hash(self, project_dir: str) -> str:
        # PERF-3: fingerprint of package.json + lockfile, computed on the real
        # files in the prepared project dir (build() runs _rewrite_ripple_dep
        # first, so the rewrite is included).
        return _install_inputs_hash(project_dir)

    async def install(self, project_dir: str) -> tuple[bool, str]:
        # The deps are already made resolvable by build() (_rewrite_ripple_dep runs
        # before the install decision so the dep-hash reflects the final inputs).
        # This just runs `bun install` on the prepared dir.
        timeout_s = _build_timeout_sec()
        # start_new_session=True: own process group so a wedged install is killable
        # as a group on timeout.
        proc = await asyncio.create_subprocess_exec(
            "bun",
            "install",
            "--no-save",
            cwd=project_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await _communicate_bounded(proc, timeout_s, "bun install")
        except _BuildTimeout as exc:
            # Preserve install()'s (ok, msg) contract: a timeout is a build failure,
            # so build() raises SmokeGateFailed → the publish/editable caller maps it
            # to a CloudError and the FE degrades to view-only (same as a non-zero exit).
            return (
                False,
                f"bun install timed out after {exc.timeout_s}s — killed the build "
                "(likely a wedged workerd prerender)",
            )
        if proc.returncode != 0:
            return False, f"bun install failed (exit {proc.returncode}): {stderr.decode()[-500:]}"
        return True, "ok"

    async def build_static(self, project_dir: str, *, gate: bool) -> tuple[bool, str]:
        # P0a: run `bun run build` — the step that emits the deployable
        # `.svelte-kit/cloudflare/` static output (the data-paw-section anchors + the
        # injected edit-bridge for an editable site). This ALWAYS runs (preview +
        # publish): persist_site copies whatever this leaves on disk, so skipping it
        # is exactly what served the stale anchorless build (the #1 bug).
        #   * A non-zero exit ALWAYS fails — a build that can't produce output is
        #     never servable, on any path.
        #   * A known workerd SSR marker fails the gate ONLY when ``gate=True`` (the
        #     LIVE publish path). A preview build tolerates a would-fail SSR render
        #     (the live publish still gates + rolls back, so a broken edit can't go
        #     live) but still BUILDS so the served preview is fresh + anchored.
        timeout_s = _build_timeout_sec()
        # start_new_session=True: own process group. This is the step that wedges —
        # adapter-cloudflare's workerd prerender can hang forever (upstream SvelteKit
        # bug). The group kill on timeout takes out the leaked workerd CHILD too, not
        # just the bun parent.
        proc = await asyncio.create_subprocess_exec(
            "bun",
            "run",
            "build",
            cwd=project_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await _communicate_bounded(proc, timeout_s, "build")
        except _BuildTimeout as exc:
            # A wedged build (typically the hung workerd prerender) is killed as a
            # group; still run the defensive reap below for any straggler, then return
            # the same (False, msg) shape a non-zero exit returns so build() treats it
            # as a normal build failure (→ SmokeGateFailed → CloudError → view-only FE).
            reap_build_workerd(project_dir)
            return (
                False,
                f"build timed out after {exc.timeout_s}s — killed the build "
                "(likely a wedged workerd prerender)",
            )
        # P1a: reap the prerender-spawned workerd after EVERY build (the leak is
        # independent of the gate), scoped to this build's project dir.
        reap_build_workerd(project_dir)
        haystack = stdout.decode() + "\n" + stderr.decode()
        if proc.returncode != 0:
            return False, f"build failed (exit {proc.returncode})"
        if gate:
            for marker in _WORKERD_SSR_MARKERS:
                if marker in haystack:
                    return False, f"workerd SSR failure: {marker}"
        return True, "ok"

    async def smoke(self, project_dir: str) -> tuple[bool, str]:
        # Legacy entry point (== the old combined `bun run build` + SSR fail-gate).
        # The real path now routes through build_static; this delegates to it with
        # the gate ON so any direct/legacy caller keeps the publish-time semantics.
        return await self.build_static(project_dir, gate=True)


async def apply_leaf_edits(
    source: dict[str, str],
    edits: list[dict[str, Any]],
    *,
    _exec: Any = None,
) -> dict[str, Any]:
    """Splice native-editor leaf edits into a svelte source map via the paw-sites
    ``apply-leaf-edit`` CLI (NE-4b) and return its per-uid verdict.

    The Python bridge to NE-4a's ``apply-leaf-edit`` subcommand. It shells out to
    the SAME tokenised generator command as the build path (``_gen_cmd_argv()``) but
    with ``apply-leaf-edit --input <tempfile>``, where the tempfile carries
    ``{"source": {<relpath>: <contents>}, "edits": [{"uid","op"}, ...]}`` (``op`` is
    ``{"kind":"setText","html":...}`` or ``{"kind":"setProp","name":...,"value":...}``).
    The CLI applies each edit IN ORDER to the file that carries that leaf and emits
    exactly ONE JSON line on stdout:
    ``{"source": {<relpath>: <contents>}, "results": [{"uid","applied","reason"?}]}``.
    A rejected edit (``applied: false`` + ``reason``) leaves ITS file byte-identical
    in the returned ``source`` (the caller keeps the whole-file re-author for it).

    Unlike ``build`` this is a PURE transform — no ``bun install``, no
    ``bun run build``, no workerd — so it is fast and safe to call inline on the
    NE-4b persist path (the native editor already rendered the edit optimistically;
    persisting the spliced draft is the whole job).

    Failure surfaces as a ``RuntimeError`` the caller must handle: a non-zero exit
    (carrying the CLI's stderr) or a timed-out splice — a wedged splice is a failed
    splice, mirroring how ``_SubprocessRunner.generate`` converts ``_BuildTimeout``.

    ``_exec`` is an injectable subprocess-exec seam (defaults to
    ``asyncio.create_subprocess_exec``): a test passes a fake returning a stub proc
    with a canned ``communicate()`` so the bridge is unit-testable without Bun.
    """
    # Assign input_path FIRST (before json.dump) so a serialization failure can't
    # leave the created temp file un-tracked: NamedTemporaryFile(delete=False)
    # materializes the file immediately, so if json.dump raises mid-write we still
    # need its path to clean it up. ONE outer try/finally covers creation + write +
    # exec, and the finally is guarded (only unlink a path that was assigned AND still
    # exists) so it never NameErrors on an early failure and never leaks the tempfile.
    input_path = ""
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            input_path = fh.name
            json.dump({"source": source, "edits": edits}, fh)
        timeout_s = _build_timeout_sec()
        # start_new_session=True: own process group so a wedged splice (and any
        # children it leaked) can be killed as a group on timeout — same as generate().
        proc = await (_exec or asyncio.create_subprocess_exec)(
            *_gen_cmd_argv(),
            "apply-leaf-edit",
            "--input",
            input_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await _communicate_bounded(proc, timeout_s, "apply-leaf-edit")
        except _BuildTimeout as exc:
            # A timed-out splice is a failed splice (mirror generate()'s raise-on-timeout).
            raise RuntimeError(f"apply-leaf-edit timed out after {exc.timeout_s}s") from exc
        if proc.returncode != 0:
            raise RuntimeError(f"apply-leaf-edit failed: {stderr.decode()}")
        return json.loads(stdout.decode().strip().splitlines()[-1])
    finally:
        if input_path and os.path.exists(input_path):
            os.unlink(input_path)


class GeneratorClient:
    # PERF-3: per-pocket async locks serialize concurrent builds of the SAME pocket
    # (they share one on-disk working dir, so an interleaved generate/install would
    # corrupt it). Class-level so all clients in a process share the same lock per
    # pocket. Last-writer wins. Per-process only — per-tenant few editors makes
    # cross-process contention a non-issue (noted in PERF-3 scope).
    _pocket_locks: dict[str, asyncio.Lock] = {}

    def __init__(self, _runner: _Runner | None = None) -> None:
        self._runner = _runner or _SubprocessRunner()

    @classmethod
    def _lock_for(cls, pocket_id: str) -> asyncio.Lock:
        lock = cls._pocket_locks.get(pocket_id)
        if lock is None:
            lock = asyncio.Lock()
            cls._pocket_locks[pocket_id] = lock
        return lock

    async def build(
        self,
        *,
        ripple_spec: dict[str, Any] | None = None,
        theme: dict[str, Any],
        site_id: str,
        title: str,
        capture_api_base: str,
        capture_signed_key: str,
        engine: str = "ripple",
        source: dict[str, Any] | None = None,
        builder_origin: str | None = None,
        pocket_id: str | None = None,
        smoke: bool = True,
        static_build: bool = True,
    ) -> BuildResult:
        """Generate + smoke-build a Paw Site, forking STAGE 2 on ``engine``.

        ``engine="ripple"`` (default) compiles ``ripple_spec`` into the site;
        ``engine="svelte"`` materializes ``source`` (the pocket's hand-written
        SvelteKit source map ``{relative_path: file_contents}``) instead. Per
        design spec §4.2 the two payloads are mutually exclusive: the svelte
        input carries ``source`` and OMITS ``rippleSpec``; the ripple input
        carries ``rippleSpec`` and OMITS ``source`` (gaining only the
        ``engine: "ripple"`` tag). ``siteConfig`` + ``theme`` are sent on both
        tracks unchanged. Stages 1, 3-8 (install/smoke/...) are track-agnostic.

        DSV-5: a DYNAMIC svelte pocket's ``source`` envelope ALSO carries its
        live-data bindings (``objects``/``sources``/``actions``/``auth``) as
        SIBLING keys on the same dict as the files. build() SPLITS the envelope
        (``_split_svelte_source``) before sending: the file map goes on
        ``input.source`` and the bindings are spread as FLAT siblings on the
        generator input (``input.objects`` / ``input.sources`` / ``input.actions``
        / ``input.auth``) — the exact shape DSV-1's ``parseBindings`` reads, NOT
        nested under ``source`` (materializeSource would otherwise try to write a
        binding key as a file and break the build). A STATIC svelte pocket carries
        no binding keys, so the split is a no-op: ``input.source`` is the unchanged
        file map and no binding siblings are added (pre-DSV-5 wire bytes).

        ``builder_origin`` (SE-2b) makes the site EDITABLE: when set, it rides
        ``siteConfig.builderOrigin`` and the paw-sites generator (SE-1) injects
        the gated section anchors + the postMessage edit-bridge keyed on it. It
        is OMITTED from the payload when ``None`` so a normal (non-editable)
        publish keeps the exact prior wire bytes and the generator does not inject
        the bridge.

        ``pocket_id`` (PERF-3) builds into a STABLE per-pocket working dir under
        build_home() (``<build_home>/<pocket_id>/``) so node_modules persists and
        `bun install` is cached across builds (preview AND publish). When None,
        build() falls back to a fresh throwaway tempfile dir (no caching) so legacy
        callers keep their prior behaviour. Concurrent builds of the SAME pocket
        are serialized by a per-pocket lock (they share the on-disk dir).

        ``static_build`` (P0a) controls whether ``bun run build`` runs to emit the
        deployable ``.svelte-kit/cloudflare/`` static output. It defaults to ``True``
        and MUST stay ``True`` on any path whose result is SERVED via deploy_local
        (every preview / publish — ``persist_site`` copies whatever this leaves on
        disk, so skipping it serves a stale build, the #1 bug). The dev server
        (``vite dev`` serves from source, never deploy_local) passes
        ``static_build=False`` so it only runs generate + cached install — the static
        build is wasted work there.

        ``smoke`` (P0a — was PERF-4) now gates ONLY the workerd SSR FAIL-check, NOT
        whether ``bun run build`` runs. A PREVIEW/edit/arm build passes ``smoke=False``
        to SKIP the SSR fail-gate (a preview that would fail SSR is no longer blocked
        — the live publish still gates + rolls back) but the static build STILL runs
        so the served preview is fresh + anchored. A LIVE publish keeps the default
        ``smoke=True`` so the SSR gate (and its rollback-on-SmokeGateFailed behaviour
        at the caller) is unchanged. Before this fix ``smoke`` ALSO gated the build
        itself, so a preview skipped ``bun run build`` and served the stale build.
        """
        if pocket_id is None:
            return await self._build_one(
                ripple_spec=ripple_spec,
                theme=theme,
                site_id=site_id,
                title=title,
                capture_api_base=capture_api_base,
                capture_signed_key=capture_signed_key,
                engine=engine,
                source=source,
                builder_origin=builder_origin,
                pocket_id=None,
                smoke=smoke,
                static_build=static_build,
            )
        async with self._lock_for(pocket_id):
            return await self._build_one(
                ripple_spec=ripple_spec,
                theme=theme,
                site_id=site_id,
                title=title,
                capture_api_base=capture_api_base,
                capture_signed_key=capture_signed_key,
                engine=engine,
                source=source,
                builder_origin=builder_origin,
                pocket_id=pocket_id,
                smoke=smoke,
                static_build=static_build,
            )

    async def _build_one(
        self,
        *,
        ripple_spec: dict[str, Any] | None,
        theme: dict[str, Any],
        site_id: str,
        title: str,
        capture_api_base: str,
        capture_signed_key: str,
        engine: str,
        source: dict[str, Any] | None,
        builder_origin: str | None,
        pocket_id: str | None,
        smoke: bool,
        static_build: bool = True,
    ) -> BuildResult:
        # PERF-3: stable per-pocket working dir (overwrite the source each build)
        # so node_modules persists; fall back to a throwaway tempfile dir when no
        # pocket_id is given (legacy callers — no caching).
        if pocket_id is not None:
            out_dir = str(build_home() / pocket_id)
            Path(out_dir).mkdir(parents=True, exist_ok=True)
        else:
            out_dir = tempfile.mkdtemp(prefix=f"paw-site-{site_id}-")
        # §4.2: ``engine`` is always present; the STAGE-2 payload key forks on
        # it. svelte → ``source`` (no rippleSpec); ripple → ``rippleSpec`` (no
        # source). siteConfig + theme ride both tracks unchanged.
        site_config: dict[str, Any] = {
            "siteId": site_id,
            "title": title,
            "captureApiBase": capture_api_base,
            "captureSignedKey": capture_signed_key,
        }
        # SE-2b: only present when the site is being published as editable, so a
        # non-editable publish's payload is byte-identical to before this change.
        if builder_origin:
            site_config["builderOrigin"] = builder_origin
        input_json: dict[str, Any] = {
            "engine": engine,
            "theme": theme,
            "siteConfig": site_config,
        }
        if engine == "svelte":
            # DSV-5: a DYNAMIC svelte pocket carries its live-data bindings
            # (objects/sources/actions/auth) as SIBLING keys on the same ``source``
            # envelope that holds the {path: contents} files. Peel them out: the
            # file map goes on ``input.source`` (materializeSource writes each key
            # as a file, so a binding key mixed in would break the build), and the
            # bindings are spread as FLAT siblings on GenerateInput — the exact
            # shape DSV-1's parseBindings reads (``input.objects`` / ``input.sources``
            # / ``input.actions`` / ``input.auth``), NOT nested under ``source``. A
            # STATIC svelte pocket has no binding keys → ``bindings`` is empty and
            # ``input.source`` is the unchanged file map (pre-DSV-5 wire bytes).
            files, bindings = _split_svelte_source(source)
            input_json["source"] = files
            input_json.update(bindings)
        else:
            input_json["rippleSpec"] = ripple_spec
        gen = await self._runner.generate(input_json, out_dir)
        project_dir = gen["projectDir"]
        # Make the generated project's deps resolvable BEFORE install (and before
        # the dep-hash is computed, so the rewrite is part of the fingerprint).
        # The template pins an unpublished @ripple-ui/svelte; the rewrite swaps it
        # for a resolvable source and adds motion. No-op on the svelte track (no
        # @ripple-ui/svelte dep). Guarded so the fake-runner tests (whose projectDir
        # is not a real dir) don't fail on a missing package.json.
        if Path(project_dir, "package.json").is_file():
            _rewrite_ripple_dep(project_dir, _ripple_dep_source())
        # PERF-3 install cache: run `bun install` ONLY when the dependency set
        # changed. Fingerprint the install inputs and compare to the sentinel from
        # the last successful install in this dir. Match → skip (reuse the cached
        # node_modules). Mismatch / first build → install, then record the new
        # fingerprint. A runner without install_inputs_hash() always installs
        # (legacy fakes), so existing tests are unaffected.
        hasher = getattr(self._runner, "install_inputs_hash", None)
        if hasher is not None:
            new_hash = hasher(project_dir)
            sentinel = Path(project_dir, _INSTALL_HASH_FILE)
            prior_hash = sentinel.read_text().strip() if sentinel.is_file() else None
            if new_hash != prior_hash:
                ok, reason = await self._runner.install(project_dir)
                if not ok:
                    raise SmokeGateFailed(reason)
                # Record AFTER a successful install so a failed install never
                # poisons the cache (next build retries the install).
                if Path(project_dir).is_dir():
                    sentinel.write_text(new_hash)
            # else: deps unchanged — reuse cached node_modules, skip install.
        else:
            ok, reason = await self._runner.install(project_dir)
            if not ok:
                raise SmokeGateFailed(reason)
        # P0a: run the static-output step (`bun run build`) so the served file is
        # FRESH + anchored. The fix splits the two concerns PERF-4's ``smoke`` flag
        # conflated: ``bun run build`` (REQUIRED on any locally-served path —
        # persist_site copies whatever it leaves on disk) vs the workerd SSR
        # FAIL-gate (the actual "smoke" safety check). ``build_static(gate=smoke)``
        # builds; the SSR-marker fail-check applies only on a LIVE publish
        # (smoke=True). A non-zero build exit fails on EITHER path (a build that
        # can't produce output is never servable). This reintroduces per-edit build
        # time — acceptable + correct for now; instant-HMR via dev_server is separate
        # (the dev server passes ``static_build=False`` — it serves from source via
        # ``vite dev`` and never goes through deploy_local, so it needs no static
        # output and skips this whole step).
        if static_build:
            builder = getattr(self._runner, "build_static", None)
            if builder is not None:
                ok, reason = await builder(project_dir, gate=smoke)
                if not ok:
                    raise SmokeGateFailed(reason)
            elif smoke:
                # Legacy fakes without build_static: keep the old
                # smoke()-only-on-publish contract so the existing
                # generator/cache/smoke-gate unit tests (which fake the subprocess and
                # never produce real files) are unaffected.
                ok, reason = await self._runner.smoke(project_dir)
                if not ok:
                    raise SmokeGateFailed(reason)
        # ``rippleVersion`` is present only on the ripple GenerateResult; the svelte
        # path omits it (paw-sites types.ts §4.2), so read it defensively — a svelte
        # build must not KeyError here.
        return BuildResult(project_dir=project_dir, ripple_version=gen.get("rippleVersion"))
