# tests/ee/sites/test_artifact_preview.py — the rules of the static artifact preview:
# what gets unpacked, what is refused, and what a request resolves to.
#
# Created 2026-08-10 (SG-10, re-aimed). These are the unit half; the HTTP half and the
# publish-isolation proof live in test_artifact_preview_server.py.
#
# The tarballs here are shaped like the ones MEASURED off the live Daytona lane on
# 2026-08-09 — react's 4-entry `dist` and svelte's 24-entry `.svelte-kit/cloudflare`
# WITH a `_worker.js` — because the two engines produce genuinely different trees and a
# preview that only ever saw one of them would pass while being wrong about the other.
# Member names carry the `./` prefix real `tar -C <dir> .` emits.
#
# Three things here are security assertions rather than behaviour checks, and should be
# read as such: `_worker.js` never lands on disk and never comes back over the wire (it
# has carried a per-site signed key), a tarball member cannot write outside the preview
# root, and a request path cannot read outside it — including percent-encoded.

from __future__ import annotations

import io
import tarfile

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.sites import artifact_preview as ap  # noqa: E402

# The 4-entry react `dist` shape (measured: 61,487 bytes, no node_modules, no worker).
_REACT_ARTIFACT = {
    "./index.html": b"<!doctype html><title>Acme</title><div id=root>hello react</div>",
    "./assets/index-a1b2c3.js": b"console.log('react bundle')",
    "./assets/index-d4e5f6.css": b"body{color:#111}",
    "./vite.svg": b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
}

# The svelte `.svelte-kit/cloudflare` shape (measured: 24 entries, 33,104 bytes,
# `_worker.js` at 4,335 bytes even with `src/routes/api/` deleted).
_SVELTE_ARTIFACT = {
    "./index.html": b"<!doctype html><title>Acme</title><body>hello svelte</body>",
    "./_worker.js": b"export default {fetch(){}} // SIGNED_KEY_WOULD_BE_HERE",
    "./_routes.json": b'{"include":["/*"]}',
    "./.assetsignore": b"_worker.js\n",
    "./_app/version.json": b'{"version":"1"}',
    "./_app/immutable/entry/start.abc.js": b"export const start = 1",
    "./_app/immutable/assets/_layout.def.css": b".x{}",
}


def _tar(
    members: dict[str, bytes],
    *,
    links: dict[str, str] | None = None,
    hardlinks: dict[str, str] | None = None,
) -> bytes:
    """Pack ``members`` (names used VERBATIM) into gzipped tar bytes.

    Names are verbatim so a test can post a hostile one — ``../escape.txt``,
    ``/etc/shadow`` — which is the whole point of the rejection tests. ``links`` adds
    symlink members and ``hardlinks`` adds hardlink members; both are the shapes a
    tarball uses to reach a file the extraction directory does not contain.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        root = tarfile.TarInfo("./")
        root.type = tarfile.DIRTYPE
        tar.addfile(root)
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        for kind, entries in ((tarfile.SYMTYPE, links), (tarfile.LNKTYPE, hardlinks)):
            for name, target in (entries or {}).items():
                info = tarfile.TarInfo(name)
                info.type = kind
                info.linkname = target
                tar.addfile(info)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _preview_home(tmp_path, monkeypatch):
    """Point the preview root at a temp dir so no test writes the real ~/.pocketpaw."""
    monkeypatch.setenv("PAW_SITES_PREVIEW_DIR", str(tmp_path / "site-previews"))
    return tmp_path / "site-previews"


# --- the two engine shapes -------------------------------------------------


def test_react_artifact_previews_from_its_own_root():
    snap = ap.store_artifact("react1", _tar(_REACT_ARTIFACT), engine="react")

    assert snap.unpacked.entries == 4
    assert snap.url_path == "/_preview/react1/"

    index = ap.resolve("react1", "/")
    assert index.status == 200
    assert b"hello react" in index.body
    assert index.content_type == "text/html; charset=utf-8"

    js = ap.resolve("react1", "/assets/index-a1b2c3.js")
    assert js.status == 200
    assert js.body == b"console.log('react bundle')"
    assert js.content_type == "text/javascript; charset=utf-8"


def test_svelte_artifact_previews_and_its_deeper_tree_resolves():
    """The engines emit 4 vs 24 entries; nothing here knows which. The lane tars from
    ``static_output_rel(engine)``, so both arrive rooted at their deployable root."""
    ap.store_artifact("svelte1", _tar(_SVELTE_ARTIFACT), engine="svelte")

    index = ap.resolve("svelte1", "/")
    assert index.status == 200
    assert b"hello svelte" in index.body

    nested = ap.resolve("svelte1", "/_app/immutable/entry/start.abc.js")
    assert nested.status == 200
    assert nested.content_type == "text/javascript; charset=utf-8"


# --- _worker.js ------------------------------------------------------------


def test_worker_entry_is_never_written_and_never_served(_preview_home):
    snap = ap.store_artifact("svelte2", _tar(_SVELTE_ARTIFACT), engine="svelte")

    assert snap.unpacked.server_entries == ("_worker.js",)
    assert not (snap.root / "_worker.js").exists()

    refused = ap.resolve("svelte2", "/_worker.js")
    assert refused.status == 404
    assert refused.reason == "server_entry_refused"
    assert b"SIGNED_KEY_WOULD_BE_HERE" not in refused.body


def test_the_worker_does_not_break_the_rest_of_the_preview():
    """The stated risk: its presence must neither be executed nor 404 everything else."""
    ap.store_artifact("svelte3", _tar(_SVELTE_ARTIFACT), engine="svelte")

    assert ap.resolve("svelte3", "/").status == 200
    assert ap.resolve("svelte3", "/_app/version.json").status == 200


def test_a_worker_directory_is_refused_wholesale():
    """adapter-cloudflare emits `_worker.js` as a DIRECTORY of chunks for larger apps,
    so matching the leaf filename alone would let the chunks through."""
    snap = ap.store_artifact(
        "svelte4",
        _tar(
            {
                "./index.html": b"<title>x</title>ok",
                "./_worker.js/index.js": b"// server shell",
                "./_worker.js/chunks/0.js": b"// chunk",
            }
        ),
        engine="svelte",
    )

    assert not (snap.root / "_worker.js").exists()
    assert sorted(snap.unpacked.server_entries) == ["_worker.js/chunks/0.js", "_worker.js/index.js"]
    assert ap.resolve("svelte4", "/_worker.js/index.js").reason == "server_entry_refused"


def test_worker_source_is_refused_even_when_it_is_on_disk():
    """Defence in depth: the unpack skip is the primary guard, but a tree unpacked some
    other way must still not hand back the server bundle's source."""
    snap = ap.store_artifact("svelte5", _tar(_REACT_ARTIFACT), engine="svelte")
    (snap.root / "_worker.js").write_bytes(b"const KEY='paw_sk_live_deadbeef'")

    got = ap.resolve("svelte5", "/_worker.js")
    assert got.status == 404
    assert b"paw_sk_live_deadbeef" not in got.body


