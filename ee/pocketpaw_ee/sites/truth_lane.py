# ee/pocketpaw_ee/sites/truth_lane.py — the TRUTH LANE. Gates a build artifact for
# previewability, stores the previewable ones through ``artifact_preview``, and refuses
# the rest BY NAME. No HTTP surface of its own; no engine-name guessing.
#
# Created 2026-08-10 (SG-10 wiring). ``artifact_preview.py`` shipped complete and
# UNWIRED — a correct, guarded, mutation-tested static server for exactly the tree the
# ephemeral build lane emits, with no caller anywhere in the product. This module is
# that caller, and it exists because "store the artifact and serve it" is not
# sufficient: on one of our four engine shapes the artifact the lane tars CANNOT RUN,
# and storing it anyway produces the one failure mode a verification preview must not
# have.
#
# WHAT "TRUTH LANE" MEANS, AND WHY IT CONSTRAINS EVERY BRANCH BELOW. The edit lane
# renders an APPROXIMATION of an edit in the browser (an in-tab svelte compile is not a
# Vite build). This lane renders the real built artifact, so it is the only surface
# permitted to assert the site is correct. An assertion surface that is sometimes wrong
# is worse than none at all — the user stops being able to tell which lane to believe.
# So every path here resolves to "faithful" or "refused, with a reason". There is no
# "close enough" branch, and adding one would remove this module's reason to exist.
#
# ┌───────────────────────────────────────────────────────────────────────────────────┐
# │ THE FACT THIS MODULE IS BUILT AROUND: A WORKER-BEARING ARTIFACT CANNOT EXECUTE     │
# │ HERE, SO IT IS REFUSED RATHER THAN PARTIALLY SERVED.                               │
# └───────────────────────────────────────────────────────────────────────────────────┘
#
# ``adapter-cloudflare`` emits a ``_worker.js`` that is the RENDERER, and the artifact
# ships a ``_routes.json`` handing it ``/*`` minus the immutable assets. Two independent
# reasons that tree cannot be previewed by a static file server:
#
#   1. WE CANNOT RUN IT. This lane serves files; it has no workerd, no Node, no D1
#      binding and no ``hooks.server.ts`` gate. Whatever the worker would have rendered
#      is simply absent.
#   2. IT IS NOT EVEN COMPLETE. Measured 2026-08-09 (proving record §8 item 14): the
#      emitted worker imports ``../output/server/index.js`` and
#      ``../cloudflare-tmp/manifest.js``, and BOTH sit outside the directory the lane
#      tars. The artifact ships a routing table pointing at a worker that cannot start.
#
# The static files beside that worker still unpack cleanly, and THAT is the trap. Serve
# them and the customer is shown a page their site never serves — an empty shell, or a
# prerendered fallback — and reads it as their site being broken when it is our
# packaging that is. So worker presence is a REFUSAL, not a warning.
#
# The refusal is deliberately coarser than the diagnosis. We do not parse the worker to
# see whether its imports happen to resolve: we cannot execute it either way, so the
# question decides nothing — and the worker bundle has historically carried a
# substituted per-site signed key, which makes reading its bytes to answer a moot
# question a bad trade on its own.
#
# RESOLVED OFF THE ARTIFACT, NEVER OFF THE ENGINE NAME. Since SL-1 the svelte track
# spans two adapters: a static landing site builds on ``adapter-static`` (output
# ``build``, no worker, self-contained, previewable) and a dynamic/auth site on
# ``adapter-cloudflare`` (output ``.svelte-kit/cloudflare``, worker load-bearing, not
# previewable). Both are engine ``"svelte"``. So the gate reads the artifact —
# :func:`assess_artifact` scans tar member names, :func:`assess_project` calls
# ``engines.resolve_emits_server_worker`` — and ``engines.emits_server_worker`` is never
# consulted here. A name-based gate would refuse every static svelte landing site and
# admit every dynamic one, i.e. be wrong in both directions at once.
#
# WHY THE GATE RUNS BEFORE THE STORE, NOT AFTER. ``store_artifact`` unpacks into a
# hidden sibling and swaps it in, so gating on its RESULT would leave a window where a
# refused artifact is live at the preview address. Scanning names first costs one cheap
# tar pass and keeps the refusal structural: a refused artifact never lands. The result
# is still cross-checked against the scan afterwards (:data:`REASON_GATE_DISAGREED`) —
# two readings of the same bytes that disagree mean one of them is wrong, and the safe
# response to not knowing which is to serve neither.
#
# A REFUSAL ALSO DISCARDS ANY PREVIOUS PREVIEW, which is the non-obvious half. Leaving
# the last good tree in place would keep answering 200 at the same address for a build
# that was refused — the truth lane asserting correctness about the wrong build. That is
# the same confusion ``artifact_preview``'s ``Cache-Control: no-store`` exists to
# prevent, one level up, so it gets the same answer.
#
# THE EXPOSURE SEAM IS A SEAM ON PURPOSE — see :func:`preview_base_url`. Which origin
# serves preview content to a browser is an OPEN CAPTAIN DECISION (research doc §8
# item 3), and the proving record's §9f warns explicitly that it must not be settled as
# a byproduct of a preview slice. So the default is loopback, which needs no decision,
# and every mode that would settle the question refuses with a message naming what has
# to be decided. Choosing later is one environment variable, not a rewrite.
from __future__ import annotations

