# ee/pocketpaw_ee/paw_bar/contact_route.py — the v2 concierge's deterministic route to the team.
#
# A visitor who asks for a person must leave the turn with a way to reach the
# team whatever the model did: wrote no lead card, ran out of tokens thinking,
# or failed outright. ``is_contact_request`` is a small, conservative phrase
# check (a person / human / the team / a callback / contact me), not a
# classifier: a miss just leaves the turn to the model, a false hit only adds a
# form the visitor can ignore. ``contact_reply`` is what the runner appends when
# such a turn has no valid lead card:
#
#   * lead capture on: a server-built send_to_team form card, prefilled with
#     nothing, run through ``card_spec.render_card`` exactly as a model card is;
#   * lead capture off: one line pointing to the bar's own "Talk to a person"
#     (always offered while the bar is up), so no new client contract.
#
# ``ROUTE_LINE`` leads the reply only when nothing else was said.

from __future__ import annotations

import json
import re

ROUTE_LINE = "I can pass this to the team."
TALK_TO_A_PERSON_LINE = (
    'Tap "Talk to a person" and leave your email, and someone will get back to you.'
)

# Who a visitor may ask for, after "talk to" and friends.
_WHO = (
    r"(?:a\s+|an\s+|the\s+|your\s+|some\s+|one\s+of\s+your\s+)?"
    r"(?:real\s+|live\s+|actual\s+)?"
    r"(?:person|people|human|humans|someone|somebody|team|manager|agent|"
    r"representative|rep|staff|owner|support|sales|employee|operator)\b"
)
_CONTACT_RE = re.compile(
    "|".join(
        [
            rf"\b(?:talk|speak|chat)\s+(?:to|with)\s+{_WHO}",
            rf"\breach\s+(?:{_WHO}|you\b)",
            r"\bconnect\s+me\s+(?:to|with)\b",
            r"\b(?:call|ring|phone|text|email|e-mail|contact)\s+me\b",
            r"\bcall\s*-?\s*back\b",
            r"\b(?:get|be|keep)\s+in\s+(?:touch|contact)\b",
            r"\b(?:real|live|actual)\s+(?:person|human|agent|people)\b",
            r"\b(?:someone|somebody)\s+(?:from|on|at)\s+(?:your|the)\b",
            r"\b(?:how|who)\s+(?:do|can|should|could)\s+i\s+(?:contact|reach|call|email)\b",
            r"\bcontact\s+(?:you|your\s+team|the\s+team|support|someone|somebody)\b",
            r"\b(?:get|want|need)\s+(?:a|the|your)\s+(?:manager|human|person|representative)\b",
            r"^\W*(?:a\s+)?(?:human|person|agent|operator|representative|manager|"
            r"real\s+person|live\s+agent)\W*$",
        ]
    ),
    re.IGNORECASE,
)


def is_contact_request(message: str) -> bool:
    """Whether the visitor is asking to reach a person at the business."""
    return bool(_CONTACT_RE.search(message or ""))


def _lead_card_body() -> str:
    return json.dumps(
        {
            "ui": {
                "type": "form",
                "props": {
                    "verb": "send_to_team",
                    "title": "Send this to the team",
                    "submit_label": "Send",
                    "fields": [
                        {"name": "name", "label": "Name", "type": "text"},
                        {"name": "email", "label": "Email", "type": "email"},
                        {"name": "phone", "label": "Phone", "type": "tel"},
                        {"name": "message", "label": "Message", "type": "textarea"},
                    ],
                },
            }
        }
    )


def lead_card_fence() -> str | None:
    """The send_to_team form fence, validated as a model's card would be (lead
    capture on); None only if card_spec ever stops accepting it."""
    from pocketpaw_ee.paw_bar.card_spec import render_card

    return render_card(_lead_card_body(), [], lead_capture=True)


def contact_reply(*, lead_capture: bool, said_something: bool) -> list[str]:
    """The text pieces that give the visitor a route to the team.

    With lead capture on: the form card, after ``ROUTE_LINE`` when the reply
    was empty. With it off: nothing after a reply that said something (the
    model's own words, usually the business's contact details, stand), else
    ``ROUTE_LINE`` and the "Talk to a person" pointer."""
    card = lead_card_fence() if lead_capture else None
    if card is not None:
        return [f"\n\n{card}"] if said_something else [f"{ROUTE_LINE}\n\n{card}"]
    if said_something:
        return []
    return [f"{ROUTE_LINE} {TALK_TO_A_PERSON_LINE}"]


__all__ = [
    "ROUTE_LINE",
    "TALK_TO_A_PERSON_LINE",
    "contact_reply",
    "is_contact_request",
    "lead_card_fence",
]
