# ee/pocketpaw_ee/integrations/providers/_email_list.py: what mailing-list providers share.
#
# The pieces every "add a visitor to the owner's list" provider (mailchimp, brevo)
# needs:
#   * ``SubscribeArgs``  the public action's args: a validated, lowercased email and
#                        optional one-line first / last names (control characters
#                        stripped, capped), and nothing else
#   * ``picked_list``    the site's chosen list / audience id from the binding
#                        settings; none (or a malformed one) is a ``setup_required``
#                        refusal, so the owner sees the site needs a list picked
#
# The providers keep their own write rules (never re-subscribe someone who opted
# out, fill empty name fields only); this module only shapes the input.

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pydantic import Field, field_validator

from pocketpaw_ee.integrations.providers.base import ActionArgs, ActionRefused
from pocketpaw_ee.integrations.providers.newsletter import _EMAIL_RE, MAX_EMAIL

MAX_NAME = 100


def _single_line(value: str) -> str:
    return "".join(c for c in value if ord(c) >= 0x20 and ord(c) != 0x7F)


class SubscribeArgs(ActionArgs):
    email: str = Field(max_length=MAX_EMAIL)
    first_name: str = Field(default="", max_length=MAX_NAME)
    last_name: str = Field(default="", max_length=MAX_NAME)

    @field_validator("email")
    @classmethod
    def _valid_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not _EMAIL_RE.match(value):
            raise ValueError("Enter a valid email address.")
        return value

    @field_validator("first_name", "last_name")
    @classmethod
    def _one_line(cls, value: str) -> str:
        return _single_line(value).strip()


def picked_list(settings: Mapping[str, Any], pattern: re.Pattern[str]) -> str:
    """The stored list id, or ``setup_required`` when none valid is picked."""
    value = str(settings.get("list_id") or "").strip()
    if not pattern.fullmatch(value):
        raise ActionRefused(
            "setup_required",
            "This signup form isn't set up yet.",
            setup_error="no_list_picked",
        )
    return value
