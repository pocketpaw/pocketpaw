# tests/cloud/sites/test_concierge_not_on_the_domain_flag.py — turning on the
# custom-domain paywall must not switch off every concierge in production.
#
# Created 2026-08-21 (fix/sites-concierge-flag). REPRODUCES A LIVE REGRESSION
# introduced by feat/sites-billing-flag (#1988) and observed in production the
# same day: the captain set POCKETPAW_SITES_BILLING_ENFORCED=1 to start capping
# custom domains, and the visitor concierge vanished from every site.
#
# The mechanism, and every step of it is load-bearing:
#
#   1. #1988 routed FIVE seams through ``billing.enforcement.sites_enforced()``.
#      Two of them are the domain caps. Two of them are the concierge — the
#      visitor-facing ``auth.site_keys.concierge_available`` and the publish-time
#      embed decision. One switch, two unrelated products.
#   2. ``concierge_entitled`` needs ``tier.sells_concierge`` AND an active
#      subscription. ``sells_concierge`` is ``key != BASE_SITE_PLAN_KEY``, and
#      every production site is on the floor.
#   3. There is no way out. ``subscription_status`` only reaches "active" through
#      ``activate_site``, which runs off the Dodo webhook, which needs a product
#      id from ``POCKETPAW_DODO_SITE_PRODUCTS`` — configured in no deploy file
#      anywhere. So no site can be on a concierge-selling tier, and no site could
#      buy its way onto one even if it were.
#
# A paywall that can only refuse and never convert is not a paywall, it is an
# outage. So the concierge gate gets its own switch, defaulting off, and neither
# ``billing_enforced`` nor ``sites_billing_enforced`` turns it on any more. The
# second half of that matters as much as the first: ``billing_enforced`` had the
# identical trap armed for whenever someone turns credits enforcement on.

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.auth.site_keys import concierge_available  # noqa: E402
from pocketpaw_ee.cloud.billing import site_plans  # noqa: E402

import pocketpaw.config as ppconfig  # noqa: E402


def _flags(monkeypatch, *, glob=False, sites=False, concierge=False) -> None:
    monkeypatch.setattr(
        ppconfig,
        "get_settings",
        lambda: SimpleNamespace(
            billing_enforced=glob,
            sites_billing_enforced=sites,
            sites_concierge_enforced=concierge,
            dodo_site_products=None,
        ),
    )


def _a_free_site(*, concierge_enabled: bool = True) -> SimpleNamespace:
    """A production site as production actually has them: floor tier, no
    subscription, owner's concierge switch on."""
    return SimpleNamespace(
        id="6512c1f0e4b0a1b2c3d4e5f6",
        workspace="ws_1",
        concierge_enabled=concierge_enabled,
        plan_tier=site_plans.BASE_SITE_PLAN_KEY,
        subscription_status="none",
    )


# --------------------------------------------------------------------------- #
# The regression, exactly as it was reported.
# --------------------------------------------------------------------------- #


def test_the_domain_flag_does_not_switch_off_the_concierge(monkeypatch):
    """The reported bug. Capping custom domains is not a statement about the
    concierge, and a customer who turns on one must not silently lose the other."""
    _flags(monkeypatch, sites=True)

    assert concierge_available(_a_free_site()) is True


def test_the_global_billing_flag_does_not_switch_off_the_concierge(monkeypatch):
    """The same trap, still armed on the other switch.

    ``billing_enforced`` is the credits/seats/pockets switch. Before this fix it
    also killed every concierge, so whoever eventually turns credit enforcement on
    would have filed this identical report. Fixed in the same breath because
    leaving it is knowingly leaving a landmine.
    """
    _flags(monkeypatch, glob=True)

    assert concierge_available(_a_free_site()) is True


def test_both_paywall_flags_together_still_leave_the_concierge_alone(monkeypatch):
    _flags(monkeypatch, glob=True, sites=True)

    assert concierge_available(_a_free_site()) is True


# --------------------------------------------------------------------------- #
# The gate is not deleted. It is opt-in.
# --------------------------------------------------------------------------- #


def test_its_own_flag_still_enforces_the_plan(monkeypatch):
    """The entitlement logic is intact and reachable — it just needs to be asked
    for. This is what someone sets on the day a tier can actually sell it."""
    _flags(monkeypatch, concierge=True)

    assert concierge_available(_a_free_site()) is False


def test_a_paying_site_serves_its_concierge_when_the_gate_is_on(monkeypatch):
    """The gate must not become the bug. A tier that sells the concierge, on an
    active subscription, is served."""
    _flags(monkeypatch, concierge=True)
    selling = next(
        (t.key for t in site_plans.list_site_plans() if t.sells_concierge),
        None,
    )
    assert selling is not None, "no catalog tier sells the concierge — catalog changed"
    paid = SimpleNamespace(
        id="6512c1f0e4b0a1b2c3d4e5f6",
        workspace="ws_1",
        concierge_enabled=True,
        plan_tier=selling,
        subscription_status="active",
    )

    assert concierge_available(paid) is True


# --------------------------------------------------------------------------- #
# The owner's own switch is untouched by any of this.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("glob", "sites", "concierge"),
    [(False, False, False), (True, True, True), (False, True, False)],
)
def test_the_owner_switch_still_wins_under_every_flag_combination(
    monkeypatch, glob, sites, concierge
):
    """``concierge_enabled=False`` is the owner saying no. No billing posture
    overrides that, in either direction."""
    _flags(monkeypatch, glob=glob, sites=sites, concierge=concierge)

    assert concierge_available(_a_free_site(concierge_enabled=False)) is False


def test_the_setting_itself_defaults_off_and_reads_its_env_var(monkeypatch):
    """Against the real Settings, not a stub.

    Every other test here monkeypatches ``get_settings``, so none of them can see
    the field's declared default — and the default is the whole safety property.
    Ship it as True and every deployment loses its concierge on the next upgrade,
    with no env change to blame it on.
    """
    from pocketpaw.config import Settings

    assert Settings().sites_concierge_enforced is False

    monkeypatch.setenv("POCKETPAW_SITES_CONCIERGE_ENFORCED", "1")
    assert Settings().sites_concierge_enforced is True


def test_a_settings_object_predating_the_flag_reads_as_off(monkeypatch):
    """Fail OPEN here, unlike the domain caps.

    Everywhere else in this codebase an absent flag means "do not enforce", which
    is also the safe direction for a paywall. For the concierge it is doubly
    right: the failure mode of guessing wrong is a visitor talking to nobody.
    """
    monkeypatch.setattr(ppconfig, "get_settings", lambda: SimpleNamespace())

    assert concierge_available(_a_free_site()) is True
