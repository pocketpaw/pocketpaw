# ee/pocketpaw_ee/sites/site_token_rewrite.py — points a converted site's own CSS at
# its theme tokens, so a theme edit from the Design rail actually changes the page.
#
# ``site_tokens.add_tokens`` writes ``styles/tokens.css`` from the site's colours and
# fonts, then calls :func:`rewrite_source` with that file's values. Every literal equal
# to a token's value becomes ``var(--token)``:
#   * in .css files and inline ``<style>`` blocks (not a ``<style media=…>``), inside
#     TOP-LEVEL rules only: anything in an at-rule block (``@media``, ``@supports``,
#     ``@font-face``, ``@layer``, …) and rules nested in a rule are left as written;
#   * in ``color`` / ``background(-color)`` / ``border(-side)-color`` / ``fill`` /
#     ``stroke`` (colour roles, matched per colour token so a shorthand or gradient
#     keeps its other parts; ``url()`` and strings are never touched), whole-value in
#     ``font-family`` (a heading selector prefers ``--font-heading``), and whole-value in
#     the site's own custom properties (``--ink: #222`` -> ``--ink: var(--foreground)``).
#   * Colours compare by value: hex case, ``#abc`` / ``#aabbcc``, and hex vs
#     ``rgb()``/``rgba()`` are equal when the channels are exactly equal; any other
#     form compares as text.
# Skipped: the tokens file, vendor / library CSS (``vendor/``, ``node_modules/``,
# bootstrap, tailwind output, font-awesome and similar, by path or by banner), a
# declaration carrying a comment, and any role the site's CSS declares itself (its
# own ``--primary`` would shadow the token).
#
# Invariant: rendering is unchanged. Each replaced literal equals the token's value,
# the tokens file is linked on every page, and roles a stylesheet could shadow are
# dropped first. Running it again changes nothing. Pure functions; no I/O.
from __future__ import annotations

import re
from collections.abc import Mapping

from pocketpaw_ee.sites import site_tokens

_COLOUR_ROLES = ("--background", "--foreground", "--primary")
_FONT_ROLES = ("--font-sans", "--font-heading")

#: Colour property -> the roles it may take, in preference order.
_COLOUR_PROPS: dict[str, tuple[str, ...]] = {
    "color": ("--foreground", "--primary"),
    "background": ("--background", "--primary"),
    "background-color": ("--background", "--primary"),
    **{
        prop: ("--primary", "--foreground")
        for prop in (
            "border-color",
            "border-top-color",
            "border-right-color",
            "border-bottom-color",
            "border-left-color",
            "fill",
            "stroke",
        )
    },
}

_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_STYLE_BLOCK = re.compile(r"(<style\b([^>]*)>)(.*?)(</style>)", re.I | re.S)
_MEDIA_ATTR = re.compile(r"\bmedia\s*=", re.I)
_DECL = re.compile(r"(\s*)([-\w]+)(\s*:)(.*)", re.S)
_IMPORTANT = re.compile(r"(.*?)(\s*!\s*important)?(\s*)", re.S | re.I)
_PROTECTED = re.compile(r"url\([^)]*\)|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'", re.I)
_COLOUR_TOKEN = re.compile(
    r"(?<![-\w#])(?:#[0-9a-f]{3,8}(?![-\w])"
    r"|(?:rgba?|hsla?|oklch|oklab|lab|lch|hwb|color)\([^()]*\)"
    r"|[a-z]+(?![-\w(]))",
    re.I,
)
_RGB = re.compile(r"rgba?\((.*)\)", re.S)
_HEADING = re.compile(r"(?<![-\w])h[1-6](?![-\w])", re.I)
_CUSTOM_DECL = re.compile(r"(?<![-\w])(--[-\w]+)\s*:")

_VENDOR_DIRS = frozenset(
    {"vendor", "vendors", "node_modules", "bower_components", "third_party", "third-party"}
)
_LIBRARY_WORDS = frozenset(
    {
        "bootstrap",
        "tailwind",
        "tailwindcss",
        "fontawesome",
        "awesome",
        "normalize",
        "animate",
        "bulma",
        "foundation",
        "materialize",
        "uikit",
        "tachyons",
        "swiper",
        "slick",
        "owl",
        "aos",
        "glide",
        "splide",
        "flickity",
        "fancybox",
        "lightbox",
        "magnific",
        "select2",
        "leaflet",
        "prism",
        "highlight",
        "katex",
        "mapbox",
        "jquery",
        "pico",
    }
)
_LIBRARY_BANNER = re.compile(
    r"/\*!?\s*(?:bootstrap|tailwindcss|font\s*awesome|normalize\.css|animate\.css|bulma|"
    r"foundation|swiper)\b",
    re.I,
)


# ---------------------------------------------------------------------------
# Value keys
# ---------------------------------------------------------------------------


