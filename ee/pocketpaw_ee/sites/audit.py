# ee/pocketpaw_ee/sites/audit.py — the site-audit engine (BP-7, Branch primitive
# producer 2). A PURE, deterministic pass over a published site's source that
# surfaces fixable issues — each finding carries a ``fix_prompt`` the UI feeds to
# the EXISTING edit path (edit_svelte_component / refine), which lands the fix as
# a reviewable draft in the Tray. No DB, no network, no LLM in the deterministic
# core — every check is unit-testable over a sample svelte ``source`` map / HTML.
#
# Created: 2026-06-18 (feat/branch-primitive-audit, BP-7 backend half).
#
# Scope of the deterministic checks (the tested deliverable):
#   * a11y  — <img> without alt; <button>/<a> with no accessible text/aria-label;
#             missing or multiple <h1>; form inputs without an associated label.
#   * links — empty / placeholder hrefs (href="", href="#"), malformed hrefs.
#   * seo   — missing <title>; missing meta description; missing Open Graph basics
#             (og:title / og:image), checked across app.html + page <head>.
# Each finding: {id, check, tier, severity, location{file,hint}, message,
# fix_prompt}. ``fix_prompt`` is a concise refine instruction (e.g. "Add
# descriptive alt text to the hero image in Hero.svelte").
#
# The judgment tier (copy quality / SEO wording via a small LLM pass) is DEFERRED
# — see the TODO near the bottom. The deterministic checks stand alone and must
# never depend on or be gated by a live model.
#
# Updated 2026-07-10 (HE-2 — canonical engine module): the "does this engine carry a
# hand-written {path: contents} markup map?" checks (the source-map document scan,
# the markup-shape a11y pass, the heading-structure pass) now route through
# ``is_source_engine(engine)`` instead of ``== "svelte"``. These are markup-engine
# capabilities, not svelte-brand facts — the ripple branch (a flattened rippleSpec
# blob) is unchanged. Pure refactor, zero behaviour change for ripple/svelte.

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from pocketpaw_ee.sites.engines import is_source_engine


# ── Finding model ──────────────────────────────────────────────────────────────
@dataclass
class Finding:
    """One audit issue. ``check`` is the short check id (e.g. "a11y.img_alt");
    ``tier`` is "deterministic" for the rule-based core (or "judgment" for the
    deferred LLM pass); ``severity`` is "error" | "warning". ``location`` is a
    {file, hint} pointer the UI can show ("Hero.svelte" + a source snippet).
    ``fix_prompt`` is the concise refine instruction the UI sends to the existing
    edit path so the fix lands as a reviewable draft."""

    id: str
    check: str
    tier: str
    severity: str
    message: str
    fix_prompt: str
    location: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Source normalization ───────────────────────────────────────────────────────
# A svelte site is a {path: contents} map; a ripple site renders to a single HTML
# document. The deterministic checks operate over (path, html) pairs, so both
# engines normalize to the same list of "documents to scan".
_SCANNABLE_EXT = (".svelte", ".html", ".htm")


def _documents(engine: str, content: Any) -> list[tuple[str, str]]:
    """Normalize a pocket's content into a list of (file, html) pairs to scan.

    * svelte → every ``.svelte`` / ``.html`` file in the source map (skip .ts /
      .css / .js — they carry no markup the deterministic checks read).
    * ripple → a single ("rippleSpec", <rendered-text>) pair. The rippleSpec is a
      widget tree, not HTML, so we flatten its string values into one blob the
      text-level checks (links, copy) can still scan. Markup-shape checks (img
      alt, h1 count) are svelte-only — a ripple site has no hand-written markup.
    """
    if is_source_engine(engine) and isinstance(content, dict):
        docs: list[tuple[str, str]] = []
        for path, body in content.items():
            if isinstance(path, str) and path.lower().endswith(_SCANNABLE_EXT):
                docs.append((path, body if isinstance(body, str) else ""))
        return docs
    if engine == "ripple" and isinstance(content, dict):
        # Flatten every string leaf of the spec into one scannable blob so the
        # text-level checks (placeholder hrefs in a button's url, etc.) still run.
        return [("rippleSpec", _flatten_spec_text(content))]
    return []


def _flatten_spec_text(spec: Any) -> str:
    """Collect every string value in a rippleSpec tree into one newline-joined
    blob (depth-first). Used only for the engine-agnostic text-level checks."""
    out: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            out.append(node)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(spec)
    return "\n".join(out)


def _short_id(check: str, n: int) -> str:
    """Stable-ish finding id: ``<check>-<index>``. Deterministic per run so the
    same audit over the same content yields the same ids (the UI can de-dup)."""
    return f"{check}-{n}"


