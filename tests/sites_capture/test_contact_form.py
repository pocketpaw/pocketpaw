# tests/sites_capture/test_contact_form.py — the canonical contact-form schema:
# alias resolution for the field names that actually arrive, and a validation
# rule tuned so it can never be the reason a real customer is lost.
#
# Created 2026-08-13 alongside src/pocketpaw/sites_capture/contact_form.py.
#
# The schema exists because the form and the mapping that reads it were declared
# in two packages and drifted (see tests/ee/sites/test_contact_form_contract.py
# for that reproduction). These tests cover the module's own two jobs:
#
#   * NORMALIZE — the deployed fleet POSTs ``name=``, imported forms POST
#     whatever their original author typed, and both have to reach the canonical
#     key. Non-destructive, and a real canonical value always beats an alias.
#   * VALIDATE — strict about SHAPE (per-field caps, is there any way to reply)
#     and deliberately lenient about everything else. The asymmetry is the point:
#     a dropped lead is the worst outcome this subsystem has, so every judgment
#     call here errs toward keeping the submission.
#
# The leniencies are pinned as hard as the rejections, because they are the part
# a later "let's tighten validation" pass would quietly undo.
from __future__ import annotations

import pytest

from pocketpaw.sites_capture import contact_form as cf

# --------------------------------------------------------------------------- #
# Normalization — the names that actually arrive
# --------------------------------------------------------------------------- #


def test_the_deployed_fleets_field_name_resolves_to_the_canonical_one():
    """``name`` is what every site published before this module POSTs, and those
    Workers keep POSTing it until someone republishes them — which nobody does,
    because the site looks fine. If this alias breaks, the fleet goes dark."""
    out = cf.normalize({"name": "Sam Rivera"})
    assert out[cf.FULL_NAME] == "Sam Rivera"


@pytest.mark.parametrize(
    ("submitted", "expected"),
    [
        ("Your Name", cf.FULL_NAME),
        ("your-name", cf.FULL_NAME),
        ("your_name", cf.FULL_NAME),
        ("FULLNAME", cf.FULL_NAME),
        ("e-mail", cf.EMAIL),
        ("Email Address", cf.EMAIL),
        ("work_email", cf.EMAIL),
        ("Phone Number", cf.PHONE),
        ("tel", cf.PHONE),
        ("mobile", cf.PHONE),
        ("Comments", cf.MESSAGE),
        ("your-message", cf.MESSAGE),
    ],
)
def test_imported_forms_spelling_conventions_all_collapse_to_one_key(submitted, expected):
    """Imported pages were built by whoever built them. Matching these exactly
    would mean enumerating the cross product of casing, spaces, hyphens and
    underscores, so the matcher strips to letters and digits instead."""
    assert cf.normalize({submitted: "value"})[expected] == "value"


def test_normalization_keeps_every_original_key():
    """NON-DESTRUCTIVE by contract. The honeypot check reads a field that is not in
    this schema at all, and it runs on the same dict — so dropping unknown keys
    here would disable spam protection from a long way away."""
    out = cf.normalize({"name": "Sam", "company_website": "spam", "budget": "10k"})
    assert out["company_website"] == "spam"
    assert out["budget"] == "10k"


def test_a_real_canonical_value_beats_an_alias():
    """A form can send both. Which one wins must not depend on dict ordering."""
    out = cf.normalize({"full_name": "Canonical", "name": "Alias"})
    assert out[cf.FULL_NAME] == "Canonical"


def test_an_empty_canonical_value_yields_to_a_filled_alias():
    """The generated form renders every field, so unfilled ones POST as empty
    strings. An empty canonical key must not shadow an alias that has the value —
    that would reintroduce the original bug for any form sending both."""
    out = cf.normalize({"full_name": "", "name": "Sam"})
    assert out[cf.FULL_NAME] == "Sam"


# --------------------------------------------------------------------------- #
# Validation — rejections
# --------------------------------------------------------------------------- #


def test_a_contactable_submission_passes():
    assert cf.validate({cf.FULL_NAME: "Sam", cf.EMAIL: "sam@example.com"}) is None


