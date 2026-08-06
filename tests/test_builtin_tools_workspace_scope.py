# tests/test_builtin_tools_workspace_scope.py
# Created: 2026-08-06 (C4-a — builtin Fabric/Instinct tools: stop cross-tenant
#   leakage and fail closed on unresolvable tenancy).
#
# What these pin, and why they are written against a SHARED store handle:
#
# The defect is ROW-LEVEL. The builtin tools passed no ``workspace_id`` into any
# store call, so every write landed with ``workspace_id = NULL`` and every read
# ran unfiltered. NULL is not merely "unscoped": ``_workspace_scope()`` renders a
# scoped read as ``(workspace_id = ? OR workspace_id IS NULL)`` for W4a legacy
# compatibility, so a NULL row is ACTIVELY RETURNED to every tenant's scoped
# query against that file.
#
# ISO-1/ISO-2 give each workspace its own database file when a workspace is in
# context, which would mask the row-level bug: a test that let the factory route
# by workspace would pass even with the fix reverted, because the two tenants
# would never share a file. So each test pins ONE shared store handle for both
# tenants (monkeypatching the tool module's ``_get_*_store``) — reproducing the
# real shared-file condition (no workspace resolved and
# POCKETPAW_REQUIRE_WORKSPACE_SCOPE unset -> the legacy singleton) and isolating
# exactly the tenancy argument under test. Every mutation in
# tests/mutations/builtin_tool_tenancy.json drops one of those arguments; each
# has been observed to fail these tests.
#
# Covers:
#   * instinct_propose stamps the caller's workspace; the Action is invisible to
#     another tenant's scoped pending queue (the reported leak);
#   * instinct_pending / instinct_audit read workspace-filtered;
#   * fabric_create stamps objects/types/links; a link refuses an endpoint that
#     does not resolve in the caller's workspace;
#   * fabric_query / fabric_stats never surface another workspace's objects or
#     type names;
#   * fail-closed — inside a cloud chat run with NO resolvable identity, every
#     tool REFUSES and performs no store access;
#   * the legacy path — a workspace-less run with NO per-run marker (OSS, CLI,
#     background job) keeps working unscoped, so nobody "tightens" the guard
#     into a process-global without seeing this break.

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.chat.agent_service import (  # noqa: E402
    attach_agent_identity,
    detach_agent_identity,
    mark_cloud_chat_run,
)

import pocketpaw.stores as stores  # noqa: E402
from pocketpaw.fabric.store import FabricStore  # noqa: E402
from pocketpaw.instinct.store import InstinctStore  # noqa: E402
from pocketpaw.tools.builtin import fabric_tools, instinct_tools  # noqa: E402

WS_A = "ws-alpha"
WS_B = "ws-bravo"


async def _action_workspaces(store: InstinctStore) -> list[str | None]:
    """Read the ``workspace_id`` COLUMN straight from SQLite.

    ``Action`` (instinct/models.py) does not surface ``workspace_id`` as a model
    field — it is persisted tenancy, not part of the artifact — so the only way
    to assert what actually landed on the row is to read the column.
    """
    import aiosqlite

    async with aiosqlite.connect(store._db_path) as db:
        async with db.execute("SELECT workspace_id FROM instinct_actions ORDER BY rowid") as cur:
            return [row[0] for row in await cur.fetchall()]


@pytest.fixture(autouse=True)
def _clean_context():
    """No ambient workspace / marker leaking in from another test."""
    token = stores.current_workspace.set(None)
    try:
        yield
    finally:
        try:
            stores.current_workspace.reset(token)
        except ValueError:
            stores.current_workspace.set(None)


@pytest.fixture
def shared_instinct(tmp_path, monkeypatch) -> InstinctStore:
    """ONE Instinct store both tenants use — the shared-file condition."""
    store = InstinctStore(db_path=str(tmp_path / "shared_instinct.db"))
    monkeypatch.setattr(instinct_tools, "_get_instinct_store", lambda: store)
    return store


@pytest.fixture
def shared_fabric(tmp_path, monkeypatch) -> FabricStore:
    """ONE Fabric store both tenants use — the shared-file condition."""
    store = FabricStore(db_path=str(tmp_path / "shared_fabric.db"))
    monkeypatch.setattr(fabric_tools, "_get_fabric_store", lambda: store)
    return store


