# ee/pocketpaw_ee/sites/artifact_preview.py — preview a site's BUILT STATIC
# ARTIFACT. Unpacks the tarball the ephemeral build lane produces and answers
# HTTP requests against it. No Node runtime, no dev server, no cross-origin
# isolation.
#
# Created 2026-08-10 (SG-10, re-aimed). The slice originally booted an in-tab Node
# runtime (StackBlitz WebContainer) to run a dev server for the site project. Two
# things retired that premise:
#
#   1. THE BUILD LEFT THE REQUEST PATH. Every source-engine site now builds in an
#      ephemeral Daytona sandbox that deletes itself, and only the STATIC OUTPUT
#      comes back — ``daytona_build.artifact_tar_command`` packs exactly
#      ``static_output_rel(engine)`` and nothing else. Measured 2026-08-09: react
#      61,487 bytes / 4 entries in 8.70s, svelte 33,104 bytes / 24 entries in
#      14.67s, neither containing ``node_modules``. Serving an already-built static
#      tree needs no Node at all, so the in-browser runtime solves a problem this
#      lane no longer has.
#   2. THE LICENCE QUESTION IS PROCUREMENT, NOT ENGINEERING. Nobody has asked
#      StackBlitz whether a licence covers sessions booted by tenants' own end
#      visitors. Note that ``@webcontainer/api`` declaring ``"license": "MIT"`` in
#      its npm manifest covers the CLIENT LIBRARY ONLY — not the hosted runtime and
#      not the commercial terms. It is not permission and must never be cited as
#      such.
#
# Choosing static REMOVES constraints. The in-tab runtime needs cross-origin
# isolation, and COEP ``require-corp`` blocks presigned-S3 images — exactly what the
# sites gallery cards render. ``credentialless`` dodges that but is Chromium/Firefox
# only, so Safari still breaks. This path sets no COOP/COEP header at all.
#
# THE ARTIFACT IS ALREADY ENGINE-NORMALIZED, and that is the whole reason this module
# carries no per-engine layout knowledge. The lane packs with
# ``tar -C <static_output_rel(engine)> .``, so react's ``dist`` and svelte's
# ``.svelte-kit/cloudflare`` both arrive as a tree whose ROOT is the deployable root.
# The engine is still consulted — via ``engines`` predicates, never a string compare —
# for two things it genuinely decides: whether the engine belongs in this lane at all
# (``static_output_rel`` == "." means it has no build subdir) and whether a
# ``_worker.js`` in the artifact is expected (``emits_server_worker``).
#
# FOUR DECISIONS WORTH READING BEFORE CHANGING ANYTHING HERE:
#
#   * ``_worker.js`` IS NEITHER EXECUTED NOR SERVED, AND NEVER LANDS ON DISK. svelte
#     emits one even with no server routes — confirmed by deleting ``src/routes/api/``,
#     wiping ``.svelte-kit`` and rebuilding clean; ``adapter-cloudflare`` emits a Server
#     shell regardless. It is skipped at unpack AND refused at resolve (404, not 403 —
#     a static preview genuinely has no server entry to offer, and 403 would advertise
#     the file). The refusal is not tidiness: on the svelte track that bundle
#     historically carried a substituted per-site ``__CAPTURE_SIGNED_KEY__``, so
#     returning its SOURCE would be a secret disclosure. Its presence must not break
#     the rest of the preview, and it does not — everything beside it still serves.
#   * ``/api/*`` IS REFUSED VISIBLY, NEVER FAKED. Lead capture is deferred, so no
#     ``/api/submit`` exists to serve. A form POST that appeared to succeed would be
#     worse than an error, so any request under ``api/`` gets 501 and a page that says
#     so, and any non-GET method gets 405 the same way. Nothing in this module can
#     return 2xx for a write.
#   * NO SPA FALLBACK. A missing asset is a 404, never a rewrite to ``index.html``.
#     Rewriting would make a broken build look finished, which is the one failure mode
#     a preview whose job is verification must not have.
#   * PATH TRAVERSAL IS GUARDED AT BOTH ENDS, because this is a static file server over
#     an extracted archive and that is two attack surfaces, not one.
#       - AT EXTRACT TIME (:func:`_normalized_member_path`): a member named
#         ``../../etc/passwd``, an absolute path, a drive letter, a backslash, a NUL, or
#         a symlink/hardlink escapes regardless of how careful the request side is.
#         ``tarfile`` has historically been unsafe by default here, which is why nothing
#         in this module calls ``extractall`` — members are written one at a time after
#         their names have been validated, with archive permissions discarded.
#       - AT REQUEST TIME (:func:`_request_segments` then :func:`_contained`): the path
#         is percent-decoded and validated, and then — the part that matters — RESOLVED
#         and checked against the resolved root. A prefix check on the raw string is
#         defeated by encoding and by symlinks; a check on the real path is not.
#     Every one of those guards has a mutation in ``tests/mutations`` proving it fires.
#     A traversal guard nobody has watched break is not a guard.
#
# Storage lives at ``previews_home()`` — deliberately NOT under
# ``local_server.sites_home()``. That server serves its root as plain static files, so
# a preview tree inside it would be reachable without passing these guards, and the
# ``_worker.js`` refusal would have a bypass. Keeping the roots disjoint is what makes
# the refusals structural rather than advisory.
from __future__ import annotations