def test_a_submission_nobody_can_reply_to_is_rejected():
    """No email, no phone. The business cannot answer it, so it is not a lead —
    and letting it ring the bell trains the owner to ignore the bell."""
    assert cf.validate({cf.FULL_NAME: "Sam", cf.MESSAGE: "call me"}) == "no_reply_channel"


def test_an_oversized_field_is_rejected():
    """Per-field caps are what make the 8KB payload cap mean anything — without
    them a single field can carry the entire budget."""
    assert cf.validate({cf.EMAIL: "a@b.co", cf.MESSAGE: "x" * 5001}) == "too_long"


def test_the_cap_is_per_field_not_shared():
    """A long message must not make a normal name look oversized."""
    assert cf.validate({cf.EMAIL: "a@b.co", cf.FULL_NAME: "Sam", cf.MESSAGE: "x" * 4999}) is None


# --------------------------------------------------------------------------- #
# Validation — the leniencies, pinned so a later tightening pass has to argue
# --------------------------------------------------------------------------- #


def test_a_typod_email_with_a_good_phone_is_kept():
    """THE ASYMMETRY. This person is reachable and wants to be contacted; the only
    thing wrong is a typo. Rejecting them costs a customer to enforce tidiness."""
    assert cf.validate({cf.FULL_NAME: "Sam", cf.EMAIL: "sam@@", cf.PHONE: "555-0134"}) is None


def test_an_unrecognized_form_is_kept_rather_than_silently_dropped():
    """An imported form whose field names are outside the alias table is OUR
    configuration gap, not a bad submission.

    Dropping it makes a broken capture look like an absence of visitors — the
    owner sees silence and concludes nobody fills the form. Storing an empty lead
    is ugly, but it is an honest signal that submissions ARE arriving and
    something needs fixing. Silence is the worse failure, so this must not become
    a rejection."""
    assert cf.validate({"how_did_you_hear": "a friend", "budget_range": "10-20k"}) is None


def test_a_phone_only_submission_is_kept():
    """Plenty of real contact forms ask for a phone and nothing else."""
    assert cf.validate({cf.FULL_NAME: "Sam", cf.PHONE: "(555) 010-1234"}) is None


def test_a_nameless_but_contactable_submission_is_kept():
    """``full_name`` is marked required in the GENERATED markup — that is a browser
    hint on our own form, not a server-side rule. An imported form may not ask for
    a name at all, and a reachable person is a lead regardless."""
    assert cf.validate({cf.EMAIL: "sam@example.com"}) is None


# --------------------------------------------------------------------------- #
# The reply-channel predicates
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", ["sam@example.com", "a.b+tag@sub.example.co.uk"])
def test_real_addresses_read_as_email(value):
    assert cf.looks_like_email(value)


@pytest.mark.parametrize("value", ["", "sam", "sam@", "@example.com", "sam@example", "a b@c.com"])
def test_non_addresses_do_not(value):
    assert not cf.looks_like_email(value)


@pytest.mark.parametrize("value", ["5550134", "(555) 010-1234", "+1 555 010 1234", "555-0134 x22"])
def test_real_numbers_read_as_phone(value):
    """Formatting is whatever the visitor typed — only the digit count matters."""
    assert cf.looks_like_phone(value)


@pytest.mark.parametrize("value", ["", "555", "call me", "n/a"])
def test_too_few_digits_does_not(value):
    """Seven digits is the shortest real subscriber number."""
    assert not cf.looks_like_phone(value)


# --------------------------------------------------------------------------- #
# The derived mapping
# --------------------------------------------------------------------------- #


def test_the_seeded_mapping_reads_exactly_the_fields_the_schema_declares():
    """The mapping is DERIVED, not restated — that is the whole point of the
    module. If a field is added to CONTACT_FIELDS, the seed must grow with it
    without anyone remembering to edit a second list."""
    mapping = cf.default_event_mapping()[cf.CONTACT_FORM_TYPE]
    fields = mapping["fields"]
    assert isinstance(fields, dict)
    assert mapping["creates"] == cf.CONTACT_CREATES
    assert set(fields) == set(cf.CONTACT_FIELD_NAMES)
    for name, template in fields.items():
        assert template == f"{{{{ payload.{name} }}}}"
