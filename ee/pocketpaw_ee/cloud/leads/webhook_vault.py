# ee/pocketpaw_ee/cloud/leads/webhook_vault.py
# Lead webhook destination URLs in the integrations vault.
#
# A destination's URL lives in an ``IntegrationConnection`` row of the internal
# ``lead_webhook`` provider (workspace = the site's, ``account_ref`` =
# ``"<site_id>:<webhook_id>"``, bundle ``{"url": ...}``); the destination in
# ``site_notification_settings`` keeps only ``connection_id`` plus the non-secret
# ``url_fp`` / ``url_hint`` / ``detected_platform``. ``webhook_destinations`` calls
# ``store_url`` / ``read_url`` / ``revoke``; nothing else reads a vault URL.
#
# The vault needs ``CLOUD_ENCRYPTION_KEY``. Without it (``enabled()`` False)
# destinations keep their own Fernet ``url_enc`` exactly as before, so a deployment
# without the key loses nothing.
#
# MIGRATION. ``migrate_site`` moves one site's stored URLs (the flat pre-list
# webhook, encrypted ``url_enc`` rows and old plaintext ``url`` rows) into the
# vault; ``webhook_destinations._prepare`` runs it before every write, and
# ``migrate_all`` (``scripts/migrations/2026_10_23_lead_webhooks_to_vault.py``)
# runs it over every site once. Idempotent: ``store_url`` upserts on the ref, and
# the pointer update is conditional on the values it read, so a re-run, a
# concurrent write or a crash between the two steps never makes a second row or
# loses a URL. A vault row the pointer update lost to a concurrent write is
# revoked again.
#
# INVARIANTS A READER MUST NOT BREAK:
#   * No URL in a log line, an exception message or a return value other than
#     ``read_url``. Logs carry counts and ids only.
#   * ``read_url`` checks the row is this workspace's, this provider's and this
#     destination's own (``account_ref``), so a destination can't borrow another
#     site's or workspace's URL by pointing at its row.

from __future__ import annotations

import logging
from typing import Any

from pocketpaw_ee.cloud._core import crypto as vault_crypto
from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.integrations.providers.lead_webhook import PROVIDER_ID

logger = logging.getLogger(__name__)


def enabled() -> bool:
    return vault_crypto.is_configured()


def account_ref(site_id: str, webhook_id: str) -> str:
    return f"{site_id}:{webhook_id}"


async def store_url(workspace_id: str, site_id: str, webhook_id: str, url: str) -> str:
    """Put ``url`` in the vault for this destination; returns the connection id."""
    from pocketpaw_ee.cloud.leads.webhook_destinations import url_hint
    from pocketpaw_ee.integrations import vault

    conn = await vault.store_secret(
        workspace_id=workspace_id,
        provider=PROVIDER_ID,
        account_ref=account_ref(site_id, webhook_id),
        account_label=url_hint(url),
        secret={"url": url},
    )
    return str(conn.id)


async def _own_row(workspace_id: str, connection_id: str, site_id: str, webhook_id: str):
    from pocketpaw_ee.integrations import vault

    if not connection_id:
        return None
    try:
        conn = await vault.get_connection(workspace_id, connection_id, internal=True)
    except NotFound:
        return None
    if conn.provider != PROVIDER_ID or conn.account_ref != account_ref(site_id, webhook_id):
        return None
    return conn


async def read_url(workspace_id: str, connection_id: str, site_id: str, webhook_id: str) -> str:
    """The destination's URL from its own vault row, else ""."""
    from pocketpaw_ee.integrations import vault

    conn = await _own_row(workspace_id, connection_id, site_id, webhook_id)
    return vault.read_secret(conn).get("url", "") if conn is not None else ""


async def revoke(workspace_id: str, connection_id: str, site_id: str, webhook_id: str) -> None:
    from pocketpaw_ee.integrations import vault

    conn = await _own_row(workspace_id, connection_id, site_id, webhook_id)
    if conn is not None and conn.status != "revoked":
        await vault.mark_revoked(conn)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def _stored_url(hook: dict[str, Any]) -> str:
    """The URL a pre-vault destination row holds (ciphertext or old plaintext)."""
    if hook.get("url_enc"):
        from pocketpaw_ee.cloud.auth.sso import crypto

        try:
            return crypto.decrypt(hook["url_enc"])
        except Exception:  # noqa: BLE001 - a key change: nothing to move
            return ""
    return str(hook.get("url") or "")


