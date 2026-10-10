# ee/pocketpaw_ee/cloud/notifications/webhook_formats.py
# The message a site's lead-notification webhook gets, in its platform's native
# shape. Pure: no I/O. The outbox calls ``render`` at send time (with the lead it
# just loaded); the settings preview calls it on ``sample_data``.
#
#   slack        Block Kit: header, section fields, message section, context,
#                "Open in Paw" button, plus a ``text`` fallback
#   discord      ``{content, embeds:[{title, fields, url, timestamp, color}]}``
#                with ``allowed_mentions: {parse: []}`` so nothing can ping
#   teams        an Adaptive Card 1.4 inside a ``message`` attachment (what
#                Workflows webhooks take; old connector URLs accept it too)
#   google_chat  ``cardsV2`` (decoratedText widgets, a button) + ``text``
#   json         the existing envelope ``{id, type, created_at, data}``; the
#                outbox signs it. The template does not apply to json.
#
# Everything from the visitor (and the site name) is treated as hostile: links
# are defanged (``hxxps://``, as ``outbox.whatsapp_lead_text`` does), mention
# and markup syntax is neutralised per platform (Slack ``&<>`` escaped so
# ``<!channel>`` is inert; Discord markdown backslash-escaped and ``@`` broken
# with a zero-width space; HTML-escaped in Google Chat card text), and every
# value is capped below that platform's field limit. Fallback ``text`` carries
# only the title and site, never visitor data.
#
# The template picks the title (empty = the event's default), which catalogue
# fields show, in catalogue order, and whether the link button shows.

from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any

from pocketpaw_ee.cloud.models.lead_notifications import DEFAULT_TEMPLATE_FIELDS
from pocketpaw_ee.cloud.notifications import webhook_signing

TEMPLATE_FIELDS: tuple[str, ...] = (
    "name",
    "email",
    "phone",
    "message",
    "company",
    "source",
    "page",
    "extras",
)
FIELD_LABELS: dict[str, str] = {
    "name": "Name",
    "email": "Email",
    "phone": "Phone",
    "message": "Message",
    "company": "Company",
    "source": "Source",
    "page": "Page",
    "extras": "Other fields",
}
TITLE_MAX = 150
# Long free text gets its own block; the rest are short facts.
_LONG_FIELDS = frozenset({"message", "extras"})

# Owner-facing event name -> wire ``type``.
EVENT_TYPES: dict[str, str] = {
    "lead_captured": "lead.captured",
    "handoff": "concierge.handoff",
    "booking": "booking.created",
    "test": "notification.test",
    "lead_updated": "lead.updated",
}
DEFAULT_TITLES: dict[str, str] = {
    "lead.captured": "New lead",
    "concierge.handoff": "A visitor asked for a person",
    "booking.created": "New booking",
    "notification.test": "Test notification",
    "lead.updated": "Lead updated",
}
_DISCORD_COLORS: dict[str, int] = {
    "lead.captured": 0x2563EB,
    "concierge.handoff": 0xD97706,
    "booking.created": 0x059669,
    "notification.test": 0x6B7280,
    "lead.updated": 0x7C3AED,
}
_SOURCE_LABELS = {
    "form": "Form",
    "concierge": "Concierge",
    "handoff": "Concierge handoff",
    "booking": "Booking",
}
_COMPANY_KEYS = (
    "company",
    "company_name",
    "companyname",
    "organization",
    "organisation",
    "business",
)
_EXTRAS_MAX = 10
_LINK_TEXT = "Open in Paw"


def event_type(event: str) -> str:
    """The wire type for an owner-facing event name (types pass through)."""
    return EVENT_TYPES.get(event, event)


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------

_LINK = re.compile(r"\b(h)tt(ps?://)", re.IGNORECASE)
_BLANK_RUNS = re.compile(r"\n{3,}")
_SLACK_MARKS = re.compile(r"[*~`]")
# An ``@`` that could start a mention (not the one inside an email address).
_MENTION_AT = re.compile(r"(?<![\w.+-])@")
_SCHEME = re.compile(r"^https?://", re.IGNORECASE)
_DISCORD_SPECIALS = re.compile(r"([\\*_~`|>#\[\]()<])")


