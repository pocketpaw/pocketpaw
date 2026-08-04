# tests/test_start_flow_contract_is_shared.py — one tool, one contract.
# Created: 2026-08-04 (fix/prompt-tells-the-truth).
#
# WHAT THIS CAUGHT. ``start_flow`` is exposed on two surfaces —
# ``tools/builtin/flow_tool.py`` (every runtime backend) and
# ``agents/sdk_mcp_widgets.py`` (the Claude SDK in-process MCP server) — and
# each carried its own hand-written description and JSON schema. They measured
# 82.7% similar: close enough that nobody reading either one would notice, far
# enough apart to change behaviour.
#
# THEY HAD ALREADY DRIFTED, ASYMMETRICALLY. The SDK copy carried four rules the
# runtime copy did not:
#
#   * "DO NOT fake a flow with a single ``set``-stepped spec" — the anti-pattern
#     that renders step 1 and dead-ends, and the reason start_flow exists.
#   * a terminal ``complete`` uses ``action:``, NEVER ``type:``/``kind:``.
#   * approve / reject / fulfill is a ``call_binding`` ACTION BUTTON, not a
#     yes/no select.
#   * the builder REPAIRS recoverable slips and REJECTS only real structural
#     bugs — the retry contract that makes a failed call worth retrying.
#
# So the backend most deployments actually run was briefed worse than the one
# fewer of them run, and nothing said so. This is the SECOND split of this exact
# pair; the 2026-06-15 "SPLIT-BRAIN FIX" landed because the SDK handler was
# preset-only while the prompt taught flat graphs. Twice is a pattern, and the
# pattern is two copies.
#
# WHY EQUALITY AND NOT A CHECKLIST. Asserting the four recovered rules alone
# would let the copies re-fork anywhere else. Equality is the invariant that
# actually holds: there is one text and one schema, and both surfaces return
# it. The rule checks below are kept as well, because equality would still pass
# if someone deleted a rule from the shared constant.
#
# EACH TEST NAMES THE MUTATION THAT BREAKS IT, and every one was applied, run,
# observed to fail, and reverted (``scripts/mutate.py``).

from __future__ import annotations

import pytest


def _runtime_tool():
    from pocketpaw.tools.builtin.flow_tool import StartFlowTool

    return StartFlowTool()


def _sdk_description() -> str:
    from pocketpaw.agents.sdk_mcp_widgets import _start_flow_description

    return _start_flow_description()


def _sdk_parameters() -> dict:
    from pocketpaw.agents.sdk_mcp_widgets import _start_flow_parameters

    return _start_flow_parameters()


class TestBothSurfacesShareOneContract:
    def test_the_descriptions_are_identical(self) -> None:
        """THE MUTATION THAT BREAKS THIS: give the SDK surface its own text
        again by returning a literal from ``_start_flow_description``. Run:
        failed. (Applied 2026-08-04.)"""
        assert _runtime_tool().description == _sdk_description(), (
            "the two start_flow surfaces describe the tool differently — that is "
            "how the last split-brain started"
        )

    def test_the_schemas_are_identical(self) -> None:
        """The schema was the quieter half of the duplication: same seven
        properties, written out twice, with the ``flow_type`` enum read from
        ``FLOW_TYPES`` in both places.

        THE MUTATION THAT BREAKS THIS: drop the ``title`` property from
        ``start_flow_parameters`` and hand the SDK a literal schema. Run:
        failed. (Applied 2026-08-04.)
        """
        assert _runtime_tool().parameters == _sdk_parameters()

    def test_the_shared_contract_comes_from_the_builder(self) -> None:
        """The contract lives next to the code that enforces it.

        ``_flows.py`` owns both the builder and the text describing it, so the
        prose and the validator are edited in one place. Anywhere else and they
        drift again.

        THE MUTATION THAT BREAKS THIS: move ``START_FLOW_DESCRIPTION`` into
        ``flow_tool.py`` and import it from there. Run: failed. (Applied
        2026-08-04.)
        """
        from pocketpaw.ripple._flows import START_FLOW_DESCRIPTION, start_flow_parameters

        assert _runtime_tool().description is START_FLOW_DESCRIPTION
        assert _runtime_tool().parameters == start_flow_parameters()

    def test_the_schema_is_not_a_shared_mutable(self) -> None:
        """Two callers must not be able to corrupt each other's schema.

        ``start_flow_parameters`` is a function partly for this: a module-level
        dict would hand every caller the same object, and one in-place edit
        would silently change the other surface's tool definition.

        THE MUTATION THAT BREAKS THIS: cache the dict in a module global and
        return it. Run: the mutation leaked into the second call and this
        failed. (Applied 2026-08-04.)
        """
        from pocketpaw.ripple._flows import start_flow_parameters

        first = start_flow_parameters()
        first["properties"]["flow"]["description"] = "clobbered"
        assert start_flow_parameters()["properties"]["flow"]["description"] != "clobbered"

    def test_the_flow_type_enum_tracks_the_builder(self) -> None:
        """A hardcoded enum is how the preset list goes stale.

        THE MUTATION THAT BREAKS THIS: hardcode the enum to
        ``["onboarding_wizard"]``. Run: failed. (Applied 2026-08-04.)
        """
        from pocketpaw.ripple._flows import FLOW_TYPES

        assert _runtime_tool().parameters["properties"]["flow_type"]["enum"] == list(FLOW_TYPES)


