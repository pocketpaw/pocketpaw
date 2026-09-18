# tests/cloud/sites/test_foreign_concierge_bind.py — binding a foreign concierge
# is IDEMPOTENT: one row, one agent, one charge, however many times it is called.
#
# WHY THIS EXISTS. ``mint_foreign_site`` mints unconditionally and now debits $19
# per call, so the moment it acquired a caller a repeat "connect" click became a
# repeat purchase. ``bind_foreign_concierge`` is the resolve-or-buy layer over it,
# and every assertion here is about the WALLET and the COLLECTION rather than
# about a return value, because a version that resolves the row but re-charges
# anyway returns exactly the right Site.
#
# The ones that carry the most weight:
#
#   * ``test_two_concurrent_binds_buy_one_concierge`` — the shape a billing race
#     actually takes. Sequential idempotence is the easy half; two binds awaited
#     together both find nothing and both mint unless something serialises them.
#   * ``test_a_bind_never_touches_a_published_site_for_the_same_pocket`` — the
#     foreign row and the published Worker row coexist for one pocket on purpose.
#     A resolver that conflated them would let a rotate invalidate the embed key
#     of a site somebody is serving.
#   * ``test_rotating_retires_the_old_key_at_the_resolver`` — asked of
#     ``resolve_site_key`` rather than of the field, since rotation exists to make
#     a LEAKED key stop working and only the resolver can say whether it did.
#   * ``test_a_rebind_to_another_tenants_agent_is_refused`` — a rebind is the
#     obvious place to try to attach a victim tenant's agent to a bar you control.
#
# tests/mutations/foreign_bind_idempotence.json is the other half of this file: it
# deletes the resolve-or-create check and the charge's one-time-ness on purpose
# and expects these tests to notice.

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound
from pocketpaw_ee.cloud.billing import site_plans
from pocketpaw_ee.cloud.credits import service as credits_service
from pocketpaw_ee.cloud.models.agent import Agent
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.cloud.models.site_origin_claim import SiteOriginClaim
from pocketpaw_ee.sites import service as sites_service

from pocketpaw.paw_bar.store import PawBarStore

pytestmark = pytest.mark.anyio

_HOST = "brewco.example"
_OWNER = "user:maya"
_POCKET = "pk-bind"
_OWNED_POCKET = {"name": "Shop", "rippleSpec": {}}

# The price off the catalog, not written as a number here: a test that hard-codes
# 19 stops testing the purchase and starts testing the sticker.
_STAFF = site_plans.site_scoped_tier("staff")
assert _STAFF is not None
_PRICE_CREDITS = _STAFF.monthly_price_usd * 100
_FUNDED = _PRICE_CREDITS * 5


@pytest_asyncio.fixture
async def store(tmp_path, mongo_db):  # noqa: ARG001 — mongo_db initialises Beanie
    """A tmp paw-bar store patched in at the source, so the provisioning funnel and
    the assertions below read the SAME widget table."""
    s = PawBarStore(tmp_path / "foreign-bind.db")
    with patch("pocketpaw_ee.api.get_paw_bar_store", return_value=s):
        yield s


async def _verify_origin(workspace_id: str, host: str = _HOST) -> None:
    """Seed the VERIFIED ownership claim the mint gates on.

    Written directly rather than driven through the probe: this suite is about
    idempotence, and tests/ee/sites/test_domain_ownership.py owns how a proof is
    obtained.
    """
    now = datetime.now(UTC)
    await SiteOriginClaim(
        workspace=workspace_id,
        host=host,
        token=f"pawverify-{uuid4().hex}",
        status="verified",
        issued_at=now,
        expires_at=now + timedelta(days=7),
        verified_at=now,
        method="well-known",
        issued_by=_OWNER,
    ).insert()


async def _fund(workspace_id: str, credits: int = _FUNDED) -> None:
    await credits_service.grant(
        workspace=workspace_id,
        amount=credits,
        cause="top_up",
        idempotency_key=f"seed-{workspace_id}-{credits}",
    )


