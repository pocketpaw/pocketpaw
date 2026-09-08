"""Two live keys sharing a prefix must both keep working.

``resolve_bearer`` looked its key up with::

    doc = await APIKey.find_one(APIKey.prefix == prefix, APIKey.revoked == False)

The prefix is 8 hex characters carved off the secret, so two live keys collide
by birthday at roughly 65k keys in one deployment. ``find_one`` then returns an
arbitrary one of them, the argon2 verify fails against the other key's secret,
and that OTHER key stops authenticating — permanently, with a 401 that explains
nothing.

Availability rather than disclosure: the argon2 verify is what actually
authenticates and it is unchanged, so a collision could never let the wrong key
in. It could only lock a legitimate key out.

The loop costs nothing in the normal case, because the normal case is one
document. It performs a second ~30ms verify only when a collision genuinely
exists, which is the situation it exists to survive.

Mutations that must fail these tests: going back to ``find_one``, and returning
after the first candidate instead of continuing past a failed verify.
"""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.auth import api_keys


@pytest.fixture(autouse=True)
def _clear_caches():
    api_keys._reset_caches_for_tests()
    yield
    api_keys._reset_caches_for_tests()


async def test_a_colliding_prefix_does_not_lock_the_other_key_out(mongo_db, monkeypatch):  # noqa: ARG001
    """The reproduction: two live keys, same prefix, both must resolve.

    Their secrets are forced to share the first 8 characters so the lookup
    returns two documents. Before the fix, whichever one Mongo did not return
    first was dead.
    """
    created: list[tuple[str, str]] = []
    for workspace in ("ws-alpha", "ws-beta"):
        doc, full_key = await api_keys.create_api_key(
            workspace_id=workspace,
            owner_user_id=f"owner-{workspace}",
            name=f"key for {workspace}",
            scopes=["chat.read"],
        )
        created.append((workspace, full_key))

    # Force the collision: give the second key the first key's prefix. Both
    # rows stay live, and each still verifies only against its OWN secret.
    first_prefix = (await api_keys.APIKey.find_one(APIKey_owner("owner-ws-alpha"))).prefix
    other = await api_keys.APIKey.find_one(APIKey_owner("owner-ws-beta"))
    other.prefix = first_prefix
    await other.save()

    api_keys._reset_caches_for_tests()

    for workspace, full_key in created:
        # The colliding key's token still carries its own prefix in the body,
        # so rewrite the lookup prefix the same way the row was rewritten.
        token = full_key
        if workspace == "ws-beta":
            body = full_key[4:]
            token = "paw_" + first_prefix + body[len(first_prefix) :]
            # The secret is the whole body, so changing the prefix changes the
            # secret; re-point the row's hash at the token we will present.
            other.hashed_secret = api_keys._password_hash.hash(token[4:])
            await other.save()
            api_keys._reset_caches_for_tests()

        resolved = await api_keys.resolve_bearer(token)
        assert resolved is not None, (
            f"{workspace}'s key stopped authenticating because another live key "
            "shares its prefix"
        )
        assert resolved[1] == workspace


def APIKey_owner(owner: str):  # noqa: N802 — reads as a query helper at call sites
    return api_keys.APIKey.owner_user_id == owner


async def test_an_unknown_prefix_is_still_refused(mongo_db):  # noqa: ARG001
    assert await api_keys.resolve_bearer("paw_" + "0" * 40) is None


async def test_a_wrong_secret_on_a_known_prefix_is_still_refused(mongo_db):  # noqa: ARG001
    """The verify still decides. Iterating candidates must not weaken it."""
    doc, full_key = await api_keys.create_api_key(
        workspace_id="ws-1",
        owner_user_id="owner-1",
        name="k",
        scopes=["chat.read"],
    )
    api_keys._reset_caches_for_tests()

    tampered = full_key[:-4] + ("0000" if not full_key.endswith("0000") else "1111")
    assert await api_keys.resolve_bearer(tampered) is None
