# tests/cloud/growth/test_delete_prospects.py — HTTP-layer tests for prospect
# deletion: POST /growth/prospects/bulk-delete and DELETE
# /growth/prospects/{prospect_id}. Covers the cascade to drafts, the survival of
# the message-log audit rows, tenancy (another workspace's ids are skipped, never
# deleted), the id-list bounds, and the best-effort withdrawal of pending
# Instinct proposals for proposed drafts. Reuses the test_router.py app harness.

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from beanie import PydanticObjectId
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud.growth import service as growth_service
from pocketpaw_ee.cloud.growth.propose import GROWTH_SEND_PARAM_KEY

from tests.cloud.growth.test_router import _build_app, _payload

PROSPECTS_URL = "/api/v1/growth/prospects"
BULK_DELETE_URL = "/api/v1/growth/prospects/bulk-delete"


@pytest_asyncio.fixture
async def w1_client(mongo_db: Any) -> AsyncClient:
    app = _build_app(workspace_id="w1")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


@pytest_asyncio.fixture
async def w2_client(mongo_db: Any) -> AsyncClient:
    app = _build_app(workspace_id="w2")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


class _FakeInstinctStore:
    """Stands in for the per-workspace InstinctStore: serves a fixed pending
    list and records every rejection."""

    def __init__(self, actions: list[Any], *, raises: Exception | None = None) -> None:
        self._actions = actions
        self._raises = raises
        self.list_calls: list[dict[str, Any]] = []
        self.rejected: list[tuple[str, str, str]] = []

    async def list_actions(self, **kwargs: Any) -> list[Any]:
        self.list_calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return list(self._actions)

    async def reject(self, action_id: str, reason: str = "", rejector: str = "user") -> Any:
        self.rejected.append((action_id, reason, rejector))
        return SimpleNamespace(id=action_id)


def _action(action_id: str, draft_id: str) -> Any:
    return SimpleNamespace(id=action_id, parameters={GROWTH_SEND_PARAM_KEY: {"draft_id": draft_id}})