def test_deploy_metadata_is_not_served_as_content():
    snap = ap.store_artifact("svelte6", _tar(_SVELTE_ARTIFACT), engine="svelte")

    assert sorted(snap.unpacked.metadata_entries) == [".assetsignore", "_routes.json"]
    assert ap.resolve("svelte6", "/_routes.json").reason == "metadata_refused"
    assert ap.resolve("svelte6", "/.assetsignore").reason == "metadata_refused"


# --- no backend ------------------------------------------------------------


def test_api_submit_is_refused_visibly_not_faked():
    ap.store_artifact("form1", _tar(_REACT_ARTIFACT), engine="react")

    posted = ap.resolve("form1", "/api/submit", method="POST")
    assert posted.status == 501
    assert posted.reason == "backend_not_served"
    assert not posted.ok
    assert b"form submissions" in posted.body
    # A redirect would read as a successful submission in the browser's address bar.
    assert posted.location is None

    # GET of the same path is refused identically — no "endpoint exists" signal.
    assert ap.resolve("form1", "/api/submit").status == 501


def test_a_post_to_the_page_itself_is_refused():
    """A form with no `action` posts to the current URL, so refusing only `/api/*`
    would leave that case returning the page with a 200."""
    ap.store_artifact("form2", _tar(_REACT_ARTIFACT), engine="react")

    got = ap.resolve("form2", "/", method="POST")
    assert got.status == 405
    assert got.reason == "method_not_allowed"
    assert b"hello react" not in got.body


def test_the_backend_refusal_does_not_depend_on_a_stored_artifact():
    """Policy before existence: the honest answer to a form POST is the same whether or
    not a build happens to be stored."""
    got = ap.resolve("never-built", "/api/submit", method="POST")
    assert got.status == 501
    assert got.reason == "backend_not_served"


# --- hostile tarballs ------------------------------------------------------


def test_traversal_members_are_refused_and_write_nothing_outside_the_root(_preview_home):
    outside = _preview_home.parent / "escaped.txt"
    snap = ap.store_artifact(
        "eve1",
        _tar(
            {
                "./index.html": b"<title>x</title>ok",
                "../escaped.txt": b"pwned",
                "../../escaped-deeper.txt": b"pwned",
                "/tmp/absolute.txt": b"pwned",
            }
        ),
        engine="react",
    )

    assert len(snap.unpacked.rejected) == 3
    assert not outside.exists()
    assert not (_preview_home.parent / "escaped-deeper.txt").exists()
    # The safe member still landed: a hostile entry does not cost the whole preview.
    assert ap.resolve("eve1", "/").status == 200


