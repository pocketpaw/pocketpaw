# ee/pocketpaw_ee/sites/binding_provisioner.py: create (and tear down) the per-site
# Cloudflare resources a bundle deploy binds, so a ``paw-build.json`` that asks for
# ``kv`` or ``r2`` gets a namespace / bucket of its own instead of a refusal.
#
# ``ensure_bindings(site, requests)`` returns the ``ProvisionedResources`` that
# ``bundle_deploy.map_bindings`` maps onto the upload. Rules:
#   * Only ``type`` and ``name`` are read from a request. Resource ids come from
#     Cloudflare and are recorded on the Site doc (``kv_namespaces``,
#     ``r2_buckets``, keyed by binding name), never from the build.
#   * Idempotent: a recorded resource is reused with no Cloudflare call; an
#     unrecorded one is looked up by its derived name first (a create that died
#     before the save), then created. The doc is saved after every create.
#   * Plan gating (captain, 2026-10-07): d1 and kv are allowed on free, kv capped
#     tighter there; r2 needs the ``site`` tier or above with an active
#     subscription (``entitlements.site_paid_backends_entitled``). do, queues and
#     ai are refused as not supported yet. Every check runs before the first create.
#   * Caps per site are env-configurable (see ``_cap``). Cloudflare allows 1,000 KV
#     namespaces per ACCOUNT, so KV per site is the scarce one.
#   * d1 is not provisioned here: it stays the site's own D1 (``d1_database_id``).
#
# ``teardown_bindings`` is the delete cascade's ``bindings`` step: best effort,
# per resource, logging each failure; a bucket that still holds objects gets an
# expire-everything lifecycle rule and is reported, since the REST API cannot empty it.
from __future__ import annotations

import hashlib
import logging
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites.bundle_deploy import _BINDING_NAME, ProvisionedResources

logger = logging.getLogger(__name__)

KV = "kv"
R2 = "r2"
_NOT_SUPPORTED = frozenset({"do", "queues", "ai"})

# R2 bucket names: 3-63 chars, lowercase letters, digits and hyphens, no leading or
# trailing hyphen (https://developers.cloudflare.com/r2/buckets/create-buckets/).
# KV titles allow up to 512 chars; one name for both keeps the dashboard readable.
_MAX_NAME = 63

_CAP_ENV = {
    (KV, False): ("PAW_SITES_MAX_KV_NAMESPACES_FREE", 1),
    (KV, True): ("PAW_SITES_MAX_KV_NAMESPACES", 3),
    (R2, True): ("PAW_SITES_MAX_R2_BUCKETS", 3),
}


