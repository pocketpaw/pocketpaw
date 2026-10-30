# Tests for humanized tool narration (HTN-1).
# Created: 2026-08-15 — locks the rendering contract in
# ``pocketpaw.tools.narration``. Most of these are security tests, not
# formatting tests: tool args are model-authored and routinely carry secrets,
# so the allowlist and the sanitizer are the things that must not regress.
# Updated: 2026-08-15 (security review) — added coverage for the Cf category
# (bidi overrides, zero-width characters), declaration-time validation of
# ``safe_args``, ``str``-subclass normalization, and the bounded sanitize path.
# Invalid declarations now raise, so the tests that prove ``render`` still
# fails closed build their Narration through ``_unvalidated``.
#
# Invisible characters are written as \uXXXX escapes, never as literals: a
# literal is unreviewable in a diff, and an editor or tool that rewrites the
# file can silently mangle it (one became a NUL byte while this file was being
# written, which broke collection outright).
#
# Updated: 2026-08-15 (HTN-2) — coverage for the lookup chain that replaced the
# ``_ANNOTATED_TOOLS`` stopgap: declared-on-the-live-instance, then the override
# table, then derive-from-name, then nothing. Three of these are security tests
# rather than phrasing tests:
#   - the lookup must never CONSTRUCT a tool to read its narration (a registry
#     entry whose ``__init__`` raises and records that it ran);
#   - a derived phrase must be argument-blind, since a NAME carries no
#     ``safe_args`` allowlist that could make an argument safe to show;
#   - a tool name is externally controlled — a user-added MCP server names its
#     own tools — and a derived phrase embeds it, so the hostile-name table
#     covers the same attacks the argument sanitizer already defends against.

from __future__ import annotations

import unicodedata

import pytest

from pocketpaw.tools.narration import Narration, narration_for_tool, render

SEARCH = Narration(
    active="Searching the web for {query}",
    bare="Searching the web",
    safe_args=("query",),
)

# Categories the sanitizer promises to remove.
_INVISIBLE_CATEGORIES = {"Cc", "Cf", "Zl", "Zp"}

# Bidi embeddings, overrides and isolates.
_BIDI_CONTROLS = (
    "\u202a",  # LRE
    "\u202b",  # RLE
    "\u202c",  # PDF
    "\u202d",  # LRO
    "\u202e",  # RLO
    "\u2066",  # LRI
    "\u2067",  # RLI
    "\u2068",  # FSI
    "\u2069",  # PDI
)

# Invisible but not White_Space.
_ZERO_WIDTH = (
    "\u200b",  # ZERO WIDTH SPACE
    "\u200c",  # ZERO WIDTH NON-JOINER
    "\u200d",  # ZERO WIDTH JOINER
    "\ufeff",  # ZERO WIDTH NO-BREAK SPACE / BOM
    "\u00ad",  # SOFT HYPHEN
    "\u2060",  # WORD JOINER
)


def _unvalidated(active: str, bare: str, safe_args) -> Narration:
    """Build a Narration bypassing ``__post_init__``.

    Declaration-time validation is the first line of defense; ``render`` failing
    closed is the second. These must be tested independently, or a regression
    that removes the second one goes unnoticed behind the first.
    """
    narration = object.__new__(Narration)
    object.__setattr__(narration, "active", active)
    object.__setattr__(narration, "bare", bare)
    object.__setattr__(narration, "safe_args", safe_args)
    return narration


def test_interpolates_an_allowlisted_arg():
    """The happy path: an allowlisted arg lands in the active phrase."""
    assert (
        render(SEARCH, {"query": "quarterly filings"}) == "Searching the web for quarterly filings"
    )


def test_no_narration_renders_nothing():
    """An unannotated tool produces no narration — never an invented one."""
    assert render(None, {"query": "anything"}) is None


def test_missing_safe_arg_falls_back_to_bare():
    assert render(SEARCH, {}) == "Searching the web"
    assert render(SEARCH, None) == "Searching the web"


def test_empty_safe_arg_falls_back_to_bare():
    """Empty / whitespace-only args would render "Searching the web for "."""
    assert render(SEARCH, {"query": ""}) == "Searching the web"
    assert render(SEARCH, {"query": "   "}) == "Searching the web"


def test_non_scalar_safe_arg_falls_back_to_bare():
    """Only strings and numbers interpolate — never a dict/list/None repr."""
    for bad in ({"nested": 1}, ["a", "b"], None, object()):
        assert render(SEARCH, {"query": bad}) == "Searching the web"


