# tests/ee/sites/test_artifact_preview_server.py — the artifact preview over real HTTP,
# and the proof that a publish does not depend on it.
#
# Created 2026-08-10 (SG-10, re-aimed). test_artifact_preview.py covers the rules; this
# file covers the two things only a running server can show:
#
#   1. A BUILT ARTIFACT IS ACTUALLY PREVIEWABLE with no Node runtime, no dev server and
#      no cross-origin isolation — the page and its bundle come back over HTTP from an
#      unpacked tarball, and the refusals hold on the wire (not just in a return value).
#   2. PUBLISH IS UNAFFECTED WHEN PREVIEW FAILS, demonstrated rather than asserted in
#      prose. A local deploy is served, the preview is then broken three different ways
#      (unreadable artifact, a resolver that raises, no artifact at all), and the
#      deployed site is re-fetched after each one and still answers 200.
#
# The deploy and preview roots being disjoint is checked here too: it is what stops the
# static branch serving a preview tree without passing `resolve`'s guards, which would
# give the `_worker.js` refusal a bypass.

from __future__ import annotations

import io
import tarfile
import urllib.error
import urllib.request

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.sites import artifact_preview as ap  # noqa: E402

_ARTIFACT = {
    "./index.html": b"<!doctype html><title>Acme</title><script src=./assets/app.js></script>ok",
    "./assets/app.js": b"console.log('bundle')",
    "./_worker.js": b"export default {fetch(){}} // SIGNED_KEY_WOULD_BE_HERE",
}


