# ee/pocketpaw_ee/sites/verify_checks.py — the deterministic page checks ``verify_site``
# runs over a site's SERVED files, each reported under its ``sites-check`` rule id
# (src/pocketpaw/bundled_skills/_bundled/skills/sites-check/SKILL.md).
#
# WARN-ONLY. :func:`run_checks` returns ``{"warnings": [{id, page, detail[, pages]}],
# "omitted", "pages"}`` (or :func:`skipped`) and nothing here can fail a verdict:
# ``verify._with_checks`` attaches it as ``checks`` after the status is decided. A check
# that cannot tell says nothing rather than guess. The same defect on several pages is
# one warning with a ``pages`` count, so a shared footer cannot fill the cap alone.
#
# Checked: SC-LINK-01 anchor target (hash routes ``#/`` ``#!`` skipped), SC-LINK-02
# empty action (not on an anchor wired as a button), SC-LINK-03 internal path,
# SC-LINK-04 form with no target (only on pages with no script and no handlers),
# SC-COPY-01 em dash in visible copy (not the title, not aria-hidden), SC-HONEST-07
# the platform's placeholder tokens, lorem ipsum and example.com addresses,
# SC-HONEST-03 default people and fake numbers, SC-BUILD-12 title / description /
# og:title / og:description / favicon / generator (skipped on react, whose index.html
# the generator owns), SC-A11Y-06 exactly one h1.
#
# NOT CHECKED YET, though sites-check names them: og:image and Twitter cards, the 404
# page, the current-page nav marker, heading order, SC-COPY-02..09 (banned stems, "not
# X, it's Y", staged reveals, staccato runs), SC-LINK-05..07 and everything visual.
#
# Markup is read with the stdlib ``html.parser`` (no parser is in the deps), never
# regex over tags; regexes run only over extracted text. ``audit.py`` is the regex
# audit of AUTHORED source with fix prompts; the one definition shared with it is the
# empty-href set. No supplied-copy exemption: verify never sees the brief. Work is
# bounded by :data:`MAX_PAGES` and :data:`MAX_TOTAL_BYTES`; pages past either are
# counted in ``pages_skipped``.
from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote

from pocketpaw_ee.sites.audit import PLACEHOLDER_HREFS

#: The most warnings one verdict carries; the rest are counted in ``omitted``.
MAX_WARNINGS = 30

#: Pages and bytes read, so a huge tree cannot make verify slow.
MAX_PAGES = 200
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024

#: Engines whose single page can route on the client, so page links need not be files.
CLIENT_ROUTED_ENGINES = frozenset({"react", "svelte", "project"})

#: Report order: the defects a visitor hits first lead, so the cap drops the soft ones.
_ORDER = (
    "SC-LINK-01",
    "SC-LINK-02",
    "SC-LINK-03",
    "SC-LINK-04",
    "SC-HONEST-03",
    "SC-HONEST-07",
    "SC-COPY-01",
    "SC-A11Y-06",
    "SC-BUILD-12",
)

_SKIP_TEXT_TAGS = frozenset({"script", "style", "code", "pre", "template", "textarea"})
_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source"}
    | {"track", "wbr"}
)
_TEXT_ATTRS = ("alt", "title", "aria-label")
_ASSET_ATTRS = {
    "img": ("src",),
    "script": ("src",),
    "source": ("src",),
    "video": ("src", "poster"),
    "audio": ("src",),
    "iframe": ("src",),
    "link": ("href",),
}
_LINK_RELS_SKIPPED = frozenset({"canonical", "alternate", "preconnect", "dns-prefetch"})
_NON_EXEC_SCRIPT_TYPES = frozenset({"application/ld+json", "application/json", "importmap"})
#: Platform-owned paths a build or publish adds later (paw.js, Cloudflare).
_PLATFORM_SEGMENTS = frozenset({"_paw", "cdn-cgi"})
#: Paths a Worker or framework server answers, never files in the assets dir.
_SERVER_SEGMENTS = frozenset({"_next", "_image", "api", "_server", "_vercel"})
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")

