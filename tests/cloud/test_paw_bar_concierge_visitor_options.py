# tests/cloud/test_paw_bar_concierge_visitor_options.py — the owner's visitor
# options on the concierge settings API and in the frame config.
#
# Five Site fields (disclosure line, privacy link, consent, voice, full-screen) and
# the appearance's bar ``size`` go through PATCH /paw-bar/admin/site/{id}/settings
# and come out of both frames' ``window.__PAWBAR__`` as ``disclosure``,
# ``privacyHref``, ``consentRequired``, ``voice``, ``expandable`` and ``barSize``.
# These tests pin:
#
#   * validation: the disclosure cap and one-line cleaning, the https-only privacy
#     link, and the size enum coercing to "sm";
#   * old rows: a Site without the fields reads and boots today's bar;
#   * the branding gate: hiding "Powered by" on a site without the badge-removal
#     entitlement is 402 ``branding_not_entitled`` with nothing written, while
#     showing it or re-sending a stored False is always accepted; the frame emits
#     ``poweredBy = show_branding or not entitled``;
#   * ``branding_removable`` and ``actions_snippet`` in the settings response, the
#     snippet empty exactly when ``embed_snippet`` is.

# Fixtures are imported from the settings suite; naming one as a parameter injects it.
# ruff: noqa: F811

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from pocketpaw.paw_bar.appearance import ConciergeAppearance
from tests.cloud.test_paw_bar_concierge_settings import (  # noqa: F401 — fixtures
    _PUBLIC_BASE,
    _VALID_KEY,
    _site,
    _widget,
    client,
    owner_client,
    real_pocket_client,
)

_ENTITLED = "pocketpaw_ee.sites.service.badge_removal_entitled"


def _settings_url(site: Any) -> str:
    return f"/paw-bar/admin/site/{site.id}/settings"


def _boot(html: str) -> dict[str, Any]:
    m = re.search(r"window\.__PAWBAR__ = (\{.*?\});</script>", html, re.S)
    assert m, html[:400]
    return json.loads(m.group(1))


async def _frame(c) -> dict[str, Any]:
    res = await c.get("/paw-bar/frame", params={"key": _VALID_KEY})
    assert res.status_code == 200, res.text
    return _boot(res.text)


# --------------------------------------------------------------------------- #
# 1. Validation
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"concierge_disclosure": "d" * 141},
        {"concierge_privacy_url": "http://brewco.com/privacy"},
        {"concierge_privacy_url": "javascript:alert(1)"},
        {"concierge_privacy_url": "https://"},
        {"concierge_privacy_url": "//brewco.com/privacy"},
        {"concierge_privacy_url": "https://brewco.com/a b"},
        {"concierge_privacy_url": 'https://brewco.com/"onmouseover=x'},
        {"concierge_privacy_url": "https://brewco.com/<script>"},
        {"concierge_privacy_url": "https://brewco.com/" + "p" * 500},
    ],
)
async def test_patch_rejects_bad_visitor_texts(client, body):
    from pocketpaw_ee.cloud.models.site import Site

    c, _store = client
    site = await _site(concierge_disclosure="Kept")
    res = await c.patch(_settings_url(site), json=body)
    assert res.status_code == 422, res.text
    stored = await Site.get(site.id)
    assert stored is not None and stored.concierge_disclosure == "Kept"


@pytest.mark.asyncio
async def test_patch_round_trips_every_visitor_option(client):
    c, _store = client
    site = await _site()
    body = {
        "concierge_disclosure": "  An AI\nanswers here.​ Check with us.  ",
        "concierge_privacy_url": "  https://brewco.com/privacy?x=1#top ",
        "concierge_consent_required": True,
        "concierge_voice": False,
        "concierge_expandable": False,
    }
    res = await c.patch(_settings_url(site), json=body)
    assert res.status_code == 200, res.text
    got = (await c.get(_settings_url(site))).json()
    for out in (res.json(), got):
        assert out["concierge_disclosure"] == "An AI answers here. Check with us."
        assert out["concierge_privacy_url"] == "https://brewco.com/privacy?x=1#top"
        assert out["concierge_consent_required"] is True
        assert out["concierge_voice"] is False
        assert out["concierge_expandable"] is False


@pytest.mark.asyncio
async def test_patch_accepts_the_caps_and_clears(client):
    c, _store = client
    site = await _site(concierge_disclosure="old", concierge_privacy_url="https://a.test/p")
    url = "https://brewco.com/" + "p" * (500 - len("https://brewco.com/"))
    res = await c.patch(
        _settings_url(site),
        json={"concierge_disclosure": "d" * 140, "concierge_privacy_url": url},
    )
    assert res.status_code == 200, res.text
    res = await c.patch(
        _settings_url(site), json={"concierge_disclosure": "", "concierge_privacy_url": ""}
    )
    assert res.status_code == 200, res.text
    assert res.json()["concierge_disclosure"] == ""
    assert res.json()["concierge_privacy_url"] == ""


