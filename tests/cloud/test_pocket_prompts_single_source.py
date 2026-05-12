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
    Path(__file__).resolve().parent.parent.parent / "ee" / "cloud" / "chat" / "agent_service.py"
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
    """The cloud chat agent must source pocket prompts from ee.ripple."""
    assert "from ee.ripple import" in agent_service_source
    assert "get_pocket_prompts" in agent_service_source
    assert "POCKET_ID_TOKEN" in agent_service_source


def test_canonical_prompts_carry_required_features() -> None:
    """The creation prompts now delegate to the specialist (STEP 0 block);
    the heavy workflow lives on POCKET_SPECIALIST_PROMPT. The interaction
    prompts still carry the interactive-by-default rule and the
    pocket-workflow block."""
    from ee.ripple import (
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
    from ee.ripple import (
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
    from ee.ripple import POCKET_ID_TOKEN, POCKET_INTERACTION_PROMPT_MCP

    assert POCKET_ID_TOKEN == "__POCKET_ID__"
    assert POCKET_ID_TOKEN in POCKET_INTERACTION_PROMPT_MCP
    swapped = POCKET_INTERACTION_PROMPT_MCP.replace(POCKET_ID_TOKEN, "abc123")
    assert "abc123" in swapped
    assert POCKET_ID_TOKEN not in swapped


class TestSpecialistDelegationBlock:
    """The new STEP 0 delegation block must replace the legacy STEP 1..N
    inline-creation block in BOTH MCP and CLI prompt variants."""

    def test_mcp_prompt_has_delegation_block(self):
        from ee.ripple._pockets import POCKET_CREATION_PROMPT_MCP

        assert "pocket_specialist__create" in POCKET_CREATION_PROMPT_MCP
        # The MCP variant uses the "TWO-PHASE DELEGATION" framing
        # (think first, hand off). CLI keeps the older STEP-0 marker.
        assert "TWO-PHASE DELEGATION" in POCKET_CREATION_PROMPT_MCP

    def test_cli_prompt_has_delegation_block(self):
        from ee.ripple._pockets import POCKET_CREATION_PROMPT_CLI

        assert "cloud_pocket_specialist_create" in POCKET_CREATION_PROMPT_CLI
        assert "DELEGATE TO SPECIALIST" in POCKET_CREATION_PROMPT_CLI

    def test_legacy_inline_steps_removed_mcp(self):
        from ee.ripple._pockets import POCKET_CREATION_PROMPT_MCP

        # Calling agent must NEVER call create_pocket / update_pocket directly.
        assert "mcp__pocketpaw_pocket__create_pocket" not in POCKET_CREATION_PROMPT_MCP
        assert "mcp__pocketpaw_pocket__update_pocket" not in POCKET_CREATION_PROMPT_MCP

    def test_legacy_inline_steps_removed_cli(self):
        from ee.ripple._pockets import POCKET_CREATION_PROMPT_CLI

        assert "cloud_create_pocket" not in POCKET_CREATION_PROMPT_CLI
        assert "cloud_update_pocket" not in POCKET_CREATION_PROMPT_CLI
