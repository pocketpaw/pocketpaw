# ee/pocketpaw_ee/cloud/partners/service.py — Paw Partners tenant service.
#
# Created 2026-10-01 (feat/partners-foundation, PH-1). Reads the caller's own
# partner profile and does tenant-scoped CRUD on clients. Client routes require
# an ACTIVE partner profile (``Forbidden`` otherwise).
# Updated 2026-10-02 (feat/partners-sell, PH-2): ``list_offers`` (partner-only
# plans at the caller's country price), ``sell`` (validates the client, then
# ``sites.service.sell_site_plan`` — the ordinary paid-publish path — debits the
# wallet, redeploys and stamps ``partner_client_id``) and ``list_sites``.
# Updated 2026-10-02 (feat/partners-earnings, PH-11): ``sell`` optionally records
# what the partner charged its client as a PAID receipt on the site's existing
# client record (``sites.service.record_site_invoice`` — no second writer), only
# when the sale really happened. ``summary`` and ``earnings`` read the sold sites'
# paid receipts (per currency, never FX-mixed) and their ``site_plan`` debits
# (through ``credits.service.history``) for the partner earnings view.
# Review fixes: the receipt is booked only against a debit the sale newly
# recorded (``sell_site_plan`` returns its key), with an id derived from that key
# so a double submit books one receipt; a failed receipt write leaves the sale
# standing with ``invoice_id=None``; ``summary`` reads the sold sites once.
# Updated 2026-10-02 (feat/partners-commissions, PH-13): ``create_pay_link`` —
# a one-time checkout the partner's client pays for a site's year at list price
# (``site_plans.partner_client_price``), through ``billing.service.
# create_partner_client_checkout``; the pending record lands on the Site via the
# sites owner (``client_pay_link_target`` refuses 404/409 and hands back an open
# link to reuse). ``list_offers`` adds the client price; ``list_sites`` adds
# ``billing_mode``; ``summary`` / ``earnings`` add commission credits read from
# the ledger (``partner_commission`` grants plus their clawbacks, net).
# Review fix: the pay link reserves its slot through
# ``sites.reserve_client_pay_link`` (a conditional push), so a double submit opens
# one Dodo payment; a failed checkout releases the reservation.
# Re-check fix: a reservation that went stale while Dodo answered is released
# and refused (409 ``partners.link_in_progress``), never handed out.
# Updated 2026-10-02: ``_default_store`` delegates to the shared
# ``pocketpaw.fabric.default_journal_store()`` (same as ``people.service``)
# instead of building its own ``FabricJournalStore``, so client writes are
# also projected into the per-workspace FabricStore read model.
# Updated 2026-10-02: PATCH re-reads via the scoped ``_load``; empty PATCH is a
# no-op; the operator switch moved to ``cloud/platform/partners.py``.
# Updated 2026-10-01: clients are Fabric ``Customer`` objects in the org
# journal (``FabricJournalStore``), scope ``workspace:<id>`` = tenancy, copying
# ``people/service.py``. The journal's ``fabric.object.*`` events ARE the
# emit-on-write, so no cloud realtime event is fired. Delete = Fabric archive.
# ``get_active_profile`` / ``get_client`` are consumed by later PH tasks by name.
# ``partner_profile_for_workspace`` is the billing seam's loader
# (``billing.enforcement.sites_enforced_for``).

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from beanie import PydanticObjectId
from soul_protocol.spec.journal import Actor

from pocketpaw.fabric.journal_store import FabricJournalStore
from pocketpaw.fabric.models import FabricObject, FabricQuery
from pocketpaw_ee.cloud._core.context import RequestContext
from pocketpaw_ee.cloud._core.errors import ConflictError, Forbidden, NotFound, ValidationError
from pocketpaw_ee.cloud.billing import site_plans
from pocketpaw_ee.cloud.models.workspace import PartnerProfile
from pocketpaw_ee.cloud.models.workspace import Workspace as _WorkspaceDoc
from pocketpaw_ee.cloud.partners.domain import (
    CUSTOMER_TYPE_ID,
    CUSTOMER_TYPE_NAME,
    SOURCE_PAW_PARTNERS,
    PartnerClient,
)
from pocketpaw_ee.cloud.partners.dto import (
    PartnerClientCreateRequest,
    PartnerClientOut,
    PartnerClientUpdateRequest,
    PartnerEarningsMonthOut,
    PartnerMoneyOut,
    PartnerOfferOut,
    PartnerPayLinkOut,
    PartnerPayLinkRequest,
    PartnerProfileOut,
    PartnerSaleOut,
    PartnerSellRequest,
    PartnerSiteOut,
    PartnerSummaryOut,
)

