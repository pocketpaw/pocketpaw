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
# root, and a request path cannot read outside it.
#
# Updated 2026-08-10 (`store_artifact` is gated): the six calls here that store a
# WORKER-BEARING artifact now go through `store_unvouched_artifact`, the explicitly-named
# seam past the gate. That is not a workaround — those six are precisely the tests of the
# un-vouched primitive's behaviour (the worker is skipped at unpack, its whole subtree
# goes with it, deploy metadata is skipped, the shape mismatch is logged), and they need a
# tree containing the thing being skipped. Every assertion is unchanged; only the door
# they come in through moved. `store_artifact` refusing such an artifact outright is
# asserted separately, in test_truth_lane.py.
#
# Updated 2026-08-10: path traversal covered end to end after the lead flagged it as
# missing from the brief. Both surfaces, since a static server over an extracted archive
# has two: MEMBER names at extract time (`..`, absolute, drive letter, backslash, NUL,
# symlink, hardlink) and REQUEST paths at read time (the same, plus percent-encoded
# forms, plus a symlink planted inside the tree — the case no string check can see, and
# the reason containment runs on the RESOLVED path).

from __future__ import annotations

import io
import os
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
    ap.store_unvouched_artifact("svelte1", _tar(_SVELTE_ARTIFACT), engine="svelte")

    index = ap.resolve("svelte1", "/")
    assert index.status == 200
    assert b"hello svelte" in index.body

    nested = ap.resolve("svelte1", "/_app/immutable/entry/start.abc.js")
    assert nested.status == 200
    assert nested.content_type == "text/javascript; charset=utf-8"


# --- _worker.js ------------------------------------------------------------


def test_worker_entry_is_never_written_and_never_served(_preview_home):
    snap = ap.store_unvouched_artifact("svelte2", _tar(_SVELTE_ARTIFACT), engine="svelte")

    assert snap.unpacked.server_entries == ("_worker.js",)
    assert not (snap.root / "_worker.js").exists()

    refused = ap.resolve("svelte2", "/_worker.js")
    assert refused.status == 404
    assert refused.reason == "server_entry_refused"
    assert b"SIGNED_KEY_WOULD_BE_HERE" not in refused.body


def test_the_worker_does_not_break_the_rest_of_the_preview():
    """The stated risk: its presence must neither be executed nor 404 everything else."""
    ap.store_unvouched_artifact("svelte3", _tar(_SVELTE_ARTIFACT), engine="svelte")

    assert ap.resolve("svelte3", "/").status == 200
    assert ap.resolve("svelte3", "/_app/version.json").status == 200


