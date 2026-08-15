# tests/cloud/test_paw_bar_concierge_entitlement.py — the concierge is a PAID
# per-site capability, and until this branch no plan was consulted anywhere.
#
# Created 2026-08-15 (feat/sites-concierge-entitlement). The hole:
# ``SiteEntitlements.concierge_enabled`` was a straight PASS-THROUGH of the owner's
# kill switch — it echoed what the caller handed it and read no tier, no
# subscription, nothing. Its two shipped siblings (``badge_required``,
# ``custom_domain``) both gate on tier AND an active subscription. So a free site
# turned its concierge on and served it indefinitely, and so did a paid site whose
# subscription had lapsed.
#
# WHAT IS NOT NEW HERE. The disable → remove chain was already built and works:
# ``site_keys`` 403s chat/action, the frame returns the invisible shell which posts
# ``pawbar:dead``, and the loader removes the iframe. This branch adds the BILLING
# reason to trigger it, and reuses that path byte for byte — a refused visitor sees
# exactly what an owner-disabled one sees.
#
# The gate rule (captain's call 2026-08-15): any tier above the free floor, with an
# active subscription. Derived from ``BASE_SITE_PLAN_KEY`` rather than a per-tier
# catalog flag, because no tier grants concierge today and the one that will
# (``staff``) does not exist until the pricing-spec rekey. So every criterion below
# reads the floor out of the catalog instead of hardcoding "basic"/"pro" — the rekey
# moves these tests with the catalog instead of breaking them.
#
# The two questions stay SEPARATE (``concierge_enabled`` vs ``concierge_entitled``)
# and that separation is itself pinned below: collapsing them into one boolean makes
# "off" unattributable, and support cannot tell an owner who flipped the switch from
# an owner whose plan lapsed.

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.billing import site_plans  # noqa: E402
from pocketpaw_ee.cloud.entitlements.service import resolve_site_entitlements  # noqa: E402

import pocketpaw.config as ppconfig  # noqa: E402

_VALID_KEY = "site_key_" + "a" * 24


def _paid_tier() -> str:
    """The cheapest catalog tier above the free floor — see the header."""
    for tier in site_plans.list_site_plans():
        if tier.key != site_plans.BASE_SITE_PLAN_KEY:
            return tier.key
    raise AssertionError("catalog has no tier above the floor — catalog changed")


def _free_tier() -> str:
    return site_plans.BASE_SITE_PLAN_KEY


def _enforce(monkeypatch, *, on: bool) -> None:
    """Point the lazily-imported ``get_settings`` at a billing-posture stub."""
    monkeypatch.setattr(
        ppconfig,
        "get_settings",
        lambda: SimpleNamespace(billing_enforced=on, dodo_site_products=None),
    )


def _resolve(**ov):
    kw = {
        "site_id": "6512c1f0e4b0a1b2c3d4e5f6",
        "workspace_id": "ws-1",
        "plan_tier": _paid_tier(),
        "subscription_status": "active",
        "concierge_enabled": True,
    }
    kw.update(ov)
    return resolve_site_entitlements(**kw)


# --------------------------------------------------------------------------- #
# Layer 1 — the resolver. Pure, so every branch runs without a database.
# --------------------------------------------------------------------------- #


def test_a_free_site_is_not_entitled_to_a_concierge():
    """The headline case. The owner's switch is ON and the plan still says no."""
    ent = _resolve(plan_tier=_free_tier(), concierge_enabled=True)

    assert ent.concierge_enabled is True  # the owner's intent, untouched
    assert ent.concierge_entitled is False  # the plan's answer
    assert ent.concierge_available is False  # what the seams ask


def test_an_unset_tier_is_not_entitled():
    """No ``plan_tier`` at all (pre-BC-9 rows, every first publish) resolves to the
    floor. Fail-closed: absent is free, not exempt."""
    assert _resolve(plan_tier=None).concierge_available is False


def test_an_unknown_tier_is_not_entitled():
    """A typo'd or retired tier key must not grant a paid capability."""
    assert _resolve(plan_tier="enterprise_platinum").concierge_available is False


@pytest.mark.parametrize("status", ["none", "pending", "cancelled"])
def test_a_paid_tier_without_an_active_subscription_is_not_entitled(status):
    """Cancellation never resets ``plan_tier``, and an unconfigured Dodo product
    records a paid tier with no charge at all. Tier-alone would serve both."""
    ent = _resolve(plan_tier=_paid_tier(), subscription_status=status)

    assert ent.subscription_active is False
    assert ent.concierge_entitled is False
    assert ent.concierge_available is False


def test_an_active_paid_site_is_entitled():
    """The paying case still works — the gate must not become the bug."""
    ent = _resolve(plan_tier=_paid_tier(), subscription_status="active")

    assert ent.concierge_entitled is True
    assert ent.concierge_available is True