import io
import logging
import os
import tarfile
from dataclasses import dataclass
from pathlib import Path

from pocketpaw_ee.sites import artifact_preview

# ``_SERVER_ENTRY_NAMES`` is IMPORTED rather than mirrored, private name and all. A local
# copy plus a drift test is the other option and it is strictly worse here: the two sets
# have to agree by construction, because a name this gate does not recognise as a server
# entry is a name ``unpack_artifact`` would happily write into a served tree.
from pocketpaw_ee.sites.artifact_preview import (
    _SERVER_ENTRY_NAMES as SERVER_ENTRY_NAMES,
)
from pocketpaw_ee.sites.artifact_preview import (
    ArtifactRejected,
    PreviewSnapshot,
    UnpackedArtifact,
)
from pocketpaw_ee.sites.engines import (
    normalize_engine,
    resolve_emits_server_worker,
    resolve_static_output_rel,
    static_output_rel,
)

logger = logging.getLogger(__name__)

#: ``adapter-cloudflare``'s routing table. Its presence means the build expected a
#: worker to answer requests. Skipped at unpack by ``artifact_preview`` (it is deploy
#: configuration, not content) — this module reads it as EVIDENCE, which is a different
#: job from deciding whether to serve it.
ROUTING_TABLE_NAME = "_routes.json"

#: The document a preview opens at. An artifact without one has nothing to show, and
#: saying so beats opening the preview onto its own 404 page.
ENTRY_DOCUMENT_NAME = "index.html"

# ---------------------------------------------------------------------------
# Refusal reasons
# ---------------------------------------------------------------------------
#
# Named constants rather than inline strings so a caller can branch on the reason and a
# test can assert one without pinning prose. Each is a DISTINCT reason, for the same
# rationale ``artifact_preview.resolve`` gives for keeping ``unsafe_path`` and
# ``escaped_root`` apart: with one shared string, a mutation that disabled one check
# would still look caught because a different check refused the same input.

#: The build produced no artifact at all (nothing to preview, and not an error).
REASON_NO_ARTIFACT = "no_artifact"

#: The engine runs no build, so it never produces an artifact. Mirrors the refusals in
#: ``artifact_preview.store_artifact`` and ``daytona_build.artifact_tar_command``.
REASON_ENGINE_RUNS_NO_BUILD = "engine_runs_no_build"

#: The bytes are not a readable gzipped tar.
REASON_ARTIFACT_UNREADABLE = "artifact_unreadable"

#: The artifact's pages are rendered by a worker this lane cannot execute. THE reason
#: this module exists; see the header.
REASON_SERVER_RENDERED = "server_rendered_artifact"

#: The artifact carries a routing table naming a renderer the artifact does not contain.
#: Proving record §8 item 14's incompleteness, seen from the other side.
REASON_INCOMPLETE_ARTIFACT = "incomplete_artifact"

#: The artifact has no root ``index.html``.
REASON_NO_ENTRY_DOCUMENT = "no_entry_document"

