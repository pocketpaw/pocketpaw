# ee/pocketpaw_ee/sites/project_build.py — the ``project`` engine's draft build and
# the reads/gates its publish needs.
#
# A project pocket's source map is a whole repo the author owns. Its draft builds only
# in a Daytona sandbox: the source map is uploaded as the project tree, the vendored
# paw-sites generator is uploaded beside it, and ``paw-sites-gen project-build`` runs
# install + build + (worker targets) wrangler dry-run and writes ``paw-build.json``. A
# small stage step then copies the manifest and the files it names into
# ``engines.PROJECT_STAGE_REL``, which the existing wrapper tars like any other output.
#
# What a finished build leaves, all keyed by ``(pocket_id, content_hash)``:
#   * the whole bundle (manifest + assets + worker modules) in the artifact store under
#     ``<hash>.bundle`` — what publish deploys through ``bundle_deploy``;
#   * the assets alone as the draft's preview files + token (``preview_origin``),
#     unless they have no root index.html (see ``preview_mode`` below);
#   * a build record in ``verify_store`` (``build-<job id>``): status, rung, the
#     redacted and capped build log, ``preview_mode`` and the framework; plus the
#     pocket's ``latest-build`` pointer.
#
# Invariants: nothing here runs author code on this host; the build log is scrubbed
# (``verify_diagnostics.scrub_log_text``) before it is stored; ``preview_mode`` is
# ``"full"`` when the assets are the whole site or a draft Worker serves the build
# (``draft_worker``, behind ``PAW_SITES_DRAFT_WORKERS``; its fallback rung is the
# record's ``draft_worker_reason``), else ``"static"`` for a worker build, and
# ``"server_only"`` when a worker build's assets have no root index.html (next,
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
from pocketpaw_ee.sites import capacity
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

BUILD_KEY_PREFIX = "build-"

#: Statuses a build record carries. ``queued`` / ``building`` / ``failed`` are the
#: publish lane's wire vocabulary; ``built`` is a finished, stored draft.
STATUS_BUILT = "built"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def project_content_hash(source: Mapping[str, Any]) -> str:
    """The draft's content hash: the source map, the engine and this lane's format.
    The generator version rides it too, so a new ``project-build`` rebuilds drafts."""
    from pocketpaw_ee.sites.generator_client import generator_version

    h = hashlib.sha256()
    for part in (
        BUILD_FORMAT,
        generator_version(),
        ENGINE,
        json.dumps(dict(source), sort_keys=True, separators=(",", ":"), ensure_ascii=False),
    ):
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
    if "package.json" not in files:
        raise ValidationError(
            "sites.project_no_package_json",
            "A project site needs a package.json at its root.",
        )
    if len(files) > MAX_SOURCE_FILES or total > MAX_SOURCE_BYTES:
        raise ValidationError(
            "sites.project_too_large",
            f"This project is over the build limits ({MAX_SOURCE_FILES} files, "
            f"{MAX_SOURCE_BYTES // (1024 * 1024)} MiB of source).",
        )
    return files


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
    when it is not on this host. Its dist has no npm runtime deps (vendor script)."""
    root = generator_dir()
    if root is None:
        return None
    uploads = [((root / "package.json").read_bytes(), f"{SANDBOX_GEN_DIR}/package.json")]
    for path in sorted((root / "dist").rglob("*")):
        if path.is_file() and not path.is_symlink() and path.suffix != ".map":
            rel = path.relative_to(root).as_posix()
            uploads.append((path.read_bytes(), f"{SANDBOX_GEN_DIR}/{rel}"))
    return uploads


def build_script(project_dir: str) -> str:
    """The bash the wrapper's build step runs (under its timeout, logging to its log):
    ``project-build``, then the stage step. Exits with the first failure's code."""
    project = shlex.quote(project_dir)
    out = shlex.quote(f"{project_dir.rstrip('/')}/{PROJECT_STAGE_REL}")
    cli = shlex.quote(f"{SANDBOX_GEN_DIR}/dist/cli.js")
    manifest = shlex.quote(f"{project_dir.rstrip('/')}/{PAW_BUILD_FILENAME}")
    return f"""#!/usr/bin/env bash
# Generated by pocketpaw_ee.sites.project_build — do not edit in place.
set -u
RUN=node
command -v node >/dev/null 2>&1 || RUN=bun
"$RUN" {cli} project-build --dir {project} --out {manifest} --json
CODE=$?
if [ "$CODE" -ne 0 ]; then
  echo "paw: project-build exited $CODE"
  exit "$CODE"
fi
python3 {shlex.quote(SANDBOX_STAGE_SCRIPT)} {project} {out}
"""


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

    gen = _gen_uploads if _gen_uploads is not None else generator_uploads()
    if gen is None:
        logger.error("sites.project: no vendored generator on this host (%s)", GEN_DIR_ENV)
        return _finish("failed", f"{build_job.RUNG_SANDBOX_UNAVAILABLE}:generator_missing")

    write_build_record(records, pocket_id, new_record(job_id, content_hash, "building"))
    uploads = [
        *gen,
        (build_script(SANDBOX_PROJECT_DIR).encode(), SANDBOX_BUILD_SCRIPT),
        (STAGE_SCRIPT.encode(), SANDBOX_STAGE_SCRIPT),
    ]
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

    try:
        manifest, bundle_files = read_bundle(result.artifact or b"")
        if not store.write_dist(pocket_id, bundle_key(content_hash), result.artifact):
            raise RuntimeError("the artifact store refused the bundle")
        preview = preview_files(manifest, bundle_files)
        mode = preview_mode(manifest, preview)
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
    if manifest_has_worker(dict(manifest)):
        mode, draft_reason, missing = await _draft_worker_mode(
            pocket_id,
            content_hash,
            result.artifact or b"",
            manifest,
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
    requests = manifest.get("bindingRequests")
    extra = sorted(
        {
            f"{str(req.get('type', '')).lower()} {req.get('name')!s}"
            for req in (requests if isinstance(requests, list) else [])
            if isinstance(req, dict)
            and str(req.get("type", "")).strip().lower() not in FREE_WORKER_BINDINGS
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
    "PREVIEW_PUBLISHED",
    "PREVIEW_SERVER_ONLY",
    "STAGE_SCRIPT",
    "build_record_key",
    "build_script",
    "bundle_key",
    "check_plan_allows",
    "cli_failure_code",
    "generator_uploads",
    "job_belongs_to",
    "materialize_bundle",
    "preview_files",
    "preview_mode",
    "project_content_hash",
    "project_files",
    "read_build_record",
    "read_bundle",
    "read_latest_build",
    "redact_build_log",
    "run_project_preview_build",
    "write_build_record",
]
