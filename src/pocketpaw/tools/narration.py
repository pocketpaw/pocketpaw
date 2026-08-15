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
#
# Updated: 2026-08-15 (HTN-2) — every tool reads as English, not just the one
# annotated tool. ``_ANNOTATED_TOOLS`` (the one-entry name -> (module, class)
# stopgap) is DELETED: it mapped to builtin classes, so MCP and connector tools
# could never appear in it, and reading a narration through it CONSTRUCTED the
# tool. ``narration_for_tool`` now resolves in three steps —
#
#   1. the ``Narration`` declared on the LIVE instance a ``ToolRegistry``
#      already holds. Never construct a tool to read a property:
#      ``ShellTool.__init__`` calls ``get_settings()``, so a registry-wide
#      version of the old instantiate-to-read pattern would build settings, and
#      whatever the credential store does on first load, on the event loop just
#      to phrase a status line.
#   2. ``_NARRATION_OVERRIDES`` — phrasing for tools that are external and
#      cannot self-declare (MCP servers, connector surfaces, proxy-side tools).
#   3. derive-from-name — strip the vendor prefix, find a verb in a small fixed
#      lexicon, phrase it verb-first. Deterministic string work, no LLM.
#
# and returns ``None`` when all three come up empty, so a caller omits the field
# rather than inventing a phrase. A derived phrase never interpolates arguments:
# derivation reads the tool's NAME, and a name carries no ``safe_args``
# allowlist, so there is nothing that would make an argument safe to show.

from __future__ import annotations

import logging
import re
import string
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
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


# Phrasing for tools that cannot declare a ``Narration`` of their own. An MCP
# server's tools and a connector's hosted actions are defined outside this
# codebase, so there is no class to annotate — this table is the only place
# their phrasing can live.
#
# This is NOT the deleted ``_ANNOTATED_TOOLS`` under a new name. That map named
# a module and a class and CONSTRUCTED the tool to read a property off it; this
# one holds finished ``Narration`` values and imports nothing. A builtin belongs
# on its own class (``BaseTool.narration``), never here.
#
# Keying is by wire name, which is the only identity the bridge has. See the
# collision caveat in ``narration_for_tool``.
_NARRATION_OVERRIDES: dict[str, Narration] = {
    # LiteLLM's proxy exposes web search under this name, with a ``query``
    # parameter. Derivation alone would read it as the bare "Searching the web"
    # — correct but incurious — because a derived phrase never interpolates
    # arguments. Declaring it here is what puts the query back in the sentence.
    #
    # This entry does NOT make the registry lookup optional, and the two cover
    # different tools. With LiteLLM's search interception off, the cloud path
    # calls the proxy's search tool under THIS name and narrates from this
    # table, needing no registry. The builtin ``web_search`` — our own
    # ``WebSearchTool`` via the MCP bridge — is a different row of that table
    # and declares its own phrase, which is reachable only through the live
    # registry. Delete the seam and this entry keeps the cloud path speaking
    # while the builtin quietly drops its query.
    "litellm_web_search": Narration(
        active="Searching the web for {query}",
        bare="Searching the web",
        safe_args=("query",),
    ),
}

# Verb -> gerund. Deliberately small and fixed: a derived phrase is a fallback,
# so it needs to be predictable and wrong-in-a-boring-way rather than clever. A
# name whose verb is not in here derives nothing and narrates nothing.
_VERB_LEXICON: dict[str, str] = {
    "publish": "Publishing",
    "create": "Creating",
    "search": "Searching",
    "invite": "Inviting",
    "delete": "Deleting",
    "update": "Updating",
    "list": "Listing",
    "send": "Sending",
    "read": "Reading",
    "write": "Writing",
    "run": "Running",
    "fetch": "Fetching",
}

# Leading tokens that name the vendor rather than the thing being acted on.
# Stripped only from the FRONT, so ``pocketpaw_sites_publish`` loses its prefix
# while a hypothetical ``export_pocketpaw`` keeps its object.
_VENDOR_PREFIXES = frozenset({"mcp", "pocketpaw", "paw", "litellm", "composio"})

