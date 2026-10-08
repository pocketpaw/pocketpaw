# ee/pocketpaw_ee/sites/bundle_deploy.py: deploy a ``paw-build.json`` build (the
# ``project`` engine / base app templates) through the Cloudflare HTTP API, either into
# our Workers for Platforms namespace (``dispatch``) or, as an interim for an account
# without WfP, to a regular account-level script (``account``; picked by
# ``project_deploy_target``, risks in docs/deployment/sites-bundle-deploys.md). Only
# the API URLs differ between the two.
#
# This module is the trust boundary for an author-built bundle: it reads ONLY
# ``paw-build.json`` and the files it names, never a wrangler config, and runs nothing.
#   * modules and assets: path-contained, typed by extension, size and count capped;
#     ``_headers`` / ``_redirects`` travel as ``assets.config`` strings.
#   * compat: the author's date (bumped for nodejs_compat, clamped to today) and only
#     allow-listed flags.
#   * bindings: requests map by TYPE and NAME onto resources WE provisioned
#     (``provision`` callback, ``binding_provisioner``); author ids are never read,
#     unsupported kinds are dropped with a warning, an unprovisioned backend refuses.
#   * secrets bind as ``secret_text`` (a missing required one refuses with
#     ``sites.secrets_missing``; values never reach a log or repr); platform env
#     (``plain_text``) wins over a secret of the same name.
#   * Durable Objects (``durable_objects``, behind ``PAW_SITES_DURABLE_OBJECTS``):
#     vetted and migration-planned before ``provision``, bound as
#     ``durable_object_namespace``; the result carries Cloudflare's ``migration_tag``.
#   * worker settings: sampled observability, plan-tiered ``limits`` and opt-in Smart
#     Placement (``worker_settings``); drafts use the ``PAW_SITES_DRAFT_*`` knobs.
# Every refusal happens before the first upload, so the live site is untouched.
# The manifest is paw-sites' ``buildPawManifest`` (an earlier shape is still accepted);
# unknown fields are ignored and everything dropped is a warning.
from __future__ import annotations

import json
import logging
import os
import posixpath
import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites import durable_objects
from pocketpaw_ee.sites.cloudflare_client import (
    ACCOUNT_TARGET,
    DISPATCH_TARGET,
    SCRIPT_TARGETS,
    WorkerModule,
    module_content_type,
)

logger = logging.getLogger(__name__)

PAW_BUILD_FILENAME = "paw-build.json"

# Operator override for where project bundles deploy: ``account`` or ``dispatch``.
# Unset (or unknown) follows PAW_CF_DEPLOY_MODE (``project_deploy_target``).
PROJECT_TARGET_ENV = "PAW_SITES_PROJECT_DEPLOY_TARGET"

# Smart Placement for workers that talk to a regional backend. OFF unless set to a
# truthy value. https://developers.cloudflare.com/workers/configuration/placement/
SMART_PLACEMENT_ENV = "PAW_SITES_SMART_PLACEMENT"
# Workers Logs + traces in the upload metadata (``observability``). On unless set to
# a falsy value; the head sampling rate defaults to 10%.
# https://developers.cloudflare.com/api/resources/workers/subresources/scripts/methods/update/
OBSERVABILITY_ENV = "PAW_SITES_OBSERVABILITY"
OBSERVABILITY_SAMPLE_ENV = "PAW_SITES_OBSERVABILITY_SAMPLE"
DEFAULT_OBSERVABILITY_SAMPLE = 0.1
# Per-request caps in the upload metadata (``limits``), by the site's plan. CPU
# defaults: 50 ms free, 300 ms paid (captain, 2026-10-07). Subrequests default to
# Cloudflare's own Free / Paid account defaults (50 / 10,000); 0 leaves a field out.
# https://developers.cloudflare.com/workers/wrangler/configuration/#limits
CPU_MS_ENV = {False: "PAW_SITES_CPU_MS_FREE", True: "PAW_SITES_CPU_MS_PAID"}
DEFAULT_CPU_MS = {False: 50, True: 300}
SUBREQUESTS_ENV = {False: "PAW_SITES_SUBREQUESTS_FREE", True: "PAW_SITES_SUBREQUESTS_PAID"}
DEFAULT_SUBREQUESTS = {False: 50, True: 10_000}
# Draft Workers (``draft_worker``): the plan's caps unless these override them, and
# observability at its own sample rate (drafts are low traffic and exist to debug).
DRAFT_CPU_MS_ENV = "PAW_SITES_DRAFT_CPU_MS"
DRAFT_SUBREQUESTS_ENV = "PAW_SITES_DRAFT_SUBREQUESTS"
DRAFT_OBSERVABILITY_SAMPLE_ENV = "PAW_SITES_DRAFT_OBSERVABILITY_SAMPLE"
DEFAULT_DRAFT_OBSERVABILITY_SAMPLE = 1.0
# Cloudflare caps cpu_ms at 300,000 and subrequests at 10,000,000 (Paid).
_MAX_CPU_MS = 300_000
_MAX_SUBREQUESTS = 10_000_000
_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}
# Upload binding types whose data lives in one region: a request to them from a far
# edge pays the round trip. KV is edge-cached and assets are served at the edge, so
# neither earns placement on its own.
_REGIONAL_BINDING_TYPES = frozenset({"d1", "r2_bucket"})

