# ee/pocketpaw_ee/sites/draft_worker.py: the account-level draft Worker of a
# ``project`` pocket, so a draft preview runs the site's real server code.
#
# Behind ``PAW_SITES_DRAFT_WORKERS=1`` and only on the ``account`` project deploy
# target (``bundle_deploy.project_deploy_target``). Design and rationale:
# docs/design/drafts/2026-10-08-sites-draft-worker.md (workspace root).
#
# What this module owns:
#   * naming: ``paw-draft-[<env tag>-]<pocket id>-<16 hex>``. ``paw-`` is reserved
#     from user slugs (``slug.has_reserved_prefix``) and published scripts are a slug
#     or ``paw-site-<id>``, so names never collide. The random tail keeps the
#     workers.dev host unguessable and rotates on every purge. ``PAW_SITES_DRAFT_ENV_TAG``
#     scopes names (and the orphan sweep) to one deployment sharing the account.
#   * the registry (``SiteDraftWorker`` rows; ``MemoryRegistry`` for tests): the
#     script, its host, the content hash it runs, its draft-only D1 / KV / R2, and a
#     ``version`` every write bumps. A deploy writes only by compare-and-set on it,
#     so a purge that lands mid-deploy wins: the deploy deletes what it uploaded.
#   * ``deploy_for_build``: called by the preview build after the bundle is stored.
#     It provisions draft-only resources, applies migrations (destructive allowed,
#     a changed applied migration recreates the draft DB) and ``seed/*.sql`` once,
#     binds ONLY draft secrets (``NAME__DRAFT`` as ``NAME``, per-draft signing
#     secrets, never a production value), sets ``BETTER_AUTH_URL`` / ``PAW_SITE_URL``
#     to the preview origin, wraps the entry in a guard that requires the per-draft
#     ``X-Paw-Draft-Key`` (static assets bypass it), and deploys. Any failure returns
#     a rung and the build falls back; it never fails.
#   * ``proxy_target``: which token the preview origin proxies (only the deployed
#     hash of a ``live`` row; an older hash of a proxied draft is ``SUPERSEDED``).
#   * Durable Objects (``durable_objects``): the row keeps the draft script's tag
#     history and live classes, written right after a successful upload. Draft data
#     is throwaway, so destructive migrations need no confirmation, and a history the
#     row does not recognise tears the old script down and rotates to a fresh name.
#     A new class counts against the account DO budget like a published one.
#   * cleanup: ``purge_pocket_drafts`` (publish, site / pocket delete, sweeper).
#     Idempotent; a Cloudflare 404 is success; a script with DOs goes through
#     ``durable_objects.teardown_script``. A failure leaves the row ``deleting``
#     with a backoff and ``sweep_draft_workers`` retries it, reaps drafts idle past
#     ``PAW_SITES_DRAFT_TTL_DAYS`` and force-deletes unregistered ``paw-draft-*``
#     scripts.
#   * the script cap: a NEW draft reserves a slot under ``PAW_SITES_DRAFT_SCRIPT_CAP``
#     account scripts (released on failure), or is refused when the count cannot be
#     read. Replicas each cache the count for a minute, so they can still overshoot.
#
# Invariant: secret VALUES never appear in a log line, an error, a rung or a record
# field other than the Fernet-encrypted ``auth_secret_enc`` / ``draft_key_enc``.
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pocketpaw_ee.cloud._core.errors import ValidationError

logger = logging.getLogger(__name__)

ENABLE_ENV = "PAW_SITES_DRAFT_WORKERS"
SCRIPT_CAP_ENV = "PAW_SITES_DRAFT_SCRIPT_CAP"
DEFAULT_SCRIPT_CAP = 450
TTL_DAYS_ENV = "PAW_SITES_DRAFT_TTL_DAYS"
DEFAULT_TTL_DAYS = 7

ENV_TAG_ENV = "PAW_SITES_DRAFT_ENV_TAG"
SCRIPT_PREFIX = "paw-draft-"
_TAG_MAX = 8

#: The guard module the draft's upload enters through, and the secret it checks.
GUARD_MODULE = "__paw_draft_guard.mjs"
DRAFT_KEY_BINDING = "PAW_DRAFT_KEY"
DRAFT_KEY_HEADER = "x-paw-draft-key"

#: Session-signing secrets a draft never shares with production.
SIGNING_SECRETS = ("BETTER_AUTH_SECRET", "AUTH_SECRET", "SESSION_SECRET")
#: ``NAME__DRAFT`` binds as ``NAME`` in a draft, in place of the live value.
DRAFT_SUFFIX = "__DRAFT"
#: Platform env set to the draft's preview origin; wins over owner values.
PLATFORM_URL_VARS = ("BETTER_AUTH_URL", "PAW_SITE_URL")

SEED_DIR = "seed"

#: ``draft_worker_reason`` rungs (``draft_worker:<reason>``).
REASONS = frozenset(
    {
        "disabled",
        "cap",
        "cleanup_pending",
        "not_entitled",
        "secrets_missing",
        "provision_failed",
        "migration_failed",
        "deploy_failed",
        "superseded",
        "gone",
    }
)

_TARGET_TTL = 30.0
_COUNT_TTL = 60.0
_TRUTHY = {"1", "true", "yes", "on"}


def enabled() -> bool:
    return (os.environ.get(ENABLE_ENV) or "").strip().lower() in _TRUTHY


def _int_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        logger.warning("sites.draft: %s=%r is not an int; using %d", name, raw, default)
        return default
    return value if value > 0 else default


