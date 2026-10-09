# Tests for Durable Objects operations, slice 9 (ee/pocketpaw_ee/sites/do_metering.py,
# the platform vars bundle_deploy sets for a DO site, the cascade's pending-teardown
# record and the two sweeps). Cloudflare is faked; nothing real is called.
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites import bundle_deploy, delete_cascade, do_metering, draft_worker
from pocketpaw_ee.sites import durable_objects as dobj
from pocketpaw_ee.sites.cloudflare_client import CloudflareClient

from tests.ee.sites.test_draft_worker import FakeCF, Store, _build
from tests.ee.sites.test_durable_objects import _CF, _metadata, do_build  # noqa: F401
from tests.ee.sites.test_durable_objects_lifecycle import V1, _draft_manifest, _drafts  # noqa: F401
from tests.ee.sites.test_project_engine import _Records

ACCT = "acct_1"
DAY = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(dobj.FLAG_ENV, "1")
    for key in (
        dobj.ROOM_MAX_PAID_ENV,
        do_metering.DAILY_REQUESTS_ENV[False],
        do_metering.DAILY_REQUESTS_ENV[True],
        do_metering.TEARDOWN_MAX_ATTEMPTS_ENV,
    ):
        monkeypatch.delenv(key, raising=False)
    do_metering._reset()


# ------------------------------------------------------------- room cap


def test_room_cap_by_plan(monkeypatch):
    assert dobj.room_max_peers(paid=False) == 10
    assert dobj.room_max_peers(paid=True) == 50
    monkeypatch.setenv(dobj.ROOM_MAX_PAID_ENV, "20")
    assert dobj.room_max_peers(paid=True) == 20
    monkeypatch.setenv(dobj.ROOM_MAX_PAID_ENV, "999")
    assert dobj.room_max_peers(paid=True) == 50  # clamped
    monkeypatch.setenv(dobj.ROOM_MAX_PAID_ENV, "lots")
    assert dobj.room_max_peers(paid=True) == 50


def _plain(meta: dict) -> dict[str, str]:
    return {b["name"]: b["text"] for b in meta["bindings"] if b["type"] == "plain_text"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "paid,throttled,cap,flag", [(False, False, "10", "0"), (True, True, "50", "1")]
)
async def test_a_do_deploy_carries_the_platform_vars(do_build, paid, throttled, cap, flag):  # noqa: F811
    cf = _CF(tag="v1")
    await bundle_deploy.deploy_bundle(
        cf.client(),
        script_name="s",
        build_dir=do_build,
        salt="ws",
        paid=paid,
        do_throttled=throttled,
    )
    meta = _metadata(next(r for r in cf.requests if r.method == "PUT"))
    plain = _plain(meta)
    assert plain["ROOM_MAX_PEERS"] == cap and plain["PAW_DO_THROTTLED"] == flag
    assert plain["PAW_SITE_ORIGINS"] == "" and plain["PAW_DO_SUSPENDED"] == "0"


@pytest.mark.asyncio
async def test_a_bundle_without_dos_gets_no_platform_vars(do_build):  # noqa: F811
    manifest = json.loads((do_build / "paw-build.json").read_text())
    manifest.pop("durableObjects")
    manifest["bindingRequests"] = []
    (do_build / "paw-build.json").write_text(json.dumps(manifest))
    cf = _CF()
    await bundle_deploy.deploy_bundle(
        cf.client(), script_name="s", build_dir=do_build, salt="ws", do_throttled=True
    )
    assert _plain(_metadata(next(r for r in cf.requests if r.method == "PUT"))) == {}


@pytest.mark.asyncio
@pytest.mark.usefixtures("_drafts")
async def test_a_draft_gets_the_room_cap_and_is_never_throttled():
    store, records, cf, registry = Store(), _Records(), FakeCF(), draft_worker.MemoryRegistry()
    out = await _build(store, records, cf, registry, manifest=_draft_manifest(V1))
    assert out["preview_mode"] == "full"
    put = cf.named("put_worker")[-1]
    plain = {b["name"]: b["text"] for b in put["bindings"] if b["type"] == "plain_text"}
    assert plain["ROOM_MAX_PEERS"] == "10" and plain["PAW_DO_THROTTLED"] == "0"


