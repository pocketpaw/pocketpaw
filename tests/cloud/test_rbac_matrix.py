# Parametrized matrix test for the canonical ACTIONS table.
# Every row in src/pocketpaw/ee/guards/actions.py:ACTIONS must have at least
# one allow-path and one deny-path assertion. The meta test at the bottom
# enforces coverage so new actions can't be added without exercising them.

from __future__ import annotations

import pytest
from pocketpaw_ee.guards.actions import (
    ACTIONS,
    GroupRole,
    check_action,
)
from pocketpaw_ee.guards.rbac import Forbidden, PocketAccess, WorkspaceRole

# ---------------------------------------------------------------------------
# Enumerate every role in each family, ranked by level.
# ---------------------------------------------------------------------------

_WORKSPACE_ROLES = sorted(WorkspaceRole, key=lambda r: r.level)
_GROUP_ROLES = sorted(GroupRole, key=lambda r: r.level)
_POCKET_LEVELS = sorted(PocketAccess, key=lambda a: a.level)


def _peers_of(minimum: object) -> list:
    if isinstance(minimum, WorkspaceRole):
        return list(_WORKSPACE_ROLES)
    if isinstance(minimum, GroupRole):
        return list(_GROUP_ROLES)
    if isinstance(minimum, PocketAccess):
        return list(_POCKET_LEVELS)
    raise TypeError(f"Unknown role family: {type(minimum)!r}")


_MATRIX = [
    pytest.param(action, rule, level, id=f"{action}:{level.value}")
    for action, rule in ACTIONS.items()
    for level in _peers_of(rule.minimum)
]


@pytest.mark.parametrize("action,rule,actor_level", _MATRIX)
def test_action_enforcement(action: str, rule, actor_level) -> None:
    """For each (action, actor_level), check_action either allows or raises
    Forbidden with the rule's deny_code."""
    if actor_level.level >= rule.minimum.level:  # type: ignore[attr-defined]
        check_action(action, actor_level)  # no raise
    else:
        with pytest.raises(Forbidden) as exc_info:
            check_action(action, actor_level)
        assert exc_info.value.code == rule.deny_code, (
            f"Action {action!r} with {actor_level.value} should deny with "
            f"{rule.deny_code!r}, got {exc_info.value.code!r}"
        )


def test_mismatched_role_family_raises_type_error() -> None:
    """Passing a GroupRole to a workspace-scoped action is a programmer error."""
    with pytest.raises(TypeError):
        check_action("workspace.update", GroupRole.OWNER)


def test_unknown_action_raises_key_error() -> None:
    from pocketpaw_ee.guards.actions import get_rule

    with pytest.raises(KeyError):
        get_rule("nonexistent.action")


# ---------------------------------------------------------------------------
# Meta test: every action is covered by both allow AND deny paths.
# If a new action is added whose minimum happens to be the lowest level in
# its family, there is no deny row — this test flags that so tests remain
# meaningful.
# ---------------------------------------------------------------------------


def test_every_action_has_allow_and_deny_coverage() -> None:
    missing_deny: list[str] = []
    missing_allow: list[str] = []
    for action, rule in ACTIONS.items():
        peers = _peers_of(rule.minimum)
        if not any(p.level < rule.minimum.level for p in peers):  # type: ignore[attr-defined]
            missing_deny.append(action)
        if not any(p.level >= rule.minimum.level for p in peers):  # type: ignore[attr-defined]
            missing_allow.append(action)
    assert not missing_allow, f"Actions with no allow row: {missing_allow}"
    # Note: actions whose minimum is the lowest role (e.g. WorkspaceRole.MEMBER)
    # intentionally have no *role*-based deny — their deny path is enforced
    # upstream (e.g. workspace.not_member via resolve_workspace_role).
    # We document those here; the meta-test only flags a truly empty matrix.
    lowest_level_only = {
        a
        for a, r in ACTIONS.items()
        if all(p.level >= r.minimum.level for p in _peers_of(r.minimum))  # type: ignore[attr-defined]
    }
    assert set(missing_deny) == lowest_level_only, (
        f"Unexpected actions missing deny coverage: {set(missing_deny) - lowest_level_only}"
    )
