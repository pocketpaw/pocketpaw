# tests/cloud/growth/test_discovery.py — tests for the /growth ICP discovery
# engine: the email-provenance rule, the ICP CRUD service seams, the discovery
# run against a FAKE ``ResearchFn`` (code under test never calls a real LLM —
# the belt/headless pattern), the preview path, and the bounds.
#
# Created 2026-07-29 (feat/growth-discovery): the provenance rule first,
# because it is the constraint the rest of the slice exists to protect. An LLM
# cannot run Clay's verification waterfall, so a "found" address that nobody
# read off a page is a guess — and a guessed address bounces, burns the sending
# domain's reputation, and poisons the list. These tests pin that the rule is
# STRUCTURAL (a filter the data must pass) rather than a request in a prompt.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.growth.domain import (
    RECORDABLE_EMAIL_CONFIDENCE,
    EmailEvidence,
    recordable_emails,
)

# ---------------------------------------------------------------------------
# The provenance rule at the domain level
# ---------------------------------------------------------------------------


class TestRecordableEmails:
    """``recordable_emails`` is THE door between "the research mentioned an
    address" and "we stored one". Nothing else in the discovery path may open
    it, so every way of failing to prove where an address came from is pinned
    here."""

    def test_an_observed_address_with_its_page_is_recorded(self):
        found = recordable_emails(
            [
                EmailEvidence(
                    address="hello@acme-dental.com",
                    confidence="observed",
                    seen_at_url="https://acme-dental.com/contact",
                )
            ]
        )
        assert found == ("hello@acme-dental.com",)

    @pytest.mark.parametrize("confidence", ["guessed", "claimed"])
    def test_an_unobserved_address_produces_nothing(self, confidence: str):
        """The core case. A pattern-built ``first@company.com`` and an address
        an aggregator merely asserted are both refused, even when they carry a
        URL — the URL is where the CLAIM was made, not where the address was
        read."""
        found = recordable_emails(
            [
                EmailEvidence(
                    address="sam@acme-dental.com",
                    confidence=confidence,
                    seen_at_url="https://directory.example/acme",
                )
            ]
        )
        assert found == ()

    def test_observed_without_a_url_produces_nothing(self):
        """An ``observed`` claim with no page is unfalsifiable — which is
        exactly the shape a model produces when it wants to say yes."""
        assert recordable_emails([EmailEvidence("sam@acme.com", confidence="observed")]) == ()

    def test_the_default_confidence_is_the_untrusted_one(self):
        """Evidence that never says how it was obtained fails closed. A
        research implementation that forgets the field gets no address, not a
        free promotion to observed."""
        assert EmailEvidence(address="sam@acme.com").confidence not in RECORDABLE_EMAIL_CONFIDENCE
        assert recordable_emails([EmailEvidence(address="sam@acme.com")]) == ()

    def test_a_blank_address_is_dropped_even_when_observed(self):
        assert recordable_emails([EmailEvidence("   ", "observed", "https://acme.com")]) == ()

    def test_the_same_address_on_two_pages_is_one_address(self):
        found = recordable_emails(
            [
                EmailEvidence("Hello@Acme.com", "observed", "https://acme.com/contact"),
                EmailEvidence("hello@acme.com", "observed", "https://acme.com/about"),
                EmailEvidence("sales@acme.com", "observed", "https://acme.com/about"),
            ]
        )
        assert found == ("hello@acme.com", "sales@acme.com")

    def test_a_good_address_survives_a_batch_of_bad_ones(self):
        """Mixed evidence keeps what it can prove instead of failing the whole
        prospect — the row is still worth having without the guesses."""
        found = recordable_emails(
            [
                EmailEvidence("guess@acme.com", "guessed"),
                EmailEvidence("hello@acme.com", "observed", "https://acme.com/contact"),
                EmailEvidence("claimed@acme.com", "claimed", "https://directory.example/acme"),
            ]
        )
        assert found == ("hello@acme.com",)