import io
import logging
import mimetypes
import os
import re
import shutil
import tarfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from pocketpaw_ee.sites.engines import emits_server_worker, normalize_engine, static_output_rel

logger = logging.getLogger(__name__)

#: URL path segment the preview tree is mounted under on the local static server.
#: A distinct prefix (rather than a bare ``/<site_id>/``) is what lets one handler
#: route preview requests through :func:`resolve` while leaving the deploy trees on
#: plain static serving.
PREVIEW_URL_PREFIX = "_preview"

#: A site id is a URL path segment here, so it is validated rather than trusted.
#: Real ids are Mongo ObjectIds / uuids; this rejects separators, dots and anything
#: that could climb out of ``previews_home()``.
_SAFE_SITE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: Members skipped at unpack and refused at resolve. ``_worker.js`` arrives as a FILE
#: on the measured svelte artifact (4,335 bytes) but ``adapter-cloudflare`` emits a
#: DIRECTORY of chunks for larger apps, so the match is per path SEGMENT and takes the
#: whole subtree with it.
_SERVER_ENTRY_NAMES: frozenset[str] = frozenset({"_worker.js"})

#: Deploy configuration that rides along in the artifact and is not site content.
#: ``_routes.json`` and ``.assetsignore`` are what adapter-cloudflare emits; the
#: Pages-style ``_headers`` / ``_redirects`` are here because they are the same kind of
#: thing and would be equally wrong to serve as a page.
_DEPLOY_METADATA_NAMES: frozenset[str] = frozenset(
    {"_routes.json", ".assetsignore", "_headers", "_redirects"}
)

#: Zip-bomb ceilings. The measured artifacts are 4 and 24 entries at tens of KB, so
#: these are orders of magnitude clear of anything real; they exist because the tarball
#: is built from customer content and a preview must not be a way to fill the disk.
MAX_ENTRIES = 20_000
MAX_TOTAL_BYTES = 128 * 1024 * 1024

#: Content types resolved from an EXPLICIT table before falling back to
#: ``mimetypes``, because the stdlib reads the Windows registry and has been observed
#: returning ``text/plain`` for ``.js`` there. A preview that serves the site's
#: JavaScript as ``text/plain`` renders a page that never hydrates — and the captain's
#: standing rule is that JS is load-bearing for sites, so getting this wrong silently
#: breaks the thing the preview exists to check.
_CONTENT_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".txt": "text/plain; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".wasm": "application/wasm",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}

_OCTET_STREAM = "application/octet-stream"


class ArtifactRejected(ValueError):
    """The artifact cannot be turned into a preview. Base of the refusals below."""


class ArtifactUnreadable(ArtifactRejected):
    """The bytes are not a readable gzipped tar (truncated download, wrong file)."""


