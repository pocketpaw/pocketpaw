# tests/cloud/ship/test_ship_env_router.py — the SHIP-9 env-management HTTP
# surface (the four /ship/apps/{id}/env routes).
#
# Pins the FROZEN masked-only wire shape the /ship console (SHIP-10) builds
# against, the masking invariant (a long secret never crosses the wire in the
# clear — only a suffix), upsert / delete / .env-import behaviour, and the
# tenancy filter on every env route.
#
# Created 2026-07-23 (feat/ship-9-env-store, SHIP-9): new module.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.ship import store

from tests.cloud.ship.conftest import _app_on_box, _ready_box

# A long secret whose full form must never appear on the wire.
SECRET = "sk-live-DoNotLeakThisSuperSecretTokenBody-9f8e7d6c5b4a3f21"


async def _app(w1) -> str:
    return await _app_on_box(w1, await _ready_box(w1))


# ---------------------------------------------------------------------------
# Read + the frozen shape
# ---------------------------------------------------------------------------


async def test_get_env_is_empty_for_a_fresh_app(w1):
    app_id = await _app(w1)

    resp = await w1.get(f"/ship/apps/{app_id}/env")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"vars": []}


async def test_put_returns_the_frozen_masked_shape(w1):
    app_id = await _app(w1)

    resp = await w1.put(
        f"/ship/apps/{app_id}/env",
        json={"vars": [{"key": "API_KEY", "value": SECRET, "scope": "prod"}]},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"vars"}
    assert set(body["vars"][0]) == {"key", "masked_value", "scope"}
    row = body["vars"][0]
    assert (row["key"], row["scope"]) == ("API_KEY", "prod")
    # The value is masked: a suffix only, never the whole secret.
    assert row["masked_value"] != SECRET
    assert SECRET not in resp.text
    assert row["masked_value"].endswith(SECRET[-3:])
    assert len(row["masked_value"]) < len(SECRET)


# ---------------------------------------------------------------------------
# Masking is a hard invariant
# ---------------------------------------------------------------------------


async def test_a_long_secret_is_never_returned_in_the_clear(w1):
    app_id = await _app(w1)
    await w1.put(
        f"/ship/apps/{app_id}/env",
        json={"vars": [{"key": "TOKEN", "value": SECRET}]},
    )

    got = await w1.get(f"/ship/apps/{app_id}/env")

    masked = got.json()["vars"][0]["masked_value"]
    assert SECRET not in got.text
    assert masked != SECRET
    # Only a short suffix leaks — the mask can't reconstruct the secret.
    assert masked.endswith(SECRET[-3:])
    assert len(masked) <= 4


async def test_short_values_are_fully_hidden(w1):
    app_id = await _app(w1)

    resp = await w1.put(
        f"/ship/apps/{app_id}/env",
        json={"vars": [{"key": "PORT", "value": "3000"}]},
    )

    assert resp.json()["vars"][0]["masked_value"] == "••••••"
    assert "3000" not in resp.text


# ---------------------------------------------------------------------------
# Upsert + delete
# ---------------------------------------------------------------------------


async def test_put_upserts_add_new_and_overwrite_existing(w1):
    app_id = await _app(w1)

    await w1.put(
        f"/ship/apps/{app_id}/env",
        json={"vars": [{"key": "A", "value": "first-value-a"}]},
    )
    resp = await w1.put(
        f"/ship/apps/{app_id}/env",
        json={
            "vars": [
                {"key": "A", "value": "second-value-a"},
                {"key": "B", "value": "value-b-here"},
            ]
        },
    )

    keys = {v["key"] for v in resp.json()["vars"]}
    assert keys == {"A", "B"}  # A overwritten in place, not duplicated
    # The store holds the overwritten value, not the original.
    app_doc = await store.get_app("w1", app_id)
    assert store.decrypt_app_env(app_doc)["A"] == "second-value-a"


