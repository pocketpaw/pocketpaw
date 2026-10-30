"""Joining a group must be confined to the caller's own workspace.

``join_group`` verified three things — channel visibility, group type, archived
state — and none of them was tenancy. ``_get_group_domain_or_404`` loads by
``_id`` alone, so the ``group_id`` in the path could name a group in any
workspace on the deployment.

Every tenant ships a target: ``seed_default_group`` inserts ``type="public"``
for each new workspace (called from ``cloud/auth/core.py``), so the default
"General" group is exactly the type this route admits.

WHY THIS ONE IS WORSE THAN A READ

``_add_member_doc`` appends the caller to ``Group.members``, and membership is
what the rest of the chat surface gates on. After this write the attacker is a
real member: ``send_message`` and ``patch_ui_state`` — which mutates
``message.content`` — both open up. It is a cross-tenant write that converts
every cross-tenant read in the same subsystem into a write.

NotFound rather than Forbidden, because a group in a workspace you are not in
should not confirm that it exists.

Mutations that must fail these tests: dropping the workspace comparison,
comparing against the group's own workspace instead of the caller's, and
raising Forbidden (which is an existence oracle) instead of NotFound.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pocketpaw_ee.cloud.chat import group_service
from pocketpaw_ee.cloud.models.group import Group as _GroupDoc
from pocketpaw_ee.cloud.shared.errors import NotFound


async def _empty_lookups(_groups):
    return {}, {}


@pytest.fixture
def patched_lookups(monkeypatch):
    """Replace ``_populate_lookups_for_domain_groups`` with an empty stub.

    Mirrors the fixture in ``test_group_emits.py``; the success-path test
    reaches the wire-dict build, which would otherwise need real user rows.
    """
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.group_service._populate_lookups_for_domain_groups",
        _empty_lookups,
    )


@pytest.fixture
def resolver_mock(monkeypatch):
    rmock = MagicMock()
    monkeypatch.setattr("pocketpaw_ee.cloud.chat.group_service.get_resolver", lambda: rmock)
    return rmock


async def _group(*, workspace: str, owner: str, type: str = "public", members=None):
    doc = _GroupDoc(
        workspace=workspace,
        name="General",
        slug=f"general-{workspace}",
        description="",
        icon="",
        color="",
        type=type,
        members=members or [owner],
        member_roles={owner: "admin"},
        agents=[],
        pinned_messages=[],
        owner=owner,
        archived=False,
    )
    await doc.insert()
    return doc


@pytest.mark.asyncio
async def test_an_outsider_cannot_join_another_workspaces_public_group(
    mongo_db, recording_bus, patched_lookups, resolver_mock
):
    """The finding. One POST made an outsider a real member of another tenant."""
    victim = await _group(workspace="ws-victim", owner="u-victim")

    with pytest.raises(NotFound):
        await group_service.join_group(str(victim.id), "u-attacker", "ws-attacker")

    after = await _GroupDoc.get(victim.id)
    assert "u-attacker" not in after.members, "an outsider was added to another workspace's group"


@pytest.mark.asyncio
async def test_the_refusal_does_not_confirm_the_group_exists(
    mongo_db, recording_bus, patched_lookups, resolver_mock
):
    """A foreign group and a nonexistent one must be indistinguishable.

    Forbidden here would be an existence oracle over every group id on the
    deployment, which is most of what the read findings in this subsystem are.
    """
    victim = await _group(workspace="ws-victim", owner="u-victim")

    with pytest.raises(NotFound) as foreign:
        await group_service.join_group(str(victim.id), "u-attacker", "ws-attacker")

    missing_id = "0" * 24
    with pytest.raises(NotFound) as absent:
        await group_service.join_group(missing_id, "u-attacker", "ws-attacker")

    assert type(foreign.value) is type(absent.value)


@pytest.mark.asyncio
async def test_a_member_of_the_same_workspace_still_joins(
    mongo_db, recording_bus, patched_lookups, resolver_mock
):
    """The scoping must not break the feature it guards."""
    group = await _group(workspace="ws-1", owner="u1")

    await group_service.join_group(str(group.id), "u2", "ws-1")

    after = await _GroupDoc.get(group.id)
    assert "u2" in after.members


@pytest.mark.asyncio
async def test_the_pre_existing_checks_still_deny_on_their_own(
    mongo_db, recording_bus, patched_lookups, resolver_mock
):
    """This change only ADDS a factor; the other three must be untouched.

    Written with the workspace matching so the new check cannot be what denies
    — otherwise a mutation deleting any of the original three would escape.
    """
    from pocketpaw_ee.cloud.shared.errors import Forbidden

    dm = await _group(workspace="ws-1", owner="u1", type="dm")
    with pytest.raises(Forbidden):
        await group_service.join_group(str(dm.id), "u2", "ws-1")

    archived = await _group(workspace="ws-1", owner="u1")
    archived.archived = True
    await archived.save()
    with pytest.raises(Forbidden):
        await group_service.join_group(str(archived.id), "u2", "ws-1")


@pytest.mark.asyncio
async def test_the_route_passes_the_callers_workspace_not_the_paths(mongo_db):
    """A helper nobody calls correctly is documentation.

    Asserted against the parsed route rather than by driving it, because the
    route needs the whole cloud dependency stack to reach a state where
    ``current_workspace_id`` resolves.
    """
    import ast
    import inspect
    import sys

    import pocketpaw_ee.cloud.chat.router  # noqa: F401 — registers the module

    # ``sys.modules`` rather than the attribute: ``chat/__init__.py`` rebinds
    # the name ``router`` to the APIRouter instance, so the dotted attribute
    # resolves to an object inspect cannot read source from.
    module = sys.modules["pocketpaw_ee.cloud.chat.router"]
    tree = ast.parse(inspect.getsource(module))
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "join_group"
    )
    args = {a.arg for a in handler.args.args} | {a.arg for a in handler.args.kwonlyargs}
    assert "workspace_id" in args, (
        "the join route does not bind current_workspace_id, so the service "
        "check has nothing to compare against"
    )

    call = next(
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join_group"
    )
    passed = [a.id for a in call.args if isinstance(a, ast.Name)]
    assert "workspace_id" in passed, "the route resolves a workspace and does not pass it on"