def _hint(snippet: str, limit: int = 120) -> str:
    """A trimmed one-line source snippet for the location hint."""
    flat = " ".join(snippet.split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


# ── Regexes (compiled once) ─────────────────────────────────────────────────────
_RE_IMG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_RE_HAS_ALT = re.compile(r"\balt\s*=", re.IGNORECASE)
_RE_BUTTON = re.compile(r"<button\b[^>]*>(.*?)</button>", re.IGNORECASE | re.DOTALL)
_RE_ANCHOR = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_RE_H1 = re.compile(r"<h1\b[^>]*>", re.IGNORECASE)
_RE_INPUT = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_RE_LABEL = re.compile(r"<label\b", re.IGNORECASE)
_RE_HREF = re.compile(r"href\s*=\s*(['\"])(.*?)\1", re.IGNORECASE)
_RE_ARIA_LABEL = re.compile(r"aria-label\s*=\s*(['\"]).*?\1", re.IGNORECASE)
_RE_TYPE_ATTR = re.compile(r"type\s*=\s*(['\"])(.*?)\1", re.IGNORECASE)
_RE_TITLE_TAG = re.compile(r"<title\b[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_RE_TITLE_PLACEHOLDER = re.compile(r"%[\w.]+%")  # svelte-kit %sveltekit.head% etc.
_RE_META_DESC = re.compile(r"<meta\b[^>]*\bname\s*=\s*(['\"])description\1[^>]*>", re.IGNORECASE)
_RE_META_OG = re.compile(r"<meta\b[^>]*\bproperty\s*=\s*(['\"])og:(title|image)\1", re.IGNORECASE)
# Strip markup tags + svelte blocks so "accessible text" reflects rendered text.
_RE_TAGS = re.compile(r"<[^>]+>")
_RE_SVELTE_EXPR = re.compile(r"\{[^}]*\}", re.DOTALL)


def _strip_text(inner: str) -> str:
    """Approximate the visible text of an element body: drop nested tags and
    svelte ``{...}`` expressions, collapse whitespace. A non-empty result means
    the element has accessible text content."""
    no_tags = _RE_TAGS.sub(" ", inner)
    no_expr = _RE_SVELTE_EXPR.sub(" ", no_tags)
    return no_expr.strip()


# ── Deterministic a11y checks ───────────────────────────────────────────────────
def _check_a11y(file: str, html: str, counter: dict[str, int]) -> list[Finding]:
    findings: list[Finding] = []

    # <img> without alt — every <img> must carry an alt attribute.
    for m in _RE_IMG.finditer(html):
        tag = m.group(0)
        if not _RE_HAS_ALT.search(tag):
            counter["a11y.img_alt"] = counter.get("a11y.img_alt", 0) + 1
            findings.append(
                Finding(
                    id=_short_id("a11y.img_alt", counter["a11y.img_alt"]),
                    check="a11y.img_alt",
                    tier="deterministic",
                    severity="error",
                    message="Image is missing an alt attribute (screen readers can't describe it).",
                    fix_prompt=(
                        f"Add descriptive alt text to the image in {file} "
                        f"({_hint(tag, 60)}). Describe what the image shows; "
                        'use alt="" only if it is purely decorative.'
                    ),
                    location={"file": file, "hint": _hint(tag)},
                )
            )

    # <button> with no accessible text and no aria-label.
    for m in _RE_BUTTON.finditer(html):
        opening = m.group(0).split(">", 1)[0]
        inner = m.group(1) or ""
        if not _strip_text(inner) and not _RE_ARIA_LABEL.search(opening):
            counter["a11y.button_name"] = counter.get("a11y.button_name", 0) + 1
            findings.append(
                Finding(
                    id=_short_id("a11y.button_name", counter["a11y.button_name"]),
                    check="a11y.button_name",
                    tier="deterministic",
                    severity="error",
                    message="Button has no accessible name (no text and no aria-label).",
                    fix_prompt=(
                        f"Give the button in {file} an accessible name: add visible text "
                        "or an aria-label describing its action "
                        f"({_hint(m.group(0), 60)})."
                    ),
                    location={"file": file, "hint": _hint(m.group(0))},
                )
            )

    # <a> with no accessible text and no aria-label (e.g. an icon-only link).
    for m in _RE_ANCHOR.finditer(html):
        attrs = m.group(1) or ""
        inner = m.group(2) or ""
        if not _strip_text(inner) and not _RE_ARIA_LABEL.search(attrs):
            counter["a11y.link_name"] = counter.get("a11y.link_name", 0) + 1
            findings.append(
                Finding(
                    id=_short_id("a11y.link_name", counter["a11y.link_name"]),
                    check="a11y.link_name",
                    tier="deterministic",
                    severity="error",
                    message="Link has no accessible name (no text and no aria-label).",
                    fix_prompt=(
                        f"Give the link in {file} an accessible name: add visible text "
                        "or an aria-label describing where it goes "
                        f"({_hint(m.group(0), 60)})."
                    ),
                    location={"file": file, "hint": _hint(m.group(0))},
                )
            )

    # Form <input> without an associated <label> in the same document. Heuristic:
    # any non-hidden input present while the document has zero <label> elements.
    inputs = [
        m
        for m in _RE_INPUT.finditer(html)
        if (
            lambda t: (
                not (
                    _RE_TYPE_ATTR.search(t)
                    and _RE_TYPE_ATTR.search(t).group(2).lower() in {"hidden", "submit", "button"}
                )
            )
        )(m.group(0))
    ]
    if inputs and not _RE_LABEL.search(html):
        counter["a11y.input_label"] = counter.get("a11y.input_label", 0) + 1
        findings.append(
            Finding(
                id=_short_id("a11y.input_label", counter["a11y.input_label"]),
                check="a11y.input_label",
                tier="deterministic",
                severity="warning",
                message="Form inputs have no associated <label> elements.",
                fix_prompt=(
                    f"Add a <label> for each form input in {file} (or an aria-label) "
                    "so the field's purpose is announced to assistive tech."
                ),
                location={"file": file, "hint": _hint(inputs[0].group(0))},
            )
        )

    return findings


def _check_h1(docs: list[tuple[str, str]], counter: dict[str, int]) -> list[Finding]:
    """Heading structure is a per-PAGE concern, not per-file (a page is built from
    many components). Count <h1> across all scannable docs: zero → missing,
    more than one → too many. Only meaningful for svelte (hand-written markup)."""
    total = 0
    first_file = ""
    for file, html in docs:
        n = len(_RE_H1.findall(html))
        if n and not first_file:
            first_file = file
        total += n

    findings: list[Finding] = []
    if total == 0 and docs:
        counter["a11y.h1_missing"] = counter.get("a11y.h1_missing", 0) + 1
        findings.append(
            Finding(
                id=_short_id("a11y.h1_missing", counter["a11y.h1_missing"]),
                check="a11y.h1_missing",
                tier="deterministic",
                severity="warning",
                message="Page has no <h1> heading (hurts a11y and SEO).",
                fix_prompt=(
                    "Add a single descriptive <h1> to the page's hero / top section "
                    "so the main heading is clear to readers, screen readers, and search engines."
                ),
                location={"file": docs[0][0], "hint": ""},
            )
        )
    elif total > 1:
        counter["a11y.h1_multiple"] = counter.get("a11y.h1_multiple", 0) + 1
        findings.append(
            Finding(
                id=_short_id("a11y.h1_multiple", counter["a11y.h1_multiple"]),
                check="a11y.h1_multiple",
                tier="deterministic",
                severity="warning",
                message=f"Page has {total} <h1> headings; it should have exactly one.",
                fix_prompt=(
                    f"The page has {total} <h1> headings (first in {first_file}). "
                    "Keep one <h1> for the main heading and demote the rest to <h2>/<h3>."
                ),
                location={"file": first_file, "hint": ""},
            )
        )
    return findings


# ── Deterministic link checks ───────────────────────────────────────────────────
_PLACEHOLDER_HREFS = {"", "#", "javascript:void(0)", "javascript:;"}


def _check_links(file: str, html: str, counter: dict[str, int]) -> list[Finding]:
    findings: list[Finding] = []
    for m in _RE_HREF.finditer(html):
        href = (m.group(2) or "").strip()
        # Skip svelte dynamic hrefs (href={...}) and templated values — those are
        # resolved at render time, not placeholders.
        if href.startswith("{") or _RE_TITLE_PLACEHOLDER.search(href):
            continue
        bad = href in _PLACEHOLDER_HREFS
        # Obviously-malformed: a scheme-looking prefix with nothing after it, or a
        # stray space inside a non-anchor URL.
        malformed = bool(re.match(r"^[a-z]+:$", href, re.IGNORECASE)) or (
            " " in href and not href.startswith("#") and "mailto:" not in href
        )
        if bad or malformed:
            counter["links.placeholder"] = counter.get("links.placeholder", 0) + 1
            findings.append(
                Finding(
                    id=_short_id("links.placeholder", counter["links.placeholder"]),
                    check="links.placeholder",
                    tier="deterministic",
                    severity="warning" if bad else "error",
                    message=(
                        f'Link has a placeholder href ("{href}") that goes nowhere.'
                        if bad
                        else f'Link has a malformed href ("{href}").'
                    ),
                    fix_prompt=(
                        f'Set a real destination for the link with href="{href}" in {file} '
                        f"({_hint(m.group(0), 50)}) — point it at the correct page, section "
                        "anchor, or external URL."
                    ),
                    location={"file": file, "hint": _hint(m.group(0))},
                )
            )
    return findings


# ── Deterministic SEO checks ────────────────────────────────────────────────────
def _check_seo(docs: list[tuple[str, str]], counter: dict[str, int]) -> list[Finding]:
    """SEO head tags are page-wide: scan every doc and the app shell together.
    A site is missing a tag only when NO scanned document carries it."""
    findings: list[Finding] = []
    if not docs:
        return findings

    blob = "\n".join(html for _, html in docs)
    # Where to point the fix: prefer the app shell (app.html) if present, else the
    # root page, else the first doc.
    head_file = next(
        (f for f, _ in docs if f.lower().endswith("app.html")),
        next((f for f, _ in docs if "+page.svelte" in f or "+layout.svelte" in f), docs[0][0]),
    )

    # <title> — present AND non-empty (ignore svelte-kit's %sveltekit.head% which
    # only RESERVES the slot; the title is set elsewhere via <svelte:head>).
    title_texts = [t.strip() for t in _RE_TITLE_TAG.findall(blob) if _strip_text(t).strip()]
    if not title_texts:
        counter["seo.title"] = counter.get("seo.title", 0) + 1
        findings.append(
            Finding(
                id=_short_id("seo.title", counter["seo.title"]),
                check="seo.title",
                tier="deterministic",
                severity="error",
                message="Site has no <title> (search results and browser tabs show no name).",
                fix_prompt=(
                    f"Add a concise, descriptive <title> in {head_file} (in <svelte:head> for a "
                    "svelte page, or app.html). Lead with the business / page name."
                ),
                location={"file": head_file, "hint": ""},
            )
        )

    # meta description.
    if not _RE_META_DESC.search(blob):
        counter["seo.meta_description"] = counter.get("seo.meta_description", 0) + 1
        findings.append(
            Finding(
                id=_short_id("seo.meta_description", counter["seo.meta_description"]),
                check="seo.meta_description",
                tier="deterministic",
                severity="warning",
                message="Site has no meta description (search engines summarize it for you).",
                fix_prompt=(
                    f'Add a <meta name="description" content="…"> in {head_file} with a '
                    "150–160 character summary of what the page offers."
                ),
                location={"file": head_file, "hint": ""},
            )
        )

    # Open Graph basics — need both og:title and og:image for a rich share card.
    og_props = {m.group(2).lower() for m in _RE_META_OG.finditer(blob)}
    missing_og = [p for p in ("title", "image") if p not in og_props]
    if missing_og:
        counter["seo.open_graph"] = counter.get("seo.open_graph", 0) + 1
        findings.append(
            Finding(
                id=_short_id("seo.open_graph", counter["seo.open_graph"]),
                check="seo.open_graph",
                tier="deterministic",
                severity="warning",
                message=(
                    "Site is missing Open Graph tags ("
                    + ", ".join(f"og:{p}" for p in missing_og)
                    + ") so shared links have no preview card."
                ),
                fix_prompt=(
                    f"Add Open Graph meta tags in {head_file}: "
                    + ", ".join(f'<meta property="og:{p}" content="…">' for p in missing_og)
                    + " so links shared on social show a title and image."
                ),
                location={"file": head_file, "hint": ""},
            )
        )

    return findings


# ── Public entry point ──────────────────────────────────────────────────────────
def audit_pocket_site(*, engine: str, content: Any) -> list[dict[str, Any]]:
    """Run the deterministic audit over a site's content and return findings.

    ``engine`` is "svelte" or "ripple"; ``content`` is the matching content the
    service read for the pocket (the {path: contents} svelte source map, or the
    rippleSpec dict). Returns a list of finding dicts (see :class:`Finding`).
    A clean site returns an empty list. Pure — no I/O, no model — so the whole
    surface is unit-testable over a sample content map.
    """
    docs = _documents(engine, content)
    counter: dict[str, int] = {}
    findings: list[Finding] = []

    # Per-document checks (markup-shape a11y + links). a11y only applies to engines
    # with hand-written markup (source-map engines); a ripple site has none.
    for file, html in docs:
        if is_source_engine(engine):
            findings.extend(_check_a11y(file, html, counter))
        findings.extend(_check_links(file, html, counter))

    # Page-wide checks (heading structure needs hand-written markup; SEO spans the shell).
    if is_source_engine(engine):
        findings.extend(_check_h1(docs, counter))
    findings.extend(_check_seo(docs, counter))

    # TODO(BP-7 judgment tier): a small, clearly-separated copy-quality / SEO-
    # wording pass (e.g. "the hero headline is generic", "the meta description is
    # keyword-stuffed") via a single LLM call, tier="judgment". DEFERRED for the
    # backend deliverable: it cannot run without live-LLM flakiness, and the spec
    # is explicit that it must never gate or flake the deterministic core. When
    # added it appends tier="judgment" findings here AFTER the deterministic ones,
    # behind an opt-in flag, so a model outage degrades to the deterministic list.

    return [f.to_dict() for f in findings]
