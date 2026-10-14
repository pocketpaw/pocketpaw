# ee/pocketpaw_ee/integrations/webhooks.py: provider webhook ingress and site orders.
#
#   POST /integrations/webhooks/{provider}
#     1. the provider verifies the signature -> NormalizedEvent, or 401. A verified
#        event of a type we don't use comes back as ``ignored`` and is acked as is
#     2. an event id already in ``IntegrationWebhookEvent`` answers 200 duplicate
#        (an account-level event the provider's ``handle_event`` takes, such as an
#        app uninstall or a privacy request, is handled here, recorded, and acked;
#        a failure answers 500 and records nothing, so the provider retries)
#     3. route to ONE site (see ``_site_for``): a named site must be bound through a
#        connection with the event's ``account_ref``; the account_ref-only fallback
#        refuses ambiguity. Unroutable events are acked 200 and not recorded.
#     4. ``order.*`` events upsert a ``SiteOrder`` (unique provider + external id).
#        A failure answers 500 and records nothing, so the provider retries. With
#        ``data.only_if_exists`` an event updates an existing order and never makes
#        one (a failed card attempt is not an order); a miss is acked and recorded,
#        except for a REFUND, which answers 500 unrecorded so the provider redelivers
#        it until the paid event that makes the order has landed. Status moves by
#        ``_NEXT_STATUS``: refunded is terminal, only a refund replaces paid.
#     5. only then the event id is recorded (unique index), and
#        ``integrations.event`` goes on the in-process bus with identifiers only
#
#   ``connection.updated`` is account-level, not site-level: it skips step 3, merges
#   ``data.meta`` into every live connection with that provider + account_ref, and
#   tells each bound site its recomputed status (``site.integration_changed``).
#   ``data.as_of`` (event time) is kept as ``meta.as_of``; an older event is skipped.
#
#   list_orders: GET /sites/by-pocket/{pocket_id}/orders, newest first, cursor = the
#   last row's id. Amounts are ISO 4217 minor units. Customer emails are masked
#   unless the caller owns the pocket or is a workspace admin.

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from pocketpaw_ee.integrations import bindings, vault
from pocketpaw_ee.integrations.dto import OrdersPage, OrderView
from pocketpaw_ee.integrations.providers import get_provider
from pocketpaw_ee.integrations.providers.base import NormalizedEvent

logger = logging.getLogger(__name__)

MAX_WEBHOOK_BYTES = 256 * 1024
BUS_EVENT = "integrations.event"
_ORDER_STATUS = {
    "order.created": "pending",
    "order.pending": "pending",
    "order.paid": "paid",
    "order.refunded": "refunded",
    "order.failed": "failed",
}
# Which status may replace which. Refunded is terminal; only a refund replaces
# paid; failed never replaces paid; pending never replaces a settled order.
_NEXT_STATUS: dict[str, frozenset[str]] = {
    "pending": frozenset({"pending", "paid", "failed", "refunded"}),
    "failed": frozenset({"failed", "paid", "refunded"}),
    "paid": frozenset({"paid", "refunded"}),
    "refunded": frozenset({"refunded"}),
}
_CURRENCY = re.compile(r"^[A-Z]{3}$")
MAX_ORDERS_PAGE = 100
IGNORED = "ignored"
CONNECTION_UPDATED = "connection.updated"
_META_VALUE = (str, int, float, bool, type(None))


class _OrderNotYet(Exception):
    """An update-only event arrived before the order it updates."""


