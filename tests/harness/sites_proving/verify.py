"""``verify(bundle)`` — the lane-independent replacement for the workerd smoke gate.

Created for SG-1 (sites proving harness).

WHAT: parses the entry HTML and asserts four things, raising ``VerifyFailed`` on
the first violation:

1. the entry HTML parses and has a ``<body>`` with real content (not just
   comments and whitespace),
2. every internal link resolves — to a file in the bundle, or to an in-page
   anchor that exists,
3. the lead form's ``<form action=...>`` survived the render,
4. no error markers leaked into the output.

WHY it replaces the workerd gate: the old gate proved a page renders by BUILDING
a SvelteKit project and booting workerd — 45-60s, and it can only judge the one
lane that produces a Cloudflare worker. These four assertions are about the HTML
itself, so the same function judges any lane and any rung, in milliseconds.

WHY it must FAIL CLOSED: verify is the only thing between a broken render and a
deploy. It raises rather than returning a verdict so a caller cannot accidentally
proceed by ignoring a falsy return — a missed ``if not ok`` would publish a blank
site. Every path out of a failed check raises.

Deliberately NOT checked here (later slices own these): resting visibility
(paw-sites' checkRestingVisibility, which needs the CSS pipeline SG-1 leaves out
of scope), hydration, and live form submission.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

from .bundle import Bundle

# Markers ripple/Svelte emit when a render goes wrong but does not throw.
# NodeRenderer's loud red box for an out-of-catalog node type is the main one — a
# spec full of unknown widgets renders "successfully" and passes a length check,
# so it must be matched explicitly.
#
# The first entry is the one that matters: `data-ripple-unknown-widget` is the
# attribute NodeRenderer.svelte stamps on that box, verified against ripple
# 0.5.0's dist. Matching the ATTRIBUTE rather than the prose means a copy edit
# upstream cannot silently disarm this check. The visible sentence is kept as a
# corroborating match, and `svelte-ssr-error` covers a Svelte-level failure.
#
# `class="ripple-empty"` is the third and subtlest: Ripple.svelte emits it for a
# spec it understood but that defines no UI ("No UI definition for intent: X").
# Found while building this slice — a `{intent:'custom'}` spec with no `ui` key
# renders that placeholder as real TEXT, so the empty-body check counts it as
# content and PASSED it. That is a blank site verifying green, which is exactly
# the failure this gate exists to stop.
_ERROR_MARKERS: tuple[str, ...] = (
    "data-ripple-unknown-widget",
    "isn't in the catalog",
    "svelte-ssr-error",
    'class="ripple-empty"',
    "No UI definition for intent",
)

# Schemes that are legitimately not bundle-internal, so link resolution skips
# them. mailto/tel are the two a marketing page actually uses.
_EXTERNAL_SCHEMES = frozenset({"http", "https", "mailto", "tel", "data", "javascript"})


class VerifyFailed(Exception):
    """A rendered bundle failed verification. Nothing may proceed to deploy."""


class _EntryParser(HTMLParser):
    """Collects what verification needs in ONE pass over the entry HTML.

    convert_charrefs is left at its default (True) so entity-only text like
    ``&nbsp;`` counts as content rather than being dropped.
    """

    def __init__(self) -> None:
        super().__init__()
        self.in_body = False
        self._body_depth = 0
        self.saw_body = False
        self.body_text_chars = 0
        self.body_elements: list[str] = []
        self.form_actions: list[str] = []
        self.links: list[str] = []
        self.anchor_ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k.lower(): (v or "") for k, v in attrs}

        if tag == "body":
            self.in_body = True
            self.saw_body = True
            self._body_depth = 0
            return

        if attr.get("id"):
            self.anchor_ids.add(attr["id"])
        if tag == "a" and attr.get("name"):
            # Pre-HTML5 anchors are still valid targets for a #fragment.
            self.anchor_ids.add(attr["name"])

        if tag == "form" and "action" in attr:
            self.form_actions.append(attr["action"])

        # Only href/src that can actually 404. A <form action> is a POST target
        # served by the worker, not a bundle file, so it is checked separately.
        for key in ("href", "src"):
            if attr.get(key):
                self.links.append(attr[key])

        if self.in_body:
            self._body_depth += 1
            self.body_elements.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "body":
            self.in_body = False

    def handle_data(self, data: str) -> None:
        if self.in_body:
            self.body_text_chars += len(data.strip())


def _looks_like_full_document(html: str) -> bool:
    """Whether the HTML has a real ``<body>``, vs being a bare fragment."""
    return re.search(r"<body\b", html, re.IGNORECASE) is not None


def _check_body_not_empty(parser: _EntryParser, html: str) -> None:
    if not parser.saw_body and not _looks_like_full_document(html):
        raise VerifyFailed("entry HTML has no <body> element")

    # Content = visible text OR at least one element that renders something.
    # Svelte SSR output is dense with `<!--[-->` hydration comments, and a spec
    # that rendered NOTHING still emits the ripple-root div plus a pile of them.
    # So structural tags that are always present regardless of the spec do not
    # count as content.
    structural = {"form", "div", "span", "script", "style", "template", "link", "meta"}
    meaningful = [t for t in parser.body_elements if t not in structural]

    if parser.body_text_chars == 0 and not meaningful:
        raise VerifyFailed(
            "entry HTML body is empty — no text and no content elements "
            f"(saw only {sorted(set(parser.body_elements)) or 'nothing'}). "
            "A bare UINode spec renders this way: ripple's normalizeSpec only "
            "accepts {intent:...} or {ui:...}, so a bare {type,children} node "
            "falls through to an empty container."
        )


def _check_no_error_markers(html: str) -> None:
    for marker in _ERROR_MARKERS:
        if marker in html:
            raise VerifyFailed(f"entry HTML contains a render error marker: {marker!r}")


def _check_internal_links(parser: _EntryParser, bundle: Bundle) -> None:
    known = set(bundle.files)

    for raw in parser.links:
        link = raw.strip()
        if not link:
            continue

        split = urlsplit(link)
        if split.scheme.lower() in _EXTERNAL_SCHEMES or split.netloc:
            continue
        if link.startswith("//"):  # protocol-relative — external
            continue

        if not split.path:
            # Pure fragment: #services must point at something on the page.
            if split.fragment and split.fragment not in parser.anchor_ids:
                raise VerifyFailed(
                    f"internal anchor {link!r} points at no id on the page "
                    f"(known ids: {sorted(parser.anchor_ids) or 'none'})"
                )
            continue

        target = unquote(split.path).lstrip("/")
        if target in known:
            continue
        # A directory link (/pricing or /pricing/) is served by its index.html.
        if f"{target.rstrip('/')}/index.html" in known:
            continue
        raise VerifyFailed(
            f"internal link {link!r} resolves to {target!r}, which is not in the "
            f"bundle (bundle has: {sorted(known)})"
        )


def _check_form_action(parser: _EntryParser, expected_action: str | None) -> None:
    if not parser.form_actions:
        raise VerifyFailed(
            "no <form action=...> in the entry HTML — the lead form must survive "
            "the render or the site captures nothing with JavaScript off"
        )

    non_empty = [a for a in parser.form_actions if a.strip()]
    if not non_empty:
        raise VerifyFailed("<form> present but its action is empty")

    if expected_action is not None and expected_action not in non_empty:
        raise VerifyFailed(f"expected <form action={expected_action!r}>, found {non_empty!r}")


def verify(bundle: Bundle, *, expected_form_action: str | None = None) -> None:
    """Verify a rendered bundle. Returns ``None``; raises ``VerifyFailed``.

    ``expected_form_action`` pins the exact action when the caller knows it
    (the capture endpoint for that site). Left ``None``, any non-empty action
    passes — enough to prove the wrapper survived the render.
    """
    try:
        html = bundle.entry_text()
    except KeyError as exc:
        raise VerifyFailed(
            f"manifest entry_html={bundle.manifest.entry_html!r} is not in the bundle "
            f"(files: {sorted(bundle.files)})"
        ) from exc
    except UnicodeDecodeError as exc:
        raise VerifyFailed(f"entry HTML is not valid UTF-8: {exc}") from exc

    if not html.strip():
        raise VerifyFailed("entry HTML is empty")

    parser = _EntryParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # html.parser is lenient; a raise here is real
        raise VerifyFailed(f"entry HTML failed to parse: {exc}") from exc

    _check_body_not_empty(parser, html)
    _check_no_error_markers(html)
    _check_internal_links(parser, bundle)
    _check_form_action(parser, expected_form_action)