# Worker size: 64 MiB uncompressed on Free and Paid, no compressed cap.
# https://developers.cloudflare.com/workers/platform/limits/
MAX_WORKER_BYTES = 64 * 1024 * 1024
# Cloudflare documents no module-count limit (same page). This is our own cap so a
# runaway build (an unbundled node_modules tree) is refused here, not at upload.
MAX_WORKER_MODULES = 1000
# Static assets (Paid): 100,000 files per version, 25 MiB per file. Same page.
MAX_ASSET_FILES = 100_000
MAX_ASSET_FILE_BYTES = 25 * 1024 * 1024

# Flags a tenant may turn on. The minimal set OpenNext needs plus the Node compat
# variants; anything else (experimental, unsafe, or behaviour we have not vetted on
# untrusted WfP) is dropped with a warning.
ALLOWED_COMPAT_FLAGS = frozenset(
    {"nodejs_compat", "nodejs_compat_v2", "nodejs_als", "global_fetch_strictly_public"}
)
_NODE_FLAGS = frozenset({"nodejs_compat", "nodejs_compat_v2"})
# nodejs_compat needs this date or later for the v2 polyfills OpenNext relies on.
# https://opennext.js.org/cloudflare/get-started
NODEJS_COMPAT_MIN_DATE = "2024-09-23"
DEFAULT_COMPAT_DATE = "2026-09-01"

_ASSETS_CONFIG_FILES = ("_headers", "_redirects")
_ASSETS_SKIP = frozenset({"_headers", "_redirects", ".assetsignore"})
_HTML_HANDLING = frozenset(
    {"auto-trailing-slash", "force-trailing-slash", "drop-trailing-slash", "none"}
)
_NOT_FOUND_HANDLING = frozenset({"single-page-application", "404-page", "none"})
_BINDING_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_NEVER_FORWARDED = frozenset(
    {
        "service",
        "services",
        "dispatch_namespace",
        "dispatch_namespaces",
        "tail_consumer",
        "tail_consumers",
        "images",
        "routes",
        "route",
        "unknown",
    }
)
_PROVISIONED_TYPES = frozenset({"d1", "kv", "r2", "do", "ai", "queues"})


@dataclass(frozen=True)
class ProvisionedResources:
    """What our provisioner stood up for THIS site. Empty means not provisioned.

    D1, queues and ai serve one binding request each. KV and R2 are keyed by the
    binding NAME the build requested (``{name: namespace_id}`` / ``{name:
    bucket_name}``), so a site may bind several. The author picks names, we pick
    the resources. ``secrets`` maps names to values for ``secret`` requests (the
    values come from our encrypted store, never from the build, and stay out of
    ``repr`` so a logged object cannot carry them)."""

    d1_database_id: str = ""
    kv_namespaces: dict[str, str] = field(default_factory=dict)
    r2_buckets: dict[str, str] = field(default_factory=dict)
    queue_name: str = ""
    ai: bool = False
    secrets: dict[str, str] = field(default_factory=dict, repr=False)
    # Platform env (``plain_text``), e.g. a draft's ``BETTER_AUTH_URL``. Wins over a
    # secret of the same name.
    plain_text: dict[str, str] = field(default_factory=dict)
    # Vetted Durable Object bindings, ``{name: class_name}`` (``durable_objects``).
    # Nothing to create: Cloudflare makes the namespace when the migration applies.
    durable_objects: dict[str, str] = field(default_factory=dict)


@dataclass
class PawBundle:
    """A vetted bundle, ready for ``CloudflareClient`` (nothing author-controlled
    left in it except module/asset bytes and binding names)."""

    main_module: str | None
    modules: list[WorkerModule]
    assets: dict[str, bytes]
    assets_config: dict[str, Any]
    compatibility_date: str
    compatibility_flags: list[str]
    # Carries secret_text values: kept out of repr.
    bindings: list[dict] = field(repr=False)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BundleDeployResult:
    script_name: str
    modules: int
    assets: int
    warnings: list[str]
    # The script's Durable Object migration tag after the upload (Cloudflare's
    # ``migration_tag``), the applied tag history ending in it, and the classes live
    # on it; None / () without DOs. Callers store the history and classes.
    migration_tag: str | None = None
    do_classes: tuple[str, ...] = ()
    migration_tags: tuple[str, ...] = ()


