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
async def test_the_review_card_records_the_reviewed_content(beanie_test_db, instinct_store, pocket):
    from pocketpaw_ee.sites import verify

    action = await _request(instinct_store)

    blob = action.parameters["_artifact_change"]
    assert blob["reviewed_content_hash"] == verify.publish_content_hash(pocket)


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
    # A failed card cannot be approved again, so it offers no Publish anyway.
    assert blocked["can_override"] is False


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
        ("single", "anyway", True),
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
    if body == "anyway":
        from pocketpaw_ee.sites import publish_gate

        seen = action.parameters["_artifact_change"]["publish_findings"]
        body = {
            "confirm_placeholders": True,
            "confirm_findings_digest": publish_gate.findings_digest(seen),
        }
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


def _approve_client(instinct_store, monkeypatch, deploys: list[dict[str, Any]]) -> Any:
    from .test_destructive_migration_approval import _instinct_client

    async def _deploy(**kw: Any) -> None:
        deploys.append(kw)

    monkeypatch.setattr(sites_service, "publish_pocket", _deploy)
    monkeypatch.setattr("tests.ee.sites.test_destructive_migration_approval.WS", WS)
    monkeypatch.setattr("tests.ee.sites.test_destructive_migration_approval.USER", USER)
    return _instinct_client()


@pytest.mark.asyncio
async def test_a_refused_approve_returns_the_block_not_a_stale_approval(
    beanie_test_db, instinct_store, pocket, monkeypatch
):
    """The bug: the gate refused, the route still said 200 "approved", and the client
    showed "Publishing" then a bare "Not published". The approve must answer 409
    ``sites.publish_blocked`` with the owner-worded reasons and the override rule, and
    leave the card pending so "Publish anyway" can approve that same card."""
    from unittest.mock import patch

    action = await _request(instinct_store)
    deploys: list[dict[str, Any]] = []
    client = _approve_client(instinct_store, monkeypatch, deploys)
    with patch("pocketpaw_ee.instinct.router._store", return_value=instinct_store):
        resp = client.post(f"/instinct/actions/{action.id}/approve", json={})

    assert resp.status_code == 409, resp.text
    error = resp.json()["error"]
    assert error["code"] == "sites.publish_blocked"
    details = error["details"]
    messages = [f["message"] for f in details["findings"]]
    assert 'Your site still shows "[Address]" on the home page.' in messages
    assert {f["page_label"] for f in details["findings"]} == {"the home page"}
    assert details["override"] == "confirm_placeholders"
    assert details["can_override"] is True
    assert details["action_id"] == action.id
    assert deploys == []
    after = await instinct_store.get_action(action.id)
    assert after.status.value == "pending"
    assert after.parameters["_artifact_change"]["publish_blocked"]["findings"]


@pytest.mark.asyncio
async def test_publish_anyway_approves_the_refused_card(
    beanie_test_db, instinct_store, pocket, monkeypatch
):
    from unittest.mock import patch

    action = await _request(instinct_store)
    deploys: list[dict[str, Any]] = []
    client = _approve_client(instinct_store, monkeypatch, deploys)
    with patch("pocketpaw_ee.instinct.router._store", return_value=instinct_store):
        refused = client.post(f"/instinct/actions/{action.id}/approve", json={})
        digest = refused.json()["error"]["details"]["findings_digest"]
        anyway = client.post(
            f"/instinct/actions/{action.id}/approve",
            json={"confirm_placeholders": True, "confirm_findings_digest": digest},
        )

    assert refused.status_code == 409, refused.text
    assert anyway.status_code == 200, anyway.text
    assert len(deploys) == 1
    after = await instinct_store.get_action(action.id)
    assert after.status.value == "executed"


@pytest.mark.asyncio
async def test_a_clean_draft_approves_with_200(beanie_test_db, instinct_store, pocket, monkeypatch):
    from unittest.mock import patch

    pocket["source"] = {"index.html": _CLEAN}
    action = await _request(instinct_store)
    deploys: list[dict[str, Any]] = []
    client = _approve_client(instinct_store, monkeypatch, deploys)
    with patch("pocketpaw_ee.instinct.router._store", return_value=instinct_store):
        resp = client.post(f"/instinct/actions/{action.id}/approve", json={})

    assert resp.status_code == 200, resp.text
    assert len(deploys) == 1


