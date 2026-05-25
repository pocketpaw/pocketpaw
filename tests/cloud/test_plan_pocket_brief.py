# Created: 2026-05-25 (feat/pocket-planner-skill) — unit tests for the
#   plan_pocket brief schema parser. Two modules in scope:
#
#     * ``_parse_pocket_prd_sections`` — splits the PRD output into the
#       five labelled sections (NARRATIVE / WIDGETS / STATE / SOURCES /
#       ACTIONS).
#     * ``_build_pocket_brief`` — turns the raw PRD + parsed TaskSpec
#       list into the brief dict the MCP tool returns and the chat
#       agent renders as markdown.
#     * ``_compose_intent_with_iteration`` — splices prior_plan +
#       iteration_delta into the brief on follow-up calls.
#
# The brief parser is the contract between the planner LLM output and
# the markdown render. These tests pin the section delimiters, the
# bullet-line format, and the TaskSpec → todo projection so a planner
# prompt tweak that changes the output shape fails loudly here instead
# of silently shipping a busted brief to the chat agent.
"""Tests for the ``plan_pocket`` brief schema parser.

The MCP tool itself wraps an LLM call so it isn't unit-tested here —
``tests/test_deep_work_planner.py`` already covers PlannerAgent.
These tests exercise the pure-Python brief construction so we can
catch parser regressions without burning LLM time.
"""

from __future__ import annotations

import pytest
from pocketpaw_ee.agent.mcp_servers.planner import (
    _build_pocket_brief,
    _compose_intent_with_iteration,
    _parse_bullet_pairs,
    _parse_pocket_prd_sections,
)

from pocketpaw.deep_work.models import TaskSpec


class TestParsePocketPrdSections:
    """Pin the section-splitting contract for the PRD prompt output."""

    def test_well_formed_prd_splits_into_five_sections(self) -> None:
        prd = """\
## NARRATIVE
A sales command center for tracking the quarterly pipeline.

It uses a kanban as the focal widget.

## WIDGETS
- kanban: deal stages
- stat: total pipeline value
- form-layout: add-deal composer

## STATE
- cards: list[dict] — pipeline cards
- draft: str — text input buffer
- next_id: int — monotonic id counter

## SOURCES
- crm: feeds state.cards via GET /leads

## ACTIONS
- Add Deal button: push on state.cards (new card)
- Stage filter: set on state.filter (the chosen value)
"""
        sections = _parse_pocket_prd_sections(prd)

        assert "A sales command center" in sections["NARRATIVE"]
        assert "kanban: deal stages" in sections["WIDGETS"]
        assert "cards: list[dict]" in sections["STATE"]
        assert "crm: feeds state.cards" in sections["SOURCES"]
        assert "Add Deal button" in sections["ACTIONS"]

    def test_missing_section_returns_empty_string(self) -> None:
        """A malformed PRD without all five sections degrades gracefully.

        The brief parser is the boundary between LLM stochasticity and
        the chat agent's markdown render. If the LLM forgets a section
        the brief shows it as empty, not KeyError.
        """
        prd = """\
## NARRATIVE
Just a narrative — the model forgot the other sections.

## WIDGETS
- stat: a count
"""
        sections = _parse_pocket_prd_sections(prd)

        assert "narrative" in sections["NARRATIVE"].lower()
        assert "stat: a count" in sections["WIDGETS"]
        assert sections["STATE"] == ""
        assert sections["SOURCES"] == ""
        assert sections["ACTIONS"] == ""

    def test_unknown_section_does_not_leak_into_others(self) -> None:
        """A heading we don't recognise closes the current section.

        Without that, an LLM that emits ``## EXTRA NOTES`` between
        STATE and SOURCES would dump that prose into the STATE
        section, producing garbage bullet pairs.
        """
        prd = """\
## NARRATIVE
A pocket.

## STATE
- items: list[dict] — the items

## EXTRA NOTES
This should not land in STATE.

## SOURCES
- none: pocket runs on seeded state
"""
        sections = _parse_pocket_prd_sections(prd)

        assert "items: list[dict]" in sections["STATE"]
        # The "## EXTRA NOTES" block must not leak into STATE.
        assert "should not land in STATE" not in sections["STATE"]
        # And SOURCES is still found after the unknown heading.
        assert "none: pocket runs on seeded state" in sections["SOURCES"]

    def test_empty_prd(self) -> None:
        sections = _parse_pocket_prd_sections("")
        assert all(v == "" for v in sections.values())

    def test_prd_parser_tolerates_heading_drift(self) -> None:
        """Heading-level / case / trailing-punctuation / bold-wrapper
        drift must not silently drop a section.

        The PRD prompt asks for ``## NARRATIVE``, ``## WIDGETS`` etc.
        but LLMs produce variants the brain accepts:
          * ``### Widgets`` — one extra hash level
          * ``**Narrative**`` — bold wrapper, no hash heading at all
          * ``## Sources:`` — trailing colon
          * ``## actions`` — lowercase
        Each variant has been observed in real model output. The
        parser tolerates all four; otherwise the brief silently loses
        the section and the chat agent renders an empty list.
        """
        prd = """\
## Narrative
Drift case: lowercase + h2.

### Widgets
- stat: a metric

**State**
- count: int — the tally

## Sources:
- crm: feeds count via GET /total

## actions
- click: set on count
"""
        sections = _parse_pocket_prd_sections(prd)
        # All five sections must surface non-empty content.
        assert "Drift case" in sections["NARRATIVE"]
        assert "stat: a metric" in sections["WIDGETS"], "h3 heading must be accepted"
        assert "count: int" in sections["STATE"], "bold-wrapper heading must be accepted"
        assert "crm: feeds count" in sections["SOURCES"], "trailing colon must be stripped"
        assert "click: set on count" in sections["ACTIONS"], "lowercase heading must match"


