# ee/pocketpaw_ee/sites/site_tokens.py — the theme tokens file a site gets when it
# moves onto the ``static`` starter from html it already had (the "Upgrade this site"
# conversion, and an imported site copied as-is).
#
# The ``static`` starter's manifest names ``styles/tokens.css`` as the theme
# (``customization.tokens``), and the builder's Design rail and theme edits write
# there. A converted site never had that file, so :func:`add_tokens` writes one and
# links it from every page:
#   * :func:`detect_theme` reads the five theme roles (``ROLES``) from the site's own
#     TOP-LEVEL rules, ``.css`` files and inline ``<style>`` blocks alike: ``html`` /
#     ``body`` / ``:root`` for background, text colour and body font, ``h1``-``h3`` for
#     the heading font, a button-like class (else ``a``) for the primary colour.
#     ``var()`` is resolved through ``design_extract``; a value that is not exactly one
#     colour (a background shorthand with an image, ``inherit``) is skipped, and
#     ``@media`` / ``@supports`` blocks are ignored, since they are conditional.
#   * :func:`tokens_css` keeps the starter's light ``:root`` defaults for every token
#     not detected (``FALLBACK`` when the starter has none).
#   * :func:`link_tokens` puts one ``<link>`` at the top of ``<head>``, before the
#     site's own stylesheets.
#   * ``site_token_rewrite`` then points the site's own CSS at the values the file pins
#     (:func:`file_theme`), so a theme edit reaches the elements that used them.
#
# Invariants: nothing here changes how the site renders. The file only declares
# custom properties, with no ``color-scheme`` and no dark-mode block, and it loads
# first, so the site's own CSS still wins; every rewritten literal equals its token.
# An owner's existing tokens file is never overwritten. Pure functions over the
# source map; no I/O.
from __future__ import annotations

import re
from collections.abc import Mapping

from pocketpaw_ee.sites import design_extract

TOKENS_REL = "styles/tokens.css"
TOKENS_HREF = "/styles/tokens.css"

#: The roles detection fills, in the order the file lists them.
ROLES = ("--background", "--foreground", "--primary", "--font-sans", "--font-heading")

#: The starter's own light defaults, for a starter copy that ships no tokens file.
FALLBACK: dict[str, str] = {
    "--background": "oklch(0.99 0 0)",
    "--foreground": "oklch(0.18 0 0)",
    "--primary": "oklch(0.25 0 0)",
    "--primary-foreground": "oklch(0.98 0 0)",
    "--font-sans": "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
    "--font-heading": "var(--font-sans)",
}

_HEADER = (
    "/* Theme tokens for this site. Generated when it moved onto the static starter:\n"
    "   colours and fonts read from its own stylesheets where they could be, the\n"
    "   starter's defaults for the rest. The theme editor writes here; reference them\n"
    "   with var(--...) in the site's CSS. */\n"
)

_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_STYLE_BLOCK = re.compile(r"<style\b[^>]*>(.*?)</style>", re.I | re.S)
_DECL = re.compile(r"([-\w]+)\s*:\s*([^;]+)")
_ROOT_BLOCK = re.compile(r"(?<![-\w]):root\s*\{([^{}]*)\}")
_ONE_COLOUR = re.compile(
    r"#[0-9a-fA-F]{3,8}|(?:rgba?|hsla?|oklch|oklab|lab|lch|color|hwb)\([^()]*\)|[a-zA-Z]+"
)
_NOT_A_COLOUR = frozenset(
    {"inherit", "initial", "unset", "revert", "none", "transparent", "currentcolor", "auto"}
)
_NOT_A_FONT = frozenset({"inherit", "initial", "unset", "revert"})

_BASE = frozenset({"html", "body", ":root"})
_HEADINGS = frozenset({"h1", "h2", "h3"})
_BUTTONS = frozenset(
    {"button", ".button", ".btn", ".btn-primary", ".button-primary", ".cta", ".button--primary"}
)
_PAGE_EXTS = (".html", ".htm")

_LINK_TAG = f'<link rel="stylesheet" href="{TOKENS_HREF}">'
_LINKED = re.compile(r"<link\b[^>]*href\s*=\s*[\"']?[^\"'\s>]*styles/tokens\.css", re.I)
_HEAD_OPEN = re.compile(r"<head\b[^>]*>", re.I)
_HTML_OPEN = re.compile(r"<html\b[^>]*>", re.I)
_DOCTYPE = re.compile(r"\s*<!doctype[^>]*>", re.I)


def _stylesheets(source: Mapping[str, object]) -> list[str]:
    sheets: list[str] = []
    for path, text in source.items():
        if not isinstance(text, str):
            continue
        lower = path.lower()
        if lower.endswith(".css"):
            sheets.append(text)
        elif lower.endswith(_PAGE_EXTS):
            sheets.extend(_STYLE_BLOCK.findall(text))
    return [_COMMENT.sub("", sheet) for sheet in sheets]


def _top_level_rules(css: str) -> list[tuple[str, str]]:
    """``(selector list, body)`` of every rule outside an at-rule block."""
    rules: list[tuple[str, str]] = []
    depth, start, selector, body_start = 0, 0, "", 0
    for i, ch in enumerate(css):
        if ch == "{":
            if depth == 0:
                selector, body_start = css[start:i].strip(), i + 1
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0:
                if not selector.startswith("@"):
                    rules.append((selector, css[body_start:i]))
                start = i + 1
        elif ch == ";" and depth == 0:
            start = i + 1  # a block-less at-rule (@import, @charset)
    return rules