class ArtifactTooLarge(ArtifactRejected):
    """The artifact exceeds :data:`MAX_ENTRIES` or :data:`MAX_TOTAL_BYTES`."""


class BadSiteId(ArtifactRejected):
    """The site id is not a safe single path segment."""


@dataclass(frozen=True)
class UnpackedArtifact:
    """What came out of one artifact, including what was deliberately left out.

    The skip lists are RETURNED rather than only logged: ``server_entries`` is the
    evidence that svelte's server shell was seen and dropped, and ``rejected`` is the
    evidence a hostile member was refused. A preview that silently discarded either
    would leave nothing to check.
    """

    entries: int
    bytes_written: int
    server_entries: tuple[str, ...] = ()
    metadata_entries: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreviewSnapshot:
    """A stored preview: where it is on disk, and what it is mounted at."""

    site_id: str
    engine: str
    root: Path
    url_path: str
    unpacked: UnpackedArtifact


@dataclass(frozen=True)
class PreviewResponse:
    """One resolved request. Deliberately a value, not a write to a socket, so every
    rule below is unit-testable without an HTTP server in the way."""

    status: int
    reason: str
    content_type: str = "text/html; charset=utf-8"
    body: bytes = b""
    #: Set only on 301. The subpath to redirect to, RELATIVE to the site's mount
    #: point — the caller composes the absolute URL because only it knows the prefix.
    location: str | None = None
    headers: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def previews_home() -> Path:
    """Root of the unpacked preview trees.

    ``~/.pocketpaw/site-previews`` by default; override with ``PAW_SITES_PREVIEW_DIR``
    (tests point it at a temp dir so a run never writes the real home).

    A SIBLING of ``local_server.sites_home()``, never a child. That server hands its
    whole root out as static files, so a preview stored inside it would be reachable
    without passing :func:`resolve` — and the ``_worker.js`` refusal would have a
    bypass. Disjoint roots are what make the guards structural.
    """
    raw = os.environ.get("PAW_SITES_PREVIEW_DIR")
    base = Path(raw) if raw else Path.home() / ".pocketpaw" / "site-previews"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _check_site_id(site_id: str) -> str:
    if not isinstance(site_id, str) or not _SAFE_SITE_ID.match(site_id):
        raise BadSiteId(f"unsafe site id for a preview path: {site_id!r}")
    return site_id


def preview_root(site_id: str) -> Path:
    """Where this site's unpacked artifact lives. Does not create it."""
    return previews_home() / _check_site_id(site_id)


def has_preview(site_id: str) -> bool:
    """True when an unpacked artifact is on disk for this site."""
    try:
        return preview_root(site_id).is_dir()
    except BadSiteId:
        return False


def discard_preview(site_id: str) -> bool:
    """Remove a site's preview tree. True when something was removed."""
    root = preview_root(site_id)
    if not root.is_dir():
        return False
    shutil.rmtree(root, ignore_errors=True)
    return not root.exists()


# ---------------------------------------------------------------------------
# Unpacking
# ---------------------------------------------------------------------------


def _normalized_member_path(name: str) -> tuple[str, ...] | None:
    """Split a tar member name into safe segments, or ``None`` when it is not safe.

    ``tar -C <dir> .`` names every member ``./x/y``, so the leading ``.`` is normal and
    stripped. Everything else here is a refusal: an absolute path, a ``..`` segment, a
    backslash, a drive letter, or a NUL.

    EACH REFUSAL IS ITS OWN STATEMENT rather than one combined condition, so a mutation
    can remove exactly one of them and be seen to break exactly one test. A single
    ``if a or b or c`` guard reads as verified when only one of the three is.
    """
    if not name or name.startswith("/"):
        return None
    if "\x00" in name:
        # A NUL truncates the name for any C-level consumer, so a member called
        # "index.html\x00/../../evil" can pass a suffix or prefix check and land
        # somewhere else entirely.
        return None
    if "\\" in name:
        # A path separator on Windows and a legal filename character on POSIX.
        # Treating it as either means the same artifact writes to two different
        # places depending on where it is unpacked.
        return None
    if len(name) > 1 and name[1] == ":":  # a Windows drive-qualified path
        return None
    segments: list[str] = []
    for raw in name.split("/"):
        if raw in ("", "."):
            continue
        if raw == "..":
            return None
        segments.append(raw)
    return tuple(segments)


