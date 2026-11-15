# tests/cloud/growth/social/test_setup_status.py — the brand's "Set up" checklist
# (``GET /growth/social/setup-status`` and ``POST /setup-status/skip``). Same harness
# as test_router.py: the social router over mongomock-backed Beanie with
# per-workspace clients; Postiz is the in-memory fake from test_accounts.py.
#
# Covers: each row done and todo; skip persists and is idempotent; un-skip; done
# beats skipped; an unknown key is a 422; the blueprint row reads the approved
# version only; the accounts row goes through ``list_accounts``; tenancy on
# workspace and on profile; a member can read and skip; the mapper round trip.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from pocketpaw_ee.cloud._core.crypto import encrypt
from pocketpaw_ee.cloud.growth.social import service as social_service
from pocketpaw_ee.cloud.growth.social.dto import SocialAccountListResponse
from pocketpaw_ee.cloud.growth.social.postiz import PostizError, set_production_postiz
from pocketpaw_ee.cloud.models.growth_blueprint import GrowthBlueprint as BlueprintDoc
from pocketpaw_ee.cloud.models.social_postiz_org import SocialPostizOrg
from pocketpaw_ee.cloud.models.social_profile import SocialProfile as ProfileDoc

from tests.cloud.growth.social.test_accounts import _FakePostiz, _integration
from tests.cloud.growth.social.test_router import BASE, _client

STATUS = f"{BASE}/setup-status"
SKIP = f"{STATUS}/skip"
KEYS = ["scan", "blueprint", "accounts", "repos", "daily_drafts"]


@pytest_asyncio.fixture
async def w1(mongo_db: Any):
    async with await _client("w1") as client:
        yield client


@pytest_asyncio.fixture
async def w2(mongo_db: Any):
    async with await _client("w2") as client:
        yield client


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CLOUD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    try:
        yield
    finally:
        set_production_postiz(None)


async def _profile(workspace: str = "w1", **extra: Any) -> str:
    doc = ProfileDoc(workspace=workspace, company_name="Acme", **extra)
    await doc.insert()
    return str(doc.id)


async def _states(client, pid: str) -> dict[str, str]:
    resp = await client.get(STATUS, params={"profile_id": pid})
    assert resp.status_code == 200, resp.text
    rows = resp.json()["rows"]
    assert [r["key"] for r in rows] == KEYS
    return {r["key"]: r["state"] for r in rows}


async def _blueprint(pid: str, status: str, version: int = 1, workspace: str = "w1") -> None:
    await BlueprintDoc(
        workspace=workspace, profile_id=pid, version=version, status=status, created_by="u1"
    ).insert()


async def _connect(pid: str, workspace: str = "w1") -> _FakePostiz:
    fake = _FakePostiz()
    fake.integrations["key-1"] = [_integration("acc-1")]
    set_production_postiz(fake)  # type: ignore[arg-type]
    await SocialPostizOrg(
        workspace=workspace, profile=pid, postiz_org_id="org-1", api_key_enc=encrypt("key-1")
    ).insert()
    return fake


async def _skip(client, pid: str, key: str, skipped: bool | None = None):
    body: dict[str, Any] = {"key": key}
    if skipped is not None:
        body["skipped"] = skipped
    return await client.post(SKIP, params={"profile_id": pid}, json=body)


# --- rows ----------------------------------------------------------------------------


async def test_a_fresh_brand_is_all_todo(w1):
    pid = await _profile()
    assert await _states(w1, pid) == dict.fromkeys(KEYS, "todo")


async def test_every_row_done(w1):
    pid = await _profile(
        analysis_status="ready",
        analysis={"summary": "s"},
        analyzed_at=datetime(2026, 11, 1, tzinfo=UTC),
        github_repos=["acme/web"],
        active=True,
    )
    await _blueprint(pid, "approved")
    await _connect(pid)
    assert await _states(w1, pid) == dict.fromkeys(KEYS, "done")


async def test_scan_needs_a_completed_analysis_not_a_failed_one(w1):
    pid = await _profile(analysis_status="failed", analysis_error="boom")
    assert (await _states(w1, pid))["scan"] == "todo"


async def test_blueprint_row_reads_the_approved_version_only(w1):
    pid = await _profile()
    await _blueprint(pid, "draft")
    await _blueprint(pid, "superseded", version=2)
    assert (await _states(w1, pid))["blueprint"] == "todo"
    await _blueprint(pid, "approved", version=3)
    assert (await _states(w1, pid))["blueprint"] == "done"


async def test_blueprint_row_never_reads_another_brand(w1):
    pid = await _profile()
    other = await _profile()
    await _blueprint(other, "approved")
    await _blueprint(pid, "approved", workspace="w2")
    assert (await _states(w1, pid))["blueprint"] == "todo"


