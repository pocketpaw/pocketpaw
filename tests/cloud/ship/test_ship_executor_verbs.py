# tests/cloud/ship/test_ship_executor_verbs.py — the REAL body of
# ``ship.executor._run_verb``, the code an approved Instinct proposal runs.
#
# WHY THIS EXISTS. tests/cloud/ship/test_instinct_gate.py monkeypatches
# ``_run_verb`` out entirely (its ``ran`` fixture), so it proves the GATE
# (locking, RBAC re-check, idempotency, never-raises) while never executing the
# verbs themselves. Three defects lived in that gap, all on the path a human
# explicitly approved:
#
#   * ``deploy_app`` built ``DeployRequest(app=app.name, ...)`` — a ``str``
#     where the port requires an ``AppSpec`` — so the driver raised
#     AttributeError, the blanket handler reported "engine call failed
#     (AttributeError)", and NO approved production deploy could ever succeed.
#     The same branch never decrypted the app's env and had no git-source path,
#     so a prod app deploying from a repo could not deploy at all.
#   * ``destroy_app`` wrote the status ``"destroyed"``, which was not a member
#     of ``ShipAppStatus``. Beanie does not validate on assignment, so the doc
#     persisted out-of-vocabulary and every later READ of that workspace's app
#     list raised ValidationError — one torn-down app made the whole list
#     unreadable through the API.
#   * ``destroy_box`` destroyed every app's container but left the app ROWS
#     untouched, so the console kept listing them live with reachable URLs.
#
# These tests call ``_run_verb`` directly against the transcript-replay engine,
# so the verbs' real bodies run.
#
# Created 2026-07-29 (fix/ship-review-p0): new module.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.ship import executor as ship_executor
from pocketpaw_ee.cloud.ship import store

from tests.cloud.ship.conftest import APP, IMAGE, install_fake_engine

WS = "w1"


async def _box_and_app(*, prod: bool = False, source_kind: str = "image"):
    box = await store.create_provisioning_box(
        workspace_id=WS,
        provider="hcloud",
        server_type="cx22",
        region="fsn1",
        ssh_private_key="FAKE-KEY",
        ssh_public_key="ssh-ed25519 AAAAFAKE test",
    )
    await store.mark_ready(box, server_id="srv-1", ip="203.0.113.9", price_monthly=8.25)
    app = await store.create_app(
        workspace_id=WS,
        box_id=str(box.id),
        name=APP,
        build_path="dockerfile",
        git_ref="",
        image=IMAGE,
        env_refs=[],
        prod=prod,
    )
    return box, app


# ---------------------------------------------------------------------------
# deploy_app — the verb the gate exists to serve
# ---------------------------------------------------------------------------


async def test_approved_deploy_actually_deploys(mongo_db, enc_key, monkeypatch):  # noqa: ARG001
    """The whole point of the gate: approval must produce a real deploy."""
    install_fake_engine(monkeypatch)
    _box, app = await _box_and_app(prod=True)

    ok, detail = await ship_executor._run_verb(
        {"workspace_id": WS, "verb": "deploy_app", "app_id": str(app.id), "params": {}}
    )

    assert ok is True, f"approved prod deploy failed: {detail}"
    assert IMAGE in detail
    refreshed = await store.get_app(WS, str(app.id))
    assert refreshed is not None and refreshed.image == IMAGE


async def test_approved_deploy_applies_the_apps_encrypted_env(mongo_db, enc_key, monkeypatch):  # noqa: ARG001
    """The approved path must merge env like the queued path does.

    The old copy skipped ``decrypt_app_env`` entirely, so a gated deploy shipped
    the app with no config vars at all.
    """
    issued = install_fake_engine(monkeypatch)
    _box, app = await _box_and_app(prod=True)
    await store.upsert_app_env(
        app,
        [
            store.EnvVarWrite(
                key="API_KEY", masked="…lue", scope="both", value="hunter2-super-secret-value"
            )
        ],
    )

    ok, _detail = await ship_executor._run_verb(
        {"workspace_id": WS, "verb": "deploy_app", "app_id": str(app.id), "params": {}}
    )

    assert ok is True
    assert any("config:set" in c for c in issued), "the app's env never reached the engine"


# ---------------------------------------------------------------------------
# destroy_app / destroy_box — the rows must follow the containers
# ---------------------------------------------------------------------------


async def test_destroyed_app_stays_readable(mongo_db, enc_key, monkeypatch):  # noqa: ARG001
    """A torn-down app must not poison every later read of the app list."""
    install_fake_engine(monkeypatch)
    _box, app = await _box_and_app()

    ok, _ = await ship_executor._run_verb(
        {"workspace_id": WS, "verb": "destroy_app", "app_id": str(app.id), "params": {}}
    )
    assert ok is True

    # The read that used to raise ValidationError once "destroyed" was persisted.
    apps = await store.list_apps(WS)
    assert [a.status for a in apps] == ["destroyed"]


async def test_destroy_box_marks_its_apps_destroyed_too(mongo_db, enc_key, monkeypatch):  # noqa: ARG001
    """Destroying a box must not leave its apps reading live with live URLs."""
    install_fake_engine(monkeypatch)
    box, _app = await _box_and_app()

    ok, _ = await ship_executor._run_verb(
        {"workspace_id": WS, "verb": "destroy_box", "box_id": str(box.id), "params": {}}
    )

    assert ok is True
    assert all(a.status == "destroyed" for a in await store.list_apps(WS))
    refreshed_box = await store.get_box(WS, str(box.id))
    assert refreshed_box is not None and refreshed_box.status == "destroyed"


# ---------------------------------------------------------------------------
# Unknown verbs stay refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["", "shutdown", "rm -rf"])
async def test_unsupported_verbs_are_refused(mongo_db, enc_key, monkeypatch, verb):  # noqa: ARG001
    install_fake_engine(monkeypatch)
    _box, app = await _box_and_app()

    ok, detail = await ship_executor._run_verb(
        {"workspace_id": WS, "verb": verb, "app_id": str(app.id), "params": {}}
    )

    assert ok is False and "unsupported" in detail