def test_bool_safe_arg_falls_back_to_bare():
    """``bool`` is an ``int`` subclass — it must not slip through as "True"."""
    assert render(SEARCH, {"query": True}) == "Searching the web"


def test_numeric_safe_arg_interpolates():
    narration = Narration(active="Reading page {page}", bare="Reading", safe_args=("page",))
    assert render(narration, {"page": 42}) == "Reading page 42"


def test_non_allowlisted_field_never_reaches_output():
    """THE security test: a template may name a field, but only ``safe_args``
    decides what can be interpolated. Declaration-time validation now rejects
    this shape outright, so build it unvalidated to prove ``render`` ALSO
    refuses — the leak must be closed at both layers."""
    leaky = _unvalidated("Searching {query} with {api_key}", "Searching the web", ("query",))
    result = render(leaky, {"query": "filings", "api_key": "sk-live-SUPERSECRET"})

    assert result == "Searching the web"
    assert "SUPERSECRET" not in result
    assert "api_key" not in result


def test_args_dict_is_never_rendered_wholesale():
    """No "render whatever args exist" fallback — unreferenced args stay out."""
    result = render(SEARCH, {"query": "filings", "password": "hunter2", "num_results": 5})

    assert result == "Searching the web for filings"
    assert "hunter2" not in result
    assert "num_results" not in result


def test_newlines_and_control_chars_are_stripped():
    """An arg must not be able to inject newlines or escape sequences into a
    status line that channel adapters write straight to a terminal."""
    result = render(SEARCH, {"query": "line one\nline two\r\tand\x1b[31m more"})

    assert "\n" not in result
    assert "\r" not in result
    assert "\t" not in result
    assert "\x1b" not in result
    # Control chars collapse to a single space rather than vanishing, so words
    # either side of one don't get silently glued together.
    assert result == "Searching the web for line one line two and [31m more"


# --- Cf category: bidi overrides and zero-width characters -------------------


def test_bidi_override_is_stripped():
    """A right-to-left override makes a bidi-aware renderer reorder the run, so
    attacker text can visually cross the trusted "Searching the web for" prefix.
    Reachable by ordinary indirect prompt injection: a poisoned page leads the
    agent to search with a bidi-laden query."""
    result = render(SEARCH, {"query": "\u202egnihton gnitteled"})

    assert "\u202e" not in result
    assert result == "Searching the web for gnihton gnitteled"


def test_every_bidi_control_is_stripped():
    """The whole embedding/override/isolate family, not just RLO."""
    for ch in _BIDI_CONTROLS:
        result = render(SEARCH, {"query": f"before{ch}after"})
        # Assert the whole phrase, not just the character's absence: a broken
        # implementation that dropped the value entirely would also satisfy
        # "the control is gone", and that is not the behavior we want here.
        assert result == "Searching the web for before after", (
            f"U+{ord(ch):04X} was not stripped cleanly: {result!r}"
        )


def test_zero_width_only_value_falls_back_to_bare():
    """U+200B is category Cf, not White_Space — ``\\s`` and ``str.strip()`` both
    leave it alone, so before this was stripped the empty->bare check never
    fired and the wire carried a phrase with a blank slot."""
    assert render(SEARCH, {"query": "\u200b" * 90}) == "Searching the web"


def test_each_zero_width_character_alone_falls_back_to_bare():
    for ch in _ZERO_WIDTH:
        assert render(SEARCH, {"query": ch * 10}) == "Searching the web", (
            f"U+{ord(ch):04X} alone did not fall back to bare"
        )


def test_zero_width_padding_never_reaches_the_truncation_cap():
    """The original bug produced 79 invisible characters plus an ellipsis — the
    cap fired on content with no visible width at all. The empty check runs
    against the STRIPPED text, so padding can never get that far, however long
    it is: past the 80-char cap, and past the sanitize scan limit."""
    from pocketpaw.tools.narration import _SANITIZE_SCAN_LIMIT

    zwsp = _ZERO_WIDTH[0]
    for count in (90, _SANITIZE_SCAN_LIMIT + 500, 5_000):
        assert render(SEARCH, {"query": zwsp * count}) == "Searching the web", (
            f"{count} zero-width characters did not fall back to bare"
        )


