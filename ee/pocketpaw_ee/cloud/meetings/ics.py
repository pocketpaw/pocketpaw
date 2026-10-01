# Meetings — joining-info text and the .ics file for one meeting.
# Created: 2026-10-01 (feat/meetings-ics, MC-4). Pure functions, no model or
# service imports, so the service, the router and the calendar bridge share them.
#
#   * ``joining_info`` — the text a host pastes into an invite: title, the time in
#     UTC (a human line plus ISO instants; clients render local time themselves),
#     ``Join: <link>``, ``Meeting code: <code>`` and the description.
#   * ``build_ics`` — one VEVENT (RFC 5545): TEXT values escaped (backslash, ``;``,
#     ``,``, newlines as ``\n``), lines folded at 75 octets without splitting a
#     UTF-8 character, CRLF line ends, times as UTC ``...Z`` (never the server's
#     local zone). No ORGANIZER: its value must be a cal-address URI and the only
#     one we have is an email, which this file must not carry.

from __future__ import annotations

import re
from datetime import UTC, datetime

_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def utc(dt: datetime | None) -> datetime | None:
    """Aware UTC. Naive values are stored UTC (Mongo); aware ones are converted."""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _day(dt: datetime) -> str:
    # Name tables, not %A/%B: those follow the process locale.
    return f"{_DAYS[dt.weekday()]}, {dt.day} {_MONTHS[dt.month - 1]} {dt.year}"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _when(start: datetime | None, end: datetime | None) -> list[str]:
    start, end = utc(start), utc(end)
    if start is None:
        return []
    if end is None:
        return [f"{_day(start)} · {start:%H:%M} UTC", _iso(start)]
    if start.date() == end.date():
        human = f"{_day(start)} · {start:%H:%M}–{end:%H:%M} UTC"
    else:
        human = f"{_day(start)} · {start:%H:%M} UTC – {_day(end)} · {end:%H:%M} UTC"
    return [human, f"{_iso(start)} – {_iso(end)}"]


def joining_info(
    *,
    title: str | None,
    start: datetime | None,
    end: datetime | None,
    link: str,
    code: str,
    description: str | None,
) -> str:
    """Plain-text joining info. An undated meeting has no date lines."""
    lines = [title or "Meeting", *_when(start, end), f"Join: {link}", f"Meeting code: {code}"]
    if description:
        lines += ["", description]
    return "\n".join(lines)


def _escape(text: str) -> str:
    """RFC 5545 §3.3.11 TEXT. Backslash first, or the others get doubled."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """RFC 5545 §3.1: at most 75 octets a line; a continuation starts with a space."""
    parts: list[str] = []
    cur, size = "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > 75:
            parts.append(cur)
            cur, size = " ", 1
        cur += ch
        size += n
    parts.append(cur)
    return "\r\n".join(parts)


def _stamp(dt: datetime) -> str:
    return utc(dt).strftime("%Y%m%dT%H%M%SZ")  # type: ignore[union-attr]


def build_ics(
    *,
    uid: str,
    title: str,
    start: datetime,
    end: datetime,
    link: str,
    description: str,
    now: datetime,
) -> str:
    """One VEVENT calendar, CRLF-terminated."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//PocketPaw//Meetings//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{_stamp(now)}",
        f"DTSTART:{_stamp(start)}",
        f"DTEND:{_stamp(end)}",
        f"SUMMARY:{_escape(title)}",
        f"DESCRIPTION:{_escape(description)}",
        f"URL:{link}",  # URI value: not TEXT-escaped
        f"LOCATION:{_escape(link)}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "".join(_fold(line) + "\r\n" for line in lines)


def filename(title: str | None) -> str:
    """ASCII-only ``<slug>.ics`` for Content-Disposition."""
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:60].strip("-")
    return f"{slug or 'meeting'}.ics"