def _plain(value: Any) -> str:
    """One string: control characters dropped, blank-line runs collapsed, links
    defanged so visitor text never becomes a tappable link."""
    text = str(value if value is not None else "")
    text = "".join(c for c in text if c in "\n\t" or c >= " ").replace("\t", " ").strip()
    return _LINK.sub(r"\1xx\2", _BLANK_RUNS.sub("\n\n", text))


def _slack(text: str) -> str:
    return (
        _SLACK_MARKS.sub("", text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _discord(text: str) -> str:
    return _MENTION_AT.sub("@\u200b", _DISCORD_SPECIALS.sub(r"\\\1", text))


def _teams(text: str) -> str:
    return _SLACK_MARKS.sub("", text)


def _gchat(text: str) -> str:
    return html.escape(text, quote=False).replace("\n", "<br>")


def _gchat_plain(text: str) -> str:
    """Google Chat message ``text`` / plain card text: ``<users/all>`` would
    mention everyone, so no angle brackets survive."""
    return _SLACK_MARKS.sub("", text).replace("<", "(").replace(">", ")")


def _fit(raw: str, limit: int, clean) -> str:
    """``clean(raw)`` no longer than ``limit``, cut on the RAW text (with an
    ellipsis) so an escape sequence is never split."""
    out = clean(raw)
    while len(out) > limit and raw:
        raw = raw[: max(0, len(raw) - (len(out) - limit) - 1)].rstrip()
        out = clean(raw + "…")
    return out[:limit]


# ---------------------------------------------------------------------------
# Template + data
# ---------------------------------------------------------------------------


def _template(template: Any) -> tuple[str, list[str], bool]:
    """(title, fields, show_link) from a dict, a model or None."""
    if template is None:
        return "", list(DEFAULT_TEMPLATE_FIELDS), True
    get = template.get if isinstance(template, dict) else lambda k, d=None: getattr(template, k, d)
    title = str(get("title", "") or "").strip()
    fields = get("fields", None)
    fields = list(DEFAULT_TEMPLATE_FIELDS) if fields is None else list(fields)
    show_link = get("show_link", True) is not False
    return title, [f for f in TEMPLATE_FIELDS if f in fields], show_link


def _properties(data: dict[str, Any]) -> dict[str, Any]:
    props = data.get("properties")
    return dict(props) if isinstance(props, dict) else {}


def _company(data: dict[str, Any]) -> str:
    if data.get("company"):
        return str(data["company"])
    props = _properties(data)
    for key, value in props.items():
        if re.sub(r"[^a-z]", "", str(key).lower()) in _COMPANY_KEYS and str(value or "").strip():
            return str(value)
    return ""


def _source(etype: str, data: dict[str, Any]) -> str:
    if etype == "concierge.handoff":
        return _SOURCE_LABELS["handoff"]
    src = data.get("source")
    if isinstance(src, dict):
        kind = str(src.get("kind") or "form")
        label = _SOURCE_LABELS.get(kind, kind.capitalize())
        form_type = str(src.get("form_type") or data.get("form_type") or "")
        return f"{label} · {form_type}" if form_type and form_type != kind else label
    if etype == "booking.created":
        return _SOURCE_LABELS["booking"]
    return str(src or "")


def _page(data: dict[str, Any]) -> str:
    """The page the lead came from, without its scheme: shown, never a link."""
    src = data.get("source")
    page = src.get("origin") if isinstance(src, dict) else None
    page = page or data.get("page") or data.get("page_url") or ""
    return _SCHEME.sub("", str(page).strip())


def _extras(data: dict[str, Any]) -> str:
    from pocketpaw.sites_capture.contact_form import canonical_name

    lines = []
    for key, value in _properties(data).items():
        name = str(key)
        if name.startswith("_") or canonical_name(name) is not None:
            continue
        if re.sub(r"[^a-z]", "", name.lower()) in _COMPANY_KEYS:
            continue
        text = " ".join(str(value if value is not None else "").split())
        if text:
            lines.append(f"{name}: {text}")
        if len(lines) >= _EXTRAS_MAX:
            break
    return "\n".join(lines)


def lead_fields(
    event: str, data: dict[str, Any] | None, fields: list[str]
) -> list[tuple[str, str]]:
    """(catalogue key, raw value) for each chosen field that has a value."""
    etype = event_type(event)
    d = data or {}
    values = {
        "name": d.get("name"),
        "email": d.get("email"),
        "phone": d.get("phone"),
        "message": d.get("question") if etype == "concierge.handoff" else d.get("message"),
        "company": _company(d),
        "source": _source(etype, d),
        "page": _page(d),
        "extras": _extras(d),
    }
    if etype == "lead.updated" and d.get("status"):
        values["source"] = f"{values['source']} · status: {d['status']}".strip(" ·")
    out = []
    for key in fields:
        text = _plain(values.get(key))
        if text:
            out.append((key, text))
    return out


def _timestamp(data: dict[str, Any], created_at: str) -> str | None:
    for raw in (data.get("created_at"), created_at):
        if not raw:
            continue
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).isoformat()
        except ValueError:
            continue
    return None


def sample_data(event: str, site_name: str = "") -> dict[str, Any]:
    """A realistic made-up payload for the settings preview."""
    etype = event_type(event)
    site = site_name or "Your site"
    if etype == "concierge.handoff":
        return {
            "site_name": site,
            "customer_ref": "visitor-42",
            "question": "Can I talk to someone about a bulk order before Friday?",
            "name": "Priya Sharma",
            "email": "priya@example.com",
        }
    if etype == "notification.test":
        return {"site_name": site}
    return {
        "id": "sample",
        "site_name": site,
        "form_type": "booking" if etype == "booking.created" else "contact",
        "status": "contacted" if etype == "lead.updated" else "new",
        "name": "Priya Sharma",
        "email": "priya@example.com",
        "phone": "+1 555 010 1234",
        "message": "Hi! Could you quote 20 embroidered jackets for our team?",
        "properties": {"company": "Northwind Studio", "team_size": "20", "budget": "$2,000"},
        "source": {
            "kind": "booking" if etype == "booking.created" else "form",
            "form_type": "booking" if etype == "booking.created" else "contact",
            "origin": "https://northwind.example.com/contact",
        },
        "created_at": "2026-10-10T09:30:00+00:00",
    }


# ---------------------------------------------------------------------------
# Platform renderers
# ---------------------------------------------------------------------------


def _slack_body(title, site, rows, link, etype) -> dict[str, Any]:
    short = [(k, v) for k, v in rows if k not in _LONG_FIELDS][:10]
    long_rows = [(k, v) for k, v in rows if k in _LONG_FIELDS]
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": _fit(title, 150, str)}}
    ]
    if short:
        blocks.append(
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*{FIELD_LABELS[k]}*\n{_fit(v, 1900, _slack)}"}
                    for k, v in short
                ],
            }
        )
    for k, v in long_rows:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{FIELD_LABELS[k]}*\n{_fit(v, 2900, _slack)}"},
            }
        )
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"{_fit(site, 200, _slack)} · Paw"}],
        }
    )
    if link:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": _LINK_TEXT},
                        "url": link[:3000],
                    }
                ],
            }
        )
    fallback = f"{title} · {site}" if site else title
    return {"text": _fit(fallback, 300, _slack), "blocks": blocks}