logger = logging.getLogger(__name__)

# Mirrors ``billing.service.CLIENT_BILLING_RAIL`` (the client paid the year).
_CLIENT_RAIL = "client"


def _default_store() -> FabricJournalStore:
    """The shared process-wide journal store (same as ``people.service``). Tests pass ``store=``."""
    from pocketpaw.fabric import default_journal_store

    return default_journal_store()


def _scope(workspace_id: str) -> list[str]:
    return [f"workspace:{workspace_id}"]


def _actor(ctx: RequestContext, scope: list[str]) -> Actor:
    return Actor(kind="user", id=f"user:{ctx.user_id}", scope_context=list(scope))


def _oid(value: str | None) -> PydanticObjectId | None:
    try:
        return PydanticObjectId(value)
    except Exception:
        return None


async def partner_profile_for_workspace(workspace_id: str | None) -> PartnerProfile | None:
    """The partner profile of ``workspace_id``, or None (not a partner / no such id)."""
    oid = _oid(workspace_id)
    if oid is None:
        return None
    # global-read: billing seams and the platform route ask about a workspace by id;
    # tenant callers reach this only through ``ctx.workspace_id``.
    ws = await _WorkspaceDoc.find_one({"_id": oid, "deleted_at": None})
    return ws.partner if ws is not None else None


async def get_active_profile(ctx: RequestContext) -> PartnerProfile | None:
    """The caller's workspace partner profile when it is ``active``, else None."""
    profile = await partner_profile_for_workspace(ctx.workspace_id)
    return profile if profile is not None and profile.status == "active" else None


async def get_profile(ctx: RequestContext) -> PartnerProfileOut:
    profile = await partner_profile_for_workspace(ctx.workspace_id)
    if profile is None:
        raise NotFound("partner_profile", ctx.workspace_id or "")
    return PartnerProfileOut.model_validate(profile, from_attributes=True)


async def _active_profile(ctx: RequestContext) -> PartnerProfile:
    profile = await get_active_profile(ctx)
    if profile is None:
        raise Forbidden("partner.not_active", "This workspace is not an active partner")
    return profile


async def _require_active(ctx: RequestContext) -> str:
    await _active_profile(ctx)
    return ctx.workspace_id  # type: ignore[return-value]  # active ⇒ workspace resolved


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _client_from_object(obj: FabricObject, *, workspace_id: str) -> PartnerClient:
    p = obj.properties
    return PartnerClient(
        id=obj.id,
        workspace_id=workspace_id,
        name=str(p.get("name", "")),
        whatsapp=str(p.get("whatsapp", "")),
        whatsapp_opt_in_at=p.get("whatsapp_opt_in_at") or None,
        gstin=p.get("gstin") or None,
        notes=str(p.get("notes", "")),
        created_at=obj.created_at,
        updated_at=obj.updated_at,
        source=str(p.get("source", SOURCE_PAW_PARTNERS)),
    )


def _to_out(client: PartnerClient) -> PartnerClientOut:
    return PartnerClientOut(
        id=client.id,
        workspace_id=client.workspace_id,
        name=client.name,
        whatsapp=client.whatsapp,
        whatsapp_opt_in_at=client.whatsapp_opt_in_at,
        gstin=client.gstin,
        notes=client.notes,
        created_at=client.created_at,
        updated_at=client.updated_at,
    )


def _ours(obj: FabricObject | None) -> bool:
    return (
        obj is not None
        and obj.type_id == CUSTOMER_TYPE_ID
        and obj.source_connector == SOURCE_PAW_PARTNERS
    )