def _colour(value: str) -> str | None:
    match = _ONE_COLOUR.fullmatch(value.strip())
    if match is None or value.strip().lower() in _NOT_A_COLOUR:
        return None
    return value.strip()


def _font(value: str) -> str | None:
    value = value.strip()
    if not value or "var(" in value or value.lower() in _NOT_A_FONT:
        return None
    return value


def detect_theme(source: Mapping[str, object]) -> dict[str, str]:
    """The theme roles the site's own top-level rules set, as ``{--token: value}``.
    A role nothing sets is absent. Later rules win, as in the cascade, and ``body``
    outranks ``html`` and ``:root``."""
    sheets = _stylesheets(source)
    props = dict(design_extract._CUSTOM_PROP.findall("\n".join(sheets)))
    props = {k: v.strip() for k, v in props.items()}
    found: dict[str, tuple[int, str]] = {}

    def offer(role: str, rank: int, value: str | None) -> None:
        if value is not None and rank >= found.get(role, (-1, ""))[0]:
            found[role] = (rank, value)

    for sheet in sheets:
        for selectors, body in _top_level_rules(sheet):
            names = {s.strip().lower() for s in selectors.split(",")}
            decls = {
                k.lower(): design_extract._resolve(v.strip(), props) for k, v in _DECL.findall(body)
            }
            if names & _BASE:
                rank = 2 if "body" in names else 1
                bg = decls.get("background-color") or decls.get("background")
                if bg is not None:
                    offer("--background", rank, _colour(bg))
                if "color" in decls:
                    offer("--foreground", rank, _colour(decls["color"]))
                if "font-family" in decls:
                    offer("--font-sans", rank, _font(decls["font-family"]))
            if names & _HEADINGS and "font-family" in decls:
                offer("--font-heading", 1, _font(decls["font-family"]))
            if names & _BUTTONS:
                bg = decls.get("background-color") or decls.get("background")
                if bg is not None:
                    offer("--primary", 2, _colour(bg))
            if "a" in names and "color" in decls:
                offer("--primary", 1, _colour(decls["color"]))
    return {role: found[role][1] for role in ROLES if role in found}


def root_declarations(css: str | None) -> dict[str, str]:
    """The custom properties of the first top-level ``:root`` block, in order."""
    for selectors, body in _top_level_rules(_COMMENT.sub("", css or "")):
        if selectors.strip() == ":root":
            return {
                k: v.strip() for k, v in _DECL.findall(body) if k.startswith("--") and v.strip()
            }
    return {}


def file_theme(css: str | None) -> dict[str, str]:
    """The roles a tokens file pins unconditionally: each of ``ROLES`` its first
    top-level ``:root`` gives a literal (not ``var()``) value and that the file
    declares nowhere else (an ``@media`` override would make the value conditional).
    These are the values the site's CSS may be pointed at."""
    clean = _COMMENT.sub("", css or "")
    root = root_declarations(clean)
    theme: dict[str, str] = {}
    for role in ROLES:
        value = root.get(role)
        declared = re.findall(rf"(?<![-\w]){re.escape(role)}\s*:", clean)
        if value and "var(" not in value.lower() and len(declared) == 1:
            theme[role] = value
    return theme


def tokens_css(starter_css: str | None, detected: Mapping[str, str]) -> str:
    """The tokens file: the starter's light ``:root`` tokens (``FALLBACK`` for roles it
    lacks) with ``detected`` over them. No ``color-scheme``, no dark-mode block."""
    tokens = dict(FALLBACK)
    tokens.update(root_declarations(starter_css))
    tokens.update(detected)
    lines = [f"  {name}: {value};" for name, value in tokens.items()]
    return _HEADER + "\n:root {\n" + "\n".join(lines) + "\n}\n"


def link_tokens(html: str) -> str:
    """``html`` with the tokens stylesheet linked first in its head (after ``<html>``
    or the doctype when it has no head). A page already linking it is unchanged."""
    if _LINKED.search(html):
        return html
    for pattern in (_HEAD_OPEN, _HTML_OPEN, _DOCTYPE):
        match = pattern.search(html)
        if match is not None:
            return f"{html[: match.end()]}\n{_LINK_TAG}{html[match.end() :]}"
    return f"{_LINK_TAG}\n{html}"


def add_tokens(
    source: Mapping[str, object], starter_css: str | None
) -> tuple[dict[str, object], list[str]]:
    """``(new source, added paths)``: ``source`` with ``styles/tokens.css`` written
    (unless the owner already has one), linked from every page, and the site's own
    CSS pointed at the file's values (``site_token_rewrite``). ``source`` is not
    mutated. Running it again on its own output changes nothing."""
    from pocketpaw_ee.sites import site_token_rewrite

    out = dict(source)
    added: list[str] = []
    if TOKENS_REL not in out:
        out[TOKENS_REL] = tokens_css(starter_css, detect_theme(source))
        added.append(TOKENS_REL)
    for path, text in source.items():
        if isinstance(text, str) and path.lower().endswith(_PAGE_EXTS):
            out[path] = link_tokens(text)
    tokens = out[TOKENS_REL]
    theme = file_theme(tokens if isinstance(tokens, str) else None)
    return site_token_rewrite.rewrite_source(out, theme), added


__all__ = [
    "FALLBACK",
    "ROLES",
    "TOKENS_HREF",
    "TOKENS_REL",
    "add_tokens",
    "detect_theme",
    "file_theme",
    "link_tokens",
    "root_declarations",
    "tokens_css",
]