def test_zero_width_padding_around_real_text_is_removed():
    """Padding must be stripped without taking the real value with it, and
    without eating into the cap."""
    zwsp = _ZERO_WIDTH[0]
    result = render(SEARCH, {"query": zwsp * 200 + "visible" + zwsp * 200})

    assert result == "Searching the web for visible"
    assert zwsp not in result


def test_no_invisible_characters_survive():
    """Blanket guard over the categories the sanitizer promises to remove."""
    hostile = "a\u200bb\u202ec\ufeffd\x00e\u00adf\u2028g"
    result = render(SEARCH, {"query": hostile})

    leaked = [ch for ch in result if unicodedata.category(ch) in _INVISIBLE_CATEGORIES]
    assert not leaked, f"invisible characters survived: {[hex(ord(c)) for c in leaked]}"
    # ...and the visible letters between them are still there, so this can't be
    # satisfied by an implementation that simply threw the value away.
    for letter in "abcdefg":
        assert letter in result, f"visible text was lost: {result!r}"


def test_emoji_zwj_sequences_split_a_known_accepted_cost():
    """Stripping the whole Cf category takes U+200D with it, so a multi-person
    emoji splits into its components: the phrase stays readable but the glyph
    is no longer joined.

    This is deliberate, and pinned here so it does not get "fixed" by exempting
    U+200D. Doing that would reopen the zero-width padding bypass for that
    character — a ZWJ-only value would once again be invisible content that
    sails past the empty check. Cosmetic degradation in a status line is the
    cheaper side of that trade.
    """
    zwj = "\u200d"
    woman_technologist = "\U0001f469" + zwj + "\U0001f4bb"

    result = render(SEARCH, {"query": woman_technologist})

    assert result == "Searching the web for \U0001f469 \U0001f4bb"
    assert zwj not in result

    # Plain emoji, variation selectors and skin-tone modifiers are NOT Cf and
    # must come through untouched.
    assert render(SEARCH, {"query": "\U0001f600 recipes"}) == (
        "Searching the web for \U0001f600 recipes"
    )
    assert render(SEARCH, {"query": "\U0001f44d\U0001f3fd"}) == (
        "Searching the web for \U0001f44d\U0001f3fd"
    )


def test_ordinary_unicode_text_is_preserved():
    """Stripping targets invisibles, not non-ASCII — a real query in Hindi or
    Japanese must still render."""
    assert render(SEARCH, {"query": "तिमाही रिपोर्ट"}) == "Searching the web for तिमाही रिपोर्ट"
    assert render(SEARCH, {"query": "四半期報告"}) == "Searching the web for 四半期報告"


def test_non_breaking_space_is_folded_not_stripped():
    """NBSP is category Zs and IS White_Space, so the whitespace collapse folds
    it to a plain space — it must not reach the wire as a literal U+00A0."""
    result = render(SEARCH, {"query": "quarterly\u00a0filings"})

    assert result == "Searching the web for quarterly filings"
    assert "\u00a0" not in result


# --- Truncation and bounded work --------------------------------------------


def test_long_values_are_truncated_to_80_chars():
    """A pasted essay in a tool arg can't blow out the status line."""
    result = render(SEARCH, {"query": "x" * 500})

    prefix = "Searching the web for "
    assert result.startswith(prefix)
    value = result[len(prefix) :]
    assert len(value) == 80, f"interpolated value was {len(value)} chars: {value!r}"
    assert value.endswith("…")


def test_value_just_under_the_cap_is_untouched():
    """Off-by-one guard: exactly 80 chars must not gain an ellipsis."""
    result = render(SEARCH, {"query": "y" * 80})

    assert result == "Searching the web for " + "y" * 80
    assert "…" not in result


def test_sanitizing_is_bounded_before_the_cap():
    """Sanitizing truncates to a scan limit BEFORE walking the value, so work is
    bounded by the limit rather than by the caller's input size.

    The observable consequence: content past the scan limit cannot influence the
    result. Here the whole scanned window is whitespace, so the value sanitizes
    to empty and falls back to bare — if sanitizing still walked the full string
    it would find "hello" and render it.
    """
    from pocketpaw.tools.narration import _SANITIZE_SCAN_LIMIT

    padded = " " * (_SANITIZE_SCAN_LIMIT + 80) + "hello"
    assert render(SEARCH, {"query": padded}) == "Searching the web"