def _tar(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def _get(url: str, *, method: str = "GET", data: bytes | None = None):
    """Fetch ``url`` and return ``(status, body, headers)``, treating an HTTP error as a
    result rather than an exception — every refusal here is a status code we assert on."""
    req = urllib.request.Request(url, data=data, method=method)  # noqa: S310 - localhost
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - localhost
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A freshly-rooted local static server, torn down afterwards.

    The server is a process singleton whose served root is captured when it starts, so
    a test that only set the env var would be served by whatever root a previous test
    started with. Resetting the singleton is what makes the roots in this file real."""
    monkeypatch.setenv("PAW_SITES_LOCAL_DIR", str(tmp_path / "sites"))
    monkeypatch.setenv("PAW_SITES_PREVIEW_DIR", str(tmp_path / "site-previews"))

    from pocketpaw_ee.sites import local_server

    previous = local_server._server
    local_server._server = None
    try:
        yield local_server
    finally:
        started = local_server._server
        if started is not None:
            started.shutdown()
            started.server_close()
        local_server._server = previous


def _react_project(tmp_path):
    """A built react project dir: `dist/index.html`, what `deploy_local` copies."""
    dist = tmp_path / "project" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_bytes(b"<!doctype html><title>Live</title>the deployed site")
    return tmp_path / "project"


# --- the preview, over the wire -------------------------------------------


def test_a_built_artifact_is_previewable_over_http(server):
    url = server.serve_artifact_preview("site1", _tar(_ARTIFACT), engine="svelte")
    assert url is not None and url.endswith("/_preview/site1/")

    status, body, headers = _get(url)
    assert status == 200
    assert b"<title>Acme</title>" in body
    assert headers["Cache-Control"] == "no-store"
    # No cross-origin isolation is needed for this path, and none is claimed.
    assert "Cross-Origin-Embedder-Policy" not in headers
    assert "Cross-Origin-Opener-Policy" not in headers

    status, body, headers = _get(url + "assets/app.js")
    assert status == 200
    assert body == b"console.log('bundle')"
    assert headers["Content-Type"] == "text/javascript; charset=utf-8"


def test_the_slashless_preview_url_still_lands_on_the_page(server):
    server.serve_artifact_preview("site2", _tar(_ARTIFACT), engine="svelte")
    base = server.ensure_server()

    status, body, _ = _get(f"{base}/_preview/site2")
    assert status == 200
    assert b"<title>Acme</title>" in body


def test_the_worker_is_not_reachable_over_http(server):
    url = server.serve_artifact_preview("site3", _tar(_ARTIFACT), engine="svelte")

    status, body, _ = _get(url + "_worker.js")
    assert status == 404
    assert b"SIGNED_KEY_WOULD_BE_HERE" not in body


def test_a_form_post_in_preview_is_refused_visibly(server):
    url = server.serve_artifact_preview("site4", _tar(_ARTIFACT), engine="svelte")

    status, body, _ = _get(url + "api/submit", method="POST", data=b"email=a%40b.test")
    assert status == 501
    assert b"form submissions" in body

    # And a form with no action, which posts to the page itself.
    status, body, _ = _get(url, method="POST", data=b"email=a%40b.test")
    assert status == 405
    assert b"the deployed site" not in body


def test_head_returns_the_headers_without_the_body(server):
    url = server.serve_artifact_preview("site5", _tar(_ARTIFACT), engine="svelte")

    status, body, headers = _get(url, method="HEAD")
    assert status == 200
    assert body == b""
    assert headers["Content-Type"] == "text/html; charset=utf-8"


def test_an_unbuilt_site_previews_as_a_404(server):
    base = server.ensure_server()
    status, _, _ = _get(f"{base}/_preview/never-built/")
    assert status == 404


# --- publish is unaffected when preview fails -----------------------------


def test_a_deploy_keeps_serving_when_the_artifact_cannot_be_unpacked(server, tmp_path):
    site_url = server.deploy_local("site6", str(_react_project(tmp_path)), engine="react")
    assert _get(site_url)[1] == b"<!doctype html><title>Live</title>the deployed site"

    # An unreadable artifact: the failure a truncated download or a wrong file gives.
    assert server.serve_artifact_preview("site6", b"not a gzipped tar", engine="react") is None
    assert server.serve_artifact_preview("site6", None, engine="react") is None

    status, body, _ = _get(site_url)
    assert status == 200
    assert body == b"<!doctype html><title>Live</title>the deployed site"
    assert not (ap.previews_home() / "site6").exists()


def test_a_deploy_keeps_serving_when_the_resolver_itself_raises(server, tmp_path, monkeypatch):
    """The harder half. A store failure is caught by design; an exception thrown while
    ANSWERING a preview request runs on the same server thread that serves live local
    deploys, so it has to be contained to the one request."""
    site_url = server.deploy_local("site7", str(_react_project(tmp_path)), engine="react")
    preview_url = server.serve_artifact_preview("site7", _tar(_ARTIFACT), engine="react")
    assert _get(preview_url)[0] == 200

    def _boom(*_a, **_k):
        raise RuntimeError("preview resolution exploded")

    working = ap.resolve
    monkeypatch.setattr(ap, "resolve", _boom)

    assert _get(preview_url)[0] == 500
    status, body, _ = _get(site_url)
    assert status == 200
    assert body == b"<!doctype html><title>Live</title>the deployed site"

    # And the server is still alive for the next preview once the fault clears.
    # Restored by hand rather than with monkeypatch.undo(), which would also revert the
    # PAW_SITES_PREVIEW_DIR the `server` fixture set on the same shared MonkeyPatch and
    # point the preview root back at the real home.
    monkeypatch.setattr(ap, "resolve", working)
    assert _get(preview_url)[0] == 200


def test_the_preview_root_is_outside_the_served_deploy_root(server, tmp_path):
    """If a preview tree sat under `sites_home()` the static branch would serve it as
    plain files — `_worker.js` included — without passing any of `resolve`'s guards."""
    server.serve_artifact_preview("site8", _tar(_ARTIFACT), engine="svelte")

    sites_root = server.sites_home().resolve()
    previews_root = ap.previews_home().resolve()
    assert not previews_root.is_relative_to(sites_root)
    assert not (sites_root / "site8").exists()

    # The static branch has no route to the preview tree.
    base = server.ensure_server()
    assert _get(f"{base}/site8/")[0] == 404
    assert _get(f"{base}/site8/index.html")[0] == 404


def test_a_preview_does_not_disturb_a_prior_deploy_of_the_same_site(server, tmp_path):
    """Same site id on both branches. The two roots must not alias, or a preview of a
    new build would overwrite the live local deploy of the old one."""
    site_url = server.deploy_local("site9", str(_react_project(tmp_path)), engine="react")
    server.serve_artifact_preview("site9", _tar(_ARTIFACT), engine="svelte")

    assert _get(site_url)[1] == b"<!doctype html><title>Live</title>the deployed site"
    assert b"<title>Acme</title>" in _get(server.preview_url_for("site9"))[1]