def _refuse(message: str) -> ValidationError:
    return ValidationError("sites.bundle_invalid", message)


def _secrets_missing(names: Iterable[str]) -> ValidationError:
    listed = ", ".join(sorted(set(names)))
    return ValidationError(
        "sites.secrets_missing",
        f"This site needs secrets that are not set: {listed}. Set them in the builder "
        "(Secrets) and publish again.",
    )


def has_paw_build(build_dir: str | Path) -> bool:
    return Path(build_dir, PAW_BUILD_FILENAME).is_file()


def project_deploy_target(deploy_mode: str | None) -> str:
    """Where a project bundle deploys: ``account`` or ``dispatch``.

    ``PAW_SITES_PROJECT_DEPLOY_TARGET`` wins when it names a target. Otherwise the
    deploy mode decides: ``workers`` (an account without Workers for Platforms) means
    ``account``, anything else ``dispatch``. An unknown override is logged and
    ignored, so a typo falls back to the mode rather than failing the publish."""
    raw = (os.environ.get(PROJECT_TARGET_ENV) or "").strip().lower()
    if raw in SCRIPT_TARGETS:
        return raw
    if raw:
        logger.warning("sites: unknown %s=%r; following the deploy mode", PROJECT_TARGET_ENV, raw)
    return ACCOUNT_TARGET if deploy_mode == "workers" else DISPATCH_TARGET


def _env_flag(name: str) -> str:
    return (os.environ.get(name) or "").strip().lower()


def placement_for(bundle: PawBundle) -> dict | None:
    """``{"mode": "smart"}`` only when ``PAW_SITES_SMART_PLACEMENT`` is truthy and the
    worker has code and binds a regional backend (D1, R2); else None.

    Opt-in because it rarely helps a paw site: placement needs "consistent traffic
    ... from multiple locations" (a quiet site stays ``INSUFFICIENT_INVOCATIONS``),
    it places the whole script as one unit, which Cloudflare says is not optimized
    correctly alongside ``run_worker_first`` (vite-react-hono uses it), and running
    near the D1 primary defeats local read replicas."""
    if _env_flag(SMART_PLACEMENT_ENV) not in _TRUTHY:
        return None
    if not bundle.modules:
        return None
    if not any(b.get("type") in _REGIONAL_BINDING_TYPES for b in bundle.bindings):
        return None
    return {"mode": "smart"}


def _sample_rate(draft: bool = False) -> float:
    env = DRAFT_OBSERVABILITY_SAMPLE_ENV if draft else OBSERVABILITY_SAMPLE_ENV
    default = DEFAULT_DRAFT_OBSERVABILITY_SAMPLE if draft else DEFAULT_OBSERVABILITY_SAMPLE
    raw = _env_flag(env)
    if not raw:
        return default
    try:
        rate = float(raw)
    except ValueError:
        rate = -1.0
    if not 0.0 <= rate <= 1.0:
        logger.warning("sites: %s=%r is not between 0 and 1; using %s", env, raw, default)
        return default
    return rate


def observability_for(bundle: PawBundle, *, draft: bool = False) -> dict | None:
    """Workers Logs (invocation logs) and traces for a worker with code, sampled at
    ``PAW_SITES_OBSERVABILITY_SAMPLE`` (default 0.1). None for an assets-only bundle
    or when ``PAW_SITES_OBSERVABILITY`` is falsy."""
    if not bundle.modules or _env_flag(OBSERVABILITY_ENV) in _FALSY:
        return None
    rate = _sample_rate(draft)
    return {
        "enabled": True,
        "head_sampling_rate": rate,
        "logs": {"enabled": True, "invocation_logs": True},
        "traces": {"enabled": True, "head_sampling_rate": rate},
    }


