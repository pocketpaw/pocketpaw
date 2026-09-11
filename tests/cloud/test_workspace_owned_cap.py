# tests/cloud/test_workspace_owned_cap.py — the abuse ceiling on how many
# workspaces one account may own.
#
# Created 2026-09-11 (feat/abuse-budgets).
#
# Why this test exists at all: it is the gate that makes the other two ceilings
# mean something. The daily upload and turn budgets are keyed on the workspace,
# so an account that can mint workspaces in a loop gets a fresh empty counter
# every time and is bounded by nothing. Without this, those budgets look like
# protection and are not.
#
# Mutations: tests/mutations/abuse_budgets.json.

from __future__ import annotations

import pytest

from pocketpaw_ee.cloud._core.errors import WorkspaceLimitError
from pocketpaw_ee.cloud.workspace import service as workspace_service


def test_the_cap_reads_from_the_environment(monkeypatch):
    monkeypatch.setenv("POCKETPAW_MAX_OWNED_WORKSPACES", "3")
    assert workspace_service.max_owned_workspaces() == 3


def test_zero_means_uncapped(monkeypatch):
    """The divergence from a "0 disables the feature" reading. Creating a
    workspace is how a person starts, so an env typo must not stop it.

    Mutation that must break this: make ``create`` treat 0 as a real cap.
    """
    monkeypatch.setenv("POCKETPAW_MAX_OWNED_WORKSPACES", "0")
    assert workspace_service.max_owned_workspaces() == 0


def test_a_bad_value_uses_the_default_not_zero(monkeypatch):
    """``"ten"`` read as 0 would silently remove the ceiling."""
    monkeypatch.setenv("POCKETPAW_MAX_OWNED_WORKSPACES", "ten")
    assert workspace_service.max_owned_workspaces() == 10


async def test_create_refuses_an_account_at_its_cap(monkeypatch):
    """The gate itself, with the database stubbed to report a full account.

    Asserts the REFUSAL happens before any slug work: a capped account must not
    consume a slug it cannot use, and ``slug_reason`` is the first thing the
    unpatched function touches.
    """
    monkeypatch.setenv("POCKETPAW_MAX_OWNED_WORKSPACES", "2")
    touched: list[str] = []

    class _Count:
        async def count(self):
            return 2

    monkeypatch.setattr(
        workspace_service._WorkspaceDoc, "find", staticmethod(lambda *_a, **_k: _Count())
    )

    async def _slug(_s):
        touched.append("slug")
        return None

    monkeypatch.setattr(workspace_service, "slug_reason", _slug)

    class _Ctx:
        user_id = "u1"

    class _Body:
        name = "New"
        slug = "new"

    with pytest.raises(WorkspaceLimitError) as err:
        await workspace_service.create(_Ctx(), _Body())

    assert err.value.status_code == 429
    assert err.value.code == "workspace.owned_limit"
    assert touched == [], "the slug was resolved before the cap was checked"


async def test_an_account_under_the_cap_proceeds(monkeypatch):
    """The default path. Without this, deleting the create body entirely would
    still pass the refusal test above.
    """
    monkeypatch.setenv("POCKETPAW_MAX_OWNED_WORKSPACES", "2")

    class _Count:
        async def count(self):
            return 1

    monkeypatch.setattr(
        workspace_service._WorkspaceDoc, "find", staticmethod(lambda *_a, **_k: _Count())
    )

    reached: list[str] = []

    async def _slug(_s):
        reached.append("slug")
        raise RuntimeError("stop here — past the cap is all this test needs")

    monkeypatch.setattr(workspace_service, "slug_reason", _slug)

    class _Ctx:
        user_id = "u1"

    class _Body:
        name = "New"
        slug = "new"

    with pytest.raises(RuntimeError):
        await workspace_service.create(_Ctx(), _Body())

    assert reached == ["slug"], "an account under its cap was refused"
