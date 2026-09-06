# tests/ee/terrarium/test_rbac.py — the workspace surface's RBAC mirrors the
# belt console: ``terrarium.read`` is MEMBER (reading a journal is a spectator
# act, and speaking/pledging cost the SPEAKER tokens), ``terrarium.manage`` is
# ADMIN (creating a universe seeds souls and ticking one spends model budget
# per citizen per tick).

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")
pytest.importorskip("mongomock_motor")

from pocketpaw_ee.guards.actions import ACTIONS  # noqa: E402
from pocketpaw_ee.guards.rbac import WorkspaceRole  # noqa: E402

from .conftest import WS, create_universe, make_client  # noqa: E402


def test_both_actions_are_registered():
    """The guard registry fails LOUD on an unknown action, so registration is
    mandatory, not optional."""
    assert ACTIONS["terrarium.read"].minimum == WorkspaceRole.MEMBER
    assert ACTIONS["terrarium.manage"].minimum == WorkspaceRole.ADMIN
    # Same tiers as the belt console, which is the stated model.
    assert ACTIONS["terrarium.read"].minimum == ACTIONS["belt.read"].minimum
    assert ACTIONS["terrarium.manage"].minimum == ACTIONS["belt.manage"].minimum


async def test_a_member_may_read_speak_and_pledge(client, monkeypatch):
    uni = create_universe(client)  # admin creates it
    member = make_client(monkeypatch, workspace_id=WS, user_id="u-member", role="member")

    assert member.get("/terrarium/universes").status_code == 200
    assert member.get(f"/terrarium/universes/{uni['id']}").status_code == 200
    assert member.get(f"/terrarium/universes/{uni['id']}/events").status_code == 200
    assert member.get(f"/terrarium/universes/{uni['id']}/citizens").status_code == 200
    assert member.get(f"/terrarium/universes/{uni['id']}/artifacts").status_code == 200
    assert member.get(f"/terrarium/universes/{uni['id']}/weather").status_code == 200
    assert (
        member.post(f"/terrarium/universes/{uni['id']}/speak", json={"text": "hello"}).status_code
        == 200
    )
    assert (
        member.post(
            f"/terrarium/universes/{uni['id']}/weather/pledge", json={"kind": "rain", "tokens": 1}
        ).status_code
        == 200
    )


async def test_a_member_may_not_create_or_tick(client, monkeypatch):
    uni = create_universe(client)
    member = make_client(monkeypatch, workspace_id=WS, user_id="u-member", role="member")

    from .conftest import dust_physics

    res = member.post("/terrarium/universes", json={"physics": dust_physics()})
    assert res.status_code == 403, res.text

    res = member.post(f"/terrarium/universes/{uni['id']}/tick")
    assert res.status_code == 403, res.text