def unpack_artifact(artifact: bytes, dest: Path) -> UnpackedArtifact:
    """Extract a build artifact into ``dest``, dropping what must not be served.

    Members are written ONE AT A TIME from ``extractfile`` rather than through
    ``extractall``: this path has to distinguish three outcomes per member (write,
    security-skip, policy-skip) and report all three, which a bulk extract cannot.
    Permissions from the archive are never applied, so no member can arrive
    executable or setuid.

    Skips, all recorded on the result:
      * ``_worker.js`` (and its whole subtree) — see the module header. Not written at
        all, so the server bundle never sits inside a served tree.
      * deploy configuration (``_routes.json``, ``.assetsignore``, ...) — not content.
      * anything unsafe: absolute paths, ``..``, symlinks, hardlinks, devices, fifos.
        Links are refused rather than followed because a link is how a tarball reaches
        outside the directory it is extracted into.

    Raises :class:`ArtifactUnreadable` when the bytes are not a gzipped tar, and
    :class:`ArtifactTooLarge` past either ceiling — a bomb stops extraction rather
    than being skipped, because the harm is the writing itself.
    """
    dest.mkdir(parents=True, exist_ok=True)
    entries = 0
    written = 0
    server: list[str] = []
    metadata: list[str] = []
    rejected: list[str] = []

    try:
        tar = tarfile.open(fileobj=io.BytesIO(artifact), mode="r:gz")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ArtifactUnreadable(f"artifact is not a readable gzipped tar: {exc}") from exc

    with tar:
        for member in tar:
            segments = _normalized_member_path(member.name)
            if segments is None:
                rejected.append(member.name)
                continue
            if not segments:  # the archive's own "." root entry
                continue
            if not (member.isfile() or member.isdir()):
                # symlink / hardlink / device / fifo — the escape hatches.
                rejected.append(member.name)
                continue
            if any(seg in _SERVER_ENTRY_NAMES for seg in segments):
                server.append("/".join(segments))
                continue
            if any(seg in _DEPLOY_METADATA_NAMES for seg in segments):
                metadata.append("/".join(segments))
                continue

            target = dest.joinpath(*segments)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            entries += 1
            if entries > MAX_ENTRIES:
                raise ArtifactTooLarge(f"artifact has more than {MAX_ENTRIES} entries")
            written += max(0, member.size)
            if written > MAX_TOTAL_BYTES:
                raise ArtifactTooLarge(f"artifact unpacks to more than {MAX_TOTAL_BYTES} bytes")
            source = tar.extractfile(member)
            if source is None:  # pragma: no cover - isfile() implies a payload
                rejected.append(member.name)
                entries -= 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as fh:
                shutil.copyfileobj(source, fh)

    return UnpackedArtifact(
        entries=entries,
        bytes_written=written,
        server_entries=tuple(server),
        metadata_entries=tuple(metadata),
        rejected=tuple(rejected),
    )


