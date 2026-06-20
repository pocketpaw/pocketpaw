# broker.py — InMemoryCredentialBroker: the reference CredentialBroker impl.
# Created: 2026-06-02 (feat/retrieval-rehome, #1327) — re-homed from
# soul-protocol's deleted ``engine/retrieval/broker.py``. soul-protocol 0.4.0
# (#179) moved the ``Credential`` model + the ``CredentialBroker`` protocol +
# the credential exception classes into ``soul_protocol.spec.retrieval`` and
# deleted the concrete in-memory implementation. That implementation is
# application-layer infrastructure, so it lives here in the consuming runtime.
#
# Adapted to the 0.4.0 surface:
#   * ``Credential``, ``CredentialBroker``, ``CredentialExpiredError`` and
#     ``CredentialScopeError`` now import from ``soul_protocol.spec.retrieval``
#     (they used to live alongside this code under engine/retrieval/). The
#     ``Credential`` field set is byte-for-byte the same as the old engine
#     version, so the broker's construction call is unchanged.
#   * ``scopes_overlap`` still lives at ``soul_protocol.engine.journal`` — the
#     0.4.0 prune left the journal scope helpers in place.
#   * ``Credential`` is re-exported from this module for callers that used to
#     import it from the engine package; the canonical home is now the spec.
#
# The broker mints short-lived credentials for external sources (Drive,
# Salesforce, Snowflake, ...). Scoped per DSP scope so a credential acquired
# for ``org:sales:*`` cannot be reused by a requester operating in
# ``org:support:*``. Every acquire/use/expire emits a journal event when a
# journal is attached — that's the audit trail Zero-Copy federation depends on.
#
# ``InMemoryCredentialBroker`` is the reference impl: not persistent, not
# distributed. Production deployments swap in a broker backed by the
# platform's real secret store. ``Credential.token`` is deliberately an opaque
# string — real brokers produce bearer tokens / OAuth access tokens / signed
# JWTs; callers that need a structured token wrap this class.

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from soul_protocol.engine.journal import Journal, scopes_overlap
from soul_protocol.spec.journal import Actor, EventEntry
from soul_protocol.spec.retrieval import (
    Credential,
    CredentialExpiredError,
    CredentialScopeError,
)

# Re-export so callers that historically did ``from pocketpaw.retrieval import
# Credential`` keep working. The canonical definition lives in the 0.4.0 spec.
__all__ = ["Credential", "InMemoryCredentialBroker", "DEFAULT_TTL_S"]

DEFAULT_TTL_S: float = 300.0


class InMemoryCredentialBroker:
    """Reference broker. Not persistent, not distributed — fine for
    single-process Paw OS instances and tests."""

    def __init__(
        self,
        *,
        ttl_s: float = DEFAULT_TTL_S,
        journal: Journal | None = None,
        broker_actor: Actor | None = None,
    ) -> None:
        self._ttl_s = ttl_s
        self._journal = journal
        self._actor = broker_actor or Actor(kind="system", id="system:credential-broker")
        self._active: dict[UUID, Credential] = {}

    # -- lifecycle --------------------------------------------------------

    def acquire(self, source: str, scopes: list[str]) -> Credential:
        now = datetime.now(UTC)
        cred = Credential(
            source=source,
            scopes=list(scopes),
            token=secrets.token_urlsafe(16),
            acquired_at=now,
            expires_at=now + timedelta(seconds=self._ttl_s),
        )
        # Emit FIRST. If the audit append fails under the fail-closed policy,
        # the credential never enters ``_active`` and the caller gets the
        # exception — no orphan credentials hanging around post-failure.
        self._emit("credential.acquired", cred)
        self._active[cred.id] = cred
        return cred

    def ensure_usable(self, credential: Credential, requester_scopes: list[str]) -> None:
        if credential.is_expired():
            # Surface through the journal once, then forget the credential.
            if credential.id in self._active:
                self._emit("credential.expired", credential)
                self._active.pop(credential.id, None)
            raise CredentialExpiredError(
                f"credential {credential.id} for {credential.source} expired at "
                f"{credential.expires_at.isoformat()}"
            )
        if not scopes_overlap(credential.scopes, requester_scopes):
            raise CredentialScopeError(
                f"credential {credential.id} scoped to {credential.scopes} "
                f"cannot be used by requester with scopes {requester_scopes}"
            )

    def mark_used(self, credential: Credential) -> None:
        credential.last_used_at = datetime.now(UTC)
        self._emit("credential.used", credential)

    def revoke(self, credential_id: UUID) -> None:
        cred = self._active.pop(credential_id, None)
        if cred is not None:
            self._emit("credential.expired", cred)

    # -- journal glue -----------------------------------------------------

    def _emit(self, action: str, cred: Credential) -> None:
        """Append a credential-lifecycle event. Fail-closed by design.

        If no journal is configured the caller has explicitly opted into
        fire-and-forget operation (tests, ephemeral scripts). With a journal
        attached, a failed append propagates: audit integrity outranks broker
        availability on the credential path, and silently issuing / revoking a
        credential whose lifecycle never made it into the log is worse than
        surfacing the error to the caller.

        Contrast with the router's ``retrieval.query`` emit, which stays
        fire-and-forget — that's a query log, not an auth trail.
        """
        if self._journal is None:
            return
        entry = EventEntry(
            id=uuid4(),
            ts=datetime.now(UTC),
            actor=self._actor,
            action=action,
            scope=list(cred.scopes),
            payload={
                "credential_id": str(cred.id),
                "source": cred.source,
                "expires_at": cred.expires_at.isoformat(),
            },
        )
        self._journal.append(entry)
