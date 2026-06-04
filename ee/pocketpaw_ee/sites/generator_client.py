# ee/pocketpaw_ee/sites/generator_client.py — Python bridge to the Node/Bun
# generator (paw-sites-gen). build() runs the generate CLI, then `bun install`
# on the generated project, then the workerd smoke render; if the smoke gate
# fails the site does NOT proceed to deploy (Contract clause 4). The subprocess
# calls are isolated behind a _runner so the orchestration is unit-testable
# without Bun/workerd present.
# Created: 2026-05-30 (feat/paw-sites-backend, Task 2.3).
#
# Updated 2026-06-04 (feat/sites-svelte-engine — Paw Sites "Svelte track"):
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
import json
import os
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class SmokeGateFailed(RuntimeError):
    """Raised when the workerd smoke render fails — the site is not deployed."""


@dataclass(frozen=True)
class BuildResult:
    project_dir: str
    ripple_version: str


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

    async def install(self, project_dir: str) -> tuple[bool, str]:
        # Make the generated project's deps resolvable, then install. Without
        # this the generated package.json pins an unpublished @ripple-ui/svelte
        # and `bun run build` can't resolve it in a fresh environment.
        _rewrite_ripple_dep(project_dir, _ripple_dep_source())
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
    def __init__(self, _runner: _Runner | None = None) -> None:
        self._runner = _runner or _SubprocessRunner()

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
        """
        out_dir = tempfile.mkdtemp(prefix=f"paw-site-{site_id}-")
        # §4.2: ``engine`` is always present; the STAGE-2 payload key forks on
        # it. svelte → ``source`` (no rippleSpec); ripple → ``rippleSpec`` (no
        # source). siteConfig + theme ride both tracks unchanged.
        input_json: dict[str, Any] = {
            "engine": engine,
            "theme": theme,
            "siteConfig": {
                "siteId": site_id,
                "title": title,
                "captureApiBase": capture_api_base,
                "captureSignedKey": capture_signed_key,
            },
        }
        if engine == "svelte":
            input_json["source"] = source or {}
        else:
            input_json["rippleSpec"] = ripple_spec
        gen = await self._runner.generate(input_json, out_dir)
        # Install the generated project's deps (rewriting the @ripple-ui/svelte
        # pin to a resolvable source) BEFORE the smoke build, or `bun run build`
        # can't resolve them in a fresh environment. A failed install fails the
        # gate closed — the site does NOT proceed to deploy.
        ok, reason = await self._runner.install(gen["projectDir"])
        if not ok:
            raise SmokeGateFailed(reason)
        ok, reason = await self._runner.smoke(gen["projectDir"])
        if not ok:
            raise SmokeGateFailed(reason)
        return BuildResult(project_dir=gen["projectDir"], ripple_version=gen["rippleVersion"])