# Object tokens that are a NAME rather than a common noun, mapped to how they
# are spelled in a sentence. A name takes no article: the article rule that
# makes ``sites_publish`` read "Publishing the site" would otherwise make
# ``gmail_search`` read "Searching the gmail".
#
# The services here are the ones PocketPaw actually surfaces tools for — see
# ``_COMPOSIO_OVERLAPPING_TOOL_NAMES`` in ``agents/tool_bridge.py``, which is
# where these names come from. One missing from this map degrades to the article
# form, which reads slightly wrong rather than incorrectly.
_PROPER_NOUNS: dict[str, str] = {
    "calendar": "Calendar",
    "discord": "Discord",
    "docs": "Docs",
    "drive": "Drive",
    "github": "GitHub",
    "gmail": "Gmail",
    "jira": "Jira",
    "linear": "Linear",
    "notion": "Notion",
    "python": "Python",
    "reddit": "Reddit",
    "sheets": "Sheets",
    "slack": "Slack",
    "telegram": "Telegram",
    "youtube": "YouTube",
}

# Splits a name into words: an acronym run (``HTTP``), a capitalized word
# (``Fetch``), or a lowercase/digit run. The default backend's own builtins are
# camelCase — ``WebSearch``, ``TodoWrite``, ``WebFetch`` — so without this they
# derive nothing at all, being a single unrecognisable token.
_WORD_RUN = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")

# A tool NAME is externally controlled — a user-added MCP server names its own
# tools — and a derived phrase embeds it, so the name is validated before any
# of it reaches a user-visible string. ASCII word characters only: that rejects
# the bidi overrides and zero-width characters ``_strip_invisibles`` exists to
# catch, before they ever get near a phrase. Length is capped here so the
# derived phrase is bounded without a second cap downstream.
_DERIVABLE_NAME = re.compile(r"^[A-Za-z0-9_]{1,80}$")
_MAX_NAME_TOKENS = 8


def _name_tokens(segment: str) -> list[str]:
    """Split one name segment into lowercase words.

    Handles both conventions a tool name arrives in: ``sites_publish`` and
    ``TodoWrite``.
    """
    return [word.lower() for word in _WORD_RUN.findall(segment)]


def _strip_vendor_prefix(tokens: list[str]) -> list[str]:
    index = 0
    while index < len(tokens) and tokens[index] in _VENDOR_PREFIXES:
        index += 1
    return tokens[index:]


def _singularize(word: str) -> str:
    """Naive trailing-plural trim — enough for tool names, not for English.

    Tool names are short machine identifiers ("sites", "entries"), so the two
    common plural forms cover the surface. The ``ss``/``us``/``is``/``os``
    guard keeps "status", "analysis" and "address" intact.
    """
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 2 and word.endswith("s") and not word.endswith(("ss", "us", "is", "os")):
        return word[:-1]
    return word