async def migrate_site(key: dict[str, str]) -> tuple[int, int]:
    """Move one site's stored destination URLs into the vault. Returns (moved,
    failed); a failed one keeps its old storage and is retried next time."""
    from pocketpaw_ee.cloud.leads import notification_settings as ns
    from pocketpaw_ee.cloud.leads import webhook_destinations as wd
    from pocketpaw_ee.cloud.notifications import webhook_platforms

    if not enabled():
        return 0, 0
    await wd.migrate_legacy(key)
    coll = ns._coll()
    raw = await coll.find_one(key, {"webhooks": 1}) or {}
    moved = failed = 0
    for hook in raw.get("webhooks") or []:
        if hook.get("connection_id") or not (hook.get("url_enc") or hook.get("url")):
            continue
        webhook_id = str(hook.get("id") or "")
        url = _stored_url(hook)
        if not webhook_id or not url:
            failed += 1
            continue
        conn_id = await store_url(key["workspace"], key["site_id"], webhook_id, url)
        result = await coll.update_one(
            {
                **key,
                "webhooks": {
                    # Pinned to what was read (None matches a missing field).
                    "$elemMatch": {
                        "id": webhook_id,
                        "url_enc": hook.get("url_enc"),
                        "url": hook.get("url"),
                    }
                },
            },
            {
                "$set": {
                    "webhooks.$.connection_id": conn_id,
                    "webhooks.$.url": "",
                    "webhooks.$.url_enc": "",
                    "webhooks.$.url_fp": wd.fingerprint(url),
                    "webhooks.$.url_hint": wd.url_hint(url),
                    "webhooks.$.detected_platform": webhook_platforms.detect_platform(url),
                }
            },
        )
        if getattr(result, "modified_count", 0) == 1:
            moved += 1
            continue
        # A concurrent write changed or removed this destination: drop our row
        # unless the destination now points at it.
        fresh = await coll.find_one(key, {"webhooks": 1}) or {}
        current = next((h for h in fresh.get("webhooks") or [] if h.get("id") == webhook_id), None)
        if current is None or current.get("connection_id") != conn_id:
            await revoke(key["workspace"], conn_id, key["site_id"], webhook_id)
        failed += 1
    return moved, failed


async def migrate_all() -> dict[str, Any]:
    """Run ``migrate_site`` over every site holding a URL outside the vault."""
    from pocketpaw_ee.cloud.leads import notification_settings as ns

    if not enabled():
        logger.warning("lead webhooks: CLOUD_ENCRYPTION_KEY is unset, nothing moved to the vault")
        return {"moved": 0, "failed": 0, "sites": 0, "skipped": "vault_key_missing"}
    query = {
        "$or": [
            {"webhook_url": {"$nin": [None, ""]}},
            {"webhooks": {"$elemMatch": {"url_enc": {"$nin": [None, ""]}}}},
            {"webhooks": {"$elemMatch": {"url": {"$nin": [None, ""]}}}},
        ]
    }
    keys = [
        {"workspace": row["workspace"], "site_id": row["site_id"]}
        async for row in ns._coll().find(query, {"workspace": 1, "site_id": 1})
    ]
    moved = failed = 0
    for key in keys:
        try:
            m, f = await migrate_site(key)
        except Exception as exc:  # noqa: BLE001 - one bad site must not stop the run
            logger.warning(
                "lead webhooks: site %s failed to migrate (%s)", key["site_id"], type(exc).__name__
            )
            failed += 1
            continue
        moved += m
        failed += f
    logger.info(
        "lead webhooks: moved %d destination(s) into the vault over %d site(s), %d failed",
        moved,
        len(keys),
        failed,
    )
    return {"moved": moved, "failed": failed, "sites": len(keys), "skipped": ""}


__all__ = [
    "PROVIDER_ID",
    "account_ref",
    "enabled",
    "migrate_all",
    "migrate_site",
    "read_url",
    "revoke",
    "store_url",
]