def test_link_members_are_refused(_preview_home):
    """A link is how a tarball reaches a file the extraction directory does not contain,
    so links are refused rather than followed — symlinks AND hardlinks. The hardlink
    matters on its own: a hardlink to a member that IS in the archive extracts to real
    content, so without the member-type guard it would be written like any other file.
    """
    snap = ap.store_artifact(
        "eve2",
        _tar(
            {"./index.html": b"<title>x</title>ok"},
            links={"./secrets": "/etc/passwd", "./up": "../.."},
            hardlinks={"./copy.html": "./index.html"},
        ),
        engine="react",
    )

    assert sorted(snap.unpacked.rejected) == ["./copy.html", "./secrets", "./up"]
    assert not (snap.root / "secrets").exists()
    assert not (snap.root / "up").exists()
    assert not (snap.root / "copy.html").exists()
    assert snap.unpacked.entries == 1


def test_a_backslash_member_is_refused():
    """A backslash is a separator on Windows and a legal filename character on POSIX;
    accepting it means the same artifact writes to two different places."""
    snap = ap.store_artifact(
        "eve3",
        _tar({"./index.html": b"<title>x</title>ok", "..\\..\\escaped.txt": b"pwned"}),
        engine="react",
    )
    assert snap.unpacked.rejected == ("..\\..\\escaped.txt",)


def test_too_many_entries_is_refused_and_leaves_no_partial_tree(monkeypatch, _preview_home):
    monkeypatch.setattr(ap, "MAX_ENTRIES", 2)

    with pytest.raises(ap.ArtifactTooLarge):
        ap.store_artifact("bomb1", _tar(_REACT_ARTIFACT), engine="react")

    assert not (_preview_home / "bomb1").exists()
    assert list(_preview_home.glob("*")) == []


def test_too_many_bytes_is_refused(monkeypatch, _preview_home):
    monkeypatch.setattr(ap, "MAX_TOTAL_BYTES", 16)

    with pytest.raises(ap.ArtifactTooLarge):
        ap.store_artifact("bomb2", _tar(_REACT_ARTIFACT), engine="react")

    assert not (_preview_home / "bomb2").exists()


def test_bytes_that_are_not_a_tarball_are_refused(_preview_home):
    with pytest.raises(ap.ArtifactUnreadable):
        ap.store_artifact("junk1", b"this is not a gzipped tar", engine="react")
    assert not (_preview_home / "junk1").exists()


# --- hostile request paths -------------------------------------------------


def test_percent_encoded_traversal_cannot_read_outside_the_root(_preview_home):
    ap.store_artifact("eve4", _tar(_REACT_ARTIFACT), engine="react")
    (_preview_home / "secret.txt").write_bytes(b"tenant secret")

    for path in ("/../secret.txt", "/..%2fsecret.txt", "/%2e%2e/secret.txt", "/a/../../secret.txt"):
        got = ap.resolve("eve4", path)
        assert got.status == 404, path
        assert b"tenant secret" not in got.body, path


def test_a_drive_qualified_request_path_cannot_read_outside_the_root(_preview_home):
    """pathlib treats ``C:`` as an anchor, so joining it onto the root RESETS the path
    and the read lands wherever the segment points. Refused at the parser."""
    ap.store_artifact("eve5", _tar(_REACT_ARTIFACT), engine="react")
    secret = _preview_home.parent / "outside.txt"
    secret.write_bytes(b"tenant secret")

    for path in (
        "/" + secret.as_posix(),
        "/C:/Windows/win.ini",
        "/c:",
    ):
        got = ap.resolve("eve5", path)
        assert got.status == 404, path
        assert b"tenant secret" not in got.body, path


def test_an_unsafe_site_id_cannot_climb_out_of_the_preview_root():
    for site_id in ("..", "../..", "a/b", "", "a\\b", "site id"):
        got = ap.resolve(site_id, "/")
        assert got.status == 404
        assert got.reason == "bad_site_id"


def test_a_directory_is_never_listed():
    """A listing would hand the build tree's shape to anyone holding the preview URL."""
    ap.store_artifact("dir1", _tar(_REACT_ARTIFACT), engine="react")

    got = ap.resolve("dir1", "/assets/")
    assert got.status == 404
    assert b"index-a1b2c3.js" not in got.body


def test_a_missing_asset_is_a_404_and_not_the_index():
    """No SPA fallback: rewriting a missing asset to index.html would report a broken
    build as a working page."""
    ap.store_artifact("miss1", _tar(_REACT_ARTIFACT), engine="react")

    got = ap.resolve("miss1", "/assets/index-deleted.js")
    assert got.status == 404
    assert got.reason == "not_found"
    assert b"hello react" not in got.body


