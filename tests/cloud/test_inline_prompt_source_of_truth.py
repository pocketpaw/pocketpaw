"""Regression guard: the cloud chat-inline system prompt must live in one
place — `ee/ripple/_inline.py`. If a future refactor reintroduces a
`_RIPPLE_HINT` literal in `agent_service.py`, this test fires."""

from __future__ import annotations

from pathlib import Path

import pocketpaw_ee.cloud.chat.agent_service as agent_service

from pocketpaw.ripple import INLINE_RIPPLE_SYSTEM_PROMPT


def test_agent_service_does_not_define_ripple_hint_literal():
    """The chat-inline prompt is defined in ee.ripple._inline only."""
    source_path = Path(agent_service.__file__)
    text = source_path.read_text(encoding="utf-8")
    assert "_RIPPLE_HINT = " not in text, (
        "agent_service.py should not redefine _RIPPLE_HINT. The chat-inline "
        "system prompt lives in ee/ripple/_inline.py — import "
        "INLINE_RIPPLE_SYSTEM_PROMPT instead."
    )


def test_inline_prompt_documents_chat_send_loop():
    """Driven-UI loop guidance must be present so agents emit interactive
    specs that round-trip clicks as user messages."""
    p = INLINE_RIPPLE_SYSTEM_PROMPT
    assert "chat.send" in p
    assert "on_click" in p
    assert "emit" in p


def test_inline_prompt_does_not_forbid_buttons():
    """The chat-inline surface now supports interactive buttons via
    chat.send round-trip — the historical 'no buttons' rule is lifted."""
    p = INLINE_RIPPLE_SYSTEM_PROMPT.lower()
    assert "do not include `button`" not in p
    assert "do not include button" not in p


def test_inline_prompt_composes_shared_design_language():
    """The inline prompt splices in the shared widget catalog and
    use-the-widget rule from ``pocketpaw.ripple._design`` rather than a
    hand-maintained subset. A regression here (e.g. a broken ``_design``
    import) would otherwise only surface as a runtime ImportError."""
    from pocketpaw.ripple._design import USE_THE_WIDGET_RULE, WIDGET_CATALOG

    p = INLINE_RIPPLE_SYSTEM_PROMPT
    assert "# WIDGET CATALOG" in p
    assert "# USE-THE-WIDGET RULE" in p
    assert WIDGET_CATALOG in p
    assert USE_THE_WIDGET_RULE in p


def test_inline_prompt_requires_widget_help_before_emit():
    """Non-core widgets must be looked up via get_inline_widget_help
    before they land in a spec — guessed prop names ship empty rows."""
    p = INLINE_RIPPLE_SYSTEM_PROMPT
    assert "get_inline_widget_help" in p
    assert "MUST CALL BEFORE EMIT" in p
