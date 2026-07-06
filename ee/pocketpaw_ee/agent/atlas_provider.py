# atlas_provider.py — role-aware atlas EntitlementProvider for the cloud chat
#   agent (WA-3). Created: 2026-07-03 (feat/workspace-admin-tools).
# Updated: 2026-07-05 (fix/atlas-admin-security-hardening, FINDING D) — prime()
#   now RE-RESOLVES the caller's role every turn instead of once per provider
#   instance. The provider rides the WARM ClaudeSDKClient, which is shared across
#   users of a workspace/public/shared pocket (its cache key omits user_id). The
#   old ``_primed`` idempotency cached the FIRST caller's role for the provider's
#   lifetime, so a MEMBER querying atlas after an OWNER saw the OWNER's
#   role:admin / role:owner capability cards (and a mid-session role change stayed
#   frozen — the P3). prime() now drops ``_primed`` and resets ``_role_level`` to
#   None up front (fail-closed) before re-resolving from the CURRENT identity
#   ContextVars, so ``is_granted`` reflects the live caller. DISCOVERY-LAYER ONLY:
#   the RBAC gate inside each admin tool already re-checks the live role, so this
#   is a capability-DISCLOSURE leak (a member seeing cards they can't use), not an
#   enforcement bypass — but a real leak worth fixing. Warm-client sharing is left
#   untouched (the surgical option).
#
# The DISCOVERY layer for the workspace-admin tools — NOT the security gate.
# The RBAC checks INSIDE each admin tool (workspace_admin.py) are the real lock
# and are already audited; this provider only decides what a member SEES in
# atlas_search / atlas_describe, so a non-admin doesn't see admin capabilities
# they can't use. It must NEVER be relied on as the execution gate.
#
# Shape: implements the core ``EntitlementProvider`` protocol (overlay.py):
#   * ``is_granted(entry)`` — a role-BLIND entry (no ``role:*`` in requires)
#     delegates to the wrapped ``DefaultEntitlementProvider`` (grant, subject to
#     the existing connector/availability logic). A role-GATED entry is granted
#     only if the CALLER's resolved workspace role >= the entry's required tier
#     (owner > admin > member). FAIL-CLOSED: if the role can't be resolved (no
#     identity, user not found, not a member, error) the role stays ``None`` and
#     every role-gated entry is hidden. Never raises.
#   * ``connected_connector_names()`` — pure delegation to the wrapped default
#     provider (reuse the connector-state seam; don't reimplement).
#   * ``prime()`` — async. Resolves the caller's role EACH TURN (FINDING D — no
#     longer cached for the provider's lifetime) via the same identity the admin
#     tools use: ``current_user_id`` / ``current_workspace_id`` ContextVars → load
#     the ``User`` Beanie doc → ``resolve_workspace_role``. The sdk_mcp_atlas
#     handler awaits this before the sync grant filter so the async DB load runs
#     in the handler's own event loop (``is_granted`` stays sync, per the
#     protocol). It resets the cached level up front, so a failure leaves the role
#     unresolved (fail-closed) and is swallowed by the caller.
#
# Wiring: core's ``atlas.overlay.build_role_aware_provider(scope_key)`` imports
# this module inside a try/except (optional EE seam, mirrors
# ``atlas.fabric.build_workspace_fabric_introspector``) and constructs one per
# run bound to the run's ``ws:<id>`` scope. claude_sdk.py selects it over the
# role-blind default when a real ``ws:<id>`` scope exists.

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pocketpaw.atlas.overlay import (
    ROLE_LEVELS,
    DefaultEntitlementProvider,
    entry_role_requirement,
)

if TYPE_CHECKING:
    from pocketpaw.atlas.model import AtlasEntry

logger = logging.getLogger(__name__)