# --------------------------------------------------------------------------- #
# The two questions are separate, and stay separate.
# --------------------------------------------------------------------------- #


def test_the_owner_switch_and_the_plan_are_distinguishable():
    """Both roads lead to unavailable, and the REASON survives the trip.

    This is the test that fails if someone folds the two fields into one boolean.
    An owner who switched it off and an owner whose plan lapsed need different
    remedies, so the dashboard has to be able to tell them apart.
    """
    owner_off = _resolve(plan_tier=_paid_tier(), concierge_enabled=False)
    plan_says_no = _resolve(plan_tier=_free_tier(), concierge_enabled=True)

    assert owner_off.concierge_available is False
    assert plan_says_no.concierge_available is False
    # ...and they are not the same state.
    assert owner_off.concierge_enabled is False
    assert owner_off.concierge_entitled is True
    assert plan_says_no.concierge_enabled is True
    assert plan_says_no.concierge_entitled is False


def test_an_entitled_site_with_the_switch_off_stays_off():
    """Entitlement never overrides the owner. Paying for a concierge does not force
    one onto a site whose owner silenced it."""
    assert _resolve(plan_tier=_paid_tier(), concierge_enabled=False).concierge_available is False


# --------------------------------------------------------------------------- #
# Layer 2 — the public seams. A refused visitor sees what a disabled one sees.
# --------------------------------------------------------------------------- #


async def _site(**ov):
    from pocketpaw_ee.cloud.models.site import Site

    d = dict(
        workspace="ws-1",
        pocket_id="pocket-1",
        owner="user:maya",
        script_name="",
        signed_key=_VALID_KEY,
        allowed_origins=["brewco.com"],
        concierge_enabled=True,
        plan_tier=_free_tier(),
        subscription_status="none",
    )
    d.update(ov)
    s = Site(**d)
    await s.insert()
    return s


@pytest_asyncio.fixture
async def frame_client(mongo_db):
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.paw_bar.router import router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


@pytest.mark.asyncio
async def test_the_frame_refuses_a_free_site_with_the_self_remove_message(
    frame_client, monkeypatch
):
    """A refused frame tells the loader to remove the iframe, so the page shows
    NOTHING — not even the invisible dock sliver.

    ``po`` is the parent origin the loader always sends; the self-remove script is
    emitted only when it survives the allowlist check, because postMessage needs a
    concrete target origin. This is the same ``_dead_frame_response`` the owner's
    kill switch already returns.
    """
    _enforce(monkeypatch, on=True)
    await _site(plan_tier=_free_tier(), concierge_enabled=True)

    res = await frame_client.get(
        "/paw-bar/frame",
        params={"key": _VALID_KEY, "w": "pp_seed", "po": "https://brewco.com"},
    )

    assert res.status_code == 403
    assert "pawbar:dead" in res.text  # the loader's cue to remove the iframe
    assert "window.__PAWBAR__" not in res.text  # no bootstrap config leaks


@pytest.mark.asyncio
async def test_the_refused_frame_never_renders_an_error_payload(frame_client, monkeypatch):
    """The body lands inside a VISIBLE iframe on the customer's site, so it must be
    blank — a JSON error is a defect, not a refusal. The 2026-07-30 rig showed a
    literal {"detail":"concierge_disabled"} printed on a customer's page.

    No ``po`` here, which is the harsher case: no self-remove script is emitted, so
    the body is all the visitor could possibly see.
    """
    _enforce(monkeypatch, on=True)
    await _site(plan_tier=_free_tier(), concierge_enabled=True)

    res = await frame_client.get("/paw-bar/frame", params={"key": _VALID_KEY, "w": "pp_seed"})

    assert res.status_code == 403
    assert "concierge" not in res.text.lower()  # no reason leaks onto the page
    assert "entitled" not in res.text.lower()
    assert "plan" not in res.text.lower()
    assert "<body></body>" in res.text  # nothing rendered at all


@pytest.mark.asyncio
async def test_the_frame_still_renders_for_an_entitled_site(frame_client, monkeypatch):
    """A paying site is untouched — the gate must not take the bar off sites that
    bought it."""
    _enforce(monkeypatch, on=True)
    await _site(plan_tier=_paid_tier(), subscription_status="active")

    res = await frame_client.get("/paw-bar/frame", params={"key": _VALID_KEY, "w": "pp_seed"})

    assert res.status_code == 200
    assert "window.__PAWBAR__" in res.text


@pytest.mark.asyncio
async def test_the_frame_is_unaffected_when_billing_is_off(frame_client, monkeypatch):
    """OSS / self-host has no billing and must not lose its concierge to this
    branch."""
    _enforce(monkeypatch, on=False)
    await _site(plan_tier=_free_tier(), concierge_enabled=True)

    res = await frame_client.get("/paw-bar/frame", params={"key": _VALID_KEY, "w": "pp_seed"})

    assert res.status_code == 200


