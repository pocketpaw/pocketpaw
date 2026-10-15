# ee/pocketpaw_ee/sites/project_build.py — the ``project`` engine's draft build and
# the reads/gates its publish needs.
#
# A project pocket's source map is a whole repo the author owns. Two lanes build it:
#   * no-build (a ``paw.template.json`` with ``"build": "none"``, the ``static``
#     starter; or, with no template manifest, no package.json but a root index.html):
#     the tree IS the site. :func:`build_no_build_draft` stages it in-process, no
#     sandbox: the files minus :func:`excluded_from_static_output` (paw-sites'
#     ``excludedFromStaticOutput``) under ``STATIC_OUT_DIR``, an assets-only
#     ``paw-build.json`` (``workerModules: []``), and paw.js when the site qualifies
#     (``paw_client``); its preview pages (never the bundle) carry ``stamp-html``'s
#     ``data-paw-src`` stamps when the generator has it. Preview mode always ``"full"``.
#   * everything else builds in a Daytona sandbox: the source map is uploaded as the
#     project tree, the vendored generator beside it (plus ``templates/_paw/paw.js``),
#     and ``paw-sites-gen project-build`` installs, builds and writes ``paw-build.json``;
#     ``--site-config`` rides only a generator that knows it (queued builds have the
#     key scrubbed, so paw.js lands at publish). On a generator with ``--source-tags``
#     a draft builds twice: untagged, then tagged into :data:`TAGGED_STAGE` for the
#     preview (:func:`build_script`). A stage step copies each manifest and its files
#     into ``engines.PROJECT_STAGE_REL``, which the wrapper tars.
#
# What a finished build leaves, all keyed by ``(pocket_id, content_hash)``:
#   * the whole UNTAGGED bundle in the artifact store under ``<hash>.bundle``: what
#     publish deploys through ``bundle_deploy`` (a no-build publish re-stages from the
#     source with the live site config instead), so no tag or stamp ever ships;
#   * the (tagged when there is one) assets as the draft's preview files + token
#     (``preview_origin``), unless they have no root index.html (``preview_mode``);
#   * a build record in ``verify_store`` (``build-<job id>``): status, rung, the
#     redacted and capped build log, ``preview_mode`` and the framework; plus the
#     pocket's ``latest-build`` pointer.
#
# Invariants: nothing here runs author code on this host; the content hash is the
# source plus the draft tag mode, so the site config (id + signed key) never moves it:
# a draft keeps the paw.js it was built with, and publish re-applies paw.js
# with the live config (:func:`apply_paw_client_to_bundle`, idempotent) and resolves
# the lead-form capture placeholders (:func:`apply_capture_tokens_to_bundle`). The build log
# is scrubbed (``verify_diagnostics.scrub_log_text``) before it is stored;
# ``preview_mode`` is ``"full"`` when the assets are the whole site or a draft Worker
# serves the build (``draft_worker``, behind ``PAW_SITES_DRAFT_WORKERS``; its fallback
# rung is the record's ``draft_worker_reason``), else ``"static"`` for a worker build,
# and ``"server_only"`` when a worker build's assets have no root index.html (next,
# sveltekit): no preview files are stored then, because the URL would only 404.
# ``"published"`` is never a build's mode: ``service._project_draft_artifact`` answers
# it for the pocket's published content once its drafts were purged. No realtime
# event exists for site builds, so build progress is polled (latest-build /
# native-artifact).
from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites import capacity, durable_objects
from pocketpaw_ee.sites.engines import (
    PAW_BUILD_FILENAME,
    PROJECT_STAGE_REL,
    manifest_has_worker,
    safe_rel,
)

logger = logging.getLogger(__name__)

ENGINE = "project"

#: Bumped when this lane's build or bundle shape changes, so drafts rebuild.
BUILD_FORMAT = "project-build-1"

#: The persisted build log keeps this many bytes from the END of the output.
LOG_CAP_BYTES = 64 * 1024

#: Source-map ceilings checked before a sandbox is spent.
MAX_SOURCE_FILES = 5_000
MAX_SOURCE_BYTES = 50 * 1024 * 1024

#: Where the vendored generator lives on this host (the image layout), and the env
#: knob a dev box points at a paw-sites checkout.
GEN_DIR_ENV = "PAW_SITES_GEN_DIR"
_DEFAULT_GEN_DIR = "/opt/paw-sites"

SANDBOX_GEN_DIR = "/tmp/paw-sites-gen"
SANDBOX_BUILD_SCRIPT = "/tmp/paw-project-build.sh"
SANDBOX_STAGE_SCRIPT = "/tmp/paw-project-stage.py"

#: Dirs never uploaded from a source map (the sandbox runs its own install).
_SKIPPED_SEGMENTS = frozenset({"node_modules", ".git"})

#: Files in an assets dir that are deploy configuration, not served content.
_PREVIEW_SKIP = frozenset({"_headers", "_redirects", ".assetsignore", "_routes.json"})

#: Binding types a worker script may request on the free plan (captain, 2026-10-07:
#: free = static + light D1/KV). ``assets`` only exposes the site's own files.
FREE_WORKER_BINDINGS = frozenset({"d1", "kv", "assets"})

#: The starter manifest a template copy carries, and its no-build marker.
TEMPLATE_MANIFEST = "paw.template.json"
NO_BUILD = "none"
#: Where a no-build project's served files are staged (paw-sites ``STATIC_OUT_DIR``).
STATIC_OUT_DIR = ".paw/static"
#: Root files of a no-build project that are tooling, never served (paw-sites
#: ``STATIC_ROOT_EXCLUDES``).
STATIC_ROOT_EXCLUDES = frozenset(
    {
        "AGENTS.md",
        TEMPLATE_MANIFEST,
        "paw-build.json",
        "paw.recipes.json",
        "package.json",
        "bun.lock",
        "bun.lockb",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "wrangler.json",
        "wrangler.jsonc",
        "wrangler.toml",
    }
)