async def _load(fabric: FabricJournalStore, workspace_id: str, client_id: str) -> FabricObject:
    # Scope-filtered: another workspace's id is indistinguishable from not-found.
    # Typed query (like people.get_person) narrows to customer objects, but it still
    # scans the in-memory org projection — the same ceiling as ``list_clients``'s
    # ponytail note.
    result = await fabric.query(
        FabricQuery(type_id=CUSTOMER_TYPE_ID, limit=10_000),
        requester_scopes=_scope(workspace_id),
    )
    obj = next((o for o in result.objects if o.id == client_id), None)
    if not _ours(obj):
        raise NotFound("partner_client", client_id)
    return obj  # type: ignore[return-value]


async def list_clients(
    ctx: RequestContext, *, store: FabricJournalStore | None = None
) -> list[PartnerClientOut]:
    workspace_id = await _require_active(ctx)
    fabric = store or _default_store()
    # ponytail: in-memory projection scan, unpaginated; add a cursor near ~1k clients.
    result = await fabric.query(
        FabricQuery(type_id=CUSTOMER_TYPE_ID, limit=10_000),
        requester_scopes=_scope(workspace_id),
    )
    objs = sorted((o for o in result.objects if _ours(o)), key=lambda o: o.created_at, reverse=True)
    return [_to_out(_client_from_object(o, workspace_id=workspace_id)) for o in objs]


async def get_client(
    ctx: RequestContext, *, client_id: str, store: FabricJournalStore | None = None
) -> PartnerClientOut:
    workspace_id = await _require_active(ctx)
    obj = await _load(store or _default_store(), workspace_id, client_id)
    return _to_out(_client_from_object(obj, workspace_id=workspace_id))


async def create_client(
    ctx: RequestContext, *, body: Any, store: FabricJournalStore | None = None
) -> PartnerClientOut:
    body = PartnerClientCreateRequest.model_validate(body)
    workspace_id = await _require_active(ctx)
    fabric = store or _default_store()
    scope = _scope(workspace_id)
    client_id = f"customer-{uuid4().hex}"  # not deterministic: two clients may share a name
    props = PartnerClient(
        id=client_id,
        workspace_id=workspace_id,
        name=body.name,
        whatsapp=body.whatsapp,
        whatsapp_opt_in_at=_iso(body.whatsapp_opt_in_at),
        gstin=body.gstin,
        notes=body.notes,
        created_at=None,
        updated_at=None,
    ).to_properties()
    obj = await fabric.create(
        FabricObject(
            id=client_id,
            type_id=CUSTOMER_TYPE_ID,
            type_name=CUSTOMER_TYPE_NAME,
            properties=props,
            source_connector=SOURCE_PAW_PARTNERS,
            source_id=workspace_id,
        ),
        scope=scope,
        actor=_actor(ctx, scope),
    )
    # no-event: the journal's fabric.object.created event is the emit-on-write.
    return _to_out(_client_from_object(obj, workspace_id=workspace_id))


async def update_client(
    ctx: RequestContext, *, client_id: str, body: Any, store: FabricJournalStore | None = None
) -> PartnerClientOut:
    body = PartnerClientUpdateRequest.model_validate(body)
    workspace_id = await _require_active(ctx)
    fabric = store or _default_store()
    current = await _load(fabric, workspace_id, client_id)
    changes: dict[str, Any] = {}
    for field, value in body.model_dump(exclude_unset=True).items():
        if field in ("name", "whatsapp", "notes") and value is None:
            continue  # required on the record; null means "leave it"
        changes[field] = _iso(value) if field == "whatsapp_opt_in_at" else value
    if not changes:
        # no-event: empty PATCH writes nothing.
        return _to_out(_client_from_object(current, workspace_id=workspace_id))
    scope = _scope(workspace_id)
    # The return is ignored (as in people/service.py): it is an unscoped, capped
    # lookup. Re-read through the scoped, typed ``_load`` instead.
    await fabric.update(client_id, changes, scope=scope, actor=_actor(ctx, scope))
    # no-event: the journal's fabric.object.updated event is the emit-on-write.
    return _to_out(
        _client_from_object(await _load(fabric, workspace_id, client_id), workspace_id=workspace_id)
    )


async def delete_client(
    ctx: RequestContext, *, client_id: str, store: FabricJournalStore | None = None
) -> None:
    workspace_id = await _require_active(ctx)
    fabric = store or _default_store()
    await _load(fabric, workspace_id, client_id)
    scope = _scope(workspace_id)
    await fabric.archive(
        client_id, scope=scope, reason="deleted by partner", actor=_actor(ctx, scope)
    )
    # no-event: the journal's fabric.object.archived event is the emit-on-write.