async def test_accounts_row_needs_a_connected_account(w1):
    pid = await _profile()
    fake = await _connect(pid)
    assert (await _states(w1, pid))["accounts"] == "done"
    fake.integrations["key-1"] = []
    assert (await _states(w1, pid))["accounts"] == "todo"


async def test_accounts_row_reads_todo_when_postiz_fails(w1, monkeypatch):
    pid = await _profile()
    fake = await _connect(pid)

    async def _boom(api_key: str):
        raise PostizError("down", "postiz is down")

    monkeypatch.setattr(fake, "list_integrations", _boom)
    assert (await _states(w1, pid))["accounts"] == "todo"


async def test_accounts_row_goes_through_the_accounts_list_lookup(w1, monkeypatch):
    pid = await _profile()
    calls: list[str | None] = []

    async def _fake_list(ctx, profile_id=None):
        calls.append(profile_id)
        return SocialAccountListResponse.model_validate(
            {
                "items": [
                    {
                        "id": "a",
                        "platform": "x",
                        "name": "Acme",
                        "username": None,
                        "picture": None,
                        "disabled": False,
                        "can_post": True,
                        "needs_channel": False,
                    }
                ],
                "platforms": [],
                "configured": True,
            }
        )

    monkeypatch.setattr(social_service, "list_accounts", _fake_list)
    assert (await _states(w1, pid))["accounts"] == "done"
    assert calls == [pid]


# --- skip ----------------------------------------------------------------------------


async def test_skip_persists_is_idempotent_and_un_skips(w1):
    pid = await _profile()
    for _ in range(2):
        resp = await _skip(w1, pid, "repos")
        assert resp.status_code == 200, resp.text
        assert {r["key"]: r["state"] for r in resp.json()["rows"]}["repos"] == "skipped"
    assert (await ProfileDoc.get(pid)).setup_skipped == ["repos"]
    assert (await _states(w1, pid))["repos"] == "skipped"
    profile = (await w1.get(f"{BASE}/profile", params={"profile_id": pid})).json()
    assert profile["setup_skipped"] == ["repos"]

    resp = await _skip(w1, pid, "repos", skipped=False)
    assert resp.status_code == 200
    assert (await ProfileDoc.get(pid)).setup_skipped == []
    assert (await _states(w1, pid))["repos"] == "todo"


async def test_done_beats_skipped(w1):
    pid = await _profile(github_repos=["acme/web"], active=True)
    await _skip(w1, pid, "repos")
    await _skip(w1, pid, "daily_drafts")
    states = await _states(w1, pid)
    assert (states["repos"], states["daily_drafts"]) == ("done", "done")


@pytest.mark.parametrize("key", ["nope", "", None])
async def test_an_unknown_key_is_422(w1, key):
    pid = await _profile()
    resp = await w1.post(SKIP, params={"profile_id": pid}, json={"key": key})
    assert resp.status_code == 422
    assert (await ProfileDoc.get(pid)).setup_skipped == []


async def test_an_archived_brand_is_still_read_by_id(w1):
    pid = await _profile(archived_at=datetime(2030, 1, 1, tzinfo=UTC), active=True)
    assert (await _states(w1, pid))["daily_drafts"] == "done"
    assert (await _skip(w1, pid, "scan")).status_code == 200


# --- tenancy and RBAC ----------------------------------------------------------------


async def test_tenancy_on_workspace_and_profile(w1, w2):
    pid = await _profile()
    other = await _profile(active=True)
    assert (await w2.get(STATUS, params={"profile_id": pid})).status_code == 404
    assert (await _skip(w2, pid, "scan")).status_code == 404
    assert (await ProfileDoc.get(pid)).setup_skipped == []

    await _skip(w1, other, "scan")
    assert (await ProfileDoc.get(pid)).setup_skipped == []
    assert (await _states(w1, pid))["daily_drafts"] == "todo"
    assert (await w1.get(STATUS, params={"profile_id": "not-an-id"})).status_code == 404


async def test_a_member_can_read_and_skip(mongo_db):
    # growth.read and growth.write are both MEMBER, the lowest workspace role.
    pid = await _profile()
    async with await _client("w1", role="member") as member:
        assert (await member.get(STATUS, params={"profile_id": pid})).status_code == 200
        assert (await _skip(member, pid, "accounts")).status_code == 200
    assert (await ProfileDoc.get(pid)).setup_skipped == ["accounts"]


# --- mapper round trip ---------------------------------------------------------------


def test_profile_mappers_carry_setup_skipped():
    doc = ProfileDoc(workspace="w1", setup_skipped=["scan", "repos"])
    domain = social_service._profile_to_domain(doc)
    assert domain.setup_skipped == ("scan", "repos")
    assert social_service._profile_to_response(domain).setup_skipped == ["scan", "repos"]