# ---------------------------------------------------------------- names + secrets


def _pid(pocket_id: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(pocket_id).lower())


def env_tag() -> str:
    """``PAW_SITES_DRAFT_ENV_TAG``, lowercased to ``[a-z0-9]``, at most 8 chars."""
    raw = re.sub(r"[^a-z0-9]", "", (os.environ.get(ENV_TAG_ENV) or "").lower())
    return raw[:_TAG_MAX]


def _prefix() -> str:
    tag = env_tag()
    return f"{SCRIPT_PREFIX}{tag}-" if tag else SCRIPT_PREFIX


def new_script_name(pocket_id: str) -> str:
    return f"{_prefix()}{_pid(pocket_id)}-{secrets.token_hex(8)}"


def pocket_of_script(name: str) -> str | None:
    """The pocket of a draft script THIS deployment (its env tag) named, else None."""
    m = re.match(rf"^{re.escape(_prefix())}([a-z0-9]+)-[a-f0-9]{{16}}$", name or "")
    return m.group(1) if m else None


def draft_database_name(pocket_id: str) -> str:
    return f"{_prefix()}{_pid(pocket_id)}"


def draft_resource_id(pocket_id: str) -> str:
    """The ``site_id`` ``binding_provisioner.resource_name`` names draft KV / R2 by:
    ``paw-draft<tag><pid>-...`` can never equal a published site's ``paw-<24 hex>-...``."""
    return f"draft{env_tag()}{_pid(pocket_id)}"


def draft_secrets(owner: Mapping[str, str], *, signing_value: str) -> dict[str, str]:
    """The secrets a draft binds: ONLY each owner ``NAME__DRAFT`` (bound as ``NAME``)
    plus the draft's own session-signing value. A production value never reaches a
    draft; a secret with no draft value is simply not bound."""
    out: dict[str, str] = {}
    for name, value in owner.items():
        if name.endswith(DRAFT_SUFFIX) and len(name) > len(DRAFT_SUFFIX):
            out[name[: -len(DRAFT_SUFFIX)]] = value
    for name in SIGNING_SECRETS:
        out[name] = signing_value
    return out


def required_secrets(manifest: Mapping[str, Any]) -> list[str]:
    """Secret names the build requires, in manifest order (``requiredSecrets`` then
    ``secret`` requests marked ``required``)."""
    names: list[str] = []
    listed = manifest.get("requiredSecrets", manifest.get("required_secrets"))
    for name in listed if isinstance(listed, list) else []:
        if isinstance(name, str) and name not in names:
            names.append(name)
    requests = manifest.get("bindingRequests")
    for req in requests if isinstance(requests, list) else []:
        if (
            isinstance(req, dict)
            and str(req.get("type", "")).lower() == "secret"
            and req.get("required")
            and isinstance(req.get("name"), str)
            and req["name"] not in names
        ):
            names.append(req["name"])
    return names


def guard_module(main_module: str) -> Any:
    """The draft's entry module: re-exports the real entry, and answers 404 unless
    the request carries ``X-Paw-Draft-Key`` equal to the ``PAW_DRAFT_KEY`` binding,
    which it strips before calling the real ``fetch``. A class default export
    (``WorkerEntrypoint``) is instantiated per request. Static assets the platform
    serves before the Worker runs are not covered."""
    from pocketpaw_ee.sites.cloudflare_client import WorkerModule, module_content_type

    spec = json.dumps("./" + main_module)
    code = f"""// Paw draft guard: only the preview origin (it holds PAW_DRAFT_KEY) may call.
import * as app from {spec};
export * from {spec};
const HEADER = "{DRAFT_KEY_HEADER}";
const enc = new TextEncoder();
function allowed(got, want) {{
  if (typeof got !== "string" || typeof want !== "string" || !want) return false;
  const a = enc.encode(got);
  const b = enc.encode(want);
  return a.byteLength === b.byteLength && crypto.subtle.timingSafeEqual(a, b);
}}
const inner = app.default;
const isClass = typeof inner === "function";
const guarded = isClass ? {{}} : {{ ...inner }};
guarded.fetch = async (request, env, ctx) => {{
  if (!allowed(request.headers.get(HEADER), env.{DRAFT_KEY_BINDING})) {{
    return new Response("Not found", {{ status: 404 }});
  }}
  const headers = new Headers(request.headers);
  headers.delete(HEADER);
  const clean = new Request(request, {{ headers }});
  return isClass ? new inner(ctx, env).fetch(clean) : inner.fetch(clean, env, ctx);
}};
export default guarded;
"""
    return WorkerModule(
        name=GUARD_MODULE, content=code.encode(), content_type=module_content_type(GUARD_MODULE)
    )


# ---------------------------------------------------------------- registry


@dataclass
class DraftRecord:
    pocket_id: str
    workspace: str = ""
    script: str = ""
    host: str = ""
    deployed_hash: str = ""
    deployed_at: datetime | None = None
    d1_database_id: str = ""
    kv_namespaces: dict[str, str] = field(default_factory=dict)
    r2_buckets: dict[str, str] = field(default_factory=dict)
    do_migration_tags: list[str] = field(default_factory=list)
    do_classes: list[str] = field(default_factory=list)
    seeded: bool = False
    auth_secret_enc: str = field(default="", repr=False)
    draft_key_enc: str = field(default="", repr=False)
    state: str = "live"
    attempts: int = 0
    last_error: str = ""
    retry_after: datetime | None = None
    published_hash: str = ""
    published_url: str = ""
    version: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_FIELDS = tuple(DraftRecord.__dataclass_fields__)