def _err(code: str, status: int, message: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": {"code": code, "message": message}}, status)


async def _site_for(event: NormalizedEvent):
    """(site, binding) for an event, or (None, None) when it maps to no single site.

    Tenant safety: an event naming a ``site_id`` is only routed when that site's
    binding uses a connection whose ``account_ref`` equals the event's, so a signed
    event for account A cannot write orders into a site connected to account B. With
    no ``site_id``, an event naming an existing order (``data.order_id``) goes to that
    order's site under the same account check (a refund carries no site metadata).
    The ``account_ref``-only fallback routes only when exactly one binding matches
    across every workspace; an ambiguous match is refused."""
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection as Conn
    from pocketpaw_ee.cloud.models.integration import SiteOrder
    from pocketpaw_ee.cloud.models.site import Site
    from pocketpaw_ee.integrations.gateway import _site_by_id

    if not event.account_ref:
        return None, None
    if event.site_id:
        return await _bound_to_account(await _site_by_id(event.site_id), event)
    order_id = str(event.data.get("order_id") or "")[:200]
    if order_id:
        row = await SiteOrder.find_one(
            SiteOrder.provider == event.provider, SiteOrder.external_id == order_id
        )
        if row is not None:
            site = await Site.find_one({"workspace": row.workspace, "pocket_id": row.pocket_id})
            return await _bound_to_account(site, event)
    conns = await Conn.find(
        Conn.provider == event.provider,
        Conn.account_ref == event.account_ref,
        Conn.status != "revoked",
    ).to_list()
    matches = [b for conn in conns for b in await bindings.bindings_for_connection(str(conn.id))]
    if len(matches) != 1:
        if matches:
            logger.warning(
                "integrations: %s event %s matches %d sites; refused as ambiguous",
                event.provider,
                event.id,
                len(matches),
            )
        return None, None
    binding = matches[0]
    site = await Site.find_one({"workspace": binding.workspace, "pocket_id": binding.pocket_id})
    return site, binding


async def _bound_to_account(site: Any, event: NormalizedEvent):
    """(site, binding) when ``site`` is bound to the event's account, else Nones."""
    if site is None:
        return None, None
    binding = await bindings.binding_for_site(site, event.provider)
    conn = await bindings.connection_for(binding) if binding is not None else None
    if conn is None or conn.status == "revoked" or conn.account_ref != event.account_ref:
        logger.warning(
            "integrations: %s event %s names a site not bound to its account; refused",
            event.provider,
            event.id,
        )
        return None, None
    return site, binding


async def ingest(provider_id: str, request: Request) -> JSONResponse:
    from pymongo.errors import DuplicateKeyError

    from pocketpaw_ee.cloud.models.integration import IntegrationWebhookEvent

    provider = get_provider(provider_id)
    if provider is None:
        return _err("not_found", 404, "Unknown provider.")
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BYTES:
        return _err("invalid_args", 413, "Payload too large.")
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        event = provider.verify_webhook(body, headers)
    except Exception:  # noqa: BLE001 - a verifier crash is a refusal, not a 500
        logger.warning("integrations: %s webhook verifier raised", provider_id, exc_info=True)
        event = None
    if event is None:
        return _err("unauthorized", 401, "Signature check failed.")
    if event.type == IGNORED:
        return JSONResponse({"ok": True, "ignored": True})

    already = await IntegrationWebhookEvent.find_one(
        IntegrationWebhookEvent.provider == provider.id,
        IntegrationWebhookEvent.event_id == event.id,
    )
    if already is not None:
        return JSONResponse({"ok": True, "duplicate": True})

    if event.type == CONNECTION_UPDATED:
        return await _connection_updated(event)

    handle = getattr(provider, "handle_event", None)
    if handle is not None:
        try:
            handled = await handle(event)
        except Exception:  # noqa: BLE001 - unrecorded, so the provider retries
            logger.exception("integrations: %s event %s failed to handle", provider_id, event.id)
            return _err("provider_error", 500, "Could not process the event. Retry later.")
        if handled:
            try:
                await IntegrationWebhookEvent(
                    provider=provider.id, event_id=event.id, event_type=event.type
                ).insert()
            except DuplicateKeyError:
                return JSONResponse({"ok": True, "duplicate": True})
            return JSONResponse({"ok": True})

    site, binding = await _site_for(event)
    if binding is None:
        # Acked so the provider stops, but NOT recorded: if the site is bound later,
        # a redelivery is processed instead of being dropped as a duplicate.
        logger.info("integrations: %s event %s maps to no site", provider_id, event.type)
        return JSONResponse({"ok": True, "routed": False})

    try:
        if event.type in _ORDER_STATUS:
            await _upsert_order(event, binding, site)
    except _OrderNotYet:
        logger.info("integrations: %s refund %s before its order; retry", provider_id, event.id)
        return _err("provider_error", 500, "The order is not recorded yet. Retry later.")
    except Exception:  # noqa: BLE001 - unrecorded, so the provider's retry reprocesses
        logger.exception("integrations: %s event %s failed to process", provider_id, event.id)
        return _err("provider_error", 500, "Could not process the event. Retry later.")

    # Recorded only after the work is done. A concurrent duplicate that got here too
    # is harmless: the order upsert is keyed by (provider, external_id).
    try:
        await IntegrationWebhookEvent(
            provider=provider.id,
            event_id=event.id,
            event_type=event.type,
            workspace=binding.workspace,
            pocket_id=binding.pocket_id,
        ).insert()
    except DuplicateKeyError:
        return JSONResponse({"ok": True, "duplicate": True})
    await _emit(event, binding, site)
    return JSONResponse({"ok": True})


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _items(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:100]:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "ref": str(item.get("ref") or "")[:200],
                "name": str(item.get("name") or "")[:200],
                "qty": max(1, _int(item.get("qty") or 1)),
                "amount_minor": _int(item.get("amount_minor")),
            }
        )
    return out