#: ``store_artifact`` refused the artifact (too large, unsafe, bad site id). A fact about
#: the artifact, and a typed, expected outcome.
REASON_ARTIFACT_REJECTED = "artifact_rejected"

#: Storing broke in a way nothing anticipated (a full disk, a permission error). A fact
#: about US, deliberately not folded into :data:`REASON_ARTIFACT_REJECTED`: telling a
#: customer their build output was rejected when our disk filled up sends them to debug
#: their site.
REASON_STORE_FAILED = "store_failed"

#: The pre-store scan and the unpack disagreed about whether a server entry is present.
REASON_GATE_DISAGREED = "gate_disagreed"

#: Faithful, stored, serveable.
REASON_OK = "ok"

# ---------------------------------------------------------------------------
# The exposure seam
# ---------------------------------------------------------------------------

#: Which origin serves preview CONTENT to a browser.
EXPOSURE_ENV = "PAW_SITES_PREVIEW_EXPOSURE"

#: Base URL for :data:`EXPOSURE_PREVIEW_ORIGIN`, e.g. ``https://preview.example.net``.
PREVIEW_ORIGIN_ENV = "PAW_SITES_PREVIEW_ORIGIN"

#: The loopback server this repo already runs (``local_server``). The DEFAULT, and the
#: only mode that decides nothing: 127.0.0.1 is not reachable off the box, so there is
#: no token to design, no cookie mechanic to reason about, and no customer JavaScript
#: executing on an origin that holds a session.
EXPOSURE_LOOPBACK = "loopback"

#: A dedicated origin that serves nothing else. Config is a base URL and nothing more,
#: so the mechanism is here — but turning it on is a hosting change and therefore the
#: captain's call, which is why it is not the default.
EXPOSURE_PREVIEW_ORIGIN = "preview-origin"

#: The app's own origin. REFUSED until decided: customer-authored JavaScript would run
#: somewhere that holds the session cookie, and the obvious mitigation does not work —
#: a CSP ``sandbox`` on the document empties its site-for-cookies, so the page's own
#: same-origin CSS and JS stop loading and a working build renders as a broken one.
EXPOSURE_APP_ORIGIN = "app-origin"

#: A signed, expiring URL. REFUSED until decided: it is a security design in its own
#: right (what is signed, lifetime, revocation, who may mint one), and the standing hard
#: gate is that no per-site signed key may ever reach a client bundle or view-source.
EXPOSURE_SIGNED_URL = "signed-url"

_KNOWN_EXPOSURES = frozenset(
    {EXPOSURE_LOOPBACK, EXPOSURE_PREVIEW_ORIGIN, EXPOSURE_APP_ORIGIN, EXPOSURE_SIGNED_URL}
)


class PreviewExposureNotConfigured(RuntimeError):
    """The configured exposure cannot serve a preview address yet.

    Raised rather than quietly degraded to loopback. Falling back would hand back a
    127.0.0.1 URL to an operator who asked for a public one — a preview that silently
    works only on the API box, discovered by whoever opens the link. Worse, it would let
    the origin question be answered by a default instead of by a decision.
    """


def preview_exposure() -> str:
    """The configured exposure mode.

    An unrecognised value falls back to :data:`EXPOSURE_LOOPBACK` with a warning rather
    than raising: a typo in deploy config should cost the preview its reach, not the
    publish it hangs off. Every mode that could leak content refuses explicitly below,
    so the safe direction for an unknown string is the mode that exposes nothing.
    """
    raw = (os.environ.get(EXPOSURE_ENV) or "").strip().lower()
    if not raw:
        return EXPOSURE_LOOPBACK
    if raw not in _KNOWN_EXPOSURES:
        logger.warning(
            "sites.truth_lane: unknown %s=%r — falling back to %r",
            EXPOSURE_ENV,
            raw,
            EXPOSURE_LOOPBACK,
        )
        return EXPOSURE_LOOPBACK
    return raw