def _copy(rec: DraftRecord) -> DraftRecord:
    return replace(
        rec,
        kv_namespaces=dict(rec.kv_namespaces),
        r2_buckets=dict(rec.r2_buckets),
        do_migration_tags=list(rec.do_migration_tags),
        do_classes=list(rec.do_classes),
    )


class MemoryRegistry:
    """In-memory registry (tests, and a stand-in where Mongo is absent). ``put``
    always writes and bumps ``version``; ``cas`` writes only over ``expected``."""

    def __init__(self) -> None:
        self.rows: dict[str, DraftRecord] = {}

    async def get(self, pocket_id: str) -> DraftRecord | None:
        row = self.rows.get(pocket_id)
        return _copy(row) if row else None

    async def put(self, rec: DraftRecord) -> None:
        current = self.rows.get(rec.pocket_id)
        rec.version = max(rec.version, current.version if current else 0) + 1
        self.rows[rec.pocket_id] = _copy(rec)

    async def cas(self, rec: DraftRecord, expected: int | None) -> bool:
        current = self.rows.get(rec.pocket_id)
        if (current is None) != (expected is None):
            return False
        if current is not None and current.version != expected:
            return False
        rec.version = (expected or 0) + 1
        self.rows[rec.pocket_id] = _copy(rec)
        return True

    async def delete(self, pocket_id: str) -> None:
        self.rows.pop(pocket_id, None)

    async def all(self) -> list[DraftRecord]:
        return [await self.get(k) for k in list(self.rows)]  # type: ignore[misc]


class MongoRegistry:
    """``SiteDraftWorker`` rows, one per pocket."""

    @staticmethod
    def _model():
        from pocketpaw_ee.cloud.models.site_draft_worker import SiteDraftWorker

        return SiteDraftWorker

    @staticmethod
    def _rec(doc: Any) -> DraftRecord:
        return DraftRecord(**{k: getattr(doc, k) for k in _FIELDS})

    async def get(self, pocket_id: str) -> DraftRecord | None:
        model = self._model()
        doc = await model.find_one({"pocket_id": pocket_id})
        return self._rec(doc) if doc is not None else None

    async def put(self, rec: DraftRecord) -> None:
        """Unconditional write (purge, sweeper); bumps ``version`` so any deploy that
        read the row before loses its compare-and-set."""
        from pymongo.errors import DuplicateKeyError

        model = self._model()
        doc = await model.find_one({"pocket_id": rec.pocket_id})
        rec.version = max(rec.version, getattr(doc, "version", 0) if doc else 0) + 1
        data = rec.to_dict()
        if doc is None:
            try:
                await model(**data).insert()
                return
            except DuplicateKeyError:
                doc = await model.find_one({"pocket_id": rec.pocket_id})
                if doc is None:
                    raise
        await doc.set(data)

    async def cas(self, rec: DraftRecord, expected: int | None) -> bool:
        """Write ``rec`` only when the stored row is at ``expected`` (``None``: only
        when there is no row). True when written; ``rec.version`` is then bumped."""
        from pymongo.errors import DuplicateKeyError

        model = self._model()
        data = {**rec.to_dict(), "version": (expected or 0) + 1}
        if expected is None:
            try:
                await model(**data).insert()
            except DuplicateKeyError:
                return False
        else:
            data["updatedAt"] = datetime.now(UTC)
            result = await model.find_one({"pocket_id": rec.pocket_id, "version": expected}).update(
                {"$set": data}
            )
            if not getattr(result, "matched_count", 0):
                return False
        rec.version = data["version"]
        return True

    async def delete(self, pocket_id: str) -> None:
        await self._model().find({"pocket_id": pocket_id}).delete()

    async def all(self) -> list[DraftRecord]:
        return [self._rec(d) for d in await self._model().find({}).to_list()]


def default_registry() -> Any:
    return MongoRegistry()


# ---------------------------------------------------------------- caches


_target_cache: dict[str, tuple[float, DraftRecord | None, str]] = {}
_count_cache: list[Any] = [0.0, 0]


def _reset_caches() -> None:
    _target_cache.clear()
    _count_cache[:] = [0.0, 0]


def _invalidate(pocket_id: str) -> None:
    _target_cache.pop(pocket_id, None)


# ---------------------------------------------------------------- proxy target


@dataclass(frozen=True)
class ProxyTarget:
    pocket_id: str
    host: str
    key: str = field(default="", repr=False)


#: An older content hash of a pocket whose draft Worker now runs newer code: a 404.
SUPERSEDED = object()


async def proxy_target(pocket_id: str, content_hash: str, *, registry: Any = None) -> Any:
    """The draft Worker a token of ``(pocket_id, content_hash)`` proxies to,
    ``SUPERSEDED`` for an older hash of a proxied draft, else ``None`` (static)."""
    now = time.monotonic()
    hit = _target_cache.get(pocket_id)
    if hit is not None and hit[0] > now:
        rec, key = hit[1], hit[2]
    else:
        rec = await (registry or default_registry()).get(pocket_id)
        key = _decrypt(rec.draft_key_enc) if rec is not None else ""
        _target_cache[pocket_id] = (now + _TARGET_TTL, rec, key)
    if rec is None or rec.state != "live" or not rec.host or not rec.deployed_hash:
        return None
    if rec.deployed_hash != content_hash:
        return SUPERSEDED
    # Without its key the guard would refuse every request: serve the static draft.
    return ProxyTarget(pocket_id=pocket_id, host=rec.host, key=key) if key else None