def test_a_site_with_no_stored_build_says_so():
    got = ap.resolve("nothing1", "/")
    assert got.status == 404
    assert got.reason == "no_preview"


# --- serving details that break a page silently ---------------------------


def test_the_slashless_url_redirects_rather_than_serving_the_index():
    """Served at `/_preview/<id>` the page's own `./assets/x.js` resolves to
    `/_preview/assets/x.js` — it renders with no CSS and no JS and looks 'plain'."""
    ap.store_artifact("slash1", _tar(_REACT_ARTIFACT), engine="react")

    got = ap.resolve("slash1", "")
    assert got.status == 301
    assert got.location == "/"

    nested = ap.resolve("slash1", "/assets")
    assert nested.status == 301
    assert nested.location == "/assets/"


def test_javascript_is_typed_executably_even_when_mimetypes_says_text_plain(monkeypatch):
    """The stdlib reads the Windows registry, where `.js` has been observed mapping to
    text/plain. Served that way the browser refuses to run the bundle and the previewed
    page never hydrates — with every other signal saying the preview worked."""
    monkeypatch.setattr(ap.mimetypes, "guess_type", lambda *_a, **_k: ("text/plain", None))

    assert ap.content_type_for("index-a1b2c3.js") == "text/javascript; charset=utf-8"
    assert ap.content_type_for("app.mjs") == "text/javascript; charset=utf-8"
    assert ap.content_type_for("style.css") == "text/css; charset=utf-8"
    assert ap.content_type_for("font.woff2") == "font/woff2"
    # Not in the table → the stdlib still gets its say.
    assert ap.content_type_for("weird.zzz") == "text/plain"


def test_a_preview_is_never_cached():
    ap.store_artifact("cache1", _tar(_REACT_ARTIFACT), engine="react")

    got = ap.resolve("cache1", "/")
    assert ("Cache-Control", "no-store") in got.headers
    assert ("X-Content-Type-Options", "nosniff") in got.headers


def test_restoring_replaces_the_previous_build_completely():
    ap.store_artifact("re1", _tar(_REACT_ARTIFACT), engine="react")
    assert ap.resolve("re1", "/assets/index-a1b2c3.js").status == 200

    ap.store_artifact(
        "re1",
        _tar({"./index.html": b"<title>x</title>second build"}),
        engine="react",
    )

    assert b"second build" in ap.resolve("re1", "/").body
    # A leftover file from the previous build would serve a mixture of two builds.
    assert ap.resolve("re1", "/assets/index-a1b2c3.js").status == 404


# --- engine capability, via the predicates --------------------------------


def test_an_engine_with_no_build_subdir_is_refused(_preview_home):
    """`html` runs no build, so it produces no artifact. Mirrors the same guard in
    `daytona_build.artifact_tar_command` — an artifact arriving for it is a routing
    bug and should be loud rather than previewed."""
    with pytest.raises(ap.ArtifactRejected):
        ap.store_artifact("html1", _tar(_REACT_ARTIFACT), engine="html")
    assert not (_preview_home / "html1").exists()


def test_an_unexpected_server_entry_is_reported(caplog):
    """react emits no server entry. One turning up means the lane changed shape, which
    is worth saying out loud — but not worth refusing to preview over."""
    with caplog.at_level("WARNING"):
        snap = ap.store_artifact("mix1", _tar(_SVELTE_ARTIFACT), engine="react")

    assert snap.unpacked.server_entries == ("_worker.js",)
    assert "even though this engine emits none" in caplog.text
    assert ap.resolve("mix1", "/").status == 200


def test_a_missing_server_entry_is_reported_for_svelte(caplog):
    with caplog.at_level("WARNING"):
        ap.store_artifact("mix2", _tar(_REACT_ARTIFACT), engine="svelte")

    assert "carried NO server entry" in caplog.text


# --- the publish-safe wrapper ---------------------------------------------


def test_safe_store_never_raises(caplog, _preview_home):
    with caplog.at_level("WARNING"):
        assert ap.safe_store_artifact("safe1", b"not a tarball", engine="react") is None
        assert ap.safe_store_artifact("safe1", None, engine="react") is None
        assert ap.safe_store_artifact("../escape", b"x", engine="react") is None

    assert not (_preview_home / "safe1").exists()
    assert ap.safe_store_artifact("safe2", _tar(_REACT_ARTIFACT), engine="react") is not None


def test_has_preview_and_discard():
    assert ap.has_preview("life1") is False
    ap.store_artifact("life1", _tar(_REACT_ARTIFACT), engine="react")
    assert ap.has_preview("life1") is True
    assert ap.discard_preview("life1") is True
    assert ap.has_preview("life1") is False
    assert ap.has_preview("../escape") is False
