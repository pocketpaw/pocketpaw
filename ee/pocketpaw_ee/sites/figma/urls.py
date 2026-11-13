# ee/pocketpaw_ee/sites/figma/urls.py: Figma design links -> (file key, node id).
#
# Accepts only ``https://figma.com`` or ``https://www.figma.com`` (a scheme-less
# paste is read as https) with a ``/design/<key>`` or ``/file/<key>`` path; a
# branch link (``/design/<key>/branch/<branch>/...``) resolves to the branch key,
# which is what the REST API reads. ``node-id`` is taken from the query in its URL
# form (``12-34``) or API form (``12:34``) and returned in the API form. Every
# other host, scheme, userinfo, port or path kind (``proto``, ``board``, ``slides``)
# is refused, so nothing downstream ever builds a request from an arbitrary URL.
# ``find_links`` pulls every acceptable link out of free text (a chat message).

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

HOSTS = frozenset({"figma.com", "www.figma.com"})
_KEY_RE = re.compile(r"[A-Za-z0-9]{10,64}")
_NODE_RE = re.compile(r"(?:I?\d{1,10}[:-]\d{1,10})(?:;\d{1,10}[:-]\d{1,10}){0,8}")
_FIND_RE = re.compile(
    r"(?<![\w.@-])(?:https?://)?(?:www\.)?figma\.com/"
    r"(?:design|file)/[A-Za-z0-9]+[^\s<>\"'`)\]]*",
    re.IGNORECASE,
)
MAX_URL = 2048
MAX_LINKS = 10


class FigmaUrlError(ValueError):
    """The link is not a Figma design link. The message is safe to show."""


@dataclass(frozen=True)
class FigmaRef:
    file_key: str
    node_id: str = ""  # API form ("12:34"), or "" for the whole file


def _node(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    if not _NODE_RE.fullmatch(raw):
        raise FigmaUrlError("That link's node-id is not a Figma node id.")
    return raw.replace("-", ":")


def parse(url: str) -> FigmaRef:
    """The file key and node of a Figma design link, or ``FigmaUrlError``."""
    text = (url or "").strip()
    if not text or len(text) > MAX_URL:
        raise FigmaUrlError("Give a figma.com/design/... link.")
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise FigmaUrlError("That is not a valid link.") from None
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() != "https" or host not in HOSTS:
        raise FigmaUrlError("Only figma.com design links are read. Other sites are refused.")
    if parts.username or parts.password or port is not None:
        raise FigmaUrlError("Only figma.com design links are read. Other sites are refused.")
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2 or segments[0] not in ("design", "file"):
        raise FigmaUrlError(
            "Only Figma design files are read (figma.com/design/...), not prototypes, "
            "FigJam boards or slides."
        )
    key = segments[1]
    if len(segments) >= 4 and segments[2] == "branch":
        key = segments[3]
    if not _KEY_RE.fullmatch(key):
        raise FigmaUrlError("That link has no Figma file key.")
    query = parse_qs(parts.query)
    node = _node((query.get("node-id") or [""])[0])
    return FigmaRef(file_key=key, node_id=node)


def find_links(text: str) -> list[FigmaRef]:
    """Every acceptable Figma design link in ``text``, deduplicated, in order."""
    out: list[FigmaRef] = []
    for match in _FIND_RE.finditer(text or ""):
        try:
            ref = parse(match.group(0))
        except FigmaUrlError:
            continue
        if ref not in out:
            out.append(ref)
        if len(out) >= MAX_LINKS:
            break
    return out