def _decrypt(token: str) -> str:
    from pocketpaw_ee.cloud._core import crypto

    if not token or not crypto.is_configured():
        return ""
    try:
        return crypto.decrypt(token)
    except Exception:  # noqa: BLE001 - a rotated key: the next deploy re-mints
        return ""


async def published_view(
    pocket_id: str, content_hash: str, *, registry: Any = None
) -> tuple[bool, str | None]:
    """``(True, url or None)`` when ``content_hash`` is what this pocket last
    published and its drafts were purged: the builder shows the live site."""
    rec = await (registry or default_registry()).get(pocket_id)
    if rec is None or not rec.published_hash or rec.published_hash != content_hash:
        return False, None
    if rec.state == "live" and rec.deployed_hash == content_hash:
        return False, None
    return True, rec.published_url or None


# ---------------------------------------------------------------- deploy


@dataclass(frozen=True)
class DraftOutcome:
    mode: str | None
    reason: str | None
    #: Required secret names with no ``NAME__DRAFT`` value (names only).
    missing_secrets: tuple[str, ...] = ()


class _Superseded(Exception):
    """The registry row changed under a deploy (a purge won)."""


class _Gone(Exception):
    """The pocket or its site went away before the upload."""


async def _discard(rec: DraftRecord, current: DraftRecord | None, cf: Any) -> None:
    """Delete what a superseded deploy holds that ``current`` (the row now) does not
    name. Best effort; Cloudflare 404s count as done."""
    keep_script = current.script if current else ""
    keep = set()
    if current is not None:
        keep = {
            current.d1_database_id,
            *current.kv_namespaces.values(),
            *current.r2_buckets.values(),
        }
    steps: list[tuple[str, Any]] = []
    if rec.script and rec.script != keep_script:
        if rec.do_classes:
            # A script with Durable Objects only deletes with force.
            steps.append(
                ("delete_account_script", lambda: cf.delete_account_script(rec.script, force=True))
            )
        else:
            steps.append(("delete_account_script", lambda: cf.delete_account_script(rec.script)))
    if rec.d1_database_id and rec.d1_database_id not in keep:
        steps.append(("delete_database", lambda: cf.delete_database(rec.d1_database_id)))
    for ns in rec.kv_namespaces.values():
        if ns not in keep:
            steps.append(("delete_kv_namespace", lambda ns=ns: cf.delete_kv_namespace(ns)))
    for bucket in rec.r2_buckets.values():
        if bucket not in keep:
            steps.append(("delete_r2_bucket", lambda b=bucket: cf.delete_r2_bucket(b)))
    for label, run in steps:
        try:
            await run()
        except Exception as exc:  # noqa: BLE001 - logged; the orphan sweep catches scripts
            logger.warning(
                "sites.draft: pocket %s discard %s failed: %s", rec.pocket_id, label, exc
            )


def _skip(reason: str) -> DraftOutcome:
    return DraftOutcome(None, f"draft_worker:{reason}")


async def _reserve_slot(cf: Any) -> bool:
    """Take one script slot under the cap (counted against the account, cached for
    a minute per process). Give it back with ``_release_slot`` if the deploy fails."""
    cap = _int_env(SCRIPT_CAP_ENV, DEFAULT_SCRIPT_CAP)
    now = time.monotonic()
    if _count_cache[0] <= now:
        try:
            count = len(await cf.list_account_scripts())
        except Exception as exc:  # noqa: BLE001 - fail closed
            logger.warning("sites.draft: could not count account scripts (%s); no new draft", exc)
            return False
        _count_cache[:] = [now + _COUNT_TTL, count]
    if _count_cache[1] >= cap:
        logger.warning(
            "sites.draft: %d account scripts, cap %d; no new draft", _count_cache[1], cap
        )
        return False
    _count_cache[1] += 1
    return True


def _release_slot() -> None:
    _count_cache[1] = max(int(_count_cache[1]) - 1, 0)


async def _default_alive(pocket_id: str, workspace_id: str) -> bool:
    """The pocket still exists and its site (if any) is not being deleted."""
    from beanie import PydanticObjectId

    from pocketpaw_ee.cloud.models.pocket import Pocket
    from pocketpaw_ee.sites import service as sites_service

    if await Pocket.get(PydanticObjectId(pocket_id)) is None:
        return False
    site = (
        await sites_service._canonical_site_doc(workspace_id, pocket_id) if workspace_id else None
    )
    status = (getattr(site, "delete_status", "") or "none") if site is not None else "none"
    return status == "none"


async def _default_context(pocket_id: str) -> tuple[str, bool]:
    """``(workspace_id, paid)`` of a pocket: a pocket with no Site is free."""
    from beanie import PydanticObjectId

    from pocketpaw_ee.cloud.models.pocket import Pocket
    from pocketpaw_ee.sites import service as sites_service

    doc = await Pocket.get(PydanticObjectId(pocket_id))
    workspace_id = str(getattr(doc, "workspace", "") or "") if doc is not None else ""
    site = (
        await sites_service._canonical_site_doc(workspace_id, pocket_id) if workspace_id else None
    )
    return workspace_id, bool(site is not None and sites_service._site_paid(site))