def preview_base_url() -> str:
    """Base URL the preview tree is reachable at, without the site's mount path.

    THIS IS THE SEAM the origin decision lands on, and it is one function on purpose:
    picking an exposure is a config change plus, for the two refused modes, the design
    each of them needs. Nothing else in the sites code has to move.

    Raises :class:`PreviewExposureNotConfigured` for a mode whose design is still the
    captain's — see the module header and research doc §8 item 3.
    """
    mode = preview_exposure()
    if mode == EXPOSURE_LOOPBACK:
        # Imported here, not at module scope: ``local_server`` imports this module, so a
        # top-level import would be a cycle. It is also the only thing this module wants
        # from that server, and only in this one branch.
        from pocketpaw_ee.sites import local_server

        return local_server.ensure_server()
    if mode == EXPOSURE_PREVIEW_ORIGIN:
        origin = (os.environ.get(PREVIEW_ORIGIN_ENV) or "").strip().rstrip("/")
        if not origin:
            raise PreviewExposureNotConfigured(
                f"{EXPOSURE_ENV}={EXPOSURE_PREVIEW_ORIGIN!r} needs {PREVIEW_ORIGIN_ENV} "
                "set to the base URL of the origin that serves previews"
            )
        return origin
    if mode == EXPOSURE_APP_ORIGIN:
        raise PreviewExposureNotConfigured(
            "serving previews from the app's own origin is an open decision: customer "
            "JavaScript would execute where the session cookie lives, and a CSP sandbox "
            "on the document breaks the page's own same-origin assets. Pick an exposure "
            "deliberately before enabling this."
        )
    raise PreviewExposureNotConfigured(
        "signed expiring preview URLs are an open decision: what is signed, how long it "
        "lives, how it is revoked, and who may mint one. No per-site signed key may "
        "reach a client bundle or view-source. Design it before enabling this."
    )


def preview_address(site_id: str) -> str | None:
    """Full preview URL for a site, or ``None`` when the exposure cannot serve one.

    The soft form :func:`open_preview` uses. A missing address is not a failure of the
    build or of the artifact, so it must not read as one — the verdict stays whatever
    the artifact earned and the caller sees a stored preview with nowhere to serve it
    from yet.
    """
    try:
        base = preview_base_url()
    except PreviewExposureNotConfigured as exc:
        logger.warning("sites.truth_lane: no preview address for site %s (%s)", site_id, exc)
        return None
    except Exception:  # noqa: BLE001 - ensure_server() binds a socket and may fail
        logger.warning(
            "sites.truth_lane: could not resolve a preview address for site %s",
            site_id,
            exc_info=True,
        )
        return None
    return f"{base}/{artifact_preview.PREVIEW_URL_PREFIX}/{site_id}/"


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactShape:
    """What a NAME-LEVEL scan of an artifact found. Nothing is extracted or read.

    Names only, deliberately. The gate's questions ("is there a renderer we cannot
    run", "is there an entry document") are all answerable from the member list, and
    the one file whose contents might tempt a reader — the worker bundle, which has
    carried a substituted per-site signed key — is therefore never opened.
    """

    entries: int
    server_entries: tuple[str, ...]
    routing_tables: tuple[str, ...]
    has_entry_document: bool

    @property
    def is_server_rendered(self) -> bool:
        return bool(self.server_entries)

    @property
    def declares_absent_renderer(self) -> bool:
        """A routing table with no worker beside it: the artifact names a renderer it
        does not contain. Distinct from :attr:`is_server_rendered` because the harm is
        different — that one is a tree we cannot run, this one is a tree that is not all
        there — and because a single combined check would report both as one cause."""
        return bool(self.routing_tables) and not self.server_entries


@dataclass(frozen=True)
class TruthLaneVerdict:
    """Whether an artifact can be previewed faithfully, and why not when it cannot."""

    previewable: bool
    reason: str
    detail: str
    shape: ArtifactShape | None = None


@dataclass(frozen=True)
class TruthLanePreview:
    """The outcome of opening the truth lane on one artifact.

    ``verdict`` and ``url`` are INDEPENDENT facts and are kept apart on purpose: the
    verdict is about the artifact, the address is about where previews are exposed. An
    unconfigured exposure must not read as a broken build, and a refused build must not
    read as a missing config.
    """

    site_id: str
    engine: str
    verdict: TruthLaneVerdict
    exposure: str
    url: str | None = None
    mount_path: str | None = None
    unpacked: UnpackedArtifact | None = None

    @property
    def ok(self) -> bool:
        """Serveable: a faithful artifact AND an address to reach it at."""
        return self.verdict.previewable and self.url is not None