def _int_env(name: str, default: int, maximum: int) -> int:
    raw = _env_flag(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if not 0 <= value <= maximum:
        logger.warning("sites: %s=%r is not 0..%d; using %d", name, raw, maximum, default)
        return default
    return value


def limits_for(bundle: PawBundle, *, paid: bool, draft: bool = False) -> dict | None:
    """``{"cpu_ms": N, "subrequests": M}`` per request for a worker with code, by the
    site's plan (``paid`` is the provisioner's entitlement answer). A value of 0 in
    the env leaves that field out (Cloudflare's account default applies). None for
    an assets-only bundle: asset requests never run the worker."""
    if not bundle.modules:
        return None
    out: dict[str, int] = {}
    cpu = _int_env(CPU_MS_ENV[paid], DEFAULT_CPU_MS[paid], _MAX_CPU_MS)
    sub = _int_env(SUBREQUESTS_ENV[paid], DEFAULT_SUBREQUESTS[paid], _MAX_SUBREQUESTS)
    if draft:
        cpu = _int_env(DRAFT_CPU_MS_ENV, cpu, _MAX_CPU_MS)
        sub = _int_env(DRAFT_SUBREQUESTS_ENV, sub, _MAX_SUBREQUESTS)
    if cpu:
        out["cpu_ms"] = cpu
    if sub:
        out["subrequests"] = sub
    return out or None


def worker_settings(bundle: PawBundle, *, paid: bool, draft: bool = False) -> dict[str, dict]:
    """The optional upload-metadata blocks for this bundle, keyed by the
    ``put_worker`` keyword they travel as. Empty blocks are left out. A draft never
    gets Smart Placement."""
    settings = {
        "placement": None if draft else placement_for(bundle),
        "observability": observability_for(bundle, draft=draft),
        "limits": limits_for(bundle, paid=paid, draft=draft),
    }
    return {k: v for k, v in settings.items() if v}


def _rel(value: Any, what: str) -> str:
    """A build-relative posix path from the manifest, refusing anything that could
    point outside the build (absolute, drive letters, ``..``)."""
    if not isinstance(value, str) or not value.strip():
        raise _refuse(f"{what} must be a non-empty relative path")
    path = value.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    parts = path.split("/")
    if path.startswith("/") or ":" in parts[0] or any(p in ("", ".", "..") for p in parts):
        raise _refuse(f"{what} {value!r} must stay inside the build directory")
    return path


def _inside(root: Path, rel: str) -> Path:
    resolved = (root / rel).resolve()
    if not resolved.is_relative_to(root):
        raise _refuse(f"{rel!r} resolves outside the build directory")
    return resolved


def _walk(directory: Path) -> Iterable[Path]:
    for p in sorted(directory.rglob("*")):
        if p.is_file():
            yield p


# ---------------------------------------------------------------- compat


def resolve_compat(compat: Any, *, today: date | None = None) -> tuple[str, list[str], list[str]]:
    """``(date, flags, warnings)`` for the upload metadata."""
    warnings: list[str] = []
    compat = compat if isinstance(compat, dict) else {}
    raw_date = compat.get("date", compat.get("compatibility_date"))
    raw_flags = compat.get("flags", compat.get("compatibility_flags")) or []
    today = today or datetime.now(UTC).date()

    flags: list[str] = []
    for flag in raw_flags if isinstance(raw_flags, list) else []:
        if flag in ALLOWED_COMPAT_FLAGS:
            if flag not in flags:
                flags.append(flag)
        else:
            warnings.append(f"compatibility flag {flag!r} is not allowed and was dropped")

    try:
        parsed = date.fromisoformat(raw_date) if isinstance(raw_date, str) else None
    except ValueError:
        parsed = None
    if parsed is None:
        if raw_date:
            warnings.append(f"compatibility date {raw_date!r} is not a date; using the default")
        parsed = date.fromisoformat(DEFAULT_COMPAT_DATE)
    if parsed > today:
        warnings.append(f"compatibility date {parsed} is in the future; using {today}")
        parsed = today
    floor = date.fromisoformat(NODEJS_COMPAT_MIN_DATE)
    if parsed < floor and _NODE_FLAGS.intersection(flags):
        warnings.append(f"compatibility date {parsed} predates nodejs_compat v2; bumped to {floor}")
        parsed = floor
    return parsed.isoformat(), flags, warnings


# --------------------------------------------------------------- bindings


def _request_label(req: Any) -> str:
    if isinstance(req, dict):
        return f"{req.get('type', '?')}:{req.get('name', '?')}"
    return str(req)


def map_bindings(
    requests: Any,
    dropped: Any,
    provisioned: ProvisionedResources,
    *,
    has_assets: bool,
    required_secrets: Any = None,
) -> tuple[list[dict], list[str]]:
    """Turn binding REQUESTS into the upload's bindings, using only our resources.

    Only ``type``, ``name`` and ``required`` are read from a request. Any id, bucket,
    namespace or service the author wrote is ignored by construction. Every secret in
    ``provisioned.secrets`` binds as ``secret_text`` whether requested or not;
    ``required_secrets`` (the manifest's ``requiredSecrets``) adds names that must be
    set, on top of ``secret`` requests marked ``required``."""
    warnings = [f"binding {_request_label(d)} was dropped at build time" for d in dropped or []]
    bindings: list[dict] = []
    names: set[str] = set()
    used: set[str] = set()
    missing = [
        name
        for name in (required_secrets if isinstance(required_secrets, list) else [])
        if isinstance(name, str) and name not in provisioned.secrets
    ]

    for req in requests if isinstance(requests, list) else []:
        if not isinstance(req, dict):
            warnings.append(f"binding request {req!r} is malformed and was dropped")
            continue
        kind = str(req.get("type", "")).strip().lower()
        name = req.get("name")
        label = _request_label(req)
        if kind in _NEVER_FORWARDED or kind not in _PROVISIONED_TYPES | {"assets", "secret"}:
            warnings.append(f"binding {label} is not supported on Paw Sites and was dropped")
            continue
        if not isinstance(name, str) or not _BINDING_NAME.match(name):
            raise _refuse(f"binding name {name!r} is not a valid identifier")
        if name in names:
            raise _refuse(f"binding name {name!r} is requested twice")

        if kind == "assets":
            if not has_assets:
                warnings.append(f"binding {label} dropped: the build has no assets")
                continue
            binding: dict | None = {"type": "assets", "name": name}
        elif kind == "secret":
            value = provisioned.secrets.get(name)
            if value is None:
                if req.get("required"):
                    missing.append(name)
                else:
                    warnings.append(f"optional secret {name!r} is not set; skipped")
                continue
            binding = {"type": "secret_text", "name": name, "text": value}
        else:
            if kind in used and kind not in ("kv", "r2", "do"):
                raise _refuse(f"binding {label}: only one {kind} resource is provisioned per site")
            binding = _provisioned_binding(kind, name, provisioned)
            if binding is None:
                raise _refuse(
                    f"binding {label} needs a {kind} resource that is not provisioned for this site"
                )
            used.add(kind)
        names.add(name)
        bindings.append(binding)

    # The durableObjects block is authoritative: a DO binding with no matching request
    # still binds, but never over another binding's name.
    for name, cls in provisioned.durable_objects.items():
        if any(b["name"] == name and b["type"] == "durable_object_namespace" for b in bindings):
            continue
        if name in names:
            raise _refuse(f"Durable Object binding {name!r} has the same name as another binding")
        bindings.append({"type": "durable_object_namespace", "name": name, "class_name": cls})
        names.add(name)
    if missing:
        raise _secrets_missing(missing)
    requested = {b["name"] for b in bindings if b["type"] == "secret_text"}
    for name in sorted(provisioned.secrets):
        if name in requested:
            continue
        if name in names:
            raise _refuse(f"secret {name!r} has the same name as another binding; rename one")
        if not _BINDING_NAME.match(name):
            raise _refuse(f"secret name {name!r} is not a valid identifier")
        bindings.append({"type": "secret_text", "name": name, "text": provisioned.secrets[name]})
        names.add(name)
    for name in sorted(provisioned.plain_text):
        if not _BINDING_NAME.match(name):
            raise _refuse(f"platform variable {name!r} is not a valid identifier")
        if any(b["name"] == name and b["type"] != "secret_text" for b in bindings):
            raise _refuse(f"platform variable {name!r} has the same name as a binding; rename it")
        bindings = [b for b in bindings if b["name"] != name]
        bindings.append({"type": "plain_text", "name": name, "text": provisioned.plain_text[name]})
        names.add(name)
    return bindings, warnings


def _provisioned_binding(kind: str, name: str, res: ProvisionedResources) -> dict | None:
    if kind == "d1" and res.d1_database_id:
        return {"type": "d1", "name": name, "id": res.d1_database_id}
    if kind == "kv" and res.kv_namespaces.get(name):
        return {"type": "kv_namespace", "name": name, "namespace_id": res.kv_namespaces[name]}
    if kind == "r2" and res.r2_buckets.get(name):
        return {"type": "r2_bucket", "name": name, "bucket_name": res.r2_buckets[name]}
    if kind == "queues" and res.queue_name:
        return {"type": "queue", "name": name, "queue_name": res.queue_name}
    if kind == "ai" and res.ai:
        return {"type": "ai", "name": name}
    if kind == "do" and res.durable_objects.get(name):
        return {
            "type": "durable_object_namespace",
            "name": name,
            "class_name": res.durable_objects[name],
        }
    return None


# ---------------------------------------------------------------- modules


def _strip(base: str, rel: str) -> str:
    return rel[len(base) + 1 :] if base else rel


def _module_files(root: Path, manifest: dict) -> tuple[str | None, list[tuple[str, Path]]]:
    """``(main part name, [(part name, file)])``.

    Part names are relative to the module directory: ``workerModuleDir`` (the
    wrangler dry-run outdir) when given, else where ``mainModule`` sits under
    ``workerEntry``, else the modules' common directory. ``workerModules`` entries
    may be relative to the build root (paw-sites) or to the module directory."""
    entry = manifest.get("workerEntry")
    listed = manifest.get("workerModules") or []
    if not isinstance(listed, list):
        raise _refuse("workerModules must be a list")
    entry_rel = _rel(entry, "workerEntry") if entry else None
    main = manifest.get("mainModule")
    main_rel = _rel(main, "mainModule") if main else None
    rels = list(
        dict.fromkeys(
            ([entry_rel] if entry_rel else []) + [_rel(m, "workerModules entry") for m in listed]
        )
    )
    rels = [r for r in rels if not r.endswith(".map") and posixpath.basename(r) != "README.md"]
    if not rels:
        return None, []

    module_dir = manifest.get("workerModuleDir")
    if module_dir:
        base = _rel(module_dir, "workerModuleDir")
    elif entry_rel and main_rel and entry_rel.endswith("/" + main_rel):
        base = entry_rel[: -len(main_rel) - 1]
    elif entry_rel:
        base = posixpath.dirname(entry_rel)
    else:
        hits = [r for r in rels if main_rel and (r == main_rel or r.endswith("/" + main_rel))]
        if len(hits) == 1:
            base = hits[0][: len(hits[0]) - len(main_rel or "")].rstrip("/")
        elif len(rels) > 1:
            base = posixpath.commonpath(rels)
        else:
            base = posixpath.dirname(rels[0])

    files: dict[str, Path] = {}
    for rel in rels:
        path = _inside(root, rel)
        if not path.is_file() and base:
            rel = posixpath.join(base, rel)
            path = _inside(root, rel)
        if not path.is_file():
            raise _refuse(f"worker module {rel!r} is missing from the build")
        if base and not rel.startswith(base + "/"):
            raise _refuse(f"worker module {rel!r} is outside the module directory {base!r}")
        files.setdefault(_strip(base, rel), path)

    if main_rel and main_rel in files:
        main_name = main_rel
    elif main_rel and base and main_rel.startswith(base + "/"):
        main_name = _strip(base, main_rel)
    elif entry_rel:
        main_name = _strip(base, entry_rel)
    elif len(files) == 1:
        main_name = next(iter(files))
    else:
        raise _refuse("paw-build.json names no main module among several worker modules")
    if main_name not in files:
        raise _refuse(f"main module {main_name!r} is not one of the worker modules")
    return main_name, list(files.items())


def _load_modules(root: Path, manifest: dict) -> tuple[str | None, list[WorkerModule], set[Path]]:
    main_name, files = _module_files(root, manifest)
    if len(files) > MAX_WORKER_MODULES:
        raise _refuse(
            f"the worker has {len(files)} modules, over the {MAX_WORKER_MODULES}-module cap; "
            "bundle it before deploying"
        )
    total = sum(p.stat().st_size for _, p in files)
    if total > MAX_WORKER_BYTES:
        raise _refuse(f"the worker is {total} bytes uncompressed, over Cloudflare's 64 MiB limit")
    modules = [WorkerModule(n, p.read_bytes(), module_content_type(n)) for n, p in files]
    return main_name, modules, {p for _, p in files}


# ----------------------------------------------------------------- assets


def _assets_config(raw: Any, assets_dir: Path | None) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    raw = raw if isinstance(raw, dict) else {}
    config: dict[str, Any] = {}
    for key, value in raw.items():
        if value is None:
            continue
        if key in _ASSETS_CONFIG_FILES and isinstance(value, str):
            config[key] = value
        elif key == "html_handling" and value in _HTML_HANDLING:
            config[key] = value
        elif key == "not_found_handling" and value in _NOT_FOUND_HANDLING:
            config[key] = value
        elif key == "run_worker_first" and (
            isinstance(value, bool)
            or (isinstance(value, list) and all(isinstance(v, str) for v in value))
        ):
            config[key] = value
        else:
            warnings.append(f"assets setting {key!r} is not supported and was dropped")
    # Older manifests leave _headers / _redirects in the assets dir: lift them.
    for name in _ASSETS_CONFIG_FILES:
        if name not in config and assets_dir is not None and (assets_dir / name).is_file():
            config[name] = (assets_dir / name).read_text("utf-8")
    return config, warnings


def _assetsignore(text: str) -> re.Pattern[str] | None:
    """``.assetsignore`` (the gitignore subset wrangler honours: ``*``, ``**``,
    ``?``, a leading ``/``) as one regex over asset paths. Negations are ignored,
    matching paw-sites' ``liftAssetsConfig``."""
    patterns = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "!")):
            continue
        pat = line.lstrip("/").rstrip("/")
        anchored = line.startswith("/") or "/" in pat
        body = ".*".join(
            re.escape(part).replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
            for part in pat.split("**")
        )
        patterns.append(f"{'^' if anchored else '(?:^|/)'}{body}(?:/.*)?$")
    return re.compile("|".join(f"(?:{p})" for p in patterns)) if patterns else None