class _identity:
    """Bind a live cloud chat run for ``workspace`` (or, with None, a chat run
    that never bound identity — the mis-tenanting case)."""

    def __init__(self, workspace: str | None, user: str = "u1") -> None:
        self._workspace = workspace
        self._user = user
        self._tokens = None
        self._marker = None

    def __enter__(self):
        self._marker = mark_cloud_chat_run()
        self._marker.__enter__()
        if self._workspace is not None:
            self._tokens = attach_agent_identity(workspace_id=self._workspace, user_id=self._user)
        return self

    def __exit__(self, *exc):
        if self._tokens is not None:
            detach_agent_identity(self._tokens)
        self._marker.__exit__(*exc)
        return False


# ---------------------------------------------------------------------------
# (a) The reported leak: a builtin proposal must not reach another tenant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_builtin_proposal_carries_workspace(shared_instinct: InstinctStore) -> None:
    """instinct_propose stamps the caller's workspace, never NULL.

    Mutation that breaks this: drop ``workspace_id=workspace`` from the
    ``store.propose`` call in instinct_tools.InstinctProposeTool.execute.
    """
    with _identity(WS_A):
        out = await instinct_tools.InstinctProposeTool().execute(
            pocket_id="p1", title="Reorder oat milk", recommendation="Order 24 units"
        )
    assert "Action proposed" in out

    assert await _action_workspaces(shared_instinct) == [WS_A], (
        "the Action landed without its tenant — a NULL row is returned to EVERY "
        "tenant's scoped query by the W4a NULL leg of _workspace_scope"
    )


@pytest.mark.asyncio
async def test_builtin_proposal_is_invisible_to_another_tenant(
    shared_instinct: InstinctStore,
) -> None:
    """The exact reported defect: tenant B must not see tenant A's proposal.

    Both tenants share ONE database file here, so this passes only because the
    row carries WS_A. Mutation that breaks it: drop ``workspace_id=workspace``
    from the propose call — the row goes NULL and NULL matches B's scoped read.
    """
    with _identity(WS_A):
        await instinct_tools.InstinctProposeTool().execute(
            pocket_id="p1", title="A-only action", recommendation="do A"
        )

    with _identity(WS_B, user="u2"):
        seen = await instinct_tools.InstinctPendingTool().execute()

    assert "A-only action" not in seen
    assert "No pending actions" in seen

    # ...and tenant A still sees its own.
    with _identity(WS_A):
        own = await instinct_tools.InstinctPendingTool().execute()
    assert "A-only action" in own


@pytest.mark.asyncio
async def test_builtin_pending_read_is_workspace_filtered(
    shared_instinct: InstinctStore,
) -> None:
    """A row written directly for WS_A is not returned to WS_B's queue.

    Mutation that breaks this: drop ``workspace_id=workspace`` from the
    ``store.pending`` call in InstinctPendingTool.execute.
    """
    from pocketpaw.instinct.models import ActionTrigger

    await shared_instinct.propose(
        pocket_id="p1",
        title="A-side",
        description="",
        recommendation="r",
        trigger=ActionTrigger(type="agent", source="t", reason="r"),
        workspace_id=WS_A,
    )

    with _identity(WS_B, user="u2"):
        seen = await instinct_tools.InstinctPendingTool().execute()
    assert "A-side" not in seen


@pytest.mark.asyncio
async def test_builtin_audit_read_is_workspace_filtered(
    shared_instinct: InstinctStore,
) -> None:
    """WS_B's audit view excludes WS_A's decision trail.

    Mutation that breaks this: drop ``workspace_id=workspace`` from the
    ``store.query_audit`` call in InstinctAuditTool.execute.
    """
    with _identity(WS_A):
        await instinct_tools.InstinctProposeTool().execute(
            pocket_id="p1", title="A-audit-marker", recommendation="r"
        )

    with _identity(WS_B, user="u2"):
        seen = await instinct_tools.InstinctAuditTool().execute()
    assert "A-audit-marker" not in seen


# ---------------------------------------------------------------------------
# (b)/(c) Fabric writes are stamped; fabric reads are filtered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fabric_create_stamps_workspace_on_type_and_object(
    shared_fabric: FabricStore,
) -> None:
    """define_type / create_object carry the caller's tenant.

    Mutation that breaks this: drop ``workspace_id=workspace`` from either the
    ``store.define_type`` or the ``store.create_object`` call.
    """
    with _identity(WS_A):
        await fabric_tools.FabricCreateTool().execute(
            action="define_type", type_name="Customer", properties={"name": "string"}
        )
        out = await fabric_tools.FabricCreateTool().execute(
            action="create_object", type_name="Customer", properties={"name": "Acme"}
        )
    assert "Created Customer object" in out

    types = await shared_fabric.list_types()
    assert [t.workspace_id for t in types] == [WS_A]

    from pocketpaw.fabric.models import FabricQuery

    res = await shared_fabric.query(FabricQuery(type_name="Customer"))
    obj = await shared_fabric.get_object(res.objects[0].id, workspace_id=WS_A)
    assert obj is not None, "the object did not land in the caller's workspace"