def _owned_pocket():
    """Patch the pocket ownership gate to an owned pocket.

    The gate itself is covered in tests/cloud/sites/test_foreign_site_mint.py; this
    file is about what happens after it passes.
    """
    return patch(
        "pocketpaw_ee.cloud.pockets.service.get",
        new=AsyncMock(return_value=_OWNED_POCKET),
    )


async def _bind(workspace_id: str, **overrides: Any) -> Site:
    kwargs: dict[str, Any] = dict(
        workspace_id=workspace_id,
        pocket_id=_POCKET,
        owner=_OWNER,
        allowed_origins=[f"https://{_HOST}"],
        name="Brew Co Concierge",
    )
    kwargs.update(overrides)
    with _owned_pocket():
        return await sites_service.bind_foreign_concierge(**kwargs)


async def _foreign_rows(workspace_id: str) -> list[Site]:
    return await Site.find(
        {"workspace": workspace_id, "pocket_id": _POCKET, "foreign_origin": True}
    ).to_list()


async def _concierge_agents(workspace_id: str) -> list[Agent]:
    docs = await Agent.find(Agent.workspace == workspace_id).to_list()
    return [d for d in docs if d.slug.startswith("concierge-")]


# --------------------------------------------------------------------------- #
# 1. Idempotence — the money
# --------------------------------------------------------------------------- #


async def test_a_first_bind_buys_a_concierge_and_provisions_its_agent(store):  # noqa: ARG001
    """The feature: one bind leaves a PAID row with an agent actually behind it.

    Asserted as "the bar is bound" rather than "an agent exists", because an agent
    nothing points at answers nobody."""
    ws = "ws-bind-first"
    await _fund(ws)
    await _verify_origin(ws)

    site = await _bind(ws)

    assert site.foreign_origin is True
    assert site.subscription_status == "active", "the month was bought"
    assert await credits_service.balance(ws) == _FUNDED - _PRICE_CREDITS

    widget = await store.list_widgets(pocket_id=_POCKET, workspace_id=ws, limit=1)
    assert widget, "the bind must leave a paw-bar widget behind"
    assert widget[0].agent_id, "and that widget must be bound to a concierge agent"
    assert len(await _concierge_agents(ws)) == 1


async def test_two_sequential_binds_buy_one_concierge(store):
    """A repeat bind RESOLVES. It does not mint, and it does not charge again.

    The debit is keyed ``site_plan:<site_id>:<tier>:<date>``, so a second row is a
    second key and a second $19 — deduping the row is the only thing that dedupes
    the money."""
    ws = "ws-bind-twice"
    await _fund(ws)
    await _verify_origin(ws)

    first = await _bind(ws)
    second = await _bind(ws)

    assert str(second.id) == str(first.id), "the second bind must resolve the first row"
    assert len(await _foreign_rows(ws)) == 1, "one concierge per pocket"
    assert len(await _concierge_agents(ws)) == 1, "one agent per pocket"
    assert await credits_service.balance(ws) == _FUNDED - _PRICE_CREDITS, "charged once"

    widgets = await store.list_widgets(pocket_id=_POCKET, workspace_id=ws, limit=10)
    assert len(widgets) == 1, "and one bar"


