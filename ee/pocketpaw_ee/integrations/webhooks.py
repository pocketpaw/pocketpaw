# ee/pocketpaw_ee/integrations/webhooks.py: provider webhook ingress and site orders.
#
#   POST /integrations/webhooks/{provider}
#     1. the provider verifies the signature -> NormalizedEvent, or 401
#     2. route it to a site: by ``site_id`` when the event carries one, else by
#        ``account_ref`` through the provider's connections and their bindings.
#        A site that is not bound to the provider gets nothing.
#     3. dedupe: insert into ``IntegrationWebhookEvent`` (unique provider + event
#        id). A repeat answers 200 ``{ok, duplicate: true}`` and does nothing else,
#        so provider retries are safe.
#     4. ``order.*`` events upsert a ``SiteOrder`` (unique provider + external id)
#     5. emit ``integrations.event`` on the in-process bus with identifiers only
#
#   list_orders: GET /sites/by-pocket/{pocket_id}/orders, newest first, cursor = the
#   last row's id. Amounts are ISO 4217 minor units.

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
_CURRENCY = re.compile(r"^[A-Z]{3}$")
MAX_ORDERS_PAGE = 100


def _err(code: str, status: int, message: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": {"code": code, "message": message}}, status)


async def _site_for(event: NormalizedEvent):
    """(site or None, binding) for an event, or (None, None) when it maps to no site."""
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection as Conn
    from pocketpaw_ee.cloud.models.site import Site
    from pocketpaw_ee.integrations.gateway import _site_by_id

    if event.site_id:
        site = await _site_by_id(event.site_id)
        if site is None:
            return None, None
        binding = await bindings.binding_for_site(site, event.provider)
        return (site, binding) if binding is not None else (None, None)
    if event.account_ref:
        conns = await Conn.find(
            Conn.provider == event.provider,
            Conn.account_ref == event.account_ref,
            Conn.status != "revoked",
        ).to_list()
        for conn in conns:
            for binding in await bindings.bindings_for_connection(str(conn.id)):
                site = await Site.find_one(
                    {"workspace": binding.workspace, "pocket_id": binding.pocket_id}
                )
                return site, binding
    return None, None


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

    site, binding = await _site_for(event)
    try:
        await IntegrationWebhookEvent(
            provider=provider.id,
            event_id=event.id,
            event_type=event.type,
            workspace=binding.workspace if binding else "",
            pocket_id=binding.pocket_id if binding else "",
        ).insert()
    except DuplicateKeyError:
        return JSONResponse({"ok": True, "duplicate": True})

    if binding is None:
        logger.info("integrations: %s event %s maps to no site", provider_id, event.type)
        return JSONResponse({"ok": True, "routed": False})

    if event.type in _ORDER_STATUS:
        await _upsert_order(event, binding, site)
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
    from pocketpaw_ee.cloud.models.integration import SiteOrder

    data = event.data
    external_id = str(data.get("order_id") or data.get("external_id") or event.id)[:200]
    status = _ORDER_STATUS[event.type]
    row = await SiteOrder.find_one(
        SiteOrder.provider == event.provider, SiteOrder.external_id == external_id
    )
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
    # A late "pending" never undoes a settled order.
    if not (status == "pending" and row.status != "pending"):
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


def _order_view(row: Any) -> OrderView:
    return OrderView(
        id=str(row.id),
        provider=row.provider,
        external_id=row.external_id,
        status=row.status if row.status in ("pending", "paid", "refunded", "failed") else "pending",
        amount_minor=row.amount_minor,
        currency=row.currency,
        customer_email=row.customer_email,
        items=[i.model_dump() for i in row.items],
        created_at=vault._utc(row.createdAt),
    )


async def list_orders(
    *, workspace_id: str, user_id: str, pocket_id: str, limit: int, cursor: str | None
) -> OrdersPage:
    from beanie import PydanticObjectId

    from pocketpaw_ee.cloud._core.errors import ValidationError
    from pocketpaw_ee.cloud.models.integration import SiteOrder

    await bindings.require_edit(workspace_id, user_id, pocket_id)
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
        items=[_order_view(r) for r in rows],
        next_cursor=str(rows[-1].id) if more and rows else None,
    )
