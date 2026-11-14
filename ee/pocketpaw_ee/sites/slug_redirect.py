# ee/pocketpaw_ee/sites/slug_redirect.py — a renamed site's old address keeps
# working: it 308s to the same path and query on the site's current address until
# the old name's 30-day hold ends.
#
# Rename only exists on the ``workers`` lane, where a site's address IS its
# account-level Worker name (``<name>.<sub>.workers.dev``). So the redirect is a
# second, tiny account-level Worker deployed under the OLD name in place of the
# site's old Worker (``deploy_redirect``), recorded as a ``SiteRedirect`` row.
#
#   * The target is the site's current origin, validated by ``redirect_origin``
#     (https, a plain hostname, no userinfo or port) and baked into the Worker as a
#     constant. The Worker copies only the request's path and query onto that
#     origin, so no request can steer it to another host: no open redirect.
#   * A later rename re-points every redirect the site owns (``retarget_site``), so
#     A -> B -> C sends A straight to C, never through B.
#   * NEVER OVER A NAME A SITE CLAIMS. Before any upload or delete, a name that is
#     some site's ``worker_name``, ``slug`` or ``slug_pending`` is left alone (its
#     row is dropped and logged): a redirect must not replace a live site's Worker.
#   * A site taking one of its own old names back drops that row (``forget``)
#     before its deploy overwrites the redirect Worker. Only unexpired rows count
#     as the site's own (``owns_redirect``).
#   * The upload is a bare module Worker with no bindings, its crons cleared and
#     workers.dev on. The caller skips redirects for Durable Object sites (a
#     replacement without their classes would be refused).
#   * ``sweep_slug_redirects`` (the shared heartbeat) deletes expired redirect
#     Workers and their rows; ``remove_site_redirects`` (the site delete cascade)
#     deletes all of a site's at once.
#
# A 308 keeps the method (a form POST to the old address stays a POST). It carries
# ``Cache-Control: private, max-age=300`` so a browser forgets it within minutes and
# no shared cache keeps it: an owner who renames back (A -> B -> A) would otherwise
# leave visitors with a cached A -> B bouncing off B's redirect to A.

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from pocketpaw_ee.cloud.models.site_redirect import SiteRedirect

logger = logging.getLogger(__name__)

# The workers.dev address of an account-level script.
_ACCOUNT_TARGET = "account"
_MODULE = "redirect.mjs"
_COMPATIBILITY_DATE = "2024-09-23"
# A DNS hostname: dot-separated labels of letters, digits and inner hyphens.
_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
_HOSTNAME = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(\.{_LABEL})+$")
# Per tick, so a backlog cannot wedge the shared heartbeat.
_SWEEP_BATCH_LIMIT = 200


def redirect_origin(url: str) -> str:
    """The ``https://<host>`` a redirect may send people to, from a site's address.

    Raises ``ValueError`` for anything that is not a plain https address: another
    scheme, userinfo, a port, or a host that is not a DNS name. The site's url comes
    from our own deploy, so a refusal here means something upstream is wrong, and
    the caller falls back to deleting the old Worker rather than redirecting."""
    parts = urlsplit((url or "").strip())
    if parts.scheme != "https":
        raise ValueError(f"redirect target must be https, got {url!r}")
    if parts.username is not None or parts.password is not None or parts.port is not None:
        raise ValueError(f"redirect target must be a bare host, got {url!r}")
    host = (parts.hostname or "").lower()
    if not _HOSTNAME.match(host):
        raise ValueError(f"redirect target host is not a hostname: {url!r}")
    return f"https://{host}"


def redirect_worker_source(origin: str) -> bytes:
    """The redirect Worker: a 308 to ``origin`` with the request's path and query.

    The target URL is built FROM ``origin`` and only its path and query are set, so a
    path like ``//evil.example`` stays a path on ``origin``."""
    return (
        "const TARGET = " + json.dumps(redirect_origin(origin)) + ";\n"
        "export default {\n"
        "  fetch(request) {\n"
        "    const from = new URL(request.url);\n"
        "    const to = new URL(TARGET);\n"
        "    to.pathname = from.pathname;\n"
        "    to.search = from.search;\n"
        "    return new Response(null, {\n"
        "      status: 308,\n"
        '      headers: { Location: to.toString(), "Cache-Control": "private, max-age=300" },\n'
        "    });\n"
        "  },\n"
        "};\n"
    ).encode()


async def _upload(cf: Any, name: str, origin: str) -> None:
    from pocketpaw_ee.sites.cloudflare_client import WorkerModule

    await cf.put_worker(
        script_name=name,
        target=_ACCOUNT_TARGET,
        modules=[
            WorkerModule(_MODULE, redirect_worker_source(origin), "application/javascript+module")
        ],
        main_module=_MODULE,
        bindings=[],
        compatibility_date=_COMPATIBILITY_DATE,
    )
    await cf.clear_schedules(name)
    await cf.enable_workers_dev(name)


async def _claimed_by_a_site(name: str) -> bool:
    """Does any site serve at, or move to, ``name``? Such a name is never uploaded
    over or deleted here."""
    from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

    # global-read: workers.dev is one namespace for the whole account.
    found = await _SiteDoc.get_pymongo_collection().find_one(
        {"$or": [{"worker_name": name}, {"slug": name}, {"slug_pending": name}]}, {"_id": 1}
    )
    return found is not None


