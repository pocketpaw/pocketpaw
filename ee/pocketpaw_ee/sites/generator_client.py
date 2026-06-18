# ee/pocketpaw_ee/sites/generator_client.py — Python bridge to the Node/Bun
# generator (paw-sites-gen). build() runs the generate CLI, then `bun install`
# on the generated project, then (only for a LIVE publish) the workerd smoke
# render; if the smoke gate fails the site does NOT proceed to deploy (Contract
# clause 4). The subprocess calls are isolated behind a _runner so the
# orchestration is unit-testable without Bun/workerd present.
# Created: 2026-05-30 (feat/paw-sites-backend, Task 2.3).
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
import hashlib
import json
import os
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

# Sentinel file (in the per-pocket build dir) recording the install-input
# fingerprint of the last SUCCESSFUL `bun install`. build() skips install when the
# current fingerprint matches this; mismatch forces a reinstall (PERF-3).
_INSTALL_HASH_FILE = ".paw-install-hash"

# Files whose contents define the dependency set. If any change, node_modules is
# stale and must be reinstalled. The lockfile names cover bun's text + binary
# lockfiles and the npm fallback.
_INSTALL_INPUT_FILES = ("package.json", "bun.lock", "bun.lockb", "package-lock.json")


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
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._gen_cmd,
                "build",
                "--input",
                input_path,
                "--out",
                out_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
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
        proc = await asyncio.create_subprocess_exec(
            "bun",
            "install",
            "--no-save",
            cwd=project_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return False, f"bun install failed (exit {proc.returncode}): {stderr.decode()[-500:]}"
        return True, "ok"

    async def smoke(self, project_dir: str) -> tuple[bool, str]:
        # Build the generated project; a non-zero exit OR a known workerd marker
        # in the output fails the gate. Mirrors paw-sites/src/smoke.ts markers.
        proc = await asyncio.create_subprocess_exec(
            "bun",
            "run",
            "build",
            cwd=project_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        haystack = stdout.decode() + "\n" + stderr.decode()
        for marker in ("window is not defined", "document is not defined", "No such module"):
            if marker in haystack:
                return False, f"workerd SSR failure: {marker}"
        if proc.returncode != 0:
            return False, f"build failed (exit {proc.returncode})"
        return True, "ok"


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
        source: dict[str, str] | None = None,
        builder_origin: str | None = None,
        pocket_id: str | None = None,
        smoke: bool = True,
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

        ``smoke`` (PERF-4) gates the workerd SMOKE render (the SSR safety check).
        The smoke render is per-edit overhead only needed before a LIVE deploy, so
        a PREVIEW/edit/arm build passes ``smoke=False`` to SKIP it — the build runs
        generate + install only and never spawns the workerd render. A LIVE publish
        keeps the default ``smoke=True`` so the gate (and its
        rollback-on-SmokeGateFailed behaviour at the caller) is unchanged. A
        preview build that WOULD fail smoke is no longer blocked — acceptable, since
        the live publish still gates + rolls back.
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
        source: dict[str, str] | None,
        builder_origin: str | None,
        pocket_id: str | None,
        smoke: bool,
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
            input_json["source"] = source or {}
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
        # PERF-4: run the workerd SMOKE render only when the caller asked for it
        # (smoke=True — the LIVE publish path). A PREVIEW/edit/arm build passes
        # smoke=False to SKIP the render: it is per-edit overhead only needed before
        # a live deploy, and a preview that would fail smoke is no longer blocked
        # (the live publish still gates + rolls back). Skipping leaves
        # generate+install (the correctness-bearing steps) intact.
        if smoke:
            ok, reason = await self._runner.smoke(project_dir)
            if not ok:
                raise SmokeGateFailed(reason)
        # ``rippleVersion`` is present only on the ripple GenerateResult; the svelte
        # path omits it (paw-sites types.ts §4.2), so read it defensively — a svelte
        # build must not KeyError here.
        return BuildResult(project_dir=project_dir, ripple_version=gen.get("rippleVersion"))