@pytest.mark.asyncio
async def test_fabric_query_never_returns_another_workspaces_objects(
    shared_fabric: FabricStore,
) -> None:
    """Tenant B's fabric_query excludes tenant A's objects on a shared file.

    Mutation that breaks this: drop ``workspace_id=workspace`` from the
    ``store.query`` call in FabricQueryTool.execute.
    """
    with _identity(WS_A):
        await fabric_tools.FabricCreateTool().execute(
            action="define_type", type_name="Customer", properties={"name": "string"}
        )
        await fabric_tools.FabricCreateTool().execute(
            action="create_object", type_name="Customer", properties={"name": "AcmeSecret"}
        )

    with _identity(WS_B, user="u2"):
        seen = await fabric_tools.FabricQueryTool().execute(type_name="Customer")
    assert "AcmeSecret" not in seen

    with _identity(WS_A):
        own = await fabric_tools.FabricQueryTool().execute(type_name="Customer")
    assert "AcmeSecret" in own


@pytest.mark.asyncio
async def test_fabric_create_cannot_bind_to_another_tenants_type(
    shared_fabric: FabricStore,
) -> None:
    """A type defined by WS_A is not reusable from WS_B (SZD-2).

    With an UNSCOPED ``get_type_by_name`` the create silently resolves to the
    other tenant's type id and writes an object against their schema. Mutation
    that breaks this: drop ``workspace_id=workspace`` from the
    ``store.get_type_by_name`` call in FabricCreateTool.execute.
    """
    with _identity(WS_A):
        await fabric_tools.FabricCreateTool().execute(
            action="define_type", type_name="Customer", properties={"name": "string"}
        )

    with _identity(WS_B, user="u2"):
        out = await fabric_tools.FabricCreateTool().execute(
            action="create_object", type_name="Customer", properties={"name": "BSpy"}
        )

    assert "not found" in out, "WS_B bound to WS_A's type"
    from pocketpaw.fabric.models import FabricQuery

    assert (await shared_fabric.query(FabricQuery(type_name="Customer"))).objects == []


@pytest.mark.asyncio
async def test_fabric_stats_counts_are_workspace_scoped(
    shared_fabric: FabricStore,
) -> None:
    """Counts leak volume even when names are hidden — scope them too.

    Mutation that breaks this: drop ``workspace_id=workspace`` from the
    ``store.stats`` call in FabricStatsTool.execute.
    """
    with _identity(WS_A):
        await fabric_tools.FabricCreateTool().execute(
            action="define_type", type_name="Customer", properties={"name": "string"}
        )
        await fabric_tools.FabricCreateTool().execute(
            action="create_object", type_name="Customer", properties={"name": "Acme"}
        )

    with _identity(WS_B, user="u2"):
        seen = await fabric_tools.FabricStatsTool().execute()

    assert "0 objects" in seen, f"WS_B was told how many objects WS_A holds: {seen!r}"

    with _identity(WS_A):
        own = await fabric_tools.FabricStatsTool().execute()
    assert "1 objects" in own


@pytest.mark.asyncio
async def test_fabric_stats_never_names_another_workspaces_types(
    shared_fabric: FabricStore,
) -> None:
    """Type NAMES are tenant data — B's stats must not list A's types.

    Mutation that breaks this: drop ``workspace_id=workspace`` from either the
    ``store.stats`` or the ``store.list_types`` call in FabricStatsTool.execute.
    """
    with _identity(WS_A):
        await fabric_tools.FabricCreateTool().execute(
            action="define_type", type_name="SecretProjectX", properties={}
        )
        await fabric_tools.FabricCreateTool().execute(
            action="create_object", type_name="SecretProjectX", properties={"k": "v"}
        )

    with _identity(WS_B, user="u2"):
        seen = await fabric_tools.FabricStatsTool().execute()
    assert "SecretProjectX" not in seen


