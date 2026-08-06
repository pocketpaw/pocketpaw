# ee/pocketpaw_ee/cloud/growth/bridges/leads.py — captured site lead → outbound
# prospect. The payoff end of the site-lead funnel.
#
# Created 2026-08-06 (feat/coupling-lead-to-prospect, T-7): the second
# subscriber on ``lead.captured``, built to the shape of
# ``leads/bridges/notifications.py`` (T-6). A visitor who fills in a form on a
# published Paw Site is the warmest contact this product will ever see, and
# until now they reached nothing: the Leads view was a dead-end list somebody
# had to re-key into /growth by hand, and growth's own vocabulary could not even
# say where such a row came from.
#
# WHY IT READS THE LEAD BACK. The event carries identifiers only — the
# submitted values are untrusted visitor PII and do not ride the bus (T-6's
# design). So this bridge takes the workspace + lead id from the event and
# fetches the row through ``leads_service.get_in_workspace``, which filters on
# that same workspace. That read is the tenancy boundary: an event naming
# workspace B and a lead from workspace A resolves to nothing, so no prospect
# is filed anywhere.
#
# WHY IT NEVER RAISES. A lead is revenue; a pipeline row is convenience. Every
# outward call is wrapped, so a growth outage — a bad key, a Mongo hiccup, a
# validation change — costs the workspace a prospect and never the lead or the
# notification. ``EventBus.emit`` also logs and swallows per handler, so this
# containment is belt-and-braces; the belt is here because a subscriber that
# leans on the dispatcher's rescue is one refactor (or one direct call, or a
# replay job) away from taking capture down with it.

from __future__ import annotations

import logging
import re
from typing import Any

from pocketpaw_ee.cloud.growth.domain import contact_dedupe_key
from pocketpaw_ee.cloud.leads.domain import Lead
from pocketpaw_ee.cloud.shared.events import event_bus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Reading a form
# ---------------------------------------------------------------------------

# A site's ``event_mapping`` is authored per site, so the property names are
# whatever that site's builder chose. These are the names the shipped default
# mapping uses plus the obvious variants a hand-edited form arrives with; the
# match is on a NORMALISED key (case, spaces, dashes and underscores removed),
# so "Email Address", "email_address" and "e-mail address" are one name.
_EMAIL_FIELDS: tuple[str, ...] = (
    "email",
    "emailaddress",
    "youremail",
    "workemail",
    "businessemail",
    "contactemail",
)
_NAME_FIELDS: tuple[str, ...] = ("fullname", "name", "yourname", "contactname")
_FIRST_NAME_FIELDS: tuple[str, ...] = ("firstname", "givenname")
_LAST_NAME_FIELDS: tuple[str, ...] = ("lastname", "surname", "familyname")
_COMPANY_FIELDS: tuple[str, ...] = (
    "company",
    "companyname",
    "organisation",
    "organization",
    "business",
    "businessname",
)
# Ordered: a field that SAYS it is the company's site beats a bare "website".
#
# ``companywebsite`` is listed for sites that renamed their honeypot, and is
# usually EMPTY: ``company_website`` is the DEFAULT honeypot field name
# (``Site.honeypot_field``), so on a stock site any submission that fills it is
# dropped as a bot before a Lead is ever written. Harmless to keep, and worth
# knowing before someone "fixes" the ordering because the top entry never hits.
_WEBSITE_FIELDS: tuple[str, ...] = (
    "companywebsite",
    "companydomain",
    "companyurl",
    "website",
    "weburl",
    "siteurl",
    "domain",
)

# Deliberately loose. This decides "is this string an address at all", not "will
# it deliver" — growth's own email evidence rules govern what may be written to,
# and an over-strict pattern here would silently drop real leads.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# The DTO's caps. Applied before building the request so an over-long form value
# is truncated into a valid prospect rather than 422-ing the whole submission
# away — a lead with a 300-character company name is still a lead.
_MAX_NAME = 200
_MAX_KEY = 253


def _normalise_key(key: Any) -> str:
    """Reduce a form field name to letters and digits, lowercased."""
    return "".join(ch for ch in str(key).lower() if ch.isalnum())


def _indexed(properties: dict[str, Any]) -> dict[str, str]:
    """Normalised field name → its stringified, stripped value.

    First occurrence wins, so a mapping that produced both ``email`` and
    ``Email`` resolves deterministically instead of by dict order luck.
    """
    out: dict[str, str] = {}
    for key, value in properties.items():
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        out.setdefault(_normalise_key(key), text)
    return out


