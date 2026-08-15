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
#
# Updated: 2026-08-15 (HTN-1 security review) —
#   - strip the whole Cf (format) category, not just C0/C1 controls. Bidi
#     overrides (U+202A-202E) let attacker text visually cross the trusted
#     prefix, and zero-width characters (U+200B) are NOT White_Space, so a
#     zero-width-only value slipped past the empty->bare check.
#   - ``Narration`` validates itself in ``__post_init__``: ``safe_args=("query")``
#     (a missing comma) is a ``str``, which ``set()`` explodes into a
#     per-character allowlist that fails OPEN for single-character fields.
#   - normalize ``str`` subclasses explicitly rather than relying on ``re.sub``
#     to copy them, so a hostile ``__format__`` can never reach ``.format()``.
#   - bound sanitizing work before it starts: truncate to a scan limit first so
#     a multi-MB tool arg can't drive full-size copies on the event loop.

from __future__ import annotations

import logging
import re
import string
import unicodedata
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

# Unicode categories replaced with a space before substitution:
#   Cc — C0/C1 controls: newlines into a status line, terminal escape sequences
#        into a channel adapter that writes to a TTY.
#   Cf — format characters. Two distinct attacks live here, which is why the
#        whole category goes rather than a hand-picked list. Bidirectional
#        overrides (U+202A-202E, U+2066-2069) make a renderer reorder the run
#        so attacker text visually crosses the trusted "Searching the web for"
#        prefix. Zero-width characters (U+200B, U+FEFF, U+00AD) are invisible
#        but are NOT White_Space, so ``\s`` and ``str.strip()`` both leave them
#        alone and a zero-width-only value would sail past the empty check and
#        put a phrase with a blank slot on the wire.
#   Zl/Zp — line and paragraph separators.
# Zs (NBSP, ideographic space) is deliberately absent: those ARE White_Space,
# so the whitespace collapse below already folds them.
_STRIPPED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
_WHITESPACE_RUN = re.compile(r"\s+")

# Sanitizing walks the value character by character and allocates copies, so it
# is bounded before it starts rather than after. The slack over _MAX_VALUE_LEN
# absorbs characters that sanitizing removes, so the real cap below still has
# material to work with.
_SANITIZE_SCAN_LIMIT = _MAX_VALUE_LEN * 4

_FORMATTER = string.Formatter()


@dataclass(frozen=True)
class Narration:
    """Plain-language phrasing for a tool call.

    ``active`` may interpolate arguments, but ONLY those named in
    ``safe_args``. ``bare`` carries no arguments at all and is the fallback
    whenever ``active`` cannot be rendered safely, so it must stand on its own
    as a complete phrase.

    OUTPUT CONTRACT — the rendered phrase is PLAIN TEXT. It is sanitized
    (control and format characters removed, length capped) but it is NOT
    escaped for any markup language, and it embeds tool arguments, which are
    model-authored. Consumers must render it as text: assign it to
    ``textContent``, never to ``innerHTML``, and never interpolate it into a
    markdown or HTML template. A channel adapter sending it to Telegram or
    Slack with a ``parse_mode`` set must escape it for that mode first, or an
    argument containing ``<``, ``_`` or ``*`` will mangle the message or make
    the send fail outright.

    Validated on construction: a template that names a field outside
    ``safe_args`` is a declaration error and raises here rather than silently
    degrading to ``bare`` at first render.
    """

    active: str  # "Searching the web for {query}"
    bare: str  # "Searching the web"
    safe_args: tuple[str, ...] = ()  # allowlist — ONLY these may interpolate

    def __post_init__(self) -> None:
        if not isinstance(self.active, str) or not isinstance(self.bare, str):
            raise TypeError("Narration.active and Narration.bare must both be str")

        # ``safe_args=("query")`` — a missing trailing comma, the easy typo in
        # a one-element tuple — is a plain str, and ``set()`` would turn it into
        # the per-character allowlist {'q','u','e','r','y'}. That fails closed
        # for realistic field names but fails OPEN for any single-character
        # field, so reject the type outright instead of trusting the shape.
        if isinstance(self.safe_args, str) or not isinstance(self.safe_args, tuple | list):
            raise TypeError(
                f"Narration.safe_args must be a tuple of field names, got "
                f"{type(self.safe_args).__name__!r}. A one-element tuple needs "
                f'its trailing comma: safe_args=("query",)'
            )
        if not all(isinstance(name, str) for name in self.safe_args):
            raise TypeError("Narration.safe_args entries must all be str")
        # Normalize list -> tuple so the declared type holds and the frozen
        # dataclass stays hashable.
        object.__setattr__(self, "safe_args", tuple(self.safe_args))

        fields = _template_fields(self.active)
        if fields is None:
            raise ValueError(
                f"Narration.active is not a renderable template: {self.active!r}. "
                "Positional fields, attribute/index access, conversions and "
                "format specs are all rejected."
            )
        unlisted = sorted({name for name in fields if name not in set(self.safe_args)})
        if unlisted:
            raise ValueError(
                f"Narration.active names field(s) {unlisted} that are not in "
                f"safe_args={tuple(self.safe_args)!r}. Add them to safe_args only "
                "if they are safe to show a user; otherwise remove them from the phrase."
            )
        # ``bare`` is the fallback that must always be renderable on its own, so
        # it may not carry placeholders — they would reach the wire as literal
        # braces at exactly the moment interpolation has already failed.
        if _template_fields(self.bare):
            raise ValueError(
                f"Narration.bare must be a complete phrase with no placeholders, got {self.bare!r}"
            )


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