def _cap(kind: str, paid: bool) -> int:
    env, default = _CAP_ENV[(kind, paid)]
    raw = os.environ.get(env, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        logger.warning("%s=%r is not an int; using %d", env, raw, default)
        return default
    return max(value, 0)


def resource_name(site_id: str, binding: str) -> str:
    """``paw-<siteid>-<binding slug>-<6 hex>``: deterministic, lowercase, and a
    valid R2 bucket name. The hash of the exact binding name keeps ``MY_KV`` and
    ``my_kv`` (same slug) apart; the slug is cut to fit 63 chars."""
    sid = re.sub(r"[^a-z0-9]", "", str(site_id).lower())
    digest = hashlib.sha256(binding.encode("utf-8")).hexdigest()[:6]
    room = _MAX_NAME - len(f"paw-{sid}--{digest}")
    slug = re.sub(r"[^a-z0-9]+", "-", binding.lower())[: max(room, 0)].strip("-")
    return f"paw-{sid}-{slug}-{digest}" if slug else f"paw-{sid}-{digest}"


def _refuse(code: str, message: str) -> ValidationError:
    return ValidationError(code, message)


def _requested(binding_requests: Any) -> dict[str, list[str]]:
    """``{kind: [names]}`` for kv / r2, after refusing what cannot be provisioned.
    Malformed and non-backend requests are left to ``map_bindings``."""
    wanted: dict[str, list[str]] = {KV: [], R2: []}
    for req in binding_requests if isinstance(binding_requests, list) else []:
        if not isinstance(req, dict):
            continue
        kind = str(req.get("type", "")).strip().lower()
        name = req.get("name")
        if kind in _NOT_SUPPORTED:
            raise _refuse(
                "sites.binding_unsupported",
                f"The {kind} binding {name!r} is not supported on Paw Sites yet. "
                "Remove it from the build to deploy.",
            )
        if kind not in wanted:
            continue
        if not isinstance(name, str) or not _BINDING_NAME.match(name):
            raise _refuse(
                "sites.bundle_invalid", f"binding name {name!r} is not a valid identifier"
            )
        if name not in wanted[kind]:
            wanted[kind].append(name)
    return wanted


def _check_gates(site: Any, wanted: dict[str, list[str]], paid: bool) -> None:
    if wanted[R2] and not paid:
        raise _refuse(
            "sites.binding_not_entitled",
            "R2 file storage needs the Site plan or above. Upgrade this site, or remove "
            f"the r2 binding{'s' if len(wanted[R2]) > 1 else ''} "
            f"({', '.join(wanted[R2])}) from the build.",
        )
    for kind, field in ((KV, "kv_namespaces"), (R2, "r2_buckets")):
        if not wanted[kind]:
            continue
        cap = _cap(kind, paid)
        have = dict(getattr(site, field, None) or {})
        total = len(set(have) | set(wanted[kind]))
        if total > cap:
            label = "KV namespaces" if kind == KV else "R2 buckets"
            hint = " Upgrade to the Site plan for more." if kind == KV and not paid else ""
            raise _refuse(
                "sites.binding_cap",
                f"This site can have at most {cap} {label}; the build would make it "
                f"{total} ({', '.join(sorted(set(have) | set(wanted[kind])))}).{hint}",
            )
    names = [resource_name(str(site.id), n) for k in (KV, R2) for n in wanted[k]]
    if len(names) != len(set(names)):
        raise _refuse("sites.bundle_invalid", "two binding names map to the same resource name")


async def ensure_bindings(
    site: Any,
    binding_requests: Any,
    *,
    cloudflare: Any,
    save: Callable[[Any], Awaitable[None]],
    paid: bool,
) -> ProvisionedResources:
    """Create the site's missing KV namespaces / R2 buckets and return everything
    ``map_bindings`` needs. ``paid`` is the site's
    ``entitlements.site_paid_backends_entitled`` answer. ``save(site)`` persists the
    two resource maps; it runs after each create."""
    wanted = _requested(binding_requests)
    _check_gates(site, wanted, paid)

    kv = dict(getattr(site, "kv_namespaces", None) or {})
    r2 = dict(getattr(site, "r2_buckets", None) or {})
    for name in wanted[KV]:
        if kv.get(name):
            continue
        title = resource_name(str(site.id), name)
        ns_id = await cloudflare.find_kv_namespace(title)
        if not ns_id:
            try:
                ns_id = await cloudflare.create_kv_namespace(title)
            except ValidationError:
                # A concurrent deploy may have created it between our find and create.
                ns_id = await cloudflare.find_kv_namespace(title)
                if not ns_id:
                    raise
        kv[name] = ns_id
        site.kv_namespaces = dict(kv)
        await save(site)
        logger.info("sites.bindings: site %s kv %s -> %s", site.id, name, ns_id)
    for name in wanted[R2]:
        if r2.get(name):
            continue
        bucket = resource_name(str(site.id), name)
        if not await cloudflare.r2_bucket_exists(bucket):
            try:
                bucket = await cloudflare.create_r2_bucket(bucket)
            except ValidationError:
                if not await cloudflare.r2_bucket_exists(bucket):
                    raise
        r2[name] = bucket
        site.r2_buckets = dict(r2)
        await save(site)
        logger.info("sites.bindings: site %s r2 %s -> %s", site.id, name, bucket)

    return ProvisionedResources(
        d1_database_id=(getattr(site, "d1_database_id", "") or "").strip(),
        kv_namespaces=kv,
        r2_buckets=r2,
    )


async def teardown_bindings(site: Any, *, cloudflare: Any) -> list[str]:
    """Delete every KV namespace and R2 bucket recorded on ``site``. Best effort:
    each failure is logged and returned (``"kv:<name>"`` / ``"r2:<name>"``), and
    the rest still run. Removed entries are dropped from the site's maps, so a
    re-run only retries what is left."""
    failed: list[str] = []
    kv = dict(getattr(site, "kv_namespaces", None) or {})
    for name, ns_id in list(kv.items()):
        try:
            await cloudflare.delete_kv_namespace(ns_id)
            kv.pop(name)
        except Exception as exc:  # noqa: BLE001 - best effort, logged and reported
            logger.warning(
                "sites.bindings: site %s kv %s (%s) delete failed: %s", site.id, name, ns_id, exc
            )
            failed.append(f"kv:{name}")
    r2 = dict(getattr(site, "r2_buckets", None) or {})
    for name, bucket in list(r2.items()):
        try:
            if await cloudflare.delete_r2_bucket(bucket):
                r2.pop(name)
                continue
            logger.warning(
                "sites.bindings: site %s r2 bucket %s is not empty; setting it to expire "
                "all objects so an operator can delete it after a day",
                site.id,
                bucket,
            )
            await cloudflare.expire_r2_bucket_objects(bucket)
            failed.append(f"r2:{name}")
        except Exception as exc:  # noqa: BLE001 - best effort, logged and reported
            logger.warning(
                "sites.bindings: site %s r2 bucket %s delete failed: %s", site.id, bucket, exc
            )
            failed.append(f"r2:{name}")
    site.kv_namespaces = kv
    site.r2_buckets = r2
    return failed