def colour_key(value: str) -> tuple | None:
    """A comparable key for one colour: ``("rgb", r, g, b, a)`` for hex and rgb(a),
    the normalized text for any other colour form, ``None`` for a non-colour."""
    v = value.strip().lower()
    if v.startswith("#"):
        h = v[1:]
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        if len(h) not in (6, 8) or not re.fullmatch(r"[0-9a-f]+", h):
            return None
        alpha = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
        return ("rgb", int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), round(alpha, 6))
    match = _RGB.fullmatch(v)
    if match is not None:
        parts = [p for p in re.split(r"[\s,/]+", match.group(1).strip()) if p]
        try:
            if len(parts) in (3, 4):
                rgb = [float(p[:-1]) * 2.55 if p.endswith("%") else float(p) for p in parts[:3]]
                a = parts[3] if len(parts) == 4 else "1"
                alpha = float(a[:-1]) / 100 if a.endswith("%") else float(a)
                return ("rgb", *(round(c, 6) for c in rgb), round(alpha, 6))
        except ValueError:
            pass
    if site_tokens._colour(v) is None:
        return None
    return ("text", re.sub(r"\s+", " ", re.sub(r"\s*([(),/])\s*", r"\1", v)))


def _split_commas(value: str) -> list[str]:
    parts, buf, quote = [], [], ""
    for ch in value:
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch == ",":
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return parts


def font_key(value: str) -> tuple[str, ...] | None:
    """A comparable key for a font-family list: families unquoted, whitespace evened
    out, lowercased (family names match case-insensitively)."""
    if not value.strip() or "var(" in value.lower():
        return None
    families = []
    for part in _split_commas(value):
        name = part.strip()
        if len(name) >= 2 and name[0] == name[-1] and name[0] in "\"'":
            name = name[1:-1]
        families.append(re.sub(r"\s+", " ", name.strip()).lower())
    return tuple(families) if all(families) else None


# ---------------------------------------------------------------------------
# CSS walking
# ---------------------------------------------------------------------------


def _skip_string(css: str, i: int) -> int:
    quote, j = css[i], i + 1
    while j < len(css):
        if css[j] == "\\":
            j += 2
            continue
        if css[j] in (quote, "\n"):
            return j + 1
        j += 1
    return len(css)


def _skip_comment(css: str, i: int) -> int:
    end = css.find("*/", i + 2)
    return len(css) if end < 0 else end + 2


def _block_end(css: str, i: int) -> int:
    """Index of the ``}`` closing the block whose body starts at ``i``."""
    depth, j = 1, i
    while j < len(css):
        ch = css[j]
        if ch in "\"'":
            j = _skip_string(css, j)
            continue
        if css.startswith("/*", j):
            j = _skip_comment(css, j)
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return len(css)


class _Theme:
    def __init__(self, theme: Mapping[str, str]) -> None:
        self.colours = {
            role: key
            for role in _COLOUR_ROLES
            if role in theme and (key := colour_key(theme[role])) is not None
        }
        self.fonts = {
            role: key
            for role in _FONT_ROLES
            if role in theme and (key := font_key(theme[role])) is not None
        }

    def __bool__(self) -> bool:
        return bool(self.colours or self.fonts)

    def colour_role(self, token: str, roles: tuple[str, ...]) -> str | None:
        key = colour_key(token)
        return next((r for r in roles if key is not None and self.colours.get(r) == key), None)

    def font_role(self, value: str, roles: tuple[str, ...]) -> str | None:
        key = font_key(value)
        return next((r for r in roles if key is not None and self.fonts.get(r) == key), None)


def _replace_colours(value: str, roles: tuple[str, ...], theme: _Theme) -> str:
    def swap(match: re.Match[str]) -> str:
        role = theme.colour_role(match.group(0), roles)
        return f"var({role})" if role else match.group(0)

    out, pos = [], 0
    for protected in _PROTECTED.finditer(value):
        out.append(_COLOUR_TOKEN.sub(swap, value[pos : protected.start()]))
        out.append(protected.group(0))
        pos = protected.end()
    out.append(_COLOUR_TOKEN.sub(swap, value[pos:]))
    return "".join(out)


def _rewrite_decl(decl: str, heading: bool, theme: _Theme) -> str:
    match = _DECL.fullmatch(decl)
    if match is None or "/*" in decl:
        return decl
    lead, prop, colon, raw = match.groups()
    parts = _IMPORTANT.fullmatch(raw)
    assert parts is not None  # the pattern matches any string
    value, important, tail = parts.group(1), parts.group(2) or "", parts.group(3)
    core = value.strip()
    pad = value[: len(value) - len(value.lstrip())]
    name = prop.lower()
    new = core
    if name.startswith("--"):
        role = None
        if colour_key(core) is not None and site_tokens._colour(core) is not None:
            role = theme.colour_role(core, _COLOUR_ROLES)
        role = role or theme.font_role(core, _FONT_ROLES)
        if role and role != prop:
            new = f"var({role})"
    elif name in _COLOUR_PROPS:
        new = _replace_colours(core, _COLOUR_PROPS[name], theme)
    elif name == "font-family":
        roles = _FONT_ROLES[::-1] if heading else _FONT_ROLES
        role = theme.font_role(core, roles)
        new = f"var({role})" if role else core
    if new == core:
        return decl
    return f"{lead}{prop}{colon}{pad}{new}{important}{tail}"