@pytest.mark.asyncio
async def test_patch_is_partial_for_visitor_options(client):
    c, _store = client
    site = await _site(concierge_voice=False, concierge_disclosure="Kept")
    res = await c.patch(_settings_url(site), json={"concierge_expandable": False})
    assert res.status_code == 200, res.text
    assert res.json()["concierge_voice"] is False
    assert res.json()["concierge_disclosure"] == "Kept"


def test_bar_size_is_an_enum_that_coerces():
    assert ConciergeAppearance().size == "sm"
    assert ConciergeAppearance(size="lg").size == "lg"
    assert ConciergeAppearance(size="xl").size == "sm"


# --------------------------------------------------------------------------- #
# 2. Frame config
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_default_site_boots_todays_bar(client):
    c, _store = client
    await _site()
    with patch(_ENTITLED, new=AsyncMock(return_value=False)):
        boot = await _frame(c)
    assert boot["barSize"] == "sm"
    assert boot["disclosure"] == ""
    assert boot["privacyHref"] == ""
    assert boot["consentRequired"] is False
    assert boot["voice"] is True
    assert boot["expandable"] is True
    assert boot["poweredBy"] is True
    # Every key that was there before is still there.
    for key in ("siteKey", "greeting", "tokens", "tokensDark", "scheme", "launcher", "logo"):
        assert key in boot


@pytest.mark.asyncio
async def test_the_public_frame_carries_the_owner_options(client):
    c, _store = client
    await _site(
        concierge_disclosure="AI here. It can be wrong.",
        concierge_privacy_url="https://brewco.com/privacy",
        concierge_consent_required=True,
        concierge_voice=False,
        concierge_expandable=False,
        concierge_appearance=ConciergeAppearance(size="lg"),
    )
    boot = await _frame(c)
    assert boot["barSize"] == "lg"
    assert boot["disclosure"] == "AI here. It can be wrong."
    assert boot["privacyHref"] == "https://brewco.com/privacy"
    assert boot["consentRequired"] is True
    assert boot["voice"] is False
    assert boot["expandable"] is False


@pytest.mark.asyncio
async def test_a_stored_value_that_no_longer_validates_is_dropped(client):
    """Only the PATCH validates; a row written some other way must not put a
    non-https link or a multi-line text into the visitor's frame."""
    c, _store = client
    await _site(concierge_privacy_url="javascript:alert(1)", concierge_disclosure="x" * 200)
    boot = await _frame(c)
    assert boot["privacyHref"] == ""
    assert boot["disclosure"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("show", "entitled", "powered_by"),
    [(True, True, True), (True, False, True), (False, True, False), (False, False, True)],
)
async def test_powered_by_is_shown_unless_hidden_and_entitled(client, show, entitled, powered_by):
    c, _store = client
    await _site(concierge_appearance=ConciergeAppearance(show_branding=show))
    with patch(_ENTITLED, new=AsyncMock(return_value=entitled)):
        boot = await _frame(c)
    assert boot["poweredBy"] is powered_by


@pytest.mark.asyncio
async def test_an_entitlement_failure_shows_powered_by(client):
    c, _store = client
    await _site(concierge_appearance=ConciergeAppearance(show_branding=False))
    with patch(_ENTITLED, new=AsyncMock(side_effect=RuntimeError("billing down"))):
        boot = await _frame(c)
    assert boot["poweredBy"] is True


@pytest.mark.asyncio
async def test_the_preview_frame_carries_the_same_options(client):
    c, store = client
    site = await _site(
        concierge_disclosure="Preview line",
        concierge_voice=False,
        concierge_appearance=ConciergeAppearance(size="md", show_branding=False),
    )
    await store.create_widget(_widget())
    with patch(_ENTITLED, new=AsyncMock(return_value=True)):
        res = await c.get(f"/paw-bar/admin/site/{site.id}/preview-frame")
    assert res.status_code == 200, res.text
    boot = _boot(res.text)
    assert boot["barSize"] == "md"
    assert boot["disclosure"] == "Preview line"
    assert boot["voice"] is False
    assert boot["poweredBy"] is False


