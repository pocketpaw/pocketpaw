# ee/pocketpaw_ee/sites/site_data/pii.py — which site columns hold personal data, by
# name, so Sites AI dashboards never send them (or values derived from them) to a model.
#
# ``is_pii(table, column)`` reads the column name the way ``project_data`` reads
# credentials: camelCase becomes snake_case, then it is matched against the personal
# data names (email, phone, address parts, birth dates, government ids, card and bank
# numbers, IP addresses, precise locations, given / family / full names). A bare
# ``name`` / ``display_name`` / ``username`` counts only on a table that holds people
# (users, customers, contacts, leads, members, guests, signups …), so ``products.name``
# stays usable while ``customers.name`` is held back.
#
# ``sensitive(table, column)`` is the one question the dashboard generator asks: PII
# here, or a column the Data page already masks (``d1.Table.masked``: credentials,
# login-table secrets). A sensitive column is left out of the schema summary and out of
# the optional aggregate stats.
#
# Invariant: this only ever widens what is held back. It never unmasks anything, and a
# false positive costs a widget idea, never data.
from __future__ import annotations

import re

from pocketpaw_ee.sites.project_data import auth_table

_CAMEL = re.compile(r"(?<=[a-z0-9])([A-Z])")

# Matched against the snake_case name.
_PII = re.compile(
    r"e_?mail|phone|mobile|cell_?(?:phone|number)?$|fax|whats_?app|telegram"
    r"|address|street|addr(?:ess)?_?line|postal|post_?code|zip(?:_?code)?$|^zip"
    r"|birth|^dob$|_dob$|date_of_birth|^age$"
    r"|ssn|social_?security|national_?id|passport|driver_?licen[cs]e|tax_?(?:id|number)"
    r"|^nin$|aadhaa?r|^pan$|_pan$"
    r"|iban|card_?(?:number|no)|^pan_|credit_?card|account_?(?:number|no)|routing_?number"
    r"|sort_?code|^cvv|^cvc"
    r"|ip_?addr|^ip$|_ip$|user_?agent|device_?id"
    r"|latitude|longitude|^lat$|^lng$|^lon$|geo_?loc|coordinates"
    r"|first_?name|last_?name|full_?name|given_?name|family_?name|sur_?name|middle_?name"
    r"|maiden_?name|legal_?name|contact_?name|customer_?name|billing_?name|shipping_?name"
    r"|gender|ethnicity|religion|health|diagnos|medical|salary"
)
_PERSON_NAMES = frozenset({"name", "display_name", "username", "user_name", "nickname", "handle"})
# Tables whose rows are people (singular or plural, any casing, ``_`` ignored).
_PEOPLE = re.compile(
    r"^(?:user|customer|client|contact|lead|member|subscriber|guest|visitor|person|people"
    r"|patient|employee|staff|student|attendee|signup|registrant|applicant|candidate"
    r"|profile|account|author|owner|buyer|seller|participant|volunteer|donor|tenant"
    r"|booking|reservation|appointment|rsvp|enquir|inquir|submission|waitlist)"
)


def snake(column: str) -> str:
    """``column`` as lower snake_case (``emailAddress`` → ``email_address``)."""
    return _CAMEL.sub(r"_\1", column).lower()


def people_table(table: str) -> bool:
    """Whether ``table``'s rows are people (or records a person filled in)."""
    low = table.lower().replace("_", "").replace("-", "")
    return bool(auth_table(table)) or bool(_PEOPLE.match(low))


def is_pii(table: str, column: str) -> bool:
    """Whether ``column`` of ``table`` is named like personal data."""
    name = snake(column)
    if _PII.search(name):
        return True
    return name in _PERSON_NAMES and people_table(table)


def sensitive(table: str, column: str, masked: frozenset[str] | set[str] = frozenset()) -> bool:
    """PII, or a column the Data page masks: never shown to a model."""
    return column in masked or is_pii(table, column)