def _segments(name: str) -> tuple[str, ...]:
    """Member name split into path segments, ``.`` and empties dropped.

    ``tar -C <dir> .`` names members ``./x/y``. Backslash is treated as a separator too:
    a member named ``_app\\_worker.js`` is a single filename on POSIX and a path on
    Windows, and a gate that read it as a filename would not see the worker at all.
    ``unpack_artifact`` REFUSES such a member outright, which is the right answer there;
    here the job is to notice it, so it is split rather than dropped.
    """
    return tuple(part for part in name.replace("\\", "/").split("/") if part not in ("", "."))


def scan_artifact(artifact: bytes) -> ArtifactShape:
    """Name-level scan of an artifact tarball.

    Raises :class:`artifact_preview.ArtifactUnreadable` when the bytes are not a
    readable gzipped tar, so the caller reports the same cause ``unpack_artifact``
    would.
    """
    try:
        tar = tarfile.open(fileobj=io.BytesIO(artifact), mode="r:gz")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise artifact_preview.ArtifactUnreadable(
            f"artifact is not a readable gzipped tar: {exc}"
        ) from exc

    entries = 0
    server: list[str] = []
    routes: list[str] = []
    entry_document = False
    with tar:
        for member in tar:
            segments = _segments(member.name)
            if not segments:
                continue
            entries += 1
            if any(seg in SERVER_ENTRY_NAMES for seg in segments):
                server.append("/".join(segments))
            if segments[-1] == ROUTING_TABLE_NAME:
                routes.append("/".join(segments))
            if segments == (ENTRY_DOCUMENT_NAME,):
                entry_document = True
    return ArtifactShape(
        entries=entries,
        server_entries=tuple(server),
        routing_tables=tuple(routes),
        has_entry_document=entry_document,
    )


_SERVER_RENDERED_DETAIL = (
    "This build's pages are produced by a Cloudflare Worker, which a preview cannot "
    "run — it serves files, and it has no database binding or sign-in gate. The static "
    "files packaged beside that worker are not the pages your visitors would see, so "
    "showing them would misrepresent the site. Publish it to see the real thing."
)

_INCOMPLETE_DETAIL = (
    "This build's package includes a routing table that hands requests to a server "
    "entry the package does not contain, so it is not complete enough to preview. This "
    "is a packaging problem on our side, not a problem with your site."
)


def _refuse(reason: str, detail: str, shape: ArtifactShape | None = None) -> TruthLaneVerdict:
    return TruthLaneVerdict(previewable=False, reason=reason, detail=detail, shape=shape)


def assess_shape(shape: ArtifactShape, *, engine: str) -> TruthLaneVerdict:
    """Verdict for an already-scanned artifact.

    Split out from :func:`assess_artifact` so the rules are testable without packing a
    tarball, and so the ORDER of the refusals is visible in one place. That order
    matters: the renderer questions come before the entry-document one, because a
    worker-rendered artifact may legitimately have no ``index.html`` at all and
    reporting that as the cause would send someone looking at the wrong thing.
    """
    if shape.is_server_rendered:
        return _refuse(REASON_SERVER_RENDERED, _SERVER_RENDERED_DETAIL, shape)
    if shape.declares_absent_renderer:
        return _refuse(REASON_INCOMPLETE_ARTIFACT, _INCOMPLETE_DETAIL, shape)
    if not shape.has_entry_document:
        return _refuse(
            REASON_NO_ENTRY_DOCUMENT,
            "This build produced no home page, so there is nothing to open. The build "
            "may have written its output somewhere the packaging step did not look.",
            shape,
        )
    return TruthLaneVerdict(
        previewable=True,
        reason=REASON_OK,
        detail=f"{engine} build packaged {shape.entries} files and renders as static pages.",
        shape=shape,
    )


