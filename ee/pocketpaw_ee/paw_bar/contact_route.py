# ee/pocketpaw_ee/paw_bar/contact_route.py — the v2 concierge's deterministic route to the team.
#
# A visitor who asks for a person must leave the turn with a way to reach the
# team whatever the model did: wrote no lead card, ran out of tokens thinking,
# or failed outright. ``is_contact_request`` is a small, conservative phrase
# check for a first-person request to reach a person ("can I talk to someone",
# "call me back", "get in touch"), not a classifier. It stays narrow on purpose:
# a missed request still has the bar's "Talk to a person" button, while a false
# hit puts a form under an ordinary answer. ``contact_reply`` is what the runner appends when
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

# Who a visitor may ask for, after "talk to" and friends. Bare "you" is not on
# the list: "how do I reach you from the station?" asks for directions.
_WHO = (
    r"(?:you\s+guys\b|"
    r"(?:a\s+|an\s+|the\s+|your\s+|some\s+|one\s+of\s+your\s+)?"
    r"(?:real\s+|live\s+|actual\s+)?"
    r"(?:person|people|human|humans|someone|somebody|team|manager|agent|"
    r"representative|rep|staff|owner|support|sales|employee|operator)\b)"
)
# A first-person request lead-in: the visitor asking for themselves.
_LEAD_IN = (
    r"\b(?:(?:can|could|may)\s+i|i\s+(?:want|need|would\s+like)\s+to|"
    r"i['’]?d\s+like\s+to|i\s+wanna|let\s+me|please|how\s+(?:do|can)\s+i)"
    r"\s+(?:please\s+|just\s+)?"
)
# Whole-message requests: "talk to a human", "human please", "Representative".
# Anchored, so "does it support a talk to a human handoff?" is not one.
_ALONE = (
    r"^\W*(?:please\s+)?(?:"
    rf"(?:talk|speak|chat)\s+(?:to|with)\s+{_WHO}"
    r"|(?:a\s+)?(?:human|person|agent|operator|representative|manager|"
    r"real\s+person|live\s+agent|call\s*-?\s*back)"
    r")(?:\s+please)?\W*$"
)
_CONTACT_RE = re.compile(
    "|".join(
        [
            rf"{_LEAD_IN}(?:(?:talk|speak|chat)\s+(?:to|with)|reach)\s+{_WHO}",
            _ALONE,
            r"\bcall\s+me\b",
            r"\bcontact\s+me\b",
            r"\bconnect\s+me\s+(?:to|with)\b",
            r"\bget\s+in\s+touch\b",
            r"\b(?:i\s+(?:need|want|would\s+like)|i['’]?d\s+like|"
            r"(?:can|could|may)\s+i\s+(?:get|have|request)|request)\s+a\s+call\s*-?\s*back\b",
            r"\bhow\s+(?:do|can|could|should)\s+i\s+contact\b",
            r"\b(?:person|human|someone|somebody|one)\s+i\s+(?:can|could)\s+"
            r"(?:talk|speak|chat)\s+(?:to|with)\b",
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
