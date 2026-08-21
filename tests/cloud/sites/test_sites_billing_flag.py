# tests/cloud/sites/test_sites_billing_flag.py — the Paw Sites paywall gets its
# own switch.
#
# Created 2026-08-21 (feat/sites-billing-flag, PW-2). Until now every sites seam
# read ``billing_enforced``, the workspace-wide switch. Turning it on to start
# charging for custom domains also starts 402ing chat runs, seat invites, pocket
# creates, connector enables and uploads — unrelated decisions on unrelated
# timelines, and the reason the sites paywall could not be switched on at all.
#
# ``sites_billing_enforced`` is the second switch, and the condition at every sites
# seam is the OR of the two. Three properties matter, and the middle one is the
# whole point of the flag:
#
#   1. Setting the sites flag alone makes the DOMAIN seams enforce. Domain only:
#      the visitor concierge was on this switch for one day and it caused an
#      outage — see test_concierge_not_on_the_domain_flag.py.
#   2. Setting the sites flag alone leaves EVERY workspace cap untouched. Tested
#      as an explicit negative, not inferred — a flag that quietly widened would
#      pass every positive test in this file.
#   3. Setting the global flag alone still makes the sites seams enforce, so no
#      deployment already setting it sees a change.
#
# Not covered because it is deliberately not wired: the badge stamper
# (``sites.service._stamp_free_badge``) reads NEITHER flag and still badges a free
# site regardless. See ``cloud/billing/enforcement.py`` for why that omission is a
# product decision rather than an oversight.

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud._core.errors import CloudError  # noqa: E402
from pocketpaw_ee.cloud.billing import site_plans  # noqa: E402
from pocketpaw_ee.cloud.billing.enforcement import sites_enforced  # noqa: E402
from pocketpaw_ee.cloud.models.site import Site  # noqa: E402
from pocketpaw_ee.sites import service as sites_service  # noqa: E402
from pocketpaw_ee.sites.domain import CustomHostname, HostnameStatus  # noqa: E402

import pocketpaw.config as ppconfig  # noqa: E402

pytestmark = pytest.mark.usefixtures("mongo_db")


class _RecordingCF:
    def __init__(self) -> None:
        self.create_calls: list[dict] = []
        self.route_calls: list[dict] = []

    async def create_custom_hostname(
        self, hostname: str, *, features: set[str] | None = None
    ) -> CustomHostname:
        self.create_calls.append({"hostname": hostname, "features": features})
        return CustomHostname(
            id=f"ch_{len(self.create_calls)}",
            hostname=hostname,
            status=HostnameStatus.PENDING,
            cname_target="zone_1.cdn.cloudflare.net",
        )

    async def create_worker_route(self, *, pattern: str, script: str) -> str:
        self.route_calls.append({"pattern": pattern, "script": script})
        return f"route_{len(self.route_calls)}"

    async def delete_custom_hostname(self, hostname_id: str) -> None:  # pragma: no cover
        pass


def _flags(monkeypatch, *, glob: bool, sites: bool) -> None:
    """Point ``get_settings`` at a stub carrying both switches."""
    monkeypatch.setattr(
        ppconfig,
        "get_settings",
        lambda: SimpleNamespace(
            billing_enforced=glob,
            sites_billing_enforced=sites,
            # Off in every case here. The concierge has its own switch since
            # 2026-08-21 and neither flag under test reaches it — proven in
            # test_concierge_not_on_the_domain_flag.py, and asserted negatively
            # below alongside the workspace caps.
            sites_concierge_enforced=False,
            dodo_site_products=None,
            max_pockets=None,
        ),
    )


async def _seed_site(*, workspace_id: str, pocket_id: str, plan_tier: str | None = None) -> str:
    doc = Site(
        workspace=workspace_id,
        pocket_id=pocket_id,
        owner="u1",
        name=f"Site {pocket_id}",
        plan_tier=plan_tier or site_plans.BASE_SITE_PLAN_KEY,
        subscription_status="none",
        deployed=True,
    )
    await doc.insert()
    return str(doc.id)


