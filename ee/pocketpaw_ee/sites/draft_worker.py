# ee/pocketpaw_ee/sites/draft_worker.py: the account-level draft Worker of a
# ``project`` pocket, so a draft preview runs the site's real server code.
#
# Behind ``PAW_SITES_DRAFT_WORKERS=1`` and only on the ``account`` project deploy
# target (``bundle_deploy.project_deploy_target``). Design and rationale:
# docs/design/drafts/2026-10-08-sites-draft-worker.md (workspace root).
#
# What this module owns:
#   * naming: ``paw-draft-<pocket id>-<16 hex>``. ``paw-`` is reserved from user
#     slugs (``slug.has_reserved_prefix``) and published scripts are a slug or
#     ``paw-site-<id>``, so names never collide. The random tail keeps the
#     workers.dev host unguessable and rotates on every purge.
#   * the registry (``SiteDraftWorker`` rows; ``MemoryRegistry`` for tests): the
#     script, its host, the content hash it runs, and its draft-only D1 / KV / R2.
#   * ``deploy_for_build``: called by the preview build after the bundle is stored.
#     It provisions draft-only resources, applies migrations (destructive allowed,
#     a changed applied migration recreates the draft DB) and ``seed/*.sql`` once,
#     binds owner secrets with ``NAME__DRAFT`` overrides and per-draft signing
#     secrets, sets ``BETTER_AUTH_URL`` / ``PAW_SITE_URL`` to the preview origin, and
#     deploys. Any failure returns a rung and the build falls back; it never fails.
#   * ``proxy_target``: which token the preview origin proxies (only the deployed
#     hash of a ``live`` row; an older hash of a proxied draft is ``SUPERSEDED``).
#   * cleanup: ``purge_pocket_drafts`` (publish, site / pocket delete, sweeper).
#     Idempotent; a Cloudflare 404 is success. A failure leaves the row
#     ``deleting`` with a backoff and ``sweep_draft_workers`` retries it, reaps
#     drafts idle past ``PAW_SITES_DRAFT_TTL_DAYS`` and deletes unregistered
#     ``paw-draft-*`` scripts.
#   * the script cap: a NEW draft script is refused at ``PAW_SITES_DRAFT_SCRIPT_CAP``
#     account scripts, or when the count cannot be read (fail closed).
#
# Invariant: secret VALUES never appear in a log line, an error, a rung or a record
# field other than the Fernet-encrypted ``auth_secret_enc``.
from __future__ import annotations

import asyncio
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

SCRIPT_PREFIX = "paw-draft-"
_SCRIPT_RE = re.compile(r"^paw-draft-([a-z0-9]+)-[a-f0-9]{16}$")

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


def new_script_name(pocket_id: str) -> str:
    return f"{SCRIPT_PREFIX}{_pid(pocket_id)}-{secrets.token_hex(8)}"


def pocket_of_script(name: str) -> str | None:
    m = _SCRIPT_RE.match(name or "")
    return m.group(1) if m else None


def draft_database_name(pocket_id: str) -> str:
    return f"{SCRIPT_PREFIX}{_pid(pocket_id)}"


def draft_resource_id(pocket_id: str) -> str:
    """The ``site_id`` ``binding_provisioner.resource_name`` names draft KV / R2 by:
    ``paw-draft<pid>-...`` can never equal a published site's ``paw-<24 hex>-...``."""
    return f"draft{_pid(pocket_id)}"


def draft_secrets(owner: Mapping[str, str], *, signing_value: str) -> dict[str, str]:
    """The secrets a draft binds: owner values, a ``NAME__DRAFT`` replacing ``NAME``,
    and every session-signing secret replaced with the draft's own value."""
    out = {k: v for k, v in owner.items() if not k.endswith(DRAFT_SUFFIX)}
    for name, value in owner.items():
        if name.endswith(DRAFT_SUFFIX) and len(name) > len(DRAFT_SUFFIX):
            out[name[: -len(DRAFT_SUFFIX)]] = value
    for name in SIGNING_SECRETS:
        out[name] = signing_value
    return out


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
    seeded: bool = False
    auth_secret_enc: str = field(default="", repr=False)
    state: str = "live"
    attempts: int = 0
    last_error: str = ""
    retry_after: datetime | None = None
    published_hash: str = ""
    published_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_FIELDS = tuple(DraftRecord.__dataclass_fields__)


class MemoryRegistry:
    """In-memory registry (tests, and a stand-in where Mongo is absent)."""

    def __init__(self) -> None:
        self.rows: dict[str, DraftRecord] = {}

    async def get(self, pocket_id: str) -> DraftRecord | None:
        row = self.rows.get(pocket_id)
        return (
            replace(row, kv_namespaces=dict(row.kv_namespaces), r2_buckets=dict(row.r2_buckets))
            if row
            else None
        )

    async def put(self, rec: DraftRecord) -> None:
        self.rows[rec.pocket_id] = replace(
            rec, kv_namespaces=dict(rec.kv_namespaces), r2_buckets=dict(rec.r2_buckets)
        )

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
        from pymongo.errors import DuplicateKeyError

        model = self._model()
        data = rec.to_dict()
        doc = await model.find_one({"pocket_id": rec.pocket_id})
        if doc is None:
            try:
                await model(**data).insert()
                return
            except DuplicateKeyError:
                doc = await model.find_one({"pocket_id": rec.pocket_id})
                if doc is None:
                    raise
        await doc.set(data)

    async def delete(self, pocket_id: str) -> None:
        await self._model().find({"pocket_id": pocket_id}).delete()

    async def all(self) -> list[DraftRecord]:
        return [self._rec(d) for d in await self._model().find({}).to_list()]