async def deploy_redirect(
    cf: Any, *, name: str, site_id: str, workspace_id: str, url: str, days: int
) -> bool:
    """Replace the Worker ``name`` with a redirect to ``url`` for ``days`` days.

    False, touching nothing, when a site claims ``name`` (the old Worker is not the
    caller's to replace or delete then). Raises when the target is invalid or the
    upload fails; the caller decides what happens to the old Worker. The row is
    written only after the upload, so a row always means a redirect Worker is (or
    was) there to sweep."""
    origin = redirect_origin(url)
    if await _claimed_by_a_site(name):
        logger.warning("sites: not redirecting %s, a site claims that name", name)
        return False
    await _upload(cf, name, origin)
    now = datetime.now(UTC)
    await SiteRedirect.get_pymongo_collection().replace_one(
        {"name": name},
        {
            "name": name,
            "site_id": site_id,
            "workspace_id": workspace_id,
            "target_url": origin,
            "created_at": now,
            "expires_at": now + timedelta(days=days),
        },
        upsert=True,
    )
    logger.info("sites: %s now redirects to %s", name, origin)
    return True


async def retarget_site(cf: Any, *, site_id: str, url: str, skip: str = "") -> None:
    """Point every redirect ``site_id`` owns (but ``skip``) at ``url``. Best effort:
    a redirect that cannot be re-uploaded keeps its old target and is logged."""
    origin = redirect_origin(url)
    rows = SiteRedirect.get_pymongo_collection().find(
        {"site_id": site_id, "target_url": {"$ne": origin}}
    )
    async for row in rows:
        if row["name"] == skip:
            continue
        if await _claimed_by_a_site(row["name"]):
            logger.warning("sites: dropping redirect %s, a site claims that name", row["name"])
            await SiteRedirect.get_pymongo_collection().delete_one({"_id": row["_id"]})
            continue
        try:
            await _upload(cf, row["name"], origin)
        except Exception:  # noqa: BLE001 - the redirect still answers, one hop longer
            logger.warning(
                "sites: could not re-point redirect %s at %s", row["name"], origin, exc_info=True
            )
            continue
        await SiteRedirect.get_pymongo_collection().update_one(
            {"_id": row["_id"]}, {"$set": {"target_url": origin}}
        )


async def owns_redirect(name: str, site_id: str) -> bool:
    """Is ``name`` one of ``site_id``'s own, unexpired redirects (so the site may take
    it back)? An expired row is a name on its way out, not the site's."""
    found = await SiteRedirect.get_pymongo_collection().find_one(
        {"name": name, "site_id": site_id, "expires_at": {"$gt": datetime.now(UTC)}},
        {"_id": 1},
    )
    return found is not None


async def forget(name: str, *, site_id: str) -> None:
    """Drop ``site_id``'s row for ``name`` without touching the Worker: the site is
    deploying over it."""
    await SiteRedirect.get_pymongo_collection().delete_many({"name": name, "site_id": site_id})


async def _delete_rows(cf: Any, rows: list[dict]) -> int:
    removed = 0
    for row in rows:
        name = row["name"]
        # Never delete a Worker a site serves from, or a name one is moving to.
        if not await _claimed_by_a_site(name):
            await cf.delete_account_script(name)
        await SiteRedirect.get_pymongo_collection().delete_one({"_id": row["_id"]})
        removed += 1
    return removed


async def remove_site_redirects(cf: Any, *, site_id: str) -> int:
    """Delete every redirect Worker and row ``site_id`` owns (the site is going away).
    Raises on a failed delete so the cascade step retries; done rows stay done."""
    rows = [r async for r in SiteRedirect.get_pymongo_collection().find({"site_id": site_id})]
    return await _delete_rows(cf, rows)


async def sweep_slug_redirects(*, cf: Any = None, now: datetime | None = None) -> dict[str, int]:
    """Delete redirects whose hold has ended, freeing the name. A failed delete keeps
    its row for the next tick."""
    now = now or datetime.now(UTC)
    rows = [
        r
        async for r in SiteRedirect.get_pymongo_collection()
        .find({"expires_at": {"$lte": now}})
        .limit(_SWEEP_BATCH_LIMIT)
    ]
    if not rows:
        return {"expired": 0, "removed": 0}
    if cf is None:
        from pocketpaw_ee.sites.service import _cf_account_client

        cf = _cf_account_client()
        if cf is None:
            logger.warning("sites: %d expired redirects wait for Cloudflare credentials", len(rows))
            return {"expired": len(rows), "removed": 0}
    removed = 0
    for row in rows:
        try:
            removed += await _delete_rows(cf, [row])
        except Exception:  # noqa: BLE001 - retried next tick
            logger.warning(
                "sites: could not remove expired redirect %s", row["name"], exc_info=True
            )
    return {"expired": len(rows), "removed": removed}


__all__ = [
    "deploy_redirect",
    "forget",
    "owns_redirect",
    "redirect_origin",
    "redirect_worker_source",
    "remove_site_redirects",
    "retarget_site",
    "sweep_slug_redirects",
]
