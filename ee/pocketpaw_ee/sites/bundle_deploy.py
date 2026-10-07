# ee/pocketpaw_ee/sites/bundle_deploy.py: deploy a ``paw-build.json`` build (the
# ``project`` engine / base app templates) into the Workers for Platforms dispatch
# namespace through the Cloudflare HTTP API.
#
# The build ran in a sandbox from author-owned config. This module is the trust
# boundary on the API host: it reads ONLY ``paw-build.json`` and the files it names,
# never a wrangler config, and never runs anything. What crosses into the upload:
#   * modules: the files under the worker module dir, part-named by their path
#     relative to it, typed by extension; ``*.map`` and README.md are skipped.
#   * assets: every file under ``assetsDir`` except ``_headers`` / ``_redirects``
#     (they travel as ``assets.config`` strings, next to the routing options
#     html_handling / not_found_handling / run_worker_first) and ``.assetsignore``
#     and what it matches. Assets upload whether or not an ``assets`` binding is
#     requested; the binding only exposes them to the worker.
#   * compat: the author's date (bumped to 2024-09-23 for nodejs_compat, clamped to
#     today, defaulted when missing) and only allow-listed flags.
#   * bindings: requests are mapped by TYPE and NAME onto resources WE provisioned
#     for this site. Author-supplied ids are never read. ``deploy_bundle`` takes an
#     optional ``provision`` callback (``binding_provisioner.ensure_bindings``) that
#     runs after every other check and before the first upload, creating the site's
#     KV namespaces / R2 buckets; KV and R2 map per binding name, D1 / queues / ai
#     are one per site. services, dispatch namespaces, tail consumers, images and
#     anything unknown are dropped with a warning; an unprovisioned d1/kv/r2/do/ai/
#     queues request refuses the deploy. An optional ``before_upload`` hook gets the
#     mapped bindings after every check and before the first upload (the project
#     engine applies its D1 migrations there).
#   * secrets: every secret the owner SET for the site (``site_secrets``) binds as
#     ``secret_text``, requested or not. A ``secret`` request with ``required`` or a
#     name in the manifest's ``requiredSecrets`` that is not set refuses the deploy
#     with ``sites.secrets_missing`` naming what to set in the builder. Values only
#     ever live in the binding: never in a warning, an error, a log or a repr.
#   * limits: 64 MiB of modules, our own module-count cap (Cloudflare documents
#     none), and the static-asset caps. Over any of them refuses before upload.
#
# The manifest shape is paw-sites' ``buildPawManifest`` (src/starters.ts). The
# parser also accepts the earlier shape (no ``workerModuleDir`` / ``mainModule``,
# no ``assetsConfig``, wrangler key names in ``compat``) and ignores unknown fields
# such as ``sizes`` and ``startup``. Everything the deploy drops is a warning.
from __future__ import annotations

import json
import logging
import posixpath
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites.cloudflare_client import WorkerModule, module_content_type

logger = logging.getLogger(__name__)

PAW_BUILD_FILENAME = "paw-build.json"

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
            if kind in used and kind not in ("kv", "r2"):
                raise _refuse(f"binding {label}: only one {kind} resource is provisioned per site")
            binding = _provisioned_binding(kind, name, provisioned)
            if binding is None:
                raise _refuse(
                    f"binding {label} needs a {kind} resource that is not provisioned for this site"
                )
            used.add(kind)
        names.add(name)
        bindings.append(binding)

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
    # Durable Objects need a class + migrations we do not provision yet.
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
    migrations); raising there also leaves the live site untouched."""
    bundle, manifest = _read_bundle(build_dir)
    if provision is not None:
        provisioned = await provision(manifest.get("bindingRequests"))
    _map_into(bundle, manifest, provisioned or ProvisionedResources())
    for warning in bundle.warnings:
        logger.warning("sites.bundle_deploy %s: %s", script_name, warning)
    if before_upload is not None:
        await before_upload(bundle.bindings)

    assets_meta = None
    if bundle.assets:
        jwt = await cf.upload_assets(script_name=script_name, assets=bundle.assets, salt=salt)
        assets_meta = {"jwt": jwt, "config": bundle.assets_config}
    await cf.put_worker(
        script_name=script_name,
        modules=bundle.modules,
        main_module=bundle.main_module,
        bindings=bundle.bindings,
        compatibility_date=bundle.compatibility_date,
        compatibility_flags=bundle.compatibility_flags,
        assets=assets_meta,
    )
    return BundleDeployResult(
        script_name=script_name,
        modules=len(bundle.modules),
        assets=len(bundle.assets),
        warnings=list(bundle.warnings),
    )
