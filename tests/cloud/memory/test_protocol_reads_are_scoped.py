"""The protocol-level reads must not answer across tenants.

``recall``, ``clear_session`` and ``delete_session`` are built-in agent tools on
the ``_TENANT_SAFE_TOOLS`` allowlist in ``pocketpaw.agents.pydantic_ai``, where
the grant carries the annotation::

    # memory + sessions — scoped by the caller's own session key

``MongoMemoryStore`` did not scope them. ``search`` built its filter from type,
tags and a content regex and nothing else, so a tenant's own prompt returned
every workspace's memory facts; ``clear_session`` deleted by a model-supplied
session key with no tenant predicate.

WHY THE EXISTING ISOLATION TESTS DID NOT CATCH IT

``test_workspace_isolation.py`` covers ``get_session_in_workspace`` and
``list_facts_in_workspace`` — the helpers that take an explicit workspace and
were always correct. The agent tools do not call those. The file named for
workspace isolation tested the half that was already isolated.

WHY A CONTEXTVAR AND NOT A PARAMETER

The ``MemoryStoreProtocol`` signatures are OSS API; adding a workspace argument
would change the contract for every implementation. ``current_workspace`` lives
in OSS core (``pocketpaw.stores``) for exactly this, and
``agent_service.attach_agent_identity`` bridges the per-stream workspace onto it
— including the second bind inside prewarm that exists so in-process tools can
read identity at all (``agent_service.py:510``).

Mutations that must fail these tests: dropping either ``_scoped`` call in
``search``, returning the unscoped filter when no workspace is bound, treating
any present value of the override as truthy, and failing OPEN when the
multi-tenant signal cannot be read.
"""

from __future__ import annotations

import uuid

import pytest
from pocketpaw_ee.cloud.memory import mongo_store as ms

from pocketpaw.memory.protocol import MemoryEntry, MemoryType
from pocketpaw.stores import current_workspace


def _fact(content: str, workspace_id: str | None) -> MemoryEntry:
    meta = {"workspace_id": workspace_id} if workspace_id else {}
    return MemoryEntry(
        id="",
        type=MemoryType.LONG_TERM,
        content=content,
        metadata=meta,
    )


@pytest.fixture
def multi_tenant(monkeypatch):
    """Make ``is_multi_tenant_cloud()`` report a multi-tenant deployment."""
    import sys
    import types

    module = types.ModuleType("pocketpaw_ee.cloud.shared.db")
    module.is_multi_tenant_cloud = lambda: True
    monkeypatch.setitem(sys.modules, "pocketpaw_ee.cloud.shared.db", module)
    monkeypatch.delenv(ms._ALLOW_GLOBAL_ENV, raising=False)


@pytest.fixture
def single_tenant(monkeypatch):
    import sys
    import types

    module = types.ModuleType("pocketpaw_ee.cloud.shared.db")
    module.is_multi_tenant_cloud = lambda: False
    monkeypatch.setitem(sys.modules, "pocketpaw_ee.cloud.shared.db", module)
    monkeypatch.delenv(ms._ALLOW_GLOBAL_ENV, raising=False)


@pytest.fixture
def in_workspace():
    """Bind ``current_workspace`` the way attach_agent_identity does.

    No teardown, deliberately. The bind happens inside the async test body, so
    it lands in the context of the task running that test — which is discarded
    when the task ends, taking the binding with it. Resetting from the fixture's
    own (outer) context raises "Token was created in a different Context", and
    the assertion below is the cheaper guarantee: every test starts unbound.
    """
    assert current_workspace.get() is None, (
        "current_workspace leaked from an earlier test — these tests rely on "
        "each one starting unbound"
    )
    return current_workspace.set


# ---------------------------------------------------------------------------
# The finding
# ---------------------------------------------------------------------------


