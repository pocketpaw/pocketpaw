# ee/pocketpaw_ee/paw_bar/action_spec.py — validate the one page action a v2 concierge may suggest.
#
# A site with ``Site.concierge_page_actions`` on lets the v2 model end a reply
# with ONE ```pawbar-action fence: {"do": ..., "to"?: ..., "target"?: ...,
# "label": ...}. The model still has no tools; the fence is only a suggestion.
# ``FenceFilter`` (concierge_runtime.py) hands each such fence to
# ``render_action``, which returns the action the runner streams as the SSE
# ``action`` frame, or None to drop it (logged, never shown):
#
#   * ``navigate`` needs ``to``: a site path or absolute http(s) url that resolves
#     to ``site_origin`` AND names a known page (a crawled page or a catalog
#     item's url), compared on origin + path with a trailing slash ignored. The
#     emitted ``to`` is always absolute, query dropped, an ``#id`` fragment kept.
#   * ``scroll_to`` / ``highlight`` need ``target``: ``#id`` (``TARGET_ID_RE``) or
#     heading text, one line, at most ``TARGET_MAX`` chars, no ``<`` or ``>``.
#   * ``label`` is required everywhere: plain one-line text, at most ``LABEL_MAX``.
#
# The host script (paw-bar ``actions/``) checks again on the page; the server is
# the authority on which pages exist. ``site_pages`` lists the pages the prompt
# offers and ``known_urls`` is what ``render_action`` accepts; both read only the
# crawl index and the turn's catalog items. Verdicts shared with paw-bar live in
# tests/fixtures/action_parity/ (cases.json + expected.json, the same files as
# paw-bar's app/tests/fixtures/action_parity); server-only ones in
# server_cases.json beside them.

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Sequence
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit

logger = logging.getLogger(__name__)

VERBS: tuple[str, ...] = ("navigate", "scroll_to", "highlight")
LABEL_MAX = 80
TARGET_MAX = 120
# The longest fence body and ``to`` read at all.
BODY_MAX = 2_000
URL_MAX = 2_048
TARGET_ID_RE = re.compile(r"#[A-Za-z][A-Za-z0-9_-]{0,63}")
# Pages the prompt lists, at most: crawled pages, then catalog items with a url.
PROMPT_PAGES = 40
PROMPT_PRODUCT_PAGES = 20

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_PATH_SAFE = "/-._~!$&'()*+,;=:@"
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _drop(reason: str) -> None:
    logger.info("concierge: dropped a pawbar-action (%s)", reason)


def origin_of(url: str) -> str:
    """``scheme://host[:port]`` of an absolute http(s) url, lower-cased, default
    port dropped; "" for anything else."""
    split = _split(url)
    return split[0] if split else ""