def _discord_body(title, site, rows, link, etype, timestamp) -> dict[str, Any]:
    embed: dict[str, Any] = {
        "title": _fit(title, 256, _discord),
        "color": _DISCORD_COLORS.get(etype, 0x2563EB),
        "fields": [
            {
                "name": FIELD_LABELS[k],
                "value": _fit(v, 1024 if k in _LONG_FIELDS else 256, _discord),
                "inline": k not in _LONG_FIELDS,
            }
            for k, v in rows
        ][:25],
    }
    if site:
        embed["footer"] = {"text": _fit(f"{site} · Paw", 200, str)}
    if link:
        embed["url"] = link
    if timestamp:
        embed["timestamp"] = timestamp
    content = f"{title} · {site}" if site else title
    return {
        "content": _fit(content, 400, _discord),
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }


def _teams_body(title, site, rows, link, etype) -> dict[str, Any]:
    body: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": _fit(title, TITLE_MAX, _teams),
            "size": "Medium",
            "weight": "Bolder",
            "wrap": True,
        }
    ]
    if site:
        body.append(
            {
                "type": "TextBlock",
                "text": _fit(site, 200, _teams),
                "isSubtle": True,
                "spacing": "None",
                "wrap": True,
            }
        )
    facts = [
        {"title": FIELD_LABELS[k], "value": _fit(v, 1000, _teams)}
        for k, v in rows
        if k not in _LONG_FIELDS
    ]
    if facts:
        body.append({"type": "FactSet", "facts": facts})
    for k, v in rows:
        if k in _LONG_FIELDS:
            body.append(
                {"type": "TextBlock", "text": FIELD_LABELS[k], "weight": "Bolder", "wrap": True}
            )
            body.append(
                {
                    "type": "TextBlock",
                    "text": _fit(v, 3000, _teams),
                    "wrap": True,
                    "spacing": "Small",
                }
            )
    card: dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
    }
    if link:
        card["actions"] = [{"type": "Action.OpenUrl", "title": _LINK_TEXT, "url": link}]
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": card,
            }
        ],
    }


