# ee/pocketpaw_ee/sites/local_server.py — LOCAL static file server for the
# Sites local-deploy mode (Phase 3). With no Cloudflare creds, publish() builds
# the static site and "deploys" it by copying the built output tree under a stable
# per-site dir; this module serves that tree over HTTP so the published site has a
# real openable localhost URL (what the cmux smoke + Phase 5 open).
#
# Updated 2026-08-10 (SL-1): this header used to name `.svelte-kit/cloudflare/` as THE
# tree that gets copied. That is now only one of the shapes — a STATIC svelte landing
# site builds on adapter-static and writes `build/`. Both call sites below resolve the
# dir off the artifact via ``resolve_static_output_rel`` rather than deriving it from
# the engine name, because after the adapter fork the engine name cannot answer it.
#
# Updated 2026-08-10 (SG-10 — serve the built ARTIFACT as a preview): the one
# handler now has a second branch. `/<site_id>/...` is unchanged — plain static
# serving of a local DEPLOY. `/_preview/<site_id>/...` routes through
# `artifact_preview.resolve`, which answers from a tree unpacked out of the
# ephemeral build lane's artifact tarball.
#
# Updated 2026-08-10 (SG-10 wiring): the preview branch now has a gated caller in front
# of it — `truth_lane.open_preview`, which refuses an artifact whose pages are rendered by
# a `_worker.js` this server cannot execute, so such a tree is never stored. Nothing in
# this file's request handling changed.
#
# Updated 2026-08-10 (`serve_artifact_preview` is gated too). It shipped ungated for one
# turn, on the reasoning that `resolve`'s own `_worker.js` refusal has to stay reachable
# over real HTTP and therefore something must be able to store a worker-bearing tree. That
# reasoning was right about the requirement and wrong about which function should satisfy
# it: this one is the obvious front door, and an ungated front door acquires a caller.
# `artifact_preview.store_unvouched_artifact` satisfies the requirement instead, under a
# name that says what it is and with a test asserting no production module calls it.
#
# `truth_lane` reaches back here for the loopback base URL, which is why its import of
# this module — and this module's import of it — are both inside functions.
#
# ONE SERVER, TWO BRANCHES, on purpose. The established in-app preview pattern in
# this codebase is already "a localhost HTTP server, iframed by the builder"
# (`dev_server.py` does exactly that with Vite), so the artifact preview rides the
# server that exists rather than standing up a second one. It needs no Node, no dev
# server and no COOP/COEP: the artifact is already built.
#
# THE TWO ROOTS ARE DISJOINT, and that is load-bearing. Deploys live under
# `sites_home()`, which this server hands out as plain static files; previews live
# under `artifact_preview.previews_home()`, which it never serves directly. If a
# preview tree sat inside `sites_home()`, the static branch would serve it without
# passing `resolve`'s guards and the `_worker.js` refusal would have a bypass.
#
# A FAILING PREVIEW CANNOT TAKE A DEPLOY WITH IT. The preview branch catches
# everything and answers 500 for that ONE request; the static branch and the server
# thread are untouched, and `serve_artifact_preview` swallows a store failure and
# returns None so a publish tail can call it safely.
#
# Updated 2026-06-19 (fix/sites-preview-fresh-build, P1a — fail soft on a missing
# build dir): ``deploy_local`` no longer lets a missing ``.svelte-kit/cloudflare/``
# escape as a bare FileNotFoundError → 500. If the build dir is absent but a PRIOR
# deploy of the same site is still on disk, it keeps the prior deploy and returns
# its URL plus a logged warning (the caller serves the last-good site instead of
# erroring). Only when there is no prior deploy either does it raise
# ``MissingBuildOutput`` (a clear, typed error the caller can surface). With the
# P0a fix ``bun run build`` always runs, so a missing build dir should no longer
# happen on the normal path — this is a defensive backstop, not the happy path.
#
# Updated 2026-09-02 (feat/sites-analytics-gate, SA-2 — the copy skips the deploy
# scaffold): ``persist_site`` copied the whole static-output tree, and for an html site
# that tree IS the project root, so it took ``wrangler.jsonc``, ``.assetsignore`` and —
# once SA-1 landed — the generated analytics entry along with the pages, then served
# all three. The entry carries the per-publish salt the visitor hash is built on, which
# is the thing making that hash irreversible; loopback reach lowers the stakes but does
# not change the direction. The copytree now takes an ``ignore``. See
# ``_DEPLOY_SCAFFOLD_NAMES``.
#
# Design:
#   * One server per process, rooted at the sites home (~/.pocketpaw/sites by
#     default, override with PAW_SITES_LOCAL_DIR). A request for /<site_id>/
#     resolves to <home>/<site_id>/index.html (the generated index is
#     prerendered and references its CSS with RELATIVE ./_app/... paths, so it
#     resolves correctly under the /<site_id>/ prefix).
#   * Ephemeral port (OS-assigned) by default so concurrent test runs never
#     collide; override with PAW_SITES_LOCAL_PORT for a stable URL.
#   * Runs on a daemon thread (ThreadingHTTPServer) so it never blocks the
#     asyncio event loop and dies with the process.
#
# PROD TODO: this is a LOCAL shim. In production the static site is uploaded to
# Cloudflare (Workers-for-Platforms) by the real publish path — this server is
# never started when CF creds are present.
#
# Created: 2026-06-01 (feat/paw-sites-backend, Phase 3 Gap 2 — local fake-deploy).