_EM_DASH = "\u2014"
#: The placeholders the platform tells the agent to write (``_FACTS_RULE``, design-taste,
#: sites-check), never "any bracketed word": ``[sic]`` and ``[Smith 2020]`` are real.
_PLACEHOLDER_RE = re.compile(
    r"\[NEED:[^\]\n]{0,60}\]"
    r"|(?i:\[(?:phone|price|prices|client name|role|email|address|hours|name|company"
    r"|business name|metric to confirm|testimonial|logo)\])"
    r"|\bYour (?:Studio|Company|Business)\b"
    r"|(?i:\blorem ipsum\b)"
    r"|\b(?!johndoe@)[\w.+\-]+@example\.(?:com|org|net)\b"
)
_PROMPTONYM_RE = re.compile(
    r"\b(?:Sarah Chen|Elena Vasquez|Elara Voss|John Doe|Jane Doe)\b"
    r"|\bjohndoe@example\.com\b"
    r"|\b555[-.\s]?01\d{2}\b"
    r"|\(555\)\s?\d{3}[-.\s]?\d{4}"
)


def _clip(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class _PageParser(HTMLParser):
    """One pass over a page: ids, links, forms, scripts, head basics and text.

    ``text`` is every visible string (honesty checks); ``copy`` drops the title and
    aria-hidden subtrees (the em-dash check)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.anchors: list[tuple[str, list[str], dict[str, str]]] = []
        self.assets: list[tuple[str, str]] = []
        self.forms: list[dict[str, Any]] = []
        self.has_script = False
        self.has_handlers = False
        self.is_document = False
        self.redirect = False
        self.title: list[str] | None = None
        self.description: str | None = None
        self.og: set[str] = set()
        self.generator: str | None = None
        self.icons: list[str] = []
        self.h1 = 0
        self.text: list[str] = []
        self.copy: list[str] = []
        self.contact_refs: list[str] = []
        self._skip = 0
        self._in_title = False
        self._hidden_tag = ""
        self._hidden_depth = 0
        self._open_anchors: list[list[str]] = []
        self._open_form: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, closes=tag in _VOID_TAGS)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, closes=True)

    def _add_text(self, value: str) -> None:
        self.text.append(value)
        if not self._hidden_depth and not self._in_title:
            self.copy.append(value)

    def _start(self, tag: str, attrs: list[tuple[str, str | None]], *, closes: bool) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if self._hidden_depth and tag == self._hidden_tag and not closes:
            self._hidden_depth += 1
        hides = a.get("aria-hidden", "").lower() == "true"
        if hides and not self._hidden_depth and not closes:
            self._hidden_tag, self._hidden_depth = tag, 1
        for key in ("id", "name"):
            if a.get(key) and (key == "id" or tag in ("a", "map")):
                self.ids.add(a[key])
        if tag in ("html", "head", "body"):
            self.is_document = True
        if tag == "h1":
            self.h1 += 1
        if not self._skip and not hides:
            for name in _TEXT_ATTRS:
                if a.get(name):
                    self._add_text(a[name])
        if tag == "a" and "href" in a:
            label: list[str] = []
            self.anchors.append((a["href"], label, a))
            if a["href"].strip().lower().startswith(("mailto:", "tel:")):
                self.contact_refs.append(a["href"])
            if not closes:
                self._open_anchors.append(label)
        for attr in _ASSET_ATTRS.get(tag, ()):
            if not a.get(attr):
                continue
            if tag == "link" and _LINK_RELS_SKIPPED & set(a.get("rel", "").lower().split()):
                continue
            self.assets.append((tag, a[attr]))
        if tag == "script":
            if (a.get("type") or "").strip().lower() not in _NON_EXEC_SCRIPT_TYPES:
                self.has_script = True
        if any(k.startswith("on") for k in a):
            self.has_handlers = True
        if tag == "form":
            self._open_form = {"attrs": a, "formaction": False}
            self.forms.append(self._open_form)
            if closes:
                self._open_form = None
        elif self._open_form is not None and a.get("formaction"):
            self._open_form["formaction"] = True
        if tag == "title" and not closes:
            self.title = []
            self._in_title = True
        if tag == "meta":
            name = a.get("name", "").lower()
            if name == "description":
                self.description = a.get("content", "")
            elif name == "generator":
                self.generator = a.get("content", "") or "(empty)"
            prop = a.get("property", "").lower()
            if prop.startswith("og:") and a.get("content", "").strip():
                self.og.add(prop)
            if a.get("http-equiv", "").lower() == "refresh":
                self.redirect = True
        if tag == "link" and "icon" in a.get("rel", "").lower().split():
            self.icons.append(a.get("href", ""))
        if tag in _SKIP_TEXT_TAGS and not closes:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if self._hidden_depth and tag == self._hidden_tag:
            self._hidden_depth -= 1
        if tag in _SKIP_TEXT_TAGS and self._skip:
            self._skip -= 1
        elif tag == "a" and self._open_anchors:
            self._open_anchors.pop()
        elif tag == "form":
            self._open_form = None
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and self.title is not None:
            self.title.append(data)
        if self._skip:
            return
        self._add_text(data)
        for label in self._open_anchors:
            label.append(data)


def _decode(data: Any) -> str:
    if isinstance(data, bytes):
        return data[:MAX_PAGE_BYTES].decode("utf-8", "replace")
    return str(data)[:MAX_PAGE_BYTES]


def _is_page(name: str) -> bool:
    return name.lower().endswith((".html", ".htm"))


def _norm_files(files: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, data in files.items():
        rel = posixpath.normpath(str(name).replace("\\", "/").lstrip("/"))
        if rel not in (".", "") and not rel.startswith(".."):
            out[rel] = data
    return out


def _target(page: str, ref: str) -> str | None:
    """The tree path an internal reference points at ('' is the root), or ``None``
    when it is external, a fragment, a template token or outside the tree."""
    ref = ref.strip()
    if not ref or ref.startswith(("#", "?")) or ref.startswith("//"):
        return None
    if _SCHEME_RE.match(ref) or "__" in ref or "{" in ref or "$" in ref:
        return None
    path = unquote(ref.split("#", 1)[0].split("?", 1)[0])
    if not path:
        return None
    if path.startswith("/"):
        joined = path.lstrip("/")
    else:
        joined = posixpath.join(posixpath.dirname(page), path)
    norm = posixpath.normpath(joined) if joined else "."
    if norm.startswith(".."):
        return None
    norm = "" if norm == "." else norm
    if norm.split("/", 1)[0] in _PLATFORM_SEGMENTS or norm == "paw.js":
        return None
    return norm


def _resolve(tree: Mapping[str, Any], norm: str) -> str | None:
    """The file a target serves, by the preview's rules: exact, ``x/index.html``,
    ``x.html``."""
    candidates = ["index.html"] if norm == "" else [norm, f"{norm}/index.html", f"{norm}.html"]
    return next((c for c in candidates if c in tree), None)


def _has_extension(norm: str) -> bool:
    return "." in norm.rsplit("/", 1)[-1]


def _hash_route(fragment: str) -> bool:
    return fragment.startswith(("/", "!"))


def _wired_as_button(attrs: Mapping[str, str]) -> bool:
    """An anchor a script drives: ``role=button``, an ``on*`` handler or a
    ``data-*toggle*`` attribute. Its empty href is a pattern, not a dead link."""
    if attrs.get("role", "").lower() == "button":
        return True
    return any(k.startswith("on") or (k.startswith("data-") and "toggle" in k) for k in attrs)


def _hits(regex: re.Pattern[str], chunks: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for chunk in chunks:
        for m in regex.finditer(chunk):
            seen.setdefault(_clip(m.group(0), 40), None)
    return list(seen)


def _listed(items: list[str], limit: int = 6) -> str:
    shown = ", ".join(f'"{i}"' for i in items[:limit])
    return shown + (f" (+{len(items) - limit} more)" if len(items) > limit else "")


def _em_dash_detail(chunks: list[str]) -> str | None:
    count = 0
    example = ""
    for chunk in chunks:
        n = chunk.count(_EM_DASH)
        if n and not example:
            at = chunk.index(_EM_DASH)
            example = _clip(chunk[max(0, at - 30) : at + 30], 64)
        count += n
    if not count:
        return None
    noun = "em dash" if count == 1 else "em dashes"
    return f'{count} {noun} in visible text, e.g. "{example}"'


def _check_page(
    page: str,
    parsed: _PageParser,
    pages: Mapping[str, _PageParser],
    tree: Mapping[str, Any],
    *,
    engine: str,
    server_routes: bool,
    client_routed: bool,
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []

    def warn(rule: str, detail: str) -> None:
        out.append({"id": rule, "page": page, "detail": detail})

    missing_ids: list[str] = []
    empty: list[str] = []
    broken: list[str] = []
    for href, label_parts, attrs in parsed.anchors:
        label = _clip("".join(label_parts), 30) or "(no label)"
        value = href.strip()
        lowered = value.lower()
        if lowered in PLACEHOLDER_HREFS or lowered.startswith("javascript:"):
            if not _wired_as_button(attrs):
                empty.append(f"{label} -> {value or '(empty)'}")
            continue
        path, _, fragment = value.partition("#")
        fragment = unquote(fragment)
        if fragment and (_hash_route(fragment) or fragment.lower() == "top"):
            fragment = ""
            if not path:
                continue
        if not path:
            if fragment and fragment not in parsed.ids:
                missing_ids.append(f"#{fragment} ({label})")
            continue
        norm = _target(page, value)
        if norm is None:
            continue
        served = _resolve(tree, norm)
        if served is None:
            if server_routes or (client_routed and not _has_extension(norm)):
                continue
            broken.append(f"{value} ({label})")
        elif fragment and served in pages and fragment not in pages[served].ids:
            missing_ids.append(f"{value} ({label})")
    for _tag, ref in parsed.assets:
        norm = _target(page, ref)
        if norm is None or _resolve(tree, norm) is not None:
            continue
        if server_routes and norm.split("/", 1)[0] in _SERVER_SEGMENTS:
            continue
        broken.append(ref)
    if missing_ids:
        warn("SC-LINK-01", "no element with that id: " + _listed(missing_ids))
    if empty:
        warn("SC-LINK-02", "link goes nowhere: " + _listed(empty))
    if broken:
        warn("SC-LINK-03", "not in the build: " + _listed(sorted(set(broken), key=broken.index)))

    # Only a page with no script and no handlers can be judged: either may bind submit.
    if not parsed.has_script and not parsed.has_handlers:
        for form in parsed.forms:
            attrs = form["attrs"]
            action = attrs.get("action", "").strip()
            if action not in ("", "#") or form["formaction"]:
                continue
            if attrs.get("method", "").lower() == "dialog":
                continue
            if any(k.startswith(("hx-", "x-", "v-", "@", "data-paw")) for k in attrs):
                continue
            name = attrs.get("id") or attrs.get("name") or attrs.get("aria-label") or "form"
            warn(
                "SC-LINK-04",
                f"<form> '{_clip(name, 30)}' has no action and the page has no script, "
                "so a submit reloads the page and the entry is lost",
            )

    if people := _hits(_PROMPTONYM_RE, parsed.text + parsed.contact_refs):
        warn("SC-HONEST-03", "default name or fake contact: " + _listed(people))
    if holes := _hits(_PLACEHOLDER_RE, parsed.text + parsed.contact_refs):
        warn("SC-HONEST-07", "placeholder left (list it for the owner): " + _listed(holes))
    if dash := _em_dash_detail(parsed.copy):
        warn("SC-COPY-01", dash)

    if parsed.is_document and not parsed.redirect:
        if parsed.h1 != 1:
            warn("SC-A11Y-06", f"{parsed.h1} <h1> elements; a page has exactly one")
        # react's index.html is the generator's file: nothing in it is the agent's to fix.
        if engine != "react":
            basics = _basics(parsed, tree)
            if basics:
                warn("SC-BUILD-12", "; ".join(basics))
    return out


def _basics(parsed: _PageParser, tree: Mapping[str, Any]) -> list[str]:
    basics: list[str] = []
    if not "".join(parsed.title or []).strip():
        basics.append("no <title>")
    if not (parsed.description or "").strip():
        basics.append("no meta description")
    missing_og = [p for p in ("og:title", "og:description") if p not in parsed.og]
    if missing_og:
        basics.append("no " + " / ".join(missing_og))
    icons = [i for i in parsed.icons if i.strip()]
    if any(i.split("?", 1)[0].rstrip("/").endswith("vite.svg") for i in icons):
        basics.append("favicon is the scaffold vite.svg")
    elif not icons and not any(f in tree for f in ("favicon.ico", "favicon.svg")):
        basics.append("no favicon")
    if parsed.generator is not None:
        basics.append(f'generator meta tag left in ("{_clip(parsed.generator, 30)}")')
    return basics


def _consolidate(warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per ``(id, detail)``: the first page, plus ``pages`` when it repeats."""
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for w in warnings:
        key = (w["id"], w["detail"])
        if key in grouped:
            grouped[key]["pages"] = grouped[key].get("pages", 1) + 1
        else:
            grouped[key] = dict(w)
    return list(grouped.values())


def run_checks(
    files: Mapping[str, Any], *, engine: str = "html", server_routes: bool = False
) -> dict[str, Any]:
    """Every check over a served tree ``{relpath: bytes | str}``.

    ``server_routes`` is True when a Worker serves paths the tree does not hold, so a
    page link that is not a file, or an asset under a server path (``/_next``,
    ``/api``), is not reported. On a client-routed engine (react, svelte, project) a
    one-page tree with a script is treated the same way for page links. A tree with
    no pages at all is :func:`skipped` ``no_pages``.
    """
    from pocketpaw_ee.sites.verify_diagnostics import scrub_text

    tree = _norm_files(files)
    names = sorted((n for n in tree if _is_page(n)), key=lambda n: (n != "index.html", n))
    if not names:
        return skipped("no_pages")
    pages: dict[str, _PageParser] = {}
    budget = MAX_TOTAL_BYTES
    unread = 0
    for name in names:
        text = _decode(tree[name])
        if len(pages) >= MAX_PAGES or len(text) > budget:
            unread += 1
            continue
        budget -= len(text)
        parser = _PageParser()
        try:
            parser.feed(text)
            parser.close()
        except Exception:  # noqa: BLE001 — a page the parser chokes on is skipped
            unread += 1
            continue
        pages[name] = parser
    client_routed = (
        engine in CLIENT_ROUTED_ENGINES
        and len(pages) == 1
        and any(p.has_script for p in pages.values())
    )
    warnings: list[dict[str, Any]] = []
    for name, parsed in pages.items():
        warnings.extend(
            _check_page(
                name,
                parsed,
                pages,
                tree,
                engine=engine,
                server_routes=server_routes,
                client_routed=client_routed,
            )
        )
    rank = {rule: i for i, rule in enumerate(_ORDER)}
    warnings = _consolidate(warnings)
    warnings.sort(key=lambda w: rank.get(w["id"], len(rank)))
    kept = [{**w, "detail": scrub_text(w["detail"])} for w in warnings[:MAX_WARNINGS]]
    out: dict[str, Any] = {
        "warnings": kept,
        "omitted": max(0, len(warnings) - MAX_WARNINGS),
        "pages": len(pages),
    }
    if unread:
        out["pages_skipped"] = unread
        out["note"] = f"{unread} pages not checked (over the {MAX_PAGES}-page or 8 MB budget)"
    return out


def skipped(reason: str) -> dict[str, Any]:
    """The ``checks`` value when there was nothing to read."""
    return {"warnings": [], "omitted": 0, "pages": 0, "skipped": reason}


__all__ = ["MAX_TOTAL_BYTES", "MAX_WARNINGS", "run_checks", "skipped"]
