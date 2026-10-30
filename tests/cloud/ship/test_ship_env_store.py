# tests/cloud/ship/test_ship_env_store.py — the SHIP-9 encrypted env store, at
# the ``ship.store`` seam (below HTTP).
#
# Pins the security invariants of the store half: a value is Fernet-encrypted
# at rest (the raw Mongo doc never carries the plaintext), ``decrypt_app_env``
# round-trips it, the scope filter picks the right vars for a prod vs preview
# deploy, upsert overwrites, and delete is idempotent.
#
# Created 2026-07-23 (feat/ship-9-env-store, SHIP-9): new module.

from __future__ import annotations

import json

from pocketpaw_ee.cloud.ship import store
from pocketpaw_ee.cloud.ship.store import EnvVarWrite

# A long, unique secret so a substring can't accidentally collide with the
# stored mask (which reveals only the last few chars).
SECRET = "sk-live-DoNotLeakThisSuperSecretTokenBody-9f8e7d6c5b4a3f21"


async def _make_app(workspace="w1", *, prod=False):
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
        image="registry.example/demo:1",
        env_refs=[],
        prod=prod,
    )


async def test_value_is_encrypted_at_rest_and_round_trips(mongo_db, enc_key):  # noqa: ARG001
    app = await _make_app()

    app = await store.upsert_app_env(
        app, [EnvVarWrite(key="API_KEY", masked="…f21", scope="both", value=SECRET)]
    )

    # The plaintext must appear NOWHERE in what is persisted — not in the doc's
    # own serialization, nor in the raw Mongo collection.
    reloaded = await store.get_app("w1", str(app.id))
    assert reloaded is not None
    assert SECRET not in reloaded.model_dump_json()
    assert "DoNotLeakThisSuperSecretTokenBody" not in reloaded.model_dump_json()

    # Read the RAW Mongo document straight off the collection — no Beanie layer.
    raw = await mongo_db["ship_apps"].find_one({"_id": app.id})
    assert raw is not None
    assert SECRET not in json.dumps(raw, default=str)
    # It IS stored — just as ciphertext, which decrypts back to the plaintext.
    assert raw["env_vars"]["API_KEY"]["enc_value"] != SECRET
    assert store.decrypt_app_env(reloaded) == {"API_KEY": SECRET}


async def test_upsert_adds_new_and_overwrites_existing(mongo_db, enc_key):  # noqa: ARG001
    app = await _make_app()

    await store.upsert_app_env(
        app, [EnvVarWrite(key="A", masked="…aaa", scope="both", value="first-aaa")]
    )
    app = await store.get_app("w1", str(app.id))
    app = await store.upsert_app_env(
        app,
        [
            EnvVarWrite(key="A", masked="…bbb", scope="both", value="second-bbb"),
            EnvVarWrite(key="B", masked="…ccc", scope="both", value="brand-ccc"),
        ],
    )

    reloaded = await store.get_app("w1", str(app.id))
    assert set(reloaded.env_vars) == {"A", "B"}
    # A was overwritten, not duplicated.
    assert store.decrypt_app_env(reloaded) == {"A": "second-bbb", "B": "brand-ccc"}
    assert reloaded.env_vars["A"].masked == "…bbb"


async def test_delete_removes_and_is_idempotent(mongo_db, enc_key):  # noqa: ARG001
    app = await _make_app()
    app = await store.upsert_app_env(
        app, [EnvVarWrite(key="A", masked="…aaa", scope="both", value="val-a")]
    )

    app = await store.delete_app_env(app, "A")
    assert "A" not in (await store.get_app("w1", str(app.id))).env_vars
    # Deleting a key that no longer exists is a clean no-op.
    app = await store.delete_app_env(app, "A")
    assert (await store.get_app("w1", str(app.id))).env_vars == {}


async def test_scope_filter_selects_by_deploy_kind(mongo_db, enc_key):  # noqa: ARG001
    prod_app = await _make_app(prod=True)
    await store.upsert_app_env(
        prod_app,
        [
            EnvVarWrite(key="SHARED", masked="•", scope="both", value="shared"),
            EnvVarWrite(key="ONLY_PROD", masked="•", scope="prod", value="prod-only"),
            EnvVarWrite(key="ONLY_PREVIEW", masked="•", scope="preview", value="preview-only"),
        ],
    )
    prod_app = await store.get_app("w1", str(prod_app.id))

    # A prod app gets ``both`` + ``prod`` vars, never ``preview``.
    assert store.decrypt_app_env(prod_app) == {"SHARED": "shared", "ONLY_PROD": "prod-only"}

    # Flip the same env onto a preview (non-prod) app: ``both`` + ``preview``.
    preview_app = await _make_app(prod=False)
    preview_app.name = "demo2"
    await preview_app.save()
    await store.upsert_app_env(
        preview_app,
        [
            EnvVarWrite(key="SHARED", masked="•", scope="both", value="shared"),
            EnvVarWrite(key="ONLY_PROD", masked="•", scope="prod", value="prod-only"),
            EnvVarWrite(key="ONLY_PREVIEW", masked="•", scope="preview", value="preview-only"),
        ],
    )
    preview_app = await store.get_app("w1", str(preview_app.id))
    assert store.decrypt_app_env(preview_app) == {
        "SHARED": "shared",
        "ONLY_PREVIEW": "preview-only",
    }
