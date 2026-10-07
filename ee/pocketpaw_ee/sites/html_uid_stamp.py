# ee/pocketpaw_ee/sites/html_uid_stamp.py — Python port of paw-sites' html data-uid
# stamping (src/html-edit-manifest.ts ``stampHtmlDataUids``), for hosts with no
# paw-sites CLI.
#
# The draft preview origin serves an html page with ``?paw_edit=1`` stamped with
# ``data-uid`` on its editable leaves, so a pick in the frame maps to a rail leaf and
# to the write path (paw-sites ``apply-leaf-edit``), which re-derives the same uids
# from the raw source. ``preview_origin.materialize_html_draft`` prefers the real
# ``arm-html`` CLI and falls back to this port per page.
#
# Invariant: a uid stamped here must be the uid paw-sites derives for that element.
# paw-sites walks a parse5 (HTML5 tree construction) tree; this walks a tree built
# from ``html.parser`` tokens. The two agree on well-formed markup, which is what the
# Sites agent writes. Wherever HTML5 tree construction would REPAIR the markup (an
# implied ``</p>``, a misnested formatting element, foster-parented table content, a
# stray end tag, foreign-content breakout, ...) the trees can differ, so the port
# refuses the page (``None``) rather than stamp a uid that could address the wrong
# element. A refused page is served unstamped: picks still work, they just carry no
# uid. tests/ee/sites/test_html_uid_stamp.py pins parity against paw-sites output.
#
# Leaf rules, uid scheme ("<page>:<role|tag>:<ordinal>", pre-order) and the stamp
# splice are a line-for-line mirror of html-edit-manifest.ts at the vendored bridge
# pin; change both together.

from __future__ import annotations

import re
from html.parser import HTMLParser

DATA_UID_ATTR = "data-uid"

_HTML_FILE_RE = re.compile(r"\.html?$", re.IGNORECASE)
_SKIP_TAGS = frozenset({"script", "style", "template", "noscript"})
_STRUCTURAL = frozenset({"html", "head", "body"})
_EDIT_SAFE_ATTRS = (
    "href", "src", "alt", "title", "style", "poster", "aria-label",
    "target", "rel", "download", "loading", "width", "height",
)  # fmt: skip
_IDENTITY_ATTRS = ("href", "src", "poster", "aria-label")

# JavaScript's ``\s`` (what paw-sites trims a text run with).
_JS_WS = " \t\n\v\f\r                 　﻿"
_HTML_WS = " \t\n\r\f"

_VOID = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "source", "track", "wbr", "param", "keygen", "basefont", "bgsound",
})  # fmt: skip
_RAWTEXT = frozenset({"script", "style", "xmp", "iframe", "noembed", "noframes", "noscript"})
_RCDATA = frozenset({"title", "textarea"})
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_CLOSES_P = frozenset({
    "address", "article", "aside", "blockquote", "center", "details", "dialog", "dir",
    "div", "dl", "fieldset", "figcaption", "figure", "footer", "header", "hgroup",
    "main", "menu", "nav", "ol", "p", "search", "section", "summary", "ul", "pre",
    "listing", "form", "table", "hr", "xmp", *_HEADINGS,
})  # fmt: skip
_BUTTON_SCOPE_STOP = frozenset({
    "html", "table", "td", "th", "caption", "marquee", "object", "applet", "template",
    "button",
})  # fmt: skip
_SPECIAL = frozenset({
    "address", "applet", "area", "article", "aside", "base", "basefont", "bgsound",
    "blockquote", "body", "br", "button", "caption", "center", "col", "colgroup", "dd",
    "details", "dir", "div", "dl", "dt", "embed", "fieldset", "figcaption", "figure",
    "footer", "form", "frame", "frameset", "head", "header", "hgroup", "hr", "html",
    "iframe", "img", "input", "li", "link", "listing", "main", "marquee", "menu",
    "meta", "nav", "noembed", "noframes", "noscript", "object", "ol", "p", "param",
    "plaintext", "pre", "script", "search", "section", "select", "source", "style",
    "summary", "table", "tbody", "td", "template", "textarea", "tfoot", "th", "thead",
    "title", "tr", "track", "ul", "wbr", "xmp", *_HEADINGS,
})  # fmt: skip
_TABLE_CTX = frozenset({"table", "tbody", "thead", "tfoot", "tr"})
_TABLE_OK = frozenset({
    "caption", "colgroup", "col", "tbody", "thead", "tfoot", "tr", "td", "th",
    "script", "style", "template",
})  # fmt: skip
_TABLE_PARTS = _TABLE_OK - {"script", "style", "template"}
_SELECT_OK = frozenset({"option", "optgroup", "hr", "script", "template"})
_NEVER = frozenset({"image", "frameset", "frame", "plaintext", "isindex"})
_ONE_OPEN = frozenset({"a", "nobr", "button", "form"})
_BREAKOUT = frozenset({
    "b", "big", "blockquote", "body", "br", "center", "code", "dd", "div", "dl", "dt",
    "em", "embed", "head", "hr", "i", "img", "li", "listing", "menu", "meta", "nobr",
    "ol", "p", "pre", "ruby", "s", "small", "span", "strong", "strike", "sub", "sup",
    "table", "tt", "u", "ul", "var", *_HEADINGS,
})  # fmt: skip
_SVG_INTEGRATION = frozenset({"foreignobject", "desc", "title"})
_MATH_INTEGRATION = frozenset({"mi", "mo", "mn", "ms", "mtext"})


