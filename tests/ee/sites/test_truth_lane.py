# tests/ee/sites/test_truth_lane.py — the rules of the truth lane: which artifacts it
# will vouch for, which it refuses and with what reason, and what it leaves on disk.
#
# Created 2026-08-10 (SG-10 wiring). test_artifact_preview.py covers the static server's
# own rules and test_artifact_preview_server.py covers them over real HTTP. This file
# covers the question neither of them asks: SHOULD this artifact be served at all.
#
# The artifacts here are shaped like the ones MEASURED off the live Daytona lane on
# 2026-08-09, because the whole point is that the three shapes differ and two of them are
# engine ``"svelte"``:
#
#   * react ``dist``                     — 4 entries, self-contained, no worker.
#   * static svelte ``build``            — adapter-static, no worker, self-contained.
#   * dynamic svelte ``.svelte-kit/cloudflare`` — 24 entries INCLUDING a ``_worker.js``
#     whose two imports sit outside the tarred directory (proving record §8 item 14), and
#     a ``_routes.json`` handing that worker ``/*``. Not runnable, and therefore refused.
#
# THE LOAD-BEARING TEST IN THIS FILE IS ``test_the_engine_name_decides_nothing``: the two
# svelte shapes carry the SAME engine string and get OPPOSITE answers. A gate that read
# the engine name would refuse every static landing site and admit every dynamic one, so
# that test is what separates "resolves off the artifact" from a comment claiming it does.
#
# The security assertions here are about what a refusal leaves behind. A refused artifact
# must leave nothing on disk (so its worker bundle, which has carried a substituted
# per-site signed key, never sits inside a served tree) and must not leave a PREVIOUS
# build's tree answering at the same address (so the surface whose job is to say "this
# build is correct" is never saying it about a different build).

from __future__ import annotations

import io
import tarfile
import urllib.error
import urllib.request

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.sites import artifact_preview as ap  # noqa: E402
from pocketpaw_ee.sites import truth_lane as tl  # noqa: E402

#: A canary in the worker bundle. The standing hard gate is that no per-site signed key
#: may ever appear in a client bundle or in view-source, and the worker is the file that
#: has historically carried one, so the tests below look for these bytes specifically
#: rather than for "a file called _worker.js".
_CANARY = b"pp_tok_CANARY_TRUTHLANE"

# react's Vite `dist` (measured 61,487 bytes / 4 entries, no node_modules, no worker).
_REACT_DIST = {
    "./index.html": b"<!doctype html><title>Acme</title><script src=./assets/app.js></script>ok",
    "./assets/app.js": b"console.log('react bundle')",
    "./assets/app.css": b"body{color:#111}",
    "./vite.svg": b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
}

# A STATIC svelte landing site: adapter-static's `build`. No worker, no routing table.
_SVELTE_STATIC_BUILD = {
    "./index.html": b"<!doctype html><title>Acme</title><body>hello landing</body>",
    "./_app/version.json": b'{"version":"1"}',
    "./_app/immutable/entry/start.abc.js": b"export const start = 1",
    "./_app/immutable/assets/_layout.def.css": b".x{}",
}

# A DYNAMIC / auth svelte site: adapter-cloudflare's `.svelte-kit/cloudflare`. The worker
# is the renderer, `_routes.json` says so, and the worker's imports are not in the tar.
_SVELTE_DYNAMIC_CLOUDFLARE = {
    "./index.html": b"<!doctype html><title>Acme</title><body></body>",
    "./404.html": b"<!doctype html><title>Not found</title>",
    "./_worker.js": (
        b'import { Server } from "./../output/server/index.js";\n'
        b'import { manifest } from "./../cloudflare-tmp/manifest.js";\n'
        b'const KEY = "' + _CANARY + b'";\n'
    ),
    "./_routes.json": b'{"version":1,"include":["/*"],"exclude":["/_app/immutable/*"]}',
    "./.assetsignore": b"_worker.js\n",
    "./_app/immutable/entry/start.abc.js": b"export const start = 1",
}