async def _default_secrets(workspace_id: str, pocket_id: str) -> dict[str, str]:
    from pocketpaw_ee.sites import site_secrets

    return await site_secrets.secrets_for_deploy(
        SimpleNamespace(workspace=workspace_id, pocket_id=pocket_id)
    )


def _signing_value(rec: DraftRecord) -> str:
    """The draft's own session-signing secret, kept encrypted on the row so draft
    sessions survive redeploys. Without an encryption key it is minted per deploy."""
    from pocketpaw_ee.cloud._core import crypto

    if rec.auth_secret_enc and crypto.is_configured():
        try:
            return crypto.decrypt(rec.auth_secret_enc)
        except Exception:  # noqa: BLE001 - a rotated key mints a new value
            logger.info("sites.draft: pocket %s draft signing secret re-minted", rec.pocket_id)
    value = secrets.token_urlsafe(32)
    rec.auth_secret_enc = crypto.encrypt(value) if crypto.is_configured() else ""
    return value


def _draft_key(rec: DraftRecord) -> str:
    """The per-draft guard key, stored encrypted like the signing secret. Needs an
    encryption key (the proxy must read it back); the caller checks that first."""
    from pocketpaw_ee.cloud._core import crypto

    key = _decrypt(rec.draft_key_enc)
    if not key:
        key = secrets.token_urlsafe(32)
        rec.draft_key_enc = crypto.encrypt(key)
    return key


def seeds_from_source(source: Mapping[str, Any] | None) -> list[str]:
    """Statements of the top-level ``seed/*.sql`` files, in filename order."""
    from pocketpaw_ee.sites import project_d1

    out: list[str] = []
    for name in sorted((source or {}).keys()):
        parts = name.split("/")
        if len(parts) == 2 and parts[0] == SEED_DIR and parts[1].endswith(".sql"):
            text = source[name] if isinstance(source[name], str) else ""  # type: ignore[index]
            out.extend(project_d1.split_statements(text))
    return out


def _reason_for(exc: Exception, phase: str) -> str:
    code = getattr(exc, "code", "") or ""
    if code == "sites.secrets_missing":
        return "secrets_missing"
    if code in {
        "sites.binding_not_entitled",
        "sites.server_code_not_entitled",
        "sites.binding_unsupported",
        "sites.binding_cap",
    }:
        return "not_entitled"
    if code in {"sites.do_account_budget", "sites.do_budget_unknown"}:
        return "cap"
    if code in {"sites.do_disabled", "sites.do_class_cap"}:
        return "not_entitled"
    if code.startswith("sites.migration_"):
        return "migration_failed"
    return {"provision": "provision_failed", "migrate": "migration_failed"}.get(
        phase, "deploy_failed"
    )


