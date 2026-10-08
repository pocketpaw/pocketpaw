# Tests for Durable Objects on project sites, slice 1
# (ee/pocketpaw_ee/sites/durable_objects.py, the bundle_deploy wiring and the
# CloudflareClient additions). Pure parser / plan_migration tests first, then the
# deploy path through an httpx.MockTransport. No real Cloudflare call is made.
from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites import binding_provisioner as bp
from pocketpaw_ee.sites import bundle_deploy
from pocketpaw_ee.sites import durable_objects as dobj
from pocketpaw_ee.sites.cloudflare_client import CloudflareClient
from pocketpaw_ee.sites.project_build import check_plan_allows

ACCT = "acct_1"
API = f"https://api.cloudflare.com/client/v4/accounts/{ACCT}"
OK = {"success": True, "errors": [], "messages": []}


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    """DOs are on unless a test turns the flag off; caps at their defaults."""
    monkeypatch.setenv(dobj.FLAG_ENV, "1")
    for key in (dobj.MAX_CLASSES_ENV, dobj.ACCOUNT_BUDGET_ENV):
        monkeypatch.delenv(key, raising=False)


def _block(**over) -> dict:
    block = {
        "bindings": [{"name": "ROOM", "className": "Room"}],
        "migrations": [{"tag": "v1", "new_sqlite_classes": ["Room"]}],
        "exportedClasses": ["Room"],
    }
    block.update(over)
    return block


def _refused(code: str, block: dict, **kw) -> ValidationError:
    with pytest.raises(ValidationError) as exc:
        dobj.vet_durable_objects({"durableObjects": block}, paid=True, **kw)
    assert exc.value.code == code, exc.value.message
    return exc.value


# ------------------------------------------------------------------ parser


def test_parse_reads_bindings_migrations_and_exports():
    cfg = dobj.parse_durable_objects(_block())
    assert cfg.bindings == {"ROOM": "Room"}
    assert [m.tag for m in cfg.migrations] == ["v1"]
    assert cfg.exported == frozenset({"Room"})
    assert cfg.live_classes == ("Room",)


def test_kv_backed_classes_are_refused():
    err = _refused("sites.do_config", _block(migrations=[{"tag": "v1", "new_classes": ["Room"]}]))
    assert "new_sqlite_classes" in err.message


def test_transferred_classes_are_refused():
    step = {"tag": "v1", "transferred_classes": [{"from": "A", "from_script": "x", "to": "Room"}]}
    _refused("sites.do_config", _block(migrations=[step]))


@pytest.mark.parametrize(
    "field", ["script_name", "scriptName", "environment", "namespace_id", "dispatch_namespace"]
)
def test_cross_script_binding_fields_are_refused(field):
    binding = {"name": "ROOM", "className": "Room", field: "other"}
    err = _refused("sites.do_config", _block(bindings=[binding]))
    assert field in err.message


def test_bound_class_not_exported_is_refused():
    err = _refused("sites.do_class_missing", _block(exportedClasses=[]))
    assert "Room" in err.message


def test_bound_class_without_a_migration_is_refused():
    _refused("sites.do_config", _block(bindings=[{"name": "ROOM", "className": "Other"}]))


@pytest.mark.parametrize(
    "migrations",
    [
        [{"tag": "v1", "new_sqlite_classes": ["Room"]}, {"tag": "v1"}],  # duplicate tag
        [{"tag": "bad tag!", "new_sqlite_classes": ["Room"]}],  # tag shape
        [
            {"tag": "v1", "new_sqlite_classes": ["Room"]},
            {"tag": "v2", "new_sqlite_classes": ["Room"]},
        ],
        [{"tag": "v1", "new_sqlite_classes": ["Room"], "unknown_step": []}],
        [{"tag": "v1", "new_sqlite_classes": ["not a class"]}],
    ],
)
def test_malformed_migrations_are_refused(migrations):
    _refused("sites.do_config", _block(migrations=migrations))


def test_duplicate_binding_names_are_refused():
    two = [{"name": "ROOM", "className": "Room"}, {"name": "ROOM", "className": "Room"}]
    _refused("sites.do_config", _block(bindings=two))


def test_class_cap_by_plan(monkeypatch):
    two = _block(
        bindings=[{"name": "A", "className": "A"}, {"name": "B", "className": "B"}],
        migrations=[{"tag": "v1", "new_sqlite_classes": ["A", "B"]}],
        exportedClasses=["A", "B"],
    )
    # Free: one class.
    with pytest.raises(ValidationError) as exc:
        dobj.vet_durable_objects({"durableObjects": two}, paid=False)
    assert exc.value.code == "sites.do_class_cap"
    assert dobj.vet_durable_objects({"durableObjects": _block()}, paid=False) is not None
    # Paid: default 3, configurable.
    assert dobj.vet_durable_objects({"durableObjects": two}, paid=True) is not None
    monkeypatch.setenv(dobj.MAX_CLASSES_ENV, "1")
    _refused("sites.do_class_cap", two)