# ---------------------------------------------------------------- selling (PH-2)


async def list_offers(ctx: RequestContext) -> list[PartnerOfferOut]:
    profile = await _active_profile(ctx)
    offers = []
    for tier in site_plans.list_partner_plans():
        currency, amount = site_plans.partner_client_price(tier.key, profile.billing_country)
        offers.append(
            PartnerOfferOut(
                sku=tier.key,
                period_months=tier.period_months,
                price_credits=site_plans.partner_price_usd(tier.key, profile.billing_country) * 100,
                conversation_allowance=tier.conversation_allowance,
                label=tier.display_name,
                client_price_minor=amount,
                client_currency=currency,
            )
        )
    return offers


async def create_pay_link(
    ctx: RequestContext,
    *,
    body: Any,
    store: FabricJournalStore | None = None,
    provider: Any = None,
) -> PartnerPayLinkOut:
    """A one-time link the partner's client pays for ``site_id``'s year (PH-13).

    Priced at the LIST yearly price in the partner's billing country. Nothing
    moves here: the year and the partner's commission land when the verified
    payment matches the pending record this writes. An open link for the same plan
    and price is handed back rather than minted twice; one for another plan is a
    409. The slot is reserved before Dodo is called, so a double submit is one
    payment.
    """
    body = PartnerPayLinkRequest.model_validate(body)
    profile = await _active_profile(ctx)
    workspace_id: str = ctx.workspace_id  # type: ignore[assignment]  # active => resolved
    # Scoped read: another workspace's client is a 404 here.
    await get_client(ctx, client_id=body.client_id, store=store)
    currency, amount = site_plans.partner_client_price(body.sku, profile.billing_country)

    from pocketpaw_ee.cloud.billing import service as billing_service
    from pocketpaw_ee.sites import service as sites_service

    doc, reusable, token = await sites_service.reserve_client_pay_link(
        workspace_id=workspace_id,
        site_id=body.site_id,
        sku=body.sku,
        currency=currency,
        amount_minor=amount,
        client_id=body.client_id,
    )
    if reusable is not None:
        checkout_url = reusable.checkout_url
    else:
        try:
            checkout = await billing_service.create_partner_client_checkout(
                workspace_id=workspace_id,
                user_id=ctx.user_id,
                site_id=str(doc.id),
                amount_minor=amount,
                currency=currency,
                provider=provider,
            )
        except BaseException:
            await sites_service.release_client_pay_link(site_id=str(doc.id), token=token)
            raise
        filled = await sites_service.fill_client_pay_link(
            site_id=str(doc.id),
            token=token,
            payment_id=checkout.gateway_ref,
            checkout_url=checkout.checkout_url,
        )
        if not filled:
            # The reservation went stale while Dodo answered; another request may
            # hold a link now, so this one is never handed out.
            await sites_service.release_client_pay_link(site_id=str(doc.id), token=token)
            raise ConflictError(
                "partners.link_in_progress", "A pay link for this site is being created; retry."
            )
        checkout_url = checkout.checkout_url
    # no-event: a pending link changes nothing anyone sees until it is paid.
    return PartnerPayLinkOut(
        checkout_url=checkout_url,
        site_id=str(doc.id),
        sku=body.sku,
        amount_minor=amount,
        currency=currency,
    )