async def deploy_for_build(
    *,
    pocket_id: str,
    content_hash: str,
    artifact: bytes,
    manifest: Mapping[str, Any],
    source: Mapping[str, Any] | None,
    store: Any,
    cf: Any = None,
    registry: Any = None,
    context: Callable[[str], Awaitable[tuple[str, bool]]] | None = None,
    secrets_reader: Callable[[str, str], Awaitable[dict[str, str]]] | None = None,
    alive: Callable[[str, str], Awaitable[bool]] | None = None,
) -> DraftOutcome:
    """Deploy this build as the pocket's draft Worker. ``DraftOutcome("full", None)``
    when it serves; ``(None, rung)`` when the caller must fall back; ``(None, None)``
    when the feature is off (nothing changes)."""
    if not enabled():
        return DraftOutcome(None, None)
    from pocketpaw_ee.sites import (
        binding_provisioner,
        bundle_deploy,
        durable_objects,
        preview_origin,
        project_build,
        project_d1,
    )
    from pocketpaw_ee.sites import service as sites_service

    target = bundle_deploy.project_deploy_target(os.environ.get("PAW_CF_DEPLOY_MODE"))
    if target != bundle_deploy.ACCOUNT_TARGET:
        return _skip("disabled")
    if (
        preview_origin.preview_base_problem() is not None
        or not preview_origin.store_supports_preview(store)
    ):
        return _skip("disabled")
    registry = registry or default_registry()
    try:
        cf = cf or sites_service._cf_client()
    except Exception:  # noqa: BLE001 - Cloudflare not configured
        return _skip("disabled")

    from pocketpaw_ee.cloud._core import crypto

    if not crypto.is_configured():
        # The guard key must be readable by every proxy process.
        logger.warning("sites.draft: CLOUD_ENCRYPTION_KEY is not set; draft Workers are off")
        return _skip("disabled")

    rec = await registry.get(pocket_id)
    if rec is not None and rec.state == "deleting":
        return _skip("cleanup_pending")
    is_new = rec is None or not rec.script or rec.state != "live"
    workspace_id, paid = await (context or _default_context)(pocket_id)
    try:
        project_build.check_plan_allows(manifest, paid=paid, has_custom_domain=False)
    except ValidationError:
        return _skip("not_entitled")
    owner = await (secrets_reader or _default_secrets)(workspace_id, pocket_id)
    bound = draft_secrets(owner, signing_value="-")
    missing = [n for n in required_secrets(manifest) if n not in bound]
    if missing:
        return DraftOutcome(None, "draft_worker:secrets_missing", tuple(missing))
    if is_new and not await _reserve_slot(cf):
        return _skip("cap")

    expected: dict[str, int | None] = {"v": rec.version if rec is not None else None}
    if is_new:
        prior = rec
        rec = DraftRecord(
            pocket_id=pocket_id,
            script=new_script_name(pocket_id),
            published_hash=prior.published_hash if prior else "",
            published_url=prior.published_url if prior else "",
            version=prior.version if prior else 0,
        )
    assert rec is not None
    rec.workspace = workspace_id or rec.workspace
    phase = {"v": "provision"}

    async def _write() -> None:
        # Compare-and-set: a purge (or a newer write) since our last write wins.
        if not await registry.cas(rec, expected["v"]):
            raise _Superseded
        expected["v"] = rec.version

    db_name = draft_database_name(pocket_id)
    reason: str | None = None
    host = ""
    try:
        token = preview_origin._mint_token(store, pocket_id, content_hash)
        if not token:
            raise RuntimeError("the preview token could not be stored")
        url = (preview_origin.preview_url_for(token) or "").rstrip("/")
        await _write()

        async def _save(t: Any) -> None:
            rec.d1_database_id = t.d1_database_id
            rec.kv_namespaces = dict(t.kv_namespaces)
            rec.r2_buckets = dict(t.r2_buckets)
            await _write()

        async def _provision(requests: Any) -> Any:
            phase["v"] = "provision"
            holder = SimpleNamespace(
                id=draft_resource_id(pocket_id),
                d1_database_id=rec.d1_database_id,
                kv_namespaces=dict(rec.kv_namespaces),
                r2_buckets=dict(rec.r2_buckets),
            )
            res = await binding_provisioner.ensure_bindings(
                holder, requests, cloudflare=cf, save=_save, paid=paid, d1_name=db_name
            )
            signing = _signing_value(rec)
            key = _draft_key(rec)
            await _write()
            draft = draft_secrets(owner, signing_value=signing)
            draft[DRAFT_KEY_BINDING] = key
            return replace(
                res,
                secrets=draft,
                plain_text={name: url for name in PLATFORM_URL_VARS},
            )

        async def _migrate(bindings: list[dict]) -> None:
            phase["v"] = "migrate"
            d1 = next((b for b in bindings if b.get("type") == "d1"), None)
            if d1 is not None:
                migrations = project_d1.migrations_from_source(source)
                try:
                    await project_d1.apply_migrations(
                        cf, d1["id"], migrations, confirm_destructive=True
                    )
                except ValidationError as exc:
                    if exc.code != "sites.migration_changed":
                        raise
                    # Drafts are disposable: start the database over instead of refusing.
                    logger.info(
                        "sites.draft: pocket %s draft DB reset (migration changed)", pocket_id
                    )
                    await cf.delete_database(d1["id"])
                    rec.d1_database_id, rec.seeded = "", False
                    await _write()
                    rec.d1_database_id = await cf.create_database(db_name)
                    await _write()
                    d1["id"] = rec.d1_database_id
                    await project_d1.apply_migrations(
                        cf, d1["id"], migrations, confirm_destructive=True
                    )
                if not rec.seeded:
                    seeds = seeds_from_source(source)
                    if seeds:
                        await cf.query_d1_batch(
                            database_id=d1["id"], statements=[(s, []) for s in seeds]
                        )
                    rec.seeded = True
                    await _write()
            phase["v"] = "deploy"
            # Last look before anything is uploaded: the pocket (and its site) must
            # still exist, and no purge may have touched the row.
            if not await (alive or _default_alive)(pocket_id, workspace_id):
                raise _Gone
            await _write()

        do_state = durable_objects.DurableObjectState.from_history(
            rec.do_migration_tags, rec.do_classes
        )
        if durable_objects.declares_durable_objects(manifest) or rec.do_classes:
            try:
                durable_objects.vet_durable_objects(
                    manifest, paid=paid, state=do_state, allow_data_loss=True
                )
            except ValidationError as exc:
                if exc.code != "sites.do_history_diverged":
                    raise
                # Draft data is throwaway: start a fresh script (fresh namespaces)
                # instead of refusing, like a changed D1 migration resets the draft DB.
                await _rotate_script(rec, cf, do_state)
                await _write()
                do_state = durable_objects.DurableObjectState()

        with tempfile.TemporaryDirectory(prefix="paw-draft-") as work:
            project_build.materialize_bundle(artifact, Path(work))
            result = await bundle_deploy.deploy_bundle(
                cf,
                script_name=rec.script,
                build_dir=work,
                salt=workspace_id or pocket_id,
                provision=_provision,
                before_upload=_migrate,
                target=bundle_deploy.ACCOUNT_TARGET,
                paid=paid,
                draft=True,
                main_wrapper=guard_module,
                do_state=do_state,
                allow_do_data_loss=True,
            )
        if result.migration_tags or rec.do_classes:
            # Right after the upload, so a later failure cannot lose the applied tag.
            rec.do_migration_tags = list(result.migration_tags)
            rec.do_classes = list(result.do_classes)
            await _write()
        phase["v"] = "deploy"
        await cf.enable_workers_dev(rec.script)
        from pocketpaw_ee.sites.workers_deploy import _workers_dev_host

        host = _workers_dev_host(rec.script)
        if not host:
            sub = await cf.workers_dev_subdomain()
            host = f"{rec.script}.{sub}.workers.dev" if sub else ""
        if not host:
            raise RuntimeError("the account has no workers.dev subdomain")
        rec.host = host
        rec.deployed_hash = content_hash
        rec.deployed_at = datetime.now(UTC)
        rec.state = "live"
        await _write()
    except _Superseded:
        reason = "superseded"
    except _Gone:
        reason = "gone"
    except Exception as exc:  # noqa: BLE001 - every failure is a fallback rung
        reason = _reason_for(exc, phase["v"])
        # Cloudflare and our own messages name resources, never secret values.
        logger.warning(
            "sites.draft: pocket %s draft deploy failed (%s): %s", pocket_id, reason, exc
        )

    _invalidate(pocket_id)
    if reason is None:
        logger.info(
            "sites.draft: pocket %s draft %s runs %s", pocket_id, rec.script, content_hash[:12]
        )
        return DraftOutcome("full", None)
    if is_new:
        _release_slot()
    if reason == "superseded":
        # A purge (publish, delete) landed mid-deploy and owns the row now: remove
        # what this deploy created that the row no longer names.
        logger.info("sites.draft: pocket %s draft was purged mid-deploy; discarding", pocket_id)
        await _discard(rec, await registry.get(pocket_id), cf)
    elif reason == "gone":
        logger.info("sites.draft: pocket %s is gone; removing its draft", pocket_id)
        await purge_pocket_drafts(
            pocket_id, reason="gone", forget=True, cf=cf, registry=registry, store=store
        )
    else:
        rec.deployed_hash = ""
        try:
            await _write()
        except _Superseded:
            await _discard(rec, await registry.get(pocket_id), cf)
    return _skip(reason)


