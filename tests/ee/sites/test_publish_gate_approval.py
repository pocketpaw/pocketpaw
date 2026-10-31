# tests/ee/sites/test_publish_gate_approval.py — the publish gate on the approval path:
# agent ``request_publish_pocket`` -> Tray card -> approve -> merge executor.
#
# "Put it live" in chat files a review card instead of publishing, and the approve
# merges the draft and deploys it. The gate must hold there too: the card carries the
# owner-worded findings when it is created, a plain approve (single or bulk) of a draft
# that still shows "[Address]" publishes NOTHING (no version promoted, no deploy) and
# fails the Action with the findings, and only an individual approve with
# ``confirm_placeholders`` takes it live. Mutations: tests/mutations/sites_publish_gate.json.
from __future__ import annotations

from typing import Any

import pytest
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.versions import service as versions

from pocketpaw.instinct.store import InstinctStore

from .test_verify_checks import _doc

WS = "ws-gate-approval"
POCKET = "pocket-gate-approval"
USER = "user-gate-approval"

_CAFE = _doc("<p>Find us at [Address]. Call us on [Phone].</p>")
_CLEAN = _doc("<p>Find us at 4 Mill Lane, Leeds.</p>")


def _pocket(index: str) -> dict[str, Any]:
    return {"id": POCKET, "name": "Saltline", "engine": "html", "source": {"index.html": index}}


@pytest.fixture
def pocket(monkeypatch) -> dict[str, Any]:
    doc = _pocket(_CAFE)

    async def _get(_pid: str, _uid: str) -> dict[str, Any]:
        return doc

    monkeypatch.setattr(pockets_service, "get", _get)
    return doc


# ── the card carries the findings ───────────────────────────────────────────────


@pytest.fixture
def instinct_store(tmp_path, monkeypatch):
    store = InstinctStore(tmp_path / "gate_approval_instinct.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: store)
    return store


async def _request(instinct_store) -> Any:
    await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"index.html": "x"}
    )
    return await sites_service.request_publish_pocket(
        workspace_id=WS, user_id=USER, pocket_id=POCKET
    )


@pytest.mark.asyncio
async def test_the_review_card_carries_the_findings(beanie_test_db, instinct_store, pocket):
    action = await _request(instinct_store)

    findings = action.parameters["_artifact_change"]["publish_findings"]
    assert 'Your site still shows "[Address]" on the home page.' in [f["message"] for f in findings]
    assert 'Your site still shows "[Address]" on the home page.' in action.description
    assert "needs fixes" in action.title
    assert "publish anyway" in action.recommendation.lower()


@pytest.mark.asyncio
async def test_a_clean_draft_has_a_plain_card(beanie_test_db, instinct_store, pocket):
    pocket["source"] = {"index.html": _CLEAN}
    action = await _request(instinct_store)

    assert action.parameters["_artifact_change"]["publish_findings"] == []
    assert action.title == "Publish site changes"


def test_an_approve_time_edit_cannot_clear_the_findings() -> None:
    from pocketpaw_ee.instinct.router import _pin_gated_blobs

    stored = {"_artifact_change": {**_blob(), "publish_findings": [{"message": "x"}]}}
    incoming = {"_artifact_change": {**_blob(), "publish_findings": []}}

    pinned = _pin_gated_blobs(stored, incoming)

    assert pinned["_artifact_change"]["publish_findings"] == [{"message": "x"}]


# ── the executor: refuse before merging, unless the owner chose publish anyway ─


def _blob() -> dict[str, Any]:
    return {
        "schema": 1,
        "scope_type": "pocket",
        "scope_id": POCKET,
        "branch": "main",
        "to_version_id": "v-2",
        "workspace": WS,
        "user_id": USER,
        "destructive_migrations": [],
    }


class _Action:
    id = "act-gate"

    def __init__(self) -> None:
        self.parameters = {"_artifact_change": _blob()}