async def sell(
    ctx: RequestContext, *, body: Any, store: FabricJournalStore | None = None
) -> PartnerSaleOut:
    body = PartnerSellRequest.model_validate(body)
    profile = await _active_profile(ctx)
    workspace_id: str = ctx.workspace_id  # type: ignore[assignment]  # active ⇒ resolved
    tier = next((t for t in site_plans.list_partner_plans() if t.key == body.sku), None)
    if tier is None:
        raise ValidationError("partners.unknown_sku", f"'{body.sku}' is not a partner plan")
    # Scoped read: another workspace's client is a 404 here.
    await get_client(ctx, client_id=body.client_id, store=store)

    from pocketpaw_ee.sites import service as sites_service
    from pocketpaw_ee.sites.dto import SiteInvoiceCreate

    doc, debit_key = await sites_service.sell_site_plan(
        workspace_id=workspace_id,
        user_id=ctx.user_id,
        site_id=body.site_id,
        tier_key=body.sku,
        partner_client_id=body.client_id,
    )
    invoice_id: str | None = None
    # Only a debit THIS call recorded gets a receipt: a refused sale raised above,
    # and a re-sell / zero-delta return / resume / replay moved no money. The id is
    # derived from the debit key, so a double submit lands one receipt.
    if debit_key is not None and body.price_minor is not None:
        rid = f"inv_{hashlib.sha256(debit_key.encode()).hexdigest()[:16]}"
        try:
            await sites_service.record_site_invoice(
                workspace_id=workspace_id,
                site_id=str(doc.id),
                body=SiteInvoiceCreate(
                    amount_cents=body.price_minor,
                    currency=body.currency or ("INR" if profile.billing_country == "IN" else "USD"),
                    paid=True,
                    note=f"Paw Partners sale · {tier.display_name}",
                ),
                minor_units=True,
                invoice_id=rid,
            )
            invoice_id = rid
        except Exception as exc:
            # The sale (debit + deploy) already stands; the receipt is bookkeeping.
            # The partner can add it through POST /sites/{site_id}/invoices.
            # Ids only: the price is the partner's private figure.
            logger.warning(
                "partners.sell: receipt write failed site=%s err=%s", doc.id, type(exc).__name__
            )
    # no-event: the sale runs the publish path, which emits SitePublished on deploy;
    # the receipt is the owner's own bookkeeping (see record_site_invoice).
    return PartnerSaleOut(
        site_id=str(doc.id),
        name=doc.name,
        url=doc.url,
        plan_tier=doc.plan_tier,
        renewal_date=doc.renewal_date,
        partner_client_id=body.client_id,
        subscription_status=doc.subscription_status,
        invoice_id=invoice_id,
    )


async def list_sites(
    ctx: RequestContext,
    *,
    due_within_days: int | None = None,
    store: FabricJournalStore | None = None,
) -> list[PartnerSiteOut]:
    workspace_id = await _require_active(ctx)
    from pocketpaw_ee.sites import service as sites_service

    docs = await sites_service.list_partner_sites(workspace_id, due_within_days=due_within_days)
    names = {c.id: c.name for c in await list_clients(ctx, store=store)} if docs else {}
    return [
        PartnerSiteOut(
            site_id=str(d.id),
            name=d.name,
            url=d.url,
            plan_tier=d.plan_tier,
            renewal_date=d.renewal_date,
            partner_client_id=d.partner_client_id,
            # An archived client keeps its sold sites; the name is just gone.
            client_name=names.get(d.partner_client_id or "", ""),
            billing_mode="client" if d.billing_rail == _CLIENT_RAIL else "partner",
        )
        for d in docs
    ]


# ---------------------------------------------------------------- earnings (PH-11)


def _aware(value: datetime) -> datetime:
    # Mongo hands datetimes back naive; every stored timestamp here is UTC.
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _month(value: datetime) -> str:
    value = _aware(value)
    return f"{value.year:04d}-{value.month:02d}"


def _money(invoices: Iterable[Any]) -> list[PartnerMoneyOut]:
    """Sum receipts per currency. Never FX-mixed: one row per currency."""
    totals: dict[str, int] = defaultdict(int)
    for inv in invoices:
        totals[inv.currency] += inv.amount_cents
    return [PartnerMoneyOut(currency=c, amount_minor=a) for c, a in sorted(totals.items())]


def _paid_receipts(docs: list[Any]) -> list[Any]:
    """The PAID client receipts on the sold sites (the partner's revenue)."""
    return [inv for d in docs for inv in d.client_invoices if inv.paid]


async def _site_plan_debits(workspace_id: str, site_ids: set[str]) -> list[Any]:
    """Applied ``site_plan`` debits for ``site_ids`` (purchases, renewals, changes)."""
    from pocketpaw_ee.cloud.billing.service import SITE_PLAN_DEBIT_CAUSE
    from pocketpaw_ee.cloud.credits import service as credits_service

    out: list[Any] = []
    cursor: str | None = None
    # ponytail: ``history`` pages the workspace ledger by ``_id`` with a ``cause``
    # filter, and there is no (workspace, cause) index, so every call scans the
    # workspace's WHOLE ledger (compute spend included), not just site_plan rows.
    # Add that index plus a since-bounded read when a busy partner's ledger makes
    # this slow.
    while True:
        page, cursor = await credits_service.history(
            workspace_id, limit=200, cursor=cursor, cause=SITE_PLAN_DEBIT_CAUSE
        )
        out += [
            e
            for e in page
            # A phantom (applied False) never moved the wallet.
            if e.applied and e.amount_delta_micro < 0 and e.ref.get("site_id") in site_ids
        ]
        if cursor is None:
            return out


