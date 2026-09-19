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
# THERE ARE THREE GUARDS AND THEY MASK EACH OTHER, so each is tested at the level
# where it is the only thing standing:
#
#   * THE DERIVED ``_id`` carries the cross-process billing guarantee. The bind's
#     mutex serialises in-process, so no race test here can reach the primary key
#     — swap the derivation for ``ObjectId()`` and every race test stays green
#     (measured). It is therefore asked of the MINT:
#     ``test_a_second_mint_for_the_same_pocket_loses_before_it_can_charge``.
#   * THE SALT keeps that id clear of the published site's, so a concierge can
#     never collide with a live Worker row:
#     ``test_the_foreign_id_never_lands_on_the_published_id``.
#   * THE MUTEX stops a loser adopting a row the winner is about to delete and
#     handing back a phantom:
#     ``test_a_concurrent_loser_is_never_handed_a_row_the_winner_deletes``.
#
# Also worth the most:
#
#   * ``test_two_concurrent_binds_buy_one_concierge`` — the billing race, and the
#     reason it patches the pocket gate is written in its own docstring.
#   * ``test_a_bind_never_touches_a_published_site_for_the_same_pocket`` — the
#     foreign row and the published Worker row coexist for one pocket on purpose.
#   * ``test_rotating_retires_the_old_key_at_the_resolver`` — asked of
#     ``resolve_site_key`` rather than of the field, since rotation exists to make
#     a LEAKED key stop working and only the resolver can say whether it did.
#   * ``test_a_rebind_to_another_tenants_agent_is_refused`` — a rebind is the
#     obvious place to try to attach a victim tenant's agent to a bar you control.
#
# TWO MONGOMOCK TRAPS ARE LOAD-BEARING HERE, both measured rather than assumed.
# ``asyncio.gather`` interleaves nothing unless something genuinely suspends, and
# the fake driver never does. And a doc instance read from the fake driver is
# MUTATED IN PLACE when another holder saves it, so any assertion on a field can
# heal itself mid-test; row EXISTENCE is a collection fact and cannot.
#
# tests/mutations/foreign_bind_idempotence.json is the other half of this file.

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from pocketpaw_ee.cloud._core.errors import Forbidden, InsufficientCredits, NotFound
from pocketpaw_ee.cloud.billing import service as billing_service
from pocketpaw_ee.cloud.billing import site_plans
from pocketpaw_ee.cloud.credits import service as credits_service
from pocketpaw_ee.cloud.models.agent import Agent
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.cloud.models.site_origin_claim import SiteOriginClaim
from pocketpaw_ee.sites import service as sites_service
from pymongo.errors import DuplicateKeyError

from pocketpaw.paw_bar.store import PawBarStore

pytestmark = pytest.mark.anyio

_HOST = "brewco.example"
_OTHER_HOST = "cafe.example"
_OWNER = "user:maya"
_POCKET = "pk-bind"


def _owned_pocket_doc(workspace_id: str) -> dict:
    """The wire dict ``pockets_service.get`` hands back for a pocket this caller
    owns, IN ``workspace_id``.

    The ``workspace`` key is load-bearing. ``pockets_service.get`` denies only a
    PRIVATE pocket and the model defaults visibility to "workspace", so the mint
    compares this key against the minting tenant itself. A fixture without it
    would test the cross-tenant refusal on every call instead of the path.
    """
    return {"name": "Shop", "rippleSpec": {}, "workspace": workspace_id}


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