def test_very_large_value_renders_capped():
    """A multi-MB argument must still produce a capped one-line phrase."""
    result = render(SEARCH, {"query": "z" * 5_000_000})

    prefix = "Searching the web for "
    assert result.startswith(prefix)
    assert len(result[len(prefix) :]) == 80


# --- Hostile objects ---------------------------------------------------------


def test_hostile_str_subclass_never_reaches_format():
    """``.format()`` calls ``__format__`` on its arguments, so a ``str``
    subclass reaching it runs model-influenced code. Sanitizing normalizes to an
    exact ``str`` explicitly; this used to hold only as a side effect of
    ``re.sub`` copying its input.

    Asserting the fully interpolated phrase matters: if normalization regressed,
    the override would raise and ``render``'s guard would quietly return ``bare``,
    so a weaker assertion would still pass.
    """

    class _Hostile(str):
        def __format__(self, spec):  # pragma: no cover - must never run
            raise AssertionError("__format__ override ran")

        def __str__(self):  # pragma: no cover - must never run
            raise AssertionError("__str__ override ran")

    assert render(SEARCH, {"query": _Hostile("filings")}) == "Searching the web for filings"


def test_hostile_number_that_raises_falls_back_to_bare():
    """``_sanitize`` calls ``str(value)`` on a Number, which can raise from code
    we don't control. Channel adapters call ``render`` directly and have no
    blanket catch of their own, so the guard has to cover sanitizing too."""

    class _HostileNumber(int):
        def __str__(self):
            raise RuntimeError("boom")

    assert render(SEARCH, {"query": _HostileNumber(5)}) == "Searching the web"


def test_hostile_args_mapping_falls_back_to_bare():
    """Even the lookup can raise — ``args`` is a dict handed in by a caller."""

    class _HostileDict(dict):
        def get(self, *_args, **_kwargs):
            raise RuntimeError("boom")

    assert render(SEARCH, _HostileDict()) == "Searching the web"


# --- Declaration-time validation --------------------------------------------


def test_safe_args_missing_comma_is_rejected():
    """``safe_args=("query")`` is a str, and ``set()`` explodes it into the
    per-character allowlist {'q','u','e','r','y'}. HTN-3 hand-authors ~100 of
    these declarations, which makes this typo close to certain."""
    with pytest.raises(TypeError, match="trailing comma"):
        Narration(
            active="Searching the web for {query}",
            bare="Searching the web",
            safe_args=("query"),  # noqa: UP034 - the typo under test
        )


def test_single_character_safe_args_typo_fails_closed():
    """The fail-OPEN case that motivated the check: with a str ``safe_args``,
    a single-character field name lands inside the exploded character set and
    would have been allowlisted by accident."""
    with pytest.raises(TypeError):
        Narration(active="Reading page {q}", bare="Reading", safe_args=("q"))  # noqa: UP034


def test_str_safe_args_also_fails_closed_at_render():
    """Second layer for the same typo. ``__post_init__`` rejects a str
    ``safe_args``, but ``copy.deepcopy`` and unpickling both rebuild a dataclass
    without running it, so ``render`` must not explode the str into a
    per-character allowlist either."""
    single_char = _unvalidated("Reading page {q}", "Reading", "q")

    assert render(single_char, {"q": "secret-value"}) == "Reading"

    multi_char = _unvalidated("Searching the web for {query}", "Searching the web", "query")
    assert render(multi_char, {"query": "filings"}) == "Searching the web"


def test_safe_args_rejects_non_string_entries():
    with pytest.raises(TypeError):
        Narration(active="Reading page {page}", bare="Reading", safe_args=(1,))


def test_safe_args_accepts_a_list_and_normalizes_to_tuple():
    narration = Narration(active="Reading page {page}", bare="Reading", safe_args=["page"])

    assert narration.safe_args == ("page",)
    assert render(narration, {"page": 3}) == "Reading page 3"


def test_active_naming_an_unlisted_field_is_rejected_at_declaration():
    """The leak now fails at declaration rather than degrading silently at first
    render, where nobody is watching."""
    with pytest.raises(ValueError, match="api_key"):
        Narration(
            active="Searching {query} with {api_key}",
            bare="Searching the web",
            safe_args=("query",),
        )


