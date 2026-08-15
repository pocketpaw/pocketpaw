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


def test_narration_lookup_by_tool_name():
    assert narration_for_tool("web_search") is not None
    assert render(narration_for_tool("web_search"), {"query": "filings"}) == (
        "Searching the web for filings"
    )


def test_unannotated_tool_has_no_narration():
    """HTN-1 annotates exactly one tool; everything else stays silent until
    HTN-2 lands the derive-from-name fallback."""
    assert narration_for_tool("shell") is None
    assert narration_for_tool("pocketpaw_sites_publish") is None
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