async def test_two_concurrent_binds_buy_one_concierge(store):  # noqa: ARG001
    """THE BILLING RACE. Two binds awaited together must still buy one month.

    This is the shape the bug actually takes: a double-submitted button, or two
    requests landing in the same event loop. Both check "does a foreign row exist
    for this pocket?", both see nothing, and both mint unless the check and the
    insert are serialised. The sequential test above is green against a pure
    check-then-act and cannot catch this.

    IT NEEDS A REAL YIELD, and that is the whole reason this test is written the
    way it is. ``asyncio.gather`` interleaves nothing by itself — it only lets a
    coroutine hand over control at a point that actually suspends, and neither
    mongomock's fake driver nor a bare ``AsyncMock`` ever does. Left to those, the
    first bind runs start to finish before the second begins and the test passes
    against no guard at all (measured: it does).

    So the pocket gate — a real network round-trip in production, and the first
    await inside the critical section — is patched to something that genuinely
    suspends. That is not staging an artificial race; it is restoring the one the
    fake driver removes.
    """
    ws = "ws-bind-race"
    await _fund(ws)
    await _verify_origin(ws)

    async def _slow_pocket_gate(*_args, **_kwargs):
        # Two yields: enough for the sibling coroutine to run its own existence
        # check and reach this same point before either of us gets to the insert.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return _OWNED_POCKET

    with patch("pocketpaw_ee.cloud.pockets.service.get", new=_slow_pocket_gate):
        first, second = await asyncio.gather(
            sites_service.bind_foreign_concierge(
                workspace_id=ws,
                pocket_id=_POCKET,
                owner=_OWNER,
                allowed_origins=[f"https://{_HOST}"],
                name="Brew Co Concierge",
            ),
            sites_service.bind_foreign_concierge(
                workspace_id=ws,
                pocket_id=_POCKET,
                owner=_OWNER,
                allowed_origins=[f"https://{_HOST}"],
                name="Brew Co Concierge",
            ),
        )

    assert str(first.id) == str(second.id), "both callers must get the SAME concierge"
    assert len(await _foreign_rows(ws)) == 1, "a race must not mint a second site"
    assert len(await _concierge_agents(ws)) == 1
    assert await credits_service.balance(ws) == _FUNDED - _PRICE_CREDITS, (
        "a race that charges twice has taken a customer's money twice"
    )


async def test_a_repeat_bind_does_not_widen_the_origin_allowlist(store):  # noqa: ARG001
    """An existing row is returned UNTOUCHED.

    A bind that quietly merged the caller's ``allowed_origins`` into a live
    concierge would be a way to bypass the verified-origin gate one call at a
    time: mint against a domain you own, then re-bind naming one you do not."""
    ws = "ws-bind-no-widen"
    await _fund(ws)
    await _verify_origin(ws)

    first = await _bind(ws)
    second = await _bind(ws, allowed_origins=[f"https://{_HOST}", "https://not-mine.example"])

    assert second.allowed_origins == first.allowed_origins == [_HOST]
    stored = await Site.get(str(first.id))
    assert stored is not None and stored.allowed_origins == [_HOST]


# --------------------------------------------------------------------------- #
# 2. The gates are inherited, not re-implemented
# --------------------------------------------------------------------------- #


async def test_an_unverified_origin_refuses_with_no_row_and_no_charge(store):  # noqa: ARG001
    """The mint's origin gate must still hold THROUGH the bind.

    Asserted as no row AND no debit rather than as "it raised": a bind that
    inserted first and refused afterwards raises too, and leaves an embed
    credential for somebody else's domain in the collection."""
    ws = "ws-bind-unverified"
    await _fund(ws)
    # Deliberately no claim seeded.

    with pytest.raises(Forbidden) as exc:
        await _bind(ws)

    assert exc.value.code == "sites.origin_unverified"
    assert await Site.find_one(Site.workspace == ws) is None
    assert await credits_service.balance(ws) == _FUNDED


async def test_a_cross_tenant_pocket_is_still_refused_through_the_bind(store):  # noqa: ARG001
    """The pocket gate too — the resolved concierge context reads
    ``pocket:<pocket_id>``'s knowledge, so binding one you cannot access is a KB
    leak, not merely a bad row."""
    ws = "ws-bind-cross-tenant"
    await _fund(ws)
    await _verify_origin(ws)

    denied = AsyncMock(side_effect=Forbidden("pocket.access_denied", "no access"))
    with patch("pocketpaw_ee.cloud.pockets.service.get", new=denied):
        with pytest.raises(Forbidden) as exc:
            await sites_service.bind_foreign_concierge(
                workspace_id=ws,
                pocket_id="pk-victim",
                owner=_OWNER,
                allowed_origins=[f"https://{_HOST}"],
            )

    assert exc.value.code == "pocket.access_denied"
    assert await Site.find_one(Site.workspace == ws) is None
    assert await credits_service.balance(ws) == _FUNDED


