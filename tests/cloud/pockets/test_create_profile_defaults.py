# tests/cloud/pockets/test_create_profile_defaults.py
# Created: 2026-06-08 (M3 v2 — create-time surface_profile derivation) — pins the
#   PURE helper ``derive_create_time_profile``: the conservative mapping table is
#   empty today, so every type/pattern (including the sites shape) returns None
#   (= inherit the surface-kind default; the surface default already covers
#   sites). Also exercises the MECHANISM with a temporarily-injected rule to
#   prove wiring + match precedence + wildcard keys, so a future product policy
#   can add a real row with confidence.

from __future__ import annotations

import pocketpaw_ee.cloud.pockets.create_profile_defaults as cpd
from pocketpaw_ee.cloud.pockets.create_profile_defaults import derive_create_time_profile
from pocketpaw_ee.cloud.surface.domain import PocketSurfaceProfile


def test_empty_table_returns_none_for_everything() -> None:
    # The table is intentionally empty today — no input gets an override.
    assert derive_create_time_profile("custom", None) is None
    assert derive_create_time_profile("site", "landing") is None
    assert derive_create_time_profile("dashboard", "viewer") is None
    assert derive_create_time_profile(None, None) is None


def test_sites_inherit_surface_default_no_override() -> None:
    # Explicit pin of the sites analysis: type="site"/pattern="landing" gets NO
    # entity override because the /sites + /pockets surface default already
    # resolves ripple_mode="on" for ripple-track sites. Duplicating it here would
    # be a no-op; turning ripple off would be wrong (ripple sites author a spec).
    assert derive_create_time_profile("site", "landing") is None
    assert derive_create_time_profile("site", None) is None


def test_mechanism_matches_exact_rule(monkeypatch) -> None:
    profile = PocketSurfaceProfile(skill_names=["demo-skill"])
    monkeypatch.setattr(cpd, "_CREATE_TIME_RULES", [(("widget", "kiosk"), lambda: profile)])
    assert cpd.derive_create_time_profile("widget", "kiosk") is profile
    # Non-matching type/pattern still returns None.
    assert cpd.derive_create_time_profile("widget", "other") is None
    assert cpd.derive_create_time_profile("other", "kiosk") is None


def test_mechanism_wildcard_key(monkeypatch) -> None:
    profile = PocketSurfaceProfile(ripple_mode="off")
    # (None, "landing") is a type-wildcard: any type with pattern="landing".
    monkeypatch.setattr(cpd, "_CREATE_TIME_RULES", [((None, "landing"), lambda: profile)])
    assert cpd.derive_create_time_profile("site", "landing") is profile
    assert cpd.derive_create_time_profile("anything", "landing") is profile
    assert cpd.derive_create_time_profile("site", "viewer") is None


def test_mechanism_first_match_wins(monkeypatch) -> None:
    specific = PocketSurfaceProfile(skill_names=["specific"])
    wildcard = PocketSurfaceProfile(skill_names=["wildcard"])
    monkeypatch.setattr(
        cpd,
        "_CREATE_TIME_RULES",
        [
            (("site", "landing"), lambda: specific),  # most-specific first
            ((None, None), lambda: wildcard),
        ],
    )
    assert cpd.derive_create_time_profile("site", "landing") is specific
    # Falls through to the catch-all wildcard for anything else.
    assert cpd.derive_create_time_profile("custom", None) is wildcard