@pytest.mark.asyncio
async def test_chat_is_refused_for_a_free_site(mongo_db, monkeypatch):
    """The key-resolution seam (chat / action / cart) refuses too, so the gate is not
    a frame-only cosmetic. Without this, removing the iframe would still leave the
    API answering anyone who called it directly."""
    from fastapi import HTTPException
    from pocketpaw_ee.cloud.auth.site_keys import resolve_site_key

    _enforce(monkeypatch, on=True)
    await _site(plan_tier=_free_tier(), concierge_enabled=True)

    with pytest.raises(HTTPException) as exc:
        await resolve_site_key(_VALID_KEY, "https://brewco.com", "cust_1")

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_chat_still_works_for_an_entitled_site(mongo_db, monkeypatch):
    from pocketpaw_ee.cloud.auth.site_keys import resolve_site_key

    _enforce(monkeypatch, on=True)
    await _site(plan_tier=_paid_tier(), subscription_status="active")

    ctx = await resolve_site_key(_VALID_KEY, "https://brewco.com", "cust_1")

    assert ctx.workspace_id == "ws-1"


@pytest.mark.asyncio
async def test_the_billing_refusal_is_distinguishable_from_the_owner_switch(mongo_db, monkeypatch):
    """Both refuse with 403 — the visitor experience is identical by design — but the
    detail differs so logs and support can tell them apart."""
    from fastapi import HTTPException
    from pocketpaw_ee.cloud.auth.site_keys import resolve_site_key

    _enforce(monkeypatch, on=True)
    await _site(plan_tier=_free_tier(), concierge_enabled=True)

    with pytest.raises(HTTPException) as exc:
        await resolve_site_key(_VALID_KEY, "https://brewco.com", "cust_1")

    assert exc.value.status_code == 403
    assert exc.value.detail == "concierge_not_entitled"
    assert exc.value.detail != "concierge_disabled"


# --------------------------------------------------------------------------- #
# Layer 3 — publish. An unentitled site ships with no snippet at all.
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def bound_widget(tmp_path):
    """A pocket whose paw-bar widget exists AND is bound to an agent.

    Every other ``concierge_snippet`` gate must PASS, or a test asserting "no
    snippet" proves nothing — the first mutation sweep caught exactly that: with no
    widget in the store, gate 3 returned "" on its own and the entitlement mutation
    escaped while the test stayed green.
    """
    from unittest.mock import patch

    from pocketpaw.paw_bar.models import PawBarSpec, PawBarWidget
    from pocketpaw.paw_bar.store import PawBarStore

    store = PawBarStore(tmp_path / "entitlement.db")
    await store.create_widget(
        PawBarWidget(
            id="pp_bound",
            pocket_id="pocket-1",
            owner="user:maya",
            workspace_id="ws-1",
            agent_id="agent_1",  # bound: gate 4 passes
            name="Bar",
            spec=PawBarSpec(widget_id="pp_bound", pocket_id="pocket-1", blocks=[]),
        )
    )
    with patch("pocketpaw_ee.api.get_paw_bar_store", return_value=store):
        yield store


@pytest.mark.asyncio
async def test_an_unentitled_site_publishes_without_the_snippet(bound_widget, monkeypatch):
    """The strongest form of "remove the bar": the built page never carries it.

    ``concierge_snippet`` gate 1 already omits the snippet when the owner's switch is
    off, and the marker is only ever written and never re-written, so the next build
    regenerates the page clean. Routing the billing answer through the same gate
    means an unentitled site's page ships bar-less rather than shipping a bar that
    403s every visitor at runtime.
    """
    from pocketpaw_ee.paw_bar.embed import concierge_snippet

    _enforce(monkeypatch, on=True)
    snippet = await concierge_snippet(
        workspace_id="ws-1",
        pocket_id="pocket-1",
        site_key=_VALID_KEY,
        api_base="https://api.test/api/v1",
        concierge_enabled=True,
        concierge_entitled=False,
    )

    assert snippet == ""


@pytest.mark.asyncio
async def test_an_entitled_site_still_gets_its_snippet(bound_widget, monkeypatch):
    """The control the test above needs to mean anything: with every other gate
    passing and entitlement granted, the snippet IS produced. Without this, a
    ``return ""`` at the top of the function would satisfy the assertion above."""
    from pocketpaw_ee.paw_bar.embed import concierge_snippet

    _enforce(monkeypatch, on=True)
    snippet = await concierge_snippet(
        workspace_id="ws-1",
        pocket_id="pocket-1",
        site_key=_VALID_KEY,
        api_base="https://api.test/api/v1",
        concierge_enabled=True,
        concierge_entitled=True,
    )

    assert snippet != ""
    assert "pp_bound" in snippet