# --------------------------------------------------------------------------- #
# 3. The branding gate on PATCH
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_hiding_branding_without_entitlement_is_402_and_writes_nothing(client):
    from pocketpaw_ee.cloud.models.site import Site

    c, _store = client
    site = await _site(concierge_greeting="Kept")
    body = {
        "concierge_greeting": "Changed",
        "concierge_appearance": {"show_branding": False, "accent": "#ff0055"},
    }
    with patch(_ENTITLED, new=AsyncMock(return_value=False)):
        res = await c.patch(_settings_url(site), json=body)
    assert res.status_code == 402, res.text
    assert res.json()["detail"] == "branding_not_entitled"
    stored = await Site.get(site.id)
    assert stored is not None
    assert stored.concierge_greeting == "Kept"
    assert stored.concierge_appearance.show_branding is True
    # Untouched: still following the site, not the refused body's accent.
    assert stored.concierge_appearance.accent == ""


@pytest.mark.asyncio
async def test_hiding_branding_on_an_entitled_site_is_saved(client):
    c, _store = client
    site = await _site()
    with patch(_ENTITLED, new=AsyncMock(return_value=True)):
        res = await c.patch(
            _settings_url(site), json={"concierge_appearance": {"show_branding": False}}
        )
    assert res.status_code == 200, res.text
    assert res.json()["concierge_appearance"]["show_branding"] is False
    assert res.json()["branding_removable"] is True


@pytest.mark.asyncio
async def test_showing_branding_or_resending_a_stored_false_is_always_allowed(client):
    c, _store = client
    site = await _site(concierge_appearance=ConciergeAppearance(show_branding=False))
    spy = AsyncMock(return_value=False)
    with patch(_ENTITLED, new=spy):
        same = await c.patch(
            _settings_url(site),
            json={"concierge_appearance": {"show_branding": False, "accent": "#112233"}},
        )
        shown = await c.patch(
            _settings_url(site), json={"concierge_appearance": {"show_branding": True}}
        )
    assert same.status_code == 200, same.text
    assert same.json()["concierge_appearance"]["accent"] == "#112233"
    assert shown.status_code == 200, shown.text
    assert shown.json()["concierge_appearance"]["show_branding"] is True
    assert shown.json()["branding_removable"] is False


@pytest.mark.asyncio
async def test_settings_report_branding_removable(client):
    c, _store = client
    site = await _site()
    for entitled in (True, False):
        with patch(_ENTITLED, new=AsyncMock(return_value=entitled)):
            res = await c.get(_settings_url(site))
        assert res.status_code == 200, res.text
        assert res.json()["branding_removable"] is entitled


@pytest.mark.asyncio
async def test_branding_removal_uses_the_site_badge_rule(monkeypatch):
    """Not a new rule: per-site billing off is entitled, and with it on a site
    whose plan keeps the badge is not."""
    from pocketpaw_ee.sites.service import badge_removal_entitled

    import pocketpaw.config as ppconfig

    site = SimpleNamespace(
        id="6512c1f0e4b0a1b2c3d4e5f6",
        workspace="ws-1",
        plan_tier=None,
        subscription_status=None,
        concierge_enabled=True,
    )
    monkeypatch.setattr(ppconfig, "get_settings", lambda: SimpleNamespace(billing_enforced=False))
    assert await badge_removal_entitled(site, partner=None) is True
    monkeypatch.setattr(
        ppconfig,
        "get_settings",
        lambda: SimpleNamespace(billing_enforced=True, dodo_site_products=None),
    )
    assert await badge_removal_entitled(site, partner=None) is False


# --------------------------------------------------------------------------- #
# 4. The actions snippet
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_settings_return_the_actions_snippet_beside_the_embed(owner_client):
    c, store = owner_client
    site = await _site()
    await store.create_widget(_widget())
    res = await c.get(_settings_url(site))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["embed_snippet"]
    assert body["actions_snippet"] == (
        f'<script src="{_PUBLIC_BASE}/paw-bar/actions.js" defer '
        f'data-endpoint="{_PUBLIC_BASE}"></script>'
    )


@pytest.mark.asyncio
async def test_no_actions_snippet_without_an_embed_snippet(owner_client):
    c, store = owner_client
    site = await _site(concierge_enabled=False)
    await store.create_widget(_widget())
    res = await c.get(_settings_url(site))
    assert res.status_code == 200, res.text
    assert res.json()["embed_snippet"] == ""
    assert res.json()["actions_snippet"] == ""


def test_actions_snippet_escapes_the_base():
    from pocketpaw_ee.paw_bar.embed import build_actions_snippet

    tag = build_actions_snippet(api_base='https://api.test/"x/')
    assert '"x' not in tag
    assert "&quot;x/paw-bar/actions.js" in tag