def test_escape_hatch_templates_are_rejected_at_declaration():
    """Attribute/index access, conversions, format specs and positional fields
    are all routes around the allowlist (or ways to make formatting expensive)."""
    for template in (
        "Searching {query.__class__}",
        "Searching {query[0]}",
        "Searching {query!r}",
        "Searching {query:>999999}",
        "Searching {0}",
        "Searching {query",  # malformed
    ):
        with pytest.raises(ValueError):
            Narration(active=template, bare="Searching the web", safe_args=("query",))


def test_escape_hatch_templates_also_fail_closed_at_render():
    """Second layer: if such a declaration ever reaches ``render`` anyway, it
    falls back rather than rendering."""
    for template in (
        "Searching {query.__class__}",
        "Searching {query[0]}",
        "Searching {query!r}",
        "Searching {query:>999999}",
        "Searching {0}",
        "Searching {query",
    ):
        narration = _unvalidated(template, "Searching the web", ("query",))
        assert render(narration, {"query": "filings"}) == "Searching the web", (
            f"template {template!r} was rendered instead of falling back"
        )


def test_bare_with_a_placeholder_is_rejected():
    """``bare`` is the fallback that must always stand alone — a placeholder
    there reaches the wire as literal braces at exactly the moment
    interpolation has already failed."""
    with pytest.raises(ValueError, match="no placeholders"):
        Narration(active="Searching for {query}", bare="Searching {query}", safe_args=("query",))


def test_non_string_active_or_bare_is_rejected():
    with pytest.raises(TypeError):
        Narration(active=None, bare="Searching the web")
    with pytest.raises(TypeError):
        Narration(active="Searching the web", bare=None)


def test_template_without_placeholders_renders_as_is():
    narration = Narration(active="Thinking it over", bare="Thinking")
    assert render(narration, {"anything": "ignored"}) == "Thinking it over"


def test_unrenderable_active_with_no_bare_yields_none():
    """Nothing safe to say means say nothing — not an empty string."""
    narration = _unvalidated("Searching for {secret}", "", ())
    assert render(narration, {"secret": "x"}) is None


# --- Tool wiring -------------------------------------------------------------


def test_web_search_declares_the_reference_narration():
    """The one annotated tool (HTN-1's reference implementation)."""
    from pocketpaw.tools.builtin.web_search import WebSearchTool

    narration = WebSearchTool().narration

    assert narration is not None
    assert narration.safe_args == ("query",)
    # The allowlisted arg must be a real parameter of the tool, or the phrase
    # silently degrades to bare forever.
    assert "query" in WebSearchTool().parameters["properties"]
    assert render(narration, {"query": "quarterly filings"}) == (
        "Searching the web for quarterly filings"
    )


def test_narration_lookup_reads_the_declaration_off_a_live_registry():
    """The declared phrase is read off the instance the registry already holds.

    HTN-1 looked this up through a name -> (module, class) map and CONSTRUCTED
    the tool. HTN-2 deletes that map: the registry owns live instances, so a
    lookup is a dict get and an attribute read.
    """
    from pocketpaw.tools.builtin.web_search import WebSearchTool
    from pocketpaw.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(WebSearchTool())

    assert render(narration_for_tool("web_search", registry), {"query": "filings"}) == (
        "Searching the web for filings"
    )


def test_unannotated_tool_derives_a_phrase_instead_of_a_raw_identifier():
    """HTN-2's headline behaviour: a tool that declares nothing still reads as
    English, and the raw snake_case identifier never reaches a user."""
    assert render(narration_for_tool("pocketpaw_sites_publish"), {"pocket_id": "p1"}) == (
        "Publishing the site"
    )


def test_a_name_with_no_recognisable_verb_narrates_nothing():
    """The chain ends at None, never at an invented phrase. ``shell`` carries
    no verb this can read, so the caller omits the field and the surface keeps
    whatever fallback it already had."""
    assert narration_for_tool("shell") is None
    assert render(narration_for_tool("shell"), {"command": "ls"}) is None


def test_base_tool_defaults_to_no_narration():
    from pocketpaw.tools.protocol import BaseTool

    class _Bare(BaseTool):
        @property
        def name(self) -> str:
            return "bare_tool"

        @property
        def description(self) -> str:
            return "does nothing"

        async def execute(self, **params):
            return ""

    assert _Bare().narration is None


# ---------------------------------------------------------------------------
# HTN-2 — the lookup chain: declared (live instance) -> override -> derived.
# ---------------------------------------------------------------------------


