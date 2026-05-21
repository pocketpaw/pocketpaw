"""Regression: pocket prompts only live in ``ee/ripple/_pockets.py``.

Two duplicates used to live inside ``ee/cloud/chat/agent_service.py``
(``_CLOUD_POCKET_*`` strings + ``_MCP_POCKET_BACKENDS`` frozenset). They
drifted from the canonical source whenever someone tweaked one and
forgot the other, and silently dropped the interactive-pocket guidance
along the way.

These checks fail loudly if either copy creeps back in or the cloud
chat agent stops sourcing prompts from ``ee.ripple``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

AGENT_SERVICE = (
    Path(__file__).resolve().parent.parent.parent
    / "ee"
    / "pocketpaw_ee"
    / "cloud"
    / "chat"
    / "agent_service.py"
)


@pytest.fixture(scope="module")
def agent_service_source() -> str:
    return AGENT_SERVICE.read_text(encoding="utf-8")


def test_no_cloud_pocket_prompt_constants(agent_service_source: str) -> None:
    """No pocket-prompt constants are *defined* in the cloud agent.
    Imports from ``ee.ripple._pockets`` are fine — what we're guarding
    against is duplication that drifts from the canonical source."""
    forbidden = (
        "_CLOUD_POCKET_INTERACTION_PROMPT",
        "_CLOUD_POCKET_CREATION_PROMPT",
        "_CLOUD_POCKET_INTERACTION_PROMPT_MCP",
        "_CLOUD_POCKET_CREATION_PROMPT_MCP",
        "_MCP_POCKET_BACKENDS",
    )
    for name in forbidden:
        # Match `<name> =` at line start (assignment), not `import <name>`
        # or `if x in <name>` which are legitimate consumers.
        defn_pattern = rf"^{re.escape(name)}\s*=(?!=)"
        assert not re.search(defn_pattern, agent_service_source, re.MULTILINE), (
            f"{name!r} defined in agent_service.py — pocket prompts live in ee.ripple"
        )


def test_agent_service_imports_canonical_prompts(agent_service_source: str) -> None:
    """The cloud chat agent must source pocket prompts from pocketpaw.ripple."""
    assert "from pocketpaw.ripple import" in agent_service_source
    assert "get_pocket_prompts" in agent_service_source
    assert "POCKET_ID_TOKEN" in agent_service_source


def test_canonical_prompts_carry_required_features() -> None:
    """The creation prompts now delegate to the specialist (STEP 0 block);
    the heavy workflow lives on POCKET_SPECIALIST_PROMPT. The interaction
    prompts still carry the interactive-by-default rule and the
    pocket-workflow block."""
    from pocketpaw.ripple import (
        POCKET_CREATION_PROMPT_CLI,
        POCKET_CREATION_PROMPT_MCP,
        POCKET_EDIT_SPECIALIST_PROMPT_CLI,
        POCKET_EDIT_SPECIALIST_PROMPT_MCP,
        POCKET_INTERACTION_PROMPT_CLI,
        POCKET_INTERACTION_PROMPT_MCP,
        POCKET_SPECIALIST_PROMPT,
    )

    # Calling-agent creation prompts: scope/canvas + delegate-to-specialist
    # framing. The two variants diverged: MCP got rewritten to a richer
    # "TWO-PHASE DELEGATION" two-step plan; CLI kept the simple STEP 0 marker.
    for prompt in (POCKET_CREATION_PROMPT_MCP, POCKET_CREATION_PROMPT_CLI):
        assert "<pocket-creation>" in prompt
    assert "TWO-PHASE DELEGATION" in POCKET_CREATION_PROMPT_MCP
    assert "DELEGATE TO SPECIALIST" in POCKET_CREATION_PROMPT_CLI

    # Calling-agent interaction prompts: slim — scope + delegation block.
    # The heavy <interactive-by-default> / <pocket-workflow> content moved
    # to the edit specialist's prompt.
    for prompt in (POCKET_INTERACTION_PROMPT_MCP, POCKET_INTERACTION_PROMPT_CLI):
        assert "<pocket-interaction>" in prompt
        assert "<current-pocket>" in prompt

    # Edit specialist carries the heavy edit-time guidance.
    for prompt in (POCKET_EDIT_SPECIALIST_PROMPT_MCP, POCKET_EDIT_SPECIALIST_PROMPT_CLI):
        assert "<interactive-by-default>" in prompt
        assert "<pocket-workflow>" in prompt

    # Creation specialist carries the heavy create-time lift. The
    # specialist runtime only attaches ``persist_pocket`` (validation is
    # inline; list_pockets is handled by the parent agent before
    # delegation), so that's the only tool the prompt names.
    assert "<interactive-by-default>" in POCKET_SPECIALIST_PROMPT
    assert "<specialist-workflow>" in POCKET_SPECIALIST_PROMPT
    assert "persist_pocket" in POCKET_SPECIALIST_PROMPT

    # Tool-surface separation in the calling-agent delegation blocks: MCP
    # variant invokes the MCP specialist tool, CLI variant the shell command.
    assert "pocket_specialist__create" in POCKET_CREATION_PROMPT_MCP
    assert "cloud_pocket_specialist_create" in POCKET_CREATION_PROMPT_CLI
    # And neither calling-agent prompt teaches direct create_pocket calls.
    assert "cloud_create_pocket" not in POCKET_CREATION_PROMPT_MCP
    assert "cloud_create_pocket" not in POCKET_CREATION_PROMPT_CLI


def test_get_pocket_prompts_selects_by_backend() -> None:
    """``claude_agent_sdk`` gets the MCP variant; everything else gets CLI."""
    from pocketpaw.ripple import (
        POCKET_CREATION_PROMPT_CLI,
        POCKET_CREATION_PROMPT_MCP,
        POCKET_INTERACTION_PROMPT_CLI,
        POCKET_INTERACTION_PROMPT_MCP,
        get_pocket_prompts,
    )

    mcp_create, mcp_interact = get_pocket_prompts(backend_name="claude_agent_sdk")
    assert mcp_create is POCKET_CREATION_PROMPT_MCP
    assert mcp_interact is POCKET_INTERACTION_PROMPT_MCP

    for backend in ("codex_cli", "opencode", "gemini_cli", None, "unknown"):
        cli_create, cli_interact = get_pocket_prompts(backend_name=backend)
        assert cli_create is POCKET_CREATION_PROMPT_CLI
        assert cli_interact is POCKET_INTERACTION_PROMPT_CLI


def test_pocket_id_token_substitution() -> None:
    """The interaction prompt has a literal ``__POCKET_ID__`` token the
    caller substitutes via ``str.replace``. A naive ``str.format`` would
    crash on the unescaped braces inside RIPPLE_DESIGN_RULES."""
    from pocketpaw.ripple import POCKET_ID_TOKEN, POCKET_INTERACTION_PROMPT_MCP

    assert POCKET_ID_TOKEN == "__POCKET_ID__"
    assert POCKET_ID_TOKEN in POCKET_INTERACTION_PROMPT_MCP
    swapped = POCKET_INTERACTION_PROMPT_MCP.replace(POCKET_ID_TOKEN, "abc123")
    assert "abc123" in swapped
    assert POCKET_ID_TOKEN not in swapped


class TestSpecialistDelegationBlock:
    """The new STEP 0 delegation block must replace the legacy STEP 1..N
    inline-creation block in BOTH MCP and CLI prompt variants."""

    def test_mcp_prompt_has_delegation_block(self):
        from pocketpaw.ripple._pockets import POCKET_CREATION_PROMPT_MCP

        assert "pocket_specialist__create" in POCKET_CREATION_PROMPT_MCP
        # The MCP variant uses the "TWO-PHASE DELEGATION" framing
        # (think first, hand off). CLI keeps the older STEP-0 marker.
        assert "TWO-PHASE DELEGATION" in POCKET_CREATION_PROMPT_MCP

    def test_cli_prompt_has_delegation_block(self):
        from pocketpaw.ripple._pockets import POCKET_CREATION_PROMPT_CLI

        assert "cloud_pocket_specialist_create" in POCKET_CREATION_PROMPT_CLI
        assert "DELEGATE TO SPECIALIST" in POCKET_CREATION_PROMPT_CLI

    def test_legacy_inline_steps_removed_mcp(self):
        from pocketpaw.ripple._pockets import POCKET_CREATION_PROMPT_MCP

        # Calling agent must NEVER call create_pocket / update_pocket directly.
        assert "mcp__pocketpaw_pocket__create_pocket" not in POCKET_CREATION_PROMPT_MCP
        assert "mcp__pocketpaw_pocket__update_pocket" not in POCKET_CREATION_PROMPT_MCP

    def test_legacy_inline_steps_removed_cli(self):
        from pocketpaw.ripple._pockets import POCKET_CREATION_PROMPT_CLI

        assert "cloud_create_pocket" not in POCKET_CREATION_PROMPT_CLI
        assert "cloud_update_pocket" not in POCKET_CREATION_PROMPT_CLI


class TestAntiDashboardRebalance:
    """The specialist prompt now leads with a pattern-first step that
    diversifies output away from the default dashboard shape, while
    still treating `dashboard` as a valid pattern when the user asked
    for one. These assertions pin that contract — a regression that
    drops a pattern, drops the dashboard caveat, or reverts hero+grid
    to first-mentioned in the layout menu fails here.
    """

    @pytest.fixture
    def specialist_prompt(self) -> str:
        from pocketpaw.ripple import POCKET_SPECIALIST_PROMPT

        return POCKET_SPECIALIST_PROMPT

    def test_pattern_first_step_is_present(self, specialist_prompt: str) -> None:
        """The pattern-first forced step must appear before the layout
        menu so the LLM names the pattern before reaching for shapes."""
        assert "PICK THE PATTERN" in specialist_prompt
        # All 7 patterns must be named in the menu.
        for pattern in (
            "dashboard",
            "app",
            "viewer",
            "composer",
            "browser",
            "wizard",
            "feed",
        ):
            assert pattern in specialist_prompt, (
                f"pattern '{pattern}' missing from VISUAL_VARIATION_RULE"
            )

    def test_dashboard_default_rule_present(self, specialist_prompt: str) -> None:
        """``dashboard`` remains a valid pattern but cannot be the
        default — the prompt must carry the "only when the user
        explicitly asked" caveat."""
        # Either the exact phrasing or close variants — search for the
        # constraint shape, not the literal wording.
        text = specialist_prompt.lower()
        assert "explicitly asked" in text or "explicitly ask" in text
        assert "do not default to" in text or "not automatically a dashboard" in text

    def test_external_design_grounding_present(self, specialist_prompt: str) -> None:
        """The EXTERNAL DESIGN GROUNDING block tells the model that the
        pattern names map to canonical Material 3 / Apple HIG layouts,
        broadening the mental model beyond dashboards."""
        assert "EXTERNAL DESIGN GROUNDING" in specialist_prompt
        # At least one external system named so the LLM can anchor to
        # its training data.
        assert "Material 3" in specialist_prompt
        assert "list-detail" in specialist_prompt

    def test_layout_menu_does_not_lead_with_hero_grid(self, specialist_prompt: str) -> None:
        """``hero+grid`` (the canonical dashboard layout) used to be
        listed first; first-mentioned options bias the LLM's choice.
        After the rebalance it must appear AFTER another option."""
        # Find where the design.py layout menu starts.
        idx_pattern_step = specialist_prompt.find("PICK THE PATTERN")
        assert idx_pattern_step != -1
        idx_first_pane = specialist_prompt.find("Single full-pane")
        idx_hero_grid = specialist_prompt.find("Hero + grid")
        # Both must be present after the pattern-first step.
        assert idx_first_pane > idx_pattern_step
        assert idx_hero_grid > idx_pattern_step
        # And hero+grid must come AFTER single-pane in the menu order.
        assert idx_first_pane < idx_hero_grid, (
            "hero+grid is leading the layout menu again — the rebalance "
            "ordered it last on purpose; revert the reorder if you "
            "really want to lead with it."
        )

    def test_canonical_examples_include_non_dashboard_viewer(
        self,
    ) -> None:
        """The second creation example used to be a Q4 Revenue dashboard
        (page-header + 3 stats + area chart). It was replaced with a
        viewer pattern (text + kv-table) so the LLM sees a non-KPI
        shape as a first-class example."""
        from pocketpaw.ripple._pockets import _CREATION_EXAMPLES_CLI, _CREATION_EXAMPLES_MCP

        for examples in (_CREATION_EXAMPLES_MCP, _CREATION_EXAMPLES_CLI):
            # Old dashboard example is gone.
            assert "Q4 Revenue Report" not in examples
            # New viewer example is in.
            assert "Espresso 101" in examples
            # And it explicitly demonstrates kv-table — the canonical
            # viewer widget the old example never used.
            assert "kv-table" in examples
