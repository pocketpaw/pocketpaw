# tests/cloud/other_hand/test_illustration_credentials.py — whose fal key pays.
#
# Created 2026-09-11 (feat/byok-image-key). Every assertion here is about money
# moving to the right account, which is why they are worth more than the
# generation they gate.
#
# The rule that is easiest to get wrong, and the one this file exists for: a
# workspace on its OWN key must not claim the platform's daily ceiling. It is
# paying in real money; charging it a quota as well is charging it twice, and
# the failure is silent — the pictures keep appearing until the twentieth one
# of the day stops for no reason the user can see.
#
# No ``mongo_db`` fixture: the resolver's only database call is stubbed, because
# what is under test is the DECISION, not the read.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.other_hand import illustration_credentials as creds


@pytest.fixture
def stored_key(monkeypatch):
    """Control what the workspace has stored, without a database."""

    def _set(value):
        async def _resolve(workspace_id):
            return value

        import pocketpaw_ee.cloud.byok.service as byok_service

        monkeypatch.setattr(byok_service, "resolve_image_key", _resolve)

    return _set


@pytest.fixture
def platform_key(monkeypatch):
    """Control whether the deployment itself has a fal key."""

    def _set(value):
        from pocketpaw_ee.cloud.studio import fal_edit

        monkeypatch.setattr(fal_edit, "fal_api_key", lambda: value)

    return _set


@pytest.mark.asyncio
async def test_own_key_wins_and_is_not_the_platform_s(stored_key, platform_key):
    stored_key("tenant-id:tenant-secret")
    platform_key("platform-key")

    grant = await creds.resolve("ws-1", is_guest=False)

    assert isinstance(grant, creds.IllustrationGrant)
    assert grant.api_key == "tenant-id:tenant-secret"
    # ``byok`` is what tells the caller to SKIP the platform ceiling. Without
    # it the workspace pays twice — in money and in a quota it has no reason
    # to be inside — and nothing visibly breaks until the cap bites.
    assert grant.byok is True


@pytest.mark.asyncio
async def test_a_guest_with_their_own_key_may_illustrate(stored_key, platform_key):
    """The point of the whole feature.

    Guests are refused illustrations because a guest can mint a fresh workspace
    for a fresh ceiling — an argument about the PLATFORM's money, which says
    nothing about someone spending their own.
    """
    stored_key("guest-id:guest-secret")
    platform_key("platform-key")

    grant = await creds.resolve("ws-guest", is_guest=True)

    assert isinstance(grant, creds.IllustrationGrant)
    assert grant.byok is True


@pytest.mark.asyncio
async def test_a_guest_without_a_key_is_still_refused(stored_key, platform_key):
    """Unchanged, and it must stay unchanged: this is the branch that protects
    the platform's card from a signup form that asks for nothing."""
    stored_key(None)
    platform_key("platform-key")

    refusal = await creds.resolve("ws-guest", is_guest=True)

    assert isinstance(refusal, creds.IllustrationRefusal)
    assert refusal.guest_gate is True
    # The caller turns this into the signup prompt the page already renders,
    # so a guest sees the way forward rather than an error.


@pytest.mark.asyncio
async def test_an_account_without_a_key_falls_back_to_the_platform(stored_key, platform_key):
    stored_key(None)
    platform_key("platform-key")

    grant = await creds.resolve("ws-1", is_guest=False)

    assert isinstance(grant, creds.IllustrationGrant)
    assert grant.api_key == "platform-key"
    # False is what makes the caller claim the daily budget. This is today's
    # behaviour and the branch that must not change.
    assert grant.byok is False


@pytest.mark.asyncio
async def test_no_key_anywhere_refuses_without_a_guest_gate(stored_key, platform_key):
    """An operator who never set FAL_AI_API_KEY is not a signup opportunity —
    showing this user a "create an account" wall would send them to do
    something that would not help."""
    stored_key(None)
    platform_key(None)

    refusal = await creds.resolve("ws-1", is_guest=False)

    assert isinstance(refusal, creds.IllustrationRefusal)
    assert refusal.guest_gate is False


@pytest.mark.asyncio
async def test_no_workspace_resolves_like_no_key(stored_key, platform_key):
    """A turn with no tenancy has no stored key to find. It must degrade to the
    ordinary rules rather than raising inside a tool call."""
    stored_key(None)
    platform_key("platform-key")

    grant = await creds.resolve(None, is_guest=False)

    assert isinstance(grant, creds.IllustrationGrant)
    assert grant.byok is False
