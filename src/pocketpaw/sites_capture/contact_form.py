# src/pocketpaw/sites_capture/contact_form.py — the ONE declaration of what a
# Paw Site contact form is: its fields, the names they POST under, the older
# names still arriving from the deployed fleet, and what makes a submission
# worth storing.
#
# Created 2026-08-13. WHY IT EXISTS: the form and the thing that reads the form
# were declared in two different packages and drifted. ``landing_assembler``
# generated ``name="name"``; ``_DEFAULT_EVENT_MAPPING`` read
# ``{{ payload.full_name }}``; nobody compared them, so every lead from the
# deterministic landing path stored an empty name and discarded the value the
# visitor actually typed. Both sides now derive from ``CONTACT_FIELDS`` here, so
# the drift is not a bug that can recur — it is a rename that touches one tuple.
#
# This lives in the OSS package (not ee/) because both consumers are on opposite
# sides of the open-core split: the assembler is EE, the capture service is EE,
# but ``sites_capture`` is where the shared, dependency-free capture primitives
# already live (``ingest.py``, ``models.py``) and where both may import from.
#
# THE RULE THAT SHAPES THE VALIDATION: a lost lead is the worst failure this
# subsystem has. That principle is already why the injection screen drops at HIGH
# rather than MEDIUM (see cloud/leads/service.py). So validation here is strict
# about SHAPE — sizes, and whether the business can actually reply — and refuses
# to be a spelling critic. A typo'd email with a good phone number is a customer;
# a submission with neither is not reachable and is not a lead.

from __future__ import annotations

import re
from dataclasses import dataclass

# The logical form type for a contact submission. It is the ``event_mapping``
# key, the ``form_type`` the generated site POSTs, and the default the
# native-form endpoint assumes. One constant so those three can never disagree.
CONTACT_FORM_TYPE = "lead"

# What the mapping ``creates`` — the lead's logical record type.
CONTACT_CREATES = "Lead"


@dataclass(frozen=True)
class ContactField:
    """One field of the canonical contact form.

    ``name`` is BOTH the input's POST name and the stored property key — keeping
    them identical is what removes the seam this module exists to close.

    ``aliases`` are other names the same value arrives under. Two sources need
    them: sites published BEFORE this module existed (their Workers are deployed
    and will keep POSTing the old names until someone republishes, which nobody
    will), and IMPORTED sites, whose forms were written by whoever built the
    original page and use whatever they felt like. Aliases are matched loosely —
    see ``_key``.
    """

    name: str
    label: str
    input_type: str = "text"
    required: bool = False
    max_length: int = 500
    aliases: tuple[str, ...] = ()
    # A field a business could reply through. At least one must arrive with a
    # usable value or the submission is unreachable — see ``validate``.
    reply_channel: bool = False
    placeholder: str = ""
    multiline: bool = False


CONTACT_FIELDS: tuple[ContactField, ...] = (
    ContactField(
        name="full_name",
        label="Your name",
        required=True,
        max_length=200,
        # ``name`` is FIRST because it is what the deployed fleet sends.
        aliases=("name", "yourname", "fullname", "firstname", "contactname"),
        placeholder="Jane Doe",
    ),
    ContactField(
        name="email",
        label="Email",
        input_type="email",
        required=True,
        # RFC 5321's maximum reverse-path length. Anything longer is not an
        # address someone typed.
        max_length=320,
        aliases=("emailaddress", "youremail", "mail", "contactemail", "workemail"),
        reply_channel=True,
        placeholder="you@email.com",
    ),
    ContactField(
        name="phone",
        label="Phone",
        input_type="tel",
        max_length=50,
        aliases=(
            "tel",
            "telephone",
            "phonenumber",
            "mobile",
            "cell",
            "yourphone",
            "contactnumber",
        ),
        reply_channel=True,
        placeholder="(555) 010-1234",
    ),
    ContactField(
        name="message",
        label="How can we help?",
        # The one genuinely free-text field, so the only one sized for prose. The
        # 8KB whole-payload cap in models.py still sits above this.
        max_length=5000,
        aliases=("msg", "comments", "comment", "enquiry", "inquiry", "details", "yourmessage"),
        placeholder="Tell us what you need...",
        multiline=True,
    ),
)

CONTACT_FIELD_NAMES: tuple[str, ...] = tuple(f.name for f in CONTACT_FIELDS)

# Convenience handles so callers reference a field by constant rather than by a
# string literal that a rename would leave behind.
FULL_NAME, EMAIL, PHONE, MESSAGE = CONTACT_FIELD_NAMES


def _key(raw: str) -> str:
    """Normalize a submitted field name for alias matching.

    Lowercase and strip everything that is not a letter or digit, so ``Your Name``,
    ``your-name``, ``your_name`` and ``yourName`` all collapse to one key. Imported
    forms use every one of those conventions, and matching them exactly would mean
    enumerating the cross product."""
    return re.sub(r"[^a-z0-9]", "", raw.lower())


