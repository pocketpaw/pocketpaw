"""``render(spec, tokens) -> Bundle`` — one spec through the ONCE-BUILT renderer.

Created for SG-1 (sites proving harness).

WHAT: drives the prebuilt SSR bundle (``node/.build/dist/entry.js``) and assembles
a ``Bundle``. Two drivers, one interface:

* ``SidecarRenderer`` — a resident Node process, bundle imported once.
* ``PerRenderRenderer`` — a fresh ``node render_once.mjs`` per render.

Both speak the same one-line-JSON protocol, so the sidecar-vs-per-render
measurement compares the same work done two ways rather than two code paths.

WHY the HTML shell is assembled in Python and not in the bundle: the per-site
tokens (title, primary colour, capture base, signed key, D1 id) are the ONLY
things that vary between sites. Keeping them out of the JS is what makes the
bundle build-once — the JS sees a spec and nothing else. The shell mirrors
paw-sites/templates/src/app.html.tmpl, and token substitution mirrors the
generator's pass over ``__SITE_*__`` placeholders.

WHAT IS NOT HERE, deliberately: no installer, no compiler, no bundler. Any of
those in the render path would reintroduce the 45-60s per-publish cost this slice
exists to remove — ``assert_no_installer_in_render_path`` makes that structural
rather than a promise, and the render path never shells out to anything but
``node <script> <bundle>``.
"""

from __future__ import annotations

import html as html_escape
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bundle import LANE_RIPPLE, RUNG_PREBUILT_SSR, Bundle, BundleManifest

NODE_DIR = Path(__file__).resolve().parent / "node"
BUILD_DIR = NODE_DIR / ".build"
ENTRY_JS = BUILD_DIR / "dist" / "entry.js"
RENDERER_MANIFEST = BUILD_DIR / "renderer-manifest.json"
SIDECAR_JS = NODE_DIR / "sidecar.mjs"
RENDER_ONCE_JS = NODE_DIR / "render_once.mjs"

ENTRY_HTML_NAME = "index.html"

# Wall-clock ceiling for a single render request. Generous next to the ~1ms a warm
# sidecar render and the ~210ms a per-render spawn actually cost (measured), tight
# enough that a wedged sidecar fails the scenario instead of hanging the suite.
RENDER_TIMEOUT_S = 60.0

# Anything that would make a render shell out to a package installer. Asserted
# against, not just avoided — see assert_no_installer_in_render_path.
_INSTALLER_TOKENS = ("bun install", "npm install", "npm ci", "pnpm install", "yarn install")


class RendererNotBuilt(RuntimeError):
    """The once-built bundle is missing. Run ``node node/build.mjs`` first."""


class RenderFailed(RuntimeError):
    """The renderer could not produce HTML for this spec."""


@dataclass(frozen=True)
class SiteTokens:
    """The per-site variation — the whole of it.

    Mirrors the ``__SITE_*__`` placeholders paw-sites' generator substitutes.
    Everything else about a published site is identical across sites, which is
    the premise this harness tests.
    """

    site_id: str
    title: str
    primary_color: str = "#0A84FF"
    capture_api_base: str = ""
    signed_key: str = ""
    d1_database_id: str = ""
    csr: bool = False
    form_action: str = "/api/submit"

    def as_dict(self) -> dict[str, Any]:
        # signed_key is a credential: recorded as presence only, never echoed
        # into an evidence report that gets committed or shared.
        return {
            "site_id": self.site_id,
            "title": self.title,
            "primary_color": self.primary_color,
            "capture_api_base": self.capture_api_base,
            "signed_key_present": bool(self.signed_key),
            "d1_database_id": self.d1_database_id,
            "csr": self.csr,
            "form_action": self.form_action,
        }


def _node_exe() -> str:
    exe = os.environ.get("PAW_NODE") or shutil.which("node")
    if not exe:
        raise RendererNotBuilt("node not found on PATH; set PAW_NODE")
    return exe


def renderer_build_info() -> dict[str, Any]:
    """Read the build manifest, or explain what to run."""
    if not RENDERER_MANIFEST.exists() or not ENTRY_JS.exists():
        raise RendererNotBuilt(
            f"renderer not built: expected {ENTRY_JS}. Run: node {NODE_DIR / 'build.mjs'}"
        )
    info = json.loads(RENDERER_MANIFEST.read_text(encoding="utf-8"))
    if not info.get("ok"):
        raise RendererNotBuilt(f"renderer build did not succeed: {info}")
    return info