# ------------------------------------------------------------ graphql


@pytest.mark.asyncio
async def test_query_graphql_posts_and_fails_closed():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        seen.append(request)
        body = json.loads(request.content)
        if body["variables"].get("bad"):
            return httpx.Response(200, json={"data": None, "errors": [{"message": "no"}]})
        return httpx.Response(200, json={"data": {"viewer": {"ok": True}}, "errors": None})

    client = CloudflareClient(
        account_id=ACCT,
        api_token="tok",
        zone_id="z",
        dispatch_namespace="ns",
        _transport=httpx.MockTransport(handler),
    )
    assert client.account_id == ACCT
    data = await client.query_graphql("query { viewer { ok } }", {"a": 1})
    assert data == {"viewer": {"ok": True}}
    assert str(seen[0].url) == "https://api.cloudflare.com/client/v4/graphql"
    with pytest.raises(ValidationError):
        await client.query_graphql("query { x }", {"bad": True})


# ------------------------------------------------------------ usage


class _UsageCF:
    account_id = ACCT

    def __init__(self, *, requests: dict[str, int], fail: set[str] | None = None):
        self.requests = requests  # namespace id -> requests
        self.fail = fail or set()
        self.queries: list[dict] = []

    async def list_durable_object_namespaces(self):
        if "namespaces" in self.fail:
            raise ValidationError("sites.cloudflare_error", "Cloudflare API 500")
        return [
            {"id": "ns-a", "script": "site-a", "class": "Room"},
            {"id": "ns-b", "script": "site-b", "class": "Room"},
            {"id": "ns-x", "script": "someone-else", "class": "X"},
        ]

    async def get_script_settings(self, script_name, *, target):
        return {"bindings": []}

    async def patch_script_settings(self, script_name, settings, *, target):
        return settings

    async def query_graphql(self, query: str, variables: dict) -> dict:
        self.queries.append(variables)
        if "durableObjectsInvocationsAdaptiveGroups" in query:
            if "requests" in self.fail:
                raise ValidationError("sites.cloudflare_error", "graphql: no")
            rows = [
                {"sum": {"requests": n}, "dimensions": {"namespaceId": ns}}
                for ns, n in self.requests.items()
            ]
        elif "durableObjectsPeriodicGroups" in query:
            if "duration" in self.fail:
                raise ValidationError("sites.cloudflare_error", "graphql: no")
            rows = [{"sum": {"activeTime": 7}, "dimensions": {"namespaceId": "ns-a"}}]
        else:
            if "storage" in self.fail:
                raise ValidationError("sites.cloudflare_error", "graphql: no")
            rows = [{"max": {"storedBytes": 4096}, "dimensions": {"namespaceId": "ns-a"}}]
        return {"viewer": {"accounts": [{"rows": rows}]}}


@pytest.mark.asyncio
async def test_read_usage_sums_per_script_for_the_day():
    cf = _UsageCF(requests={"ns-a": 120, "ns-b": 5, "ns-x": 999})
    usage = await do_metering.read_usage(cf, ["site-a", "site-b"], DAY.date())
    assert usage["site-a"] == do_metering.ScriptUsage(120, 7, 4096)
    assert usage["site-b"] == do_metering.ScriptUsage(5, 0, 0)
    assert "someone-else" not in usage
    assert cf.queries[0]["account"] == ACCT and cf.queries[0]["date"] == "2026-10-08"
    assert sorted(cf.queries[0]["namespaces"]) == ["ns-a", "ns-b"]


@pytest.mark.asyncio
async def test_a_storage_or_duration_failure_still_meters_requests():
    cf = _UsageCF(requests={"ns-a": 3}, fail={"storage", "duration"})
    usage = await do_metering.read_usage(cf, ["site-a"], DAY.date())
    assert usage["site-a"].requests == 3 and usage["site-a"].stored_bytes == 0


async def _seed(name: str, **kw: Any):
    from pocketpaw_ee.cloud.models.site import Site

    doc = Site(
        workspace="ws1",
        pocket_id=f"pk-{name}",
        owner="u1",
        name=name,
        script_name=name,
        deploy_target="wfp",
        **{"do_classes": ["Room"], "do_migration_tags": ["v1"], **kw},
    )
    await doc.insert()
    return doc