def _tar(members: dict[str, bytes], *, dirs: tuple[str, ...] = ()) -> bytes:
    """Pack ``members`` (names used VERBATIM) into gzipped tar bytes.

    ``dirs`` adds DIRECTORY members, which is not a detail: adapter-cloudflare emits
    ``_worker.js`` as a directory of chunks once an app is large enough, so a gate that
    only recognised a worker FILE would wave the biggest dynamic sites straight through.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in dirs:
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            tar.addfile(info)
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


@pytest.fixture(autouse=True)
def previews(tmp_path, monkeypatch):
    """Isolate the preview root and the exposure config for every test in this file.

    The exposure defaults to a fixed ``preview-origin`` base URL rather than the real
    default (loopback), so a unit test never binds a socket and the URL it asserts on is
    deterministic. ``test_the_default_exposure_is_loopback`` covers the real default.
    """
    monkeypatch.setenv("PAW_SITES_PREVIEW_DIR", str(tmp_path / "site-previews"))
    monkeypatch.setenv("PAW_SITES_LOCAL_DIR", str(tmp_path / "sites"))
    monkeypatch.setenv(tl.EXPOSURE_ENV, tl.EXPOSURE_PREVIEW_ORIGIN)
    monkeypatch.setenv(tl.PREVIEW_ORIGIN_ENV, "https://preview.test")
    return tmp_path


# ---------------------------------------------------------------------------
# What the lane will vouch for
# ---------------------------------------------------------------------------


def test_a_react_dist_artifact_is_previewable():
    result = tl.open_preview("react1", _tar(_REACT_DIST), engine="react")

    assert result.ok
    assert result.verdict.reason == tl.REASON_OK
    assert result.url == "https://preview.test/_preview/react1/"
    assert result.mount_path == "/_preview/react1/"
    assert ap.has_preview("react1")


def test_a_static_svelte_landing_artifact_is_previewable():
    result = tl.open_preview("svstatic1", _tar(_SVELTE_STATIC_BUILD), engine="svelte")

    assert result.ok
    assert result.verdict.reason == tl.REASON_OK
    assert ap.resolve("svstatic1", "/").status == 200


def test_a_previewable_artifact_serves_its_own_bundle():
    tl.open_preview("react2", _tar(_REACT_DIST), engine="react")

    asset = ap.resolve("react2", "/assets/app.js")
    assert asset.status == 200
    # Load-bearing: served as text/plain the browser refuses to execute it and the page
    # never hydrates, so a "successful" preview would show an unfinished site.
    assert asset.content_type.startswith("text/javascript")


# ---------------------------------------------------------------------------
# The refusal that is the reason this module exists
# ---------------------------------------------------------------------------


def test_a_dynamic_svelte_artifact_is_refused_rather_than_partially_served():
    result = tl.open_preview("svdyn1", _tar(_SVELTE_DYNAMIC_CLOUDFLARE), engine="svelte")

    assert not result.ok
    assert result.verdict.previewable is False
    assert result.verdict.reason == tl.REASON_SERVER_RENDERED
    assert result.url is None
    # The static files beside the worker unpack perfectly well. The point is that they
    # are NOT stored: half of a worker-rendered site is not the site.
    assert not ap.has_preview("svdyn1")
    assert ap.resolve("svdyn1", "/").status == 404
    assert ap.resolve("svdyn1", "/").reason == "no_preview"


def test_the_refusal_says_why_in_words_a_customer_can_act_on():
    result = tl.open_preview("svdyn2", _tar(_SVELTE_DYNAMIC_CLOUDFLARE), engine="svelte")

    detail = result.verdict.detail.lower()
    assert "worker" in detail
    assert "publish" in detail
    # It must not blame the customer's site for our packaging, and it must not imply the
    # preview is merely unavailable right now.
    assert "your site is broken" not in detail


def test_the_engine_name_decides_nothing():
    """The load-bearing test: one engine string, two shapes, opposite answers.

    Since SL-1 a svelte site builds on adapter-static (no worker) or adapter-cloudflare
    (worker), and only the artifact says which. A gate keyed on ``engine == "svelte"``
    would be wrong in both directions at once.
    """
    static = tl.assess_artifact(_tar(_SVELTE_STATIC_BUILD), engine="svelte")
    dynamic = tl.assess_artifact(_tar(_SVELTE_DYNAMIC_CLOUDFLARE), engine="svelte")

    assert static.previewable is True
    assert dynamic.previewable is False
    assert dynamic.reason == tl.REASON_SERVER_RENDERED


def test_a_worker_emitted_as_a_directory_is_still_a_worker():
    """adapter-cloudflare emits ``_worker.js/chunks/0.js`` once an app is large enough.
    A gate that only matched a worker FILE would admit exactly the biggest sites."""
    members = dict(_SVELTE_STATIC_BUILD)
    members["./_worker.js/chunks/0.js"] = b"export const chunk = 1"

    verdict = tl.assess_artifact(_tar(members, dirs=("./_worker.js",)), engine="svelte")

    assert verdict.reason == tl.REASON_SERVER_RENDERED


def test_a_backslash_separated_worker_member_is_seen():
    """``_app\\_worker.js`` is one filename on POSIX and a path on Windows. ``unpack``
    refuses such a member outright; the GATE has to notice it either way, or the same
    artifact would be refused on one host and admitted on the other."""
    members = dict(_SVELTE_STATIC_BUILD)
    members["./_app\\_worker.js"] = b"export default {}"

    verdict = tl.assess_artifact(_tar(members), engine="svelte")

    assert verdict.reason == tl.REASON_SERVER_RENDERED


def test_a_routing_table_with_no_worker_is_refused_as_incomplete():
    """§8 item 14's incompleteness from the other side: the artifact ships a routing
    table naming a renderer it does not contain."""
    members = dict(_SVELTE_STATIC_BUILD)
    members["./_routes.json"] = b'{"version":1,"include":["/*"],"exclude":[]}'

    result = tl.open_preview("svdyn3", _tar(members), engine="svelte")

    assert result.verdict.reason == tl.REASON_INCOMPLETE_ARTIFACT
    assert not ap.has_preview("svdyn3")


def test_the_two_renderer_refusals_are_distinct_reasons():
    """A worker-rendered artifact and an incomplete one are different problems — one is
    ours to fix by deploying, the other is ours to fix by packaging — so they must not
    collapse into one reason. With a single shared string, a mutation disabling either
    check would still look caught."""
    assert tl.REASON_SERVER_RENDERED != tl.REASON_INCOMPLETE_ARTIFACT


def test_an_artifact_with_no_home_page_is_refused():
    members = {"./assets/app.js": b"console.log(1)"}

    result = tl.open_preview("nohome1", _tar(members), engine="react")

    assert result.verdict.reason == tl.REASON_NO_ENTRY_DOCUMENT
    assert not ap.has_preview("nohome1")


def test_a_nested_index_is_not_the_entry_document():
    """A preview opens at the root. ``docs/index.html`` is not a home page, and treating
    it as one would open the preview onto a 404 while reporting success."""
    members = {"./docs/index.html": b"<!doctype html>nested"}

    assert tl.assess_artifact(_tar(members), engine="react").reason == tl.REASON_NO_ENTRY_DOCUMENT


def test_a_worker_rendered_artifact_reports_the_renderer_not_the_missing_page():
    """Order of refusals. A worker-rendered artifact may legitimately have no root
    ``index.html``; reporting THAT would send someone to look at the wrong thing."""
    members = {
        "./_worker.js": b"export default {}",
        "./_routes.json": b'{"include":["/*"]}',
    }

    assert tl.assess_artifact(_tar(members), engine="svelte").reason == tl.REASON_SERVER_RENDERED


def test_an_engine_that_runs_no_build_is_refused():
    """html emits its static output at the project root and runs no build, so an artifact
    arriving for it is a routing bug. Mirrors the refusals in ``store_artifact`` and
    ``daytona_build.artifact_tar_command``."""
    result = tl.open_preview("html1", _tar(_REACT_DIST), engine="html")

    assert result.verdict.reason == tl.REASON_ENGINE_RUNS_NO_BUILD
    assert not ap.has_preview("html1")


def test_unreadable_bytes_and_no_bytes_refuse_differently():
    assert tl.assess_artifact(b"not a gzipped tar", engine="react").reason == (
        tl.REASON_ARTIFACT_UNREADABLE
    )
    assert tl.assess_artifact(None, engine="react").reason == tl.REASON_NO_ARTIFACT
    assert tl.assess_artifact(b"", engine="react").reason == tl.REASON_NO_ARTIFACT


def test_open_preview_never_raises_on_any_of_them():
    """It hangs off a build that may already have succeeded, so every outcome is a value."""
    for artifact in (None, b"", b"junk", _tar(_SVELTE_DYNAMIC_CLOUDFLARE)):
        result = tl.open_preview("total1", artifact, engine="svelte")
        assert result.ok is False


def test_a_store_that_fails_is_a_refusal_not_an_exception(monkeypatch):
    """The store is the last thing that can fail, and it must fail the same way as
    everything else here: a build that already succeeded must never be reported as failed
    because its preview could not be unpacked."""

    def refusing_store(*args, **kwargs):
        raise ap.ArtifactTooLarge("mutated: too big")

    monkeypatch.setattr(ap, "store_artifact", refusing_store)

    result = tl.open_preview("storefail1", _tar(_REACT_DIST), engine="react")

    assert result.verdict.reason == tl.REASON_ARTIFACT_REJECTED
    assert result.ok is False


def test_a_store_that_breaks_unexpectedly_reports_a_different_cause(monkeypatch):
    """An unforeseen failure is a fact about US, not about the artifact. Reporting it as
    "your build output was rejected" sends the customer to debug their own site."""

    def exploding_store(*args, **kwargs):
        raise MemoryError("mutated: something unforeseen")

    monkeypatch.setattr(ap, "store_artifact", exploding_store)

    result = tl.open_preview("storefail2", _tar(_REACT_DIST), engine="react")

    assert result.ok is False
    assert result.verdict.reason == tl.REASON_STORE_FAILED
    assert result.verdict.reason != tl.REASON_ARTIFACT_REJECTED


# ---------------------------------------------------------------------------
# What a refusal leaves behind
# ---------------------------------------------------------------------------


def test_a_refusal_discards_the_previous_preview():
    """The half of the refusal that is easy to miss. A previous build's tree left in
    place keeps answering 200 at the same address, so the surface whose whole job is to
    say "this build is correct" would be saying it about a different build."""
    assert tl.open_preview("stale1", _tar(_SVELTE_STATIC_BUILD), engine="svelte").ok
    assert ap.resolve("stale1", "/").status == 200

    refused = tl.open_preview("stale1", _tar(_SVELTE_DYNAMIC_CLOUDFLARE), engine="svelte")

    assert refused.verdict.reason == tl.REASON_SERVER_RENDERED
    assert not ap.has_preview("stale1")
    assert ap.resolve("stale1", "/").status == 404


def test_a_refused_artifact_leaves_no_bytes_of_its_worker_on_disk(previews):
    """The standing hard gate, checked rather than asserted in prose: the file that has
    carried a substituted per-site signed key must not land inside a served tree."""
    tl.open_preview("key1", _tar(_SVELTE_DYNAMIC_CLOUDFLARE), engine="svelte")

    root = previews / "site-previews"
    found = [path for path in root.rglob("*") if path.is_file() and _CANARY in path.read_bytes()]
    assert found == []


def _lying_scan(real):
    """A scan that reports every artifact as a clean, self-contained static tree."""

    def scan(artifact: bytes) -> ap.ArtifactShape:
        shape = real(artifact)
        return ap.ArtifactShape(
            entries=shape.entries,
            server_entries=(),
            routing_tables=(),
            has_entry_document=True,
        )

    return scan


def test_a_reason_layer_that_disagrees_with_the_door_refuses(monkeypatch):
    """Two readings of the same bytes that disagree mean one of them is wrong, and nothing
    here can tell which — so serve neither. This is the DOOR catching it: only the
    reason-layer scan is broken, and ``store_artifact`` refuses on its own reading."""
    monkeypatch.setattr(tl, "scan_artifact", _lying_scan(tl.scan_artifact))

    result = tl.open_preview("disagree1", _tar(_SVELTE_DYNAMIC_CLOUDFLARE), engine="svelte")

    assert result.verdict.reason == tl.REASON_GATE_DISAGREED
    assert not ap.has_preview("disagree1")


def test_a_scan_that_disagrees_with_the_unpack_refuses(monkeypatch):
    """And this is the POST-STORE cross-check catching it, which is the layer that
    survives when the scan itself is what is broken. ``_member_segments`` and
    ``_normalized_member_path`` are genuinely different readers of the same names, so this
    is not a tautology: with the scan blinded, the unpack is what still sees the worker."""
    monkeypatch.setattr(ap, "scan_artifact", _lying_scan(ap.scan_artifact))
    monkeypatch.setattr(tl, "scan_artifact", ap.scan_artifact)

    result = tl.open_preview("disagree2", _tar(_SVELTE_DYNAMIC_CLOUDFLARE), engine="svelte")

    assert result.verdict.reason == tl.REASON_GATE_DISAGREED
    assert not ap.has_preview("disagree2")


# ---------------------------------------------------------------------------
# No false success survives the wiring
# ---------------------------------------------------------------------------


def test_a_missing_asset_is_still_a_404_after_the_gate_admits_the_build():
    """The property ``artifact_preview``'s header commits to, checked THROUGH the truth
    lane rather than beside it: a missing asset must not be rewritten to index.html. An
    SPA fallback here would make a broken build look finished, which is the one failure
    mode a verification preview must not have."""
    tl.open_preview("nofb1", _tar(_REACT_DIST), engine="react")

    missing = ap.resolve("nofb1", "/assets/does-not-exist.js")

    assert missing.status == 404
    assert missing.reason == "not_found"
    assert b"<div id=root>" not in missing.body
    assert b"react bundle" not in missing.body


def test_a_missing_page_is_a_404_not_the_home_page():
    tl.open_preview("nofb2", _tar(_REACT_DIST), engine="react")

    missing = ap.resolve("nofb2", "/pricing/")

    assert missing.status == 404
    assert b"<title>Acme</title>" not in missing.body


# ---------------------------------------------------------------------------
# The gate is at the DISK BOUNDARY, so no call path routes around it
# ---------------------------------------------------------------------------


def test_the_store_itself_refuses_a_worker_bearing_artifact():
    """The gate is enforced in ``store_artifact`` — the one function every path to disk
    goes through — rather than only in the surface above, because a check anywhere else is
    bypassable by picking a different caller."""
    with pytest.raises(ap.ArtifactNotPreviewable):
        ap.store_artifact("door1", _tar(_SVELTE_DYNAMIC_CLOUDFLARE), engine="svelte")

    assert not ap.has_preview("door1")


def test_the_store_refuses_before_it_writes_anything(previews):
    """A refusal that unpacked first and cleaned up afterwards would leave a window where
    the tree is live at the preview address, and would touch the disk on every hostile
    artifact. Nothing is written at all — not even the hidden incoming sibling."""
    root = previews / "site-previews"
    with pytest.raises(ap.ArtifactNotPreviewable):
        ap.store_artifact("door2", _tar(_SVELTE_DYNAMIC_CLOUDFLARE), engine="svelte")

    # Stronger than "no tree for door2": the refusal happens before ``previews_home()`` is
    # consulted, so it does not even create the previews root.
    assert not root.exists() or list(root.iterdir()) == []


def test_the_refusal_is_a_kind_of_artifact_rejected():
    """So a caller that already handles ``ArtifactRejected`` — ``safe_store_artifact``, and
    ``truth_lane``'s store clause — cannot miss it by not knowing the new name."""
    assert issubclass(ap.ArtifactNotPreviewable, ap.ArtifactRejected)


