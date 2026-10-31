# ee/pocketpaw_ee/sites/verify_checks.py — the deterministic page checks ``verify_site``
# runs over a site's SERVED files, each reported under its ``sites-check`` rule id
# (src/pocketpaw/bundled_skills/_bundled/skills/sites-check/SKILL.md).
#
# NEVER A VERDICT. :func:`run_checks` returns ``{"warnings": [{id, page, detail[, pages]
# [, level, items]}], "omitted", "pages"}`` (or :func:`skipped`) and nothing here can
# fail a verdict: ``verify._with_checks`` attaches it as ``checks`` after the status is
# decided. Entries with ``level: "error"`` (and ``items``, the exact text) BLOCK A
# PUBLISH instead (``publish_gate``, read via :func:`blocking`); they sort first so the
# cap never drops one. A check that cannot tell says nothing rather than guess. The same
# defect on several pages is one entry with a ``pages`` count.
#
# Blocking: SC-LINK-04 a form that submits back to its own page with nothing to receive
# it (see :func:`_dead_forms` for what counts as handled: conservative on scripted
# pages), SC-HONEST-07 placeholder copy ([Address], [Phone], lorem ipsum, example.com
# addresses, "Your Name" outside labels, XXXXX XXXXX), SC-HONEST-03 made-up phone
# numbers (sequential or repeated digits, 555, 07700 900xxx; see :func:`_invented`).
# Warn: SC-LINK-01 anchor target (hash routes ``#/`` ``#!`` skipped), SC-LINK-02
# empty action (not on an anchor wired as a button), SC-LINK-03 internal path,
# SC-COPY-01 em dash in visible copy (not the title, not aria-hidden),
# SC-HONEST-03 default people, SC-BUILD-12 title / description /
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
    r"|(?i:\[(?:phone|phone number|price|prices|client name|role|email|email address"
    r"|address|hours|opening hours|name|company|business name|metric to confirm"
    r"|testimonial|logo|city|postcode|zip|zip code|whatsapp|whatsapp number|instagram"
    r"|website|street|owner name)\])"
    r"|(?i:\[your [^\]\n]{1,30}\])"
    r"|\bYour (?:Studio|Company|Business)\b"
    r"|(?i:\blorem ipsum\b)"
    r"|\b(?!johndoe@)[\w.+\-]+@example\.(?:com|org|net)\b"
    r"|(?<!\w)X{3,5}[\s.\-]X{3,5}(?:[\s.\-]X{2,5})?(?!\w)"
)
#: "Your Name" as copy ("Your Name, founder"). Read over text outside labels, legends,
#: buttons and options, where "Your name" is a field prompt, not a hole.
_YOUR_X_RE = re.compile(r"\bYour (?:Name|Address|City|Email|Phone(?: Number)?)\b")
_PROMPTONYM_RE = re.compile(
    r"\b(?:Sarah Chen|Elena Vasquez|Elara Voss|John Doe|Jane Doe)\b"
    r"|\bjohndoe@example\.com\b"
)
#: A run that could be a phone number: digits with spaces, dots, dashes or brackets
#: between. :func:`_invented` decides whether it is made up.
_PHONE_RE = re.compile(r"(?<![\w+#(])\+?\(?\d[\d\s().\-]{5,18}\d(?![\w-])")
#: Text inside these is a field prompt or a control label, never placeholder copy.
_PROMPT_TAGS = frozenset({"label", "legend", "button", "option", "select"})
#: Form attributes a script, framework or platform binds submit through.
_FORM_HOOK_PREFIXES = ("on", "hx-", "x-", "v-", "@", ":", "data-", "wire:", "up-", "netlify")
#: Field names that collect a visitor's details (the entry a dead form loses).
_COLLECT_NAME_RE = re.compile(
    r"e-?mail|phone|mobile|whatsapp|\btel\b|message|comment|enquiry|inquiry|address|name",
    re.IGNORECASE,
)
_COLLECT_TYPES = frozenset({"email", "tel", "password", "textarea", "file"})
#: Script text that can bind a form's submit without naming the form.
_SUBMIT_HINT_RE = re.compile(
    r"submit|formdata|document\.forms|urlsearchparams|location\.search|serialize",
    re.IGNORECASE,
)
#: Script bytes read across a tree when looking for a submit binding; past it, a form
#: on a scripted page is assumed bound.
MAX_SCRIPT_BYTES = 4 * 1024 * 1024


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
        #: Visible text outside labels, legends, buttons and options (``_YOUR_X_RE``).
        self.copy_text: list[str] = []
        #: Inline executable script bodies, external script srcs, and ``on*`` values.
        self.scripts: list[str] = []
        self.script_srcs: list[str] = []
        self.handler_code: list[str] = []
        self._skip = 0
        self._prompt_depth = 0
        self._in_script = False
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
        if not self._prompt_depth:
            self.copy_text.append(value)
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
            href = a["href"].strip().lower()
            if href.startswith(("mailto:", "tel:", "sms:")) or _whatsapp_link(href):
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
                if a.get("src", "").strip():
                    self.script_srcs.append(a["src"].strip())
                elif not closes:
                    self._in_script = True
        handlers = [v for k, v in a.items() if k.startswith("on")]
        if handlers:
            self.has_handlers = True
        if tag == "form":
            self._open_form = {"attrs": a, "formaction": False, "fields": [], "handled": False}
            self.forms.append(self._open_form)
            if closes:
                self._open_form = None
        elif self._open_form is not None:
            if a.get("formaction"):
                self._open_form["formaction"] = True
            if handlers:
                self._open_form["handled"] = True
            if tag in ("input", "textarea", "select"):
                kind = "textarea" if tag == "textarea" else (a.get("type") or "text").lower()
                self._open_form["fields"].append((kind, a.get("name", "") or a.get("id", "")))
        if handlers and (self._open_form is None or tag == "form"):
            self.handler_code.extend(handlers)
        if tag in _PROMPT_TAGS and not closes:
            self._prompt_depth += 1
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
        if tag in _PROMPT_TAGS and self._prompt_depth:
            self._prompt_depth -= 1
        if tag == "script":
            self._in_script = False
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
        if self._in_script:
            self.scripts.append(data)
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


