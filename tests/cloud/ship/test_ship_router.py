# tests/cloud/ship/test_ship_router.py — the /api/v1/ship HTTP surface (SHIP-3).
#
# Drives all thirteen routes through a real FastAPI app against a real Beanie
# store (mongomock) with the ShipEngine FAKED by SHIP-1's transcript-replaying
# transport — zero network, zero box. Three things get pinned here:
#
#   1. TENANCY. Every read is workspace-filtered, and a cross-tenant request
#      404s — not 200, not 500. The parametrized sweep covers every id-bearing
#      route so a future route can't quietly skip the filter.
#   2. THE FROZEN WIRE SHAPES. The /ship console consumes BoxOut / AppOut /
#      DeployOut / LogsOut / MetricsOut and the delete envelope verbatim; the
#      key sets are asserted exactly, so a rename breaks here first.
#   3. DESTROY IS NEVER EXECUTED. A DELETE parks a proposal and issues no
#      engine command at all.
#
# Created 2026-07-22 (feat/ship-3-cloud-entity, SHIP-3): new module.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.ship import store

from tests.cloud.ship.conftest import (
    APP,
    DOMAIN,
    SECRET_MARKERS,
    SERVICE,
    _app_on_box,
    _ready_box,
    install_fake_engine,
)

# ---------------------------------------------------------------------------
# Boxes
# ---------------------------------------------------------------------------


async def test_create_box_returns_frozen_shape_and_enqueues(w1, arq_pool):
    resp = await w1.post("/ship/boxes", json={"provider": "hcloud"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"id", "provider", "ip", "status", "price_monthly"}
    assert body["provider"] == "hcloud"
    assert body["status"] == "provisioning"

    # The provision was handed to the worker, positionally, with the box id.
    (args, kwargs) = arq_pool.enqueued[0]
    assert args == ("provision_box_job", body["id"], "w1")
    assert kwargs == {}


async def test_create_box_uses_default_server_type_and_region(w1):
    resp = await w1.post("/ship/boxes", json={})
    box = await store.get_box("w1", resp.json()["id"])
    assert box is not None
    assert (box.server_type, box.region) == ("cx22", "fsn1")


async def test_create_box_honors_explicit_shape(w1):
    resp = await w1.post(
        "/ship/boxes", json={"provider": "hcloud", "server_type": "cx32", "region": "nbg1"}
    )
    box = await store.get_box("w1", resp.json()["id"])
    assert box is not None
    assert (box.server_type, box.region) == ("cx32", "nbg1")


async def test_list_boxes_is_workspace_scoped(w1, w2):
    await w1.post("/ship/boxes", json={})

    assert len((await w1.get("/ship/boxes")).json()) == 1
    assert (await w2.get("/ship/boxes")).json() == []


async def test_box_metrics_reads_live_percentages(w1, monkeypatch):
    install_fake_engine(monkeypatch)
    box_id = await _ready_box(w1)

    resp = await w1.get(f"/ship/boxes/{box_id}/metrics")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"cpu", "mem", "disk"}
    # box_metrics.txt: load 0.42 over 2 cores, 37.5% memory, 23% root fs.
    assert body == {"cpu": 21.0, "mem": 37.5, "disk": 23.0}


async def test_box_metrics_refuses_a_box_that_is_not_ready(w1, monkeypatch):
    install_fake_engine(monkeypatch)
    resp = await w1.post("/ship/boxes", json={})

    metrics = await w1.get(f"/ship/boxes/{resp.json()['id']}/metrics")

    assert metrics.status_code == 409
    assert metrics.json()["error"]["code"] == "ship.box_not_ready"


# ---------------------------------------------------------------------------
# Apps
# ---------------------------------------------------------------------------


