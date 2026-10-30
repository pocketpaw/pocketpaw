# tests/cloud/auth/test_social_login_origin.py — consent returns to the face
# that started it.
#
# Created 2026-09-12 (fix/social-login-origin).
#
# One deployment now serves two faces on two hostnames: the Paw OS and the
# Otherhand kiosk. The social callback redirected to a single configured
# ``POCKETPAW_FRONTEND_BASE_URL``, so signing in with Google on the kiosk
# landed the user on the OS. Nothing errors — it simply reads as the button
# being broken.
#
# The origin is now pinned into the single-use OAuth state at authorize time,
# the same way ``flow`` already is, and validated against the deployment's CORS
# allowlist on the way in AND on the way out. That validation is the whole
# security story: without it this is an open redirect that also hands over a
# session cookie.
#
# Mutations: tests/mutations/social_login_origin.json.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.auth.social import service as social_service

_KIOSK = "https://otherhand.interacly.com"
_OS = "https://paw.hzd.interacly.com"


@pytest.fixture(autouse=True)
def _allowlist(monkeypatch):
    """Pin the deployment's allowed origins, the way an operator's env would."""
    import pocketpaw.api.cors as cors

    monkeypatch.setattr(cors, "allowed_origins", lambda: [_KIOSK, _OS])
    monkeypatch.setenv("POCKETPAW_FRONTEND_BASE_URL", _OS)


# ── the allowlist ────────────────────────────────────────────────────────


def test_an_allowed_origin_survives():
    assert social_service.safe_frontend_origin(_KIOSK) == _KIOSK


def test_an_unknown_origin_is_dropped():
    """The open-redirect case, and the reason this validates at all.

    An attacker who gets a victim to start a login from their own page must
    not receive the redirect that carries the session cookie.

    Mutation that must break this: return the candidate unvalidated.
    """
    assert social_service.safe_frontend_origin("https://evil.example.com") == ""


def test_a_lookalike_origin_is_dropped():
    """Exact match, not prefix or suffix. ``interacly.com.evil.test`` ends with
    nothing useful and ``https://otherhand.interacly.com.evil.test`` begins with
    a real host — both must fail.
    """
    assert social_service.safe_frontend_origin("https://otherhand.interacly.com.evil.test") == ""
    assert social_service.safe_frontend_origin("https://evil.test/otherhand.interacly.com") == ""


def test_a_missing_origin_is_dropped():
    assert social_service.safe_frontend_origin(None) == ""
    assert social_service.safe_frontend_origin("") == ""


def test_a_broken_allowlist_does_not_break_login(monkeypatch):
    """Fails closed to the configured default rather than raising. A settings
    problem must degrade the landing page, never the ability to sign in.
    """
    import pocketpaw.api.cors as cors

    def _boom():
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr(cors, "allowed_origins", _boom)

    assert social_service.safe_frontend_origin(_KIOSK) == ""


# ── which origin the callback lands on ───────────────────────────────────


def test_a_pinned_origin_wins_over_the_env_var():
    """The bug itself: the kiosk visitor comes back to the kiosk.

    Mutation that must break this: ignore the pinned origin.
    """
    assert social_service.frontend_base_url(_KIOSK) == _KIOSK


def test_no_pinned_origin_keeps_the_configured_default():
    """Every pre-existing flow — desktop, links, single-face deployments — is
    byte-identical. Without this, a deployment with one face and no Referer
    would redirect to nowhere.
    """
    assert social_service.frontend_base_url(None) == _OS
    assert social_service.frontend_base_url("") == _OS


def test_a_trailing_slash_does_not_double_up():
    """``{base}{safe_next}`` is string concatenation, so a trailing slash here
    produces ``//pockets`` — a protocol-relative URL that leaves the origin.
    """
    assert social_service.frontend_base_url(_KIOSK + "/") == _KIOSK


# ── the state carries it ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_begin_login_pins_the_validated_origin(monkeypatch):
    """Pinned at authorize time, not read from the callback's query string —
    the same rule ``flow`` follows, and what makes this unforgeable.
    """
    captured: dict = {}

    class _Provider:
        name = "google"

        def is_configured(self):
            return True

    async def _fake_issue(provider, *, extra):
        captured.update(extra)
        return "https://accounts.google.com/o/oauth2/v2/auth?x=1"

    monkeypatch.setattr(social_service, "_usable_provider", lambda _n: _Provider())
    monkeypatch.setattr(social_service, "_issue_authorize_url", _fake_issue)

    await social_service.begin_login("google", flow="web", origin=_KIOSK)

    assert captured["origin"] == _KIOSK


@pytest.mark.asyncio
async def test_begin_login_drops_a_hostile_origin(monkeypatch):
    """A hostile Referer reaches the state as an empty string, so the callback
    falls back to the configured default.
    """
    captured: dict = {}

    class _Provider:
        name = "google"

        def is_configured(self):
            return True

    async def _fake_issue(provider, *, extra):
        captured.update(extra)
        return "https://accounts.google.com/o/oauth2/v2/auth?x=1"

    monkeypatch.setattr(social_service, "_usable_provider", lambda _n: _Provider())
    monkeypatch.setattr(social_service, "_issue_authorize_url", _fake_issue)

    await social_service.begin_login("google", flow="web", origin="https://evil.example.com")

    assert captured["origin"] == ""