class TestTheRulesThatHadDrifted:
    """Equality alone would still pass if a rule were deleted from the shared
    constant, so the four rules the runtime backends were missing are pinned by
    name. Each is here because it was absent from the copy that shipped to most
    deployments."""

    @pytest.mark.parametrize(
        ("rule", "why"),
        [
            ("`set`-stepped", "the anti-pattern start_flow exists to prevent"),
            ("NEVER `type:`/`kind:`", "the terminal action key the model reaches for wrongly"),
            ("ACTION BUTTON", "approve/reject is an action, not a yes/no select"),
            ("REPAIRS recoverable", "the repair-vs-reject contract that makes a retry worthwhile"),
        ],
    )
    def test_runtime_backends_get_the_rule(self, rule: str, why: str) -> None:
        """THE MUTATION THAT BREAKS THIS: delete the rule from
        ``START_FLOW_DESCRIPTION``. Run: failed for each of the four.
        (Applied 2026-08-04.)"""
        assert rule in _runtime_tool().description, f"lost: {why}"


class TestThePromptDoesNotRestateTheSchema:
    """The third copy, and the reason this file is not only about two files.

    ``_MULTI_STEP_FLOW_RULE`` re-taught the step shape in prose — steps, kinds,
    next/branch, the terminal complete, per-step actions, ``{stepId.field}`` —
    all of which the tool schema already specifies and the builder already
    enforces. The prompt now routes to the schema and keeps what the schema
    cannot say: when to reach for a flow at all, the two anti-patterns, and the
    worked skeletons.
    """

    def test_the_prompt_points_at_the_schema_rather_than_repeating_it(self) -> None:
        """THE MUTATION THAT BREAKS THIS: restore the HOW TO AUTHOR prose block
        listing the step keys. Run: failed. (Applied 2026-08-04.)"""
        from pocketpaw.ripple._inline import INLINE_RIPPLE_SYSTEM_PROMPT as prompt

        assert "start_flow`'s own schema" in prompt, (
            "the prompt must send the model to the tool schema for the step shape"
        )
        assert '- `next: "<id>"`        → linear next step' not in prompt, (
            "the prompt is restating the transition keys the schema owns"
        )

    def test_the_prompt_keeps_what_the_schema_cannot_say(self) -> None:
        """Deduplication must not become deletion. Routing rules, anti-patterns
        and worked examples are not in the tool schema and have to survive.

        THE MUTATION THAT BREAKS THIS: delete SKELETON C from the prompt. Run:
        failed. (Applied 2026-08-04.)
        """
        from pocketpaw.ripple._inline import INLINE_RIPPLE_SYSTEM_PROMPT as prompt

        for kept in (
            "`set`-stepped",  # the anti-pattern, stated where the model plans
            "cannot mis-nest",  # why a flat graph is safe
            "Never leave an action flow",  # reading the request, not shaping output
            "ask-user-questions",  # the routing boundary
            "SKELETON A",
            "SKELETON C",
            "SKELETON E",
        ):
            assert kept in prompt, f"deduplication removed something only the prompt had: {kept}"