#: Where the site config file goes in the sandbox (``project-build --site-config``).
SANDBOX_SITE_CONFIG = "/tmp/paw-site-config.json"

BUILD_KEY_PREFIX = "build-"

#: Statuses a build record carries. ``queued`` / ``building`` / ``failed`` are the
#: publish lane's wire vocabulary; ``built`` is a finished, stored draft.
STATUS_BUILT = "built"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def draft_tag_mode(source: Mapping[str, Any] | None) -> str | None:
    """How this host marks a draft of ``source`` for visual editing: ``"stamp-html"``
    (a no-build tree, its preview pages stamped in-process), ``"source-tags"`` (a
    sandbox build, ``project-build --source-tags``), or ``None`` when the generator
    has neither. Publish output never carries either."""
    from pocketpaw_ee.sites import visual_edit

    if is_no_build(source):
        return "stamp-html" if visual_edit.stamp_html_supported() else None
    return "source-tags" if visual_edit.source_tags_supported() else None


def project_content_hash(source: Mapping[str, Any]) -> str:
    """The draft's content hash: the source map, the engine and this lane's format.
    The generator version rides it too, so a new ``project-build`` rebuilds drafts,
    and so does :func:`draft_tag_mode` when it is on, so an untagged draft is never
    served as a tagged one (with it off the hash is what it always was)."""
    from pocketpaw_ee.sites.generator_client import generator_version

    parts = [
        BUILD_FORMAT,
        generator_version(),
        ENGINE,
        json.dumps(dict(source), sort_keys=True, separators=(",", ":"), ensure_ascii=False),
    ]
    tag_mode = draft_tag_mode(source)
    if tag_mode:
        parts.append(f"tags:{tag_mode}")
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def project_files(source: Mapping[str, Any] | None) -> dict[str, bytes]:
    """The source map as the ``{relpath: bytes}`` tree to upload. Raises
    ``ValidationError`` for a path that leaves the project, a non-text value, an empty
    project, or one over the source ceilings. ``node_modules`` / ``.git`` are dropped."""
    if not isinstance(source, Mapping) or not source:
        raise ValidationError("sites.project_empty", "This project has no files to build.")
    files: dict[str, bytes] = {}
    total = 0
    for raw, contents in source.items():
        rel = safe_rel(raw)
        if rel is None or rel == ".":
            raise ValidationError(
                "sites.project_bad_path", f"The project file path {raw!r} is not allowed."
            )
        if any(seg in _SKIPPED_SEGMENTS for seg in rel.split("/")):
            continue
        if not isinstance(contents, str):
            raise ValidationError(
                "sites.project_bad_file", f"The project file {rel!r} must be text."
            )
        data = contents.encode("utf-8")
        total += len(data)
        files[rel] = data
    if "package.json" not in files and not files_are_no_build(files):
        raise ValidationError(
            "sites.project_no_package_json",
            "A project site needs a package.json at its root, or a paw.template.json "
            'with "build": "none" and an index.html.',
        )
    if len(files) > MAX_SOURCE_FILES or total > MAX_SOURCE_BYTES:
        raise ValidationError(
            "sites.project_too_large",
            f"This project is over the build limits ({MAX_SOURCE_FILES} files, "
            f"{MAX_SOURCE_BYTES // (1024 * 1024)} MiB of source).",
        )
    return files


# ---------------------------------------------------------------------------
# No-build projects
# ---------------------------------------------------------------------------