def test_a_worker_directory_is_refused_wholesale():
    """adapter-cloudflare emits `_worker.js` as a DIRECTORY of chunks for larger apps,
    so matching the leaf filename alone would let the chunks through."""
    snap = ap.store_unvouched_artifact(
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
    # The canary must be SECRET-SHAPED (so the assertion means something) without
    # matching a real credential pattern. It previously embedded a Stripe-secret-shaped
    # literal, which tripped the pr-quality-gate secret scan and failed CI on a test
    # whose entire point is that the string is NOT served. Renamed rather than
    # allow-listed: a scanner exemption on a sites test would outlive the reason for it.
    #
    # AND DO NOT QUOTE THE OLD VALUE HERE. The first attempt at this comment spelled it
    # out to explain the fix, which re-introduced the very literal the rename removed and
    # failed the scan again — the scanner reads the diff, and a comment is diff too.
    # Describe the shape, never reproduce it.
    (snap.root / "_worker.js").write_bytes(b"const KEY='paw_canary_notacredential_deadbeef'")

    got = ap.resolve("svelte5", "/_worker.js")
    assert got.status == 404
    assert b"paw_canary_notacredential_deadbeef" not in got.body


def test_deploy_metadata_is_not_served_as_content():
    snap = ap.store_unvouched_artifact("svelte6", _tar(_SVELTE_ARTIFACT), engine="svelte")

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


def test_a_nul_byte_member_name_is_refused():
    """Asserted against the name parser directly, and here is why that is the honest
    place for it: the tar format's name field is NUL-TERMINATED, so an embedded NUL
    cannot ride an archive at all. Verified — writing a member called
    ``./safe.html\\x00/../../escaped.txt`` and reading the archive back yields
    ``./safe.html``; the tail is gone before any of our code sees it.

    So this guard is unreachable through a tarball, and a test that packed one would
    prove nothing while looking like it proved something. It is kept because
    ``_normalized_member_path`` is a name parser and refusing a NUL is a property of the
    parser, not of tar — a NUL truncates the name for any C-level consumer, which is how
    a name passes a suffix check and opens a different file."""
    assert ap._normalized_member_path("./safe.html\x00/../../escaped.txt") is None
    assert ap._normalized_member_path("./a\x00b") is None
    assert ap._normalized_member_path("./safe.html") == ("safe.html",)


def test_a_drive_qualified_member_name_is_refused():
    """Asserted at the parser rather than by packing one, deliberately: with the guard
    removed, ``C:/evil.txt`` joins to an ANCHORED path and the unpacker would attempt a
    real write to the drive root. A test that has to succeed at writing outside the
    sandbox to prove a guard works is a test that damages the machine when the guard
    breaks."""
    assert ap._normalized_member_path("C:/evil.txt") is None
    assert ap._normalized_member_path("d:/evil.txt") is None
    assert ap._normalized_member_path("./assets/app.js") == ("assets", "app.js")


def test_a_nul_truncated_member_still_cannot_escape(_preview_home):
    """The consequence of the truncation above: what actually lands is the truncated
    name, which must still be a safe single segment inside the root."""
    snap = ap.store_artifact(
        "eve6",
        _tar(
            {
                "./index.html": b"<title>x</title>ok",
                "./safe.html\x00/../../escaped.txt": b"pwned",
            }
        ),
        engine="react",
    )

    assert (snap.root / "safe.html").read_bytes() == b"pwned"
    assert not (_preview_home.parent / "escaped.txt").exists()
    assert not (_preview_home / "escaped.txt").exists()


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
        # `unsafe_path`, so the PARSER refused it. Asserting only the 404 was not
        # enough: with the parser's `..` check disabled, the post-join containment
        # check refuses the same request with `escaped_root` and a status-only
        # assertion still passes. That is a mutation this suite would have shipped.
        assert got.reason == "unsafe_path", path


def test_a_nul_byte_in_a_request_path_is_refused(_preview_home):
    """A NUL can make an extension or suffix check agree with a different file than the
    one that ends up opened."""
    ap.store_artifact("eve7", _tar(_REACT_ARTIFACT), engine="react")
    (_preview_home / "secret.txt").write_bytes(b"tenant secret")

    for path in ("/index.html\x00.png", "/\x00../secret.txt", "/index.html%00.png"):
        got = ap.resolve("eve7", path)
        assert got.status == 404, path
        assert got.reason == "unsafe_path", path
        assert b"tenant secret" not in got.body, path


def test_a_backslash_in_a_request_path_is_refused():
    ap.store_artifact("eve8", _tar(_REACT_ARTIFACT), engine="react")

    for path in ("\\index.html", "/..\\..\\secret.txt", "/%5c..%5csecret.txt"):
        got = ap.resolve("eve8", path)
        assert got.status == 404, path
        assert got.reason == "unsafe_path", path


def test_a_symlink_inside_the_tree_cannot_read_outside_it(tmp_path, _preview_home):
    """The case a string check cannot see. Every segment here is innocent — no ``..``,
    no encoding, no separator — and the path still leaves the root, because one segment
    on disk is a link. Only comparing the RESOLVED path against the resolved root
    refuses it.

    A link cannot arrive via an artifact (link members are refused at unpack), so this
    plants one directly. That is the point: the containment check is what holds when the
    tree contains something the unpacker did not put there.
    """
    snap = ap.store_artifact("eve9", _tar(_REACT_ARTIFACT), engine="react")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"tenant secret")
    try:
        os.symlink(outside, snap.root / "shared", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - platform-gated
        pytest.skip(f"cannot create a symlink on this platform: {exc}")

    got = ap.resolve("eve9", "/shared/secret.txt")
    assert got.status == 404
    # `escaped_root`, not `unsafe_path` — this refusal came from containment after the
    # join, which is the only layer that can see a link. Asserting the specific reason is
    # what stops a broken parser guard from looking verified because this check caught it.
    assert got.reason == "escaped_root"
    assert b"tenant secret" not in got.body


def test_containment_resolves_both_sides(tmp_path):
    """A root that itself sits under a link must still contain its own children —
    otherwise the guard refuses every legitimate request on a platform whose temp dir
    is a link (macOS: /tmp -> /private/tmp)."""
    real_root = tmp_path / "real"
    (real_root / "assets").mkdir(parents=True)
    (real_root / "assets" / "app.js").write_bytes(b"x")
    try:
        os.symlink(real_root, tmp_path / "via-link", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - platform-gated
        pytest.skip(f"cannot create a symlink on this platform: {exc}")

    linked_root = tmp_path / "via-link"
    assert ap._contained(linked_root, linked_root / "assets" / "app.js") is not None
    assert ap._contained(linked_root, linked_root) is not None
    assert ap._contained(linked_root, tmp_path / "elsewhere.txt") is None


def test_a_drive_qualified_request_path_is_refused_at_the_parser():
    """pathlib treats ``C:`` as an anchor, so joining it onto the root RESETS the path
    and the read lands wherever the segment points. Refused at the parser.

    LITERALS ONLY, and that is the fix for a portability bug rather than a style choice.
    This case used to be checked with ``"/" + secret.as_posix()`` for a real file in a
    tmp dir, which is drive-qualified on Windows and NOT on POSIX — there it becomes
    ``//tmp/...``, whose segments are all benign names, so the parser correctly declines
    to flag it and a later layer refuses it instead. The assertion then failed on Linux
    reading ``not_found`` where it wanted ``unsafe_path``. A drive anchor is a
    platform-independent property of the STRING, so it is tested with strings.
    """
    ap.store_artifact("eve5", _tar(_REACT_ARTIFACT), engine="react")

    for path in ("/C:/Windows/win.ini", "/c:", "/D:/x", "/c:/"):
        got = ap.resolve("eve5", path)
        assert got.status == 404, path
        # Refused by the parser, not merely contained after the join — same reason as
        # in the `..` case above. Asserting the specific reason is what makes each
        # layer independently provable (see the module header).
        assert got.reason == "unsafe_path", path


def test_an_absolute_path_to_a_real_file_outside_the_root_cannot_read_it(_preview_home):
    """The security property, checked against a file that genuinely exists outside.

    Kept SEPARATE from the drive-anchor case above because WHICH layer refuses this is
    legitimately platform-dependent: on Windows the string is drive-qualified and the
    parser rejects it; on POSIX every segment is a benign name, the join lands inside
    the root, and it is refused as missing. Both are correct refusals, so this asserts
    the property that must hold on every platform — status 404 and NOT ONE BYTE of the
    outside file — and deliberately does not pin the reason.

    Pinning it here is what broke on Linux, and loosening it in the shared loop would
    have weakened the drive-anchor assertion too. Splitting keeps both strict about the
    thing each can actually promise.
    """
    ap.store_artifact("eve6", _tar(_REACT_ARTIFACT), engine="react")
    secret = _preview_home.parent / "outside.txt"
    secret.write_bytes(b"tenant secret")

    got = ap.resolve("eve6", "/" + secret.as_posix())
    assert got.status == 404
    assert b"tenant secret" not in got.body
    # Refused, by whichever guard owns it on this platform — never served.
    assert got.reason in {"unsafe_path", "escaped_root", "not_found"}


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
        snap = ap.store_unvouched_artifact("mix1", _tar(_SVELTE_ARTIFACT), engine="react")

    assert snap.unpacked.server_entries == ("_worker.js",)
    assert "even though this engine emits none" in caplog.text
    assert ap.resolve("mix1", "/").status == 200


def test_a_missing_server_entry_is_reported_for_ripple(caplog):
    """ripple builds on adapter-cloudflare and always emits a worker, so an artifact
    without one really may be incomplete.

    WAS ``..._for_svelte`` until 2026-08-10 (SL-2 slice 2), and the rename is the point:
    the old test pinned a warning that fires on every HEALTHY static svelte build. SL-1
    moved the svelte track onto two adapters, adapter-static emits no worker, and the
    cross-check was still asking the engine NAME. ripple is the engine the assertion was
    actually describing, so the coverage moves here rather than disappearing."""
    with caplog.at_level("WARNING"):
        ap.store_artifact("mix2", _tar(_REACT_ARTIFACT), engine="ripple")

    assert "carried NO server entry" in caplog.text


def test_a_static_svelte_artifact_is_not_reported_as_incomplete(caplog):
    """The other half, and the reason the rename above is not just bookkeeping. A static
    svelte site legitimately emits no ``_worker.js``, and a warning that fires on correct
    builds is worse than none: it is the SAME line a truncated artifact produces, so it
    trains whoever is on call to scroll past the real one."""
    with caplog.at_level("WARNING"):
        snap = ap.store_artifact("svelte-static1", _tar(_REACT_ARTIFACT), engine="svelte")

    assert snap.unpacked.server_entries == ()
    assert "carried NO server entry" not in caplog.text
    # Not vacuous: the preview still works, so the absence of a warning is not the
    # absence of a stored artifact.
    assert ap.resolve("svelte-static1", "/").status == 200


def test_a_dynamic_svelte_artifact_is_not_reported_either(caplog):
    """The same engine with the OTHER adapter's output. Both shapes are legitimate to
    BUILD, so neither may warn — a check that warned on this one would fire on every
    dynamic site instead of every static one.

    Two separate properties are at play and this pins both, because they pull in
    opposite directions and it would be easy to mistake one for the other:

    * the GATE refuses this artifact outright, since its pages come from a ``_worker.js``
      nothing here can execute;
    * the server-entry CROSS-CHECK still says nothing about it, because the engine name
      cannot tell which adapter ran and "svelte carried a worker" is not an anomaly.

    A refusal is not a warning, and the absence of the warning has to be observable on a
    tree that actually reached disk — hence the seam, which exists for precisely this:
    a test needing to construct the tree ``resolve`` refuses.
    """
    with pytest.raises(ap.ArtifactNotPreviewable):
        ap.store_artifact("svelte-dyn-gated", _tar(_SVELTE_ARTIFACT), engine="svelte")

    with caplog.at_level("WARNING"):
        snap = ap.store_unvouched_artifact("svelte-dyn1", _tar(_SVELTE_ARTIFACT), engine="svelte")

    assert snap.unpacked.server_entries == ("_worker.js",)
    assert "even though this engine emits none" not in caplog.text


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