async def test_a_failed_charge_leaves_nothing_to_resolve_on_the_next_bind(store):  # noqa: ARG001
    """A refused purchase must not leave a row a LATER bind would adopt for free.

    Fail-closed is inherited from the mint, but the bind is what makes the
    consequence visible: an unpaid survivor would be resolved by the next call and
    hand the customer a concierge nobody ever bought."""
    ws = "ws-bind-broke"
    await _verify_origin(ws)  # funded with nothing

    from pocketpaw_ee.cloud._core.errors import InsufficientCredits

    with pytest.raises(InsufficientCredits):
        await _bind(ws)

    assert await _foreign_rows(ws) == []
    await _fund(ws)
    site = await _bind(ws)
    assert site.subscription_status == "active"
    assert await credits_service.balance(ws) == _FUNDED - _PRICE_CREDITS


# --------------------------------------------------------------------------- #
# 3. The published site for the same pocket is a different row
# --------------------------------------------------------------------------- #


async def test_a_bind_never_touches_a_published_site_for_the_same_pocket(store):  # noqa: ARG001
    """A pocket may carry BOTH a published Worker site and a foreign concierge.

    They coexist on purpose — the foreign row is deliberately not minted at the
    published site's stable per-pocket id. A resolver that found the published row
    would charge nothing, return a live site, and let a later rotate invalidate
    the embed key of a page that is actually being served."""
    ws = "ws-bind-published-too"
    await _fund(ws)
    await _verify_origin(ws)

    published = Site(
        workspace=ws,
        pocket_id=_POCKET,
        owner=_OWNER,
        name="Brew Co",
        script_name="paw-site-live",
        deployed=True,
        url="https://brewco.pages.dev",
        signed_key="site_key_" + "p" * 24,
        plan_tier="site",
        subscription_status="active",
    )
    await published.insert()

    site = await _bind(ws)

    assert str(site.id) != str(published.id), "the bind must not adopt the published row"
    assert site.foreign_origin is True

    after = await Site.get(str(published.id))
    assert after is not None
    assert after.signed_key == "site_key_" + "p" * 24, "the live embed key is untouched"
    assert after.deployed is True and after.script_name == "paw-site-live"
    assert after.foreign_origin is False

    # And the resolver stays pointed at the foreign row.
    resolved = await sites_service.foreign_site_for_pocket(ws, _POCKET)
    assert resolved is not None and str(resolved.id) == str(site.id)


# --------------------------------------------------------------------------- #
# 4. Rotate
# --------------------------------------------------------------------------- #


async def test_rotating_retires_the_old_key_at_the_resolver(store):  # noqa: ARG001
    """Rotation exists to make a LEAKED key stop working, so ask the resolver.

    A field-shaped assertion ("signed_key changed") passes for a version that
    writes the new key to the doc in memory and never saves it."""
    from fastapi import HTTPException
    from pocketpaw_ee.cloud.auth import site_keys

    ws = "ws-bind-rotate"
    await _fund(ws)
    await _verify_origin(ws)

    site = await _bind(ws)
    old_key = site.signed_key
    assert old_key

    rotated = await sites_service.rotate_foreign_concierge_key(workspace_id=ws, pocket_id=_POCKET)

    assert rotated.signed_key != old_key
    assert rotated.signed_key.startswith("site_key_")
    assert str(rotated.id) == str(site.id), "rotation is not a repurchase"

    with pytest.raises(HTTPException) as exc:
        await site_keys.resolve_site_key(old_key, f"https://{_HOST}", "cust-1")
    assert exc.value.status_code == 401, "the leaked key must no longer resolve"

    ctx = await site_keys.resolve_site_key(rotated.signed_key, f"https://{_HOST}", "cust-1")
    assert ctx is not None, "and the new one must"