class _FakeRegistry:
    """Duck-typed stand-in for ``ToolRegistry``.

    The lookup only ever calls ``.get(name)``, so a test can hand it something
    that is deliberately NOT a tool instance — which is how the
    never-construct proof below works.
    """

    def __init__(self, entries: dict):
        self._entries = entries

    def get(self, name: str):
        return self._entries.get(name)


def test_the_stopgap_annotated_tools_map_is_deleted():
    """HTN-1's ``_ANNOTATED_TOOLS`` was a name -> (module, class) map with one
    entry. It could not describe an MCP or connector tool by construction, and
    reading through it instantiated the tool. Growing it was the wrong fix, so
    the regression guard is that the symbol is gone entirely."""
    from pocketpaw.tools import narration as narration_module

    assert not hasattr(narration_module, "_ANNOTATED_TOOLS")


def test_narration_lookup_never_constructs_a_tool():
    """The security constraint this whole task is built around.

    ``ShellTool.__init__`` calls ``get_settings()``, so a lookup that
    instantiated a tool to read a property would build settings — and whatever
    the credential store does on first load — on the event loop, just to phrase
    a status line. The registry entry here is a CLASS whose ``__init__`` raises
    and records that it ran: if anything calls it, the list below is not empty.
    """
    constructed: list[str] = []

    class _Exploding:
        name = "pocketpaw_sites_publish"

        def __init__(self):
            constructed.append("boom")
            raise RuntimeError("a narration lookup must never run this")

        @property
        def narration(self):  # pragma: no cover - only reachable via an instance
            return None

    registry = _FakeRegistry({"pocketpaw_sites_publish": _Exploding})

    # Falls through to derivation rather than raising or constructing.
    assert render(narration_for_tool("pocketpaw_sites_publish", registry), {}) == (
        "Publishing the site"
    )
    assert constructed == [], "the lookup constructed a tool to read its narration"


def test_a_narration_property_that_raises_falls_through_to_derivation():
    """``narration`` is a property a tool author wrote, so it can raise. A
    broken declaration must not take the status line down with it."""

    class _Broken:
        name = "pocketpaw_sites_publish"

        @property
        def narration(self):
            raise RuntimeError("bad declaration")

    registry = _FakeRegistry({"pocketpaw_sites_publish": _Broken()})

    assert render(narration_for_tool("pocketpaw_sites_publish", registry), {}) == (
        "Publishing the site"
    )


def test_a_registry_that_misbehaves_is_survivable():
    """``registry`` is duck-typed — a caller can hand over anything."""

    class _Hostile:
        def get(self, name):
            raise RuntimeError("no")

    assert render(narration_for_tool("pocketpaw_sites_publish", _Hostile()), {}) == (
        "Publishing the site"
    )
    assert narration_for_tool("pocketpaw_sites_publish", object()) is not None
    assert narration_for_tool("shell", object()) is None


def test_declaration_beats_the_override_table():
    """Resolution order: a tool that declares its own phrasing owns it, even
    when the name is also in the override table."""

    class _Declared:
        name = "litellm_web_search"
        narration = Narration(active="Consulting the archive", bare="Consulting the archive")

    registry = _FakeRegistry({"litellm_web_search": _Declared()})

    assert render(narration_for_tool("litellm_web_search", registry), {"query": "x"}) == (
        "Consulting the archive"
    )


def test_the_override_table_beats_derivation():
    """``litellm_web_search`` derives to the bare "Searching the web" on its
    name alone — a derived phrase never interpolates arguments, because a name
    carries no ``safe_args`` allowlist. The override is what puts the query
    back in the sentence."""
    assert render(narration_for_tool("litellm_web_search"), {"query": "quarterly filings"}) == (
        "Searching the web for quarterly filings"
    )


def test_the_override_still_honours_the_safe_args_allowlist():
    """An override is a declaration like any other, so the allowlist governs it
    too — this is the ONE entry that interpolates, so it is the one that has to
    be proven not to leak."""
    rendered = render(
        narration_for_tool("litellm_web_search"),
        {"query": "quarterly filings", "api_key": "sk-live-SUPERSECRET"},
    )

    assert rendered == "Searching the web for quarterly filings"
    assert "SUPERSECRET" not in rendered


def test_the_override_falls_back_to_bare_without_the_query():
    assert render(narration_for_tool("litellm_web_search"), {}) == "Searching the web"


