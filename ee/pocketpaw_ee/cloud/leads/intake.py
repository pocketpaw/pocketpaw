# ee/pocketpaw_ee/cloud/leads/intake.py — "Collect leads" settings for a site:
# what an owner needs to post leads in from a page built outside Paw (the signed
# key, absolute capture URLs), and which hosts may submit. Serves both kinds of
# site: BUILT (Paw hosts it; keyed by ``script_name``) and CONNECTED
# (``foreign_origin``: the owner's own website running only the Paw Bar; keyed by
# its object id, see ``domain.lead_site_id``). The key is ``Site.signed_key``, the
# same public embed credential the Paw Bar already uses on that site.
#
# Two origin sets, kept apart on purpose:
#   * DERIVED: system-owned, the owner cannot remove them, so turning on
#     ``enforce_origin`` never locks a site out of its own pages.
#       - built (``derived_origins``): ``Site.allowed_origins`` (publish stamp,
#         add_domain; also the concierge key gate and frame CSP) plus the url host
#         and custom domains, all values we wrote from a deploy we performed.
#       - connected (``verified_hosts``): the entries of ``allowed_origins`` this
#         workspace holds a VERIFIED ownership claim for, any age (see there).
#   * EXTRA (``Site.lead_intake_origins``): hosts the owner added. Stored in their
#     own field so the publish stamp and add/remove_domain (which rewrite
#     ``allowed_origins`` from a loaded doc) can never drop them, and so an
#     unverified owner-typed host only widens LEAD CAPTURE, never the concierge's
#     embed gate or frame-ancestors.
# ``resolve_origins`` (derived + extra) is what the capture gate, the 303 base and
# the lead's ``origin_unrecognized`` flag judge against; the router resolves it
# once per request.
#
# The view also carries ``honeypot_field`` (the site's bot-trap field name) so the
# owner's snippets name the field the capture service actually checks. Writes are
# targeted ``$set``s, never ``save()``, so a concurrent publish stamp is not
# rolled back.

from __future__ import annotations

import secrets
from typing import Any

from beanie import PydanticObjectId

from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
from pocketpaw_ee.cloud.leads.domain import lead_site_id
from pocketpaw_ee.cloud.leads.notification_settings import find_site
from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

MAX_EXTRA_ORIGINS = 20

# Hosts the dev posture accepts even though they are single-label.
_DEV_LOCAL_HOSTS = frozenset({"localhost"})


def _bare_host(value: str) -> str:
    """Scheme, credentials, path, query, fragment and port stripped; lowercased;
    trailing dot dropped. The same reduction ``origin_allowed`` applies to the
    inbound ``Origin`` header, so a stored host matches what a browser sends."""
    host = (value or "").strip().lower()
    if "://" in host:
        host = host.split("://", 1)[1]
    for sep in "/?#":
        host = host.split(sep, 1)[0]
    host = host.rsplit("@", 1)[-1]
    host = host.split(":", 1)[0]
    return host.rstrip(".")


def derived_origins(site: _SiteDoc) -> list[str]:
    """The system-maintained hosts, in a stable order: the stamped allowlist, then
    the site's canonical url host, then each custom domain. Every one is a value
    we wrote from a deploy we performed, never caller input."""
    hosts: list[str] = []
    candidates = list(site.allowed_origins or [])
    candidates.append(site.url or "")
    candidates.extend(d.hostname for d in (site.domains or []) if d.hostname)
    for candidate in candidates:
        host = _bare_host(candidate)
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def effective_origins(site: _SiteDoc) -> list[str]:
    """Every host a BUILT site's submission may come from: derived, then the
    owner's extras. ``resolve_origins`` answers for either kind of site."""
    return _with_extras(site, derived_origins(site))


def _with_extras(site: _SiteDoc, derived: list[str]) -> list[str]:
    hosts = list(derived)
    for extra in getattr(site, "lead_intake_origins", None) or []:
        if extra and extra not in hosts:
            hosts.append(extra)
    return hosts


def site_kind(site: _SiteDoc) -> str:
    return "connected" if getattr(site, "foreign_origin", False) else "built"


async def verified_hosts(site: _SiteDoc) -> list[str]:
    """A connected site's derived hosts: each ``allowed_origins`` entry this
    workspace holds a verified ownership claim on, in stored order.

    Any verified claim counts, however old. The 30-day window
    (``foreign_grounding.VERIFICATION_MAX_AGE``) belongs to the crawl, which
    fetches content; here the answer only decides which hosts are the site's own
    pages. Dropping a host on a stale proof would 403 the owner's own form on an
    enforcing site on the proof's anniversary, the failure the opt-in pin exists
    to avoid, and buy nothing: ``Origin`` binds browsers only. A host with no
    verified claim is dropped, so an unproved entry never counts as the site's own.
    """
    from pocketpaw_ee.sites import ownership

    hosts: list[str] = []
    for candidate in site.allowed_origins or []:
        host = _bare_host(candidate)
        if not host or host in hosts:
            continue
        if await ownership.verified_origin_record(site.workspace, host) is not None:
            hosts.append(host)
    return hosts


async def resolve_derived(site: _SiteDoc) -> list[str]:
    if site_kind(site) == "connected":
        return await verified_hosts(site)
    return derived_origins(site)