from __future__ import annotations

import logging
import os
import shutil
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from pocketpaw_ee.sites import analytics_worker, artifact_preview
from pocketpaw_ee.sites.engines import resolve_static_output_rel

logger = logging.getLogger(__name__)

# The DEPLOY SCAFFOLD — files ``workers_deploy`` writes into a project so wrangler can
# ship it. None of them is site content, and ``persist_site`` copies right past them
# into a tree this server hands out as plain static files.
#
# BOTH generated counters are listed (SA-3 added the second). A workers publish writes
# one of them depending on whether the build emitted a server entry, and a working dir
# that has published both ways can hold either — so filtering only the one this build
# would write is filtering the wrong file half the time.
#
# The generated counter entry is the one that made this a control rather than a tidy-up
# (SA-2). It carries the per-publish salt the visitor hash is built on, and a salt an
# attacker can read turns that hash into a confirmation oracle: given a candidate IP
# and user-agent, recompute and compare. The reach here is loopback — a developer's own
# machine, not the internet — which lowers the stakes without changing the direction.
# A workers publish and a local publish share the pocket's working dir, so an entry
# written by one is sitting there for the other to copy.
#
# ``wrangler.jsonc`` and ``.assetsignore`` join it because they are the same kind of
# thing: deploy plumbing that a visitor to the served site has no business fetching.
# Cloudflare already excludes all three from what it uploads (the ``.assetsignore``
# ``workers_deploy`` writes), so this makes the local target agree with the deployed
# one rather than inventing a new rule.
_DEPLOY_SCAFFOLD_NAMES = (
    analytics_worker.ENTRY_FILENAME,
    analytics_worker.SHIM_FILENAME,
    "wrangler.jsonc",
    ".assetsignore",
)

# The deployable static-output dir, relative to the project dir, is resolved off the
# ARTIFACT via ``resolve_static_output_rel`` (HE-4 engine-awareness, SL-1 artifact-
# awareness): ripple and DYNAMIC svelte serve the adapter-cloudflare output
# (``.svelte-kit/cloudflare``), a STATIC svelte landing site serves ``build``, and html
# serves the raw static tree at the project root (``.``). Without engine-awareness an
# html site published in local mode looked for a SvelteKit build that never exists and
# failed with MissingBuildOutput; without artifact-awareness a static svelte site does
# the same, because after the adapter fork the engine name no longer determines the dir.
# The constant below is retained for readability + back-compat; it is the
# ripple/dynamic-svelte value only, and is NOT what the call sites read.
_CLOUDFLARE_BUILD_REL = ".svelte-kit/cloudflare"