class TestParseBulletPairs:
    """The ``- left: right`` bullet primitive used by every section parser."""

    def test_basic_pairs(self) -> None:
        text = "- kanban: deal stages\n- stat: total pipeline value\n"
        pairs = _parse_bullet_pairs(text)
        assert pairs == [
            ("kanban", "deal stages"),
            ("stat", "total pipeline value"),
        ]

    def test_skips_non_bullet_lines(self) -> None:
        """Prose lines mixed into a bullet list must not become pairs."""
        text = (
            "Here is a list of widgets:\n"
            "- kanban: deal stages\n"
            "\n"
            "More notes.\n"
            "- stat: total pipeline value\n"
        )
        pairs = _parse_bullet_pairs(text)
        assert pairs == [
            ("kanban", "deal stages"),
            ("stat", "total pipeline value"),
        ]

    def test_bullet_without_colon_keeps_raw_text(self) -> None:
        """A bullet line missing the colon comes back as (line, '').

        We'd rather surface the user-visible text than drop the line.
        """
        text = "- kanban with deal stages\n- stat: total\n"
        pairs = _parse_bullet_pairs(text)
        assert pairs == [
            ("kanban with deal stages", ""),
            ("stat", "total"),
        ]


class TestBuildPocketBrief:
    """End-to-end: PRD + TaskSpec list → brief dict."""

    def _well_formed_prd(self) -> str:
        return """\
## NARRATIVE
A sales pipeline tracker. Users drag deals across stages.

The happy path is "add a deal, then drag it forward".

## WIDGETS
- kanban: deal stages
- stat: total pipeline value

## STATE
- cards: list[dict] — pipeline cards
- draft: str — text input buffer

## SOURCES
- crm: feeds state.cards via GET /leads

## ACTIONS
- Add Deal button: push on state.cards (new card from draft)
"""

    def test_brief_carries_all_sections(self) -> None:
        tasks = [
            TaskSpec(
                key="t1",
                title="Seed state.cards with pipeline data",
                description="Initial seed of 8 deals across stages",
                success_criteria=["state.cards has 8+ entries"],
                preconditions=[],
                blocked_by_keys=[],
                tags=["state-seed"],
                estimated_minutes=5,
            ),
            TaskSpec(
                key="t2",
                title="Add kanban widget bound to state.cards",
                description="Place at ui root with four columns",
                success_criteria=[
                    "a kanban node exists at the ui root",
                    "columns are [lead, qualified, proposal, won]",
                ],
                preconditions=["state.cards exists and is a list"],
                blocked_by_keys=["t1"],
                tags=["widget-add"],
                estimated_minutes=8,
            ),
        ]

        brief = _build_pocket_brief(
            prd=self._well_formed_prd(),
            tasks=tasks,
            research_notes="Pattern: dashboard. Focal: kanban.",
        )

        assert brief["narrative"].startswith("A sales pipeline tracker")
        assert brief["widgets"] == [
            {"type": "kanban", "purpose": "deal stages"},
            {"type": "stat", "purpose": "total pipeline value"},
        ]
        assert brief["state"] == {
            "cards": {"type": "list[dict]", "purpose": "pipeline cards"},
            "draft": {"type": "str", "purpose": "text input buffer"},
        }
        assert brief["sources"] == [
            {"connector": "crm", "feeds": "feeds state.cards via GET /leads"},
        ]
        assert brief["actions"] == [
            {
                "trigger": "Add Deal button",
                "effect": "push on state.cards (new card from draft)",
            },
        ]
        assert brief["research_notes"] == "Pattern: dashboard. Focal: kanban."

    def test_brief_projects_taskspec_into_todos(self) -> None:
        tasks = [
            TaskSpec(
                key="t1",
                title="Seed state.cards",
                description="...",
                success_criteria=["state.cards has 8+ entries"],
                preconditions=[],
                blocked_by_keys=[],
                tags=["state-seed"],
                estimated_minutes=5,
            ),
            TaskSpec(
                key="t2",
                title="Add kanban",
                description="...",
                success_criteria=["a kanban node exists at the ui root"],
                preconditions=["state.cards exists and is a list"],
                blocked_by_keys=["t1"],
                tags=["widget-add"],
                estimated_minutes=8,
            ),
        ]

        brief = _build_pocket_brief(
            prd=self._well_formed_prd(),
            tasks=tasks,
            research_notes="",
        )

        assert brief["todos"] == [
            {
                "id": "t1",
                "label": "Seed state.cards",
                "description": "...",
                "success_criteria": ["state.cards has 8+ entries"],
                "preconditions": [],
                "depends_on": [],
                "tags": ["state-seed"],
                "estimated_minutes": 5,
            },
            {
                "id": "t2",
                "label": "Add kanban",
                "description": "...",
                "success_criteria": ["a kanban node exists at the ui root"],
                "preconditions": ["state.cards exists and is a list"],
                "depends_on": ["t1"],
                "tags": ["widget-add"],
                "estimated_minutes": 8,
            },
        ]

    def test_brief_handles_missing_prd_sections_gracefully(self) -> None:
        """An LLM that drops sections still produces a usable brief.

        The chat agent's markdown render branches on emptiness — it can
        say "the planner did not propose any actions" rather than
        crash.
        """
        prd = """\
## NARRATIVE
Minimal brief.

## WIDGETS
- text: a one-line greeting
"""
        brief = _build_pocket_brief(prd=prd, tasks=[], research_notes="")

        assert brief["narrative"] == "Minimal brief."
        assert brief["widgets"] == [{"type": "text", "purpose": "a one-line greeting"}]
        assert brief["state"] == {}
        assert brief["sources"] == []
        assert brief["actions"] == []
        assert brief["todos"] == []

    def test_brief_treats_em_dash_and_hyphen_as_state_separator(self) -> None:
        """LLMs emit ``—`` or `` - `` interchangeably between type and purpose.

        Both must parse — if we only handle one variant, half the
        briefs lose their state purpose strings.
        """
        prd = """\
## NARRATIVE
P.

## STATE
- a: int — counter
- b: str - draft
- c: list - items

## SOURCES
- none: seeded

## ACTIONS
- click: set on state.a
"""
        brief = _build_pocket_brief(prd=prd, tasks=[], research_notes="")
        # All three rows must surface a non-empty purpose.
        assert brief["state"]["a"]["purpose"] == "counter"
        assert brief["state"]["b"]["purpose"] == "draft"
        assert brief["state"]["c"]["purpose"] == "items"