def _load_assets(root: Path, manifest: dict, module_paths: set[Path]) -> tuple[dict, Path | None]:
    raw_dir = manifest.get("assetsDir")
    if not raw_dir:
        return {}, None
    assets_dir = _inside(root, _rel(raw_dir, "assetsDir"))
    if not assets_dir.is_dir():
        raise _refuse(f"assetsDir {raw_dir!r} is missing from the build")
    ignore_file = assets_dir / ".assetsignore"
    ignored = _assetsignore(ignore_file.read_text("utf-8")) if ignore_file.is_file() else None
    assets: dict[str, bytes] = {}
    for path in _walk(assets_dir):
        rel = path.relative_to(assets_dir).as_posix()
        if rel in _ASSETS_SKIP or path.resolve() in module_paths:
            continue
        if ignored is not None and ignored.search(rel):
            continue
        if not path.resolve().is_relative_to(root):
            raise _refuse(f"asset {rel!r} links outside the build directory")
        size = path.stat().st_size
        if size > MAX_ASSET_FILE_BYTES:
            raise _refuse(f"asset {rel!r} is {size} bytes, over the 25 MiB per-file limit")
        if len(assets) >= MAX_ASSET_FILES:
            raise _refuse(f"the build has more than {MAX_ASSET_FILES} asset files")
        assets["/" + rel] = path.read_bytes()
    return assets, assets_dir