@pytest.mark.asyncio
async def test_usage_sweep_stores_the_day_and_throttles_over_the_ceiling(
    beanie_test_db, monkeypatch
):
    from pocketpaw_ee.cloud.models.site import Site

    monkeypatch.setenv(do_metering.DAILY_REQUESTS_ENV[False], "100")
    a, b = await _seed("site-a"), await _seed("site-b", do_throttled=True)
    await _seed("site-c", do_classes=[])  # no DOs: never read
    cf = _UsageCF(requests={"ns-a": 101, "ns-b": 5})

    out = await do_metering.sweep_do_usage(cf=cf, now=DAY)

    a, b = await Site.get(a.id), await Site.get(b.id)
    assert a.do_usage["2026-10-08"] == {"requests": 101, "active_time": 7, "stored_bytes": 4096}
    assert a.do_throttled is True
    assert b.do_throttled is False  # a new day under the ceiling lifts it
    assert out == {"sites": 2, "throttled": 1, "released": 1}


@pytest.mark.asyncio
async def test_usage_sweep_fails_open(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.models.site import Site

    monkeypatch.setenv(do_metering.DAILY_REQUESTS_ENV[False], "1")
    a = await _seed("site-a")
    for fail in ({"requests"}, {"namespaces"}):
        do_metering._reset()
        out = await do_metering.sweep_do_usage(
            cf=_UsageCF(requests={"ns-a": 50}, fail=fail), now=DAY
        )
        a = await Site.get(a.id)
        assert a.do_throttled is False and a.do_usage == {}
        assert out["throttled"] == 0


@pytest.mark.asyncio
async def test_usage_sweep_runs_at_most_once_an_interval(beanie_test_db):
    await _seed("site-a")
    cf = _UsageCF(requests={"ns-a": 1})
    await do_metering.sweep_do_usage(cf=cf, now=DAY)
    calls = len(cf.queries)
    await do_metering.sweep_do_usage(cf=cf, now=DAY + timedelta(minutes=5))
    assert len(cf.queries) == calls
    await do_metering.sweep_do_usage(cf=cf, now=DAY + timedelta(hours=2))
    assert len(cf.queries) > calls


@pytest.mark.asyncio
async def test_usage_sweep_is_off_with_the_flag(beanie_test_db, monkeypatch):
    monkeypatch.setenv(dobj.FLAG_ENV, "0")
    await _seed("site-a")
    cf = _UsageCF(requests={"ns-a": 1})
    assert await do_metering.sweep_do_usage(cf=cf, now=DAY) == {
        "sites": 0,
        "throttled": 0,
        "released": 0,
    }
    assert cf.queries == []


@pytest.mark.asyncio
async def test_a_throttled_site_deploys_with_the_flag(beanie_test_db, monkeypatch):
    from bson import ObjectId
    from pocketpaw_ee.sites import service as sites_service

    from tests.ee.sites.test_durable_objects_lifecycle import (
        DO_BASE,
        _do_manifest,
        _DOFake,
        _first,
    )
    from tests.ee.sites.test_project_d1 import _publish

    (pocket_id, site_id), source = await _first(monkeypatch, _do_manifest(DO_BASE, V1))
    cf = _DOFake()
    await _publish(pocket_id, site_id, source, cf.client())
    assert _plain(cf.metadata[-1])["PAW_DO_THROTTLED"] == "0"
    doc = await sites_service._SiteDoc.find_one({"_id": ObjectId(site_id)})
    await doc.set({"do_throttled": True})
    await _publish(pocket_id, site_id, source, cf.client())
    assert _plain(cf.metadata[-1])["PAW_DO_THROTTLED"] == "1"


# ------------------------------------------------------- teardown retry


class _RetryCF:
    def __init__(self, *, left: bool = False, fail: bool = False):
        self.left, self.fail = left, fail
        self.calls: list[tuple[str, Any]] = []

    async def put_worker(self, **kw):
        self.calls.append(("put_worker", kw))

    async def delete_worker(self, name, *, force=False):
        if self.fail:
            raise ValidationError("sites.cloudflare_error", "Cloudflare API 500")
        self.calls.append(("delete_worker", (name, force)))

    async def delete_account_script(self, name, *, force=False):
        self.calls.append(("delete_account_script", (name, force)))

    async def list_durable_object_namespaces(self):
        return [{"id": "n1", "script": "s1"}] if self.left else []


@pytest.mark.asyncio
async def test_teardown_can_skip_the_tombstone():
    cf = _RetryCF()
    out = await dobj.teardown_script(
        cf, "s1", target="dispatch", classes=["Room"], migration_tag="v1", tombstone=False
    )
    assert out.ok and [c for c, _ in cf.calls] == ["delete_worker"]


@pytest.mark.asyncio
async def test_a_partial_cascade_step_records_a_retry():
    recorded: list[dict] = []

    async def record(**kw):
        recorded.append(kw)

    site = SimpleNamespace(
        id="s1",
        workspace="w1",
        script_name="s1",
        deploy_target="wfp",
        do_classes=["Room"],
        do_migration_tags=["v1"],
        delete_ledger={},
    )
    deps = SimpleNamespace(cloudflare=_RetryCF(left=True), record_do_teardown=record)
    outcome = await delete_cascade._delete_durable_objects(site=site, deps=deps)
    assert outcome == delete_cascade.OUTCOME_PARTIAL
    assert recorded == [
        {
            "site_id": "s1",
            "workspace": "w1",
            "script": "s1",
            "target": "dispatch",
            "classes": ["Room"],
            "migration_tag": "v1",
            "error": recorded[0]["error"],
        }
    ]
    assert "namespaces still listed" in recorded[0]["error"]


@pytest.mark.asyncio
async def test_teardown_sweep_retries_with_backoff_then_hands_to_an_operator(
    beanie_test_db, monkeypatch, caplog
):
    from pocketpaw_ee.cloud.models.site_do_teardown import SiteDoTeardown

    monkeypatch.setenv(do_metering.TEARDOWN_MAX_ATTEMPTS_ENV, "2")
    await do_metering.record_do_teardown(
        site_id="s1",
        workspace="w1",
        script="s1",
        target="dispatch",
        classes=["Room"],
        migration_tag="v1",
        error="verify: namespaces still listed",
    )
    now = datetime.now(UTC)
    # Due at once; still failing: one more attempt, backed off.
    out = await do_metering.sweep_do_teardowns(cf=_RetryCF(left=True), now=now)
    (row,) = await SiteDoTeardown.find_all().to_list()
    assert out == {"retried": 1, "done": 0, "operator": 0}
    assert row.attempts == 2 and row.state == "pending"
    assert row.retry_after.replace(tzinfo=UTC) > now
    # Not due yet: untouched.
    out = await do_metering.sweep_do_teardowns(cf=_RetryCF(left=True), now=now)
    assert out == {"retried": 0, "done": 0, "operator": 0}
    # Past the backoff and out of attempts: an operator owns it, loudly.
    later = now + timedelta(days=2)
    out = await do_metering.sweep_do_teardowns(cf=_RetryCF(fail=True), now=later)
    (row,) = await SiteDoTeardown.find_all().to_list()
    assert row.state == "operator" and out["operator"] == 1
    assert "needs an operator" in caplog.text and "s1" in caplog.text


@pytest.mark.asyncio
async def test_teardown_sweep_clears_a_row_that_succeeds(beanie_test_db):
    from pocketpaw_ee.cloud.models.site_do_teardown import SiteDoTeardown

    await do_metering.record_do_teardown(
        site_id="s1",
        workspace="w1",
        script="s1",
        target="dispatch",
        classes=["Room"],
        migration_tag="v1",
        error="x",
    )
    cf = _RetryCF()
    out = await do_metering.sweep_do_teardowns(cf=cf, now=datetime.now(UTC))
    assert out["done"] == 1
    assert await SiteDoTeardown.find_all().to_list() == []
    # The script step already deleted it: a retry never uploads a stub.
    assert all(c != "put_worker" for c, _ in cf.calls)


def test_both_sweeps_are_scheduled():
    from pocketpaw_ee import extensions

    names = [fn.__name__ for fn in extensions._sweeps()[0]]
    assert "sweep_do_usage" in names and "sweep_do_teardowns" in names
