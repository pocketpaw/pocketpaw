# tests/test_prompt_names_only_real_tools.py — a prompt may not command a tool
# the agent does not have.
# Created: 2026-08-04 (fix/prompt-tells-the-truth).
#
# WHAT THIS CAUGHT. The chat-inline system prompt carried a block titled
# ``# MUST CALL BEFORE EMIT`` that read, in part, "you MUST call
# `get_inline_widget_help(types=[...])` … there is no excuse to skip it",
# closing with "if the tool returns an error, OMIT the widget rather than
# guess."
#
# That tool lives on the ``pocketpaw_widgets`` in-process MCP server, which is
# constructed in exactly one file — ``agents/claude_sdk.py``. Every other
# backend, this deployment's ``pydantic_ai`` included, ran without it. The tool
# did not error; it was absent. The agent's only consistent reading of an
# unsatisfiable precondition was the stated fallback, so the deployment quietly
# lost every widget outside the core six, which is the opposite of what the
# block was written to achieve.
#
# WHY A CONTRACT AND NOT A FIX. The specific defect is one line. The CLASS is
# that prompt text and tool wiring live in different files, are edited by
# different changes, and nothing compares them — so the prompt drifts into
# commanding tools that no longer exist, and the failure is silent because a
# model given an impossible instruction improvises rather than raising.
# ``start_flow`` had the same defect and the same cause (it shipped in the
# runtime registry and was never bridged to the MCP surface; the prompt taught
# a flat step-graph while the tool accepted only presets).
#
# WHAT THIS DOES NOT COVER. Only the inline ripple prompt, and only tools it
# names in call form. Blocks assembled in the EE cloud layer are gated per
# backend and tested next to that assembly, in
# ``tests/cloud/test_agent_service_tools_context.py``.
#
# EACH TEST NAMES THE MUTATION THAT BREAKS IT, and every one was applied, run,
# observed to fail, and reverted (``scripts/mutate.py``).

from __future__ import annotations

import re

import pytest

# A backticked call: `get_widget_spec(` — the form the prompt uses when it
# tells the model to invoke something. Bare backticked words are excluded on
# purpose: the prompt also quotes prop names, widget types and JSON keys, and
# treating those as tool names would make the test noise.
_CALL_IN_PROMPT = re.compile(r"`([a-z_][a-z0-9_]{2,})\(")


def _inline_prompt() -> str:
    from pocketpaw.ripple._inline import INLINE_RIPPLE_SYSTEM_PROMPT

    return INLINE_RIPPLE_SYSTEM_PROMPT


def _runtime_tool_names() -> set[str]:
    """Tools every backend gets, via the runtime builtin registry."""
    from pocketpaw.tools.cli import _TOOLS

    return set(_TOOLS)