class TestComposeIntentWithIteration:
    """The first call passes intent through. Follow-up calls splice
    in prior_plan + iteration_delta so the LLM iterates instead of
    re-planning from scratch."""

    def test_first_call_passes_intent_through(self) -> None:
        composed = _compose_intent_with_iteration("Build a CRM", None, None)
        assert composed == "Build a CRM"

    def test_iteration_appends_prior_plan_and_delta(self) -> None:
        prior = {"widgets": [{"type": "kanban", "purpose": "stages"}]}
        composed = _compose_intent_with_iteration(
            "Build a CRM",
            prior,
            "drop the kanban, use a feed instead",
        )
        # The original brief must still appear so the LLM does not lose
        # the user's original intent.
        assert "Build a CRM" in composed
        # The revision request must be present.
        assert "drop the kanban" in composed
        # The prior plan must be serialised so the LLM can diff.
        assert "kanban" in composed
        # And the order matters: USER REVISION REQUEST should appear
        # BEFORE PRIOR PLAN so the LLM reads the change before the
        # baseline.
        assert composed.index("USER REVISION REQUEST") < composed.index("PRIOR PLAN")

    def test_iteration_delta_without_prior_plan(self) -> None:
        """Edge case: agent forgot to pass prior_plan but supplied a delta.

        We still want the delta surfaced — the LLM gets a partial
        iteration signal rather than a silent no-op.
        """
        composed = _compose_intent_with_iteration(
            "Build a CRM",
            None,
            "actually make it green",
        )
        assert "Build a CRM" in composed
        assert "actually make it green" in composed
        assert "PRIOR PLAN" not in composed


