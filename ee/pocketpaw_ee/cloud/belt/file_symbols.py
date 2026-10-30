# ee/pocketpaw_ee/cloud/belt/file_symbols.py — declarations read straight from one
# file's text, the Code tab's fallback when the loom world-model has no symbols for
# it (no model, one too large, or a language loom does not read, such as Swift).
#
# Line patterns per language, not a parser: Swift (func, init, class, struct,
# enum, protocol, extension, actor, typealias, and var/let directly inside a
# type's braces), TS/JS/Svelte (function, class, interface, type, enum, exported
# const/let), Python (def, class) and Go (func, type). Block comments,
# triple-quoted strings and string literals are blanked first, keeping line
# numbers, so a commented-out declaration is not listed. ``callers`` is ``None``
# (a file alone cannot count them). At most ``MAX_SYMBOLS``, in file order.

from __future__ import annotations

import re
from typing import Any

MAX_SYMBOLS = 200
_MODS = (
    r"(?:(?:@\w+(?:\([^)]*\))?|public|private(?:\(set\))?|fileprivate|internal|open|final"
    r"|static|class|override|mutating|nonmutating|nonisolated|convenience|required|lazy"
    r"|weak|unowned|indirect|dynamic|async|isolated)\s+)*"
)
_SWIFT = re.compile(
    rf"\s*{_MODS}(func|init|class|struct|enum|protocol|extension|actor|typealias)\b"
    r"(?!\s+(?:var|let|func|subscript)\b)[\s?!]*([A-Za-z_][\w.]*)?"
)
_SWIFT_PROP = re.compile(rf"\s*{_MODS}(var|let)\s+([A-Za-z_]\w*)")
_SWIFT_TYPES = {"class", "struct", "enum", "protocol", "extension", "actor"}
_TS = re.compile(
    r"\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"
    r"(function|class|interface|type|enum)\*?\s+([A-Za-z_$][\w$]*)"
)
_TS_CONST = re.compile(r"\s*export\s+(const|let)\s+([A-Za-z_$][\w$]*)")
_PY = re.compile(r"\s*(?:async\s+)?(def|class)\s+([A-Za-z_]\w*)")
_GO = re.compile(r"(func)\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)|(type)\s+([A-Za-z_]\w*)")
_KIND = {"func": "function", "def": "function", "var": "property", "let": "property"}
_STRING = re.compile(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'')
_TRIPLE = re.compile(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'')
_BLOCK = re.compile(r"/\*[\s\S]*?\*/")
_TS_LANGS = {"typescript", "javascript", "svelte"}


def _blank(match: re.Match[str]) -> str:
    return "\n" * match.group().count("\n")  # keeps line numbers


def _code_lines(text: str, lang: str) -> list[str]:
    """``text`` with comments and string contents removed, one entry per line."""
    text = _TRIPLE.sub(_blank, text) if lang in ("python", "swift") else text
    text = _BLOCK.sub(_blank, text) if lang != "python" else text
    marker = "#" if lang == "python" else "//"
    return [_STRING.sub('""', line).split(marker, 1)[0] for line in text.split("\n")]


def _match(line: str, lang: str, in_type: bool) -> tuple[str, str] | None:
    """``(kind, name)`` of a declaration on ``line``, or ``None``."""
    if lang == "swift":
        if m := _SWIFT.match(line):
            return m[1], m[2] or m[1]
        return (m[1], m[2]) if in_type and (m := _SWIFT_PROP.match(line)) else None
    if lang in _TS_LANGS:
        m = _TS.match(line) or _TS_CONST.match(line)
        return (m[1], m[2]) if m else None
    if lang == "python":
        return (m[1], m[2]) if (m := _PY.match(line)) else None
    if lang == "go" and (m := _GO.match(line)):
        return (m[1], m[2]) if m[1] else (m[3], m[4])
    return None


def file_symbols(text: str, lang: str | None) -> list[dict[str, Any]]:
    """``[{name, kind, line, callers: None}]`` declared in ``text``, in file order."""
    if lang not in {"swift", "python", "go", *_TS_LANGS}:
        return []
    out: list[dict[str, Any]] = []
    braces: list[bool] = []  # per open brace: does it open a type body?
    for n, line in enumerate(_code_lines(text, lang), 1):
        hit = _match(line, lang, bool(braces) and braces[-1])
        if hit and len(out) < MAX_SYMBOLS:
            kind, name = hit
            out.append({"name": name, "kind": _KIND.get(kind, kind), "line": n, "callers": None})
        opens_type = lang == "swift" and bool(hit) and hit[0] in _SWIFT_TYPES
        for ch in line:
            if ch == "{":
                braces.append(opens_type)
                opens_type = False
            elif ch == "}" and braces:
                braces.pop()
    return out
