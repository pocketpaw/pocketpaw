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

from pathlib import Path

import pytest

AGENT_SERVICE = (
    Path(__file__).resolve().parent.parent.parent
    / "ee"
    / "cloud"
    / "chat"
    / "agent_service.py"
)


@pytest.fixture(scope="module")
def agent_service_source() -> str:
    return AGENT_SERVICE.read_text(encoding="utf-8")


def test_no_cloud_pocket_prompt_constants(agent_service_source: str) -> None:
    """No ``_CLOUD_POCKET_*`` prompt strings live in the cloud agent."""
    forbidden = (
        "_CLOUD_POCKET_INTERACTION_PROMPT",
        "_CLOUD_POCKET_CREATION_PROMPT",
        "_CLOUD_POCKET_INTERACTION_PROMPT_MCP",
        "_CLOUD_POCKET_CREATION_PROMPT_MCP",
        "_MCP_POCKET_BACKENDS",
    )
    for name in forbidden:
        assert (
            name not in agent_service_source
        ), f"{name!r} reintroduced in agent_service.py — pocket prompts live in ee.ripple"


def test_agent_service_imports_canonical_prompts(agent_service_source: str) -> None:
    """The cloud chat agent must source pocket prompts from ee.ripple."""
    assert "from ee.ripple import" in agent_service_source
    assert "get_pocket_prompts" in agent_service_source
    assert "POCKET_ID_TOKEN" in agent_service_source


def test_canonical_prompts_carry_required_features() -> None:
    """The interactive-by-default rule and list-before-create gate must
    appear in every pocket creation prompt — they are the user-visible
    behavior changes the prompts exist to enforce."""
    from ee.ripple import (
        POCKET_CREATION_PROMPT_CLI,
        POCKET_CREATION_PROMPT_MCP,
        POCKET_INTERACTION_PROMPT_CLI,
        POCKET_INTERACTION_PROMPT_MCP,
    )

    for prompt in (POCKET_CREATION_PROMPT_MCP, POCKET_CREATION_PROMPT_CLI):
        assert "<list-before-create>" in prompt
        assert "<interactive-by-default>" in prompt
        assert "<pocket-creation>" in prompt

    for prompt in (POCKET_INTERACTION_PROMPT_MCP, POCKET_INTERACTION_PROMPT_CLI):
        assert "<interactive-by-default>" in prompt
        assert "<pocket-workflow>" in prompt

    # Tool-surface separation: MCP variant points at the in-process tools,
    # CLI variant at the shell bridge. Crossover means a backend got the
    # wrong instructions.
    assert "list_pockets()" in POCKET_CREATION_PROMPT_MCP
    assert "cloud_create_pocket" not in POCKET_CREATION_PROMPT_MCP
    assert "cloud_list_pockets" in POCKET_CREATION_PROMPT_CLI
    assert "cloud_create_pocket" in POCKET_CREATION_PROMPT_CLI


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
