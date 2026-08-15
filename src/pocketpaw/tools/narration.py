# Humanized tool narration — turns a tool call into a plain-language phrase.
# Created: 2026-08-15 (HTN-1) — surfaces render "using pocketpaw_sites_publish"
# today because every consumer only ever sees the bare tool NAME. A tool now
# declares a ``Narration`` (see ``BaseTool.narration``) and the server renders
# it once, so chat, Mission Control, and every channel adapter inherit the same
# phrasing instead of each keeping its own hardcoded name->phrase map.
#
# The rendering rules here are security boundaries, not style choices: tool
# arguments are model-authored and frequently carry secrets (api keys, tokens,
# file contents), so ONLY fields a tool explicitly allowlists in ``safe_args``
# may ever reach a user-visible string, and only after sanitizing.

from __future__ import annotations

import logging
import re
import string
from dataclasses import dataclass
from functools import lru_cache
from importlib import import_module
from numbers import Number
from typing import Any

logger = logging.getLogger(__name__)

# Interpolated values are truncated to this many characters (ellipsis included)
# so a pasted essay in a tool arg can't blow out a status line.
_MAX_VALUE_LEN = 80
_ELLIPSIS = "…"

# C0/C1 control characters plus the Unicode line/paragraph separators. Stripped
# before substitution so an arg can't inject newlines into a status line (or a
# terminal escape sequence into a channel adapter that writes to a TTY).
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_WHITESPACE_RUN = re.compile(r"\s+")

_FORMATTER = string.Formatter()


@dataclass(frozen=True)
class Narration:
    """Plain-language phrasing for a tool call.

    ``active`` may interpolate arguments, but ONLY those named in
    ``safe_args``. ``bare`` carries no arguments at all and is the fallback
    whenever ``active`` cannot be rendered safely, so it must stand on its own
    as a complete phrase.
    """

    active: str  # "Searching the web for {query}"
    bare: str  # "Searching the web"
    safe_args: tuple[str, ...] = ()  # allowlist — ONLY these may interpolate


def _template_fields(template: str) -> list[str] | None:
    """Return the plain field names in ``template``.

    Returns ``None`` when the template uses any construct we refuse to render:
    positional (``{0}``), attribute/index access (``{q.__class__}``,
    ``{q[0]}``), conversions (``{q!r}``) or format specs (``{q:>999999}``).
    Those are either escape hatches out of the allowlist or a way to make
    formatting expensive, and a narration never needs them.
    """
    fields: list[str] = []
    try:
        parsed = list(_FORMATTER.parse(template))
    except ValueError:
        # Malformed template (unbalanced braces) — treat as unrenderable.
        return None
    for _literal, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if conversion is not None or format_spec:
            return None
        if not field_name.isidentifier():
            return None
        fields.append(field_name)
    return fields


def _sanitize(value: Any) -> str | None:
    """Coerce an allowlisted arg to a short, single-line string.

    Returns ``None`` when the value is unusable (wrong type, or empty once
    stripped), which the caller turns into a ``bare`` fallback.
    """
    # ``bool`` is an ``int`` subclass, so it would sail through the Number
    # check and interpolate as "True" — never what a phrase wants.
    if isinstance(value, bool) or not isinstance(value, str | Number):
        return None
    text = value if isinstance(value, str) else str(value)
    text = _CONTROL_CHARS.sub(" ", text)
    text = _WHITESPACE_RUN.sub(" ", text).strip()
    if not text:
        return None
    if len(text) > _MAX_VALUE_LEN:
        text = text[: _MAX_VALUE_LEN - len(_ELLIPSIS)].rstrip() + _ELLIPSIS
    return text


def render(narration: Narration | None, args: dict | None) -> str | None:
    """Render ``narration`` against a tool call's arguments.

    Returns the active phrase when every field it names is allowlisted and
    present, the bare phrase when it isn't, and ``None`` when there is no
    narration to render at all (an unannotated tool).
    """
    if narration is None:
        return None

    bare = (narration.bare or "").strip() or None
    template = narration.active or ""
    fields = _template_fields(template)
    if fields is None:
        return bare
    if not fields:
        # No placeholders — ``active`` is already a literal phrase.
        return template.strip() or bare

    # A template naming a field outside ``safe_args`` is a programming error.
    # Fall back rather than interpolate: the whole point of the allowlist is
    # that an un-vetted field never reaches a user-visible string.
    allowed = set(narration.safe_args)
    unlisted = [name for name in fields if name not in allowed]
    if unlisted:
        logger.warning(
            "Narration template %r names non-allowlisted field(s) %s — falling back to bare phrase",
            template,
            sorted(set(unlisted)),
        )
        return bare

    source = args if isinstance(args, dict) else {}
    values: dict[str, str] = {}
    for name in fields:
        clean = _sanitize(source.get(name))
        if clean is None:
            return bare
        values[name] = clean

    try:
        return template.format(**values).strip() or bare
    except (IndexError, KeyError, ValueError):
        # Every field was validated above, so this is belt-and-braces: a
        # narration must never be able to break the call it describes.
        logger.debug("Narration template %r failed to format", template, exc_info=True)
        return bare


# Tool name -> (module, class) for the builtin tools that declare a Narration.
# Kept explicit rather than walking every builtin so a lookup never imports the
# world (or an optional dependency) just to phrase a status line. HTN-2 replaces
# this with the real registry lookup plus the derive-from-name fallback.
_ANNOTATED_TOOLS: dict[str, tuple[str, str]] = {
    "web_search": ("pocketpaw.tools.builtin.web_search", "WebSearchTool"),
}


@lru_cache(maxsize=256)
def narration_for_tool(tool_name: str) -> Narration | None:
    """Look up the ``Narration`` a builtin tool declares, by tool name.

    Returns ``None`` for any tool that isn't annotated — callers treat that as
    "no narration", never as a reason to invent one.
    """
    target = _ANNOTATED_TOOLS.get(tool_name)
    if target is None:
        return None
    module_path, class_name = target
    try:
        tool = getattr(import_module(module_path), class_name)()
        narration = tool.narration
    except Exception:
        logger.debug("Narration lookup failed for tool %r", tool_name, exc_info=True)
        return None
    return narration if isinstance(narration, Narration) else None
