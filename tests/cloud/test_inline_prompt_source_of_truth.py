"""Regression guard: the cloud chat-inline system prompt must live in one
place — `ee/ripple/_inline.py`. If a future refactor reintroduces a
`_RIPPLE_HINT` literal in `agent_service.py`, this test fires.

Also guards the in-chat Instinct approval loop (feat/invoke-tool-v1): the
pending-approvals guidance must teach the agent to bind the Approve / Reject
buttons to the backend Instinct decision routes as `api` actions (so the
HUMAN's click finalizes the decision), point to the Tray at /deep-work, and
NEVER fall back to an `emit`/`chat.send` round-trip for the decision."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pocketpaw_ee.cloud.chat.agent_service as agent_service

from pocketpaw.ripple import INLINE_RIPPLE_SYSTEM_PROMPT


def _fenced_json_blocks(text: str) -> list[dict]:
    """Parse every ```-fenced block in the prompt that is a single JSON object.

    Mirrors how the flow skeletons are validated elsewhere: pull the fenced
    examples back out of the prompt and assert on their parsed structure so a
    future edit that breaks the example JSON (or rewires the button) is caught.
    Non-JSON fences (and JSON fragments) are skipped.
    """
    blocks: list[dict] = []
    for body in re.findall(r"```(.*?)```", text, flags=re.DOTALL):
        candidate = body.strip()
        # Drop an optional language tag on the opening fence line.
        if candidate and not candidate.lstrip().startswith("{"):
            candidate = candidate.split("\n", 1)[-1].strip()
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            blocks.append(parsed)
    return blocks


def _find_button_by_label(blocks: list[dict], label: str) -> dict | None:
    """Depth-first search the parsed example blocks for a button node whose
    ``props.label`` matches (case-insensitive)."""

    def walk(node: object) -> dict | None:
        if isinstance(node, dict):
            if node.get("type") == "button":
                props = node.get("props") or {}
                if str(props.get("label", "")).strip().lower() == label.lower():
                    return node
            for value in node.values():
                hit = walk(value)
                if hit is not None:
                    return hit
        elif isinstance(node, list):
            for item in node:
                hit = walk(item)
                if hit is not None:
                    return hit
        return None

    for block in blocks:
        hit = walk(block)
        if hit is not None:
            return hit
    return None


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


# ---------------------------------------------------------------------------
# In-chat Instinct approval loop (feat/invoke-tool-v1)
# ---------------------------------------------------------------------------


def test_inline_prompt_teaches_instinct_approve_route():
    """The pending-approvals guidance must name the backend Instinct decision
    routes so the agent binds the Approve / Reject buttons to them."""
    p = INLINE_RIPPLE_SYSTEM_PROMPT
    assert "/api/v1/instinct/actions/{id}/approve" in p
    assert "/api/v1/instinct/actions/{id}/reject" in p


def test_inline_prompt_points_to_deep_work_tray():
    """The Tray route pointer must be present so the agent can offer an
    'Open Tray' affordance and reference the queue location in its text."""
    p = INLINE_RIPPLE_SYSTEM_PROMPT
    assert "/deep-work" in p


def test_inline_prompt_approve_button_skeleton_is_api_action():
    """The embedded Approve button example must round-trip to JSON with an
    ``api`` on_click POSTing to the approve route — so the HUMAN's click
    finalizes the decision, not an autonomous agent verb.

    Parses the fenced skeletons back out of the prompt (the same shape the
    flow-descriptor tests assert on) and inspects the Approve button node.
    """
    blocks = _fenced_json_blocks(INLINE_RIPPLE_SYSTEM_PROMPT)
    approve = _find_button_by_label(blocks, "Approve")
    assert approve is not None, "Approve button skeleton must be present and valid JSON"

    on_click = approve.get("on_click") or {}
    assert on_click.get("action") == "api", (
        "Approve must fire via the `api` verb (human click hits the backend "
        "approve route), not an emit/chat.send round-trip"
    )
    assert on_click.get("method") == "POST"
    assert on_click.get("url") == "/api/v1/instinct/actions/{id}/approve"

    # The success affordance is a toast, not a further chat round-trip.
    on_success = on_click.get("on_success") or []
    assert any(cont.get("action") == "toast" for cont in on_success), (
        "Approve should confirm with a toast on success"
    )


def test_inline_prompt_approve_button_is_not_chat_send_roundtrip():
    """Security/UX guard: the Approve button must NOT be wired to
    emit/chat.send. A chat round-trip does not finalize the decision (it just
    sends the agent a message); only the backend approve route does."""
    approve = _find_button_by_label(_fenced_json_blocks(INLINE_RIPPLE_SYSTEM_PROMPT), "Approve")
    assert approve is not None
    on_click = approve.get("on_click") or {}
    assert on_click.get("action") != "emit"
    assert on_click.get("target") != "chat.send"


def test_inline_prompt_does_not_introduce_self_approve_agent_verb():
    """The fix binds the rendered BUTTON to the approve route; it must NOT
    teach the agent an autonomous 'approve action' verb it can call on its
    own. Self-approval would defeat the human-in-the-loop Instinct gate."""
    p = INLINE_RIPPLE_SYSTEM_PROMPT.lower()
    # No agent-facing self-approve tool/verb guidance.
    assert "approve_action" not in p
    assert "approve_external_action" not in p
    # The guidance frames approval as a human click, explicitly not an agent
    # acting on the user's behalf.
    assert "the user's click is the approval" in p or "the human's click" in p
