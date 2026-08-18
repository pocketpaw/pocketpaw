# tests/cloud/ship/test_ship_source.py — the SHIP-14 deploy-source surface at
# both seams: the ``ship.store`` encrypted repo-token envelope and the HTTP
# routes (``POST /ship/apps`` with a source, ``PUT /ship/apps/{id}/source``).
#
# Pins the security invariants that mirror the SHIP-9 env store: the private-repo
# token is Fernet-encrypted at rest (the raw Mongo doc never carries the
# plaintext), ``decrypt_repo_token`` is the sole round-trip, ``AppOut`` has NO
# token field, and every source route is workspace-scoped.
#
# Created 2026-07-23 (feat/ship-14-source-deploy, SHIP-14): new module.

from __future__ import annotations

import json

from pocketpaw_ee.cloud.ship import store

from tests.cloud.ship.conftest import _app_on_box, _ready_box

# A long, unique token so a substring can't accidentally collide with anything.
TOKEN = "ghp_DoNotLeakThisPrivateRepoTokenBody-9f8e7d6c5b4a3f21ZZ"
REPO = "https://github.com/paw-demo/private-app.git"


async def _make_app(workspace="w1"):
    box = await store.create_provisioning_box(
        workspace_id=workspace,
        provider="hcloud",
        server_type="cx22",
        region="fsn1",
        ssh_private_key="-----BEGIN KEY-----\nx\n-----END KEY-----\n",
        ssh_public_key="ssh-ed25519 AAAA test",
    )
    return await store.create_app(
        workspace_id=workspace,
        box_id=str(box.id),
        name="demo",
        build_path="dockerfile",
        git_ref="",
        image="",
        env_refs=[],
        prod=False,
    )


# ---------------------------------------------------------------------------
# Store — the encrypted repo-token envelope (mirrors decrypt_ssh_key / env)
# ---------------------------------------------------------------------------


async def test_repo_token_is_encrypted_at_rest_and_round_trips(mongo_db, enc_key):  # noqa: ARG001
    app = await _make_app()

    app = await store.set_app_source(
        app, source_kind="git", repo_url=REPO, repo_ref="main", repo_token=TOKEN
    )

    # The plaintext token must appear NOWHERE in what is persisted.
    reloaded = await store.get_app("w1", str(app.id))
    assert reloaded is not None
    assert TOKEN not in reloaded.model_dump_json()

    # Read the RAW Mongo document — no Beanie layer — to prove it at rest.
    raw = await mongo_db["ship_apps"].find_one({"_id": app.id})
    assert raw is not None
    assert TOKEN not in json.dumps(raw, default=str)
    # It IS stored — as ciphertext, which decrypts back to the plaintext.
    assert raw["repo_token_enc"] and raw["repo_token_enc"] != TOKEN
    assert store.decrypt_repo_token(reloaded) == TOKEN
    # The non-secret source facts are stored in the clear.
    assert (reloaded.source_kind, reloaded.repo_url, reloaded.repo_ref) == ("git", REPO, "main")


async def test_set_source_token_none_preserves_empty_clears(mongo_db, enc_key):  # noqa: ARG001
    app = await _make_app()
    app = await store.set_app_source(
        app, source_kind="git", repo_url=REPO, repo_ref="main", repo_token=TOKEN
    )

    # token=None on a re-point LEAVES the stored credential untouched.
    app = await store.set_app_source(
        app, source_kind="git", repo_url=REPO, repo_ref="release", repo_token=None
    )
    reloaded = await store.get_app("w1", str(app.id))
    assert store.decrypt_repo_token(reloaded) == TOKEN
    assert reloaded.repo_ref == "release"

    # An EMPTY token CLEARS it (a public repo) — ciphertext gone, decrypt is "".
    app = await store.set_app_source(
        app, source_kind="git", repo_url=REPO, repo_ref="release", repo_token=""
    )
    reloaded = await store.get_app("w1", str(app.id))
    assert reloaded.repo_token_enc == ""
    assert store.decrypt_repo_token(reloaded) == ""


async def test_create_app_encrypts_the_repo_token(mongo_db, enc_key):  # noqa: ARG001
    box = await store.create_provisioning_box(
        workspace_id="w1",
        provider="hcloud",
        server_type="cx22",
        region="fsn1",
        ssh_private_key="-----BEGIN KEY-----\nx\n-----END KEY-----\n",
        ssh_public_key="ssh-ed25519 AAAA test",
    )
    app = await store.create_app(
        workspace_id="w1",
        box_id=str(box.id),
        name="demo",
        build_path="dockerfile",
        git_ref="",
        image="",
        env_refs=[],
        prod=False,
        source_kind="git",
        repo_url=REPO,
        repo_ref="main",
        repo_token=TOKEN,
    )
    raw = await mongo_db["ship_apps"].find_one({"_id": app.id})
    assert TOKEN not in json.dumps(raw, default=str)
    assert store.decrypt_repo_token(await store.get_app("w1", str(app.id))) == TOKEN


# ---------------------------------------------------------------------------
# HTTP — POST /ship/apps with a source, PUT /ship/apps/{id}/source
# ---------------------------------------------------------------------------


async def test_create_app_with_git_source_masks_the_token(w1, mongo_db):
    box_id = await _ready_box(w1)

    resp = await w1.post(
        "/ship/apps",
        json={
            "name": "gitapp",
            "box_id": box_id,
            "source_kind": "git",
            "repo_url": REPO,
            "repo_ref": "main",
            "token": TOKEN,
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["source_kind"], body["repo_url"], body["repo_ref"]) == ("git", REPO, "main")
    # The token is never echoed — no field carries it, and it is nowhere in the body.
    assert "token" not in body
    assert TOKEN not in resp.text
    # ...and it is encrypted at rest.
    app = await store.get_app("w1", body["id"])
    assert store.decrypt_repo_token(app) == TOKEN
    raw = await mongo_db["ship_apps"].find_one({"_id": app.id})
    assert TOKEN not in json.dumps(raw, default=str)


async def test_set_source_route_points_the_app_at_a_repo(w1, mongo_db):
    app_id = await _app_on_box(w1, await _ready_box(w1))

    resp = await w1.put(
        f"/ship/apps/{app_id}/source",
        json={"source_kind": "git", "repo_url": REPO, "repo_ref": "main", "token": TOKEN},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["source_kind"], body["repo_url"]) == ("git", REPO)
    assert "token" not in body
    assert TOKEN not in resp.text
    # Encrypted at rest, decryptable through the sole seam.
    app = await store.get_app("w1", app_id)
    assert store.decrypt_repo_token(app) == TOKEN
    raw = await mongo_db["ship_apps"].find_one({"_id": app.id})
    assert TOKEN not in json.dumps(raw, default=str)


async def test_set_source_git_without_repo_url_is_422(w1):
    app_id = await _app_on_box(w1, await _ready_box(w1))

    resp = await w1.put(
        f"/ship/apps/{app_id}/source",
        json={"source_kind": "git", "repo_url": "", "token": TOKEN},
    )

    assert resp.status_code == 422


async def test_set_source_is_workspace_scoped(w1, w2):
    """A cross-tenant PUT /source 404s and never touches the victim's app."""
    app_id = await _app_on_box(w1, await _ready_box(w1))

    resp = await w2.put(
        f"/ship/apps/{app_id}/source",
        json={"source_kind": "git", "repo_url": REPO, "token": TOKEN},
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "ship.app.not_found"
    # The victim's app is untouched — still the default image source.
    app = await store.get_app("w1", app_id)
    assert app.source_kind == "image"
    assert store.decrypt_repo_token(app) == ""