def test_flag_off_refuses_a_bundle_that_declares_dos(monkeypatch):
    monkeypatch.setenv(dobj.FLAG_ENV, "0")
    err = _refused("sites.do_disabled", _block())
    assert "Room" in err.message and "Durable Objects" in err.message
    # A `do` binding request alone counts as declaring them.
    with pytest.raises(ValidationError) as exc:
        dobj.vet_durable_objects({"bindingRequests": [{"type": "do", "name": "ROOM"}]}, paid=True)
    assert exc.value.code == "sites.do_disabled"
    # No DOs, no refusal, flag or not.
    assert (
        dobj.vet_durable_objects({"bindingRequests": [{"type": "d1", "name": "DB"}]}, paid=True)
        is None
    )


# ---------------------------------------------------------- plan_migration

V1 = {"tag": "v1", "new_sqlite_classes": ["Room"]}
V2 = {"tag": "v2", "new_sqlite_classes": ["Chat"]}
V3_DELETE = {"tag": "v3", "deleted_classes": ["Chat"]}
V3_RENAME = {"tag": "v3", "renamed_classes": [{"from": "Chat", "to": "Talk"}]}


def _decl(*steps):
    return dobj.parse_durable_objects({"migrations": list(steps)}).migrations


def test_plan_fresh_sends_every_step_with_the_last_tag():
    plan = dobj.plan_migration(_decl(V1, V2), None)
    assert plan.migrations == {
        "new_tag": "v2",
        "steps": [{"new_sqlite_classes": ["Room"]}, {"new_sqlite_classes": ["Chat"]}],
    }
    assert plan.tag == "v2" and plan.new_classes == ("Room", "Chat")


def test_plan_incremental_sends_old_tag_and_only_the_new_steps():
    plan = dobj.plan_migration(_decl(V1, V2), "v1", live_classes=["Room"])
    assert plan.migrations == {
        "old_tag": "v1",
        "new_tag": "v2",
        "steps": [{"new_sqlite_classes": ["Chat"]}],
    }
    assert plan.new_classes == ("Chat",)


def test_plan_up_to_date_sends_no_migrations():
    plan = dobj.plan_migration(_decl(V1, V2), "v2", live_classes=["Room", "Chat"])
    assert plan.migrations is None and plan.tag == "v2" and plan.new_classes == ()


def test_plan_diverged_history_is_refused():
    with pytest.raises(ValidationError) as exc:
        dobj.plan_migration(_decl(V1, V2), "v9-rewritten", live_classes=["Room"])
    assert exc.value.code == "sites.do_history_diverged"
    assert "v9-rewritten" in exc.value.message


@pytest.mark.parametrize("step,classes", [(V3_DELETE, ["Chat"]), (V3_RENAME, ["Chat"])])
def test_plan_destructive_step_needs_confirmation(step, classes):
    declared = _decl(V1, V2, step)
    with pytest.raises(ValidationError) as exc:
        dobj.plan_migration(declared, "v2", live_classes=["Room", "Chat"])
    assert exc.value.code == "sites.do_data_loss_unconfirmed"
    assert "Chat" in exc.value.message
    # A matching confirmation lets it through.
    plan = dobj.plan_migration(declared, "v2", live_classes=["Room", "Chat"], confirm=classes)
    assert plan.migrations["old_tag"] == "v2" and plan.migrations["new_tag"] == "v3"
    # A confirmation for some other class does not.
    with pytest.raises(ValidationError):
        dobj.plan_migration(declared, "v2", live_classes=["Room", "Chat"], confirm=["Room"])


def test_destructive_step_on_a_fresh_script_needs_no_confirmation():
    # Nothing is live yet, so nothing can be lost.
    plan = dobj.plan_migration(_decl(V1, V2, V3_DELETE), None)
    assert plan.tag == "v3"


def test_rollback_that_still_exports_every_live_class_is_allowed():
    plan = dobj.plan_migration(
        _decl(V1),
        "v2",
        live_classes=["Room", "Chat"],
        exported=["Room", "Chat"],
        applied_tags=["v1", "v2"],
    )
    assert plan.migrations is None and plan.tag == "v2" and plan.rollback


def test_rollback_that_drops_a_live_class_is_refused():
    with pytest.raises(ValidationError) as exc:
        dobj.plan_migration(
            _decl(V1),
            "v2",
            live_classes=["Room", "Chat"],
            exported=["Room"],
            applied_tags=["v1", "v2"],
        )
    assert exc.value.code == "sites.do_history_diverged"
    assert "predates" in exc.value.message and "Chat" in exc.value.message