async def _prospect(client: AsyncClient, domain: str) -> dict[str, Any]:
    resp = await client.post(PROSPECTS_URL, json=_payload(domain=domain))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _draft(
    client: AsyncClient, prospect_id: str, channel: str = "linkedin"
) -> dict[str, Any]:
    resp = await client.post(
        f"{PROSPECTS_URL}/{prospect_id}/drafts", json={"channel": channel, "body": "Hi there"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _propose(client: AsyncClient, draft_id: str) -> None:
    resp = await client.post(
        f"/api/v1/growth/drafts/{draft_id}/status", json={"status": "proposed"}
    )
    assert resp.status_code == 200, resp.text


async def _prospect_ids(workspace_id: str) -> set[str]:
    from pocketpaw_ee.cloud.models.prospect import Prospect

    return {str(d.id) for d in await Prospect.find({"workspace": workspace_id}).to_list()}


async def _draft_ids(workspace_id: str) -> set[str]:
    from pocketpaw_ee.cloud.models.draft import Draft

    return {str(d.id) for d in await Draft.find({"workspace": workspace_id}).to_list()}


# ---------------------------------------------------------------------------
# Bulk delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_delete_removes_selected_prospects_and_only_their_drafts(w1_client):
    a = await _prospect(w1_client, "alpha.io")
    b = await _prospect(w1_client, "beta.io")
    keep = await _prospect(w1_client, "keep.io")
    await _draft(w1_client, a["id"])
    await _draft(w1_client, a["id"], channel="whatsapp")
    await _draft(w1_client, b["id"])
    kept_draft = await _draft(w1_client, keep["id"])

    resp = await w1_client.post(BULK_DELETE_URL, json={"ids": [a["id"], b["id"]]})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": 2, "drafts_removed": 3, "proposals_withdrawn": 0}
    assert await _prospect_ids("w1") == {keep["id"]}
    assert await _draft_ids("w1") == {kept_draft["id"]}


@pytest.mark.asyncio
async def test_bulk_delete_keeps_message_log_rows(w1_client):
    from pocketpaw_ee.cloud.models.message_log import MessageLog

    a = await _prospect(w1_client, "alpha.io")
    draft = await _draft(w1_client, a["id"], channel="email")
    await growth_service.record_message_log(
        workspace_id="w1",
        draft_id=draft["id"],
        prospect_id=a["id"],
        channel="email",
        provider="mailtrap",
        to_address="sam@alpha.io",
        outcome="sent",
    )

    resp = await w1_client.post(BULK_DELETE_URL, json={"ids": [a["id"]]})

    assert resp.json()["deleted"] == 1
    logs = await MessageLog.find({"workspace": "w1"}).to_list()
    assert [(log.draft_id, log.prospect_id) for log in logs] == [(draft["id"], a["id"])]


@pytest.mark.asyncio
async def test_bulk_delete_cannot_reach_another_workspace(w1_client, w2_client):
    a = await _prospect(w1_client, "alpha.io")
    draft = await _draft(w1_client, a["id"])

    resp = await w2_client.post(BULK_DELETE_URL, json={"ids": [a["id"]]})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": 0, "drafts_removed": 0, "proposals_withdrawn": 0}
    assert await _prospect_ids("w1") == {a["id"]}
    assert await _draft_ids("w1") == {draft["id"]}


@pytest.mark.asyncio
async def test_bulk_delete_skips_malformed_and_unknown_ids(w1_client):
    a = await _prospect(w1_client, "alpha.io")

    resp = await w1_client.post(
        BULK_DELETE_URL,
        json={"ids": ["not-an-id", str(PydanticObjectId()), a["id"]]},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] == 1
    assert await _prospect_ids("w1") == set()


@pytest.mark.parametrize(("count", "status"), [(0, 422), (500, 200), (501, 422)])
@pytest.mark.asyncio
async def test_bulk_delete_id_list_bounds(w1_client, count, status):
    ids = [str(PydanticObjectId()) for _ in range(count)]
    resp = await w1_client.post(BULK_DELETE_URL, json={"ids": ids})
    assert resp.status_code == status, resp.text


# ---------------------------------------------------------------------------
# Pending Instinct proposals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_delete_withdraws_the_pending_proposal_of_a_proposed_draft(
    w1_client, monkeypatch
):
    a = await _prospect(w1_client, "alpha.io")
    proposed = await _draft(w1_client, a["id"])
    await _propose(w1_client, proposed["id"])
    store = _FakeInstinctStore(
        [_action("act-1", proposed["id"]), _action("act-other", str(PydanticObjectId()))]
    )
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: store)

    resp = await w1_client.post(BULK_DELETE_URL, json={"ids": [a["id"]]})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": 1, "drafts_removed": 1, "proposals_withdrawn": 1}
    assert [(aid, reason) for aid, reason, _ in store.rejected] == [("act-1", "prospect deleted")]
    assert store.list_calls[0]["workspace_id"] == "w1"


@pytest.mark.asyncio
async def test_a_failing_instinct_store_does_not_block_the_delete(w1_client, monkeypatch):
    a = await _prospect(w1_client, "alpha.io")
    proposed = await _draft(w1_client, a["id"])
    await _propose(w1_client, proposed["id"])
    store = _FakeInstinctStore([], raises=RuntimeError("tray unavailable"))
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: store)

    resp = await w1_client.post(BULK_DELETE_URL, json={"ids": [a["id"]]})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": 1, "drafts_removed": 1, "proposals_withdrawn": 0}
    assert await _prospect_ids("w1") == set()
    assert await _draft_ids("w1") == set()


# ---------------------------------------------------------------------------
# Single delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_delete_returns_204_then_404(w1_client):
    a = await _prospect(w1_client, "alpha.io")
    await _draft(w1_client, a["id"])

    first = await w1_client.delete(f"{PROSPECTS_URL}/{a['id']}")
    second = await w1_client.delete(f"{PROSPECTS_URL}/{a['id']}")
    malformed = await w1_client.delete(f"{PROSPECTS_URL}/not-an-id")

    assert first.status_code == 204
    assert first.content == b""
    assert second.status_code == 404
    assert malformed.status_code == 404
    assert await _draft_ids("w1") == set()


@pytest.mark.asyncio
async def test_single_delete_of_another_workspaces_prospect_is_404(w1_client, w2_client):
    a = await _prospect(w1_client, "alpha.io")

    resp = await w2_client.delete(f"{PROSPECTS_URL}/{a['id']}")

    assert resp.status_code == 404
    assert await _prospect_ids("w1") == {a["id"]}
