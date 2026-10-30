# tests/cloud/leads/test_webhook_destinations_vault.py
# Lead webhook destinations whose URLs live in the integrations vault
# (``IntegrationConnection`` rows of the internal ``lead_webhook`` provider), over
# mongomock + the real outbox and an httpx MockTransport:
#   * with CLOUD_ENCRYPTION_KEY set, a new destination keeps no URL ciphertext of
#     its own: it points at a workspace vault row; delivery still works
#   * the vault row is workspace-scoped, never listed as a connected account, and a
#     destination pointing at another workspace's row delivers nothing
#   * a URL change rewrites the same vault row; removing a destination revokes it
#   * the one-time migration moves legacy (flat, encrypted, plaintext) URLs into the
#     vault, is idempotent (a re-run and a crash-resume make no second row) and
#     never logs a URL; without the key it changes nothing

from __future__ import annotations

import json
import logging

import pytest
from cryptography.fernet import Fernet
from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.leads import notification_settings as ns
from pocketpaw_ee.cloud.leads import webhook_destinations as wd
from pocketpaw_ee.cloud.leads import webhook_vault
from pocketpaw_ee.cloud.models.integration import IntegrationConnection
from pocketpaw_ee.cloud.models.lead_notifications import SiteNotificationSettings
from pocketpaw_ee.cloud.notifications import outbox

from tests.cloud.leads.test_notification_settings import (  # noqa: F401 - fixtures
    _capture,
    _hooks,
    _lead,
    _site,
    _tenant,
    email_on,
    net,
    public_dns,
)

pytestmark = pytest.mark.usefixtures("mongo_db", "public_dns", "email_on")

SLACK = "https://hooks.slack.com/services/T1/B1/xyz"
SLACK_2 = "https://hooks.slack.com/services/T1/B1/new"
DISCORD = "https://discord.com/api/webhooks/1/abc"
JSON_HOOK = "https://hooks.zapier.com/hooks/catch/1/abc/"


@pytest.fixture
def sent(net):  # noqa: F811 - the imported fixture
    """The requests the MockTransport saw (the shared ``net`` fixture)."""
    return net


@pytest.fixture
def vault_key(monkeypatch):
    monkeypatch.setenv("CLOUD_ENCRYPTION_KEY", Fernet.generate_key().decode())


async def _raw(site) -> dict:
    return await SiteNotificationSettings.get_pymongo_collection().find_one(
        {"workspace": site.workspace, "site_id": str(site.id)}
    )


async def _conns() -> list[dict]:
    return await IntegrationConnection.get_pymongo_collection().find({}).to_list(None)


async def test_a_new_destination_keeps_its_url_in_the_vault(sent, vault_key) -> None:
    ws, _ = await _tenant()
    site = await _site(ws)
    sid = str(site.id)
    await ns.update_settings(ws, sid, events={"lead_captured": ["webhook"]})
    added = await wd.add_webhook(ws, sid, url=SLACK, label="Sales")

    hook = (await _raw(site))["webhooks"][0]
    assert hook["url"] == "" and hook["url_enc"] == ""
    assert hook["connection_id"]
    assert hook["url_fp"] == wd.fingerprint(SLACK) and hook["url_hint"] == wd.url_hint(SLACK)
    assert SLACK not in json.dumps(await _raw(site), default=str)

    rows = await _conns()
    assert len(rows) == 1
    row = rows[0]
    assert str(row["_id"]) == hook["connection_id"]
    assert row["provider"] == webhook_vault.PROVIDER_ID
    assert row["workspace"] == ws
    assert row["account_ref"] == f"{sid}:{added['webhook']['id']}"
    assert row["token_ciphertext"] and SLACK not in json.dumps(row, default=str)

    # The wire is unchanged: hint and platform, never the URL.
    assert added["webhook"]["platform"] == "slack"
    assert added["webhook"]["detected_platform"] == "slack"
    assert SLACK not in json.dumps(added, default=str)

    await _capture(ws, site, await _lead(ws, site, email="v@x.com"))
    await outbox.process_due()
    assert len(_hooks(sent, SLACK)) == 1


async def test_vault_rows_are_not_connected_accounts(vault_key) -> None:
    from pocketpaw_ee.integrations import vault

    ws, _ = await _tenant()
    site = await _site(ws)
    await wd.add_webhook(ws, str(site.id), url=SLACK)
    assert await vault.list_connections(ws) == []
    # The integrations routes (resume, revoke, site link) can't reach the row.
    row = (await _conns())[0]
    with pytest.raises(NotFound):
        await vault.get_connection(ws, str(row["_id"]))


async def test_a_destination_never_reads_another_workspaces_vault_row(sent, vault_key) -> None:
    ws, _ = await _tenant()
    other_ws, _ = await _tenant("other@acme.test")
    site = await _site(ws)
    other_site = await _site(other_ws)
    sid = str(site.id)
    await wd.add_webhook(ws, sid, url=SLACK)
    await wd.add_webhook(other_ws, str(other_site.id), url=DISCORD)
    foreign = next(r for r in await _conns() if r["workspace"] == other_ws)

    # Point this site's destination at the other workspace's vault row.
    await SiteNotificationSettings.get_pymongo_collection().update_one(
        {"workspace": ws, "site_id": sid},
        {"$set": {"webhooks.0.connection_id": str(foreign["_id"])}},
    )
    dest = (await ns.settings_for(site)).webhooks[0]
    assert await wd.target(ws, sid, dest.id) is None