class TestSearchIsScoped:
    async def test_recall_does_not_return_another_tenants_facts(
        self, store, multi_tenant, in_workspace
    ):
        """The reproduction. One tenant's prompt must not read the other's."""
        await store.save(_fact("alpha secret roadmap", "ws-alpha"))
        await store.save(_fact("beta secret roadmap", "ws-beta"))

        in_workspace("ws-alpha")
        results = await store.search("secret")
        contents = [e.content for e in results]

        assert "alpha secret roadmap" in contents
        assert "beta secret roadmap" not in contents, (
            "recall returned another workspace's memory facts"
        )

    async def test_an_unbound_context_reads_nothing(self, store, multi_tenant):
        """No workspace in context must mean empty, never everything.

        This is the case that decides the shape of the fix. Returning the
        unscoped filter here would look like a working search and leak the
        whole deployment.
        """
        await store.save(_fact("alpha secret", "ws-alpha"))
        await store.save(_fact("beta secret", "ws-beta"))

        assert await store.search("secret") == []

    async def test_session_search_is_scoped_too(self, store, multi_tenant, in_workspace):
        """The SESSION branch builds its own filter and needs its own scoping."""
        from pocketpaw_ee.cloud.models.session import Session

        for ws, key, text in (
            ("ws-alpha", f"k-a-{uuid.uuid4().hex[:6]}", "alpha message"),
            ("ws-beta", f"k-b-{uuid.uuid4().hex[:6]}", "beta message"),
        ):
            await Session(sessionId=key, context_type="pocket", workspace=ws, owner="u").insert()
            await store.save(
                MemoryEntry(
                    id="",
                    type=MemoryType.SESSION,
                    content=text,
                    role="user",
                    session_key=key,
                )
            )

        in_workspace("ws-alpha")
        results = await store.search("message", memory_type=MemoryType.SESSION)
        contents = [e.content for e in results]

        assert "alpha message" in contents
        assert "beta message" not in contents


class TestClearSessionIsScoped:
    async def test_clear_session_cannot_delete_another_tenants_messages(
        self, store, multi_tenant, in_workspace
    ):
        """``session_key`` is a model-supplied tool argument — a destructive one."""
        from pocketpaw_ee.cloud.models.message import Message
        from pocketpaw_ee.cloud.models.session import Session

        victim_key = f"victim-{uuid.uuid4().hex[:6]}"
        await Session(
            sessionId=victim_key, context_type="pocket", workspace="ws-beta", owner="u2"
        ).insert()
        await store.save(
            MemoryEntry(
                id="",
                type=MemoryType.SESSION,
                content="beta's message",
                role="user",
                session_key=victim_key,
            )
        )

        in_workspace("ws-alpha")
        deleted = await store.clear_session(victim_key)

        assert deleted == 0, "cleared another workspace's session"
        survivors = await Message.find({"session_key": victim_key}).to_list()
        assert len(survivors) == 1, "another workspace's messages were deleted"

    async def test_clear_session_still_works_in_its_own_workspace(
        self, store, multi_tenant, in_workspace
    ):
        """The scoping must not break the tool for its legitimate use."""
        from pocketpaw_ee.cloud.models.session import Session

        key = f"mine-{uuid.uuid4().hex[:6]}"
        await Session(
            sessionId=key, context_type="pocket", workspace="ws-alpha", owner="u1"
        ).insert()
        await store.save(
            MemoryEntry(
                id="",
                type=MemoryType.SESSION,
                content="my message",
                role="user",
                session_key=key,
            )
        )

        in_workspace("ws-alpha")
        assert await store.clear_session(key) == 1


class TestIdKeyedMethodsAreScoped:
    """``get`` and ``delete`` take an id, so the check is after the fetch."""

    async def test_get_refuses_a_foreign_row(self, store, multi_tenant, in_workspace):
        entry_id = await store.save(_fact("beta fact", "ws-beta"))

        in_workspace("ws-alpha")
        assert await store.get(entry_id) is None

    async def test_delete_refuses_a_foreign_row(self, store, multi_tenant, in_workspace):
        from pocketpaw_ee.cloud.memory.documents import MemoryFactDoc

        entry_id = await store.save(_fact("beta fact", "ws-beta"))

        in_workspace("ws-alpha")
        assert await store.delete(entry_id) is False
        assert await MemoryFactDoc.get(entry_id) is not None, "foreign fact was deleted"

    async def test_get_returns_its_own_row(self, store, multi_tenant, in_workspace):
        entry_id = await store.save(_fact("alpha fact", "ws-alpha"))

        in_workspace("ws-alpha")
        got = await store.get(entry_id)
        assert got is not None and got.content == "alpha fact"


class TestSessionInfoUsesTheRightFieldName:
    """``Session`` names its tenant column ``workspace``, not ``workspace_id``.

    Scoping it on the wrong field would match nothing and silently break every
    caller, which a test asserting only "the foreign row is hidden" would pass.
    """

    async def test_own_session_is_found(self, store, multi_tenant, in_workspace):
        from pocketpaw_ee.cloud.models.session import Session

        key = f"k-{uuid.uuid4().hex[:6]}"
        await Session(
            sessionId=key, context_type="pocket", workspace="ws-alpha", owner="u1"
        ).insert()

        in_workspace("ws-alpha")
        assert await store.get_session_info(key) is not None

    async def test_foreign_session_is_not(self, store, multi_tenant, in_workspace):
        from pocketpaw_ee.cloud.models.session import Session

        key = f"k-{uuid.uuid4().hex[:6]}"
        await Session(
            sessionId=key, context_type="pocket", workspace="ws-beta", owner="u2"
        ).insert()

        in_workspace("ws-alpha")
        assert await store.get_session_info(key) is None