# ── Publish anyway covers only the findings the owner saw ──────────────────────


@pytest.mark.asyncio
async def test_publish_anyway_without_the_digest_is_refused(
    beanie_test_db, instinct_store, pocket, monkeypatch
):
    from unittest.mock import patch

    action = await _request(instinct_store)
    deploys: list[dict[str, Any]] = []
    client = _approve_client(instinct_store, monkeypatch, deploys)
    with patch("pocketpaw_ee.instinct.router._store", return_value=instinct_store):
        resp = client.post(
            f"/instinct/actions/{action.id}/approve", json={"confirm_placeholders": True}
        )

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["details"]["findings_digest"]
    assert deploys == []
    assert (await instinct_store.get_action(action.id)).status.value == "pending"


@pytest.mark.asyncio
async def test_publish_anyway_on_changed_findings_answers_the_new_ones(
    beanie_test_db, instinct_store, pocket, monkeypatch
):
    """The owner saw "[Address]"; the draft now shows "[Email]" instead. Their
    Publish anyway must not cover findings they never saw: the content changed since
    the card was filed, so it is refused as stale (no override) and fails."""
    from unittest.mock import patch

    action = await _request(instinct_store)
    deploys: list[dict[str, Any]] = []
    client = _approve_client(instinct_store, monkeypatch, deploys)
    with patch("pocketpaw_ee.instinct.router._store", return_value=instinct_store):
        seen = client.post(f"/instinct/actions/{action.id}/approve", json={})
        old_digest = seen.json()["error"]["details"]["findings_digest"]
        pocket["source"] = {"index.html": _doc("<p>Write to [Email] for orders.</p>")}
        resp = client.post(
            f"/instinct/actions/{action.id}/approve",
            json={"confirm_placeholders": True, "confirm_findings_digest": old_digest},
        )

    assert old_digest
    _assert_stale_409(resp, action.id)
    assert deploys == []
    assert (await instinct_store.get_action(action.id)).status.value == "failed"


# ── the executor re-checks content that changed after the route's gate ─────────


@pytest.mark.asyncio
async def test_content_changed_after_the_route_gate_is_refused_with_the_block(
    beanie_test_db, instinct_store, pocket, monkeypatch
):
    """The route's gate passed a clean draft; the draft changed before the merge.
    The executor refuses the card as stale (the content is not what was reviewed)
    and the approve answers that 409, with no second refusal."""
    from unittest.mock import patch

    from pocketpaw_ee.versions import instinct_executor

    pocket["source"] = {"index.html": _CLEAN}
    action = await _request(instinct_store)
    deploys: list[dict[str, Any]] = []
    client = _approve_client(instinct_store, monkeypatch, deploys)
    real_gate = instinct_executor.approve_gate

    async def _gate_then_edit(*a: Any, **kw: Any) -> Any:
        result = await real_gate(*a, **kw)
        pocket["source"] = {"index.html": _CAFE}  # an agent edit lands mid-approve
        return result

    monkeypatch.setattr(instinct_executor, "approve_gate", _gate_then_edit)
    with patch("pocketpaw_ee.instinct.router._store", return_value=instinct_store):
        resp = client.post(f"/instinct/actions/{action.id}/approve", json={})

    # The card failed, so it cannot be approved again: Publish files a new one.
    _assert_stale_409(resp, action.id)
    assert deploys == []
    after = await instinct_store.get_action(action.id)
    assert after.status.value == "failed"
    assert "publish_blocked" not in after.parameters["_artifact_change"]


