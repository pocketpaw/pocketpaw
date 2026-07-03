# atlas_provider.py — role-aware atlas EntitlementProvider for the cloud chat
#   agent (WA-3). Created: 2026-07-03 (feat/workspace-admin-tools).
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
#   * ``prime()`` — async. Resolves the caller's role ONCE (cached) via the same
#     identity the admin tools use: ``current_user_id`` / ``current_workspace_id``
#     ContextVars → load the ``User`` Beanie doc → ``resolve_workspace_role``.
#     The sdk_mcp_atlas handler awaits this before the sync grant filter so the
#     async DB load runs in the handler's own event loop (``is_granted`` stays
#     sync, per the protocol). Priming is idempotent; a failure leaves the role
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
    resolved from the per-stream identity ContextVars at ``prime()`` time and
    cached for the sync ``is_granted`` calls that follow.
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
        # ``_primed`` guards against re-resolving on every call within one query.
        self._role_level: int | None = None
        self._primed = False

    # -- connector delegation ------------------------------------------------

    def connected_connector_names(self) -> set[str]:
        """Delegate to the default provider's connector-state seam (WA-3 adds no
        connector logic — only a role gate)."""
        return self._default.connected_connector_names()

    # -- role resolution -----------------------------------------------------

    async def prime(self) -> None:
        """Resolve the caller's workspace role once (cached), fail-closed.

        Uses the SAME identity the admin tools read: the per-stream
        ``current_user_id`` / ``current_workspace_id`` ContextVars, the ``User``
        Beanie doc, and ``resolve_workspace_role``. On ANY failure — no identity,
        user not found, not a member of this workspace, a scope/workspace
        mismatch, or a DB error — the role stays ``None`` (unresolved) so every
        role-gated entry is hidden. Never raises. Idempotent within a run: once
        primed it does not re-load (the atlas server is built per run/stream)."""
        if self._primed:
            return
        self._primed = True  # mark first so a raise below still leaves us "primed but unresolved"
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