async def test_rotating_leaves_the_purchase_alone(store):  # noqa: ARG001
    """Same row, same tier, same subscription, same renewal date. A rotation that
    reset any of those would silently re-open a paid month."""
    ws = "ws-bind-rotate-billing"
    await _fund(ws)
    await _verify_origin(ws)

    site = await _bind(ws)
    before = (site.plan_tier, site.subscription_status, site.renewal_date, site.period_paid_usd)

    rotated = await sites_service.rotate_foreign_concierge_key(workspace_id=ws, pocket_id=_POCKET)

    assert (
        rotated.plan_tier,
        rotated.subscription_status,
        rotated.renewal_date,
        rotated.period_paid_usd,
    ) == before
    assert await credits_service.balance(ws) == _FUNDED - _PRICE_CREDITS, "no second charge"


async def test_rotating_a_pocket_with_no_concierge_is_a_404(store):  # noqa: ARG001
    """Not a silent no-op, and not a fresh mint: a rotate the caller thinks
    succeeded, on a pocket that has no concierge, hides a wrong pocket id."""
    with pytest.raises(NotFound):
        await sites_service.rotate_foreign_concierge_key(
            workspace_id="ws-bind-nothing", pocket_id=_POCKET
        )


# --------------------------------------------------------------------------- #
# 5. Rebind
# --------------------------------------------------------------------------- #


async def _make_agent(workspace_id: str, slug: str) -> str:
    from pocketpaw_ee.cloud.agents import service as agents_service
    from pocketpaw_ee.cloud.agents.dto import CreateAgentRequest

    ctx = agents_service.legacy_ctx(_OWNER, workspace_id)
    agent = await agents_service.create(
        ctx,
        workspace_id,
        CreateAgentRequest(name=slug, slug=slug, visibility="workspace"),
    )
    return agent.id


async def test_a_rebind_points_the_bar_at_a_new_agent_without_stranding_the_row(store):
    """The credential row survives a rebind untouched.

    That is the whole requirement: swapping which agent answers must not cost the
    buyer the ``signed_key`` already embedded on their live page, nor the month
    they paid for. A rebind that re-minted the Site would 403 every visitor until
    the owner re-pasted a snippet."""
    ws = "ws-bind-rebind"
    await _fund(ws)
    await _verify_origin(ws)

    site = await _bind(ws)
    original_key = site.signed_key
    widgets = await store.list_widgets(pocket_id=_POCKET, workspace_id=ws, limit=1)
    provisioned_agent = widgets[0].agent_id
    assert provisioned_agent

    replacement = await _make_agent(ws, "hand-built-concierge")
    bound = await sites_service.rebind_foreign_concierge(
        workspace_id=ws, pocket_id=_POCKET, agent_id=replacement
    )

    assert bound == replacement
    assert bound != provisioned_agent
    after_widgets = await store.list_widgets(pocket_id=_POCKET, workspace_id=ws, limit=10)
    assert len(after_widgets) == 1, "a rebind must not mint a second bar"
    assert after_widgets[0].agent_id == replacement

    stored = await Site.get(str(site.id))
    assert stored is not None, "the credential row must still exist"
    assert stored.signed_key == original_key, "and still carry the embedded key"
    assert stored.subscription_status == "active"
    assert len(await _foreign_rows(ws)) == 1
    assert await credits_service.balance(ws) == _FUNDED - _PRICE_CREDITS