def _rewrite_body(body: str, selector: str, theme: _Theme) -> str:
    """A rule body with its own declarations rewritten; nested blocks kept as is."""
    heading = bool(_HEADING.search(selector))
    out: list[str] = []
    start, j, parens = 0, 0, 0
    while j < len(body):
        ch = body[j]
        if ch in "\"'":
            j = _skip_string(body, j)
            continue
        if body.startswith("/*", j):
            j = _skip_comment(body, j)
            continue
        if ch == "(":
            parens += 1
        elif ch == ")":
            parens = max(0, parens - 1)
        elif ch == "{":
            end = _block_end(body, j + 1)
            out.append(body[start : end + 1])  # a nested rule: left as written
            start = j = end + 1
            continue
        elif ch == ";" and parens == 0:
            out.append(_rewrite_decl(body[start:j], heading, theme) + ";")
            start = j + 1
        j += 1
    out.append(_rewrite_decl(body[start:], heading, theme))
    return "".join(out)


def _rewrite(css: str, theme: _Theme) -> str:
    out: list[str] = []
    pos = prelude = i = 0
    while i < len(css):
        ch = css[i]
        if ch in "\"'":
            i = _skip_string(css, i)
            continue
        if css.startswith("/*", i):
            i = _skip_comment(css, i)
            continue
        if ch in ";}":
            prelude = i + 1
        elif ch == "{":
            end = _block_end(css, i + 1)
            selector = _COMMENT.sub("", css[prelude:i]).strip()
            if not selector.startswith("@"):
                out.append(css[pos : i + 1])
                out.append(_rewrite_body(css[i + 1 : end], selector, theme))
                pos = end
            i = prelude = end + 1
            continue
        i += 1
    out.append(css[pos:])
    return "".join(out)


def rewrite_css(css: str, theme: Mapping[str, str]) -> str:
    """``css`` with each literal equal to a ``theme`` value replaced by its token (see
    the module header for where). Unchanged when nothing matches."""
    compiled = _Theme(theme)
    return _rewrite(css, compiled) if compiled else css


# ---------------------------------------------------------------------------
# Source maps
# ---------------------------------------------------------------------------


def _in_vendor_dir(path: str) -> bool:
    dirs = path.lower().replace("\\", "/").split("/")[:-1]
    return any(part in _VENDOR_DIRS or part in _LIBRARY_WORDS for part in dirs)


def is_library_css(path: str, text: str) -> bool:
    """True for CSS the site did not write: a vendor directory, a known library's file
    name, or a library banner at the top of the file."""
    if _in_vendor_dir(path):
        return True
    parts = path.lower().replace("\\", "/").split("/")
    stem = parts[-1].rsplit(".", 1)[0]
    words = set(re.split(r"[.\-_]+", stem))
    return bool(words & _LIBRARY_WORDS) or bool(_LIBRARY_BANNER.search(text[:500]))


def _declared_roles(source: Mapping[str, object], skip: str) -> set[str]:
    declared: set[str] = set()
    for path, text in source.items():
        if path == skip or not isinstance(text, str):
            continue
        lower = path.lower()
        if lower.endswith(".css"):
            declared.update(_CUSTOM_DECL.findall(_COMMENT.sub("", text)))
        elif lower.endswith((".html", ".htm")):
            for block in _STYLE_BLOCK.finditer(text):
                declared.update(_CUSTOM_DECL.findall(_COMMENT.sub("", block.group(3))))
    return declared


def rewrite_source(
    source: Mapping[str, object],
    theme: Mapping[str, str],
    *,
    tokens_path: str = site_tokens.TOKENS_REL,
) -> dict[str, object]:
    """``source`` with every stylesheet and inline ``<style>`` block the site wrote
    pointed at the tokens (:func:`rewrite_css`). Roles the site declares itself are
    dropped from ``theme`` first. ``source`` is not mutated."""
    declared = _declared_roles(source, tokens_path)
    compiled = _Theme({role: value for role, value in theme.items() if role not in declared})
    out = dict(source)
    if not compiled:
        return out
    for path, text in source.items():
        if path == tokens_path or not isinstance(text, str):
            continue
        lower = path.lower()
        if lower.endswith(".css"):
            if not is_library_css(path, text):
                out[path] = _rewrite(text, compiled)
        elif lower.endswith((".html", ".htm")) and not _in_vendor_dir(path):

            def block(match: re.Match[str]) -> str:
                if _MEDIA_ATTR.search(match.group(2)):
                    return match.group(0)
                return match.group(1) + _rewrite(match.group(3), compiled) + match.group(4)

            out[path] = _STYLE_BLOCK.sub(block, text)
    return out


__all__ = ["colour_key", "font_key", "is_library_css", "rewrite_css", "rewrite_source"]
