# Platform authority axis — cross-tenant operator roles, parallel to workspace RBAC.
# Created: 2026-09-14 (feat/platform-authority-axis) — chunk 1 of the Paw Admin PRD.
#
# This is a SECOND authority axis, not an extension of the first. A WorkspaceRole
# answers "what may this user do inside one tenant"; a PlatformRole answers "may
# this user act across tenants at all". Nothing here reads workspace membership,
# and no workspace role grants anything on this axis — a workspace OWNER is the
# ceiling of their own tenant and nothing outside it.
#
# Why not reuse the existing machinery:
#
#   - ``WorkspaceRole.OWNER`` is level 4 and the top of that enum, and
#     ``ActionRule.minimum`` is typed ``WorkspaceRole | GroupRole | PocketAccess``.
#     There is no value in those families that means "platform-wide", so the
#     rule table cannot express one.
#   - ``check_workspace_action`` resolves membership FIRST and denies a
#     non-member before any override is consulted. That ordering is a deliberate
#     security property. Widening it so some users may skip membership would put
#     this axis inside the code path every tenant request already flows through,
#     which is the one place we should not be adding a bypass.
#   - ``is_superuser`` is not reusable either: it gates zero EE routes, its only
#     effect is setting ``request.state.full_access`` for OSS ``require_scope``
#     routes, and it was deliberately narrowed in June 2026 as a
#     privilege-escalation fix. Overloading it would re-entangle two things that
#     were separated on purpose.
#
# Two rungs, because the split that matters operationally is read vs. write.
# Anything finer (per-surface grants, break-glass, time-boxed elevation) waits
# until there is a second operator to need it.

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pocketpaw_ee.guards.rbac import Forbidden


class PlatformRole(StrEnum):
    """Cross-tenant operator rung. Stored on ``User.platform_role``.

    ``None`` on that field (the default for every existing row) means "no
    platform access", and is deliberately not a member of this enum: absence
    must not be representable as a rung.
    """

    SUPPORT = "support"
    OPERATOR = "operator"

    @classmethod
    def from_str(cls, value: str) -> PlatformRole:
        try:
            return cls(value.lower())
        except ValueError:
            raise ValueError(f"Unknown platform role: {value!r}") from None

    @property
    def level(self) -> int:
        return _PLATFORM_ROLE_LEVELS[self]


# A separate mapping rather than enum order, so reordering or inserting a
# member cannot silently re-rank the existing ones.
_PLATFORM_ROLE_LEVELS: dict[PlatformRole, int] = {
    PlatformRole.SUPPORT: 1,
    PlatformRole.OPERATOR: 2,
}


@dataclass(frozen=True)
class PlatformActionRule:
    """Minimum rung for one platform action, plus the stable denial code.

    Mirrors ``guards.actions.ActionRule`` deliberately: same shape, different
    role family, so the two registries read the same way while neither can be
    passed where the other is expected.
    """

    minimum: PlatformRole
    deny_code: str = "platform.insufficient_role"


# ---------------------------------------------------------------------------
# The registry. Every route under /api/v1/platform names one of these.
#
# Read actions are SUPPORT. Anything that can change a tenant's money, limits,
# model policy, or the platform's own configuration is OPERATOR.
# ---------------------------------------------------------------------------

PLATFORM_ACTIONS: dict[str, PlatformActionRule] = {
    # Tenant directory and detail (chunk 2).
    "platform.workspace.read": PlatformActionRule(PlatformRole.SUPPORT),
    "platform.user.read": PlatformActionRule(PlatformRole.SUPPORT),
    # Members and invitations on a tenant's behalf (chunk 5).
    "platform.member.read": PlatformActionRule(PlatformRole.SUPPORT),
    "platform.member.write": PlatformActionRule(PlatformRole.OPERATOR),
    # Wallet and ledger (chunk 6). Reading a balance is support work; moving
    # credits is not.
    "platform.credits.read": PlatformActionRule(PlatformRole.SUPPORT),
    "platform.credits.adjust": PlatformActionRule(PlatformRole.OPERATOR),
    # Plans and entitlement overrides (chunk 7).
    "platform.entitlements.read": PlatformActionRule(PlatformRole.SUPPORT),
    "platform.entitlements.write": PlatformActionRule(PlatformRole.OPERATOR),
    # Stats, rollups, revenue (chunks 8-9).
    "platform.stats.read": PlatformActionRule(PlatformRole.SUPPORT),
    "platform.revenue.read": PlatformActionRule(PlatformRole.SUPPORT),
    # Model catalog and per-tenant policy (chunk 10). The catalog read is
    # SUPPORT; busting the cache and re-provisioning keys are not.
    "platform.models.read": PlatformActionRule(PlatformRole.SUPPORT),
    "platform.models.write": PlatformActionRule(PlatformRole.OPERATOR),
    # Platform settings and health (chunk 11). The settings READ is OPERATOR,
    # unlike every other read here: that screen renders resolved configuration,
    # which includes credential-shaped settings and the deployment's own
    # topology. That is not support-tier information.
    "platform.settings.read": PlatformActionRule(PlatformRole.OPERATOR),
    "platform.settings.write": PlatformActionRule(PlatformRole.OPERATOR),
    "platform.health.read": PlatformActionRule(PlatformRole.SUPPORT),
    # The operator audit trail itself (chunk 1).
    "platform.audit.read": PlatformActionRule(PlatformRole.SUPPORT),
    # Granting platform access. OPERATOR, and deliberately the same rung as any
    # other write rather than a higher one: a third rung existing solely to
    # guard this route would be a rung with one member and no real coverage.
    "platform.role.grant": PlatformActionRule(PlatformRole.OPERATOR),
    "platform.role.revoke": PlatformActionRule(PlatformRole.OPERATOR),
}


def get_platform_rule(action: str) -> PlatformActionRule:
    """Look up a rule, raising on an unregistered action.

    Fails closed and loudly. An action string that is not in the registry is a
    programming error, and returning a permissive default for one is how an
    unguarded route ships looking guarded.
    """
    try:
        return PLATFORM_ACTIONS[action]
    except KeyError:
        raise KeyError(
            f"Unregistered platform action: {action!r}. "
            "Add it to PLATFORM_ACTIONS in guards/platform.py."
        ) from None


def check_platform_action(action: str, role: PlatformRole | str | None) -> None:
    """Raise ``Forbidden`` unless ``role`` satisfies ``action``.

    ``role`` is the caller's ``User.platform_role``. ``None`` — every user in
    the database today — always denies. So does any string that is not a known
    rung: a value this build does not recognise is treated as no access rather
    than as access, so an older node cannot be talked into honouring a rung it
    has never heard of.
    """
    rule = get_platform_rule(action)

    if role is None:
        raise Forbidden(
            code="platform.not_operator",
            detail="This account has no platform role.",
        )

    try:
        resolved = role if isinstance(role, PlatformRole) else PlatformRole.from_str(role)
    except ValueError:
        raise Forbidden(
            code="platform.not_operator",
            detail="Unrecognised platform role.",
        ) from None

    if resolved.level < rule.minimum.level:
        raise Forbidden(
            code=rule.deny_code,
            detail=f"Requires {rule.minimum.value}, got {resolved.value}",
        )


__all__ = [
    "PLATFORM_ACTIONS",
    "PlatformActionRule",
    "PlatformRole",
    "check_platform_action",
    "get_platform_rule",
]