class TestPlanPocketHandlerArgs:
    """Lightweight arg validation tests for the MCP handler.

    The handler itself wraps an LLM, but its arg-coercion / identity-
    check / error-envelope paths are deterministic and worth pinning.
    """

    @pytest.mark.asyncio
    async def test_missing_intent_returns_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import planner as planner_mod

        # Patch the identity check so it appears we ARE in an SSE stream.
        original = planner_mod._identity
        planner_mod._identity = lambda: ("ws_1", "user_1")
        try:
            result = await planner_mod._plan_pocket_handler({"intent": ""})
        finally:
            planner_mod._identity = original

        assert result.get("is_error") is True
        assert "intent is required" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_no_active_workspace_returns_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import planner as planner_mod

        # _identity returns (None, None) when called outside an SSE stream.
        original = planner_mod._identity
        planner_mod._identity = lambda: (None, None)
        try:
            result = await planner_mod._plan_pocket_handler({"intent": "Build a CRM"})
        finally:
            planner_mod._identity = original

        assert result.get("is_error") is True
        assert "no active workspace" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_prior_plan_must_be_dict(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import planner as planner_mod

        original = planner_mod._identity
        planner_mod._identity = lambda: ("ws_1", "user_1")
        try:
            result = await planner_mod._plan_pocket_handler(
                {"intent": "Build a CRM", "prior_plan": "not a dict"}
            )
        finally:
            planner_mod._identity = original

        assert result.get("is_error") is True
        assert "prior_plan must be a dict" in result["content"][0]["text"]


class TestPlanPocketToolSurface:
    """Pin the public surface of the planner MCP module — the IDs and
    the per-server tuples that drive the allowlist.

    PR #1223 R2 split the original single-server hosting both tools
    into two servers. ``PLANNER_TOOL_IDS`` now belongs to the
    opt-in ``pocketpaw_planner`` server and carries ``plan_project``
    only; ``POCKET_PLANNER_TOOL_IDS`` belongs to the ambient
    ``pocketpaw_pocket_planner`` server and carries ``plan_pocket``.
    """

    def test_plan_pocket_tool_id_constant(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.planner import (
            PLAN_POCKET_TOOL_ID,
            POCKET_PLANNER_SERVER_NAME,
            POCKET_PLANNER_TOOL_IDS,
        )

        assert PLAN_POCKET_TOOL_ID == f"mcp__{POCKET_PLANNER_SERVER_NAME}__plan_pocket"
        assert PLAN_POCKET_TOOL_ID in POCKET_PLANNER_TOOL_IDS
        assert len(POCKET_PLANNER_TOOL_IDS) == 1

    def test_plan_project_tool_id_constant(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.planner import (
            PLAN_PROJECT_TOOL_ID,
            PLANNER_TOOL_IDS,
            SERVER_NAME,
        )

        assert PLAN_PROJECT_TOOL_ID == f"mcp__{SERVER_NAME}__plan_project"
        assert PLAN_PROJECT_TOOL_ID in PLANNER_TOOL_IDS
        assert len(PLANNER_TOOL_IDS) == 1

    def test_tool_ids_live_on_separate_servers(self) -> None:
        """The split is what restores the per-server OPT_IN gate. The
        two tool IDs must NOT be hosted on the same server name."""
        from pocketpaw_ee.agent.mcp_servers.planner import (
            PLAN_POCKET_TOOL_ID,
            PLAN_PROJECT_TOOL_ID,
            POCKET_PLANNER_SERVER_NAME,
            SERVER_NAME,
        )

        assert SERVER_NAME != POCKET_PLANNER_SERVER_NAME
        # Tool IDs follow ``mcp__<server>__<tool>``.
        assert f"__{SERVER_NAME}__" in PLAN_PROJECT_TOOL_ID
        assert f"__{POCKET_PLANNER_SERVER_NAME}__" in PLAN_POCKET_TOOL_ID


class TestTaskBreakdownParserDrift:
    """The task-breakdown step is JSON output. LLM drift produces
    patterns the original strict regex parser silently dropped — see
    PR #1223 R2 high-priority #2. The lenient parser must extract a
    balanced array from those shapes; ``_lenient_parse_taskspecs``
    wraps it for the pipeline.
    """

    def test_task_breakdown_parser_handles_drift(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.planner import _parse_lenient_json_list

        # Case 1: trailing prose after the array.
        case_trailing_prose = (
            '[{"key": "t1", "title": "Seed state.cards"}]\n\n'
            "-- and here's why I chose those tasks..."
        )
        data, err = _parse_lenient_json_list(case_trailing_prose)
        assert err is None, f"trailing-prose case errored: {err}"
        assert data == [{"key": "t1", "title": "Seed state.cards"}]

        # Case 2: ``#`` comments inside the JSON.
        case_comments = """\
[
  # the seed task — runs first
  {"key": "t1", "title": "Seed state.cards"},
  # the widget task
  {"key": "t2", "title": "Add kanban"}
]
"""
        data, err = _parse_lenient_json_list(case_comments)
        assert err is None, f"comments case errored: {err}"
        assert data == [
            {"key": "t1", "title": "Seed state.cards"},
            {"key": "t2", "title": "Add kanban"},
        ]

        # Case 3: trailing commas (JSON5-style).
        case_trailing_commas = """\
[
  {"key": "t1", "title": "Seed state.cards",},
  {"key": "t2", "title": "Add kanban",},
]
"""
        data, err = _parse_lenient_json_list(case_trailing_commas)
        assert err is None, f"trailing-comma case errored: {err}"
        assert data == [
            {"key": "t1", "title": "Seed state.cards"},
            {"key": "t2", "title": "Add kanban"},
        ]

    def test_multiple_fenced_blocks_picks_first_array(self) -> None:
        """Some models emit a fenced 'thinking' block, then the JSON in
        a separate fence. The lenient parser walks the text and grabs
        the first balanced ``[...]`` — which is the JSON, not the prose.
        """
        from pocketpaw_ee.agent.mcp_servers.planner import _parse_lenient_json_list

        raw = """\
Here is my thinking:

```
This pocket needs three tasks. The first seeds state...
```

And here is the JSON:

```json
[
  {"key": "t1", "title": "Seed state.cards"}
]
```
"""
        data, err = _parse_lenient_json_list(raw)
        assert err is None, f"multi-fence case errored: {err}"
        assert data == [{"key": "t1", "title": "Seed state.cards"}]

    def test_unparseable_output_surfaces_diagnostic(self) -> None:
        """When the model emits nothing parseable, the parser returns
        ``(None, err)`` with a short snippet so the captain can see
        what was attempted. The handler propagates this into
        ``warnings`` on the MCP response.
        """
        from pocketpaw_ee.agent.mcp_servers.planner import _parse_lenient_json_list

        raw = "Sorry, I cannot help with that today."
        data, err = _parse_lenient_json_list(raw)
        assert data is None
        assert err is not None
        assert "no balanced JSON array" in err

    def test_strings_with_brackets_do_not_break_extractor(self) -> None:
        """A ``[`` inside a string literal must not be counted as a
        bracket — the balanced-bracket walker respects double-quoted
        strings."""
        from pocketpaw_ee.agent.mcp_servers.planner import _parse_lenient_json_list

        raw = '[{"key": "t1", "title": "Render [Project] dashboard"}]'
        data, err = _parse_lenient_json_list(raw)
        assert err is None
        assert data == [{"key": "t1", "title": "Render [Project] dashboard"}]