# ---------------------------------------------------------------------------
# The deployments that must NOT change
# ---------------------------------------------------------------------------


class TestSingleTenantIsUnaffected:
    async def test_a_local_install_still_searches_everything(self, store, single_tenant):
        """No cloud DB means one tenant, and the protocol contract is preserved.

        A local desktop install has no workspace bound and must keep working
        exactly as before — this is why the gate is ``is_multi_tenant_cloud()``
        and not "is a workspace bound".
        """
        await store.save(_fact("untagged legacy fact", None))
        await store.save(_fact("tagged fact", "ws-1"))

        contents = [e.content for e in await store.search("fact")]
        assert "untagged legacy fact" in contents
        assert "tagged fact" in contents

    async def test_a_local_install_can_still_clear_a_session(self, store, single_tenant):
        from pocketpaw_ee.cloud.models.session import Session

        key = f"local-{uuid.uuid4().hex[:6]}"
        await Session(sessionId=key, context_type="pocket", workspace="ws-1", owner="u1").insert()
        await store.save(
            MemoryEntry(
                id="",
                type=MemoryType.SESSION,
                content="local message",
                role="user",
                session_key=key,
            )
        )

        assert await store.clear_session(key) == 1


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


class TestTheGate:
    def test_an_unreadable_signal_isolates(self, monkeypatch):
        """Fail CLOSED. Here that means isolating, not opening.

        The pocket router's bypass gate returns False on an unreadable signal;
        this one returns True. Both refuse — the boolean differs because the
        dangerous answer differs.
        """
        import sys
        import types

        module = types.ModuleType("pocketpaw_ee.cloud.shared.db")

        def _explode():
            raise RuntimeError("no db")

        module.is_multi_tenant_cloud = _explode
        monkeypatch.setitem(sys.modules, "pocketpaw_ee.cloud.shared.db", module)
        monkeypatch.delenv(ms._ALLOW_GLOBAL_ENV, raising=False)

        assert ms._tenant_isolation_required() is True

    def test_the_override_re_opens_it(self, multi_tenant, monkeypatch):
        monkeypatch.setenv(ms._ALLOW_GLOBAL_ENV, "1")
        assert ms._tenant_isolation_required() is False

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
    def test_only_an_explicit_override_re_opens_it(self, multi_tenant, monkeypatch, value):
        """``...=0`` must not mean yes."""
        monkeypatch.setenv(ms._ALLOW_GLOBAL_ENV, value)
        assert ms._tenant_isolation_required() is True

    def test_a_blank_workspace_is_not_a_scope(self, multi_tenant, in_workspace):
        """An empty ContextVar must refuse, not filter on the empty string.

        Filtering on ``workspace_id == ""`` would match nothing and read as a
        working scope, which is the quiet version of the same bug.
        """
        in_workspace("   ")
        assert ms._scoped({"a": 1}) is None


class TestTheToolsThatReachThis:
    """The allowlist grant that made this reachable, pinned.

    If a future change adds another memory or session tool to
    ``_TENANT_SAFE_TOOLS``, this test is where someone finds out that the tool
    needs the same audit against the CLOUD store rather than the file store the
    annotation was written for.
    """

    def test_the_known_memory_tools_are_the_ones_audited(self):
        from pocketpaw.agents.pydantic_ai import _TENANT_SAFE_TOOLS

        memory_tools = {
            t
            for t in _TENANT_SAFE_TOOLS
            if t
            in {
                "remember",
                "recall",
                "forget",
                "clear_session",
                "delete_session",
                "list_sessions",
                "new_session",
                "rename_session",
                "switch_session",
            }
        }
        assert memory_tools == {
            "remember",
            "recall",
            "forget",
            "clear_session",
            "delete_session",
            "list_sessions",
            "new_session",
            "rename_session",
            "switch_session",
        }, (
            "The memory/session tool set on _TENANT_SAFE_TOOLS changed. Every tool "
            "there reaches MongoMemoryStore through MemoryManager; re-check the new "
            "one against the CLOUD store, not the file store the allowlist comment "
            "describes."
        )