async def test_create_and_list_apps(w1):
    box_id = await _ready_box(w1)

    created = await w1.post("/ship/apps", json={"name": APP, "box_id": box_id})

    assert created.status_code == 200, created.text
    body = created.json()
    assert set(body) == {"id", "name", "box_id", "status", "urls"}
    assert (body["name"], body["box_id"], body["status"], body["urls"]) == (
        APP,
        box_id,
        "created",
        [],
    )

    listed = (await w1.get("/ship/apps")).json()
    assert [a["id"] for a in listed] == [body["id"]]
    assert (await w1.get(f"/ship/apps?box_id={box_id}")).json() == listed


async def test_list_apps_filters_by_box(w1):
    box_a = await _ready_box(w1)
    box_b = await _ready_box(w1)
    await w1.post("/ship/apps", json={"name": "app-a", "box_id": box_a})
    await w1.post("/ship/apps", json={"name": "app-b", "box_id": box_b})

    only_b = (await w1.get(f"/ship/apps?box_id={box_b}")).json()

    assert [a["name"] for a in only_b] == ["app-b"]


async def test_duplicate_app_name_on_the_same_box_conflicts(w1):
    box_id = await _ready_box(w1)
    await w1.post("/ship/apps", json={"name": APP, "box_id": box_id})

    again = await w1.post("/ship/apps", json={"name": APP, "box_id": box_id})

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "ship.app_exists"


async def test_create_app_on_a_foreign_box_404s(w1, w2):
    box_id = await _ready_box(w1)

    resp = await w2.post("/ship/apps", json={"name": APP, "box_id": box_id})

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "ship.box.not_found"


# ---------------------------------------------------------------------------
# Deploys
# ---------------------------------------------------------------------------


async def test_deploy_enqueues_and_is_immediately_pollable(w1, arq_pool):
    box_id = await _ready_box(w1)
    app_id = await _app_on_box(w1, box_id)

    resp = await w1.post(f"/ship/apps/{app_id}/deploy")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"id", "app_id", "status", "started_at", "finished_at"}
    assert (body["app_id"], body["status"]) == (app_id, "queued")
    assert body["started_at"] is not None
    assert body["finished_at"] is None

    assert arq_pool.enqueued[-1] == (("deploy_app_job", body["id"], "w1"), {})
    # The app itself reports the in-flight deploy.
    assert (await w1.get("/ship/apps")).json()[0]["status"] == "deploying"

    listed = (await w1.get(f"/ship/apps/{app_id}/deploys")).json()
    assert [d["id"] for d in listed] == [body["id"]]


async def test_deploy_transitions_are_observable_through_the_deploys_route(w1, monkeypatch):
    """The acceptance criterion: run the job, poll the route, see the walk end."""
    from pocketpaw_ee.cloud.ship import deploy_job

    install_fake_engine(monkeypatch)
    box_id = await _ready_box(w1)
    app_id = await _app_on_box(w1, box_id)
    deploy_id = (await w1.post(f"/ship/apps/{app_id}/deploy")).json()["id"]

    assert (await w1.get(f"/ship/apps/{app_id}/deploys")).json()[0]["status"] == "queued"

    result = await deploy_job.deploy_app_job({}, deploy_id, "w1")

    assert result == {"ok": True, "status": "live"}
    landed = (await w1.get(f"/ship/apps/{app_id}/deploys")).json()[0]
    assert landed["status"] == "live"
    assert landed["finished_at"] is not None
    # ...and the app picked up the engine-reported URL.
    assert (await w1.get("/ship/apps")).json()[0] == {
        "id": app_id,
        "name": APP,
        "box_id": box_id,
        "status": "live",
        "urls": ["http://demo.paw.example"],
    }


async def test_deploy_without_an_image_is_rejected(w1):
    box_id = await _ready_box(w1)
    resp = await w1.post("/ship/apps", json={"name": APP, "box_id": box_id})

    deploy = await w1.post(f"/ship/apps/{resp.json()['id']}/deploy")

    assert deploy.status_code == 422
    assert deploy.json()["error"]["code"] == "ship.app_no_image"