def _split(url: str) -> tuple[str, str, str] | None:
    """(origin, path without trailing slash, fragment) of an absolute http(s)
    url, or None. The path is re-quoted so two spellings of one path compare
    equal; the homepage's path is ""."""
    try:
        parts = urlsplit((url or "").strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    netloc = host if port in (None, _DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    path = quote(unquote(parts.path or "/"), safe=_PATH_SAFE).rstrip("/")
    return f"{scheme}://{netloc}", path, parts.fragment


def _resolve(ref: str, site_origin: str) -> tuple[str, str, str] | None:
    """``ref`` (a site path or an absolute url) resolved against ``site_origin``,
    split; None when it is not on that origin."""
    if not site_origin:
        return None
    split = _split(urljoin(site_origin + "/", (ref or "").strip()))
    if split is None or split[0] != site_origin:
        return None
    return split


def _catalog_paths(catalog: Sequence[Any], site_origin: str) -> list[tuple[str, str]]:
    """``(path, name)`` for each catalog item whose url is on ``site_origin``."""
    out: list[tuple[str, str]] = []
    for item in catalog or ():
        raw = str(getattr(item, "url", "") or "").strip()
        split = _resolve(raw, site_origin) if raw else None
        if split is not None:
            out.append((split[1] or "/", str(getattr(item, "name", "") or "")))
    return out


def known_urls(site: Any, catalog: Sequence[Any], site_origin: str) -> list[str]:
    """Every page a ``navigate`` may name: each crawled page (``kb_page_index``)
    and each catalog item url on ``site_origin``, as absolute urls."""
    if not site_origin:
        return []
    crawled = [f"{site_origin}/{key}" for key in (getattr(site, "kb_page_index", None) or {})]
    return crawled + [site_origin + path for path, _ in _catalog_paths(catalog, site_origin)]


def site_pages(site: Any, catalog: Sequence[Any], site_origin: str) -> list[tuple[str, str]]:
    """``(path, title)`` for the pages the prompt offers: up to
    ``PROMPT_PRODUCT_PAGES`` of the turn's catalog items with a url on
    ``site_origin`` (titled by name), and crawled pages in key order filling the
    rest of ``PROMPT_PAGES``. Crawled pages are listed first."""
    if not site_origin:
        return []
    index = getattr(site, "kb_page_index", None) or {}
    crawled = [
        (f"/{key}", str(entry.get("title") or "") if isinstance(entry, dict) else "")
        for key, entry in sorted(index.items())
    ]
    seen = {path.rstrip("/") for path, _ in crawled}
    products: list[tuple[str, str]] = []
    for path, name in _catalog_paths(catalog, site_origin):
        if len(products) >= PROMPT_PRODUCT_PAGES:
            break
        if path.rstrip("/") not in seen:
            seen.add(path.rstrip("/"))
            products.append((path, name))
    return crawled[: PROMPT_PAGES - len(products)] + products


def _label(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text or len(text) > LABEL_MAX or _CONTROL_RE.search(text) or "<" in text or ">" in text:
        return None
    return text


def _target(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if text.startswith("#"):
        return text if TARGET_ID_RE.fullmatch(text) else None
    if not text or len(text) > TARGET_MAX or _CONTROL_RE.search(text) or "<" in text or ">" in text:
        return None
    return text


def _navigate_to(value: Any, site_origin: str, known: Iterable[str]) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or len(raw) > URL_MAX or "\\" in raw or _CONTROL_RE.search(raw):
        return None
    if any(ch.isspace() for ch in raw):
        return None
    split = _resolve(raw, site_origin)
    if split is None:
        return None
    pages = {s[:2] for k in known if (s := _resolve(k, site_origin)) is not None}
    if split[:2] not in pages:
        return None
    origin, path, fragment = split
    url = f"{origin}{path or '/'}"
    if fragment and TARGET_ID_RE.fullmatch(f"#{fragment}"):
        url += f"#{fragment}"
    return url


def render_action(body: str, *, site_origin: str, known_urls: Iterable[str]) -> dict | None:
    """The action a ```pawbar-action fence body asks for, validated, or None.

    ``site_origin`` is the origin the visitor's page is on (``origin_of``); ""
    refuses every ``navigate``. ``known_urls`` are the pages a ``navigate`` may
    name, absolute or as site paths. The result has ``do`` and ``label``, plus
    ``to`` (absolute) for navigate or ``target`` for scroll_to / highlight; any
    other key the model wrote is dropped."""
    if not isinstance(body, str) or len(body) > BODY_MAX:
        _drop("body too long")
        return None
    try:
        data = json.loads(body)
    except ValueError:
        _drop("not json")
        return None
    if not isinstance(data, dict):
        _drop("not an object")
        return None
    verb = data.get("do")
    if verb not in VERBS:
        _drop(f"unknown verb {str(verb)[:40]!r}")
        return None
    label = _label(data.get("label"))
    if label is None:
        _drop("bad label")
        return None
    if verb == "navigate":
        to = _navigate_to(data.get("to"), (site_origin or "").lower(), known_urls)
        if to is None:
            _drop("navigate to an unknown or foreign page")
            return None
        return {"do": verb, "to": to, "label": label}
    target = _target(data.get("target"))
    if target is None:
        _drop("bad target")
        return None
    return {"do": verb, "target": target, "label": label}


__all__ = [
    "LABEL_MAX",
    "TARGET_ID_RE",
    "TARGET_MAX",
    "VERBS",
    "known_urls",
    "origin_of",
    "render_action",
    "site_pages",
]