def test_the_swallowing_store_inherits_the_gate():
    """``safe_store_artifact`` has an inviting name and swallows everything, so an ungated
    version of it is exactly the footgun a future caller reaches for. It returns ``None``,
    which is what it has always returned for "no preview", so gating changed no contract."""
    assert (
        ap.safe_store_artifact("safe1", _tar(_SVELTE_DYNAMIC_CLOUDFLARE), engine="svelte") is None
    )
    assert not ap.has_preview("safe1")

    assert ap.safe_store_artifact("safe2", _tar(_REACT_DIST), engine="react") is not None


def test_the_front_door_inherits_the_gate(loopback):
    """``local_server.serve_artifact_preview`` is the obvious-looking entry point, and the
    one whose ungated version this change closed."""
    from pocketpaw_ee.sites import local_server

    assert (
        local_server.serve_artifact_preview(
            "front1", _tar(_SVELTE_DYNAMIC_CLOUDFLARE), engine="svelte"
        )
        is None
    )
    assert not ap.has_preview("front1")

    assert local_server.serve_artifact_preview("front2", _tar(_REACT_DIST), engine="react")
    assert ap.has_preview("front2")


def test_the_named_seam_is_the_only_way_past_the_gate():
    """The seam exists so ``resolve``'s own ``_worker.js`` refusal keeps something to
    refuse. Deleting it to close the hole would trade a proven guard for an unproven one,
    so it is kept — and named so that using it is a visible act at the call site."""
    snapshot = ap.store_unvouched_artifact(
        "seam1", _tar(_SVELTE_DYNAMIC_CLOUDFLARE), engine="svelte"
    )

    assert snapshot.unpacked.server_entries == ("_worker.js",)
    assert ap.has_preview("seam1")
    # Still not SERVED — the seam gets a tree onto disk, it does not weaken `resolve`.
    assert ap.resolve("seam1", "/_worker.js").reason == "server_entry_refused"


