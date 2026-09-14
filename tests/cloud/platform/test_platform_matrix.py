"""Matrix test for PLATFORM_ACTIONS, mirroring tests/cloud/test_rbac_matrix.py.

Every row in ``ee/pocketpaw_ee/guards/platform.py:PLATFORM_ACTIONS`` is exercised
against every rung, in both the allow and the deny direction. The meta test at
the bottom makes it impossible to add an action without covering it.

The cases that matter most here are the ones that are NOT about ranking:
``None`` (what every user in the database holds today), a workspace role handed
in by mistake, and an unrecognised string. All three must deny. A bug in any of
them is silent and grants cross-tenant access.
"""

from __future__ import annotations

import pytest
from pocketpaw_ee.guards.platform import (
    PLATFORM_ACTIONS,
    PlatformRole,
    check_platform_action,
    get_platform_rule,
)
from pocketpaw_ee.guards.rbac import Forbidden, WorkspaceRole

_RUNGS = sorted(PlatformRole, key=lambda r: r.level)

_MATRIX = [
    pytest.param(action, rule, rung, id=f"{action}:{rung.value}")
    for action, rule in PLATFORM_ACTIONS.items()
    for rung in _RUNGS
]


@pytest.mark.parametrize("action,rule,actor_rung", _MATRIX)
def test_action_enforcement(action: str, rule, actor_rung: PlatformRole) -> None:
    """Each (action, rung) either passes or raises Forbidden with the rule's code."""
    if actor_rung.level >= rule.minimum.level:
        check_platform_action(action, actor_rung)  # must not raise
    else:
        with pytest.raises(Forbidden) as exc_info:
            check_platform_action(action, actor_rung)
        assert exc_info.value.code == rule.deny_code


@pytest.mark.parametrize("action", sorted(PLATFORM_ACTIONS))
def test_no_platform_role_denies_everything(action: str) -> None:
    """``None`` is what every existing row holds. It must never satisfy anything."""
    with pytest.raises(Forbidden) as exc_info:
        check_platform_action(action, None)
    assert exc_info.value.code == "platform.not_operator"


@pytest.mark.parametrize("action", sorted(PLATFORM_ACTIONS))
@pytest.mark.parametrize("workspace_role", [r.value for r in WorkspaceRole])
def test_workspace_roles_are_not_platform_roles(action: str, workspace_role: str) -> None:
    """The two axes are parallel. A workspace OWNER is not a platform operator.

    This is the single most important assertion in the file. If the axes ever
    get merged, or a role string is compared across families, this is what
    catches it — and the failure it prevents is cross-tenant data access by
    anyone who can create a workspace, which is anyone at all.
    """
    with pytest.raises(Forbidden):
        check_platform_action(action, workspace_role)


@pytest.mark.parametrize(
    "bogus",
    ["", "OPERATOR ", "root", "superuser", "admin", "platform", "operator-ish", "0", "None"],
)
def test_unrecognised_values_deny(bogus: str) -> None:
    """Anything unparseable denies, rather than being treated as a rung.

    Covers the forward-compatibility case too: if a newer node introduces a rung
    this build has never heard of, an older node must refuse it rather than
    honour a role it cannot reason about.
    """
    with pytest.raises(Forbidden):
        check_platform_action("platform.audit.read", bogus)


def test_case_is_normalised() -> None:
    """A rung stored with different casing still resolves, and still ranks."""
    check_platform_action("platform.audit.read", "OPERATOR")
    check_platform_action("platform.audit.read", "Support")
    with pytest.raises(Forbidden):
        check_platform_action("platform.settings.write", "Support")


def test_unregistered_action_raises_loudly() -> None:
    """An action not in the registry is a programming error, not a denial.

    It must not quietly resolve to a permissive default — that is how an
    unguarded route ships looking guarded.
    """
    with pytest.raises(KeyError):
        get_platform_rule("platform.does.not.exist")
    with pytest.raises(KeyError):
        check_platform_action("platform.does.not.exist", PlatformRole.OPERATOR)


def test_operator_outranks_support() -> None:
    """The one ranking fact the whole axis rests on."""
    assert PlatformRole.OPERATOR.level > PlatformRole.SUPPORT.level


def test_every_action_is_namespaced() -> None:
    """Every key starts with ``platform.``.

    The prefix is how a reader tells at a glance which registry an action string
    belongs to, and it keeps the two registries from colliding if they are ever
    merged into one lookup.
    """
    unprefixed = [a for a in PLATFORM_ACTIONS if not a.startswith("platform.")]
    assert unprefixed == [], f"Actions missing the 'platform.' prefix: {unprefixed}"


def test_write_actions_require_operator() -> None:
    """Nothing that mutates is reachable from the read rung.

    Enforced by naming convention on purpose: a new ``*.write`` / ``*.adjust``
    action that is accidentally registered at SUPPORT fails here rather than
    shipping as a support-tier way to move money.
    """
    mutating = [
        action
        for action in PLATFORM_ACTIONS
        if action.rsplit(".", 1)[-1] in {"write", "adjust", "grant", "revoke"}
    ]
    assert mutating, "sanity: the registry should contain mutating actions"

    too_permissive = [
        action
        for action in mutating
        if PLATFORM_ACTIONS[action].minimum is not PlatformRole.OPERATOR
    ]
    assert too_permissive == [], f"Mutating actions not gated on operator: {too_permissive}"


def test_matrix_covers_every_registered_action() -> None:
    """Meta test: no action can be added without being exercised above."""
    covered = {param.values[0] for param in _MATRIX}
    assert covered == set(PLATFORM_ACTIONS), (
        "Every PLATFORM_ACTIONS row must appear in the matrix. "
        f"Missing: {set(PLATFORM_ACTIONS) - covered}"
    )