# Real names from the surfaces this task exists to cover. The MCP names are the
# ``mcp__<server>__<tool>`` form Claude Code emits (``agents/sdk_mcp_atlas.py``,
# ``agents/claude_sdk.py:494``); the connector names are lifted verbatim from
# ``_COMPOSIO_OVERLAPPING_TOOL_NAMES`` in ``agents/tool_bridge.py``; the
# camelCase ones are the default backend's own builtins.
@pytest.mark.parametrize(
    ("tool_name", "expected"),
    [
        # The PRD's worked example.
        ("pocketpaw_sites_publish", "Publishing the site"),
        # MCP surface — namespaced, and a bare-verb tool segment borrows its
        # object noun from the server segment.
        ("mcp__pocketpaw_pocket_specialist__create", "Creating the pocket specialist"),
        ("mcp__pocketpaw_widgets__list_widgets", "Listing the widgets"),
        # Connector surface — a service name takes no article.
        ("gmail_search", "Searching Gmail"),
        ("gmail_list_labels", "Listing the labels"),
        ("gmail_create_label", "Creating the label"),
        ("docs_read", "Reading Docs"),
        ("drive_list", "Listing Drive"),
        ("reddit_search", "Searching Reddit"),
        # Verb in the middle: the service prefix is dropped, not narrated.
        ("github_create_issue", "Creating the issue"),
        # Default-backend builtins are camelCase.
        ("WebSearch", "Searching the web"),
        ("TodoWrite", "Writing the todo"),
        ("Read", "Reading"),
        # Plurals are trimmed for the article form, except after "list".
        ("delete_entries", "Deleting the entry"),
        ("list_files", "Listing the files"),
        # "status" is not a plural.
        ("update_status", "Updating the status"),
        ("run_python", "Running Python"),
    ],
)
def test_derived_phrasing_for_real_tool_names(tool_name, expected):
    assert render(narration_for_tool(tool_name), {}) == expected


@pytest.mark.parametrize(
    "tool_name",
    [
        "shell",  # no verb in the lexicon
        "bash",
        "NotebookEdit",  # "edit" is deliberately not in the lexicon
        "gmail_trash",
        "",
        "___",
    ],
)
def test_names_that_narrate_nothing(tool_name):
    """Nothing is invented. The caller omits the field and the surface keeps
    whatever fallback it already had."""
    assert narration_for_tool(tool_name) is None


def test_derivation_never_interpolates_arguments():
    """A derived phrase is built from the NAME. The name carries no
    ``safe_args`` allowlist, so nothing could make an argument safe to show —
    a derived narration must be argument-blind."""
    rendered = render(
        narration_for_tool("pocketpaw_sites_publish"),
        {"pocket_id": "p1", "api_key": "sk-live-SUPERSECRET", "query": "quarterly filings"},
    )

    assert rendered == "Publishing the site"
    assert "SUPERSECRET" not in rendered
    assert "quarterly" not in rendered


# Invisible characters are \uXXXX escapes, never literals — see this file's
# header. A tool NAME is externally controlled (a user-added MCP server names
# its own tools) and a derived phrase embeds it, so every attack the renderer
# defends against for ARGUMENTS applies to the name too.
@pytest.mark.parametrize(
    ("label", "tool_name"),
    [
        ("bidi override", "sites_\u202epublish"),
        ("zero width", "sites\u200b_publish"),
        ("newline", "sites_publish\nfake"),
        ("ansi escape", "sites_publish\u001b[31m"),
        ("brace injection", "sites_{query}_publish"),
        ("non-ascii", "sites_pubÍish"),
        ("overlong", "a_" * 200 + "publish"),
    ],
)
def test_a_hostile_tool_name_derives_nothing(label, tool_name):
    """The name is validated before any of it reaches a phrase. Rejecting
    outright beats sanitizing: a name we cannot read is a name we cannot
    describe honestly."""
    assert narration_for_tool(tool_name) is None, label


def test_a_derived_phrase_carries_no_placeholders():
    """Derivation builds a template ``render`` will call ``.format()`` on, so a
    brace reaching it would either raise or interpolate. The name gate is what
    makes that impossible; this pins the property itself."""
    for tool_name in ("pocketpaw_sites_publish", "gmail_search", "TodoWrite"):
        narration = narration_for_tool(tool_name)
        assert "{" not in narration.active and "}" not in narration.active
        assert narration.safe_args == ()
        assert narration.active == narration.bare


def test_lookup_ignores_a_non_string_name():
    assert narration_for_tool(None) is None
    assert narration_for_tool(123) is None