def _owned_pocket(workspace_id: str):
    """Patch the pocket ownership gate to a pocket this caller owns in
    ``workspace_id``.

    The gate itself is covered in tests/cloud/sites/test_foreign_site_mint.py; this
    file is about what happens after it passes — which is why the doc must carry
    the tenant it belongs to rather than a shape that merely passes the access
    half.
    """
    return patch(
        "pocketpaw_ee.cloud.pockets.service.get",
        new=AsyncMock(return_value=_owned_pocket_doc(workspace_id)),
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
    with _owned_pocket(workspace_id):
        return await sites_service.bind_foreign_concierge(**kwargs)


async def _foreign_rows(workspace_id: str) -> list[Site]:
    return await Site.find(
        {"workspace": workspace_id, "pocket_id": _POCKET, "foreign_origin": True}
    ).to_list()


def _entitlements(site: Site):
    """What the resolver would actually answer for this row.

    ``concierge_entitled`` is the AND of a rung that sells the concierge with an
    active subscription, so it is the only honest way to ask "would a visitor get
    a bar that talks?" — a row can look healthy field by field and still 403.
    """
    from pocketpaw_ee.cloud.entitlements import service as entitlements_service

    return entitlements_service.resolve_site_entitlements(
        site_id=str(site.id),
        workspace_id=site.workspace,
        plan_tier=site.plan_tier,
        subscription_status=site.subscription_status,
        concierge_enabled=site.concierge_enabled,
    )


def _conflict_then_real():
    """A resolver that misses on the pre-mint check and hits on the recovery read.

    That is exactly what a second process sees: the winner's insert is not visible
    when the loser looks, and is visible once the loser's own insert has collided
    with it.
    """
    real = sites_service.foreign_site_for_pocket
    seen: list[int] = []

    async def _resolve(ws: str, pocket_id: str):
        seen.append(1)
        if len(seen) == 1:
            return None
        return await real(ws, pocket_id)

    return _resolve


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
        return _owned_pocket_doc(ws)

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


async def test_a_concurrent_loser_is_never_handed_a_row_the_winner_deletes(store):  # noqa: ARG001
    """THE HARM THE MUTEX PREVENTS, which is NOT the double charge.

    The derived ``_id`` is what stops two rows and two debits, and it does that
    across processes where a lock cannot reach. This is what the lock is still
    for, and it is a correctness harm rather than a cosmetic one.

    The winner inserts, then charges. A charge that fails DELETES the row
    (``_discard_unpaid_foreign_site``) — fail-closed, and right. In that window an
    unserialised loser catches the primary-key conflict, re-reads, and adopts a
    row that is one step from being removed. Its caller is then handed a PHANTOM
    concierge: a Site object that is not in the collection, that nobody was
    charged for, carrying an embed key ``resolve_site_key`` will never find. The
    buyer gets a snippet for a concierge that does not exist.

    With the lock, both callers fail honestly with the 402 they earned.

    Measured with the lock removed, this exact setup:
        outcomes ['InsufficientCredits', 'Site']
        returned Site d2446366ec038fc58e4e96aa -> in collection: False

    A NOTE ON WHY THE FIRST ATTEMPT AT THIS TEST WAS WRONG, since it is a trap
    anyone re-testing the lock will fall into. The obvious harm to reach for is
    the loser reading a row the winner has not activated yet. That harm is real in
    production and CANNOT be observed here: mongomock hands back the stored dict
    by reference, so the very doc instance the loser is holding flips from "none"
    to "active" underneath it when the winner saves. Asserting on
    ``subscription_status`` therefore passes with the lock deleted. Row EXISTENCE
    is a collection fact and does not heal itself, which is why this asserts that.
    """
    ws = "ws-bind-phantom"
    await _fund(ws)
    await _verify_origin(ws)

    async def _failing_charge(**_kwargs):
        # Yields BEFORE raising. That yield is the window in which the loser
        # re-reads a row the winner is about to delete.
        await asyncio.sleep(0)
        raise InsufficientCredits("credits.insufficient", "no funds")

    async def _slow_pocket_gate(*_args, **_kwargs):
        await asyncio.sleep(0)
        return _owned_pocket_doc(ws)

    def _bind_call():
        return sites_service.bind_foreign_concierge(
            workspace_id=ws,
            pocket_id=_POCKET,
            owner=_OWNER,
            allowed_origins=[f"https://{_HOST}"],
            name="Brew Co Concierge",
        )

    with patch("pocketpaw_ee.cloud.pockets.service.get", new=_slow_pocket_gate):
        with patch.object(billing_service, "charge_site_plan_credits", new=_failing_charge):
            outcomes = await asyncio.gather(_bind_call(), _bind_call(), return_exceptions=True)

    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            assert isinstance(outcome, InsufficientCredits), (
                f"the only honest failure here is the 402, got {outcome!r}"
            )
            continue
        stored = await Site.get(str(outcome.id))
        assert stored is not None, (
            "a caller was handed site "
            f"{outcome.id}, which is not in the collection - a phantom concierge"
        )

    assert await _foreign_rows(ws) == [], "a failed charge leaves no row behind"
    assert await credits_service.balance(ws) == _FUNDED, "and takes nothing"


# --------------------------------------------------------------------------- #
# 1a. The primary key — the cross-process guard, tested where it is observable.
#
# ``bind_foreign_concierge``'s mutex serialises in-process, so NO race test in
# this file can reach the primary key: the loser resolves the winner's row and
# returns before it ever attempts an insert. The two guards mask each other that
# way, and a mutation on the derived id is caught by nothing above (measured: swap
# it for ``ObjectId()`` and all the race tests stay green).
#
# The guarantee belongs to the MINT, so it is asked of the mint. That is also the
# honest level for it: what holds between processes is "a second insert for this
# pocket loses, before the debit", and the mint is the thing that inserts.
# --------------------------------------------------------------------------- #


async def _mint_direct(workspace_id: str, **overrides: Any) -> Site:
    """Call the primitive, bypassing the bind layer and therefore the mutex."""
    kwargs: dict[str, Any] = dict(
        workspace_id=workspace_id,
        pocket_id=_POCKET,
        owner=_OWNER,
        allowed_origins=[f"https://{_HOST}"],
        name="Brew Co Concierge",
    )
    kwargs.update(overrides)
    with _owned_pocket(workspace_id):
        return await sites_service.mint_foreign_site(**kwargs)


async def test_a_second_mint_for_the_same_pocket_loses_before_it_can_charge(store):  # noqa: ARG001
    """THE CROSS-PROCESS GUARD. A duplicate insert must fail, and fail unpaid.

    The foreign row is inserted at a DERIVED id, so a second mint for the same
    (workspace, pocket) collides on ``_id`` — which MongoDB enforces natively on
    every deployment, unlike a partial index mongomock cannot represent. The mint
    inserts BEFORE it debits, so the loser never reaches the charge: that ordering
    is what turns the primary key into a billing guarantee rather than just a
    uniqueness one.

    Asserted on the WALLET as well as the row count, because a version that
    charged first and inserted second would also raise here, having already taken
    the money.
    """
    ws = "ws-mint-twice-pk"
    await _fund(ws)
    await _verify_origin(ws)

    first = await _mint_direct(ws)
    balance_after_first = await credits_service.balance(ws)
    assert balance_after_first == _FUNDED - _PRICE_CREDITS

    with pytest.raises(DuplicateKeyError):
        await _mint_direct(ws)

    rows = await _foreign_rows(ws)
    assert len(rows) == 1, "the duplicate must not have become a second concierge"
    assert str(rows[0].id) == str(first.id)
    assert await credits_service.balance(ws) == balance_after_first, (
        "the loser charged the wallet - the insert is not guarding the debit"
    )


def test_the_foreign_id_never_lands_on_the_published_id() -> None:
    """The salt, which is the reason this derivation is allowed to exist at all.

    A pocket can carry a published Worker site AND a foreign concierge. The
    published row's id is ``sha1("<ws>:<pocket>")[:12]``; if the foreign
    derivation used the same preimage, minting a concierge would collide with —
    and on an upsert path could overwrite — a live site's row. The prefix is what
    keeps the two id spaces apart, and it is asserted rather than assumed because
    "simplify the hash input" is an inviting-looking edit.
    """
    for ws, pocket in (("ws-1", "pk-1"), ("acme", "shop"), ("", "")):
        published = sites_service._live_object_id(ws, pocket)
        foreign = sites_service._foreign_object_id(ws, pocket)
        assert published != foreign, f"collision for ({ws!r}, {pocket!r})"

    # And it is a pure function of the pair, or a retry would mint a second row.
    assert sites_service._foreign_object_id("ws-1", "pk-1") == sites_service._foreign_object_id(
        "ws-1", "pk-1"
    )
    assert sites_service._foreign_object_id("ws-1", "pk-1") != sites_service._foreign_object_id(
        "ws-2", "pk-1"
    )


# --------------------------------------------------------------------------- #
# 1b. The cross-process recovery branch.
#
# With the mutex in place the ``except DuplicateKeyError`` path is unreachable
# in-process, so no race test can reach it. It is the path a SECOND PROCESS takes,
# and it moves money by deciding whether to re-mint, so it is driven directly
# through the mint seam here rather than left as the only untested branch.
# --------------------------------------------------------------------------- #


async def test_a_cross_process_loser_adopts_the_winner_without_charging(store):  # noqa: ARG001
    """Another process won the insert. Adopt its row; do not charge.

    The mint inserts before it debits, so a primary-key conflict means the loser
    never reached a charge. Re-minting or raising would both be wrong: the pocket
    HAS a concierge, and the caller asked for one to exist.
    """
    ws = "ws-bind-adopt"
    await _fund(ws)
    await _verify_origin(ws)

    winner = await _bind(ws)  # stands in for the other process's row
    balance_after_winner = await credits_service.balance(ws)

    # Force the conflict the way a second process would see it: the pre-mint
    # existence check misses (as it would, mid-flight), the insert loses, and the
    # recovery re-read then finds the winner. Built OUTSIDE the patch so it closes
    # over the real resolver rather than over itself.
    resolver = _conflict_then_real()
    boom = AsyncMock(side_effect=DuplicateKeyError("duplicate key"))
    with patch.object(sites_service, "mint_foreign_site", new=boom):
        with patch.object(sites_service, "foreign_site_for_pocket", new=resolver):
            adopted = await _bind(ws)

    assert boom.await_count == 1, "the loser must NOT re-mint when the winner's row stands"

    assert str(adopted.id) == str(winner.id), "the loser must adopt the winner's row"
    assert await credits_service.balance(ws) == balance_after_winner, "and must not charge"
    assert len(await _foreign_rows(ws)) == 1


async def test_a_cross_process_loser_mints_again_when_the_winner_rolled_back(store):  # noqa: ARG001
    """The winner's charge failed and it deleted its row between our conflict and
    our re-read. Nobody has a concierge and nobody paid, so the buy is still owed.

    Returning ``None`` or re-raising here would leave the caller with no concierge
    and no error they could act on, for a purchase they asked for and can afford.
    """
    ws = "ws-bind-rollback"
    await _fund(ws)
    await _verify_origin(ws)

    real_mint = sites_service.mint_foreign_site
    calls: list[int] = []

    async def _conflict_once(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise DuplicateKeyError("duplicate key")
        return await real_mint(**kwargs)

    with patch.object(sites_service, "mint_foreign_site", new=_conflict_once):
        site = await _bind(ws)

    assert len(calls) == 2, "the rolled-back winner must be followed by exactly one re-mint"
    assert site.subscription_status == "active"
    assert len(await _foreign_rows(ws)) == 1
    assert await credits_service.balance(ws) == _FUNDED - _PRICE_CREDITS, "charged once"


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


async def test_another_tenants_readable_pocket_is_refused_through_the_bind_too(store):  # noqa: ARG001
    """The tenancy half, inherited the same way the access half is.

    ``pockets_service.get`` denies only a PRIVATE pocket, so a default-visibility
    pocket in another workspace comes back readable — the mock below is that
    function behaving normally, not a weakened one. The bind must still refuse,
    because binding a foreign concierge is what turns a pocket into a PUBLIC,
    anonymous read surface.

    Driven through the bind rather than the mint because the bind is what a route
    calls: a gate that only the primitive enforces is a gate the product does not
    have.
    """
    ws = "ws-bind-tenant-a"
    await _fund(ws)
    await _verify_origin(ws)

    with patch(
        "pocketpaw_ee.cloud.pockets.service.get",
        new=AsyncMock(return_value=_owned_pocket_doc("ws-bind-tenant-b")),
    ):
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
        workspace_id=ws, pocket_id=_POCKET, agent_id=replacement, caller_is_admin=True
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
            workspace_id=ws, pocket_id=_POCKET, agent_id=victim_agent, caller_is_admin=True
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
    await sites_service.rebind_foreign_concierge(
        workspace_id=ws, pocket_id=_POCKET, agent_id=other, caller_is_admin=True
    )
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


# --------------------------------------------------------------------------- #
# The rebind's two remaining gates: whose BAR, and whose AGENT
#
# rebind takes two caller-supplied ids and neither used to be checked against
# the site it is rebinding. ``widget_id`` is looked up workspace-scoped, so it
# could name a colleague's bar; ``agent_id`` only had to be READABLE, and
# workspace visibility makes every agent in the tenant readable by every member.
# Both matter because a foreign concierge answers ANONYMOUS visitors on a public
# page — it is a publishing surface, not a setting.
# --------------------------------------------------------------------------- #


async def test_a_rebind_cannot_repoint_another_pockets_bar(store):
    """``widget_id`` must name THIS site's bar.

    The damage is the re-provision arm, which takes no ``agent_id`` and was
    therefore gated by nothing: it clears the named widget's agent and binds it
    to this site's ``concierge-<site_id>``. So a rebind naming a colleague's bar
    would silently repoint their published site at your concierge, grounded in
    your pocket, with nothing in either page saying so.

    Asserted on the victim's widget, not the response: the call returns an agent
    id either way.
    """
    ws = "ws-bind-widget-mismatch"
    await _fund(ws, _PRICE_CREDITS * 10)
    await _verify_origin(ws)
    await _verify_origin(ws, host=_OTHER_HOST)

    mine = await _bind(ws)
    assert mine is not None
    theirs = await _bind(ws, pocket_id="pk-colleague", allowed_origins=[f"https://{_OTHER_HOST}"])
    assert theirs is not None

    their_bars = await store.list_widgets(pocket_id="pk-colleague", workspace_id=ws, limit=1)
    assert their_bars, "the colleague's bar must exist, or this test proves nothing"
    their_bar = their_bars[0]
    their_agent_before = their_bar.agent_id
    assert their_agent_before

    with pytest.raises(Forbidden) as exc:
        await sites_service.rebind_foreign_concierge(
            workspace_id=ws, pocket_id=_POCKET, widget_id=their_bar.id
        )

    assert exc.value.code == "sites.widget_pocket_mismatch"
    after = await store.get_widget(their_bar.id, workspace_id=ws)
    assert after is not None
    assert after.agent_id == their_agent_before, "the colleague's bar must be untouched"


async def test_a_member_cannot_publish_an_unrelated_agents_knowledge_to_visitors(store):
    """The rebind refuses an agent that does not already answer for a foreign
    concierge here.

    ``rebind_site_agent``'s own gate asks whether the caller may READ the agent,
    and every ``workspace``-visibility agent in the tenant is readable by every
    member. That is the wrong question: a concierge run resolves
    ``agent:<agent_id>``, so whatever is named here answers anonymous visitors on
    a public page — an internal HR or finance agent included.

    The widget is asserted, not just the code: a refusal that had already written
    would answer the same way.
    """
    ws = "ws-bind-agent-unpublished"
    await _fund(ws)
    await _verify_origin(ws)
    await _bind(ws)

    bars = await store.list_widgets(pocket_id=_POCKET, workspace_id=ws, limit=1)
    before = bars[0].agent_id
    internal = await _make_agent(ws, "internal-hr-assistant")

    with pytest.raises(Forbidden) as exc:
        await sites_service.rebind_foreign_concierge(
            workspace_id=ws, pocket_id=_POCKET, agent_id=internal
        )

    assert exc.value.code == "sites.agent_not_published"
    after = await store.list_widgets(pocket_id=_POCKET, workspace_id=ws, limit=1)
    assert after[0].agent_id == before, "a refused rebind must not have written"


async def test_the_refusal_defaults_to_on_when_the_caller_is_not_described(store):  # noqa: ARG001
    """``caller_is_admin`` defaults to False, so a caller that forgets to pass it
    gets the stricter path. A permission flag that fails open is not a permission
    flag."""
    import inspect

    sig = inspect.signature(sites_service.rebind_foreign_concierge)
    assert sig.parameters["caller_is_admin"].default is False


async def test_an_admin_may_point_a_concierge_at_any_agent_they_can_read(store):
    """The escape hatch, and the legitimate case the refusal must not break: an
    admin hand-builds a better concierge and points the bar at it."""
    ws = "ws-bind-agent-admin"
    await _fund(ws)
    await _verify_origin(ws)
    await _bind(ws)

    hand_built = await _make_agent(ws, "hand-built-front-desk")

    bound = await sites_service.rebind_foreign_concierge(
        workspace_id=ws, pocket_id=_POCKET, agent_id=hand_built, caller_is_admin=True
    )

    assert bound == hand_built
    after = await store.list_widgets(pocket_id=_POCKET, workspace_id=ws, limit=1)
    assert after[0].agent_id == hand_built


async def test_a_member_may_point_one_concierge_at_another_concierges_agent(store):
    """The test is the BINDING, not the slug.

    An agent already fronting a foreign concierge in this workspace has been
    published deliberately once, so re-pointing a second concierge at it changes
    nothing about who can reach it — and refusing that would make the rule about
    provisioning history rather than exposure.
    """
    ws = "ws-bind-agent-shared"
    await _fund(ws, _PRICE_CREDITS * 10)
    await _verify_origin(ws)
    await _verify_origin(ws, host=_OTHER_HOST)

    await _bind(ws)
    await _bind(ws, pocket_id="pk-sibling", allowed_origins=[f"https://{_OTHER_HOST}"])

    sibling_bars = await store.list_widgets(pocket_id="pk-sibling", workspace_id=ws, limit=1)
    published_agent = sibling_bars[0].agent_id
    assert published_agent

    bound = await sites_service.rebind_foreign_concierge(
        workspace_id=ws, pocket_id=_POCKET, agent_id=published_agent
    )

    assert bound == published_agent
    after = await store.list_widgets(pocket_id=_POCKET, workspace_id=ws, limit=1)
    assert after[0].agent_id == published_agent


async def test_an_agent_whose_bar_is_not_a_foreign_concierge_does_not_count_as_published(store):
    """The rule asks for a FOREIGN CONCIERGE, not merely for a bar.

    Every pocket with a paw-bar widget has an agent bound to it, and most of
    those bars are not public concierges at all — a dashboard widget mints one.
    So "this agent fronts a widget" is a far weaker statement than "this agent
    has been published to anonymous visitors", and keying the rule on the widget
    would let a member publish an agent that only ever answered inside the
    product.

    The state below is a concierge that was cancelled: the Site row is gone and
    the bar and its agent survive. That is the honest way to reach "a widget
    exists, its foreign site does not" without hand-building a widget this suite
    otherwise never constructs.
    """
    ws = "ws-bind-agent-barless"
    await _fund(ws, _PRICE_CREDITS * 10)
    await _verify_origin(ws)
    await _verify_origin(ws, host=_OTHER_HOST)

    await _bind(ws)
    cancelled = await _bind(
        ws, pocket_id="pk-cancelled", allowed_origins=[f"https://{_OTHER_HOST}"]
    )

    orphan_bars = await store.list_widgets(pocket_id="pk-cancelled", workspace_id=ws, limit=1)
    orphan_agent = orphan_bars[0].agent_id
    assert orphan_agent

    await cancelled.delete()
    assert await sites_service.foreign_site_for_pocket(ws, "pk-cancelled") is None, (
        "the fixture must leave a bar whose foreign site is gone, or this proves nothing"
    )

    bars = await store.list_widgets(pocket_id=_POCKET, workspace_id=ws, limit=1)
    before = bars[0].agent_id

    with pytest.raises(Forbidden) as exc:
        await sites_service.rebind_foreign_concierge(
            workspace_id=ws, pocket_id=_POCKET, agent_id=orphan_agent
        )

    assert exc.value.code == "sites.agent_not_published"
    after = await store.list_widgets(pocket_id=_POCKET, workspace_id=ws, limit=1)
    assert after[0].agent_id == before