# ---------------------------------------------------------------------------
# Domains, database, logs
# ---------------------------------------------------------------------------


async def test_add_and_list_domains(w1, monkeypatch):
    install_fake_engine(monkeypatch)
    app_id = await _app_on_box(w1, await _ready_box(w1))

    added = await w1.post(f"/ship/apps/{app_id}/domains", json={"domain": DOMAIN})

    assert added.status_code == 200, added.text
    assert added.json() == {"domain": DOMAIN, "tls_enabled": True}

    listed = await w1.get(f"/ship/apps/{app_id}/domains")
    assert listed.json() == {"domains": [{"domain": DOMAIN, "tls_enabled": True}]}
    # The HTTPS URL is now on the app.
    assert f"https://{DOMAIN}" in (await w1.get("/ship/apps")).json()[0]["urls"]


async def test_db_create_returns_the_env_var_name_never_the_dsn(w1, monkeypatch):
    install_fake_engine(monkeypatch)
    app_id = await _app_on_box(w1, await _ready_box(w1))

    resp = await w1.post(f"/ship/apps/{app_id}/db")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"service": SERVICE, "linked_app": APP, "env_var": "MONGO_URL"}
    # The transcripts print the full DSN; it must not reach the wire or the doc.
    app_doc = await store.get_app("w1", app_id)
    assert app_doc is not None
    for marker in SECRET_MARKERS:
        assert marker not in resp.text
        assert marker not in app_doc.model_dump_json()


async def test_logs_returns_the_frozen_lines_shape(w1, monkeypatch):
    install_fake_engine(monkeypatch)
    app_id = await _app_on_box(w1, await _ready_box(w1))

    resp = await w1.get(f"/ship/apps/{app_id}/logs")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"lines"}
    assert len(body["lines"]) == 3
    assert body["lines"][-1].endswith("GET /health 200")


# ---------------------------------------------------------------------------
# Deletes — parked, never executed
# ---------------------------------------------------------------------------