async def test_delete_removes_a_key_and_is_idempotent(w1):
    app_id = await _app(w1)
    await w1.put(
        f"/ship/apps/{app_id}/env",
        json={"vars": [{"key": "A", "value": "aaaa-value"}, {"key": "B", "value": "bbbb-value"}]},
    )

    resp = await w1.delete(f"/ship/apps/{app_id}/env/A")

    assert resp.status_code == 200, resp.text
    assert {v["key"] for v in resp.json()["vars"]} == {"B"}
    # Deleting an already-gone key is a clean 200 no-op.
    again = await w1.delete(f"/ship/apps/{app_id}/env/A")
    assert again.status_code == 200
    assert {v["key"] for v in again.json()["vars"]} == {"B"}


# ---------------------------------------------------------------------------
# .env import
# ---------------------------------------------------------------------------


async def test_import_parses_a_realistic_dotenv(w1):
    app_id = await _app(w1)
    dotenv = "\n".join(
        [
            "# a comment",
            "",
            "FOO=bar",
            'QUOTED="quoted value"',
            "SINGLE='single value'",
            "export EXPORTED=exp-value",
            "WITH_EQUALS=a=b=c",
            "   ",
            "not a valid line without an equals",
            "BAD KEY=skipme",  # space in key -> invalid -> skipped
            "=novalue",  # empty key -> skipped
        ]
    )

    resp = await w1.post(f"/ship/apps/{app_id}/env/import", json={"dotenv": dotenv})

    assert resp.status_code == 200, resp.text
    keys = {v["key"] for v in resp.json()["vars"]}
    assert keys == {"FOO", "QUOTED", "SINGLE", "EXPORTED", "WITH_EQUALS"}

    # Values landed correctly (quotes stripped, split on the FIRST '=').
    app_doc = await store.get_app("w1", app_id)
    decrypted = store.decrypt_app_env(app_doc)
    assert decrypted["QUOTED"] == "quoted value"
    assert decrypted["SINGLE"] == "single value"
    assert decrypted["WITH_EQUALS"] == "a=b=c"
    assert decrypted["EXPORTED"] == "exp-value"
    # Nothing from the import blob leaked into the response.
    assert "quoted value" not in resp.text


async def test_put_rejects_an_invalid_key(w1):
    app_id = await _app(w1)

    resp = await w1.put(
        f"/ship/apps/{app_id}/env",
        json={"vars": [{"key": "not a valid key", "value": "x"}]},
    )

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Tenancy — every env route is workspace-scoped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "suffix", "payload"),
    [
        ("get", "/env", None),
        ("put", "/env", {"vars": [{"key": "A", "value": "v"}]}),
        ("post", "/env/import", {"dotenv": "A=v"}),
        ("delete", "/env/A", None),
    ],
)
async def test_cross_tenant_env_routes_404(w1, w2, method, suffix, payload):
    app_id = await _app(w1)

    call = getattr(w2, method)
    resp = (
        await call(f"/ship/apps/{app_id}{suffix}", json=payload)
        if payload
        else await call(f"/ship/apps/{app_id}{suffix}")
    )

    assert resp.status_code == 404, f"{method.upper()} {suffix} leaked: {resp.status_code}"
    assert resp.json()["error"]["code"] == "ship.app.not_found"


async def test_a_foreign_tenant_cannot_read_or_write_env(w1, w2):
    """The other tenant's env is untouched by a cross-tenant write attempt."""
    app_id = await _app(w1)
    await w1.put(f"/ship/apps/{app_id}/env", json={"vars": [{"key": "SECRET", "value": SECRET}]})

    # w2 can't see it, and its blind write 404s rather than mutating w1's app.
    assert (await w2.get(f"/ship/apps/{app_id}/env")).status_code == 404
    await w2.put(f"/ship/apps/{app_id}/env", json={"vars": [{"key": "SECRET", "value": "x"}]})

    app_doc = await store.get_app("w1", app_id)
    assert store.decrypt_app_env(app_doc)["SECRET"] == SECRET