def test_unknown_tag_without_history_is_refused_not_guessed():
    with pytest.raises(ValidationError) as exc:
        dobj.plan_migration(_decl(V1), "v2", live_classes=["Room"], exported=["Room"])
    assert exc.value.code == "sites.do_history_diverged"


# ------------------------------------------------------- deploy + client


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture
def do_build(tmp_path: Path) -> Path:
    _write(tmp_path, "w/index.js", "export class Room {}\nexport default { fetch() {} };\n")
    manifest = {
        "workerModuleDir": "w",
        "mainModule": "index.js",
        "workerModules": ["w/index.js"],
        "compat": {"date": "2026-09-01", "flags": []},
        "bindingRequests": [{"type": "do", "name": "ROOM", "className": "Room"}],
        "durableObjects": _block(),
    }
    _write(tmp_path, "paw-build.json", json.dumps(manifest))
    return tmp_path


def _metadata(request: httpx.Request) -> dict:
    boundary = re.search(r"boundary=(.+)$", request.headers["content-type"]).group(1)
    for chunk in request.content.split(b"--" + boundary.strip('"').encode())[1:-1]:
        head, _, body = chunk[2:].partition(b"\r\n\r\n")
        if b'name="metadata"' in head:
            return json.loads(body[:-2])
    raise AssertionError("no metadata part")