def _strip_invisibles(text: str) -> str:
    """Replace control and format characters with a space.

    Replaced rather than deleted so words either side of a stripped character
    don't get silently glued together.
    """
    if not any(unicodedata.category(ch) in _STRIPPED_CATEGORIES for ch in text):
        return text
    return "".join(" " if unicodedata.category(ch) in _STRIPPED_CATEGORIES else ch for ch in text)


def _sanitize(value: Any) -> str | None:
    """Coerce an allowlisted arg to a short, single-line string.

    Returns ``None`` when the value is unusable (wrong type, or empty once
    stripped), which the caller turns into a ``bare`` fallback.
    """
    # ``bool`` is an ``int`` subclass, so it would sail through the Number
    # check and interpolate as "True" — never what a phrase wants.
    if isinstance(value, bool) or not isinstance(value, str | Number):
        return None

    raw = value if isinstance(value, str) else str(value)

    # Bound the work BEFORE the character walk and the copies it makes. A tool
    # arg can be large by design (a shell command, a file's contents, a
    # connector payload), and this runs on the response stream's own task.
    if len(raw) > _SANITIZE_SCAN_LIMIT:
        raw = raw[:_SANITIZE_SCAN_LIMIT]

    # Normalize a ``str`` SUBCLASS down to an exact ``str``, explicitly. A
    # subclass can override ``__format__`` (and ``__str__``), and ``.format()``
    # calls ``__format__`` on its arguments — so a subclass reaching that call
    # runs model-influenced code. This used to be closed only as a side effect
    # of ``re.sub()`` copying its input, which a refactor that skipped the
    # regex would have silently reopened. ``str.__str__`` is the base
    # implementation, so a subclass override cannot intercept it.
    text = raw if type(raw) is str else str.__str__(raw)

    text = _strip_invisibles(text)
    text = _WHITESPACE_RUN.sub(" ", text).strip()
    # Must come after stripping: a value of only zero-width characters is
    # visually empty but is not White_Space, so this is what catches it.
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
    #
    # ``__post_init__`` rejects a str ``safe_args``, but it is not the only way
    # an instance can exist: ``copy.deepcopy`` and unpickling both rebuild a
    # dataclass without running it. Re-check here so the character-exploded
    # allowlist can never fail open, whatever route the instance took.
    allowed = set() if isinstance(narration.safe_args, str) else set(narration.safe_args)
    unlisted = [name for name in fields if name not in allowed]
    if unlisted:
        logger.warning(
            "Narration template %r names non-allowlisted field(s) %s — falling back to bare phrase",
            template,
            sorted(set(unlisted)),
        )
        return bare

    source = args if isinstance(args, dict) else {}
    # The guard covers sanitizing as well as formatting. ``_sanitize`` calls
    # ``str(value)`` on a model-supplied object, and ``dict.get`` runs
    # ``__hash__`` / ``__eq__`` — any of which can raise from code we don't
    # control. Callers must be able to trust the docstring's promise that a
    # narration cannot break the call it describes; the design routes channel
    # adapters at ``render`` directly, and those have no blanket catch of their
    # own the way the agent bridge does.
    try:
        values: dict[str, str] = {}
        for name in fields:
            clean = _sanitize(source.get(name))
            if clean is None:
                return bare
            values[name] = clean
        return template.format(**values).strip() or bare
    except Exception:
        logger.debug("Narration template %r failed to render", template, exc_info=True)
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