def store_artifact(site_id: str, artifact: bytes, *, engine: str) -> PreviewSnapshot:
    """Unpack ``artifact`` as this site's preview, replacing any previous one.

    Refuses an engine whose static output is the project ROOT (``html``, whose
    ``static_output_rel`` is ``"."``), mirroring the guard in
    ``daytona_build.artifact_tar_command``: such an engine runs no build, so it never
    produces an artifact, and one arriving here means a routing bug that should be
    loud rather than previewed.

    Cross-checks the artifact against ``emits_server_worker(engine)`` and WARNS on a
    mismatch — a react artifact carrying a server entry, or a svelte one missing it,
    means the build lane changed shape underneath us. It is a warning and not a
    refusal because either way the static tree is still previewable, and a preview
    that refused to open would hide the discrepancy instead of surfacing it.

    Unpacks into a temporary sibling and swaps it in, so a failure part-way through
    leaves the PREVIOUS preview intact rather than a half-written tree serving a
    mixture of two builds.
    """
    site_id = _check_site_id(site_id)
    engine = normalize_engine(engine)
    if static_output_rel(engine) == ".":
        raise ArtifactRejected(
            f"engine {engine!r} emits its static output at the project root and runs no "
            "build, so it produces no artifact to preview"
        )

    root = previews_home() / site_id
    incoming = root.parent / f".{site_id}.incoming-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        unpacked = unpack_artifact(artifact, incoming)
        # rmtree-then-rename: os.replace cannot clobber an existing directory on
        # Windows. The gap where neither exists is bounded by a local rename and the
        # publish path holds a per-site single-flight, so two stores cannot race here.
        if root.exists():
            shutil.rmtree(root)
        incoming.replace(root)
    except BaseException:
        shutil.rmtree(incoming, ignore_errors=True)
        raise

    if unpacked.server_entries and not emits_server_worker(engine):
        logger.warning(
            "sites.artifact_preview: %s artifact for site %s carried a server entry (%s) "
            "even though this engine emits none — the build lane may have changed shape",
            engine,
            site_id,
            ", ".join(unpacked.server_entries),
        )
    elif emits_server_worker(engine) and not unpacked.server_entries:
        logger.warning(
            "sites.artifact_preview: %s artifact for site %s carried NO server entry, "
            "though this engine always emits one — the artifact may be incomplete",
            engine,
            site_id,
        )
    if unpacked.rejected:
        logger.warning(
            "sites.artifact_preview: refused %d unsafe member(s) from site %s's artifact: %s",
            len(unpacked.rejected),
            site_id,
            ", ".join(unpacked.rejected[:5]),
        )

    return PreviewSnapshot(
        site_id=site_id,
        engine=engine,
        root=root,
        url_path=f"/{PREVIEW_URL_PREFIX}/{site_id}/",
        unpacked=unpacked,
    )


def safe_store_artifact(
    site_id: str, artifact: bytes | None, *, engine: str
) -> PreviewSnapshot | None:
    """:func:`store_artifact` that never raises — returns ``None`` on any failure.

    The form a PUBLISH path may call. A publish that succeeded must not be reported as
    failed because a preview could not be unpacked, so this swallows everything the
    way ``screenshot.safe_take_*`` does for card images. The strict form stays
    available for a caller that is asking for a preview and is entitled to the error.
    """
    if not artifact:
        return None
    try:
        return store_artifact(site_id, artifact, engine=engine)
    except Exception:  # noqa: BLE001 — a preview must never cost anybody a publish
        logger.warning(
            "sites.artifact_preview: could not store a preview for site %s", site_id, exc_info=True
        )
        return None


# ---------------------------------------------------------------------------
# Resolving requests
# ---------------------------------------------------------------------------

_STANDARD_HEADERS: tuple[tuple[str, str], ...] = (
    # A preview is re-stored on every rebuild, so a cached copy is a preview of the
    # PREVIOUS build — the exact confusion the feature exists to remove.
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
)


def _page(title: str, detail: str) -> bytes:
    """A minimal explanatory page. Plain and self-contained: a refusal has to be
    readable inside an iframe with no styling of ours available."""
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        f"<title>{title}</title>"
        '<body style="font:14px/1.6 system-ui,sans-serif;padding:2rem;max-width:34rem">'
        f'<h1 style="font-size:1.1rem;margin:0 0 .5rem">{title}</h1>'
        f'<p style="margin:0;color:#444">{detail}</p>'
        "</body></html>"
    ).encode()


def _refusal(status: int, reason: str, title: str, detail: str) -> PreviewResponse:
    return PreviewResponse(
        status=status,
        reason=reason,
        content_type="text/html; charset=utf-8",
        body=_page(title, detail),
        headers=_STANDARD_HEADERS + (("X-Paw-Preview", reason),),
    )