def _whatsapp_link(href: str) -> bool:
    return "wa.me/" in href or ("whatsapp.com/" in href and "phone=" in href)


def _national(digits: str) -> str:
    """The national part of a number written with a +91 / +44 / +1 prefix."""
    if len(digits) == 12 and digits.startswith(("91", "44")):
        return digits[2:] if digits.startswith("91") else "0" + digits[2:]
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    return digits


def _longest_run(digits: str) -> int:
    """The longest run of consecutive digits stepping by one, up or down
    (``1234567890`` and ``9876543210`` are runs of 10)."""
    best = up = down = 1
    for a, b in zip(digits, digits[1:], strict=False):
        up = up + 1 if (int(b) - int(a)) % 10 == 1 else 1
        down = down + 1 if (int(a) - int(b)) % 10 == 1 else 1
        best = max(best, up, down)
    return best


def _invented(raw: str) -> bool:
    """A phone number nobody was given: sequential or repeated digits, a US 555 number,
    or the UK's drama mobile range (07700 900xxx). These ring real strangers or nobody."""
    digits = re.sub(r"\D", "", raw)
    if not 7 <= len(digits) <= 15:
        return False
    national = _national(digits)
    if len(national) >= 9 and (len(set(national)) == 1 or re.search(r"(\d)\1{7}", national)):
        return True
    if len(digits) >= 9 and _longest_run(digits) >= 8:
        return True
    if len(national) == 7 and national.startswith("55501"):
        return True
    if len(national) == 10 and national.startswith("555"):
        return True
    return len(national) == 11 and national.startswith("07700900")


