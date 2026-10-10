# ee/pocketpaw_ee/cloud/leads/intake.py — "Collect leads" settings for a site:
# what an owner needs to post leads in from a page built outside Paw (the signed
# key, absolute capture URLs), and which extra hosts may submit.
#
# Two origin sets, kept apart on purpose:
#   * DERIVED (``derived_origins``): ``Site.allowed_origins`` (stamped at publish,
#     grown by add_domain, also read by the concierge's key gate and frame CSP)
#     plus the site's own url host and custom domains. The system owns these; the
#     owner cannot remove them, so turning on ``enforce_origin`` never locks a site
#     out of its own pages.
#   * EXTRA (``Site.lead_intake_origins``): hosts the owner added. Stored in their
#     own field so the publish stamp and add/remove_domain (which rewrite
#     ``allowed_origins`` from a loaded doc) can never drop them, and so an
#     unverified owner-typed host only widens LEAD CAPTURE, never the concierge's
#     embed gate or frame-ancestors.
# ``effective_origins`` (derived + extra) is what the capture gate and the lead's
# ``origin_unrecognized`` flag judge against.
#
# Writes are one targeted ``$set`` on ``lead_intake_origins`` + ``enforce_origin``,
# never ``save()``, so a concurrent publish stamp is not rolled back.

from __future__ import annotations

from typing import Any

from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
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
    """Every host a submission may come from: derived, then the owner's extras."""
    hosts = derived_origins(site)
    for extra in getattr(site, "lead_intake_origins", None) or []:
        if extra and extra not in hosts:
            hosts.append(extra)
    return hosts


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
    # A site with no script_name (a foreign concierge) has no capture endpoint.
    if site is None or not site.script_name:
        raise NotFound("site", site_ref)
    return site


def _view(site: _SiteDoc) -> dict[str, Any]:
    from pocketpaw_ee.sites.service import _capture_base

    base = _capture_base().rstrip("/")
    derived = derived_origins(site)
    extra = list(getattr(site, "lead_intake_origins", None) or [])
    return {
        "site_id": site.script_name,
        "signed_key": site.signed_key,
        "capture_url": f"{base}/sites/{site.script_name}/capture",
        "form_url": f"{base}/capture/form",
        "allowed_origins": effective_origins(site),
        "derived_origins": derived,
        "extra_origins": extra,
        "enforce_origin": bool(site.enforce_origin),
        "max_extra_origins": MAX_EXTRA_ORIGINS,
    }


async def get_intake(workspace_id: str, site_ref: str) -> dict[str, Any]:
    return _view(await _load(workspace_id, site_ref))


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
    return _view(site)