def content_type_for(name: str) -> str:
    """Content type for a filename, explicit table first.

    ``mimetypes`` is consulted only for extensions the table does not name. It is not
    consulted FIRST because on Windows it reads the registry, where ``.js`` has been
    observed mapping to ``text/plain`` — served that way the browser refuses to
    execute the bundle and the previewed page never hydrates.
    """
    suffix = Path(name).suffix.lower()
    explicit = _CONTENT_TYPES.get(suffix)
    if explicit:
        return explicit
    guessed, _ = mimetypes.guess_type(name)
    return guessed or _OCTET_STREAM


def _request_segments(subpath: str) -> tuple[str, ...] | None:
    """Segments of a request subpath, or ``None`` when it is not safe.

    Percent-decodes FIRST: ``..%2f..%2fsecret`` is a traversal attempt that only looks
    safe before decoding, and this is the layer that has to see through it.

    A DRIVE-QUALIFIED segment is refused, which is not a theoretical case: pathlib
    treats ``C:`` as an anchor, so ``root.joinpath("C:", "Windows")`` returns
    ``C:/Windows`` and the join silently leaves the preview root. On POSIX the same
    segment is an ordinary filename, so the check costs nothing and closes a hole that
    exists on the platform this is developed on.

    Refusals are separate statements for the same reason as in
    :func:`_normalized_member_path`: so each one can be mutated on its own and shown
    to be load-bearing. Refusing here is not the last line of defence — the caller
    still resolves and contains (:func:`_contained`) — but it is the layer that sees
    the request as the client wrote it, before any join has happened.
    """
    decoded = unquote(subpath or "")
    if "\x00" in decoded:
        # A NUL truncates the path for any C-level consumer, so it can make a
        # suffix or extension check agree with a different file than the one opened.
        return None
    if "\\" in decoded:
        return None
    segments: list[str] = []
    for raw in decoded.split("/"):
        if raw in ("", "."):
            continue
        if raw == "..":
            return None
        if len(raw) > 1 and raw[1] == ":":
            return None
        segments.append(raw)
    return tuple(segments)


def _contained(root: Path, target: Path) -> Path | None:
    """``target``'s real path when it is inside ``root``, else ``None``.

    RESOLVE, THEN CONTAIN — in that order, and on the REAL path rather than the string.
    Sanitising the request and trusting the result is the version of this that fails: a
    prefix check on the raw string is defeated by percent-encoding (handled a layer up)
    and by a SYMLINK, which no amount of string inspection can see. Only comparing the
    resolved path against the resolved root catches a link inside the tree that points
    out of it. Both sides are resolved because the root itself can sit under a link —
    a temp dir on macOS does (``/tmp`` → ``/private/tmp``), and comparing a resolved
    target against an unresolved root would then refuse every legitimate request.

    Returning the path rather than a bool is deliberate: the caller cannot end up
    holding a path it never contained, and there is no branch where the containment
    check is skipped but a resolved path exists. An earlier version set ``inside =
    False`` in an ``except OSError`` and left ``resolved`` unassigned — safe only
    because that branch happened to return, which is exactly the coupling that breaks
    when someone adds a branch later. pyright flagged it as possibly-unbound; on a
    path-resolution routine that reads as a containment bug waiting to happen, not a
    lint.
    """
    try:
        real_root = root.resolve()
        real = target.resolve()
    except OSError:  # pragma: no cover - an unresolvable path is refused, not raised
        return None
    if real == real_root or real.is_relative_to(real_root):
        return real
    return None