@lru_cache(maxsize=256)
def _derive_narration(tool_name: str) -> Narration | None:
    """Phrase a tool call from the tool's NAME alone.

    ``pocketpaw_sites_publish`` -> "Publishing the site". Returns ``None`` when
    the name carries no verb this can recognise (``shell``), because a phrase
    invented from a name we cannot read is worse than the raw name: it is a
    confident sentence about something the agent may not be doing.

    The result carries no placeholders, so it renders identically whatever the
    call's arguments were.
    """
    if not isinstance(tool_name, str) or not _DERIVABLE_NAME.match(tool_name):
        return None

    # ``mcp__<server>__<tool>`` is how Claude Code namespaces an in-process MCP
    # tool. The last segment is the tool itself; the segment before it is the
    # server, which is the only place an object noun lives when the tool
    # segment is a bare verb (``mcp__pocketpaw_pocket_specialist__create``).
    segments = [segment for segment in tool_name.split("__") if segment]
    if not segments:
        return None
    tokens = _strip_vendor_prefix(_name_tokens(segments[-1]))
    if not tokens or len(tokens) > _MAX_NAME_TOKENS:
        return None

    # Three orders occur in the wild, so the verb is looked for in all of them:
    #   sites_publish, web_search        verb last, object before it
    #   create_pocket, send_message      verb first, object after it
    #   gmail_send, gmail_list_labels    service first, then the verb-first form
    # The trailing position wins, because a name that ENDS in a verb is using it
    # as a verb; a name that merely contains one may be using it as a noun
    # (``search_index_update`` is an update, not a search).
    if tokens[-1] in _VERB_LEXICON:
        verb, object_tokens = tokens[-1], tokens[:-1]
    else:
        for index, token in enumerate(tokens):
            if token in _VERB_LEXICON:
                verb, object_tokens = token, tokens[index + 1 :]
                break
        else:
            return None

    if not object_tokens and len(segments) >= 2:
        # ``mcp__pocketpaw_pocket_specialist__create`` — the tool segment is a
        # bare verb, so the server segment is the only noun on offer.
        object_tokens = _strip_vendor_prefix(_name_tokens(segments[-2]))
    object_tokens = [token for token in object_tokens if token != "tool"]
    if len(object_tokens) > _MAX_NAME_TOKENS:
        return None

    gerund = _VERB_LEXICON[verb]
    if not object_tokens:
        # A verb with no object still beats the raw identifier, and inventing
        # an object would be inventing a fact.
        phrase = gerund
    elif len(object_tokens) == 1 and object_tokens[0] in _PROPER_NOUNS:
        phrase = f"{gerund} {_PROPER_NOUNS[object_tokens[0]]}"
    else:
        # "list" is inherently plural — singularizing it gives "Listing the
        # file" for ``list_files``, which is the one place the trim reads worse
        # than leaving the name alone.
        if verb != "list":
            object_tokens[-1] = _singularize(object_tokens[-1])
        phrase = f"{gerund} the {' '.join(object_tokens)}"

    return Narration(active=phrase, bare=phrase)


def _declared_narration(tool_name: str, registry: Any | None) -> Narration | None:
    """Read the ``Narration`` off the LIVE instance ``registry`` holds.

    Never constructs anything. The registry already owns instantiated tools
    (``ToolRegistry.register`` stores the instance), so a lookup is a dict get
    and an attribute read — no ``__init__`` runs, no settings get built, no
    credential store gets touched to phrase a status line.

    ``.narration`` IS THE ONLY ATTRIBUTE THIS MAY TOUCH on the tool. Not
    ``definition``, not ``parameters``, never ``execute``. Narration is a
    description of a call, so it needs one declaration and nothing else, and a
    lookup that reaches further turns a status line into a second caller of the
    tool surface. Widening this is a change to the security boundary, not a
    convenience.

    Every step is guarded because none of it is our code: ``registry`` is
    duck-typed, ``get`` may be anything callable, and ``narration`` is a
    property a tool author wrote.
    """
    if registry is None:
        return None
    getter = getattr(registry, "get", None)
    if not callable(getter):
        return None
    try:
        tool = getter(tool_name)
        if tool is None:
            return None
        narration = getattr(tool, "narration", None)
    except Exception:
        logger.debug("Narration lookup failed for tool %r", tool_name, exc_info=True)
        return None
    return narration if isinstance(narration, Narration) else None


def narration_for_tool(tool_name: str, registry: Any | None = None) -> Narration | None:
    """Resolve the ``Narration`` for a tool call, by tool name.

    Resolution order, first hit wins:

    1. the narration declared on the live instance in ``registry`` (when the
       caller has one);
    2. ``_NARRATION_OVERRIDES``, for external tools that cannot self-declare;
    3. derive-from-name.

    Returns ``None`` when none of them phrase it. Callers omit the field on
    ``None`` — they never fall back to a phrase of their own.

    IDENTITY CAVEAT: every step keys on the bare wire name, which is the only
    identity a caller has at this point. On backends that emit unprefixed MCP
    tool names (``agents/codex_cli.py``), a user-added MCP server exposing a
    tool called ``web_search`` would inherit whatever ``web_search`` means
    here. Resolving that needs the tool's ORIGIN (the server field the caller
    would have to read alongside the name), not a change in this function.
    """
    if not isinstance(tool_name, str) or not tool_name:
        return None
    return (
        _declared_narration(tool_name, registry)
        or _NARRATION_OVERRIDES.get(tool_name)
        or _derive_narration(tool_name)
    )