def _alias_index() -> dict[str, str]:
    """``normalized submitted name`` → ``canonical field name``.

    Built once at import. A field's own name maps to itself, so the canonical
    spelling always resolves without being listed among its own aliases."""
    index: dict[str, str] = {}
    for spec in CONTACT_FIELDS:
        index[_key(spec.name)] = spec.name
        for alias in spec.aliases:
            index.setdefault(_key(alias), spec.name)
    return index


_ALIASES = _alias_index()


def default_event_mapping() -> dict[str, dict[str, object]]:
    """The seeded ``event_mapping`` for a freshly published site, derived from
    ``CONTACT_FIELDS`` rather than restated.

    Shaped as the plain dict the Site document stores (``SiteEventMapping``
    validates it on read), keyed by ``CONTACT_FORM_TYPE``."""
    return {
        CONTACT_FORM_TYPE: {
            "creates": CONTACT_CREATES,
            "fields": {f.name: f"{{{{ payload.{f.name} }}}}" for f in CONTACT_FIELDS},
        }
    }


def normalize(payload: dict[str, object]) -> dict[str, object]:
    """Resolve aliased field names to their canonical spelling.

    NON-DESTRUCTIVE: returns a copy carrying every original key plus the canonical
    ones. Nothing downstream loses a key it was relying on — the honeypot field and
    any extra input on an imported form still read exactly as submitted. (The
    mapping projection is what decides storage; this only makes sure the canonical
    lookup finds the value.)

    A canonical key that already arrived with a non-empty value WINS over an alias,
    so a form sending both ``full_name`` and ``name`` is not at the mercy of dict
    ordering."""
    out = dict(payload)
    for raw, value in payload.items():
        canonical = _ALIASES.get(_key(str(raw)))
        if canonical is None or canonical == raw:
            continue
        if str(out.get(canonical) or "").strip():
            continue  # the canonical spelling already carries a real value
        if str(value or "").strip():
            out[canonical] = value
    return out


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def looks_like_email(value: object) -> bool:
    """Structural check only — one ``@``, a dot in the domain, no whitespace.

    Deliberately not an RFC-complete validator and deliberately not a
    deliverability check. The question being asked is "could a human reply to
    this", not "is this address perfectly formed"; a stricter rule here rejects
    real customers, which is the expensive direction to be wrong in."""
    return bool(_EMAIL_RE.match(str(value or "").strip()))


def looks_like_phone(value: object) -> bool:
    """At least 7 digits after stripping formatting — the shortest real subscriber
    number. Extensions, spaces, dashes, parentheses and ``+`` all survive."""
    return len(re.sub(r"\D", "", str(value or ""))) >= 7


def recognized(payload: dict[str, object]) -> bool:
    """Did ANY canonical field resolve to a real value?

    False means the submission's field names are ones this schema has never seen —
    an imported form built by someone else, using names outside the alias table.
    That is a configuration gap on our side, not a bad submission, and the two are
    treated very differently in ``validate``."""
    return any(str(payload.get(spec.name) or "").strip() for spec in CONTACT_FIELDS)


def validate(payload: dict[str, object]) -> str | None:
    """Check a NORMALIZED contact payload. Returns ``None`` when it should be
    stored, or a short machine-readable reason when it should be dropped.

    Two rules, both about whether this is a lead at all:

    * ``too_long`` — a field exceeded its cap. A 5000-character name is not a
      name, and the per-field caps are what make the 8KB payload cap meaningful
      (without them one field could carry the whole budget).
    * ``no_reply_channel`` — the form was understood and carries no usable email
      and no usable phone. The business cannot answer it, so it is not a lead.

    Everything else passes. Two deliberate leniencies:

    * A malformed email with a good phone is KEPT — that is a reachable customer
      with a typo, and rejecting them is the expensive direction to be wrong in.
    * A payload where NOTHING resolved is KEPT. It is tempting to treat "no
      recognized fields" as the strongest possible ``no_reply_channel``, but the
      cause is almost always an imported form whose field names are not in the
      alias table. Dropping it makes a misconfigured capture look like an absence
      of visitors — the owner sees silence and concludes nobody is filling the
      form. Letting it through stores an empty lead and rings the bell, which is
      an ugly but HONEST signal that submissions are arriving and something needs
      fixing. Silence is the worse failure."""
    for spec in CONTACT_FIELDS:
        if len(str(payload.get(spec.name) or "")) > spec.max_length:
            return "too_long"

    if not recognized(payload):
        return None

    reachable = looks_like_email(payload.get(EMAIL)) or looks_like_phone(payload.get(PHONE))
    if not reachable:
        return "no_reply_channel"
    return None


__all__ = [
    "CONTACT_CREATES",
    "CONTACT_FIELDS",
    "CONTACT_FIELD_NAMES",
    "CONTACT_FORM_TYPE",
    "ContactField",
    "EMAIL",
    "FULL_NAME",
    "MESSAGE",
    "PHONE",
    "default_event_mapping",
    "looks_like_email",
    "looks_like_phone",
    "normalize",
    "recognized",
    "validate",
]