async def _rotate_script(rec: DraftRecord, cf: Any, state: Any) -> None:
    """Point ``rec`` at a fresh script name after tearing the old script's Durable
    Objects down. A teardown that fails is logged; the orphan sweep force-deletes
    the old ``paw-draft-*`` script once no row names it."""
    from pocketpaw_ee.sites import durable_objects

    old = rec.script
    if old:
        out = await durable_objects.teardown_script(
            cf,
            old,
            target=durable_objects.ACCOUNT_TARGET,
            classes=state.live_classes,
            migration_tag=state.migration_tag,
        )
        if not out.ok:
            logger.warning(
                "sites.draft: pocket %s old draft %s teardown incomplete: %s",
                rec.pocket_id,
                old,
                out.error,
            )
    logger.info("sites.draft: pocket %s DO history changed; rotating the draft", rec.pocket_id)
    rec.script = new_script_name(rec.pocket_id)
    rec.host, rec.deployed_hash = "", ""
    rec.do_migration_tags, rec.do_classes = [], []


# ---------------------------------------------------------------- cleanup


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _backoff(attempts: int) -> timedelta:
    return timedelta(minutes=min(5 * 2 ** max(attempts - 1, 0), 24 * 60))


def _purge_previews(store: Any, pocket_id: str) -> bool:
    from pocketpaw_ee.sites import preview_origin

    ok = True
    purge = getattr(store, "purge_previews", None)
    if callable(purge):
        try:
            ok = purge(pocket_id) is not False
        except Exception:  # noqa: BLE001 - best effort, the sweeper re-runs it
            logger.warning("sites.draft: pocket %s preview purge failed", pocket_id, exc_info=True)
            ok = False
    preview_origin.forget_pocket(pocket_id)
    return ok


async def purge_pocket_drafts(
    pocket_id: str,
    *,
    workspace_id: str = "",
    reason: str,
    published_hash: str = "",
    published_url: str = "",
    forget: bool = False,
    cf: Any = None,
    registry: Any = None,
    store: Any = None,
) -> bool:
    """Delete every draft of a pocket: its preview tokens and files, its draft
    Worker and its draft-only D1 / KV / R2. True when nothing is left. ``forget``
    drops the row too (the site or pocket is gone); otherwise the row stays
    ``purged`` and remembers what was published."""
    registry = registry or default_registry()
    if store is None:
        from pocketpaw_ee.sites import service as sites_service

        store = sites_service._default_artifact_store()
    rec = await registry.get(pocket_id)
    if rec is not None:
        if published_hash or published_url:
            rec.published_hash, rec.published_url = published_hash, published_url
        rec.state, rec.deployed_hash = "deleting", ""
        await registry.put(rec)
    _invalidate(pocket_id)
    previews_ok = _purge_previews(store, pocket_id)
    if rec is None and not previews_ok:
        # Keep a row so the sweeper retries the preview purge.
        rec = DraftRecord(pocket_id=pocket_id, workspace=workspace_id, state="deleting")
        rec.published_hash, rec.published_url = published_hash, published_url
        await registry.put(rec)
    if rec is None:
        if (published_hash or published_url) and not forget:
            await registry.put(
                DraftRecord(
                    pocket_id=pocket_id,
                    workspace=workspace_id,
                    state="purged",
                    published_hash=published_hash,
                    published_url=published_url,
                )
            )
        return True

    errors: list[str] = [] if previews_ok else ["preview_purge: incomplete"]
    owns_resources = rec.script or rec.d1_database_id or rec.kv_namespaces or rec.r2_buckets
    if owns_resources and cf is None:
        try:
            from pocketpaw_ee.sites import service as sites_service

            cf = sites_service._cf_client()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"cloudflare: {exc}")
    if owns_resources and cf is not None:
        steps: list[tuple[str, Callable[[], Awaitable[Any]], Callable[[], None]]] = []
        if rec.script and rec.do_classes:

            async def _teardown() -> None:
                from pocketpaw_ee.sites import durable_objects

                out = await durable_objects.teardown_script(
                    cf,
                    rec.script,
                    target=durable_objects.ACCOUNT_TARGET,
                    classes=rec.do_classes,
                    migration_tag=rec.do_migration_tags[-1] if rec.do_migration_tags else None,
                )
                if not out.ok:
                    raise RuntimeError(out.error)

            def _torn_down() -> None:
                rec.script, rec.do_migration_tags, rec.do_classes = "", [], []

            steps.append(("teardown_durable_objects", _teardown, _torn_down))
        elif rec.script:
            steps.append(
                (
                    "delete_account_script",
                    lambda: cf.delete_account_script(rec.script),
                    lambda: setattr(rec, "script", ""),
                )
            )
        if rec.d1_database_id:
            steps.append(
                (
                    "delete_database",
                    lambda: cf.delete_database(rec.d1_database_id),
                    lambda: setattr(rec, "d1_database_id", ""),
                )
            )
        for name, ns in list(rec.kv_namespaces.items()):
            steps.append(
                (
                    "delete_kv_namespace",
                    lambda ns=ns: cf.delete_kv_namespace(ns),
                    lambda name=name: rec.kv_namespaces.pop(name, None),
                )
            )
        for name, bucket in list(rec.r2_buckets.items()):

            async def _r2(bucket: str = bucket) -> None:
                if not await cf.delete_r2_bucket(bucket):
                    await cf.expire_r2_bucket_objects(bucket)
                    raise RuntimeError(f"r2 bucket {bucket} is not empty yet")

            steps.append(
                ("delete_r2_bucket", _r2, lambda name=name: rec.r2_buckets.pop(name, None))
            )
        for label, run, done in steps:
            try:
                await run()
                done()
            except Exception as exc:  # noqa: BLE001 - best effort, retried by the sweeper
                errors.append(f"{label}: {exc}")
        await registry.put(rec)

    if errors:
        rec.attempts += 1
        rec.last_error = "; ".join(errors)[:500]
        rec.retry_after = datetime.now(UTC) + _backoff(rec.attempts)
        await registry.put(rec)
        logger.warning(
            "sites.draft: pocket %s draft cleanup (%s) incomplete, will retry: %s",
            pocket_id,
            reason,
            rec.last_error,
        )
        return False
    if forget:
        await registry.delete(pocket_id)
    else:
        rec.state, rec.script, rec.host, rec.seeded = "purged", "", "", False
        rec.auth_secret_enc, rec.attempts, rec.last_error, rec.retry_after = "", 0, "", None
        rec.draft_key_enc = ""
        await registry.put(rec)
    logger.info("sites.draft: pocket %s drafts purged (%s)", pocket_id, reason)
    return True