async def _upsert_order(event: NormalizedEvent, binding: Any, site: Any) -> None:
    from pymongo.errors import DuplicateKeyError

    try:
        await _apply_order(event, binding, site)
    except DuplicateKeyError:
        # A concurrent delivery inserted the row first; apply onto it.
        await _apply_order(event, binding, site)


async def _apply_order(event: NormalizedEvent, binding: Any, site: Any) -> None:
    from pocketpaw_ee.cloud.models.integration import SiteOrder

    data = event.data
    external_id = str(data.get("order_id") or data.get("external_id") or event.id)[:200]
    status = _ORDER_STATUS[event.type]
    row = await SiteOrder.find_one(
        SiteOrder.provider == event.provider, SiteOrder.external_id == external_id
    )
    if row is None and data.get("only_if_exists"):
        if event.type == "order.refunded":
            # Refund before paid (out-of-order delivery): unrecorded, so it's retried.
            raise _OrderNotYet(external_id)
        return
    if row is None:
        row = SiteOrder(
            workspace=binding.workspace,
            pocket_id=binding.pocket_id,
            site_id=str(site.id) if site is not None else "",
            provider=event.provider,
            external_id=external_id,
        )
    elif row.workspace != binding.workspace or row.pocket_id != binding.pocket_id:
        logger.warning("integrations: order %s belongs to another site, ignored", row.id)
        return
    # Out-of-order deliveries: see ``_NEXT_STATUS``. Data still merges either way.
    if status in _NEXT_STATUS.get(row.status, frozenset(_NEXT_STATUS)):
        row.status = status
    if "amount_minor" in data:
        row.amount_minor = _int(data.get("amount_minor"))
    currency = str(data.get("currency") or "").upper()
    if _CURRENCY.match(currency):
        row.currency = currency
    email = data.get("customer_email")
    if isinstance(email, str) and email:
        row.customer_email = email[:254]
    items = _items(data.get("items"))
    if items:
        row.items = items
    await row.save()