def _phone_hits(chunks: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for chunk in chunks:
        for m in _PHONE_RE.finditer(chunk):
            if _invented(m.group(0)):
                seen.setdefault(_clip(m.group(0).strip(), 40), None)
    return list(seen)


def _self_action(page: str, action: str, tree: Mapping[str, Any]) -> bool:
    """The form submits back to the page it is on: no action, ``#``, a bare query, or
    a path that serves this same page."""
    if not action or action.startswith(("#", "?")):
        return True
    norm = _target(page, action)
    return norm is not None and _resolve(tree, norm) == page


def _collects(form: Mapping[str, Any]) -> bool:
    """The form asks for a visitor's details (email, phone, a message, a name)."""
    for kind, name in form["fields"]:
        if kind in ("hidden", "submit", "button", "reset", "image", "search"):
            continue
        if kind in _COLLECT_TYPES or _COLLECT_NAME_RE.search(name):
            return True
    return False


def _is_search(form: Mapping[str, Any]) -> bool:
    if form["attrs"].get("role", "").lower() == "search":
        return True
    return any(kind == "search" for kind, _name in form["fields"])


class _ScriptCorpus:
    """Every script file in the tree, read once and only when a page needs it."""

    def __init__(self, tree: Mapping[str, Any]) -> None:
        self._tree = tree
        self._text: str | None = None
        self._over = False

    def text(self) -> str | None:
        """All the tree's own JS (``None`` when it is over :data:`MAX_SCRIPT_BYTES`)."""
        if self._text is None and not self._over:
            parts: list[str] = []
            size = 0
            for name, data in self._tree.items():
                if not name.lower().endswith((".js", ".mjs", ".cjs")):
                    continue
                if name.split("/", 1)[0] in _PLATFORM_SEGMENTS or name == "paw.js":
                    continue
                body = _decode(data)
                size += len(body)
                if size > MAX_SCRIPT_BYTES:
                    self._over = True
                    return None
                parts.append(body)
            self._text = "\n".join(parts)
        return None if self._over else self._text


def _page_js(page: str, parsed: _PageParser, corpus: _ScriptCorpus) -> str | None:
    """The script text that runs on ``page``, or ``None`` when some of it cannot be
    read (a script from another host, a server path, an over-budget tree): then any
    form there may be bound and none is judged. The platform's ``/_paw/paw.js`` only
    binds ``data-paw-*`` forms, which count as handled on their own, so it is skipped."""
    parts = [*parsed.scripts, *parsed.handler_code]
    needs_tree = any("import" in s for s in parsed.scripts)
    for src in parsed.script_srcs:
        path = src.split("?", 1)[0].split("#", 1)[0]
        if "/_paw/" in f"/{path.lstrip('/')}" or path.rsplit("/", 1)[-1] == "paw.js":
            continue
        if _target(page, src) is None:
            return None
        needs_tree = True
    if needs_tree:
        tree_js = corpus.text()
        if tree_js is None:
            return None
        parts.append(tree_js)
    return "\n".join(parts)


def _dead_forms(
    page: str,
    parsed: _PageParser,
    tree: Mapping[str, Any],
    corpus: _ScriptCorpus,
    *,
    server_routes: bool,
) -> list[tuple[str, str]]:
    """``(form name, method)`` for each form whose submit goes nowhere useful: back to
    this page with nothing to receive it. Handled, and never reported: a real action
    or ``formaction``, ``method=dialog``, an ``on*`` / ``data-*`` / framework hook on the
    form or a handler inside it, a POST to the page on a site with a Worker, and a GET
    on such a site unless it collects details. On a page with script, only a form that
    collects details is judged, and only when no script can bind it (no ``submit`` /
    ``FormData`` / ``document.forms`` text and no mention of its id or name)."""
    scripted = parsed.has_script or bool(parsed.handler_code)
    page_js: str | None = ""
    if scripted:
        page_js = _page_js(page, parsed, corpus)
    out: list[tuple[str, str]] = []
    for form in parsed.forms:
        attrs = form["attrs"]
        method = attrs.get("method", "").strip().lower() or "get"
        if method == "dialog" or form["formaction"] or form["handled"]:
            continue
        if any(k.startswith(_FORM_HOOK_PREFIXES) for k in attrs):
            continue
        if not _self_action(page, attrs.get("action", "").strip(), tree):
            continue
        collects = _collects(form)
        if server_routes and (method == "post" or not collects):
            continue
        if scripted:
            if page_js is None or not collects or _is_search(form):
                continue
            if _SUBMIT_HINT_RE.search(page_js):
                continue
            if any(attrs.get(k) and attrs[k] in page_js for k in ("id", "name")):
                continue
        name = attrs.get("id") or attrs.get("name") or attrs.get("aria-label") or "form"
        out.append((_clip(name, 30), method))
    return out


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
    corpus: _ScriptCorpus,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def warn(rule: str, detail: str) -> None:
        out.append({"id": rule, "page": page, "detail": detail})

    def block(rule: str, detail: str, items: list[str]) -> None:
        out.append({"id": rule, "page": page, "detail": detail, "level": "error", "items": items})

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

    for name, method in _dead_forms(page, parsed, tree, corpus, server_routes=server_routes):
        lost = (
            "the entry ends up in the address bar"
            if method == "get"
            else "nothing on the site receives the POST"
        )
        block(
            "SC-LINK-04",
            f"<form> '{name}' submits back to this page ({method.upper()}) and nothing "
            f"handles it, so {lost} and is lost. POST it to a real handler (a server "
            "route, a data-paw-* hook or a submit script) and exercise it",
            [name],
        )

    if people := _hits(_PROMPTONYM_RE, parsed.text + parsed.contact_refs):
        warn("SC-HONEST-03", "default name or fake contact: " + _listed(people))
    if numbers := _phone_hits(parsed.text + parsed.contact_refs):
        block(
            "SC-HONEST-03",
            "made-up phone number (it may ring a real stranger; ask the owner for theirs): "
            + _listed(numbers),
            numbers,
        )
    holes = _hits(_PLACEHOLDER_RE, parsed.text + parsed.contact_refs)
    holes += [h for h in _hits(_YOUR_X_RE, parsed.copy_text) if h not in holes]
    if holes:
        block(
            "SC-HONEST-07",
            "placeholder left (ask the owner for the real detail): " + _listed(holes),
            holes,
        )
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
    corpus = _ScriptCorpus(tree)
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
                corpus=corpus,
            )
        )
    rank = {rule: i for i, rule in enumerate(_ORDER)}
    warnings = _consolidate(warnings)
    # Publish-blocking entries lead, so the cap never drops one.
    warnings.sort(key=lambda w: (w.get("level") != "error", rank.get(w["id"], len(rank))))
    kept = [_scrubbed(w, scrub_text) for w in warnings[:MAX_WARNINGS]]
    out: dict[str, Any] = {
        "warnings": kept,
        "omitted": max(0, len(warnings) - MAX_WARNINGS),
        "pages": len(pages),
    }
    if unread:
        out["pages_skipped"] = unread
        out["note"] = f"{unread} pages not checked (over the {MAX_PAGES}-page or 8 MB budget)"
    return out


def _scrubbed(entry: dict[str, Any], scrub: Any) -> dict[str, Any]:
    out = {**entry, "detail": scrub(entry["detail"])}
    if "items" in out:
        out["items"] = [scrub(i) for i in out["items"]]
    return out


def blocking(checks: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The entries of a ``checks`` value that block a publish (``level: "error"``)."""
    if not isinstance(checks, Mapping):
        return []
    return [w for w in checks.get("warnings") or () if w.get("level") == "error"]


def skipped(reason: str) -> dict[str, Any]:
    """The ``checks`` value when there was nothing to read."""
    return {"warnings": [], "omitted": 0, "pages": 0, "skipped": reason}


__all__ = ["MAX_TOTAL_BYTES", "MAX_WARNINGS", "blocking", "run_checks", "skipped"]