_tasks: set[asyncio.Task] = set()


def schedule_purge(pocket_id: str, **kwargs: Any) -> None:
    """Run ``purge_pocket_drafts`` in the background. Its first await marks the row
    ``deleting``, so nothing proxies to the draft from then on; anything it cannot
    finish is retried by ``sweep_draft_workers``."""

    async def _run() -> None:
        try:
            await purge_pocket_drafts(pocket_id, **kwargs)
        except Exception:  # noqa: BLE001 - the sweeper retries
            logger.warning("sites.draft: pocket %s purge task failed", pocket_id, exc_info=True)

    task = asyncio.get_running_loop().create_task(_run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def sweep_draft_workers(
    *, now: datetime | None = None, cf: Any = None, registry: Any = None, store: Any = None
) -> dict[str, int]:
    """Retry pending cleanups, reap drafts idle past the TTL, delete ``paw-draft-*``
    scripts no row knows. Runs in the cluster sweep loop (``extensions._sweeps``)."""
    out = {"purged": 0, "orphans": 0, "failed": 0}
    if not enabled():
        return out
    registry = registry or default_registry()
    now = now or datetime.now(UTC)
    idle_before = now - timedelta(days=_int_env(TTL_DAYS_ENV, DEFAULT_TTL_DAYS))
    for rec in await registry.all():
        retry_at = _aware(rec.retry_after)
        due = rec.state == "deleting" and (retry_at is None or retry_at <= now)
        deployed = _aware(rec.deployed_at)
        idle = (
            rec.state == "live"
            and bool(rec.script)
            and deployed is not None
            and deployed < idle_before
        )
        if not (due or idle):
            continue
        ok = await purge_pocket_drafts(
            rec.pocket_id, reason="idle" if idle else "retry", cf=cf, registry=registry, store=store
        )
        out["purged" if ok else "failed"] += 1

    try:
        if cf is None:
            from pocketpaw_ee.sites import service as sites_service

            cf = sites_service._cf_client()
        scripts = await cf.list_account_scripts()
    except Exception as exc:  # noqa: BLE001 - try again next tick
        logger.info("sites.draft: orphan sweep skipped (%s)", exc)
        return out
    known = {r.script for r in await registry.all() if r.script}
    for name in scripts:
        if pocket_of_script(name) is None or name in known:
            continue
        try:
            # Forced: an orphan may hold Durable Object namespaces (a rotated draft).
            await cf.delete_account_script(name, force=True)
            out["orphans"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("sites.draft: orphan script %s delete failed: %s", name, exc)
            out["failed"] += 1
    return out


__all__ = [
    "DraftOutcome",
    "DraftRecord",
    "MemoryRegistry",
    "MongoRegistry",
    "ProxyTarget",
    "SUPERSEDED",
    "default_registry",
    "deploy_for_build",
    "draft_secrets",
    "enabled",
    "new_script_name",
    "pocket_of_script",
    "proxy_target",
    "published_view",
    "purge_pocket_drafts",
    "schedule_purge",
    "sweep_draft_workers",
]
