# Tests for humanized tool narration (HTN-1).
# Created: 2026-08-15 — locks the rendering contract in
# ``pocketpaw.tools.narration``. Most of these are security tests, not
# formatting tests: tool args are model-authored and routinely carry secrets,
# so the allowlist and the sanitizer are the things that must not regress.

from __future__ import annotations

from pocketpaw.tools.narration import Narration, narration_for_tool, render

SEARCH = Narration(
    active="Searching the web for {query}",
    bare="Searching the web",
    safe_args=("query",),
)


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
    decides what can be interpolated. A tool author who writes ``{api_key}``
    into a phrase must get the bare fallback, not a leaked credential."""
    leaky = Narration(
        active="Searching {query} with {api_key}",
        bare="Searching the web",
        safe_args=("query",),
    )
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


def test_template_without_placeholders_renders_as_is():
    narration = Narration(active="Thinking it over", bare="Thinking")
    assert render(narration, {"anything": "ignored"}) == "Thinking it over"


def test_format_escape_hatches_fall_back_to_bare():
    """Attribute/index access, conversions and format specs are all routes
    around the allowlist (or ways to make formatting expensive). Each must
    fall back rather than render."""
    hatches = [
        "Searching {query.__class__}",
        "Searching {query[0]}",
        "Searching {query!r}",
        "Searching {query:>999999}",
        "Searching {0}",
        "Searching {query",  # malformed
    ]
    for template in hatches:
        narration = Narration(active=template, bare="Searching the web", safe_args=("query",))
        assert render(narration, {"query": "filings"}) == "Searching the web", (
            f"template {template!r} was rendered instead of falling back"
        )


def test_unrenderable_active_with_no_bare_yields_none():
    """Nothing safe to say means say nothing — not an empty string."""
    narration = Narration(active="Searching for {secret}", bare="", safe_args=())
    assert render(narration, {"secret": "x"}) is None


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