def assess_artifact(artifact: bytes | None, *, engine: str) -> TruthLaneVerdict:
    """Can this artifact be previewed faithfully?

    Reads the ARTIFACT, never the engine name — see the module header. The engine is
    consulted for exactly one thing, the same thing ``artifact_preview.store_artifact``
    consults it for: whether the engine belongs in this lane at all (an engine whose
    static output is the project root runs no build, so an artifact arriving for it is a
    routing bug and should be loud).
    """
    normalized = normalize_engine(engine)
    if static_output_rel(normalized) == ".":
        return _refuse(
            REASON_ENGINE_RUNS_NO_BUILD,
            f"Sites on the {normalized} engine run no build step, so there is no build "
            "to preview. The published pages are the authored files.",
        )
    if not artifact:
        return _refuse(
            REASON_NO_ARTIFACT,
            "No build output has come back for this site yet, so there is nothing to preview.",
        )
    try:
        shape = scan_artifact(artifact)
    except ArtifactRejected as exc:
        logger.warning(
            "sites.truth_lane: artifact for a %s site is unreadable (%s)", normalized, exc
        )
        return _refuse(
            REASON_ARTIFACT_UNREADABLE,
            "The build output could not be read, so it cannot be previewed. The download "
            "may have been truncated; building again should produce a readable one.",
        )
    return assess_shape(shape, engine=normalized)


def assess_project(project_dir: str | os.PathLike[str], engine: str) -> TruthLaneVerdict:
    """Verdict for a build that is still a PROJECT DIR rather than a tarball.

    The pre-tar half of the same question, for a locally-built site. It resolves both
    facts off disk — ``resolve_static_output_rel`` for where this build's output landed,
    ``resolve_emits_server_worker`` for whether it emitted a renderer — because since
    SL-1 the engine name answers neither for svelte. Kept in step with
    :func:`assess_shape` by returning the same reasons; the shape is ``None`` because
    there is no tarball to have scanned.
    """
    normalized = normalize_engine(engine)
    if static_output_rel(normalized) == ".":
        return _refuse(
            REASON_ENGINE_RUNS_NO_BUILD,
            f"Sites on the {normalized} engine run no build step, so there is no build "
            "to preview. The published pages are the authored files.",
        )
    if resolve_emits_server_worker(project_dir, normalized):
        return _refuse(REASON_SERVER_RENDERED, _SERVER_RENDERED_DETAIL)
    out_dir = Path(project_dir) / resolve_static_output_rel(project_dir, normalized)
    if (out_dir / ROUTING_TABLE_NAME).exists():
        return _refuse(REASON_INCOMPLETE_ARTIFACT, _INCOMPLETE_DETAIL)
    if not (out_dir / ENTRY_DOCUMENT_NAME).is_file():
        return _refuse(
            REASON_NO_ENTRY_DOCUMENT,
            "This build produced no home page, so there is nothing to open. The build "
            "may have written its output somewhere the packaging step did not look.",
        )
    return TruthLaneVerdict(
        previewable=True,
        reason=REASON_OK,
        detail=f"{normalized} build at {out_dir.name} renders as static pages.",
    )


# ---------------------------------------------------------------------------
# Opening the lane
# ---------------------------------------------------------------------------


def _refused(site_id: str, engine: str, verdict: TruthLaneVerdict) -> TruthLanePreview:
    """Record a refusal AND drop any preview already stored for this site.

    The discard is the load-bearing half. A previous build's tree left in place keeps
    answering 200 at the same address, so the surface whose whole job is to say "this
    build is correct" would be saying it about a different build. Refusing loudly while
    still serving the old tree is not a refusal.
    """
    if artifact_preview.discard_preview(site_id):
        logger.info(
            "sites.truth_lane: discarded site %s's previous preview — this build is refused (%s)",
            site_id,
            verdict.reason,
        )
    return TruthLanePreview(
        site_id=site_id,
        engine=engine,
        verdict=verdict,
        exposure=preview_exposure(),
    )


