"""PlatformAuditEvent — the operator action log, which its subjects cannot erase.

Created: 2026-09-14 (feat/platform-authority-axis) — chunk 1 of the Paw Admin PRD.

A SEPARATE collection from ``AuditEvent``, and the separation is the point.

``AuditEvent.workspace`` is required, every index and read path is keyed on it,
and ``purge_workspace_audit`` deletes by ``{workspace, at < cutoff}`` driven by
``WorkspaceSettings.retention_days`` — a value the TENANT controls. Filing
operator actions there would let a customer set a 30-day retention and thereby
delete the record of what the platform did to their account. That is a
compliance defect rather than a preference, and no amount of care at the call
site fixes it, because the deletion is driven by the subject of the record.

The existing collection also has no cross-workspace read path at all: both of
its read routes bind a single ``workspace_id``, so "show me everything this
operator did last week" is unanswerable there by construction.

Two more deliberate differences from ``AuditEvent``:

  - **No TTL.** ``AuditEvent`` expires after a year. This does not expire, so the
    collection grows without bound. That is the intended trade for an operator
    log; the retention number is a decision the captain owes this PRD, and an
    unbounded log is the safe default to hold until it is made.
  - **``reason`` is required and has no default.** An operator write without a
    stated reason is not a record of anything. Making it non-optional here means
    a caller that forgets one fails at construction rather than writing a row
    that looks complete and explains nothing.

This is the durable, queryable record. ``guards.audit.log_privileged_action``
is the complementary structured-log line for the same event — that one goes to
the process audit logger and is what an external SIEM tails. Privileged platform
writes emit BOTH: the log line cannot be queried by target, and the document
cannot be shipped off-box in real time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from beanie import Document, Indexed
from pydantic import Field
from pymongo import IndexModel


class PlatformAuditEvent(Document):
    """One privileged action taken by a platform operator against a tenant."""

    # Who acted. Always a real user id — never a service account, because every
    # route that writes one of these is reached through an operator session.
    actor_id: Indexed(str)  # type: ignore[valid-type]
    actor_email: str = ""
    # The rung the actor held AT THE TIME. Denormalised on purpose: reading the
    # actor's current role later answers a different question than "what were
    # they entitled to when they did this".
    actor_platform_role: str = ""

    # The registry key from guards/platform.py PLATFORM_ACTIONS.
    action: Indexed(str)  # type: ignore[valid-type]

    # What was acted on. Both nullable because not every platform action targets
    # a tenant (a settings write targets the deployment) and not every one
    # targets a user.
    target_type: str = ""
    target_workspace: Indexed(str) | None = None  # type: ignore[valid-type]
    target_user: str | None = None

    # Required, operator-supplied, free text. See the module docstring.
    reason: str

    # Outcome. Exists because the ordering of "write the record" against "do the
    # thing" has no free lunch:
    #
    #   record-then-act  - if the act fails, the log claims something happened
    #                      that did not. A false positive.
    #   act-then-record  - if the record fails, money moved and nothing says so.
    #                      A false negative.
    #
    # For a compliance log a false negative is the worse failure, so the house
    # rule is record FIRST as "attempted", then settle to "applied" or "failed".
    # A row left at "attempted" is itself meaningful: it means the process died
    # mid-action, which is exactly the case someone will need to reconstruct.
    status: str = Field(default="applied", pattern="^(attempted|applied|failed)$")

    # Before/after values for the fields the action changed. Free-form because
    # the shape differs per action; the alternative is a typed payload per
    # action, which ossifies the log against the routes that write it.
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)

    # Where the action came from. Useful when an operator account is suspected
    # of being compromised, which is the scenario this whole collection exists
    # to make answerable.
    ip: str | None = None
    user_agent: str | None = None

    at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "platform_audit_events"
        indexes = [
            # "What happened to this tenant" — the support question.
            IndexModel([("target_workspace", 1), ("at", -1)]),
            # "What did this operator do" — the oversight question, and the one
            # the tenant-scoped AuditEvent collection cannot answer at all.
            IndexModel([("actor_id", 1), ("at", -1)]),
            # The global reverse-chronological feed.
            IndexModel([("at", -1)]),
        ]
        # Deliberately NO TTL index here. See the module docstring.


__all__ = ["PlatformAuditEvent"]