async def resolve_origins(site: _SiteDoc) -> list[str]:
    """Every host a submission may come from, for either kind of site."""
    return _with_extras(site, await resolve_derived(site))


async def find_capture_site(site_id: str) -> _SiteDoc | None:
    """The site a PUBLIC capture names, by ``lead_site_id``: a built site's
    script_name, else a connected site's object id. A draft (no script_name, not
    connected) is never found, so it still has no capture endpoint."""
    if not site_id:
        return None
    # global-read: public ingest is keyed by site, not by a workspace session.
    site = await _SiteDoc.find_one({"script_name": site_id})
    if site is not None:
        return site
    try:
        oid = PydanticObjectId(site_id)
    except Exception:  # noqa: BLE001 — not an object id, so not a connected site
        return None
    # global-read: as above. Only a connected site is addressable by object id.
    return await _SiteDoc.find_one({"_id": oid, "foreign_origin": True})


def normalize_origin(value: str) -> str:
    """One owner-typed origin reduced to a bare hostname, or ``ValidationError``.

    Accepts a URL or a host (``https://Shop.Example.com:443/contact`` becomes
    ``shop.example.com``). Refuses IP literals, single-label names and anything
    that is not a legal DNS name (the rules ``ownership.normalize_claim_host``
    applies to origin claims). ``localhost`` is allowed only outside a production
    posture. Wildcards are refused: ``origin_allowed`` matches hosts exactly, so a
    ``*.example.com`` entry would be stored and never match anything.
    """
    from pocketpaw_ee.cloud.auth.core import _is_production
    from pocketpaw_ee.sites.ownership import normalize_claim_host

    raw = (value or "").strip()
    if "*" in raw:
        raise ValidationError(
            "leads.intake_origin_invalid",
            "Wildcards aren't supported. Add each hostname on its own.",
            details={"origin": raw[:300]},
        )
    host = _bare_host(raw)
    if host in _DEV_LOCAL_HOSTS and not _is_production():
        return host
    try:
        return normalize_claim_host(host)
    except ValidationError as exc:
        raise ValidationError(
            "leads.intake_origin_invalid",
            "Use a website hostname like shop.example.com (no IP addresses).",
            details={"origin": str(value)[:300]},
        ) from exc


def normalize_origins(values: list[str]) -> list[str]:
    """Normalize, dedupe (first spelling wins, order kept), then cap."""
    hosts: list[str] = []
    for value in values:
        host = normalize_origin(value)
        if host not in hosts:
            hosts.append(host)
    if len(hosts) > MAX_EXTRA_ORIGINS:
        raise ValidationError(
            "leads.intake_origins_too_many",
            f"At most {MAX_EXTRA_ORIGINS} extra origins per site.",
        )
    return hosts


async def _load(workspace_id: str, site_ref: str) -> _SiteDoc:
    site = await find_site(workspace_id, site_ref)
    # A draft (no script_name, not connected) has no capture endpoint.
    if site is None or not lead_site_id(site):
        raise NotFound("site", site_ref)
    if not site.signed_key:
        await _mint_key(site)
    return site


async def _mint_key(site: _SiteDoc) -> None:
    """Give a keyless site a key in the format the mint and publish use. The write
    is conditional on the key still being empty, so two racing reads settle on one
    key, and the stored winner is read back either way."""
    await _SiteDoc.get_pymongo_collection().update_one(
        {"_id": site.id, "signed_key": {"$in": ["", None]}},
        {"$set": {"signed_key": f"site_key_{secrets.token_urlsafe(24)}"}},
    )
    fresh = await _SiteDoc.get(site.id)
    site.signed_key = fresh.signed_key if fresh is not None else ""


async def _view(site: _SiteDoc) -> dict[str, Any]:
    from pocketpaw_ee.sites.service import _capture_base

    base = _capture_base().rstrip("/")
    public_id = lead_site_id(site)
    derived = await resolve_derived(site)
    extra = list(getattr(site, "lead_intake_origins", None) or [])
    return {
        "site_id": public_id,
        "site_kind": site_kind(site),
        "signed_key": site.signed_key,
        "capture_url": f"{base}/sites/{public_id}/capture",
        "form_url": f"{base}/capture/form",
        "allowed_origins": _with_extras(site, derived),
        "derived_origins": derived,
        "extra_origins": extra,
        "enforce_origin": bool(site.enforce_origin),
        "max_extra_origins": MAX_EXTRA_ORIGINS,
        # The hidden field the capture service treats as a bot trap; snippets
        # must use this exact name.
        "honeypot_field": site.honeypot_field,
    }


async def get_intake(workspace_id: str, site_ref: str) -> dict[str, Any]:
    return await _view(await _load(workspace_id, site_ref))


async def update_intake(
    workspace_id: str,
    site_ref: str,
    *,
    extra_origins: list[str],
    enforce_origin: bool,
) -> dict[str, Any]:
    """Replace the owner's extra hosts and set the pin. Validation runs before
    the site is touched, so a bad entry leaves everything as it was."""
    hosts = normalize_origins(extra_origins)
    site = await _load(workspace_id, site_ref)
    await site.set({"lead_intake_origins": hosts, "enforce_origin": bool(enforce_origin)})
    return await _view(site)