# ------------------------------------------------------------------ entry


def load_bundle(build_dir: str | Path, provisioned: ProvisionedResources) -> PawBundle:
    """Read and vet a build's ``paw-build.json``. Raises ``ValidationError`` with a
    clear message on anything that must not deploy."""
    bundle, manifest = _read_bundle(build_dir)
    _map_into(bundle, manifest, provisioned)
    return bundle


def _map_into(bundle: PawBundle, manifest: dict, provisioned: ProvisionedResources) -> None:
    bindings, binding_warnings = map_bindings(
        manifest.get("bindingRequests"),
        manifest.get("droppedBindings"),
        provisioned,
        has_assets=bool(bundle.assets),
        required_secrets=manifest.get("requiredSecrets", manifest.get("required_secrets")),
    )
    bundle.bindings = bindings
    bundle.warnings.extend(binding_warnings)


def _read_bundle(build_dir: str | Path) -> tuple[PawBundle, dict]:
    """Everything ``load_bundle`` vets except the bindings, plus the manifest."""
    root = Path(build_dir).resolve()
    try:
        manifest = json.loads((root / PAW_BUILD_FILENAME).read_text("utf-8"))
    except FileNotFoundError as exc:
        raise _refuse("the build has no paw-build.json") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _refuse(f"paw-build.json is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise _refuse("paw-build.json must be a JSON object")

    main_module, modules, module_paths = _load_modules(root, manifest)
    assets, assets_dir = _load_assets(root, manifest, module_paths)
    if not modules and not assets:
        raise _refuse("paw-build.json has neither worker modules nor assets")
    config, warnings = _assets_config(manifest.get("assetsConfig"), assets_dir)
    date_, flags, compat_warnings = resolve_compat(manifest.get("compat"))
    bundle = PawBundle(
        main_module=main_module,
        modules=modules,
        assets=assets,
        assets_config=config,
        compatibility_date=date_,
        compatibility_flags=flags,
        bindings=[],
        warnings=warnings + compat_warnings,
    )
    return bundle, manifest


async def deploy_bundle(
    cf: Any,
    *,
    script_name: str,
    build_dir: str | Path,
    salt: str,
    provisioned: ProvisionedResources | None = None,
    provision: Callable[[Any], Awaitable[ProvisionedResources]] | None = None,
    before_upload: Callable[[list[dict]], Awaitable[None]] | None = None,
    target: str = DISPATCH_TARGET,
    paid: bool = False,
    draft: bool = False,
    main_wrapper: Callable[[str], WorkerModule] | None = None,
    do_state: durable_objects.DurableObjectState | None = None,
    confirm_do_data_loss: Sequence[str] = (),
    allow_do_data_loss: bool = False,
    do_quota_used: Callable[[], Awaitable[int]] | None = None,
    do_throttled: bool = False,
    site_origins: Sequence[str] | Callable[[], Awaitable[Sequence[str]]] = (),
) -> BundleDeployResult:
    """Vet the build, provision its backends, upload its assets, then PUT the
    Worker. Live on success.

    ``salt`` is the tenant key the asset hashes are salted with (the workspace id).
    ``provision``, when given, receives the raw ``bindingRequests`` and returns the
    site's resources (it replaces ``provisioned``). It runs after every module,
    asset and compat check, so a bundle refused for those creates nothing; a
    binding refusal still happens before the first upload, so the live site is
    untouched either way. ``before_upload``, when given, receives the mapped bindings
    once every check has passed and runs before the first upload (a project's D1
    migrations); raising there also leaves the live site untouched.

    ``target`` is ``dispatch`` (the WfP namespace) or ``account`` (a regular
    account-level script, interim until the account has WfP: the tenant's code runs
    as one of the account's own Workers with no dispatch isolation in front).

    ``paid`` is the site's ``entitlements.site_paid_backends_entitled`` answer; it
    picks the per-request CPU and subrequest caps (``limits_for``). Unknown means
    free, the tighter cap. ``draft`` (a ``draft_worker`` deploy) uses the draft
    limit / observability knobs and no placement. ``main_wrapper``, given the main
    module's name, returns a module that becomes the new entry (the draft guard); it
    is added after every check, so the wrapper itself is never author-controlled.

    ``do_state`` is what the script already has applied (Durable Object migration
    tag, live classes); unknown means a fresh script. ``confirm_do_data_loss`` names
    the classes the owner agreed to delete or rename; ``allow_do_data_loss`` (drafts)
    skips that confirmation. ``do_quota_used`` returns how many DO classes the
    workspace's other scripts hold; it is asked only when this deploy creates a
    class (``durable_objects.check_workspace_quota``). A DO bundle also gets the
    platform vars (``durable_objects.platform_vars``: the plan's room cap,
    ``do_throttled`` from the usage sweep, and ``site_origins``, a list or an async
    callable only awaited for a DO bundle). The DO block is vetted, its
    migration planned and the account budget checked before ``provision``, so a
    refused bundle creates nothing. The result carries the tag Cloudflare reports."""
    if target not in SCRIPT_TARGETS:
        raise _refuse(f"unknown deploy target {target!r}")
    bundle, manifest = _read_bundle(build_dir)
    vetted = durable_objects.vet_durable_objects(
        manifest,
        paid=paid,
        state=do_state,
        confirm=confirm_do_data_loss,
        allow_data_loss=allow_do_data_loss,
    )
    if vetted is not None and not bundle.modules:
        raise _refuse("Durable Objects need a worker module; the build has only assets")
    if vetted is not None and vetted.plan.new_classes and do_quota_used is not None:
        durable_objects.check_workspace_quota(await do_quota_used(), vetted)
    await durable_objects.check_account_budget(cf, vetted, target=target, draft=draft)
    if provision is not None:
        provisioned = await provision(manifest.get("bindingRequests"))
    provisioned = provisioned or ProvisionedResources()
    if vetted is not None:
        provisioned = replace(
            provisioned,
            durable_objects=dict(vetted.bindings),
            plain_text={
                **provisioned.plain_text,
                **durable_objects.platform_vars(
                    paid=paid,
                    throttled=do_throttled,
                    origins=await site_origins() if callable(site_origins) else site_origins,
                ),
            },
        )
    _map_into(bundle, manifest, provisioned)
    for warning in bundle.warnings:
        logger.warning("sites.bundle_deploy %s: %s", script_name, warning)
    if main_wrapper is not None and bundle.main_module:
        wrapper = main_wrapper(bundle.main_module)
        if any(m.name == wrapper.name for m in bundle.modules):
            raise _refuse(f"the build already has a module named {wrapper.name}")
        bundle.modules.append(wrapper)
        bundle.main_module = wrapper.name
    if before_upload is not None:
        await before_upload(bundle.bindings)

    if target == ACCOUNT_TARGET:
        logger.warning(
            "sites.bundle_deploy %s: deploying as an ACCOUNT-LEVEL Worker (interim, no "
            "dispatch isolation); move project sites to Workers for Platforms",
            script_name,
        )
    assets_meta = None
    if bundle.assets:
        jwt = await cf.upload_assets(
            script_name=script_name, assets=bundle.assets, salt=salt, target=target
        )
        assets_meta = {"jwt": jwt, "config": bundle.assets_config}
    # Sent only when the plan has steps, so a deploy without DOs is unchanged.
    migrations = vetted.plan.migrations if vetted is not None else None
    upload = await cf.put_worker(
        script_name=script_name,
        modules=bundle.modules,
        main_module=bundle.main_module,
        bindings=bundle.bindings,
        compatibility_date=bundle.compatibility_date,
        compatibility_flags=bundle.compatibility_flags,
        assets=assets_meta,
        target=target,
        **worker_settings(bundle, paid=paid, draft=draft),
        **({"migrations": migrations} if migrations else {}),
    )
    tag: str | None = None
    history: tuple[str, ...] = ()
    if vetted is not None:
        # Cloudflare's answer wins; ours is the cross-check (and the fallback when the
        # response omits it, since a 2xx upload applied the migration).
        reported = getattr(upload, "migration_tag", None)
        if reported and reported != vetted.plan.tag:
            logger.warning(
                "sites.bundle_deploy %s: Cloudflare reports migration tag %r, we planned %r",
                script_name,
                reported,
                vetted.plan.tag,
            )
        tag = reported or vetted.plan.tag
        history = durable_objects.migration_tags_after(vetted, reported)
    return BundleDeployResult(
        script_name=script_name,
        modules=len(bundle.modules),
        assets=len(bundle.assets),
        warnings=list(bundle.warnings),
        migration_tag=tag,
        do_classes=vetted.classes if vetted is not None else (),
        migration_tags=history,
    )