async def test_a_url_change_rewrites_the_same_vault_row_and_remove_revokes_it(
    sent, vault_key
) -> None:
    ws, _ = await _tenant()
    site = await _site(ws)
    sid = str(site.id)
    added = await wd.add_webhook(ws, sid, url=SLACK)
    wid = added["webhook"]["id"]
    first = (await _raw(site))["webhooks"][0]["connection_id"]

    await wd.update_webhook(ws, sid, wid, url=SLACK_2)
    hook = (await _raw(site))["webhooks"][0]
    assert hook["connection_id"] == first and hook["url_enc"] == ""
    assert hook["url_fp"] == wd.fingerprint(SLACK_2)
    target = await wd.target(ws, sid, wid)
    assert target is not None and target.url == SLACK_2
    assert len(await _conns()) == 1

    await wd.remove_webhook(ws, sid, wid)
    rows = await _conns()
    assert len(rows) == 1 and rows[0]["status"] == "revoked"
    assert rows[0]["token_ciphertext"] == ""


async def test_without_the_vault_key_destinations_keep_their_own_ciphertext() -> None:
    ws, _ = await _tenant()
    site = await _site(ws)
    await wd.add_webhook(ws, str(site.id), url=SLACK)
    hook = (await _raw(site))["webhooks"][0]
    assert hook["url_enc"] and not hook.get("connection_id")
    assert await _conns() == []


async def _legacy_rows(ws: str, site_a, site_b) -> None:
    """Site A: a flat pre-destinations webhook. Site B: one encrypted destination
    and one plaintext destination, the way older builds stored them."""
    from pocketpaw_ee.cloud.auth.sso import crypto

    coll = SiteNotificationSettings.get_pymongo_collection()
    await coll.insert_one(
        {
            "workspace": ws,
            "site_id": str(site_a.id),
            "include_owner": True,
            "emails": [],
            "webhooks": [],
            "webhook_url": JSON_HOOK,
            "webhook_secret_enc": crypto.encrypt("old-secret"),
            "webhook_secret_prev_enc": "",
            "webhook_failure_count": 0,
            "webhook_disabled_at": None,
            "events": {"lead_captured": ["webhook"]},
        }
    )
    await coll.insert_one(
        {
            "workspace": ws,
            "site_id": str(site_b.id),
            "include_owner": True,
            "emails": [],
            "webhooks": [
                {
                    "id": "enc1",
                    **{k: v for k, v in wd.sealed(SLACK).items()},
                    "secret_enc": crypto.encrypt("s1"),
                    "events": ["lead_captured"],
                },
                {
                    "id": "plain1",
                    "url": DISCORD,
                    "secret_enc": crypto.encrypt("s2"),
                    "events": ["lead_captured"],
                },
            ],
            "events": {"lead_captured": ["webhook"]},
        }
    )


async def test_migration_moves_every_stored_url_into_the_vault_once(
    sent, vault_key, caplog
) -> None:
    ws, _ = await _tenant()
    site_a = await _site(ws, "A")
    site_b = await _site(ws, "B")
    await _legacy_rows(ws, site_a, site_b)

    caplog.set_level(logging.DEBUG)
    first = await webhook_vault.migrate_all()
    assert first["moved"] == 3 and first["failed"] == 0

    for site in (site_a, site_b):
        raw = await _raw(site)
        assert raw.get("webhook_url") in (None, "")
        for hook in raw["webhooks"]:
            assert hook["connection_id"] and hook["url_enc"] == "" and hook["url"] == ""
        blob = json.dumps(raw, default=str)
        assert SLACK not in blob and DISCORD not in blob and JSON_HOOK not in blob
    assert len(await _conns()) == 3

    # Delivery reads through the vault.
    await _capture(ws, site_b, await _lead(ws, site_b, email="v@x.com"))
    await outbox.process_due()
    assert len(_hooks(sent, SLACK)) == 1 and len(_hooks(sent, DISCORD)) == 1

    # A second run is a no-op.
    second = await webhook_vault.migrate_all()
    assert second["moved"] == 0
    assert len(await _conns()) == 3

    logged = "\n".join(r.getMessage() for r in caplog.records)
    for url in (SLACK, DISCORD, JSON_HOOK):
        assert url not in logged


async def test_migration_resumes_after_a_crash_without_a_second_vault_row(vault_key) -> None:
    ws, _ = await _tenant()
    site_a = await _site(ws, "A")
    site_b = await _site(ws, "B")
    await _legacy_rows(ws, site_a, site_b)

    # A run that stored the vault row and died before pointing the destination at it.
    await webhook_vault.store_url(ws, str(site_b.id), "enc1", SLACK)
    assert len(await _conns()) == 1

    result = await webhook_vault.migrate_all()
    assert result["moved"] == 3
    rows = await _conns()
    assert len(rows) == 3
    refs = sorted(r["account_ref"] for r in rows)
    assert refs == sorted([f"{site_a.id}:legacy", f"{site_b.id}:enc1", f"{site_b.id}:plain1"])


async def test_migration_without_the_key_changes_nothing() -> None:
    ws, _ = await _tenant()
    site_a = await _site(ws, "A")
    site_b = await _site(ws, "B")
    await _legacy_rows(ws, site_a, site_b)
    before = await _raw(site_b)

    result = await webhook_vault.migrate_all()
    assert result["moved"] == 0 and result["skipped"] == "vault_key_missing"
    assert await _raw(site_b) == before
    assert await _conns() == []