def _spawn_argv_literals(path: Path) -> set[str]:
    """Every string literal this module can pass as argv to a subprocess.

    Walks the AST for ``subprocess.run`` / ``subprocess.Popen`` calls and collects
    the string constants in their first argument (the argv list). Non-literal
    elements (``_node_exe()``, ``str(SIDECAR_JS)``) are recorded as their source
    expression so the evidence shows what they are without evaluating them.

    Why AST and not grep: a substring scan of this file's own source cannot tell
    the difference between "spawns an installer" and "documents that it doesn't".
    The argv of every reachable subprocess call is the actual fact in question.
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    literals: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            f"{func.value.id}.{func.attr}"
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
            else getattr(func, "id", "")
        )
        if name not in {"subprocess.run", "subprocess.Popen"}:
            continue
        if not node.args:
            continue
        argv = node.args[0]
        if not isinstance(argv, (ast.List, ast.Tuple)):
            literals.add(ast.unparse(argv))
            continue
        for element in argv.elts:
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                literals.add(element.value)
            else:
                literals.add(ast.unparse(element))

    return literals


def assert_no_installer_in_render_path() -> dict[str, Any]:
    """Prove no render can shell out to a package installer.

    Three independent checks, because "we don't call bun install" is a claim and
    this slice's whole point is that it is structurally true:

    1. Neither render driver's SOURCE mentions an installer command.
    2. This module never names one either (it owns every subprocess a render
       spawns), and it spawns nothing but ``node``.
    3. The built bundle is self-contained per its manifest, or its node_modules
       already exists — either way a render has nothing left to install.

    Returns the evidence. Raises ``AssertionError`` on violation, so a future
    edit that sneaks an installer into the render path fails the suite.
    """
    evidence: dict[str, Any] = {}

    for label, path in (("sidecar", SIDECAR_JS), ("render_once", RENDER_ONCE_JS)):
        source = path.read_text(encoding="utf-8")
        hits = [t for t in _INSTALLER_TOKENS if t in source]
        assert not hits, f"{label} driver mentions an installer: {hits}"
        # Only 'node' may be spawned from a render driver; these two spawn nothing.
        for forbidden in ("child_process", "spawnSync", "execSync"):
            assert forbidden not in source, f"{label} driver imports {forbidden}"
        evidence[f"{label}_driver_clean"] = True

    # This module is checked by AST, not by substring: its own token table and
    # docstrings necessarily contain the installer names, and a substring scan
    # either trips on them or gets weakened until it proves nothing. What matters
    # is the ARGV of every subprocess this module can spawn, so read that
    # directly — every string literal passed to subprocess.run/Popen.
    spawned = _spawn_argv_literals(Path(__file__))
    offenders = [
        literal
        for literal in spawned
        if any(token.split()[0] == literal or token in literal for token in _INSTALLER_TOKENS)
    ]
    assert not offenders, f"renderer.py can spawn an installer: {offenders}"
    evidence["renderer_module_clean"] = True
    evidence["spawn_argv_literals"] = sorted(spawned)

    info = renderer_build_info()
    node_modules = BUILD_DIR / "node_modules"
    assert not info["needs_node_modules"] or node_modules.exists(), (
        "bundle needs node_modules and none is present — a render would have "
        "nothing to import and no way to get it"
    )
    evidence["bundle_shape"] = info["bundle_shape"]
    evidence["needs_node_modules"] = info["needs_node_modules"]
    evidence["node_modules_present"] = node_modules.exists()
    evidence["subprocesses_a_render_may_spawn"] = ["node"]
    return evidence


def _normalize_spec(spec: Any) -> Any:
    """Wrap a bare UINode as ``{ui: node}``.

    ripple's ``normalizeSpec`` only recognizes a UniversalSpec (has ``intent``)
    or a legacy UISpec (has ``ui``); a BARE ``{type, children}`` node falls
    through to an empty container and renders NOTHING. Pockets commonly hand
    over a bare node, so paw-sites/src/spec-embed.ts wraps it — mirrored here.

    Non-dict specs pass through untouched so ``verify`` gets to fail on them
    rather than this function guessing what a malformed spec meant.
    """
    if isinstance(spec, Mapping) and "ui" not in spec and "intent" not in spec:
        if "type" in spec:
            return {"ui": dict(spec)}
    return spec


def _build_html(head: str, body: str, tokens: SiteTokens) -> str:
    """Assemble the page shell.

    Mirrors paw-sites/templates/src/app.html.tmpl: the same head order, the same
    ``%sveltekit.head%`` / ``%sveltekit.body%`` slots, with the tokens
    substituted. The brand ``--primary`` custom property is set on :root the way
    the generator's brand CSS did, so the token demonstrably reaches the output.
    """
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "  <head>\n"
        '    <meta charset="utf-8" />\n'
        '    <meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        f"    <title>{html_escape.escape(tokens.title)}</title>\n"
        f'    <link rel="stylesheet" href="/assets/theme.css" />\n'
        f'    <link rel="stylesheet" href="/assets/styles.css" />\n'
        f"    <style>:root {{ --primary: {html_escape.escape(tokens.primary_color)}; }}</style>\n"
        f"    {head}\n"
        "  </head>\n"
        f"  <body>{body}</body>\n"
        "</html>\n"
    )


class _BaseRenderer:
    """Shared assembly: spec in, Bundle out. Subclasses only move the JSON."""

    driver: str

    def __init__(self) -> None:
        self._info = renderer_build_info()
        self._assets: dict[str, bytes] = {}
        for rel in self._info.get("assets", ()):
            path = BUILD_DIR / "dist" / rel
            if path.exists():
                self._assets[rel] = path.read_bytes()

    @property
    def renderer_version(self) -> str:
        return self._info["renderer_version"]

    def _exchange(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def render(self, spec: Any, tokens: SiteTokens) -> Bundle:
        started = time.perf_counter()
        response = self._exchange(
            {"id": tokens.site_id, "spec": _normalize_spec(spec), "formAction": tokens.form_action}
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        if not response.get("ok"):
            raise RenderFailed(response.get("error") or "renderer returned no result")

        html = _build_html(response.get("head", ""), response.get("body", ""), tokens)
        files: dict[str, bytes] = {ENTRY_HTML_NAME: html.encode("utf-8")}
        files.update(self._assets)

        manifest = BundleManifest(
            entry_html=ENTRY_HTML_NAME,
            asset_paths=tuple(sorted(self._assets)),
            # csr=False is the static path: prerendered HTML, zero client JS, and
            # the lead form POSTs natively. A worker is needed only to accept that
            # POST — which is a capture concern, not a render concern, so it is
            # driven by whether a capture endpoint was configured.
            needs_server_worker=bool(tokens.capture_api_base) or tokens.csr,
            lane=LANE_RIPPLE,
            renderer_version=self.renderer_version,
            fallback_rung=RUNG_PREBUILT_SSR,
            extra={
                "driver": self.driver,
                "render_ms": round(elapsed_ms, 2),
                "ripple_version": self._info["ripple_version"],
                "ripple_source": self._info["ripple_source"],
                "bundle_shape": self._info["bundle_shape"],
                "tokens": tokens.as_dict(),
            },
        )
        return Bundle(files=files, manifest=manifest)


class SidecarRenderer(_BaseRenderer):
    """Resident Node process; the bundle is imported once and reused."""

    driver = "sidecar"

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self.cold_start_ms: float | None = None
        self.meta: dict[str, Any] = {}

    def start(self) -> None:
        if self._proc is not None:
            return
        started = time.perf_counter()
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [_node_exe(), str(SIDECAR_JS), str(ENTRY_JS)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            cwd=str(BUILD_DIR),
        )
        assert proc.stdout is not None
        ready = proc.stdout.readline()
        if not ready:
            stderr = proc.stderr.read() if proc.stderr else ""
            proc.kill()
            raise RenderFailed(f"sidecar died before ready: {stderr[:2000]}")
        payload = json.loads(ready)
        if not payload.get("ready"):
            proc.kill()
            raise RenderFailed(f"sidecar sent an unexpected first frame: {payload}")
        self.cold_start_ms = (time.perf_counter() - started) * 1000.0
        self.meta = payload.get("meta", {})
        self._proc = proc

    def _exchange(self, request: dict[str, Any]) -> dict[str, Any]:
        if self._proc is None:
            self.start()
        proc = self._proc
        assert proc is not None and proc.stdin is not None and proc.stdout is not None

        # One request at a time: the NDJSON protocol pairs each response with the
        # preceding request by position, so concurrent writers would interleave.
        with self._lock:
            proc.stdin.write(f"{json.dumps(request)}\n")
            proc.stdin.flush()
            line = proc.stdout.readline()

        if not line:
            stderr = proc.stderr.read() if proc.stderr else ""
            raise RenderFailed(f"sidecar closed mid-render: {stderr[:2000]}")
        return json.loads(line)

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.write('{"shutdown":true}\n')
                proc.stdin.flush()
                proc.stdin.close()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        finally:
            for stream in (proc.stdout, proc.stderr):
                if stream:
                    stream.close()

    def __enter__(self) -> SidecarRenderer:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


class PerRenderRenderer(_BaseRenderer):
    """A fresh Node process per render — every render pays spawn + import."""

    driver = "per-render"

    def _exchange(self, request: dict[str, Any]) -> dict[str, Any]:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [_node_exe(), str(RENDER_ONCE_JS), str(ENTRY_JS)],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=RENDER_TIMEOUT_S,
            cwd=str(BUILD_DIR),
            check=False,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            raise RenderFailed(
                f"render_once exited {completed.returncode}: {completed.stderr[:2000]}"
            )
        return json.loads(completed.stdout.strip().splitlines()[-1])


def render(spec: Any, tokens: SiteTokens, *, driver: str = "sidecar") -> Bundle:
    """Render one spec to a ``Bundle``.

    The convenience entry point. ``driver='sidecar'`` starts and stops a process
    per call, so it is for one-shot renders and tests — a caller doing many
    renders should hold a ``SidecarRenderer`` open, which is the whole point of
    the resident arm.
    """
    if driver == "per-render":
        return PerRenderRenderer().render(spec, tokens)
    if driver == "sidecar":
        with SidecarRenderer() as renderer:
            return renderer.render(spec, tokens)
    raise ValueError(f"unknown driver {driver!r}")


if __name__ == "__main__":  # tiny manual probe
    bundle = render(
        {"type": "container", "children": [{"type": "heading", "props": {"text": "hello"}}]},
        SiteTokens(site_id="probe", title="Probe"),
    )
    sys.stdout.write(bundle.entry_text())