@pytest.mark.asyncio
async def test_fabric_link_refuses_cross_tenant_endpoint(
    shared_fabric: FabricStore,
) -> None:
    """An object id belonging to another tenant cannot be linked into ours.

    Mutation that breaks this: drop the scoped ``store.get_object`` endpoint
    check in the ``link`` branch of FabricCreateTool.execute.
    """
    with _identity(WS_A):
        await fabric_tools.FabricCreateTool().execute(
            action="define_type", type_name="Customer", properties={"name": "string"}
        )
        await fabric_tools.FabricCreateTool().execute(
            action="create_object", type_name="Customer", properties={"name": "Acme"}
        )

    from pocketpaw.fabric.models import FabricQuery

    a_obj = (await shared_fabric.query(FabricQuery(type_name="Customer"))).objects[0]

    with _identity(WS_B, user="u2"):
        await fabric_tools.FabricCreateTool().execute(
            action="define_type", type_name="Vendor", properties={"name": "string"}
        )
        await fabric_tools.FabricCreateTool().execute(
            action="create_object", type_name="Vendor", properties={"name": "BCorp"}
        )
        b_obj = (
            await shared_fabric.query(FabricQuery(type_name="Vendor"), workspace_id=WS_B)
        ).objects[0]

        out = await fabric_tools.FabricCreateTool().execute(
            action="link", from_id=b_obj.id, to_id=a_obj.id, link_type="supplies"
        )

    assert "was not found in this workspace" in out
    links, _total = await shared_fabric.list_links()
    assert links == [], "the cross-tenant link was written anyway"


# ---------------------------------------------------------------------------
# (d) Fail closed on unresolvable identity — and ONLY inside a marked run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        (
            instinct_tools.InstinctProposeTool,
            {"pocket_id": "p", "title": "t", "recommendation": "r"},
        ),
        (instinct_tools.InstinctPendingTool, {}),
        (instinct_tools.InstinctAuditTool, {}),
        (fabric_tools.FabricQueryTool, {"type_name": "Customer"}),
        (fabric_tools.FabricStatsTool, {}),
        (fabric_tools.FabricCreateTool, {"action": "define_type", "type_name": "X"}),
    ],
)
async def test_unresolvable_identity_refuses(tool, kwargs, monkeypatch) -> None:
    """A cloud chat run that never bound a workspace REFUSES — it does not fall
    back to NULL/global scope, and it never touches the store.

    Mutation that breaks this: make ``is_tenant_scoped_run()`` return False, or
    drop the ``if refusal: return refusal`` guard from the tool.
    """

    def _boom():
        raise AssertionError("the tool reached the store without a resolved workspace")

    monkeypatch.setattr(instinct_tools, "_get_instinct_store", _boom)
    monkeypatch.setattr(fabric_tools, "_get_fabric_store", _boom)

    # Marked as a live cloud chat run, but identity was never attached.
    with _identity(None):
        out = await tool().execute(**kwargs)

    assert "requires workspace context" in out
    assert "Refusing rather than falling back" in out


@pytest.mark.asyncio
async def test_blank_workspace_is_not_accepted_as_identity(monkeypatch) -> None:
    """A whitespace-only workspace must not satisfy the guard nor be stamped.

    Mutation that breaks this: drop the ``.strip()`` emptiness check in
    _tenancy.current_workspace.
    """

    def _boom():
        raise AssertionError("a blank workspace was treated as resolved")

    monkeypatch.setattr(instinct_tools, "_get_instinct_store", _boom)

    with mark_cloud_chat_run():
        token = stores.current_workspace.set("   ")
        try:
            out = await instinct_tools.InstinctPendingTool().execute()
        finally:
            stores.current_workspace.reset(token)

    assert "requires workspace context" in out


# ---------------------------------------------------------------------------
# The legacy path stays open — the guard is per-run, NOT a process-global
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspaceless_run_without_marker_still_works(
    shared_instinct: InstinctStore,
) -> None:
    """OSS / CLI / background runs have no workspace AND no marker: they keep
    the legacy unscoped behavior rather than being refused.

    This is the #1570 guard. Gating the fail-closed on a PROCESS-GLOBAL (e.g.
    ``is_multi_tenant_cloud()``) instead of the per-run marker would refuse
    every one of these runs. Mutation that breaks this: make
    ``is_tenant_scoped_run()`` return True unconditionally.
    """
    out = await instinct_tools.InstinctProposeTool().execute(
        pocket_id="p1", title="local action", recommendation="r"
    )
    assert "Action proposed" in out

    # legacy, unscoped — unchanged
    assert await _action_workspaces(shared_instinct) == [None]

    seen = await instinct_tools.InstinctPendingTool().execute()
    assert "local action" in seen


@pytest.mark.asyncio
async def test_fabric_workspaceless_run_without_marker_still_works(
    shared_fabric: FabricStore,
) -> None:
    """Same legacy guarantee on the Fabric side."""
    await fabric_tools.FabricCreateTool().execute(
        action="define_type", type_name="Customer", properties={"name": "string"}
    )
    out = await fabric_tools.FabricCreateTool().execute(
        action="create_object", type_name="Customer", properties={"name": "LocalCo"}
    )
    assert "Created Customer object" in out

    seen = await fabric_tools.FabricQueryTool().execute(type_name="Customer")
    assert "LocalCo" in seen