@pytest.mark.asyncio
async def test_unchanged_content_skips_the_executor_recheck(recorder, pocket, monkeypatch) -> None:
    from pocketpaw_ee.sites import verify
    from pocketpaw_ee.versions import instinct_executor

    pocket["source"] = {"index.html": _CLEAN}
    calls: list[int] = []
    real = instinct_executor._gate

    async def _counting(*a: Any, **kw: Any) -> Any:
        calls.append(1)
        return await real(*a, **kw)

    monkeypatch.setattr(instinct_executor, "_gate", _counting)
    await instinct_executor.execute_approved_change(
        _Action(), gated_hash=verify.publish_content_hash(pocket)
    )

    assert calls == []
    assert len(recorder.merges) == 1


# ── a refusal does not persist the approver's unreviewed edits ─────────────────


@pytest.mark.asyncio
async def test_a_refused_approve_keeps_the_stored_proposal(
    beanie_test_db, instinct_store, pocket, monkeypatch
):
    from unittest.mock import patch

    action = await _request(instinct_store)
    deploys: list[dict[str, Any]] = []
    client = _approve_client(instinct_store, monkeypatch, deploys)
    with patch("pocketpaw_ee.instinct.router._store", return_value=instinct_store):
        resp = client.post(
            f"/instinct/actions/{action.id}/approve",
            json={"parameters": {**action.parameters, "note": "edited at approve"}},
        )

    assert resp.status_code == 409, resp.text
    stored = await instinct_store.get_action(action.id)
    assert "note" not in stored.parameters
    assert stored.parameters["_artifact_change"]["publish_blocked"]["findings"]


# ── a stale review card answers 409 sites.publish_review_stale ─────────────────


def _assert_stale_409(resp, action_id: str) -> None:
    assert resp.status_code == 409, resp.text
    error = resp.json()["error"]
    assert error["code"] == "sites.publish_review_stale"
    assert error["message"] == (
        "This review is out of date: the site changed since it was filed. Request a new review."
    )
    assert error["details"]["can_override"] is False
    assert error["details"]["action_id"] == action_id


@pytest.mark.asyncio
async def test_a_card_whose_site_changed_since_review_answers_stale_409(
    beanie_test_db, instinct_store, pocket, monkeypatch
):
    """An edit after the review: the single approve refuses with the stale 409 (no
    override, Publish anyway included), deploys nothing and fails the card."""
    from unittest.mock import patch

    pocket["source"] = {"index.html": _CLEAN}
    action = await _request(instinct_store)
    pocket["source"] = {"index.html": _doc("<p>An edit nobody reviewed.</p>")}
    deploys: list[dict[str, Any]] = []
    client = _approve_client(instinct_store, monkeypatch, deploys)
    with patch("pocketpaw_ee.instinct.router._store", return_value=instinct_store):
        resp = client.post(
            f"/instinct/actions/{action.id}/approve", json={"confirm_placeholders": True}
        )

    _assert_stale_409(resp, action.id)
    assert deploys == []
    after = await instinct_store.get_action(action.id)
    assert after.status.value == "failed"
    assert after.error.startswith("sites.publish_review_stale:")
    assert after.parameters["_artifact_change"]["publish_stale"]["can_override"] is False


@pytest.mark.asyncio
async def test_a_legacy_card_whose_draft_was_superseded_answers_stale_409(
    beanie_test_db, instinct_store, pocket, monkeypatch
):
    """A card without ``reviewed_content_hash`` whose ``to_version_id`` a later edit
    superseded: the stale 409, never the raw refusal ``versions.publish`` raises for
    a superseded row."""
    from unittest.mock import patch

    pocket["source"] = {"index.html": _CLEAN}
    action = await _request(instinct_store)
    params = dict(action.parameters)
    params["_artifact_change"] = {
        k: v for k, v in params["_artifact_change"].items() if k != "reviewed_content_hash"
    }
    await instinct_store.update_parameters(action.id, params)
    await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"index.html": "y"}
    )
    deploys: list[dict[str, Any]] = []
    client = _approve_client(instinct_store, monkeypatch, deploys)
    with patch("pocketpaw_ee.instinct.router._store", return_value=instinct_store):
        resp = client.post(f"/instinct/actions/{action.id}/approve", json={})

    _assert_stale_409(resp, action.id)
    assert deploys == []
    assert (await instinct_store.get_action(action.id)).status.value == "failed"