async def test_a_rebind_to_another_tenants_agent_is_refused(store):
    """Tenancy, at the one call that takes an arbitrary agent id.

    An unchecked id here serves a victim workspace's agent — its persona, its
    knowledge scope, its connectors — to the visitors of a bar you control."""
    ws = "ws-bind-rebind-tenant"
    await _fund(ws)
    await _verify_origin(ws)
    await _bind(ws)

    widgets = await store.list_widgets(pocket_id=_POCKET, workspace_id=ws, limit=1)
    before = widgets[0].agent_id

    victim_agent = await _make_agent("ws-someone-else", "victim-concierge")

    with pytest.raises(NotFound):
        await sites_service.rebind_foreign_concierge(
            workspace_id=ws, pocket_id=_POCKET, agent_id=victim_agent
        )

    after = await store.list_widgets(pocket_id=_POCKET, workspace_id=ws, limit=1)
    assert after[0].agent_id == before, "a refused rebind must not have written"


async def test_a_rebind_with_no_agent_reprovisions_the_canonical_one(store):
    """The repair path for a bar whose agent was deleted.

    ``ensure_site_agent`` refuses to replace a live bind, so without an explicit
    re-provision a stale ``agent_id`` is permanent and the bar answers nothing."""
    ws = "ws-bind-reprovision"
    await _fund(ws)
    await _verify_origin(ws)

    site = await _bind(ws)
    widgets = await store.list_widgets(pocket_id=_POCKET, workspace_id=ws, limit=1)
    original = widgets[0].agent_id

    # Point the bar at an agent that no longer exists, the state a delete leaves.
    await store.update_fields(widgets[0].id, {"agent_id": "gone-forever"}, workspace_id=ws)

    bound = await sites_service.rebind_foreign_concierge(workspace_id=ws, pocket_id=_POCKET)

    assert bound == original, "re-provisioning resolves the deterministic slug again"
    after = await store.list_widgets(pocket_id=_POCKET, workspace_id=ws, limit=1)
    assert after[0].agent_id == original
    assert str((await Site.get(str(site.id))).id) == str(site.id)


async def test_a_reprovision_resets_a_bar_bound_to_a_LIVE_but_wrong_agent(store):
    """Re-provision means RESET TO CANONICAL, not merely "repair a dead pointer".

    A bar pointing at an agent that was deleted is repaired by the funnel on its
    own — ``ensure_site_agent`` notices the id does not resolve and falls through.
    The bind that only that case exercises is therefore not tested at all, which
    is how the stale-bind clear escaped a mutation that deleted it.

    The case that needs the clear is a bar bound to an agent that is perfectly
    ALIVE and simply not the one the owner wants any more. ``ensure_site_agent``
    returns a live bind untouched by design (a manual bind is somebody's
    deliberate choice), so without clearing it first a re-provision is a no-op and
    the owner has no way back to the site's own concierge.
    """
    ws = "ws-bind-reset"
    await _fund(ws)
    await _verify_origin(ws)

    site = await _bind(ws)
    widgets = await store.list_widgets(pocket_id=_POCKET, workspace_id=ws, limit=1)
    canonical = widgets[0].agent_id
    assert canonical

    # Point the bar at a different, LIVE agent — the state an explicit rebind
    # leaves, and the one the funnel refuses to overwrite.
    other = await _make_agent(ws, "some-other-live-agent")
    await sites_service.rebind_foreign_concierge(workspace_id=ws, pocket_id=_POCKET, agent_id=other)
    mid = await store.list_widgets(pocket_id=_POCKET, workspace_id=ws, limit=1)
    assert mid[0].agent_id == other

    bound = await sites_service.rebind_foreign_concierge(workspace_id=ws, pocket_id=_POCKET)

    assert bound == canonical, "a re-provision must come back to the site's own concierge"
    after = await store.list_widgets(pocket_id=_POCKET, workspace_id=ws, limit=1)
    assert after[0].agent_id == canonical
    assert len(await _concierge_agents(ws)) == 1, "and must not mint a second one"
    stored = await Site.get(str(site.id))
    assert stored is not None and stored.subscription_status == "active"


async def test_a_rebind_on_a_pocket_with_no_concierge_is_a_404(store):  # noqa: ARG001
    with pytest.raises(NotFound):
        await sites_service.rebind_foreign_concierge(
            workspace_id="ws-bind-nothing-2", pocket_id=_POCKET
        )
