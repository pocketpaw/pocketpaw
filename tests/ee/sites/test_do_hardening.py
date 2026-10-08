# Tests for the security-review fixes on the DO operations slice: H1 (a platform-owned
# entry wrapper enforces the throttle / suspend tiers whatever the author's Worker
# does), H2 (the settings PATCH inherits every binding it does not replace, refuses
# unknown types, runs under a per-script lock, and the sweep re-pushes every run),
# L9 (metering that keeps failing gets loud) and L10 (DO state is reconciled with
# Cloudflare before planning, and persisting it retries). Cloudflare is faked.
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites import bundle_deploy, do_lock, do_metering, draft_worker, platform_guard
from pocketpaw_ee.sites import durable_objects as dobj

from tests.ee.sites.test_do_live_settings import LIVE_BINDINGS, _SettingsCF, _SweepCF
from tests.ee.sites.test_do_metering import _plain
from tests.ee.sites.test_do_metering import _seed as _seed_site
from tests.ee.sites.test_draft_worker import FakeCF, Store, _build
from tests.ee.sites.test_durable_objects import _CF, _metadata, _parts_of, do_build  # noqa: F401
from tests.ee.sites.test_durable_objects_lifecycle import V1, _draft_manifest, _drafts  # noqa: F401
from tests.ee.sites.test_project_engine import _Records

DAY = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(dobj.FLAG_ENV, "1")
    monkeypatch.setenv(do_metering.DAILY_REQUESTS_ENV[False], "100")
    for key in (do_metering.SUSPEND_FACTOR_ENV, do_metering.FAILURE_ALERT_ENV):
        monkeypatch.delenv(key, raising=False)
    do_metering._reset()


# --------------------------------------------------------------------- H1


def _code(module) -> str:
    return module.content.decode()


def test_the_platform_wrapper_enforces_both_tiers():
    mod = platform_guard.wrapper_module("index.js", do_limits=True)
    code = _code(mod)
    assert mod.name == platform_guard.PLATFORM_GUARD_MODULE
    assert 'import * as app from "./index.js";' in code
    assert 'export * from "./index.js";' in code  # DO classes stay exported
    assert 'env.PAW_DO_SUSPENDED === "1"' in code and "status: 503" in code
    assert 'env.PAW_DO_THROTTLED === "1"' in code and "status: 429" in code
    assert '"websocket"' in code
    for handler in ("scheduled", "queue"):
        assert f'"{handler}"' in code  # skipped while suspended
    assert "x-paw-draft-key" not in code


def test_the_draft_guard_composes_the_key_check_and_the_tiers():
    both = _code(draft_worker.guard_module("index.js", do_limits=True))
    assert "x-paw-draft-key" in both and "PAW_DRAFT_KEY" in both
    assert "PAW_DO_SUSPENDED" in both and "PAW_DO_THROTTLED" in both
    # The key check runs first: nothing about the site's state leaks to a stranger.
    assert both.index("allowed(request.headers.get(") < both.index("PAW_DO_SUSPENDED ===")
    plain = _code(draft_worker.guard_module("index.js"))
    assert "x-paw-draft-key" in plain and "PAW_DO_SUSPENDED" not in plain


@pytest.mark.asyncio
async def test_a_do_deploy_enters_through_the_platform_wrapper(do_build):  # noqa: F811
    cf = _CF(tag="v1")
    await bundle_deploy.deploy_bundle(cf.client(), script_name="s", build_dir=do_build, salt="ws")
    put = next(r for r in cf.requests if r.method == "PUT")
    meta = _metadata(put)
    assert meta["main_module"] == platform_guard.PLATFORM_GUARD_MODULE
    names = {p["name"] for p in _parts_of(put)}
    assert {"index.js", platform_guard.PLATFORM_GUARD_MODULE} <= names
    assert _plain(meta)["PAW_DO_SUSPENDED"] == "0"