def _gchat_body(title, site, rows, link, etype) -> dict[str, Any]:
    widgets: list[dict[str, Any]] = [
        {
            "decoratedText": {
                "topLabel": FIELD_LABELS[k],
                "text": _fit(v, 2000 if k in _LONG_FIELDS else 500, _gchat),
                "wrapText": True,
            }
        }
        for k, v in rows
    ]
    if link:
        widgets.append(
            {
                "buttonList": {
                    "buttons": [{"text": _LINK_TEXT, "onClick": {"openLink": {"url": link}}}]
                }
            }
        )
    header: dict[str, Any] = {"title": _fit(title, TITLE_MAX, _gchat_plain)}
    if site:
        header["subtitle"] = _fit(site, 200, _gchat_plain)
    card: dict[str, Any] = {"header": header}
    if widgets:
        card["sections"] = [{"widgets": widgets}]
    fallback = f"{title} · {site}" if site else title
    return {
        "text": _fit(fallback, 300, _gchat_plain),
        "cardsV2": [{"cardId": etype.replace(".", "-"), "card": card}],
    }


def render(
    platform: str,
    event: str,
    data: dict[str, Any] | None,
    template: Any = None,
    *,
    site_name: str = "",
    link: str = "",
    event_id: str = "",
    created_at: str = "",
) -> tuple[dict[str, Any], dict[str, str]]:
    """(JSON body, headers) for one delivery. ``event`` is an owner-facing name
    (``lead_captured``) or a wire type (``lead.captured``). The json headers are
    unsigned here; the outbox adds the signature."""
    etype = event_type(event)
    headers = {"Content-Type": "application/json"}
    d = dict(data or {})
    if platform == "json":
        return (
            webhook_signing.build_event(
                event_id=event_id or "sample",
                event_type=etype,
                created_at=created_at or datetime.now().astimezone().isoformat(),
                data=d,
            ),
            headers,
        )
    title_raw, fields, show_link = _template(template)
    title = _plain(title_raw or DEFAULT_TITLES.get(etype, "Notification"))
    site = _plain(site_name or d.get("site_name") or "")
    rows = lead_fields(etype, d, fields)
    url = link if show_link and link.startswith(("https://", "http://")) else ""
    if platform == "slack":
        return _slack_body(title, site, rows, url, etype), headers
    if platform == "discord":
        return _discord_body(title, site, rows, url, etype, _timestamp(d, created_at)), headers
    if platform == "teams":
        return _teams_body(title, site, rows, url, etype), headers
    if platform == "google_chat":
        return _gchat_body(title, site, rows, url, etype), headers
    raise ValueError(f"unknown platform {platform!r}")


__all__ = [
    "DEFAULT_TEMPLATE_FIELDS",
    "DEFAULT_TITLES",
    "EVENT_TYPES",
    "FIELD_LABELS",
    "TEMPLATE_FIELDS",
    "TITLE_MAX",
    "event_type",
    "lead_fields",
    "render",
    "sample_data",
]