def _first(indexed: dict[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        if indexed.get(name):
            return indexed[name]
    return ""


def _contact_name(indexed: dict[str, str]) -> str:
    """A display name: a full-name field, else first + last joined."""
    full = _first(indexed, _NAME_FIELDS)
    if full:
        return full
    parts = [_first(indexed, _FIRST_NAME_FIELDS), _first(indexed, _LAST_NAME_FIELDS)]
    return " ".join(part for part in parts if part)


def _email(indexed: dict[str, str]) -> str:
    """The first named field holding something shaped like an address.

    Only NAMED fields are considered. A message body mentioning an address is
    not the submitter volunteering it, and harvesting one would put an address
    nobody offered into an outbound pipeline.
    """
    for name in _EMAIL_FIELDS:
        candidate = indexed.get(name, "").lower()
        if _EMAIL_RE.match(candidate):
            return candidate
    return ""


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


async def _on_lead_captured(data: dict[str, Any]) -> None:
    """``lead.captured`` → one Prospect in the lead's own workspace.

    A payload without both workspace_id and lead_id is malformed — there is no
    tenant to file into or nothing to read — so it no-ops rather than guessing.
    """
    workspace_id = data.get("workspace_id")
    lead_id = data.get("lead_id")
    if not (workspace_id and lead_id):
        logger.warning("lead.captured ignored by growth — incomplete payload keys=%s", sorted(data))
        return

    lead = await _load_lead(str(workspace_id), str(lead_id))
    if lead is None:
        return

    indexed = _indexed(lead.properties or {})
    email = _email(indexed)
    key = contact_dedupe_key(email=email, company_domain=_first(indexed, _WEBSITE_FIELDS))
    if not key or len(key) > _MAX_KEY:
        # Neither a usable address nor a usable company domain — a phone-only
        # or message-only submission. There is no stable identity to dedupe on,
        # and a synthetic per-submission key would file a fresh row every time
        # the same visitor came back. The lead itself is untouched: it is
        # persisted, it rang the workspace, and it is in the Leads view.
        logger.info(
            "lead.captured filed no prospect — no dedupe key workspace=%s lead=%s",
            workspace_id,
            lead_id,
        )
        return

    site_label = str(data.get("site_name") or "").strip() or str(data.get("site_id") or "")
    form_type = str(data.get("form_type") or "form")
    await _upsert(
        workspace_id=str(workspace_id),
        lead_id=str(lead_id),
        key=key,
        name=_contact_name(indexed)[:_MAX_NAME],
        company=_first(indexed, _COMPANY_FIELDS)[:_MAX_NAME],
        email=email,
        # The brief carries PROVENANCE, never the visitor's own words. It is
        # read by the drafting model, and the message body is untrusted text
        # that has only passed a HIGH-threshold injection screen; the operator
        # reads the real thing on the Lead, which ``lead_id`` links to.
        brief=f"Inbound: submitted the {form_type} form on {site_label}.".strip(),
    )


# ---------------------------------------------------------------------------
# Helpers — every outward call is contained (see the module header)
# ---------------------------------------------------------------------------


async def _load_lead(workspace_id: str, lead_id: str) -> Lead | None:
    """Fetch the captured Lead, scoped to the workspace the event named.

    Late import to avoid a module-load cycle and to tolerate the leads service
    being absent in unit-test contexts, matching the notifications bridge.
    """
    try:
        from pocketpaw_ee.cloud.leads import service as leads_service

        lead = await leads_service.get_in_workspace(workspace_id, lead_id)
    except Exception:
        logger.exception("Failed to read lead=%s for growth bridge", lead_id)
        return None
    if lead is None:
        logger.warning(
            "lead.captured named a lead growth cannot see workspace=%s lead=%s",
            workspace_id,
            lead_id,
        )
    return lead


async def _upsert(
    *,
    workspace_id: str,
    lead_id: str,
    key: str,
    name: str,
    company: str,
    email: str,
    brief: str,
) -> None:
    """Create-or-enrich the prospect. Late import, and never raises.

    ``whatsapp_number`` is deliberately NOT populated from a form's phone
    field: filling in a contact form is not consent to be messaged on WhatsApp,
    and that field is the compliance claim the WhatsApp channel records at send
    time. ``opted_in`` stays false for the same reason.
    """
    try:
        from pocketpaw_ee.cloud.growth import service as growth_service
        from pocketpaw_ee.cloud.growth.dto import CreateProspectRequest

        await growth_service.upsert_from_site_lead(
            workspace_id,
            CreateProspectRequest(
                name=name,
                company=company,
                domain=key,
                source="site_lead",
                emails=[email] if email else [],
                research_brief=brief,
                lead_id=lead_id,
            ),
        )
    except Exception:
        logger.exception("Failed to file a prospect for lead=%s", lead_id)


# ---------------------------------------------------------------------------
# Registration — called from mount_cloud() after init_realtime.
# ---------------------------------------------------------------------------


def register_growth_lead_listeners() -> None:
    """Wire the ``lead.captured`` → prospect subscriber."""
    event_bus.subscribe("lead.captured", _on_lead_captured)
    logger.info("registered lead.captured → growth prospect subscriber")


__all__ = ["register_growth_lead_listeners"]