@pytest.mark.asyncio
async def test_a_bundle_without_dos_keeps_its_own_entry(do_build):  # noqa: F811
    manifest = json.loads((do_build / "paw-build.json").read_text())
    manifest.pop("durableObjects")
    manifest["bindingRequests"] = []
    (do_build / "paw-build.json").write_text(json.dumps(manifest))
    cf = _CF()
    await bundle_deploy.deploy_bundle(cf.client(), script_name="s", build_dir=do_build, salt="ws")
    assert _metadata(next(r for r in cf.requests if r.method == "PUT"))["main_module"] == "index.js"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_drafts")
async def test_a_do_draft_gets_one_wrapper_doing_both():
    store, records, cf, registry = Store(), _Records(), FakeCF(), draft_worker.MemoryRegistry()
    await _build(store, records, cf, registry, manifest=_draft_manifest(V1))
    put = cf.named("put_worker")[-1]
    assert put["main_module"] == draft_worker.GUARD_MODULE
    guard = next(m for m in put["modules"] if m.name == draft_worker.GUARD_MODULE)
    code = guard.content.decode()
    assert "x-paw-draft-key" in code and "PAW_DO_SUSPENDED" in code
    assert not any(m.name == platform_guard.PLATFORM_GUARD_MODULE for m in put["modules"])