async def _fill_the_free_allowance(ws: str) -> None:
    """Give the workspace its one domained free site, so the next attach is at the cap."""
    first = await _seed_site(workspace_id=ws, pocket_id="pk_first")
    await sites_service.add_domain(
        workspace_id=ws, site_id=first, hostname="www.first.com", _cloudflare=_RecordingCF()
    )


# --------------------------------------------------------------------------- #
# The predicate itself.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("glob", "sites", "expected"),
    [(False, False, False), (True, False, True), (False, True, True), (True, True, True)],
)
def test_either_flag_turns_the_sites_seams_on(monkeypatch, glob, sites, expected):
    """OR, never a replacement. The global flag keeps working on its own, which is
    what makes this change additive for a deployment already setting it."""
    _flags(monkeypatch, glob=glob, sites=sites)

    assert sites_enforced() is expected


def test_settings_missing_the_new_field_read_as_off(monkeypatch):
    """A settings object predating the flag must not raise.

    Stubs across this repo build a namespace with only the fields under test, and a
    seam that AttributeErrors because a stub is a version behind fails for a reason
    that has nothing to do with billing. Absent is also the right fail direction for
    a paywall.
    """
    monkeypatch.setattr(ppconfig, "get_settings", lambda: SimpleNamespace())

    assert sites_enforced() is False


# --------------------------------------------------------------------------- #
# 1. The sites flag alone makes the sites seams enforce.
# --------------------------------------------------------------------------- #


async def test_the_sites_flag_alone_makes_the_domain_cap_bite(monkeypatch):
    _flags(monkeypatch, glob=False, sites=True)
    ws = "ws_sites_flag_only"
    await _fill_the_free_allowance(ws)
    second = await _seed_site(workspace_id=ws, pocket_id="pk_second")
    cf = _RecordingCF()

    with pytest.raises(CloudError) as exc:
        await sites_service.add_domain(
            workspace_id=ws, site_id=second, hostname="www.second.com", _cloudflare=cf
        )

    assert exc.value.code == "billing.custom_domain_limit"
    assert cf.create_calls == []


async def test_the_sites_flag_alone_makes_the_hostname_guard_bite(monkeypatch):
    _flags(monkeypatch, glob=False, sites=True)
    ws = "ws_sites_flag_hostnames"
    site_id = await _seed_site(workspace_id=ws, pocket_id="pk_1")
    for i in range(site_plans.free_max_hostnames_per_site()):
        await sites_service.add_domain(
            workspace_id=ws,
            site_id=site_id,
            hostname=f"host{i}.acme.com",
            _cloudflare=_RecordingCF(),
        )

    with pytest.raises(CloudError) as exc:
        await sites_service.add_domain(
            workspace_id=ws,
            site_id=site_id,
            hostname="onemore.acme.com",
            _cloudflare=_RecordingCF(),
        )

    assert exc.value.code == "billing.custom_domain_limit"


# --------------------------------------------------------------------------- #
# 2. The negative. This is the flag's entire reason for existing.
# --------------------------------------------------------------------------- #


async def test_the_sites_flag_does_not_start_402ing_chat_runs(monkeypatch):
    """``over_billing_limit`` is the run-start credit gate. It reads
    ``billing_enforced`` and must keep reading only that: a customer who turns on
    the sites paywall has not agreed to have their chat runs rejected."""
    from pocketpaw_ee.cloud.credits.guards import over_billing_limit

    _flags(monkeypatch, glob=False, sites=True)

    assert await over_billing_limit("ws_1") is None


async def test_the_sites_flag_does_not_turn_on_the_pocket_cap(monkeypatch):
    from pocketpaw_ee.cloud.pockets.service import _pocket_cap_exceeded

    _flags(monkeypatch, glob=False, sites=True)

    assert await _pocket_cap_exceeded("ws_1") == (False, 0, None)


async def test_the_sites_flag_does_not_turn_on_the_connector_cap(monkeypatch):
    from pocketpaw_ee.cloud.connectors.service import _connector_cap_exceeded

    _flags(monkeypatch, glob=False, sites=True)

    assert await _connector_cap_exceeded("ws_1") == (False, 0, None)