class _Recorder:
    def __init__(self) -> None:
        self.deploys: list[dict[str, Any]] = []
        self.merges: list[dict[str, Any]] = []
        self.marks: list[tuple[str, str]] = []
        self.parameters: dict[str, Any] | None = None

    async def publish_pocket(self, **kw: Any) -> None:
        self.deploys.append(kw)

    async def merge(self, **kw: Any) -> None:
        self.merges.append(kw)

    async def mark_executed(self, _id: str, outcome: str) -> None:
        self.marks.append(("executed", outcome))

    async def mark_failed(self, _id: str, outcome: str) -> None:
        self.marks.append(("failed", outcome))

    async def update_parameters(self, _id: str, parameters: dict[str, Any]) -> None:
        self.parameters = parameters


@pytest.fixture
def recorder(monkeypatch, pocket) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(versions, "publish", rec.merge)
    monkeypatch.setattr(sites_service, "publish_pocket", rec.publish_pocket)
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: rec)
    return rec


@pytest.mark.asyncio
async def test_an_approve_with_findings_publishes_nothing(recorder) -> None:
    from pocketpaw_ee.versions import instinct_executor

    await instinct_executor.execute_approved_change(_Action(), confirm_destructive=True)

    assert recorder.merges == [] and recorder.deploys == []
    ((status, error),) = recorder.marks
    assert status == "failed"
    assert error.startswith("sites.publish_blocked: Your site isn't ready to go live yet.")
    assert '"[Address]"' in error
    blocked = recorder.parameters["_artifact_change"]["publish_blocked"]
    assert blocked["override"] == "confirm_placeholders"
    assert any(f["text"] == "[Address]" for f in blocked["findings"])


@pytest.mark.asyncio
async def test_an_approve_with_publish_anyway_goes_live(recorder) -> None:
    from pocketpaw_ee.versions import instinct_executor

    await instinct_executor.execute_approved_change(
        _Action(), confirm_destructive=True, confirm_placeholders=True
    )

    assert len(recorder.merges) == 1
    (deploy,) = recorder.deploys
    assert deploy["machine_republish"] is True
    assert recorder.marks[0][0] == "executed"


@pytest.mark.asyncio
async def test_a_clean_draft_approves_and_skips_the_second_check(recorder, pocket) -> None:
    from pocketpaw_ee.versions import instinct_executor

    pocket["source"] = {"index.html": _CLEAN}
    await instinct_executor.execute_approved_change(_Action())

    (deploy,) = recorder.deploys
    assert deploy["machine_republish"] is True
    assert recorder.marks[0][0] == "executed"


# ── the router: only an individual approve can say publish anyway ──────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "body", "published"),
    [
        ("single", None, False),
        ("single", {"confirm_placeholders": True}, True),
        ("bulk", None, False),
    ],
)
async def test_the_router_publishes_only_on_publish_anyway(
    beanie_test_db, instinct_store, pocket, monkeypatch, route, body, published
):
    from unittest.mock import patch

    from .test_destructive_migration_approval import _instinct_client

    action = await _request(instinct_store)
    deploys: list[dict[str, Any]] = []

    async def _deploy(**kw: Any) -> None:
        deploys.append(kw)

    monkeypatch.setattr(sites_service, "publish_pocket", _deploy)
    monkeypatch.setattr("tests.ee.sites.test_destructive_migration_approval.WS", WS)
    monkeypatch.setattr("tests.ee.sites.test_destructive_migration_approval.USER", USER)
    client = _instinct_client()
    with patch("pocketpaw_ee.instinct.router._store", return_value=instinct_store):
        if route == "bulk":
            resp = client.post("/instinct/actions/bulk-approve", json={"ids": [action.id]})
        else:
            resp = client.post(f"/instinct/actions/{action.id}/approve", json=body or {})
    assert resp.status_code == 200, resp.text

    after = await instinct_store.get_action(action.id)
    if published:
        assert len(deploys) == 1 and after.status.value == "executed"
    else:
        assert deploys == [] and after.status.value == "failed"
        assert after.error.startswith("sites.publish_blocked:")