async def _commission_entries(workspace_id: str) -> list[Any]:
    """Applied commission grants and their clawbacks (negative), for the ledger net."""
    from pocketpaw_ee.cloud.billing.service import (
        PARTNER_COMMISSION_CAUSE,
        PARTNER_COMMISSION_REVERSAL_CAUSE,
    )
    from pocketpaw_ee.cloud.credits import service as credits_service

    out: list[Any] = []
    for cause in (PARTNER_COMMISSION_CAUSE, PARTNER_COMMISSION_REVERSAL_CAUSE):
        cursor: str | None = None
        # ponytail: same unindexed (workspace, cause) ledger scan as _site_plan_debits.
        while True:
            page, cursor = await credits_service.history(
                workspace_id, limit=200, cursor=cursor, cause=cause
            )
            out += [e for e in page if e.applied]
            if cursor is None:
                break
    return out


async def summary(
    ctx: RequestContext, *, store: FabricJournalStore | None = None
) -> PartnerSummaryOut:
    workspace_id = await _require_active(ctx)
    from pocketpaw_ee.sites import service as sites_service

    docs = await sites_service.list_partner_sites(workspace_id)
    debits = await _site_plan_debits(workspace_id, {str(d.id) for d in docs})
    receipts = _paid_receipts(docs)
    commissions = await _commission_entries(workspace_id)
    now = datetime.now(UTC)
    since, due_by = now - timedelta(days=30), now + timedelta(days=30)
    return PartnerSummaryOut(
        clients=len(await list_clients(ctx, store=store)),
        sites_sold=len(docs),
        active_sites=sum(1 for d in docs if d.subscription_status == "active"),
        # Same rule as list_partner_sites(due_within_days=30): a null date is never due.
        renewals_due_30d=sum(
            1 for d in docs if d.renewal_date is not None and _aware(d.renewal_date) <= due_by
        ),
        spent_credits_30d=sum(-e.amount_delta for e in debits if _aware(e.created_at) >= since),
        spent_credits_total=sum(-e.amount_delta for e in debits),
        revenue_30d=_money(r for r in receipts if _aware(r.issued_at) >= since),
        revenue_total=_money(receipts),
        commission_credits_30d=sum(
            e.amount_delta for e in commissions if _aware(e.created_at) >= since
        ),
        commission_credits_total=sum(e.amount_delta for e in commissions),
    )


async def earnings(ctx: RequestContext, *, months: int = 12) -> list[PartnerEarningsMonthOut]:
    """One row per UTC calendar month, newest first, empty months included."""
    if not 1 <= months <= 24:
        raise ValidationError("partners.invalid_months", "months must be between 1 and 24")
    workspace_id = await _require_active(ctx)
    from pocketpaw_ee.sites import service as sites_service

    now = datetime.now(UTC)
    keys = [
        f"{y:04d}-{m + 1:02d}"
        for y, m in (divmod(now.year * 12 + now.month - 1 - i, 12) for i in range(months))
    ]
    docs = await sites_service.list_partner_sites(workspace_id)
    debits = await _site_plan_debits(workspace_id, {str(d.id) for d in docs})
    receipts = _paid_receipts(docs)
    commissions = await _commission_entries(workspace_id)
    rows = []
    for key in keys:
        month_debits = [e for e in debits if _month(e.created_at) == key]
        rows.append(
            PartnerEarningsMonthOut(
                month=key,
                sales=len(month_debits),
                revenue=_money(r for r in receipts if _month(r.issued_at) == key),
                spent_credits=sum(-e.amount_delta for e in month_debits),
                commission_credits=sum(
                    e.amount_delta for e in commissions if _month(e.created_at) == key
                ),
            )
        )
    return rows