def _template_manifest(files: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = files.get(TEMPLATE_MANIFEST)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def files_are_no_build(files: Mapping[str, Any]) -> bool:
    """True for a project tree (normalized relpaths) that is its own output: its
    ``paw.template.json`` says ``"build": "none"``, or it has no template manifest,
    no package.json and a root index.html (paw-sites' ``detectProject`` rule). A
    manifest with a build object always builds, whatever else the tree holds."""
    manifest = _template_manifest(files)
    if manifest is not None and "build" in manifest:
        return manifest.get("build") == NO_BUILD
    return "package.json" not in files and "index.html" in files


def is_no_build(source: Mapping[str, Any] | None) -> bool:
    """:func:`files_are_no_build` over a raw source map (paths not yet normalized)."""
    if not isinstance(source, Mapping):
        return False
    files = {rel: v for raw, v in source.items() if (rel := safe_rel(raw)) not in (None, ".")}
    return files_are_no_build(files)


def excluded_from_static_output(rel: str) -> bool:
    """paw-sites ``excludedFromStaticOutput``: a no-build project path that is not
    served. Dotfiles and dot-dirs at any depth, ``node_modules``, and the root
    tooling files (:data:`STATIC_ROOT_EXCLUDES`)."""
    segments = rel.replace("\\", "/").split("/")
    if any(seg.startswith(".") or seg == "node_modules" for seg in segments):
        return True
    return len(segments) == 1 and segments[0] in STATIC_ROOT_EXCLUDES


def no_build_bundle(
    files: Mapping[str, bytes],
    *,
    site_config: Mapping[str, Any] | None = None,
    client: bytes | None = None,
) -> tuple[dict[str, Any], dict[str, bytes], bool]:
    """``(manifest, bundle files, paw.js shipped)`` of a no-build project: the served
    tree under :data:`STATIC_OUT_DIR` plus an assets-only ``paw-build.json`` in the
    shape paw-sites writes for an ``assets`` target. ``site_config`` (a generator
    ``siteConfig``) tags the pages with paw.js when the source uses it. Raises
    ``ValidationError`` when nothing serves a root index.html."""
    from pocketpaw_ee.sites import paw_client

    served = {rel: data for rel, data in files.items() if not excluded_from_static_output(rel)}
    if "index.html" not in served:
        raise ValidationError(
            "sites.project_no_index",
            "A site without a build step needs an index.html at its root.",
        )
    shipped = paw_client.apply_to_assets(
        served,
        paw_client.client_config(site_config),
        paw_client.source_texts(files),
        client=client,
    )
    template = _template_manifest(files) or {}
    slug = template.get("slug") if isinstance(template.get("slug"), str) else ""
    manifest: dict[str, Any] = {
        "slug": slug or "static",
        "assetsDir": STATIC_OUT_DIR,
        "workerModules": [],
        "compat": {"date": None, "flags": []},
        "bindingRequests": [],
        "framework": "static",
        "sizes": {
            "assetFiles": len(served),
            "assetBytes": sum(len(b) for b in served.values()),
            "workerBytes": 0,
        },
    }
    bundle = {PAW_BUILD_FILENAME: json.dumps(manifest, indent=2).encode("utf-8")}
    bundle.update({f"{STATIC_OUT_DIR}/{rel}": data for rel, data in served.items()})
    return manifest, bundle, shipped


async def build_no_build_draft(
    pocket_id: str,
    content_hash: str,
    source: Mapping[str, Any] | None,
    *,
    store: Any,
    records: Any,
    site_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a no-build draft in-process (no sandbox, well under a second): store the
    bundle under :func:`bundle_key` and the served tree as the preview, and record the
    build. Returns the job's ``{status, reason, preview_mode, job_id}`` shape; a tree
    that cannot serve records ``failed`` with a ``scaffold_failed`` rung. Never raises
    for a build outcome.

    The preview's pages (never the bundle) are stamped with ``data-paw-src`` by the
    generator's ``stamp-html`` when it has it (:func:`_stamped_preview`); a stamp that
    fails serves them unstamped. The stored preview is the cache: one stamp per
    content hash."""
    from pocketpaw_ee.sites import build_job, preview_origin

    job_id = build_job._preview_job_id(pocket_id, content_hash)

    def _finish(status: str, reason: str | None, **extra: Any) -> dict[str, Any]:
        write_build_record(
            records, pocket_id, new_record(job_id, content_hash, status, reason=reason, **extra)
        )
        return {
            "status": status,
            "reason": reason,
            "job_id": job_id,
            "preview_mode": extra.get("preview_mode"),
        }

    try:
        files = project_files(source)
        manifest, bundle, shipped = no_build_bundle(files, site_config=site_config)
    except ValidationError as exc:
        return _finish("failed", f"{build_job.RUNG_SCAFFOLD_FAILED}:{exc.code}", log=exc.message)
    preview, stamped = await _stamped_preview(files, manifest, bundle, site_config)
    try:
        artifact = preview_origin.pack_files(bundle)
        if not store.write_dist(pocket_id, bundle_key(content_hash), artifact):
            raise RuntimeError("the artifact store refused the bundle")
        if preview_origin.store_supports_preview(store):
            preview_origin.publish_draft(
                store, pocket_id, content_hash, preview_origin.pack_files(preview)
            )
    except Exception:
        logger.exception("sites.project: pocket %s no-build draft could not be stored", pocket_id)
        return _finish("failed", f"{build_job.RUNG_PREVIEW_UNREADABLE}:bundle_unreadable")
    count = manifest["sizes"]["assetFiles"]
    log = f"No build step: {count} file(s) served as they are."
    if shipped:
        log += " paw.js added to the pages."
    return _finish(
        STATUS_BUILT,
        None,
        preview_mode="full",
        framework="static",
        log=log,
        paw_client=shipped,
        source_tags=stamped,
    )


async def _stamped_preview(
    files: Mapping[str, bytes],
    manifest: Mapping[str, Any],
    bundle: Mapping[str, bytes],
    site_config: Mapping[str, Any] | None,
) -> tuple[dict[str, bytes], bool]:
    """``(preview files, stamped)`` of a no-build draft. The served .html SOURCE pages
    go through ``stamp-html`` before paw.js is applied, so each ``data-paw-src`` names
    a line of the file the author edits; anything else, or a failed stamp, is the
    plain preview."""
    from pocketpaw_ee.sites import visual_edit

    plain = preview_files(manifest, bundle)
    if draft_tag_mode(files) != "stamp-html":
        return plain, False
    pages = {
        rel: data
        for rel, data in files.items()
        if rel.lower().endswith(".html") and not excluded_from_static_output(rel)
    }
    stamped = await visual_edit.stamp_html(pages)
    if not stamped:
        return plain, False
    try:
        _m, stamped_bundle, _ = no_build_bundle({**files, **stamped}, site_config=site_config)
    except ValidationError:
        logger.warning("sites.project: the stamped preview could not be staged; serving it plain")
        return plain, False
    return preview_files(manifest, stamped_bundle), True


def apply_paw_client_to_bundle(
    root: Path, manifest: Mapping[str, Any], source: Mapping[str, Any] | None, site_config: Any
) -> bool:
    """Publish-time paw.js over a materialized bundle under ``root``: tag the .html
    pages of its assets dir and write ``_paw/paw.js`` there when the live site config
    qualifies and the source uses the client. Idempotent over a bundle the generator
    already tagged. Returns whether the client shipped."""
    from pocketpaw_ee.sites import paw_client

    cfg = paw_client.client_config(site_config)
    texts = paw_client.source_texts(source) if isinstance(source, Mapping) else []
    rel = safe_rel(manifest.get("assetsDir"))
    if cfg is None or rel is None or rel == "." or not paw_client.uses_client(texts):
        return False
    assets_dir = root.joinpath(*rel.split("/"))
    if not assets_dir.is_dir():
        return False
    pages = {
        p.relative_to(assets_dir).as_posix(): p.read_bytes()
        for p in assets_dir.rglob("*")
        if p.is_file() and not p.is_symlink() and p.suffix.lower() in (".html", ".htm")
    }
    before = dict(pages)
    if not paw_client.apply_to_assets(pages, cfg, texts):
        return False
    for name, data in pages.items():
        if before.get(name) != data:
            target = assets_dir.joinpath(*name.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    return True


def apply_capture_tokens_to_bundle(
    root: Path, manifest: Mapping[str, Any], site_config: Any
) -> int:
    """Publish-time lead-form placeholders over a materialized bundle under ``root``.

    A contact form on a project site's static pages is written to the same native
    capture contract the html lane uses (``__CAPTURE_API_BASE__``, ``__SITE_ID__``,
    ``__CAPTURE_SIGNED_KEY__``; ``generator_client._resolve_capture_tokens``), and
    the author never has the real values. Replace them in the .html pages of the
    assets dir with the live site config. Returns how many pages changed; a site with
    no capture config (no signed key yet) is left alone. Idempotent: a resolved page
    has no placeholders left."""
    from pocketpaw_ee.sites import paw_client

    cfg = paw_client.client_config(site_config)
    rel = safe_rel(manifest.get("assetsDir"))
    if cfg is None or rel is None or rel == ".":
        return 0
    assets_dir = root.joinpath(*rel.split("/"))
    if not assets_dir.is_dir():
        return 0
    values = {
        "__CAPTURE_API_BASE__": cfg["api"],
        "__SITE_ID__": cfg["siteId"],
        "__CAPTURE_SIGNED_KEY__": cfg["siteKey"],
    }
    changed = 0
    for page in assets_dir.rglob("*"):
        if not page.is_file() or page.is_symlink() or page.suffix.lower() not in (".html", ".htm"):
            continue
        try:
            text = page.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue
        resolved = text
        for token, value in values.items():
            resolved = resolved.replace(token, value)
        if resolved != text:
            page.write_bytes(resolved.encode("utf-8"))
            changed += 1
    return changed


# ---------------------------------------------------------------------------
# The in-sandbox step
# ---------------------------------------------------------------------------


def generator_dir() -> Path | None:
    """The vendored generator (``dist/cli.js`` + ``package.json``), or ``None``."""
    raw = (os.environ.get(GEN_DIR_ENV) or "").strip()
    root = Path(raw) if raw else Path(_DEFAULT_GEN_DIR)
    if (root / "dist" / "cli.js").is_file() and (root / "package.json").is_file():
        return root
    return None


def generator_uploads() -> list[tuple[bytes, str]] | None:
    """The generator's runtime files as ``(bytes, sandbox path)`` pairs, or ``None``
    when it is not on this host. Its dist has no npm runtime deps (vendor script).
    The prebuilt paw.js (``templates/_paw/paw.js``) rides along when the vendored
    tree has it: ``project-build --site-config`` reads it from there."""
    from pocketpaw_ee.sites.paw_client import CLIENT_TEMPLATE_REL

    root = generator_dir()
    if root is None:
        return None
    uploads = [((root / "package.json").read_bytes(), f"{SANDBOX_GEN_DIR}/package.json")]
    for path in sorted((root / "dist").rglob("*")):
        if path.is_file() and not path.is_symlink() and path.suffix != ".map":
            rel = path.relative_to(root).as_posix()
            uploads.append((path.read_bytes(), f"{SANDBOX_GEN_DIR}/{rel}"))
    client = root / CLIENT_TEMPLATE_REL
    if client.is_file() and not client.is_symlink():
        uploads.append((client.read_bytes(), f"{SANDBOX_GEN_DIR}/{CLIENT_TEMPLATE_REL}"))
    return uploads


def supports_site_config(uploads: list[tuple[bytes, str]]) -> bool:
    """True when the generator in ``uploads`` takes ``project-build --site-config``
    (paw-sites#79) and ships the paw.js it reads. An older vendored generator would
    refuse the unknown flag, so its builds run exactly as before."""
    by_path = {path: data for data, path in uploads}
    from pocketpaw_ee.sites.paw_client import CLIENT_TEMPLATE_REL

    cli = by_path.get(f"{SANDBOX_GEN_DIR}/dist/cli.js") or b""
    return b"--site-config" in cli and f"{SANDBOX_GEN_DIR}/{CLIENT_TEMPLATE_REL}" in by_path


def supports_source_tags(uploads: list[tuple[bytes, str]]) -> bool:
    """True when the generator in ``uploads`` takes ``project-build --source-tags``
    (paw-sites#80). An older one would refuse the flag, so it never gets it."""
    from pocketpaw_ee.sites.visual_edit import cli_has_source_tags

    by_path = {path: data for data, path in uploads}
    return cli_has_source_tags(by_path.get(f"{SANDBOX_GEN_DIR}/dist/cli.js") or b"")


#: Where a draft's source-tagged second build is staged, inside the stage dir. Split
#: out of the artifact on this side (:func:`split_tagged_artifact`); never deployed.
TAGGED_STAGE = "_paw_tagged"


def build_script(
    project_dir: str, *, site_config_path: str | None = None, source_tags: bool = False
) -> str:
    """The bash the wrapper's build step runs (under its timeout, logging to its log):
    ``project-build`` (with ``--site-config`` when given), then the stage step. Exits
    with the first failure's code.

    ``source_tags`` (a draft on a generator that has the flag) adds a SECOND build with
    ``--source-tags``, staged under :data:`TAGGED_STAGE`: the preview shows the tagged
    pages while the untagged bundle stays the one publish deploys. A tagged build that
    fails only costs the tags (the draft previews untagged)."""
    project = shlex.quote(project_dir)
    out_dir = f"{project_dir.rstrip('/')}/{PROJECT_STAGE_REL}"
    out = shlex.quote(out_dir)
    cli = shlex.quote(f"{SANDBOX_GEN_DIR}/dist/cli.js")
    manifest = shlex.quote(f"{project_dir.rstrip('/')}/{PAW_BUILD_FILENAME}")
    stage = shlex.quote(SANDBOX_STAGE_SCRIPT)
    extra = f" --site-config {shlex.quote(site_config_path)}" if site_config_path else ""
    tagged = ""
    if source_tags:
        tagged_out = shlex.quote(f"{out_dir}/{TAGGED_STAGE}")
        tagged = (
            f'if "$RUN" {cli} project-build --dir {project} --out {manifest}{extra}'
            " --source-tags --json; then\n"
            f"  python3 {stage} {project} {tagged_out} || {{ rm -rf {tagged_out};"
            ' echo "paw: the source-tagged preview could not be staged"; }\n'
            "else\n"
            '  echo "paw: the source-tagged build failed; the preview is untagged"\n'
            "fi\n"
        )
    return f"""#!/usr/bin/env bash
# Generated by pocketpaw_ee.sites.project_build — do not edit in place.
set -u
RUN=node
command -v node >/dev/null 2>&1 || RUN=bun
"$RUN" {cli} project-build --dir {project} --out {manifest}{extra} --json
CODE=$?
if [ "$CODE" -ne 0 ]; then
  echo "paw: project-build exited $CODE"
  exit "$CODE"
fi
python3 {stage} {project} {out} || exit $?
{tagged}"""


#: Copies paw-build.json and the files it names into the stage dir. Refuses a path that
#: leaves the project and an assets dir at the project root (that would ship the source).
STAGE_SCRIPT = r"""
import json, os, shutil, sys

root, out = sys.argv[1], sys.argv[2]
manifest_path = os.path.join(root, "paw-build.json")
try:
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
except (OSError, ValueError) as exc:
    sys.exit("paw: project-build wrote no readable paw-build.json (%s)" % exc)


def rel(value):
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        sys.exit("paw: paw-build.json names an invalid path %r" % (value,))
    parts = [p for p in value.split("/") if p not in ("", ".")]
    if ".." in parts:
        sys.exit("paw: paw-build.json names a path outside the project %r" % (value,))
    return "/".join(parts)


def copy(path):
    src, dst = os.path.join(root, path), os.path.join(out, path)
    if os.path.islink(src):
        sys.exit("paw: %s is a symlink" % path)
    if os.path.isdir(src):
        shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns("node_modules"))
    elif os.path.isfile(src):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    else:
        sys.exit("paw: %s named in paw-build.json was not produced" % path)


assets = rel(manifest.get("assetsDir"))
if not assets:
    sys.exit("paw: assetsDir is the project root; build static output into a subdirectory")
if os.path.isdir(out):
    shutil.rmtree(out)
os.makedirs(out)
copy(assets)
for module in manifest.get("workerModules") or []:
    copy(rel(module))
shutil.copy2(manifest_path, os.path.join(out, "paw-build.json"))
print("paw: staged paw-build.json, %s and %d worker module(s)"
      % (assets, len(manifest.get("workerModules") or [])))
"""


# ---------------------------------------------------------------------------
# The built bundle
# ---------------------------------------------------------------------------


def bundle_key(content_hash: str) -> str:
    """The artifact-store key the whole bundle is kept under, beside the preview files.
    A dotted suffix so the filesystem store evicts it with its content hash."""
    return f"{content_hash}.bundle"


def split_tagged_artifact(artifact: bytes) -> tuple[bytes, bytes | None]:
    """``(publish bundle, tagged bundle or None)`` of a staged build tar. Without a
    :data:`TAGGED_STAGE` dir the artifact is returned as it is; with one, its files
    become the tagged bundle and everything else is repacked as the publish bundle,
    so a tag can never reach a deploy."""
    from pocketpaw_ee.sites import preview_origin

    files = preview_origin.unpack_files(artifact)
    prefix = f"{TAGGED_STAGE}/"
    if not any(name.startswith(prefix) for name in files):
        return artifact, None
    plain = {n: d for n, d in files.items() if not n.startswith(prefix)}
    tagged = {n[len(prefix) :]: d for n, d in files.items() if n.startswith(prefix)}
    return preview_origin.pack_files(plain), preview_origin.pack_files(tagged)


#: ``preview_mode`` of a draft whose worker renders every page: its assets have no
#: root index.html, so the static preview origin has nothing to open.
PREVIEW_SERVER_ONLY = "server_only"
#: ``preview_mode`` of the pocket's PUBLISHED content once its drafts were purged:
#: the builder shows the live site (``preview_url`` is its URL, or null).
PREVIEW_PUBLISHED = "published"


def preview_mode(manifest: Mapping[str, Any], preview: Mapping[str, bytes] | None = None) -> str:
    """``"static"`` when the draft has server routes the preview cannot run yet (a
    worker), ``"full"`` when the assets are the whole site, ``"server_only"`` when
    ``preview`` (the files :func:`preview_files` packs) has no root index.html."""
    from pocketpaw_ee.sites.preview_origin import ENTRY

    if preview is not None and ENTRY not in preview:
        return PREVIEW_SERVER_ONLY
    return "static" if manifest_has_worker(dict(manifest)) else "full"


def read_bundle(artifact: bytes) -> tuple[dict[str, Any], dict[str, bytes]]:
    """``(manifest, files)`` of a staged bundle tar. Raises ``ValueError`` when it is
    unreadable, over the draft ceilings, or carries no valid manifest."""
    from pocketpaw_ee.sites import preview_origin

    files = preview_origin.unpack_files(artifact)
    raw = files.get(PAW_BUILD_FILENAME)
    if raw is None:
        raise ValueError("the bundle has no paw-build.json")
    manifest = json.loads(raw.decode("utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("paw-build.json is not an object")
    if safe_rel(manifest.get("assetsDir")) in (None, "."):
        raise ValueError("paw-build.json has no valid assetsDir")
    return manifest, files


def preview_files(manifest: Mapping[str, Any], files: Mapping[str, bytes]) -> dict[str, bytes]:
    """The files the preview origin serves: the assets dir only, rooted at itself, with
    deploy configuration and any ``_worker.js`` left out."""
    prefix = f"{safe_rel(manifest.get('assetsDir'))}/"
    out: dict[str, bytes] = {}
    for name, data in files.items():
        if not name.startswith(prefix):
            continue
        rel = name[len(prefix) :]
        segments = rel.split("/")
        if "_worker.js" in segments or segments[-1] in _PREVIEW_SKIP:
            continue
        out[rel] = data
    return out


def materialize_bundle(artifact: bytes, dest: Path) -> dict[str, Any]:
    """Write a stored bundle under ``dest`` (manifest at its root, as
    ``bundle_deploy`` reads it) and return the manifest. Unpacked through the guarded
    ``preview_origin.unpack_files``: no links, no escapes, size-capped."""
    manifest, files = read_bundle(artifact)
    for name, data in files.items():
        target = dest.joinpath(*name.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return manifest


# ---------------------------------------------------------------------------
# Build records + log
# ---------------------------------------------------------------------------


def redact_build_log(text: str | None, *, cap: int = LOG_CAP_BYTES) -> tuple[str, bool]:
    """The build output made safe to store and show: secrets redacted, sandbox paths
    made project-relative, then capped to the LAST ``cap`` bytes (the end of a failed
    build is where the error is). Returns ``(log, truncated)``."""
    from pocketpaw_ee.sites.verify_diagnostics import scrub_log_text

    clean = scrub_log_text(text or "")
    data = clean.encode("utf-8")
    if len(data) <= cap:
        return clean, False
    tail = data[-cap:].decode("utf-8", "ignore")
    return tail, True


#: ``project-build``'s failure codes (exit 1, ``{error, code, log}`` on stdout). A
#: closed set paw-sites owns, so it is safe as the second half of a rung.
CLI_FAILURE_CODES = frozenset(
    {
        "not_a_project",
        "unknown_framework",
        "invalid_template",
        "install_failed",
        "build_failed",
        "output_missing",
        "wrangler_failed",
        "size_limit",
        "do_config",
        "internal_error",
    }
)


def cli_failure_code(output: str | None) -> str | None:
    """The ``code`` of the last ``project-build`` failure line in ``output``, when it is
    one of :data:`CLI_FAILURE_CODES`; else ``None``."""
    for line in reversed((output or "").splitlines()):
        line = line.strip()
        if not (line.startswith("{") and '"code"' in line):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        code = payload.get("code") if isinstance(payload, dict) else None
        return code if code in CLI_FAILURE_CODES else None
    return None


def build_record_key(job_id: str) -> str:
    return f"{BUILD_KEY_PREFIX}{job_id}"


def job_belongs_to(job_id: str, pocket_id: str) -> bool:
    """True when ``job_id`` is one of this pocket's preview-lane build ids."""
    return job_id.startswith(f"site-preview-{pocket_id}-")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def write_build_record(store: Any, pocket_id: str, record: dict[str, Any]) -> None:
    """Persist a build record and point ``latest-build`` at it. Best effort."""
    from pocketpaw_ee.sites import verify_store

    job_id = str(record.get("job_id") or "")
    if not job_id:
        return
    try:
        store.write(pocket_id, build_record_key(job_id), record)
        pointer = {k: record.get(k) for k in _POINTER_FIELDS}
        store.write(pocket_id, verify_store.LATEST_BUILD_KEY, pointer)
    except Exception:  # noqa: BLE001 — a lost record costs a log, never a build
        logger.warning("sites.project: could not record build %s", job_id, exc_info=True)


_POINTER_FIELDS = (
    "job_id",
    "content_hash",
    "status",
    "reason",
    "preview_mode",
    "framework",
    "updated_at",
)


def read_build_record(store: Any, pocket_id: str, job_id: str) -> dict[str, Any] | None:
    try:
        return store.read(pocket_id, build_record_key(job_id))
    except Exception:  # noqa: BLE001
        return None


def read_latest_build(store: Any, pocket_id: str) -> dict[str, Any] | None:
    from pocketpaw_ee.sites import verify_store

    try:
        return store.read(pocket_id, verify_store.LATEST_BUILD_KEY)
    except Exception:  # noqa: BLE001
        return None


def new_record(job_id: str, content_hash: str, status: str, **extra: Any) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "content_hash": content_hash,
        "status": status,
        "reason": None,
        "log": "",
        "log_truncated": False,
        "preview_mode": None,
        "framework": None,
        "updated_at": _now(),
        **extra,
    }


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------


async def run_project_preview_build(
    ctx: dict[str, Any],
    pocket_id: str,
    content_hash: str,
    generator_input: dict[str, Any],
    timeout_seconds: int,
    *,
    _runner: Any = None,
    _client: Any = None,
    _store: Any = None,
    _verify_store: Any = None,
    _gen_uploads: list[tuple[bytes, str]] | None = None,
    _draft: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """arq job body for a project draft (dispatched by ``build_job.run_site_preview_build``).

    Builds in a sandbox, stores the bundle and the preview files, records the build.
    Returns ``{status, reason, preview_mode, job_id}``; ``reason`` is a rung, never
    stderr. Never raises for a build outcome; re-raises only when no sandbox could be
    created (the same contract as the other preview jobs)."""
    from pocketpaw_ee.sites import build_job, preview_origin
    from pocketpaw_ee.sites import service as sites_service
    from pocketpaw_ee.sites import verify_store as verify_store_mod
    from pocketpaw_ee.sites.daytona_runner import SANDBOX_PROJECT_DIR, run_build

    job_id = build_job._preview_job_id(pocket_id, content_hash)
    records = (
        _verify_store if _verify_store is not None else verify_store_mod.default_verify_store()
    )
    store = _store if _store is not None else sites_service._default_artifact_store()

    def _finish(status: str, reason: str | None, **extra: Any) -> dict[str, Any]:
        write_build_record(
            records, pocket_id, new_record(job_id, content_hash, status, reason=reason, **extra)
        )
        return {
            "status": status,
            "reason": reason,
            "job_id": job_id,
            "preview_mode": extra.get("preview_mode"),
        }

    try:
        files = project_files(generator_input.get("source"))
    except ValidationError as exc:
        return _finish("failed", f"{build_job.RUNG_SCAFFOLD_FAILED}:{exc.code}", log=exc.message)

    site_config = generator_input.get("siteConfig")
    site_config = site_config if isinstance(site_config, dict) else None
    if files_are_no_build(files):
        # Its own output: staged in-process, no generator and no sandbox needed.
        return await build_no_build_draft(
            pocket_id,
            content_hash,
            generator_input.get("source"),
            store=store,
            records=records,
            site_config=site_config,
        )

    gen = _gen_uploads if _gen_uploads is not None else generator_uploads()
    if gen is None:
        logger.error("sites.project: no vendored generator on this host (%s)", GEN_DIR_ENV)
        return _finish("failed", f"{build_job.RUNG_SANDBOX_UNAVAILABLE}:generator_missing")

    write_build_record(records, pocket_id, new_record(job_id, content_hash, "building"))
    # paw.js for the built pages rides --site-config, only on a generator that has it
    # and a config that still carries the site key. The queued path never does today:
    # ``build_job.enqueue_preview_build`` scrubs ``captureSignedKey`` before Redis and
    # the sandbox (build_job's header), so a sandbox-built site gets paw.js at publish
    # instead (:func:`apply_paw_client_to_bundle`, the "inject at deploy" rule).
    from pocketpaw_ee.sites.paw_client import client_config

    wants_client = client_config(site_config) is not None
    config_path = SANDBOX_SITE_CONFIG if wants_client and supports_site_config(gen) else None
    # A draft builds twice when the generator can tag sources: untagged for the bundle
    # publish deploys, tagged for the preview (see build_script).
    source_tags = draft_tag_mode(files) == "source-tags" and supports_source_tags(gen)
    script = build_script(
        SANDBOX_PROJECT_DIR, site_config_path=config_path, source_tags=source_tags
    )
    uploads = [
        *gen,
        (script.encode(), SANDBOX_BUILD_SCRIPT),
        (STAGE_SCRIPT.encode(), SANDBOX_STAGE_SCRIPT),
    ]
    if config_path:
        uploads.append((json.dumps(site_config).encode("utf-8"), config_path))
    runner = _runner or run_build
    try:
        result = await runner(
            files,
            engine=ENGINE,
            timeout_seconds=timeout_seconds,
            client=_client,
            install_command="true",
            build_command=f"bash {SANDBOX_BUILD_SCRIPT}",
            artifact_rel=PROJECT_STAGE_REL,
            extra_uploads=uploads,
            ssr_markers=(),
            log_tail_bytes=LOG_CAP_BYTES,
        )
    except Exception as exc:
        if capacity.is_capacity_error(exc):
            return await _capacity_outcome(ctx, pocket_id, job_id, _finish, records, content_hash)
        logger.exception("sites.project: no sandbox for pocket %s", pocket_id)
        _finish("failed", f"{build_job.RUNG_SANDBOX_UNAVAILABLE}:{capacity.NO_SANDBOX_CAUSE}")
        raise

    settlement = build_job.resolve_build_settlement(result)
    log, truncated = redact_build_log(result.classification.stderr_tail)
    logged = {"log": log, "log_truncated": truncated}
    if settlement.status != "built":
        reason = settlement.reason
        code = cli_failure_code(result.classification.stderr_tail)
        if result.classification.outcome == "build_failed" and code is not None:
            reason = f"build_failed:{code}"
        return _finish(settlement.status or "failed", reason, **logged)

    tagged: bytes | None = None
    try:
        artifact, tagged = split_tagged_artifact(result.artifact or b"")
        manifest, bundle_files = read_bundle(artifact)
        if not store.write_dist(pocket_id, bundle_key(content_hash), artifact):
            raise RuntimeError("the artifact store refused the bundle")
        view_manifest, view_files = manifest, bundle_files
        if tagged is not None:
            try:
                view_manifest, view_files = read_bundle(tagged)
            except ValueError:
                logger.warning("sites.project: pocket %s tagged build unreadable", pocket_id)
                tagged = None
                view_manifest, view_files = manifest, bundle_files
        preview = preview_files(view_manifest, view_files)
        mode = preview_mode(view_manifest, preview)
        # A server-only draft gets no preview files or token: its URL would 404.
        if mode != PREVIEW_SERVER_ONLY and preview_origin.store_supports_preview(store):
            preview_origin.publish_draft(
                store, pocket_id, content_hash, preview_origin.pack_files(preview)
            )
    except Exception:
        logger.exception("sites.project: pocket %s built but the bundle was unusable", pocket_id)
        return _finish("failed", f"{build_job.RUNG_PREVIEW_UNREADABLE}:bundle_unreadable", **logged)

    draft_reason: str | None = None
    draft_extra: dict[str, Any] = {}
    if manifest_has_worker(dict(view_manifest)):
        mode, draft_reason, missing = await _draft_worker_mode(
            pocket_id,
            content_hash,
            tagged or artifact,
            view_manifest,
            generator_input,
            store,
            mode,
            _draft or {},
        )
        if missing:
            draft_extra["draft_secrets_missing"] = list(missing)

    framework = manifest.get("framework") if isinstance(manifest.get("framework"), str) else None
    return _finish(
        STATUS_BUILT,
        settlement.reason,
        preview_mode=mode,
        framework=framework,
        draft_worker_reason=draft_reason,
        source_tags=tagged is not None,
        **draft_extra,
        **logged,
    )


async def _draft_worker_mode(
    pocket_id: str,
    content_hash: str,
    artifact: bytes,
    manifest: Mapping[str, Any],
    generator_input: Mapping[str, Any],
    store: Any,
    mode: str,
    deps: dict[str, Any],
) -> tuple[str, str | None, tuple[str, ...]]:
    """``(preview_mode, draft_worker_reason, missing secret names)`` once the draft
    Worker had its try (``draft_worker``, behind ``PAW_SITES_DRAFT_WORKERS``):
    ``"full"`` when it serves, else the static mode already computed plus the rung.
    Never raises."""
    from pocketpaw_ee.sites import draft_worker

    try:
        outcome = await draft_worker.deploy_for_build(
            pocket_id=pocket_id,
            content_hash=content_hash,
            artifact=artifact,
            manifest=manifest,
            source=generator_input.get("source"),
            store=store,
            **deps,
        )
    except Exception:  # noqa: BLE001 - a draft Worker never fails the build
        logger.exception("sites.project: draft worker for pocket %s failed", pocket_id)
        return mode, "draft_worker:deploy_failed", ()
    return outcome.mode or mode, outcome.reason, outcome.missing_secrets


async def _capacity_outcome(
    ctx: dict[str, Any],
    pocket_id: str,
    job_id: str,
    finish: Any,
    records: Any,
    content_hash: str,
) -> dict[str, Any]:
    """The org's Daytona limit refused the sandbox. Superseded → stop and write
    nothing (the newer job owns ``latest-build``). Tries left → record ``queued`` /
    ``waiting_for_capacity`` and let ``Retry`` re-run the job. Budget spent → settle
    ``failed`` / ``sandbox_unavailable:capacity`` without raising."""
    from arq.worker import Retry

    from pocketpaw_ee.sites import build_job

    if await build_job._preview_superseded(ctx, pocket_id):
        logger.info("sites.project: pocket %s job %s superseded — not retrying", pocket_id, job_id)
        return {
            "status": "failed",
            "reason": build_job.SUPERSEDED_REASON,
            "job_id": job_id,
            "preview_mode": None,
        }
    delay = capacity.next_retry_delay(ctx)
    if delay is not None:
        logger.warning(
            "sites.project: Daytona capacity full for pocket %s; retrying in %.0fs (try %s)",
            pocket_id,
            delay,
            ctx.get("job_try"),
        )
        write_build_record(
            records,
            pocket_id,
            new_record(job_id, content_hash, "queued", reason=capacity.WAITING_REASON),
        )
        raise Retry(defer=delay)
    logger.warning("sites.project: Daytona capacity still full for pocket %s; giving up", pocket_id)
    return finish("failed", f"{build_job.RUNG_SANDBOX_UNAVAILABLE}:{capacity.CAPACITY_CAUSE}")


# ---------------------------------------------------------------------------
# Publish gates
# ---------------------------------------------------------------------------


def check_plan_allows(manifest: Mapping[str, Any], *, paid: bool, has_custom_domain: bool) -> None:
    """Refuse a project publish the site's plan does not cover (captain, 2026-10-07).

    Free covers static sites and a worker script that binds nothing beyond D1 / KV
    (plus its own assets). Anything else a worker asks for, and server code on a
    custom domain, needs the Site plan. A static build is never refused here."""
    if paid or not manifest_has_worker(dict(manifest)):
        return
    # With Durable Objects on, a free site may bind one class (``durable_objects``
    # enforces the count); with them off ``do`` stays a Site-plan binding.
    free_bindings = FREE_WORKER_BINDINGS | ({"do"} if durable_objects.enabled() else set())
    requests = manifest.get("bindingRequests")
    extra = sorted(
        {
            f"{str(req.get('type', '')).lower()} {req.get('name')!s}"
            for req in (requests if isinstance(requests, list) else [])
            if isinstance(req, dict)
            and str(req.get("type", "")).strip().lower() not in free_bindings
        }
    )
    if extra:
        raise ValidationError(
            "sites.server_code_not_entitled",
            "On the free plan a site's server code can use D1 and KV only. This build "
            f"also asks for {', '.join(extra)}. Upgrade this site to the Site plan, or "
            "remove those bindings from the project.",
        )
    if has_custom_domain:
        raise ValidationError(
            "sites.server_code_not_entitled",
            "Server code on a custom domain needs the Site plan. Upgrade this site, or "
            "publish it as a static build.",
        )


__all__ = [
    "BUILD_FORMAT",
    "CLI_FAILURE_CODES",
    "ENGINE",
    "FREE_WORKER_BINDINGS",
    "LOG_CAP_BYTES",
    "NO_BUILD",
    "PREVIEW_PUBLISHED",
    "PREVIEW_SERVER_ONLY",
    "STAGE_SCRIPT",
    "STATIC_OUT_DIR",
    "STATIC_ROOT_EXCLUDES",
    "TAGGED_STAGE",
    "TEMPLATE_MANIFEST",
    "apply_capture_tokens_to_bundle",
    "apply_paw_client_to_bundle",
    "build_no_build_draft",
    "build_record_key",
    "build_script",
    "bundle_key",
    "check_plan_allows",
    "cli_failure_code",
    "draft_tag_mode",
    "excluded_from_static_output",
    "files_are_no_build",
    "generator_uploads",
    "is_no_build",
    "job_belongs_to",
    "materialize_bundle",
    "no_build_bundle",
    "preview_files",
    "preview_mode",
    "project_content_hash",
    "project_files",
    "read_build_record",
    "read_bundle",
    "read_latest_build",
    "redact_build_log",
    "run_project_preview_build",
    "split_tagged_artifact",
    "supports_site_config",
    "supports_source_tags",
    "write_build_record",
]
