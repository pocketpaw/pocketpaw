"""Tests for current_workspace_id membership fallback in ee.cloud._core.deps.

New file: reproduces and guards the "joined member locked out of every
workspace-scoped read" bug. A user who is a valid member of a workspace
(invite accept / verified-domain auto-join) but whose ``active_workspace``
field was never set must NOT get HTTP 400 on workspace-scoped GETs. The
dep now falls back to the first membership and persists it. A user with no
memberships at all still raises 400 (genuine create/join-first case).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pocketpaw_ee.cloud._core.deps import current_workspace_id


class _StubMembership:
    def __init__(self, workspace: str, role: str = "member") -> None:
        self.workspace = workspace
        self.role = role


class _StubUser:
    """Stand-in for the Beanie User doc with an async ``save()``."""

    def __init__(
        self,
        active_workspace: str | None,
        workspaces: list[_StubMembership] | None = None,
    ) -> None:
        self.active_workspace = active_workspace
        self.workspaces = workspaces or []
        self.save = AsyncMock()


async def test_falls_back_to_first_membership_when_active_unset() -> None:
    """Member with active_workspace=None resolves to their first membership
    and persists it — instead of raising 400."""
    user = _StubUser(
        active_workspace=None,
        workspaces=[_StubMembership("w_joined"), _StubMembership("w_other")],
    )

    wid = await current_workspace_id(user=user)  # type: ignore[arg-type]

    assert wid == "w_joined"
    # The fallback persists the choice so it sticks across requests.
    assert user.active_workspace == "w_joined"
    user.save.assert_awaited_once()


async def test_raises_400_when_no_memberships() -> None:
    """A user with no memberships at all genuinely needs to create/join one."""
    user = _StubUser(active_workspace=None, workspaces=[])

    with pytest.raises(HTTPException) as exc_info:
        await current_workspace_id(user=user)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 400
    assert "No active workspace" in str(exc_info.value.detail)
    user.save.assert_not_awaited()