def default_registry() -> Any:
    return MongoRegistry()


# ---------------------------------------------------------------- caches


_target_cache: dict[str, tuple[float, DraftRecord | None]] = {}
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


#: An older content hash of a pocket whose draft Worker now runs newer code: a 404.
SUPERSEDED = object()


async def proxy_target(pocket_id: str, content_hash: str, *, registry: Any = None) -> Any:
    """The draft Worker a token of ``(pocket_id, content_hash)`` proxies to,
    ``SUPERSEDED`` for an older hash of a proxied draft, else ``None`` (static)."""
    now = time.monotonic()
    hit = _target_cache.get(pocket_id)
    if hit is not None and hit[0] > now:
        rec = hit[1]
    else:
        rec = await (registry or default_registry()).get(pocket_id)
        _target_cache[pocket_id] = (now + _TARGET_TTL, rec)
    if rec is None or rec.state != "live" or not rec.host or not rec.deployed_hash:
        return None
    if rec.deployed_hash == content_hash:
        return ProxyTarget(pocket_id=pocket_id, host=rec.host)
    return SUPERSEDED


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


def _skip(reason: str) -> DraftOutcome:
    return DraftOutcome(None, f"draft_worker:{reason}")


async def _under_cap(cf: Any) -> bool:
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
    return True


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
) -> DraftOutcome:
    """Deploy this build as the pocket's draft Worker. ``DraftOutcome("full", None)``
    when it serves; ``(None, rung)`` when the caller must fall back; ``(None, None)``
    when the feature is off (nothing changes)."""
    if not enabled():
        return DraftOutcome(None, None)
    from pocketpaw_ee.sites import (
        binding_provisioner,
        bundle_deploy,
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

    rec = await registry.get(pocket_id)
    if rec is not None and rec.state == "deleting":
        return _skip("cleanup_pending")
    is_new = rec is None or not rec.script or rec.state != "live"
    if is_new and not await _under_cap(cf):
        return _skip("cap")
    workspace_id, paid = await (context or _default_context)(pocket_id)
    try:
        project_build.check_plan_allows(manifest, paid=paid, has_custom_domain=False)
    except ValidationError:
        return _skip("not_entitled")
    if is_new:
        prior = rec
        rec = DraftRecord(
            pocket_id=pocket_id,
            script=new_script_name(pocket_id),
            published_hash=prior.published_hash if prior else "",
            published_url=prior.published_url if prior else "",
        )
    assert rec is not None
    rec.workspace = workspace_id or rec.workspace
    token = preview_origin._mint_token(store, pocket_id, content_hash)
    if not token:
        return _skip("deploy_failed")
    url = (preview_origin.preview_url_for(token) or "").rstrip("/")
    await registry.put(rec)

    phase = {"v": "provision"}
    reader = secrets_reader or _default_secrets
    db_name = draft_database_name(pocket_id)

    async def _save(t: Any) -> None:
        rec.d1_database_id = t.d1_database_id
        rec.kv_namespaces = dict(t.kv_namespaces)
        rec.r2_buckets = dict(t.r2_buckets)
        await registry.put(rec)

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
        await registry.put(rec)
        owner = await reader(workspace_id, pocket_id)
        return replace(
            res,
            secrets=draft_secrets(owner, signing_value=signing),
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
                logger.info("sites.draft: pocket %s draft DB reset (migration changed)", pocket_id)
                await cf.delete_database(d1["id"])
                rec.d1_database_id, rec.seeded = "", False
                await registry.put(rec)
                rec.d1_database_id = await cf.create_database(db_name)
                await registry.put(rec)
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
                await registry.put(rec)
        phase["v"] = "deploy"

    reason: str | None = None
    host = ""
    try:
        with tempfile.TemporaryDirectory(prefix="paw-draft-") as work:
            project_build.materialize_bundle(artifact, Path(work))
            await bundle_deploy.deploy_bundle(
                cf,
                script_name=rec.script,
                build_dir=work,
                salt=workspace_id or pocket_id,
                provision=_provision,
                before_upload=_migrate,
                target=bundle_deploy.ACCOUNT_TARGET,
                paid=paid,
                draft=True,
            )
        phase["v"] = "deploy"
        await cf.enable_workers_dev(rec.script)
        from pocketpaw_ee.sites.workers_deploy import _workers_dev_host

        host = _workers_dev_host(rec.script)
        if not host:
            sub = await cf.workers_dev_subdomain()
            host = f"{rec.script}.{sub}.workers.dev" if sub else ""
        if not host:
            raise RuntimeError("the account has no workers.dev subdomain")
    except Exception as exc:  # noqa: BLE001 - every failure is a fallback rung
        reason = _reason_for(exc, phase["v"])
        # Cloudflare and our own messages name resources, never secret values.
        logger.warning(
            "sites.draft: pocket %s draft deploy failed (%s): %s", pocket_id, reason, exc
        )

    if reason is not None:
        rec.deployed_hash = ""
        await registry.put(rec)
        _invalidate(pocket_id)
        return _skip(reason)
    rec.host = host
    rec.deployed_hash = content_hash
    rec.deployed_at = datetime.now(UTC)
    rec.state = "live"
    await registry.put(rec)
    _invalidate(pocket_id)
    if is_new:
        _count_cache[1] += 1
    logger.info("sites.draft: pocket %s draft %s runs %s", pocket_id, rec.script, content_hash[:12])
    return DraftOutcome("full", None)


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
        if rec.script:
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
            await cf.delete_account_script(name)
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