def test_no_production_module_stores_an_unvouched_artifact():
    """THE CONTROL ON THE SEAM, and the reason its name is load-bearing rather than
    decorative. A comment saying "never call this from production" is enforced by whoever
    happens to read it; this is enforced by CI. Scans every module under ``src/`` and
    ``ee/`` — the shipped code — and fails if any of them names the function.

    ``artifact_preview`` itself is excluded because that is where it is defined.

    The scan is AST-based, not a substring search. Prose ABOUT the seam is exactly what a
    module should carry — ``local_server``'s header explains why its own front door is
    gated and names the seam while doing so — and a grep would fail on the documentation
    that makes the design legible. The AST sees calls, imports and attribute access and
    never sees a comment or a docstring, which is the distinction that matters.
    """
    import ast
    from pathlib import Path

    import pocketpaw_ee

    import pocketpaw

    symbol = "store_unvouched_artifact"
    definer = Path(ap.__file__).resolve()
    roots = [Path(pocketpaw.__file__).parent, Path(pocketpaw_ee.__file__).parent]

    def references(path: Path) -> bool:
        try:
            tree = ast.parse(path.read_text("utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - vendored forks
            return False
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == symbol:
                return True
            if isinstance(node, ast.Name) and node.id == symbol:
                return True
            if isinstance(node, ast.ImportFrom) and any(a.name == symbol for a in node.names):
                return True
        return False

    offenders = sorted(
        str(path)
        for root in roots
        for path in root.rglob("*.py")
        if path.resolve() != definer and references(path)
    )

    assert offenders == [], (
        f"{symbol} bypasses the previewability gate and must never be reachable from "
        f"production. Referenced by: {offenders}"
    )


def test_the_guard_on_the_seam_would_actually_fire(tmp_path):
    """A guard nobody has watched fail is not a guard. Proves the scan above detects a
    real call — otherwise a typo in the symbol name would leave it passing forever while
    checking nothing."""
    import ast

    module = tmp_path / "offender.py"
    module.write_text(
        "from pocketpaw_ee.sites import artifact_preview\n"
        "def wire(site_id, artifact):\n"
        "    return artifact_preview.store_unvouched_artifact(\n"
        "        site_id, artifact, engine='svelte'\n"
        "    )\n",
        encoding="utf-8",
    )

    tree = ast.parse(module.read_text("utf-8"))
    hits = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "store_unvouched_artifact"
    ]
    assert len(hits) == 1

    # And prose about it does not count, which is the whole reason the scan is AST-based.
    commented = tmp_path / "documented.py"
    commented.write_text(
        "# never call store_unvouched_artifact from here\nx = 1\n", encoding="utf-8"
    )
    prose_tree = ast.parse(commented.read_text("utf-8"))
    assert not [
        node
        for node in ast.walk(prose_tree)
        if isinstance(node, ast.Attribute) and node.attr == "store_unvouched_artifact"
    ]


def test_the_gate_and_the_seam_disagree_by_design(previews):
    """Pins the two paths against each other, so the difference stays deliberate rather
    than becoming a surprise: the same artifact, refused by the door and stored by the
    seam."""
    artifact = _tar(_SVELTE_DYNAMIC_CLOUDFLARE)

    assert ap.store_unvouched_artifact("both1", artifact, engine="svelte") is not None
    assert ap.has_preview("both1")

    assert tl.open_preview("both2", artifact, engine="svelte").ok is False
    assert not ap.has_preview("both2")


# ---------------------------------------------------------------------------
# The project-dir half (a build that has not been tarred)
# ---------------------------------------------------------------------------


def _static_svelte_project(root):
    build = root / "project" / "build"
    build.mkdir(parents=True)
    (build / "index.html").write_bytes(b"<!doctype html>landing")
    return root / "project"


def _dynamic_svelte_project(root):
    out = root / "project" / ".svelte-kit" / "cloudflare"
    out.mkdir(parents=True)
    (out / "index.html").write_bytes(b"<!doctype html>shell")
    (out / "_worker.js").write_bytes(b"export default {}")
    (out / "_routes.json").write_bytes(b'{"include":["/*"]}')
    return root / "project"


def test_a_static_svelte_project_dir_is_previewable(previews):
    verdict = tl.assess_project(_static_svelte_project(previews), "svelte")

    assert verdict.previewable is True
    assert verdict.reason == tl.REASON_OK


def test_a_dynamic_svelte_project_dir_is_refused(previews):
    verdict = tl.assess_project(_dynamic_svelte_project(previews), "svelte")

    assert verdict.reason == tl.REASON_SERVER_RENDERED


def test_the_project_probe_resolves_the_output_dir_off_disk(previews):
    """A stale ``.svelte-kit/cloudflare`` from a pre-SL-1 build must not shadow what this
    build emitted — ``resolve_static_output_rel`` probes ``build`` first, and this is the
    case where getting that order wrong silently refuses a good static site."""
    project = _static_svelte_project(previews)
    stale = project / ".svelte-kit" / "cloudflare"
    stale.mkdir(parents=True)
    (stale / "_worker.js").write_bytes(b"export default {} // stale")

    assert tl.assess_project(project, "svelte").previewable is True


def test_a_project_with_a_worker_directory_is_refused(previews):
    project = _static_svelte_project(previews)
    (project / "build" / "_worker.js").mkdir()

    assert tl.assess_project(project, "svelte").reason == tl.REASON_SERVER_RENDERED


def test_a_project_with_no_home_page_is_refused(previews):
    project = previews / "empty"
    (project / "dist").mkdir(parents=True)

    assert tl.assess_project(project, "react").reason == tl.REASON_NO_ENTRY_DOCUMENT


def test_an_html_project_runs_no_build(previews):
    assert tl.assess_project(previews, "html").reason == tl.REASON_ENGINE_RUNS_NO_BUILD


# ---------------------------------------------------------------------------
# The exposure seam
# ---------------------------------------------------------------------------


def test_the_default_exposure_is_loopback(monkeypatch):
    """The default decides nothing: 127.0.0.1 is not reachable off the box, so there is
    no token to design and no customer JavaScript running where a session cookie lives."""
    monkeypatch.delenv(tl.EXPOSURE_ENV, raising=False)

    assert tl.preview_exposure() == tl.EXPOSURE_LOOPBACK


def test_an_unknown_exposure_falls_back_to_the_one_that_exposes_nothing(monkeypatch):
    monkeypatch.setenv(tl.EXPOSURE_ENV, "publik")

    assert tl.preview_exposure() == tl.EXPOSURE_LOOPBACK


def test_the_app_origin_exposure_refuses_until_it_is_decided(monkeypatch):
    monkeypatch.setenv(tl.EXPOSURE_ENV, tl.EXPOSURE_APP_ORIGIN)

    with pytest.raises(tl.PreviewExposureNotConfigured) as caught:
        tl.preview_base_url()

    # The message has to name the mechanic, not just refuse. Whoever reads it needs to
    # know that adding a sandbox attribute is not the fix — it empties the document's
    # site-for-cookies and the page's own same-origin assets stop loading.
    assert "cookie" in str(caught.value)


def test_the_signed_url_exposure_refuses_until_it_is_decided(monkeypatch):
    monkeypatch.setenv(tl.EXPOSURE_ENV, tl.EXPOSURE_SIGNED_URL)

    with pytest.raises(tl.PreviewExposureNotConfigured) as caught:
        tl.preview_base_url()

    assert "revoked" in str(caught.value)


def test_a_preview_origin_with_no_base_url_refuses_rather_than_degrading(monkeypatch):
    """Degrading to loopback would hand a 127.0.0.1 URL to an operator who asked for a
    public one — a preview that silently works only on the API box."""
    monkeypatch.setenv(tl.EXPOSURE_ENV, tl.EXPOSURE_PREVIEW_ORIGIN)
    monkeypatch.delenv(tl.PREVIEW_ORIGIN_ENV, raising=False)

    with pytest.raises(tl.PreviewExposureNotConfigured):
        tl.preview_base_url()


def test_a_configured_preview_origin_is_used_and_its_trailing_slash_dropped(monkeypatch):
    monkeypatch.setenv(tl.PREVIEW_ORIGIN_ENV, "https://preview.example.net/")

    assert tl.preview_base_url() == "https://preview.example.net"
    assert tl.preview_address("s1") == "https://preview.example.net/_preview/s1/"


def test_an_unconfigured_exposure_does_not_read_as_a_broken_build(monkeypatch, caplog):
    """The verdict is about the artifact; the address is about where previews are
    exposed. Conflating them would report a perfectly good build as unpreviewable
    because nobody has picked an origin yet."""
    monkeypatch.setenv(tl.EXPOSURE_ENV, tl.EXPOSURE_APP_ORIGIN)

    with caplog.at_level("WARNING"):
        result = tl.open_preview("noaddr1", _tar(_REACT_DIST), engine="react")

    # An undecided exposure and a socket that would not bind both end with no address, and
    # they are logged DIFFERENTLY on purpose: this one has to say what needs deciding,
    # because an operator reading "could not resolve a preview address" plus a traceback
    # goes looking for a fault instead of making a decision.
    assert "cookie" in caplog.text
    assert "could not resolve a preview address" not in caplog.text

    assert result.verdict.previewable is True
    assert result.verdict.reason == tl.REASON_OK
    assert result.url is None
    assert result.ok is False
    # Stored, so flipping the exposure needs no rebuild.
    assert ap.has_preview("noaddr1")
    assert ap.resolve("noaddr1", "/").status == 200


# ---------------------------------------------------------------------------
# Over real HTTP, on the one exposure that needs no decision
# ---------------------------------------------------------------------------


def _get(url: str):
    req = urllib.request.Request(url, method="GET")  # noqa: S310 - localhost
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - localhost
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@pytest.fixture
def loopback(monkeypatch):
    """The real default exposure, with the process-singleton server reset.

    The served root is captured when the server starts, so a test that only set the env
    var would be served by whatever root a previous test started with."""
    monkeypatch.setenv(tl.EXPOSURE_ENV, tl.EXPOSURE_LOOPBACK)
    monkeypatch.delenv(tl.PREVIEW_ORIGIN_ENV, raising=False)

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


def test_a_gated_preview_serves_the_real_page_over_http(loopback):
    result = tl.open_preview("http1", _tar(_REACT_DIST), engine="react")

    assert result.ok
    assert result.url is not None
    assert result.url.startswith("http://127.0.0.1:")

    status, body = _get(result.url)
    assert status == 200
    assert b"<title>Acme</title>" in body

    status, body = _get(result.url + "assets/app.js")
    assert status == 200
    assert b"react bundle" in body


def test_a_refused_build_answers_404_at_the_preview_address(loopback):
    refused = tl.open_preview("http2", _tar(_SVELTE_DYNAMIC_CLOUDFLARE), engine="svelte")
    assert refused.url is None

    # The address a caller would have been handed had it been previewable. Nothing is
    # there, and nothing renders — the refusal is a fact about the filesystem, not a
    # flag some later reader has to remember to check.
    address = f"{loopback.ensure_server()}/_preview/http2/"
    status, body = _get(address)

    assert status == 404
    assert _CANARY not in body
    assert b"hello landing" not in body


def test_the_worker_is_unreachable_even_for_an_artifact_the_lane_admits(loopback):
    """A static svelte build has no worker, so there is nothing to leak — but the request
    must still refuse rather than 404 by accident of the file being absent. The refusal
    is what holds when a future build shape puts one back."""
    result = tl.open_preview("http3", _tar(_SVELTE_STATIC_BUILD), engine="svelte")
    assert result.url is not None

    status, body = _get(result.url + "_worker.js")

    assert status == 404
    assert _CANARY not in body


def test_a_missing_asset_over_http_does_not_render_the_home_page(loopback):
    result = tl.open_preview("http4", _tar(_REACT_DIST), engine="react")
    assert result.url is not None

    status, body = _get(result.url + "assets/missing.js")

    assert status == 404
    assert b"react bundle" not in body
    assert b"<title>Acme</title>" not in body


# ---------------------------------------------------------------------------
# The gate's own vocabulary stays in step with the server's
# ---------------------------------------------------------------------------


def test_a_healthy_static_svelte_preview_logs_no_incompleteness_warning(caplog):
    """``store_artifact``'s cross-check asks the engine name whether a worker was
    expected, and since SL-1 the name cannot answer it for svelte — so unless the truth
    lane passes its own resolved answer, every healthy static landing preview warns that
    the artifact may be incomplete. A warning that fires on the happy path stops carrying
    information, which is worse than not having it."""
    with caplog.at_level("WARNING"):
        assert tl.open_preview("quiet1", _tar(_SVELTE_STATIC_BUILD), engine="svelte").ok

    assert "may be incomplete" not in caplog.text


def test_a_react_artifact_carrying_a_worker_still_warns(caplog):
    """The other direction of the same cross-check has to keep working: an artifact whose
    shape contradicts its engine is evidence the build lane changed underneath us. The
    truth lane refuses it, and the warning is what says why the shape was unexpected."""
    with caplog.at_level("WARNING"):
        # Through the seam: the gated door refuses a worker-bearing artifact outright, so
        # the shape-mismatch WARNING is only reachable on the un-vouched path now. That is
        # the right place for it — the mismatch is evidence for whoever is looking at a
        # tree that exists, and on the gated path there is no tree to look at.
        result = ap.store_unvouched_artifact(
            "shape1", _tar(_SVELTE_DYNAMIC_CLOUDFLARE), engine="react"
        )

    assert result.unpacked.server_entries
    assert "emits none" in caplog.text


def test_the_gate_reads_the_same_server_entry_names_the_unpack_skips():
    """Imported, not mirrored. A name the gate does not recognise as a server entry is a
    name ``unpack_artifact`` would write into a served tree, so these cannot drift."""
    assert tl.SERVER_ENTRY_NAMES is ap._SERVER_ENTRY_NAMES


def test_the_routing_table_the_gate_reads_is_one_the_unpack_never_serves():
    """``_routes.json`` is evidence for the gate and deploy configuration for the server.
    Both are true; if it ever stopped being skipped at unpack it would be served as site
    content, which is a different bug this pins the boundary of."""
    assert tl.ROUTING_TABLE_NAME in ap._DEPLOY_METADATA_NAMES
