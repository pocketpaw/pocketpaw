# ee/pocketpaw_ee/sites/local_server.py — LOCAL static file server for the
# Sites local-deploy mode (Phase 3). With no Cloudflare creds, publish() builds
# the static site and "deploys" it by copying the prerendered
# `.svelte-kit/cloudflare/` tree under a stable per-site dir; this module serves
# that tree over HTTP so the published site has a real openable localhost URL
# (what the cmux smoke + Phase 5 open).
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

import os
import shutil
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# The control plane reads the deployable static site adapter-cloudflare emits
# here (same dir the real path reads _worker.js from).
_CLOUDFLARE_BUILD_REL = ".svelte-kit/cloudflare"


def sites_home() -> Path:
    """Root dir the local server serves and where built sites are persisted.
    ~/.pocketpaw/sites by default; override with PAW_SITES_LOCAL_DIR (tests use
    a temp dir so they never write the real home)."""
    raw = os.environ.get("PAW_SITES_LOCAL_DIR")
    base = Path(raw) if raw else Path.home() / ".pocketpaw" / "sites"
    base.mkdir(parents=True, exist_ok=True)
    return base


def persist_site(site_id: str, project_dir: str) -> Path:
    """Copy the built static site (.svelte-kit/cloudflare/) into the stable
    per-site dir <home>/<site_id>/ and return that dir. Replaces any prior
    deploy of the same site so a re-publish serves fresh content."""
    src = Path(project_dir, _CLOUDFLARE_BUILD_REL)
    if not src.is_dir():
        raise FileNotFoundError(f"no built static site at {src}")
    dest = sites_home() / site_id
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    return dest


# --- the singleton server -------------------------------------------------

_server: ThreadingHTTPServer | None = None
_lock = threading.Lock()


class _QuietHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler that doesn't spam stderr with a line per GET."""

    def log_message(self, *args: object) -> None:  # noqa: D401 - silence access log
        pass


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


def deploy_local(site_id: str, project_dir: str) -> str:
    """Persist the built site and return its served localhost URL. The one call
    the service makes in local mode in place of cf.put_worker()."""
    persist_site(site_id, project_dir)
    return local_url_for(site_id)


__all__ = ["sites_home", "persist_site", "ensure_server", "local_url_for", "deploy_local"]