async def _connection_updated(event: NormalizedEvent) -> JSONResponse:
    """Merge the provider's non-secret account state into its connections."""
    from pymongo.errors import DuplicateKeyError

    from pocketpaw_ee.cloud._core.errors import NotFound
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection as Conn
    from pocketpaw_ee.cloud.models.integration import IntegrationWebhookEvent

    if not event.account_ref:
        return JSONResponse({"ok": True, "routed": False})
    raw = event.data.get("meta")
    meta = {
        str(k)[:64]: v
        for k, v in (raw.items() if isinstance(raw, dict) else ())
        if isinstance(v, _META_VALUE)
    }
    as_of = event.data.get("as_of")
    as_of = as_of if isinstance(as_of, int) and not isinstance(as_of, bool) else 0
    try:
        conns = await Conn.find(
            Conn.provider == event.provider,
            Conn.account_ref == event.account_ref,
            Conn.status != "revoked",
        ).to_list()
        found = len(conns)
        conns = [c for c in conns if _int((c.meta or {}).get("as_of")) <= as_of]
        for conn in conns:
            conn.meta = {**(conn.meta or {}), **meta, "as_of": as_of}
            await conn.save()
    except Exception:  # noqa: BLE001 - unrecorded, so the provider's retry reprocesses
        logger.exception("integrations: %s event %s failed to process", event.provider, event.id)
        return _err("provider_error", 500, "Could not process the event. Retry later.")
    if not conns:
        # Every match already holds newer state: an out-of-order redelivery.
        return JSONResponse({"ok": True, "stale": True} if found else {"ok": True, "routed": False})
    try:
        await IntegrationWebhookEvent(
            provider=event.provider,
            event_id=event.id,
            event_type=event.type,
            workspace=conns[0].workspace,
        ).insert()
    except DuplicateKeyError:
        return JSONResponse({"ok": True, "duplicate": True})
    provider = get_provider(event.provider)
    for conn in conns:
        for row in await bindings.bindings_for_connection(str(conn.id)):
            try:
                pocket = await bindings.pocket_in_workspace(row.workspace, row.pocket_id)
            except NotFound:
                continue
            status = bindings.binding_status(row, provider, conn) if provider else "connected"
            await bindings.emit_changed(pocket, row.provider, status, "")
    return JSONResponse({"ok": True})


async def _emit(event: NormalizedEvent, binding: Any, site: Any) -> None:
    try:
        from pocketpaw_ee.cloud.shared.events import event_bus

        await event_bus.emit(
            BUS_EVENT,
            {
                "type": event.type,
                "provider": event.provider,
                "event_id": event.id,
                "workspace_id": binding.workspace,
                "pocket_id": binding.pocket_id,
                "site_id": str(site.id) if site is not None else "",
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug("integrations: bus emit failed", exc_info=True)


# ------------------------------------------------------------------- orders


def mask_email(email: str | None) -> str | None:
    """``jane@x.io`` -> ``j***@x.io``. Editors see this; owners and admins the full one."""
    if not email:
        return email
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def _order_view(row: Any, *, full_email: bool) -> OrderView:
    return OrderView(
        id=str(row.id),
        provider=row.provider,
        external_id=row.external_id,
        status=row.status if row.status in ("pending", "paid", "refunded", "failed") else "pending",
        amount_minor=row.amount_minor,
        currency=row.currency,
        customer_email=row.customer_email if full_email else mask_email(row.customer_email),
        items=[i.model_dump() for i in row.items],
        created_at=vault._utc(row.createdAt),
    )


async def list_orders(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    limit: int,
    cursor: str | None,
    is_admin: bool = False,
) -> OrdersPage:
    from beanie import PydanticObjectId

    from pocketpaw_ee.cloud._core.errors import ValidationError
    from pocketpaw_ee.cloud.models.integration import SiteOrder

    pocket = await bindings.require_edit(workspace_id, user_id, pocket_id)
    full_email = is_admin or pocket.owner == user_id
    limit = max(1, min(int(limit or 50), MAX_ORDERS_PAGE))
    query: dict[str, Any] = {"workspace": workspace_id, "pocket_id": pocket_id}
    if cursor:
        try:
            query["_id"] = {"$lt": PydanticObjectId(cursor)}
        except Exception as exc:  # noqa: BLE001
            raise ValidationError("integrations.bad_cursor", "That cursor is not valid.") from exc
    rows = await SiteOrder.find(query).sort("-_id").limit(limit + 1).to_list()
    more = len(rows) > limit
    rows = rows[:limit]
    return OrdersPage(
        items=[_order_view(r, full_email=full_email) for r in rows],
        next_cursor=str(rows[-1].id) if more and rows else None,
    )