class TestEveryToolTheInlinePromptCommandsExists:
    def test_called_tools_are_all_in_the_runtime_registry(self) -> None:
        """The gate itself.

        The inline prompt is backend-agnostic — it is appended for every
        backend — so a tool it names in call form has to be reachable from
        every backend, which means the runtime builtin registry. Reaching only
        the SDK's MCP surface is precisely the bug.

        THE MUTATION THAT BREAKS THIS: point the MUST-CALL block back at
        ``get_inline_widget_help`` before that tool was bridged into the
        registry (i.e. revert both the prompt line and the registry entry).
        Run: the name resolved nowhere and this failed naming it.
        (Applied 2026-08-04.)
        """
        named = set(_CALL_IN_PROMPT.findall(_inline_prompt()))
        assert named, "extractor found no tool calls at all — it has stopped measuring anything"

        missing = sorted(named - _runtime_tool_names())
        assert not missing, (
            f"the inline prompt tells the agent to call {missing}, which no backend "
            "can reach — it is not in the runtime builtin registry. Either bridge the "
            "tool (see tools/builtin/widget_spec.py) or stop naming it."
        )

    @pytest.mark.parametrize("tool_name", ["get_widget_spec", "get_inline_widget_help"])
    def test_the_widget_reference_tools_reach_every_backend(self, tool_name: str) -> None:
        """Pins the specific pair, so a registry cleanup cannot quietly drop them.

        The test above only fires if the PROMPT still names the tool. Someone
        removing a tool and its prompt mention together would pass it while
        re-opening the capability gap, so the pair is asserted directly.

        THE MUTATION THAT BREAKS THIS: delete the ``WidgetSpecTool()`` entry
        from ``tools/cli.py::_TOOLS``. Run: this failed for get_widget_spec.
        (Applied 2026-08-04.)
        """
        assert tool_name in _runtime_tool_names()

    def test_they_are_offered_and_not_withheld_on_a_shared_process(self) -> None:
        """Present in the registry is not the same as offered to the model.

        ``pydantic_ai`` withholds any bridged tool it has not explicitly
        classified — a deliberate fail-closed default — so a registry entry
        alone would still leave the prompt commanding an absent tool. Both
        tools are in-process reads of a static manifest and a static catalog:
        no tenant data, no host state.

        THE MUTATION THAT BREAKS THIS: remove ``get_widget_spec`` from
        ``_TENANT_SAFE_TOOLS``. Run: it landed in the withheld set and this
        failed. (Applied 2026-08-04.)
        """
        from pocketpaw.agents.pydantic_ai import _TENANT_SAFE_TOOLS, _WITHHELD_TOOLS

        for tool_name in ("get_widget_spec", "get_inline_widget_help"):
            assert tool_name in _TENANT_SAFE_TOOLS, (
                f"{tool_name} is unclassified, so pydantic_ai withholds it — the "
                "prompt would command a tool the model cannot see"
            )
            assert tool_name not in _WITHHELD_TOOLS


class TestTheMandateNamesTheToolThatCanAnswerIt:
    """The second half of the defect, and the subtler one.

    Both tools existed on the SDK backend, so the mandate was satisfiable
    there — it just pointed at the tool that could not answer. Measured on the
    same widgets:

        get_widget_spec         definition-list  759 chars, the prop schema
        get_inline_widget_help  definition-list  18,623 chars, no schema

    ``definition-list`` is the example the block itself cites as having shipped
    broken with ``description`` where the manifest says ``definition``. The
    agent called the mandated tool, received 18k characters that named the
    widget only in a catalog listing, and guessed. Prop names come from the
    manifest; ``get_widget_spec`` is what reads it.
    """

    def test_the_prop_schema_mandate_names_get_widget_spec(self) -> None:
        """Asserts the MANDATE SENTENCE, not a mention anywhere in the block.

        A first draft checked ``"get_widget_spec(types=" in block``, and the
        mutation that swapped the mandate back to ``get_inline_widget_help``
        escaped it: the block names ``get_widget_spec`` three more times, in
        the batching line and the worked example. Whichever tool the MUST
        sentence names is the one the model calls, so that sentence is what has
        to be pinned.

        THE MUTATION THAT BREAKS THIS: swap the tool named in the MUST-CALL
        sentence back to ``get_inline_widget_help``. Run: failed. (Applied
        2026-08-04.)
        """
        prompt = _inline_prompt()
        start = prompt.index("# MUST CALL BEFORE EMIT")
        # The prompt is hard-wrapped, so the mandate spans lines. Collapse
        # whitespace before matching or the assertion tracks line breaks.
        block = " ".join(prompt[start : start + 2000].split())

        assert "you MUST call `get_widget_spec(types=[...])`" in block, (
            "the prop-schema mandate must name get_widget_spec — it is the tool "
            "that reads the manifest"
        )

    def test_the_block_keeps_the_two_tools_distinct(self) -> None:
        """The fix is only durable if the block says WHY they differ.

        Naming the right tool without distinguishing it invites the next editor
        to 'simplify' by collapsing them again — they have near-identical names
        and adjacent purposes.

        THE MUTATION THAT BREAKS THIS: delete the paragraph separating the two
        tools. Run: failed. (Applied 2026-08-04.)
        """
        prompt = _inline_prompt()
        start = prompt.index("# MUST CALL BEFORE EMIT")
        block = prompt[start : start + 2000]

        assert "get_inline_widget_help" in block, (
            "the block must say what the OTHER tool is for, or the two get conflated again"
        )
        assert "DIFFERENT" in block or "does not answer" in block