async def test_delete_box_parks_a_proposal_and_destroys_nothing(w1, monkeypatch):
    issued = install_fake_engine(monkeypatch)
    box_id = await _ready_box(w1)

    resp = await w1.delete(f"/ship/boxes/{box_id}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"status", "proposal_id"}
    assert body["status"] == "pending_approval"
    assert body["proposal_id"]

    box = await store.get_box("w1", box_id)
    assert box is not None
    assert box.pending_destroy_proposal_id == body["proposal_id"]
    # Still ready — parking a teardown does not change what the box IS...
    assert box.status == "ready"
    # ...and no engine command ran at all, least of all a destroy.
    assert issued == []


async def test_delete_app_parks_a_proposal_and_destroys_nothing(w1, monkeypatch):
    issued = install_fake_engine(monkeypatch)
    app_id = await _app_on_box(w1, await _ready_box(w1))

    resp = await w1.delete(f"/ship/apps/{app_id}")

    assert resp.json()["status"] == "pending_approval"
    app_doc = await store.get_app("w1", app_id)
    assert app_doc is not None
    assert app_doc.pending_destroy_proposal_id == resp.json()["proposal_id"]
    assert app_doc.status == "created"
    assert issued == []


async def test_repeated_delete_returns_the_same_proposal(w1):
    app_id = await _app_on_box(w1, await _ready_box(w1))

    first = (await w1.delete(f"/ship/apps/{app_id}")).json()["proposal_id"]
    second = (await w1.delete(f"/ship/apps/{app_id}")).json()["proposal_id"]

    assert first == second


# ---------------------------------------------------------------------------
# Tenancy — every id-bearing route
# ---------------------------------------------------------------------------


async def test_cross_tenant_box_routes_404(w1, w2, monkeypatch):
    install_fake_engine(monkeypatch)
    box_id = await _ready_box(w1)

    assert (await w2.get(f"/ship/boxes/{box_id}/metrics")).status_code == 404
    assert (await w2.delete(f"/ship/boxes/{box_id}")).status_code == 404
    # ...and the foreign tenant's DELETE did not park anything on w1's box.
    box = await store.get_box("w1", box_id)
    assert box is not None
    assert box.pending_destroy_proposal_id is None


@pytest.mark.parametrize(
    ("method", "suffix", "payload"),
    [
        ("post", "/deploy", None),
        ("get", "/deploys", None),
        ("post", "/domains", {"domain": DOMAIN}),
        ("get", "/domains", None),
        ("post", "/db", {}),
        ("get", "/logs", None),
        ("delete", "", None),
    ],
)
async def test_cross_tenant_app_routes_404(w1, w2, monkeypatch, method, suffix, payload):
    install_fake_engine(monkeypatch)
    app_id = await _app_on_box(w1, await _ready_box(w1))

    call = getattr(w2, method)
    resp = (
        await call(f"/ship/apps/{app_id}{suffix}", json=payload)
        if payload
        else await call(f"/ship/apps/{app_id}{suffix}")
    )

    assert resp.status_code == 404, f"{method.upper()} {suffix} leaked: {resp.status_code}"
    assert resp.json()["error"]["code"] == "ship.app.not_found"


async def test_a_malformed_id_reads_as_not_found_not_a_crash(w1):
    resp = await w1.get("/ship/apps/not-an-object-id/deploys")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Input constraints at the DTO boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "Demo App", "box_id": "x"},  # spaces / capitals aren't Dokku names
        {"name": "-leading-hyphen", "box_id": "x"},
        {"name": "demo", "box_id": "x", "env_refs": ["API_KEY=hunter2"]},  # a VALUE
        {"name": "demo", "box_id": "x", "build_path": "bazel"},
        # Shell metacharacters in the build inputs. The driver shell-quotes
        # these, so this is defence in depth — but an image reference is a
        # constrained grammar, so a value carrying `;`/`$()`/backticks is
        # refused at the boundary rather than surviving to an SSH round trip.
        {"name": "demo", "box_id": "x", "image": "alpine; rm -rf /"},
        {"name": "demo", "box_id": "x", "image": "alpine$(id)"},
        {"name": "demo", "box_id": "x", "git_ref": "main`id`"},
    ],
)
async def test_create_app_rejects_unusable_input(w1, payload):
    resp = await w1.post("/ship/apps", json=payload)

    assert resp.status_code == 422


@pytest.mark.parametrize(
    "image,git_ref",
    [
        ("", ""),  # the "not specified yet" body
        ("alpine:3.20", "main"),
        ("ghcr.io/owner/repo@sha256:abc123", "v1.2.3"),
    ],
)
async def test_create_app_accepts_real_build_inputs(w1, image, git_ref):
    """The tightened image/git_ref patterns must not reject legitimate refs."""
    box_id = await _ready_box(w1)
    resp = await w1.post(
        "/ship/apps",
        json={"name": "demo", "box_id": box_id, "image": image, "git_ref": git_ref},
    )

    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("domain", ["not a domain", "-bad.example.com", "localhost"])
async def test_add_domain_rejects_a_non_hostname(w1, domain):
    app_id = await _app_on_box(w1, await _ready_box(w1))

    resp = await w1.post(f"/ship/apps/{app_id}/domains", json={"domain": domain})

    assert resp.status_code == 422


async def test_metrics_on_an_unreachable_box_is_a_409_not_a_500(w1, monkeypatch):
    from tests.cloud.ship.conftest import install_refused_engine

    box_id = await _ready_box(w1)
    install_refused_engine(monkeypatch)

    resp = await w1.get(f"/ship/boxes/{box_id}/metrics")

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "ship.metrics_failed"
    # The box's address is not published in the error body.
    assert "203.0.113.9" not in resp.text