@pytest.mark.asyncio
async def test_metering_suspends_past_the_factor_and_pushes_both_flags(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.models.site import Site

    monkeypatch.setenv(do_metering.SUSPEND_FACTOR_ENV, "3")
    site = await _seed_site("site-a")
    cf = _SweepCF(requests={"ns-a": 301})  # ceiling 100, 3x = 300
    await do_metering.sweep_do_usage(cf=cf, now=DAY)
    site = await Site.get(site.id)
    assert site.do_throttled is True and site.do_suspended is True
    sent = {b["name"]: b for b in cf.patches()[0]["bindings"]}
    assert sent["PAW_DO_THROTTLED"]["text"] == "1" and sent["PAW_DO_SUSPENDED"]["text"] == "1"

    do_metering._reset()
    cf = _SweepCF(requests={"ns-a": 150})  # throttled, not suspended
    await do_metering.sweep_do_usage(cf=cf, now=DAY + timedelta(hours=2))
    site = await Site.get(site.id)
    assert site.do_throttled is True and site.do_suspended is False


# --------------------------------------------------------------------- H2


@pytest.mark.asyncio
async def test_the_patch_inherits_every_binding_it_does_not_replace():
    cf = _SettingsCF()
    await dobj.set_platform_vars_live(cf, "s1", target="dispatch", values={"PAW_DO_THROTTLED": "1"})
    sent = cf.patches()[0]["bindings"]
    names = [b["name"] for b in LIVE_BINDINGS if b["name"] != "PAW_DO_THROTTLED"]
    assert sent[:-1] == [{"type": "inherit", "name": n} for n in names]
    assert sent[-1] == {"type": "plain_text", "name": "PAW_DO_THROTTLED", "text": "1"}
    assert "db-1" not in json.dumps(sent)  # nothing echoed back


@pytest.mark.asyncio
async def test_a_binding_type_outside_the_allow_list_blocks_the_patch():
    cf = _SettingsCF([*LIVE_BINDINGS, {"type": "mtls_certificate", "name": "CERT"}])
    with pytest.raises(ValidationError) as exc:
        await dobj.set_platform_vars_live(cf, "s1", target="account", values={"X_VAR": "1"})
    assert exc.value.code == "sites.do_settings_unsupported"
    assert "mtls_certificate" in exc.value.message
    assert cf.patches() == []


@pytest.mark.asyncio
async def test_the_script_lock_serializes_in_process():
    order: list[str] = []

    async def holder(tag: str, hold: float):
        async with do_lock.script_lock("s1"):
            order.append(f"{tag}-in")
            await asyncio.sleep(hold)
            order.append(f"{tag}-out")

    await asyncio.gather(holder("a", 0.05), holder("b", 0))
    assert order == ["a-in", "a-out", "b-in", "b-out"]


class _FakeRedis:
    """Just enough of redis for ``Lease``: SET NX PX and the renew / release scripts."""

    def __init__(self):
        self.data: dict[str, str] = {}

    async def set(self, key, value, nx=False, px=None):
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    async def eval(self, script, _n, key, owner, *_args):
        if self.data.get(key) != owner:
            return 0
        if "DEL" in script:
            del self.data[key]
        return 1


@pytest.mark.asyncio
async def test_the_script_lock_uses_a_lease_across_processes(monkeypatch):
    from pocketpaw_ee.cloud._core import lease as lease_mod

    redis = _FakeRedis()
    monkeypatch.setattr(lease_mod, "multi_worker_enabled", lambda: True)
    monkeypatch.setattr(lease_mod, "get_redis", lambda: redis)
    # Another process holds this script's lease.
    redis.data[f"{lease_mod.LEASE_PREFIX}{do_lock.LEASE_NAME_PREFIX}s1"] = "elsewhere"
    with pytest.raises(ValidationError) as exc:
        async with do_lock.script_lock("s1", wait_s=0.3):
            pass
    assert exc.value.code == "sites.do_busy"
    redis.data.clear()
    async with do_lock.script_lock("s1", wait_s=0.3):
        assert any(k.endswith("s1") for k in redis.data)
    assert redis.data == {}  # released


@pytest.mark.asyncio
async def test_the_live_push_runs_under_the_script_lock(monkeypatch):
    held: list[str] = []
    real = do_lock.script_lock

    def spy(script, **kw):
        held.append(script)
        return real(script, **kw)

    monkeypatch.setattr(do_lock, "script_lock", spy)
    await dobj.set_platform_vars_live(_SettingsCF(), "s9", target="account", values={"A_VAR": "1"})
    assert held == ["s9"]


@pytest.mark.asyncio
async def test_the_sweep_repushes_the_current_flags_every_run(beanie_test_db):
    await _seed_site("site-a")
    cf = _SweepCF(requests={"ns-a": 1})
    await do_metering.sweep_do_usage(cf=cf, now=DAY)
    sent = {b["name"]: b for b in cf.patches()[0]["bindings"]}
    assert sent["PAW_DO_THROTTLED"]["text"] == "0" and sent["PAW_DO_SUSPENDED"]["text"] == "0"


@pytest.mark.asyncio
async def test_publish_deploys_under_the_script_lock(beanie_test_db, monkeypatch):
    from tests.ee.sites.test_durable_objects_lifecycle import DO_BASE, _do_manifest, _DOFake, _first
    from tests.ee.sites.test_project_d1 import _publish

    held: list[str] = []
    real = do_lock.script_lock

    def spy(script, **kw):
        held.append(script)
        return real(script, **kw)

    monkeypatch.setattr(do_lock, "script_lock", spy)
    (pocket_id, site_id), source = await _first(monkeypatch, _do_manifest(DO_BASE, V1))
    await _publish(pocket_id, site_id, source, _DOFake().client())
    assert held == [site_id]


# --------------------------------------------------------------------- L9


@pytest.mark.asyncio
async def test_metering_gets_loud_after_repeated_failures(beanie_test_db, monkeypatch, caplog):
    from pocketpaw_ee.cloud.models.site import Site

    monkeypatch.setenv(do_metering.FAILURE_ALERT_ENV, "2")
    site = await _seed_site("site-a")
    caplog.set_level(logging.WARNING)
    fail = _SweepCF(requests={"ns-a": 999})
    fail.fail = {"requests"}
    await do_metering.sweep_do_usage(cf=fail, now=DAY)
    assert do_metering.metering_status()["consecutive_failures"] == 1
    assert "Analytics" not in caplog.text
    do_metering._state["last_usage_run"] = None
    with pytest.raises(do_metering.DoMeteringUnhealthy):
        await do_metering.sweep_do_usage(cf=fail, now=DAY + timedelta(hours=2))
    assert "Account Analytics: Read" in caplog.text
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    status = do_metering.metering_status()
    assert status["consecutive_failures"] == 2 and status["last_error"]
    assert (await Site.get(site.id)).do_throttled is False  # never on missing data
    do_metering._state["last_usage_run"] = None
    await do_metering.sweep_do_usage(
        cf=_SweepCF(requests={"ns-a": 1}), now=DAY + timedelta(hours=4)
    )
    assert do_metering.metering_status()["consecutive_failures"] == 0


# -------------------------------------------------------------------- L10


class _TagCF:
    def __init__(self, tag: str | None, bindings: list[dict], *, fail: bool = False):
        self.tag, self.bindings, self.fail = tag, bindings, fail

    async def get_script_migration_tag(self, script, *, target):
        if self.fail:
            raise ValidationError("sites.cloudflare_error", "Cloudflare API 500")
        return self.tag

    async def get_script_settings(self, script, *, target):
        return {"bindings": self.bindings}


ROOM_B = {"type": "durable_object_namespace", "name": "ROOM", "class_name": "Room"}
CHAT_B = {"type": "durable_object_namespace", "name": "CHAT", "class_name": "Chat"}


@pytest.mark.asyncio
async def test_reconcile_keeps_agreeing_state():
    stored = dobj.DurableObjectState.from_history(["v1", "v2"], ["Room", "Chat"])
    got = await dobj.reconcile_state(
        _TagCF("v2", [ROOM_B, CHAT_B]), "s", target="account", stored=stored
    )
    assert got == stored


@pytest.mark.asyncio
async def test_reconcile_trusts_cloudflare_when_the_stored_state_was_lost():
    got = await dobj.reconcile_state(
        _TagCF("v2", [ROOM_B, CHAT_B]),
        "s",
        target="account",
        stored=dobj.DurableObjectState(),
    )
    assert got.migration_tag == "v2" and got.applied_tags == ("v2",)
    assert set(got.live_classes) == {"Room", "Chat"}  # so a delete still needs confirming


@pytest.mark.asyncio
async def test_reconcile_truncates_a_stored_history_ahead_of_cloudflare():
    stored = dobj.DurableObjectState.from_history(["v1", "v2"], ["Room", "Chat"])
    got = await dobj.reconcile_state(_TagCF("v1", [ROOM_B]), "s", target="account", stored=stored)
    assert got.applied_tags == ("v1",) and "Room" in got.live_classes


@pytest.mark.asyncio
async def test_reconcile_with_no_tag_on_cloudflare_is_fresh():
    stored = dobj.DurableObjectState.from_history(["v1"], ["Room"])
    got = await dobj.reconcile_state(_TagCF(None, []), "s", target="account", stored=stored)
    assert got == dobj.DurableObjectState()


@pytest.mark.asyncio
async def test_reconcile_fails_closed_when_cloudflare_cannot_be_read():
    with pytest.raises(ValidationError) as exc:
        await dobj.reconcile_state(
            _TagCF("v1", [], fail=True),
            "s",
            target="account",
            stored=dobj.DurableObjectState.from_history(["v1"], ["Room"]),
        )
    assert exc.value.code == "sites.do_state_unknown"


@pytest.mark.asyncio
async def test_a_lost_state_still_needs_confirmation_to_delete(do_build):  # noqa: F811
    # Cloudflare has v1 (Room) and v2 (Chat); the site doc lost both. The build
    # deletes Chat: reconciled state must still ask for confirmation.
    manifest = json.loads((do_build / "paw-build.json").read_text())
    manifest["durableObjects"]["migrations"] = [
        {"tag": "v1", "new_sqlite_classes": ["Room"]},
        {"tag": "v2", "new_sqlite_classes": ["Chat"]},
        {"tag": "v3", "deleted_classes": ["Chat"]},
    ]
    (do_build / "paw-build.json").write_text(json.dumps(manifest))
    tag_cf = _TagCF("v2", [ROOM_B, CHAT_B])

    async def state():
        return await dobj.reconcile_state(
            tag_cf, "s", target="dispatch", stored=dobj.DurableObjectState()
        )

    with pytest.raises(ValidationError) as exc:
        await bundle_deploy.deploy_bundle(
            _CF().client(),
            script_name="s",
            build_dir=do_build,
            salt="ws",
            paid=True,
            do_state=state,
        )
    assert exc.value.code == "sites.do_data_loss_unconfirmed"
    assert exc.value.details == {"classes": ["Chat"]}


@pytest.mark.asyncio
async def test_persisting_do_state_retries():
    from pocketpaw_ee.sites import service as sites_service

    calls: list[dict] = []

    class _Doc:
        async def set(self, data):
            calls.append(data)
            if len(calls) < 3:
                raise RuntimeError("mongo blip")

    await sites_service._persist_do_state(_Doc(), ("v1",), ("Room",), delay=0)
    assert len(calls) == 3 and calls[-1] == {"do_migration_tags": ["v1"], "do_classes": ["Room"]}

    class _Down:
        async def set(self, data):
            raise RuntimeError("down")

    # Never raises: the next publish reconciles from Cloudflare.
    await sites_service._persist_do_state(_Down(), ("v1",), ("Room",), delay=0)


# ------------------------------------------------------- room / site caps


@pytest.mark.parametrize(
    "env,paid,expected",
    [
        ({}, False, (5, 30)),
        ({}, True, (20, 200)),
        (
            {"PAW_SITES_DO_ROOM_MAX_ROOMS_PAID": "7", "PAW_SITES_DO_SITE_MAX_PEERS_PAID": "70"},
            True,
            (7, 70),
        ),
        (
            {
                "PAW_SITES_DO_ROOM_MAX_ROOMS_PAID": "9999",
                "PAW_SITES_DO_SITE_MAX_PEERS_PAID": "9999",
            },
            True,
            (200, 1000),
        ),
        (
            {"PAW_SITES_DO_ROOM_MAX_ROOMS_PAID": "0", "PAW_SITES_DO_SITE_MAX_PEERS_PAID": "-4"},
            True,
            (1, 1),
        ),
        (
            {"PAW_SITES_DO_ROOM_MAX_ROOMS_PAID": "x", "PAW_SITES_DO_SITE_MAX_PEERS_PAID": "y"},
            True,
            (20, 200),
        ),
        # Free ignores the paid knobs.
        (
            {"PAW_SITES_DO_ROOM_MAX_ROOMS_PAID": "7", "PAW_SITES_DO_SITE_MAX_PEERS_PAID": "70"},
            False,
            (5, 30),
        ),
    ],
)
def test_room_and_site_peer_caps(monkeypatch, env, paid, expected):
    for key in ("PAW_SITES_DO_ROOM_MAX_ROOMS_PAID", "PAW_SITES_DO_SITE_MAX_PEERS_PAID"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert (dobj.room_max_rooms(paid=paid), dobj.site_max_peers(paid=paid)) == expected
    plain = dobj.platform_vars(paid=paid, throttled=False)
    assert plain["ROOM_MAX_ROOMS"] == str(expected[0])
    assert plain["ROOM_MAX_SITE_PEERS"] == str(expected[1])


@pytest.mark.asyncio
async def test_a_do_deploy_carries_the_room_and_site_caps(do_build):  # noqa: F811
    cf = _CF(tag="v1")
    await bundle_deploy.deploy_bundle(
        cf.client(), script_name="s", build_dir=do_build, salt="ws", paid=True
    )
    plain = _plain(_metadata(next(r for r in cf.requests if r.method == "PUT")))
    assert plain["ROOM_MAX_ROOMS"] == "20" and plain["ROOM_MAX_SITE_PEERS"] == "200"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_drafts")
async def test_a_draft_carries_the_room_and_site_caps():
    store, records, cf, registry = Store(), _Records(), FakeCF(), draft_worker.MemoryRegistry()
    await _build(store, records, cf, registry, manifest=_draft_manifest(V1))
    put = cf.named("put_worker")[-1]
    plain = {b["name"]: b["text"] for b in put["bindings"] if b["type"] == "plain_text"}
    assert plain["ROOM_MAX_ROOMS"] == "5" and plain["ROOM_MAX_SITE_PEERS"] == "30"
