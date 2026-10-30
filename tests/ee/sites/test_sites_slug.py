# tests/ee/sites/test_sites_slug.py
# Created: 2026-09-23 (VS-2, feat/sites-first-publish-slug) — the pure rules for a
# site's name-based address: how a display name becomes a slug, which slugs are
# refused, and the order candidates are tried in.
#
# MUTATIONS THAT BREAK THIS FILE (tests/mutations/sites_first_publish_slug.json):
# ``has_reserved_prefix`` returning False (a user name could shadow ``paw-site-<id>``
# or ``paw-sites-dispatch``), and dropping the numbered-variant patterns.
from __future__ import annotations

import pytest
from pocketpaw_ee.sites import slug


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Acme Bakery!", "acme-bakery"),
        ("Café Déjà", "cafe-deja"),
        ("  --Hello,   World--  ", "hello-world"),
        ("ACME_2024 Co.", "acme-2024-co"),
        ("🎉🎂", ""),
        ("", ""),
    ],
)
def test_normalize(raw, expected):
    assert slug.normalize(raw) == expected


def test_a_long_name_is_cut_without_leaving_a_trailing_hyphen():
    # Position 40 lands right after a hyphen, so a naive cut would end in "-".
    raw = "a" * 39 + " bakery"
    out = slug.normalize(raw)

    assert len(out) <= slug.MAX_LEN
    assert not out.endswith("-")
    assert out == "a" * 39


@pytest.mark.parametrize("name", ["acme-bakery", "abc", "a1b", "x" * 40, "cafe-2"])
def test_valid_slugs_pass(name):
    assert slug.validate(name) is None


@pytest.mark.parametrize(
    "name",
    ["ab", "x" * 41, "-acme", "acme-", "Acme", "acme_bakery", "acme bakery", "", "café"],
)
def test_malformed_slugs_are_invalid(name):
    err = slug.validate(name)
    assert err is not None and err.reason == "invalid"


@pytest.mark.parametrize(
    "name",
    ["www", "api", "admin", "mail", "status", "pocketpaw", "dispatch", "localhost"],
)
def test_reserved_names_are_refused(name):
    err = slug.validate(name)
    assert err is not None and err.reason == "reserved"


@pytest.mark.parametrize("name", ["www2", "mail10", "ns1", "mx3", "smtp2", "ftp9"])
def test_numbered_variants_are_refused(name):
    err = slug.validate(name)
    assert err is not None and err.reason == "reserved"


@pytest.mark.parametrize(
    "name", ["paw-site-507f1f77bcf86cd799439011", "paw-sites-dispatch", "paw-x1"]
)
def test_the_paw_prefix_is_ours(name):
    assert slug.has_reserved_prefix(name)
    err = slug.validate(name)
    assert err is not None and err.reason == "reserved"


def test_a_name_that_merely_contains_a_reserved_word_is_fine():
    assert slug.validate("wwwidgets") is None
    assert slug.validate("mailbox-cafe") is None
    assert slug.validate("pawsome") is None


def _fixed(ch: str):
    return lambda _alphabet: ch


def test_candidates_start_with_the_base_then_number_then_randomize():
    got = list(slug.candidates("Acme Bakery", rand=_fixed("q")))

    assert got[:6] == [
        "acme-bakery",
        "acme-bakery-2",
        "acme-bakery-3",
        "acme-bakery-4",
        "acme-bakery-5",
        "acme-bakery-6",
    ]
    assert got[6:] and all(c == "acme-bakery-qqqq" for c in got[6:])


@pytest.mark.parametrize("name", ["🎉", "", "ab", "www", "Admin", "paw-site-123", "paw sites"])
def test_an_unusable_name_falls_back_to_site_random(name):
    got = list(slug.candidates(name, rand=_fixed("7")))

    assert got and all(c == "site-7777" for c in got)
    assert slug.validate("site-7777") is None


def test_every_candidate_of_a_long_name_fits_and_validates():
    name = "The Extremely Long And Wordy Name Of A Very Small Neighbourhood Bakery"
    got = list(slug.candidates(name, rand=_fixed("z")))

    assert len(got) > 7
    for c in got:
        assert len(c) <= slug.MAX_LEN, c
        assert slug.validate(c) is None, c


def test_the_default_randomness_is_base36():
    got = list(slug.candidates("🎉"))

    for c in got:
        assert slug.validate(c) is None
        assert c.startswith("site-") and len(c) == len("site-") + 4
