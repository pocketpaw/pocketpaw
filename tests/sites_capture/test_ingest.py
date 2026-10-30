# tests/sites_capture/test_ingest.py — failing-first tests for the OSS-core
# site form-capture ingest primitive (Task 3.1, Paw Sites publish pipeline).
# Exercises the pure predicates/interpolation generalized from paw_bar:
# origin pinning (fail-closed on the public capture path), honeypot detection,
# and {{ placeholder }} mapping interpolation. Created 2026-05-30.
from __future__ import annotations

from pocketpaw.sites_capture.ingest import (
    interpolate_mapping,
    is_honeypot_tripped,
    origin_allowed,
)
from pocketpaw.sites_capture.models import SiteEventMapping


def test_origin_allowed_host_only_match():
    # host-only match: port + path ignored (mirrors paw-bar _origin_allowed)
    assert origin_allowed(["brightsmiledental.com"], "https://brightsmiledental.com:443/contact")
    assert not origin_allowed(["brightsmiledental.com"], "https://evil.example.com")


def test_origin_allowed_empty_allowlist_blocks_in_capture_mode():
    # Capture path is public-on-the-internet; unlike paw-bar's demo default,
    # an empty allowlist must FAIL CLOSED (no origin → reject).
    assert not origin_allowed([], "https://anything.example.com")


def test_honeypot_tripped_when_hidden_field_filled():
    assert is_honeypot_tripped(
        {"name": "Sam", "company_website": "spammy"}, honeypot_field="company_website"
    )
    assert not is_honeypot_tripped(
        {"name": "Sam", "company_website": ""}, honeypot_field="company_website"
    )


def test_interpolate_mapping_resolves_placeholders():
    mapping = SiteEventMapping(
        creates="AppointmentRequest",
        fields={"name": "{{ payload.full_name }}", "note": "from {{ payload.city }}"},
    )
    props = interpolate_mapping(mapping, {"payload": {"full_name": "Sam", "city": "Reno"}})
    assert props == {"name": "Sam", "note": "from Reno"}