def open_preview(site_id: str, artifact: bytes | None, *, engine: str) -> TruthLanePreview:
    """Gate an artifact, store it when it is faithful, and return where to reach it.

    THE WIRED ENTRY POINT of the truth lane — one call for a build lane's tail or a
    per-site preview action to make. Never raises: every outcome is a
    :class:`TruthLanePreview`, because this hangs off a build that may already have
    succeeded and a preview must never be able to fail one.

    Storing goes through ``artifact_preview.store_artifact`` unchanged, so the unpack
    guards, the ``_worker.js`` skip and the size ceilings all still apply. What this
    adds in front of them is the previewability question they do not ask.
    """
    normalized = normalize_engine(engine)
    verdict = assess_artifact(artifact, engine=normalized)
    # ``artifact is None`` is already covered by the verdict (REASON_NO_ARTIFACT); it is
    # repeated here to narrow the type for the checker rather than to assert a fact.
    if not verdict.previewable or artifact is None:
        return _refused(site_id, normalized, verdict)

    try:
        snapshot: PreviewSnapshot = artifact_preview.store_artifact(
            site_id, artifact, engine=normalized, expect_server_worker=False
        )
    except ArtifactRejected as exc:
        # The store REFUSED the artifact — a typed, expected outcome (too large, unsafe
        # member, bad site id). Reported separately from the clause below because they
        # are different facts: this one says something about the artifact, that one says
        # something broke on our side. One shared reason would also make the two clauses
        # indistinguishable, and a mutation removing either would still look caught.
        logger.warning("sites.truth_lane: store refused site %s's artifact (%s)", site_id, exc)
        return _refused(
            site_id,
            normalized,
            _refuse(
                REASON_ARTIFACT_REJECTED,
                "The build output could not be unpacked for preview.",
                verdict.shape,
            ),
        )
    except Exception:  # noqa: BLE001 - a preview must never cost anybody a build
        logger.warning(
            "sites.truth_lane: could not store site %s's preview", site_id, exc_info=True
        )
        return _refused(
            site_id,
            normalized,
            _refuse(
                REASON_STORE_FAILED,
                "The preview could not be prepared. The site itself is unaffected.",
                verdict.shape,
            ),
        )

    if snapshot.unpacked.server_entries:
        # The scan said no server entry and the unpack found one. One of the two
        # readings of these bytes is wrong and nothing here can tell which, so serve
        # neither: the stored tree is dropped and the build is refused.
        logger.error(
            "sites.truth_lane: site %s's artifact scanned clean but unpacked a server "
            "entry (%s) — refusing rather than serving a tree we cannot vouch for",
            site_id,
            ", ".join(snapshot.unpacked.server_entries),
        )
        return _refused(
            site_id,
            normalized,
            _refuse(
                REASON_GATE_DISAGREED,
                "The build output could not be checked consistently, so it is not being "
                "previewed. This is a packaging problem on our side.",
                verdict.shape,
            ),
        )

    return TruthLanePreview(
        site_id=site_id,
        engine=normalized,
        verdict=verdict,
        exposure=preview_exposure(),
        url=preview_address(site_id),
        mount_path=snapshot.url_path,
        unpacked=snapshot.unpacked,
    )


__all__ = [
    "ENTRY_DOCUMENT_NAME",
    "EXPOSURE_APP_ORIGIN",
    "EXPOSURE_ENV",
    "EXPOSURE_LOOPBACK",
    "EXPOSURE_PREVIEW_ORIGIN",
    "EXPOSURE_SIGNED_URL",
    "PREVIEW_ORIGIN_ENV",
    "REASON_ARTIFACT_REJECTED",
    "REASON_ARTIFACT_UNREADABLE",
    "REASON_ENGINE_RUNS_NO_BUILD",
    "REASON_GATE_DISAGREED",
    "REASON_INCOMPLETE_ARTIFACT",
    "REASON_NO_ARTIFACT",
    "REASON_NO_ENTRY_DOCUMENT",
    "REASON_OK",
    "REASON_SERVER_RENDERED",
    "REASON_STORE_FAILED",
    "ROUTING_TABLE_NAME",
    "SERVER_ENTRY_NAMES",
    "ArtifactShape",
    "PreviewExposureNotConfigured",
    "TruthLanePreview",
    "TruthLaneVerdict",
    "assess_artifact",
    "assess_project",
    "assess_shape",
    "open_preview",
    "preview_address",
    "preview_base_url",
    "preview_exposure",
    "scan_artifact",
]