class _CF:
    def __init__(self, *, namespaces: int = 0, list_status: int = 200, tag: str | None = "v1"):
        self.requests: list[httpx.Request] = []
        self.namespaces = namespaces
        self.list_status = list_status
        self.tag = tag

    def handler(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        if request.url.path.endswith("/workers/durable_objects/namespaces"):
            if self.list_status != 200:
                return httpx.Response(self.list_status, json={"success": False, "errors": []})
            rows = [
                {"id": f"ns{i}", "class": "X", "script": f"s{i}"} for i in range(self.namespaces)
            ]
            return httpx.Response(200, json={**OK, "result": rows})
        if request.method == "PUT":
            result = {"id": "site_1"} | ({"migration_tag": self.tag} if self.tag else {})
            return httpx.Response(200, json={**OK, "result": result})
        if request.method == "DELETE":
            return httpx.Response(200, json={**OK, "result": None})
        return httpx.Response(404, json={"success": False, "errors": []})

    def client(self) -> CloudflareClient:
        return CloudflareClient(
            account_id=ACCT,
            api_token="tok_1",
            zone_id="zone_1",
            dispatch_namespace="paw-sites",
            _transport=httpx.MockTransport(self.handler),
        )


@pytest.mark.asyncio
async def test_deploy_sends_do_bindings_and_migrations_and_returns_the_tag(do_build):
    cf = _CF(tag="v1")
    result = await bundle_deploy.deploy_bundle(
        cf.client(), script_name="site_1", build_dir=do_build, salt="ws", paid=True
    )
    (put,) = [r for r in cf.requests if r.method == "PUT"]
    meta = _metadata(put)
    assert {"type": "durable_object_namespace", "name": "ROOM", "class_name": "Room"} in meta[
        "bindings"
    ]
    assert meta["migrations"] == {"new_tag": "v1", "steps": [{"new_sqlite_classes": ["Room"]}]}
    assert result.migration_tag == "v1"
    assert result.do_classes == ("Room",)


@pytest.mark.asyncio
async def test_deploy_up_to_date_sends_no_migrations(do_build):
    cf = _CF(tag="v1")
    result = await bundle_deploy.deploy_bundle(
        cf.client(),
        script_name="site_1",
        build_dir=do_build,
        salt="ws",
        paid=True,
        do_state=dobj.DurableObjectState(migration_tag="v1", live_classes=("Room",)),
    )
    meta = _metadata(next(r for r in cf.requests if r.method == "PUT"))
    assert "migrations" not in meta
    assert result.migration_tag == "v1"


@pytest.mark.asyncio
async def test_cloudflare_tag_wins_over_ours(do_build, caplog):
    cf = _CF(tag="cf-says-this")
    result = await bundle_deploy.deploy_bundle(
        cf.client(), script_name="site_1", build_dir=do_build, salt="ws", paid=True
    )
    assert result.migration_tag == "cf-says-this"
    assert "migration tag" in caplog.text


@pytest.mark.asyncio
async def test_flag_off_refuses_the_deploy_before_any_call(do_build, monkeypatch):
    monkeypatch.delenv(dobj.FLAG_ENV)
    cf = _CF()
    called = []

    async def provision(_requests):
        called.append(1)
        return bundle_deploy.ProvisionedResources()

    with pytest.raises(ValidationError) as exc:
        await bundle_deploy.deploy_bundle(
            cf.client(), script_name="s", build_dir=do_build, salt="ws", provision=provision
        )
    assert exc.value.code == "sites.do_disabled"
    assert cf.requests == [] and called == []


@pytest.mark.asyncio
async def test_account_budget_fails_closed_when_the_count_is_unreadable(do_build):
    cf = _CF(list_status=500)
    with pytest.raises(ValidationError) as exc:
        await bundle_deploy.deploy_bundle(
            cf.client(), script_name="s", build_dir=do_build, salt="ws", paid=True, target="account"
        )
    assert exc.value.code == "sites.do_budget_unknown"
    assert all(r.method == "GET" for r in cf.requests)


@pytest.mark.asyncio
async def test_account_budget_refuses_a_new_class_over_the_budget(do_build, monkeypatch):
    monkeypatch.setenv(dobj.ACCOUNT_BUDGET_ENV, "5")
    cf = _CF(namespaces=5)
    with pytest.raises(ValidationError) as exc:
        await bundle_deploy.deploy_bundle(
            cf.client(), script_name="s", build_dir=do_build, salt="ws", paid=True, target="account"
        )
    assert exc.value.code == "sites.do_account_budget"
    # Under the budget it deploys; the dispatch target never reads the list.
    ok = _CF(namespaces=4)
    await bundle_deploy.deploy_bundle(
        ok.client(), script_name="s", build_dir=do_build, salt="ws", paid=True, target="account"
    )
    wfp = _CF(list_status=500)
    await bundle_deploy.deploy_bundle(
        wfp.client(), script_name="s", build_dir=do_build, salt="ws", paid=True
    )
    assert not any("durable_objects" in r.url.path for r in wfp.requests)


@pytest.mark.asyncio
async def test_up_to_date_deploy_skips_the_budget_read(do_build):
    cf = _CF(list_status=500)
    await bundle_deploy.deploy_bundle(
        cf.client(),
        script_name="s",
        build_dir=do_build,
        salt="ws",
        paid=True,
        target="account",
        do_state=dobj.DurableObjectState(migration_tag="v1", live_classes=("Room",)),
    )


@pytest.mark.asyncio
async def test_put_worker_legacy_form_refuses_migrations():
    with pytest.raises(ValidationError):
        await (
            _CF()
            .client()
            .put_worker(
                script_name="s",
                bundle=b"export default {}",
                migrations={"new_tag": "v1", "steps": []},
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["dispatch", "account"])
async def test_script_deletes_can_force(target):
    cf = _CF()
    client = cf.client()
    delete = client.delete_worker if target == "dispatch" else client.delete_account_script
    await delete("s", force=True)
    await delete("s")
    forced, plain = cf.requests
    assert forced.url.params.get("force") == "true"
    assert "force" not in plain.url.params


@pytest.mark.asyncio
async def test_list_durable_object_namespaces():
    rows = await _CF(namespaces=3).client().list_durable_object_namespaces()
    assert [r["id"] for r in rows] == ["ns0", "ns1", "ns2"]


# ------------------------------------------------------ plan + provisioner


def test_free_plan_allows_do_only_with_the_flag(monkeypatch):
    manifest = {
        "workerModules": ["w/index.js"],
        "bindingRequests": [{"type": "do", "name": "ROOM"}],
    }
    check_plan_allows(manifest, paid=False, has_custom_domain=False)
    monkeypatch.setenv(dobj.FLAG_ENV, "0")
    with pytest.raises(ValidationError):
        check_plan_allows(manifest, paid=False, has_custom_domain=False)


@pytest.mark.asyncio
async def test_provisioner_skips_do_with_the_flag_on():
    class _Site:
        id = "65f0c0ffee0000000000abcd"
        kv_namespaces: dict = {}
        r2_buckets: dict = {}
        d1_database_id = ""

    async def _save(_site):
        return None

    res = await bp.ensure_bindings(
        _Site(), [{"type": "do", "name": "ROOM"}], cloudflare=_CF().client(), save=_save, paid=True
    )
    assert res.d1_database_id == ""


def test_the_data_loss_refusal_carries_the_classes_as_details():
    declared = _decl(V1, V2, {"tag": "v3", "deleted_classes": ["Room", "Chat"]})
    with pytest.raises(ValidationError) as exc:
        dobj.plan_migration(declared, "v2", live_classes=["Room", "Chat"])
    assert exc.value.code == "sites.do_data_loss_unconfirmed"
    assert exc.value.details == {"classes": ["Chat", "Room"]}  # sorted, stable
    assert exc.value.to_dict()["error"]["details"] == {"classes": ["Chat", "Room"]}