class MissingBuildOutput(RuntimeError):
    """Raised by deploy_local when there is no built static site to serve AND no
    prior deploy to fall back to. A clear, typed error the caller can surface as a
    meaningful message instead of a bare 500 (P1a)."""


def sites_home() -> Path:
    """Root dir the local server serves and where built sites are persisted.
    ~/.pocketpaw/sites by default; override with PAW_SITES_LOCAL_DIR (tests use
    a temp dir so they never write the real home)."""
    raw = os.environ.get("PAW_SITES_LOCAL_DIR")
    base = Path(raw) if raw else Path.home() / ".pocketpaw" / "sites"
    base.mkdir(parents=True, exist_ok=True)
    return base


def persist_site(site_id: str, project_dir: str, engine: str = "ripple") -> Path:
    """Copy the built static site into the stable per-site dir <home>/<site_id>/
    and return that dir. Replaces any prior deploy of the same site so a re-publish
    serves fresh content. The source tree is ``resolve_static_output_rel(...)`` —
    ``.svelte-kit/cloudflare`` for ripple and dynamic svelte, ``build`` for a STATIC
    svelte site (SL-1), the project root for html.

    THE DEPLOY SCAFFOLD DOES NOT COME WITH IT (SA-2). For an html site the static
    output dir IS the project root, so an unfiltered copy takes the wrangler config
    and the generated analytics entry along with the pages and then serves them. See
    ``_DEPLOY_SCAFFOLD_NAMES`` for why the entry in particular must not travel.

    ``shutil.ignore_patterns`` matches by NAME at every level rather than only at the
    copy root, which is what we want: those three names are ours, no site authors a
    file called any of them, and a nested one would be exactly as wrong as a top-level
    one."""
    src = Path(project_dir, resolve_static_output_rel(project_dir, engine))
    if not src.is_dir():
        raise FileNotFoundError(f"no built static site at {src}")
    dest = sites_home() / site_id
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(*_DEPLOY_SCAFFOLD_NAMES))
    return dest


# --- the singleton server -------------------------------------------------

_server: ThreadingHTTPServer | None = None
_lock = threading.Lock()