class RoleAwareEntitlementProvider:
    """Entitlement provider that grants ``role:*`` atlas entries by workspace role.

    Wraps a ``DefaultEntitlementProvider`` for all non-role behavior (connector
    availability + granting role-blind entries) and adds a role gate on top for
    entries carrying a ``role:<tier>`` marker. Bound to ONE ``ws:<id>`` scope at
    construction — per-run, never a process-global. The caller's role is
    re-resolved from the per-stream identity ContextVars on EVERY ``prime()`` (once
    per turn — FINDING D), then read by the sync ``is_granted`` calls that follow.
    It is NOT cached for the provider's lifetime, because the provider can ride a
    warm client shared across users.
    """

    def __init__(self, scope_key: str, registry: Any | None = None) -> None:
        if not isinstance(scope_key, str) or not scope_key.startswith("ws:"):
            # A role-aware provider only makes sense under a real workspace scope.
            # Refuse anything else so the core bridge falls back to the default.
            raise ValueError(
                f"RoleAwareEntitlementProvider needs a ws:<id> scope, got {scope_key!r}"
            )
        self._scope_key = scope_key
        # Reuse the OSS default for connector availability + role-blind grants.
        self._default = DefaultEntitlementProvider(scope_key=scope_key, registry=registry)
        # Resolved caller role level (int from ROLE_LEVELS) or None = unresolved.
        # FINDING D: re-resolved on EVERY prime() (once per turn), not cached for
        # the provider's lifetime. The provider rides the WARM ClaudeSDKClient,
        # which is shared across users of a workspace/public pocket (its cache key
        # omits user_id), so a once-per-instance cache froze the FIRST caller's
        # role and leaked their admin/owner cards to later callers. There is no
        # ``_primed`` flag any more — prime() always reflects the CURRENT identity.
        self._role_level: int | None = None

    # -- connector delegation ------------------------------------------------

    def connected_connector_names(self) -> set[str]:
        """Delegate to the default provider's connector-state seam (WA-3 adds no
        connector logic — only a role gate)."""
        return self._default.connected_connector_names()

    # -- role resolution -----------------------------------------------------

    async def prime(self) -> None:
        """Resolve the CURRENT caller's workspace role, per turn, fail-closed.

        Uses the SAME identity the admin tools read: the per-stream
        ``current_user_id`` / ``current_workspace_id`` ContextVars, the ``User``
        Beanie doc, and ``resolve_workspace_role``. On ANY failure — no identity,
        user not found, not a member of this workspace, a scope/workspace
        mismatch, or a DB error — the role stays ``None`` (unresolved) so every
        role-gated entry is hidden. Never raises.

        FINDING D: this RE-RESOLVES on every call (the sdk_mcp_atlas handler awaits
        it before each turn's grant filter), it is NOT cached for the provider's
        lifetime. The provider rides the warm ClaudeSDKClient shared across users
        of a workspace/public pocket, so a once-per-instance cache would freeze the
        first caller's role and leak their admin/owner cards to a later member (and
        would also freeze a mid-session role change). ``_role_level`` is reset to
        ``None`` up front so a failed re-resolution can never leave a STALE role
        behind — fail-closed on every turn."""
        # Reset first: an unresolved (or newly-restricted) caller must never
        # inherit the previous turn's role level.
        self._role_level = None
        try:
            from pocketpaw_ee.cloud.chat.agent_service import (
                current_user_id,
                current_workspace_id,
            )

            user_id = current_user_id()
            workspace_id = current_workspace_id()
            if not user_id or not workspace_id:
                logger.debug("atlas role-aware provider: no identity on stream — role unresolved")
                return

            # Defense in depth: the provider is bound to ws:<id>; the identity's
            # workspace must match it, or we refuse to resolve (fail-closed).
            if self._scope_key != f"ws:{workspace_id}":
                logger.debug(
                    "atlas role-aware provider: scope %s != identity workspace ws:%s — unresolved",
                    self._scope_key,
                    workspace_id,
                )
                return

            user = await self._load_user(user_id)
            if user is None:
                logger.debug(
                    "atlas role-aware provider: user %s not found — role unresolved", user_id
                )
                return

            from pocketpaw_ee.guards.deps import resolve_workspace_role
            from pocketpaw_ee.guards.rbac import Forbidden

            try:
                role = resolve_workspace_role(user, workspace_id)
            except Forbidden:
                # Not a member of this workspace → no role, hide gated entries.
                logger.debug(
                    "atlas role-aware provider: user %s not a member of %s — role unresolved",
                    user_id,
                    workspace_id,
                )
                return

            self._role_level = ROLE_LEVELS.get(str(role.value), None)
        except Exception as exc:  # noqa: BLE001 — fail-closed: unresolved role hides gated entries
            logger.debug("atlas role-aware provider: role resolution failed: %s", exc)

    @staticmethod
    async def _load_user(user_id: str) -> Any | None:
        """Load the ``User`` Beanie doc (the same load the admin tools use). Lazy
        import keeps the module's top-level surface free of an ee.cloud
        dependency. Returns ``None`` on a bad id / missing user / DB error."""
        from beanie import PydanticObjectId

        from pocketpaw_ee.cloud.models.user import User as _UserDoc

        try:
            return await _UserDoc.get(PydanticObjectId(user_id))
        except Exception:  # noqa: BLE001 — malformed id / DB error → treat as no user
            return None

    # -- grant gate ----------------------------------------------------------

    def is_granted(self, entry: AtlasEntry) -> bool:
        """Grant *entry* for the caller's resolved role, fail-closed.

        A role-blind entry delegates to the default provider (unchanged OSS
        grant). A role-gated entry is granted only if the caller's resolved role
        level clears the entry's required tier; an unresolved role (prime never
        ran, or failed) or an unknown required tier hides it. Never raises."""
        required_tier = entry_role_requirement(entry)
        if required_tier is None:
            # Not role-gated — the default provider decides (grants role-blind
            # entries; it also hides role-gated ones, but that branch is dead
            # here since required_tier is None).
            return self._default.is_granted(entry) is True

        required_level = ROLE_LEVELS.get(required_tier)
        if required_level is None:
            # Unknown role marker — fail-closed, hide it.
            logger.debug(
                "atlas role-aware provider: unknown role tier %r on %s", required_tier, entry.id
            )
            return False
        if self._role_level is None:
            # Role unresolved → hide every role-gated entry (fail-closed).
            return False
        return self._role_level >= required_level


__all__ = ["RoleAwareEntitlementProvider"]