def test_the_sites_flag_does_not_switch_off_the_visitor_concierge(monkeypatch):
    """The negative that cost a production outage to learn.

    This file originally asserted the OPPOSITE — that the sites flag armed the
    concierge gate too, filed under "the sites seams". Enabling the flag for the
    DOMAIN caps on 2026-08-21 then took the concierge off every site at once, with
    no customer able to buy it back: no tier below the unbuilt ``staff`` sells a
    concierge and no Dodo product exists to charge for one. The concierge has its
    own switch now. Full reasoning and the rest of the coverage live in
    tests/cloud/sites/test_concierge_not_on_the_domain_flag.py; this line is here
    so the boundary is visible in the flag's own scope list.
    """
    from pocketpaw_ee.cloud.auth.site_keys import concierge_available

    _flags(monkeypatch, glob=False, sites=True)
    free_site = SimpleNamespace(
        id="6512c1f0e4b0a1b2c3d4e5f6",
        workspace="ws_1",
        concierge_enabled=True,
        plan_tier=site_plans.BASE_SITE_PLAN_KEY,
        subscription_status="none",
    )

    assert concierge_available(free_site) is True


# --------------------------------------------------------------------------- #
# 3. No regression for a deployment already setting the global flag.
# --------------------------------------------------------------------------- #


async def test_the_global_flag_alone_still_enforces_the_domain_cap(monkeypatch):
    _flags(monkeypatch, glob=True, sites=False)
    ws = "ws_global_only"
    await _fill_the_free_allowance(ws)
    second = await _seed_site(workspace_id=ws, pocket_id="pk_second")

    with pytest.raises(CloudError) as exc:
        await sites_service.add_domain(
            workspace_id=ws, site_id=second, hostname="www.second.com", _cloudflare=_RecordingCF()
        )

    assert exc.value.code == "billing.custom_domain_limit"


# --------------------------------------------------------------------------- #
# Both off — OSS / self-host reads nothing extra.
# --------------------------------------------------------------------------- #


async def test_with_both_flags_off_the_second_site_attaches(monkeypatch):
    _flags(monkeypatch, glob=False, sites=False)
    ws = "ws_both_off"
    await _fill_the_free_allowance(ws)
    second = await _seed_site(workspace_id=ws, pocket_id="pk_second")

    res = await sites_service.add_domain(
        workspace_id=ws, site_id=second, hostname="www.second.com", _cloudflare=_RecordingCF()
    )

    assert res.hostname == "www.second.com"


async def test_with_both_flags_off_the_census_query_never_runs(monkeypatch):
    """Not merely "no error" — no read either. ``_load`` uses ``find_one``; the
    census is the only caller of ``find`` on this path, which is what makes the
    assertion specific rather than incidental."""
    _flags(monkeypatch, glob=False, sites=False)
    ws = "ws_both_off_noread"
    site_id = await _seed_site(workspace_id=ws, pocket_id="pk_1")

    finds: list[object] = []
    original = sites_service._SiteDoc.find

    def _spy(*args, **kwargs):
        finds.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(sites_service._SiteDoc, "find", _spy)

    await sites_service.add_domain(
        workspace_id=ws, site_id=site_id, hostname="www.noread.com", _cloudflare=_RecordingCF()
    )

    assert finds == []


def test_with_both_flags_off_only_the_owners_switch_decides_the_concierge(monkeypatch):
    from pocketpaw_ee.cloud.auth.site_keys import concierge_available

    _flags(monkeypatch, glob=False, sites=False)
    free_site = SimpleNamespace(
        id="6512c1f0e4b0a1b2c3d4e5f6",
        workspace="ws_1",
        concierge_enabled=True,
        plan_tier=site_plans.BASE_SITE_PLAN_KEY,
        subscription_status="none",
    )

    assert concierge_available(free_site) is True


# --------------------------------------------------------------------------- #
# The setting is real, not just a name the seams agree on.
# --------------------------------------------------------------------------- #


def test_the_setting_defaults_off_and_reads_its_env_var(monkeypatch):
    """Default off is what keeps this branch dark, and the env name is the thing an
    operator actually types."""
    from pocketpaw.config import Settings

    assert Settings().sites_billing_enforced is False

    monkeypatch.setenv("POCKETPAW_SITES_BILLING_ENFORCED", "1")
    assert Settings().sites_billing_enforced is True