class _QuietHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler that doesn't spam stderr with a line per GET, plus the
    `/_preview/<site_id>/...` branch that serves an unpacked build artifact.

    The preview branch answers the request itself rather than going through
    ``translate_path``, so it never resolves inside ``sites_home()`` — the preview
    tree lives outside the served root by design (see the module header)."""

    def log_message(self, *args: object) -> None:  # noqa: D401 - silence access log
        pass

    # --- the preview branch ------------------------------------------------

    def _preview_target(self) -> tuple[str, str] | None:
        """``(site_id, subpath)`` when this request is for a preview, else ``None``.

        ``subpath`` keeps its leading slash — and keeps the difference between ``""``
        (``/_preview/<id>``) and ``"/"`` (``/_preview/<id>/``), which is what lets
        ``resolve`` redirect the first to the second. Without that redirect the page
        loads and its relative asset URLs resolve one level too high, so it renders
        with no CSS and no JS.
        """
        raw = urlsplit(self.path).path
        prefix = f"/{artifact_preview.PREVIEW_URL_PREFIX}"
        if raw != prefix and not raw.startswith(prefix + "/"):
            return None
        rest = raw[len(prefix) :].lstrip("/")
        site_id, separator, tail = rest.partition("/")
        # The separator, not the tail, is what distinguishes `/_preview/<id>` from
        # `/_preview/<id>/` — both leave `tail` empty.
        return site_id, (f"/{tail}" if separator else "")

    def _serve_preview(self, site_id: str, subpath: str) -> None:
        """Write one resolved preview response.

        Catches EVERYTHING. A preview is a best-effort read of a tree built from
        customer content; an exception escaping here would surface as a broken
        connection on this request and, worse, put a traceback between the server
        thread and every OTHER site it is serving — including live local deploys.
        """
        try:
            result = artifact_preview.resolve(site_id, subpath, method=self.command)
        except Exception:  # noqa: BLE001 — contain it to this one request
            logger.warning(
                "sites.artifact_preview: resolve failed for site %s (%s)",
                site_id,
                subpath,
                exc_info=True,
            )
            self.send_error(500, "preview could not be served")
            return

        self.send_response(result.status)
        if result.location is not None:
            mount = f"/{artifact_preview.PREVIEW_URL_PREFIX}/{site_id}"
            self.send_header("Location", f"{mount}{result.location}")
        for name, value in result.headers:
            self.send_header(name, value)
        if result.body:
            self.send_header("Content-Type", result.content_type)
            self.send_header("Content-Length", str(len(result.body)))
        else:
            self.send_header("Content-Length", "0")
        self.end_headers()
        if result.body and self.command != "HEAD":
            self.wfile.write(result.body)

    def do_GET(self) -> None:
        target = self._preview_target()
        if target is None:
            super().do_GET()
            return
        self._serve_preview(*target)

    def do_HEAD(self) -> None:
        target = self._preview_target()
        if target is None:
            super().do_HEAD()
            return
        self._serve_preview(*target)

    def do_POST(self) -> None:
        """A POST is only ever answered for a preview path, and only ever refused.

        A site's form posts to ``/api/submit``, which this deployment does not serve
        (lead capture is deferred). Letting the request fall through to static serving
        would return the page itself with a 200 — indistinguishable from a successful
        submission, and the message would be silently lost. ``resolve`` answers 501
        with a page that says so. Any other path keeps the stock 501, which is what
        this server already did for every POST."""
        # Drain the body before answering. The server speaks HTTP/1.0 and closes after
        # each response, so an unread body would be discarded anyway — but on Windows
        # closing a socket with unread data can reach the client as a reset instead of
        # the 501 page it needs to see.
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 0:
            self.rfile.read(min(length, 1024 * 1024))

        target = self._preview_target()
        if target is None:
            self.send_error(501, f"Unsupported method ({self.command})")
            return
        self._serve_preview(*target)


def ensure_server() -> str:
    """Start the local static server once (idempotent) and return its base URL,
    e.g. "http://127.0.0.1:54321". The server is rooted at sites_home(), so a
    site lives at "<base>/<site_id>/"."""
    global _server
    with _lock:
        if _server is None:
            host = "127.0.0.1"
            port = int(os.environ.get("PAW_SITES_LOCAL_PORT", "0"))
            handler = partial(_QuietHandler, directory=str(sites_home()))
            _server = ThreadingHTTPServer((host, port), handler)
            thread = threading.Thread(
                target=_server.serve_forever, name="paw-sites-local", daemon=True
            )
            thread.start()
        host, port = _server.server_address[0], _server.server_address[1]
    return f"http://{host}:{port}"


def local_url_for(site_id: str) -> str:
    """The localhost URL a locally-deployed site is served at (trailing slash so
    SimpleHTTPRequestHandler serves the dir's index.html)."""
    return f"{ensure_server()}/{site_id}/"


def deploy_local(site_id: str, project_dir: str, *, engine: str = "ripple") -> str:
    """Persist the built site and return its served localhost URL. The one call
    the service makes in local mode in place of cf.put_worker().

    ``engine`` plus the artifact on disk select the static-output dir
    (``resolve_static_output_rel``): ripple and DYNAMIC svelte serve
    ``.svelte-kit/cloudflare``; a STATIC svelte landing site serves ``build``; html
    serves the project root (its raw static tree). The default (``"ripple"``) preserves
    the exact prior behaviour.

    Fails SOFT on a missing build dir (P1a): if the static output is not present (a
    build that produced no output), this does NOT raise a bare FileNotFoundError →
    500. When a PRIOR deploy of the same site is still on disk it keeps that deploy
    and returns its URL with a logged warning (serve the last-good site rather than
    break the page); only when there is no prior deploy EITHER does it raise
    ``MissingBuildOutput`` — a clear, typed error the caller can surface."""
    src = Path(project_dir, resolve_static_output_rel(project_dir, engine))
    if not src.is_dir():
        dest = sites_home() / site_id
        if dest.is_dir():
            logger.warning(
                "sites: no fresh build at %s — serving the PRIOR deploy at %s "
                "(the build produced no static output)",
                src,
                dest,
            )
            return local_url_for(site_id)
        raise MissingBuildOutput(
            f"no built static site at {src} and no prior deploy for {site_id} — "
            "the build produced no static output to serve."
        )
    persist_site(site_id, project_dir, engine)
    return local_url_for(site_id)


# --- the artifact preview -------------------------------------------------


def preview_url_for(site_id: str) -> str:
    """The localhost URL a site's unpacked build artifact is previewed at.

    Trailing slash on purpose: the page's own asset links are relative (``./_app/...``,
    ``./assets/...``) and resolve one level too high without it, so the preview would
    open with no CSS and no JS. ``resolve`` also redirects the slashless form, but the
    URL we hand out should not need the redirect."""
    return f"{ensure_server()}/{artifact_preview.PREVIEW_URL_PREFIX}/{site_id}/"


def serve_artifact_preview(site_id: str, artifact: bytes | None, *, engine: str) -> str | None:
    """Store a build artifact as this site's preview and return its URL, or ``None``.

    The artifact-lane peer of :func:`deploy_local`. NEVER RAISES — an artifact that
    cannot be previewed faithfully, one that could not be unpacked, or a server that
    could not start, all return ``None`` and are logged. That is the whole difference
    between the two functions: ``deploy_local`` is the publish itself and must report its
    failures, while a preview is a convenience and must not be able to fail a publish
    that already succeeded.

    ``engine`` selects nothing about the LAYOUT — the lane tars from the resolved
    static-output dir, so every artifact already arrives rooted at its deployable root.
    It is passed through for the server-entry cross-check in ``store_artifact``.

    GATED, via ``truth_lane.open_preview``. It used to store whatever unpacked, which
    meant an ``adapter-cloudflare`` artifact — whose pages come from a ``_worker.js``
    nothing here can execute — had its static leftovers served as though they were the
    site. That is the failure mode a verification preview must not have. The function had
    no production caller when the gate landed, and closing it then rather than later is
    the point: whoever eventually wires the obvious-looking "serve an artifact preview"
    front door has no reason to suspect a gate exists elsewhere that they are skipping.

    A caller that wants the REASON for a refusal should call ``truth_lane.open_preview``
    directly — this signature returns ``str | None`` and cannot carry one. ``None`` has
    always meant "no preview", which is why gating changed no contract here.

    SL-1's caveat here said the server-entry cross-check should ask
    ``resolve_emits_server_worker`` against the build dir once the lane had a caller. IT
    NOW HAS ONE (``sites/build_job.py``, SL-2 slice 2) and the resolver turned out to be
    the wrong instrument FOR THIS PATH: on the Daytona lane the build happens inside a
    sandbox that deletes itself, so no local process ever holds the built project dir to
    resolve against. All that comes back is the artifact.

    So the two halves answer it from different evidence, and neither consults an engine
    name. ``truth_lane`` scans the tar's member names to decide previewability.
    ``store_artifact`` asks ``engines.expects_server_worker`` for its cross-check, a
    tri-state returning ``None`` for svelte because the name genuinely cannot say which
    adapter ran — "either shape is legitimate" being the honest answer rather than a
    warning on every healthy static build. The ``resolve_*`` form stays right for the
    LOCAL builder path, which does hold a project dir (see ``workers_deploy``)."""
    # Imported inside the function: ``truth_lane`` reaches back here for the loopback base
    # URL, so a module-level import would close the cycle.
    from pocketpaw_ee.sites import truth_lane

    try:
        return truth_lane.open_preview(site_id, artifact, engine=engine).url
    except Exception:  # noqa: BLE001 — open_preview is total, so this is belt-and-braces
        logger.warning(
            "sites: could not serve an artifact preview for site %s", site_id, exc_info=True
        )
        return None


__all__ = [
    "sites_home",
    "persist_site",
    "ensure_server",
    "local_url_for",
    "deploy_local",
    "preview_url_for",
    "serve_artifact_preview",
    "MissingBuildOutput",
]