def resolve(site_id: str, subpath: str, *, method: str = "GET") -> PreviewResponse:
    """Answer one preview request. Pure enough to test: reads disk, writes nothing.

    ``subpath`` is the URL path AFTER the site's mount point — ``""``, ``"/"``,
    ``"/assets/app.js"``. The distinction between ``""`` and ``"/"`` is load-bearing:
    a page served without a trailing slash resolves its own ``./assets/...`` links one
    level too high and loads with no CSS or JS, so ``""`` is a 301 rather than the
    index.

    The order of the checks is deliberate. Policy (no backend, no writes) is answered
    before existence, so a form POST gets the same honest refusal whether or not an
    artifact happens to be stored; and the refusals are answered before any disk read,
    so a refused path never touches the filesystem.
    """
    try:
        site_id = _check_site_id(site_id)
    except BadSiteId:
        return _refusal(404, "bad_site_id", "No preview here", "That preview address isn't valid.")

    segments = _request_segments(subpath)
    if segments is None:
        return _refusal(404, "unsafe_path", "No preview here", "That path isn't valid.")

    if segments and segments[0] == "api":
        # Lead capture is deferred, so there is no backend behind a preview. Saying so
        # is the point: a POST that returned somebody's own page with a 200 would read
        # as a successful submission and lose the message.
        return _refusal(
            501,
            "backend_not_served",
            "Not available in preview",
            "This preview serves the built pages only — form submissions and other "
            "API calls aren't handled here. Publish the site to enable them.",
        )

    if method.upper() not in ("GET", "HEAD"):
        return _refusal(
            405,
            "method_not_allowed",
            "Not available in preview",
            "A preview serves pages; it can't accept submissions. Publish the site to enable them.",
        )

    root = preview_root(site_id)
    if not root.is_dir():
        return _refusal(
            404,
            "no_preview",
            "Nothing built yet",
            "No build has been previewed for this site yet.",
        )

    if any(seg in _SERVER_ENTRY_NAMES for seg in segments):
        # 404, not 403: a static preview HAS no server entry to serve, and this
        # bundle has carried a per-site signed key in the past — its source must
        # never come back over the wire, and a 403 would confirm it is there.
        return _refusal(
            404, "server_entry_refused", "Not found", "There's no such page in this preview."
        )
    if any(seg in _DEPLOY_METADATA_NAMES or seg.startswith(".") for seg in segments):
        return _refusal(
            404, "metadata_refused", "Not found", "There's no such page in this preview."
        )

    resolved = _contained(root, root.joinpath(*segments) if segments else root)
    if resolved is None:
        # A DISTINCT reason from the parser's ``unsafe_path``, though the client sees the
        # same 404 and the same page. The two layers refuse for different reasons, and
        # with one shared reason string a mutation that disabled the parser would still
        # look caught — the containment check would refuse the same request and the test
        # would pass. Separate reasons are what make each layer independently provable.
        return _refusal(404, "escaped_root", "No preview here", "That path isn't valid.")

    if resolved.is_dir():
        if not subpath.endswith("/"):
            return PreviewResponse(
                status=301,
                reason="redirect_slash",
                location=(subpath or "") + "/",
                headers=_STANDARD_HEADERS + (("X-Paw-Preview", "redirect_slash"),),
            )
        index = resolved / "index.html"
        if not index.is_file():
            # Never a directory listing. A preview that enumerated the build tree
            # would hand out the artifact's shape to anyone holding the URL.
            return _refusal(
                404, "not_found", "Not found", "There's no page at this address in this preview."
            )
        resolved = index

    if not resolved.is_file():
        # Deliberately NOT rewritten to index.html. An SPA fallback would report a
        # missing asset as a working page, which is the one thing a verification
        # preview must not do.
        return _refusal(
            404, "not_found", "Not found", "There's no page at this address in this preview."
        )

    try:
        body = resolved.read_bytes()
    except OSError as exc:
        logger.warning("sites.artifact_preview: could not read %s (%s)", resolved, exc)
        return _refusal(
            404, "not_found", "Not found", "There's no page at this address in this preview."
        )

    return PreviewResponse(
        status=200,
        reason="ok",
        content_type=content_type_for(resolved.name),
        body=body,
        headers=_STANDARD_HEADERS + (("X-Paw-Preview", "ok"),),
    )


__all__ = [
    "MAX_ENTRIES",
    "MAX_TOTAL_BYTES",
    "PREVIEW_URL_PREFIX",
    "ArtifactRejected",
    "ArtifactTooLarge",
    "ArtifactUnreadable",
    "BadSiteId",
    "PreviewResponse",
    "PreviewSnapshot",
    "UnpackedArtifact",
    "content_type_for",
    "discard_preview",
    "has_preview",
    "preview_root",
    "previews_home",
    "resolve",
    "safe_store_artifact",
    "store_artifact",
    "unpack_artifact",
]