class _Ambiguous(Exception):
    """HTML5 tree construction would repair this markup; refuse the page."""


class _Node:
    __slots__ = (
        "tag", "attrs", "start", "start_end", "content_end", "children",
        "has_comment", "foreign", "integration", "svg",
    )  # fmt: skip

    def __init__(self, tag: str, attrs: dict[str, str | None], start: int, start_end: int):
        self.tag = tag
        self.attrs = attrs
        self.start = start
        self.start_end = start_end
        self.content_end: int | None = None
        self.children: list[_Node] = []
        self.has_comment = False
        self.foreign = False
        self.integration = False
        self.svg = False


class _TreeBuilder(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.html = html
        self._line_starts = [0]
        for m in re.finditer("\n", html):
            self._line_starts.append(m.end())
        self.root = _Node("#root", {}, 0, 0)
        self.stack: list[_Node] = [self.root]
        self._seen_structural: set[str] = set()

    def _offset(self) -> int:
        line, col = self.getpos()
        return self._line_starts[line - 1] + col

    # -- start tags ---------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, self_closing=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, self_closing=True)

    def _in_stack(self, tag: str) -> bool:
        return any(n.tag == tag and not n.foreign for n in self.stack[1:])

    def _p_in_button_scope(self) -> bool:
        for n in reversed(self.stack[1:]):
            if n.foreign:
                if n.integration:
                    return False
                continue
            if n.tag == "p":
                return True
            if n.tag in _BUTTON_SCOPE_STOP:
                return False
        return False

    def _open_list_item(self, items: frozenset[str]) -> bool:
        for n in reversed(self.stack[1:]):
            if n.foreign:
                continue
            if n.tag in items:
                return True
            if n.tag in _SPECIAL and n.tag not in ("address", "div", "p"):
                return False
        return False

    def _check_html_start(self, tag: str, attrs: dict[str, str | None], parent: _Node) -> None:
        if tag in _NEVER:
            raise _Ambiguous(tag)
        if tag in _STRUCTURAL:
            if tag in self._seen_structural or any(
                n.tag not in _STRUCTURAL for n in self.stack[1:]
            ):
                raise _Ambiguous(tag)
            self._seen_structural.add(tag)
        if tag in _CLOSES_P and self._p_in_button_scope():
            raise _Ambiguous("implied </p>")
        if tag == "li" and self._open_list_item(frozenset({"li"})):
            raise _Ambiguous("implied </li>")
        if tag in ("dd", "dt") and self._open_list_item(frozenset({"dd", "dt"})):
            raise _Ambiguous("implied </dd>")
        if tag in _HEADINGS and parent.tag in _HEADINGS and not parent.foreign:
            raise _Ambiguous("nested heading")
        if tag in _ONE_OPEN and self._in_stack(tag):
            raise _Ambiguous(f"nested {tag}")
        if tag in ("option", "optgroup") and parent.tag == "option":
            raise _Ambiguous("implied </option>")
        if not parent.foreign:
            if parent.tag in _TABLE_CTX and tag not in _TABLE_OK:
                raise _Ambiguous("foster parenting")
            if parent.tag in ("select", "optgroup") and tag not in _SELECT_OK | {"optgroup"}:
                raise _Ambiguous("select content")
        if tag in _TABLE_PARTS and not (
            parent.tag in _TABLE_CTX or (parent.tag == "colgroup" and tag == "col")
        ):
            raise _Ambiguous("table part outside a table")
        if tag in ("td", "th") and parent.tag not in ("tr", "tbody", "thead", "tfoot", "table"):
            raise _Ambiguous("cell outside a row")

    def _start(self, tag: str, attr_list: list[tuple[str, str | None]], self_closing: bool) -> None:
        parent = self.stack[-1]
        start = self._offset()
        raw = self.get_starttag_text() or ""
        if not raw.startswith("<"):
            raise _Ambiguous("unlocated start tag")
        attrs: dict[str, str | None] = {}
        for name, value in attr_list:
            attrs.setdefault(name, value)  # duplicates: the first one wins (HTML5)

        in_foreign = parent.foreign and not parent.integration
        if in_foreign and (
            tag in _BREAKOUT
            or (tag == "font" and any(a in attrs for a in ("color", "face", "size")))
        ):
            raise _Ambiguous("foreign-content breakout")
        foreign = tag in ("svg", "math") or in_foreign
        if not foreign:
            self._check_html_start(tag, attrs, parent)

        node = _Node(tag, attrs, start, start + len(raw))
        node.foreign = foreign
        if foreign:
            node.svg = tag == "svg" or (parent.foreign and parent.svg)
            node.integration = (node.svg and tag in _SVG_INTEGRATION) or (
                not node.svg and tag in _MATH_INTEGRATION
            )
        parent.children.append(node)

        if foreign and self_closing:
            node.content_end = node.start_end
            return
        if not foreign and tag in _VOID:
            node.content_end = node.start_end
            return
        # A non-void html element's "/>" is ignored by HTML5: it stays open.
        self.stack.append(node)
        if not foreign and tag in _RAWTEXT:
            self.set_cdata_mode(tag)
        elif not foreign and tag in _RCDATA:
            self.set_cdata_mode(tag, escapable=True)

    # -- end tags -----------------------------------------------------------------

    def handle_endtag(self, tag: str) -> None:
        top = self.stack[-1]
        if top is not self.root and top.tag == tag:
            top.content_end = self._offset()
            self.stack.pop()
            return
        if tag in _STRUCTURAL:
            names = [n.tag for n in self.stack[1:]]
            if tag not in names:
                return  # an implied html/head/body: nothing of ours to close
            while self.stack[-1].tag != tag:
                closing = self.stack.pop()
                if closing.tag not in _STRUCTURAL:
                    raise _Ambiguous("unclosed element before </body>")
                closing.content_end = self._offset()
            self.stack[-1].content_end = self._offset()
            self.stack.pop()
            return
        raise _Ambiguous(f"mismatched </{tag}>")

    # -- other tokens -------------------------------------------------------------

    def handle_data(self, data: str) -> None:
        parent = self.stack[-1]
        if parent.tag in _TABLE_CTX and not parent.foreign and data.strip(_HTML_WS):
            raise _Ambiguous("text foster-parented out of a table")

    def handle_comment(self, data: str) -> None:
        self.stack[-1].has_comment = True

    def handle_pi(self, data: str) -> None:
        self.stack[-1].has_comment = True  # HTML5: a bogus comment

    def unknown_decl(self, data: str) -> None:
        raise _Ambiguous("CDATA / unknown declaration")

    def finish(self) -> _Node:
        self.close()
        for n in self.stack[1:]:  # EOF closes whatever is still open
            n.content_end = len(self.html)
        return self.root


# ---------------------------------------------------------------------------
# Leaf derivation (mirror of deriveHtmlLeaves)
# ---------------------------------------------------------------------------


def classify_role(tag: str, classes: list[str]) -> str | None:
    """Mirror of paw-sites ``classifyRole`` (svelte-edit-manifest.ts)."""
    cls = {c.lower() for c in classes}
    if cls & {"eyebrow", "kicker", "overline"}:
        return "eyebrow"
    if cls & {"lead", "subhead", "subtitle"}:
        return "subhead"
    if cls & {"badge", "pill", "tag"}:
        return "badge"
    if tag in ("h1", "h2"):
        return "headline"
    if tag == "img":
        return "image"
    if tag == "a" and cls & {"btn", "button", "cta"}:
        return "cta"
    if tag == "button":
        return "cta"
    if tag == "a":
        return "link"
    if tag == "p":
        return "body"
    return None


def _classes(node: _Node) -> list[str]:
    value = node.attrs.get("class")
    return [c for c in re.split(f"[{_JS_WS}]+", value) if c] if value else []


def _has_value(node: _Node, name: str) -> bool:
    return name in node.attrs and node.attrs[name] is not None


def _text_content(html: str, node: _Node) -> str:
    end = node.content_end if node.content_end is not None else node.start_end
    text = html[node.start_end : end]
    if node.tag in ("pre", "listing", "textarea") and not node.foreign:
        # HTML5 drops one newline right after these start tags.
        if text.startswith("\r\n"):
            text = text[2:]
        elif text.startswith(("\n", "\r")):
            text = text[1:]
    return text


def _leaves(html: str, file: str, root: _Node) -> list[tuple[str, _Node]]:
    page = _HTML_FILE_RE.sub("", file)
    ordinals: dict[str, int] = {}
    out: list[tuple[str, _Node]] = []

    def next_uid(key: str) -> str:
        n = ordinals.get(key, 0)
        ordinals[key] = n + 1
        return f"{page}:{key}:{n}"

    def leaf(node: _Node) -> None:
        if node.tag in ("html", "head") and not node.foreign:
            return  # HTML5 always gives these element children (head/body)
        role = classify_role(node.tag, _classes(node))
        text = _text_content(html, node)
        if text:
            value = text.strip(_JS_WS)
            has_attrs = any(_has_value(node, a) for a in _EDIT_SAFE_ATTRS)
            if value == "" and not has_attrs:
                return
        elif not (
            (node.tag == "img" and _has_value(node, "src"))
            or (node.tag in ("a", "area") and _has_value(node, "href"))
        ):
            return
        out.append((next_uid(role or node.tag), node))

    def container_identity(node: _Node) -> None:
        if node.tag in _STRUCTURAL:
            return
        if not any(_has_value(node, a) for a in _IDENTITY_ATTRS):
            return
        role = classify_role(node.tag, _classes(node))
        out.append((next_uid(role or node.tag), node))

    def visit(node: _Node) -> None:
        if node.tag in _SKIP_TAGS:
            return
        if node.children:
            container_identity(node)
            for child in node.children:
                visit(child)
            return
        if node.has_comment:
            return
        leaf(node)

    for child in root.children:
        visit(child)
    return out


def stamp_html_data_uids(html: str, file: str) -> str | None:
    """``html`` with ``data-uid`` stamped on every editable leaf, byte-identical
    otherwise; ``None`` when the page needs HTML5 repair the port does not model."""
    if html.startswith("﻿") or "\x00" in html:
        return None
    try:
        builder = _TreeBuilder(html)
        builder.feed(html)
        root = builder.finish()
    except _Ambiguous:
        return None
    inserts: list[tuple[int, str]] = []
    for uid, node in _leaves(html, file, root):
        if DATA_UID_ATTR in node.attrs:
            continue
        inserts.append((node.start + 1 + len(node.tag), f' {DATA_UID_ATTR}="{uid}"'))
    out = html
    for at, text in sorted(inserts, key=lambda i: i[0], reverse=True):
        out = out[:at] + text + out[at:]
    return out


__all__ = ["DATA_UID_ATTR", "classify_role", "stamp_html_data_uids"]
