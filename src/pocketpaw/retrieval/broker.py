# broker.py — InMemoryCredentialBroker: reference implementation of the
# CredentialBroker Protocol from soul_protocol.spec.retrieval.
# Updated: feat/receive-retrieval-infra (2026-04-19) — moved here from
# soul-protocol/engine/retrieval/broker.py as part of the 0.3.2 split. The
# Credential data class and CredentialBroker Protocol now live in
# soul_protocol.spec.retrieval (the standard); this file keeps only the
# concrete in-memory broker — application-layer, not part of the standard.
# Credential lifecycle journal emits remain fail-closed: if a journal is
# attached and append raises, the exception surfaces to the caller instead
# of silently issuing / using / revoking a credential that never made it
# into the audit trail.
#
# The broker mints short-lived credentials for external sources (Drive,
# Salesforce, Snowflake, ...). Scoped per DSP scope so a credential
# acquired for `org:sales:*` cannot be reused by a requester operating in
# `org:support:*`. Every acquire/use/expire emits a journal event when a
# journal is attached — that's the audit trail Zero-Copy federation
# depends on.
#
# `InMemoryCredentialBroker` is the reference impl. Production deployments
# swap in a broker backed by the platform's real secret store; this class
# is the one the tests and local dev use.

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
        # the credential never enters `_active` and the caller gets the
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
        availability on the credential path, and silently issuing /
        revoking a credential whose lifecycle never made it into the log is
        worse than surfacing the error to the caller.

        Contrast with the router's `retrieval.query` emit, which stays
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
