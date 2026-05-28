# events.py — Realtime Event dataclass registry for the cloud bus.
# Each subclass pins an EVENT_TYPE literal that routes both the WebSocket
# fan-out and any in-process bus subscribers.
# Updated: 2026-05-22 (RFC 05 M2b.2) — added PocketOutcomeEvent
#   (type="pocket.outcome"), emitted after a successful write action whose
#   binding declared a named `outcome`. Feeds the outcomes JSONL ledger.
# Updated: 2026-05-28 (feat/wave-3a-instinct-dispatch) — added the three
#   instinct.approval.* events (created / approved / rejected) for the
#   RFC 03 v2 template-level approval queue. Emitted by
#   ``instinct_approvals.service`` on every state-mutating function per
#   the EE cloud rule 9 (``emit on every write``).
# Updated: 2026-05-28 (feat/wave-3b-action-pipeline) — added
#   ``BulkActionDispatched`` (type="pocket.bulk_action.dispatched"),
#   emitted by ``pockets.service.dispatch_bulk_action`` after a bulk
#   fan-out completes. Carries the dispatch summary (counts + optional
#   batch approval id) so downstream listeners (audit, analytics) can
#   key off a single event per dispatch call.
# Updated: 2026-05-28 (feat/wave-3c-outcomes) — added ``OutcomeEmitted``
#   (type="pocket.outcome_emitted"), emitted by
#   ``pockets.outcomes_emitter.emit_outcomes`` for EACH name declared in
#   an action's ``outcomes_emitted[]`` after a successful write. RFC 03
#   v2's template-driven outcomes catalog is distinct from M2b.2's
#   binding-driven ``PocketOutcomeEvent``: the template-level emit fires
#   per declared catalog event (multiple per action allowed), the M2b.2
#   binding-level emit fires the single ``binding.outcome`` name. Both
#   coexist on the bus under different EVENT_TYPE strings.
# Updated: 2026-05-28 (feat/wave-3d-temporal-scheduler) — added
#   ``TemporalSweepCompleted`` (type="pocket.temporal_sweep_completed"),
#   emitted once per per-pocket sweep tick by
#   ``temporal_sweeps.service.upsert_state``. Carries the sweep tally
#   (edges_fired / blocked / escalated / errors / sweep_duration_ms) so
#   audit + dashboards listen to one event per dispatch rather than N
#   per-row events, the same shape ``BulkActionDispatched`` uses.
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import ClassVar

EVENT_REGISTRY: dict[str, type[Event]] = {}


@dataclass
class Event:
    type: str = ""
    data: dict = field(default_factory=dict)
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        cls_type = getattr(type(self), "EVENT_TYPE", "")
        if cls_type:
            self.type = cls_type

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # Register subclasses by their EVENT_TYPE discriminator so a
        # cross-process consumer can reconstruct the original class from a
        # JSON envelope. Subclasses without EVENT_TYPE (intermediate bases)
        # are skipped.
        evt_type = getattr(cls, "EVENT_TYPE", "")
        if evt_type:
            EVENT_REGISTRY[evt_type] = cls


def rebuild_event(payload: dict) -> Event:
    """Reconstruct an ``Event`` (or matching subclass) from a JSON envelope.

    Unknown ``type`` values fall back to the base ``Event`` so a newer worker
    shipping a fresh event type doesn't crash an older web consumer. Missing
    ``ts`` defaults to now.
    """
    evt_type = str(payload.get("type") or "")
    data = payload.get("data", {})
    if not isinstance(data, dict):
        raise TypeError(f"event payload `data` must be a dict, got {type(data).__name__}")
    ts_raw = payload.get("ts")
    if ts_raw is None:
        ts = datetime.now(UTC)
    else:
        ts = datetime.fromisoformat(str(ts_raw))
    cls = EVENT_REGISTRY.get(evt_type, Event)
    inst = cls(data=data, ts=ts)
    # Subclasses overwrite `type` in __post_init__; the base Event needs the
    # explicit value so listeners that match on the string still work.
    if cls is Event:
        inst.type = evt_type
    return inst


# Workspace
@dataclass
class WorkspaceUpdated(Event):
    EVENT_TYPE: ClassVar[str] = "workspace.updated"


@dataclass
class WorkspaceDeleted(Event):
    EVENT_TYPE: ClassVar[str] = "workspace.deleted"


@dataclass
class WorkspaceMemberAdded(Event):
    EVENT_TYPE: ClassVar[str] = "workspace.member_added"


@dataclass
class WorkspaceMemberRemoved(Event):
    EVENT_TYPE: ClassVar[str] = "workspace.member_removed"


@dataclass
class WorkspaceMemberRole(Event):
    EVENT_TYPE: ClassVar[str] = "workspace.member_role"


@dataclass
class WorkspaceInviteCreated(Event):
    EVENT_TYPE: ClassVar[str] = "workspace.invite.created"


@dataclass
class WorkspaceInviteAccepted(Event):
    EVENT_TYPE: ClassVar[str] = "workspace.invite.accepted"


@dataclass
class WorkspaceInviteRevoked(Event):
    EVENT_TYPE: ClassVar[str] = "workspace.invite.revoked"


# Groups
@dataclass
class GroupCreated(Event):
    EVENT_TYPE: ClassVar[str] = "group.created"


@dataclass
class GroupUpdated(Event):
    EVENT_TYPE: ClassVar[str] = "group.updated"


@dataclass
class GroupDeleted(Event):
    EVENT_TYPE: ClassVar[str] = "group.deleted"


@dataclass
class GroupMemberAdded(Event):
    EVENT_TYPE: ClassVar[str] = "group.member_added"


@dataclass
class GroupJoined(Event):
    """Full group payload delivered only to a newly-added user.

    Lets the recipient's sidebar insert the room without a manual refresh.
    Existing members don't need this (they already have the room); they
    receive ``GroupMemberAdded`` instead.
    """

    EVENT_TYPE: ClassVar[str] = "group.joined"


@dataclass
class GroupMemberRemoved(Event):
    EVENT_TYPE: ClassVar[str] = "group.member_removed"


@dataclass
class GroupMemberRole(Event):
    EVENT_TYPE: ClassVar[str] = "group.member_role"


@dataclass
class GroupAgentAdded(Event):
    EVENT_TYPE: ClassVar[str] = "group.agent_added"


@dataclass
class GroupAgentRemoved(Event):
    EVENT_TYPE: ClassVar[str] = "group.agent_removed"


@dataclass
class GroupAgentUpdated(Event):
    EVENT_TYPE: ClassVar[str] = "group.agent_updated"


@dataclass
class GroupPinned(Event):
    EVENT_TYPE: ClassVar[str] = "group.pinned"


@dataclass
class GroupUnpinned(Event):
    EVENT_TYPE: ClassVar[str] = "group.unpinned"


@dataclass
class GroupUnreadDelta(Event):
    EVENT_TYPE: ClassVar[str] = "group.unread_delta"


# Messages
@dataclass
class MessageNew(Event):
    EVENT_TYPE: ClassVar[str] = "message.new"


@dataclass
class MessageSent(Event):
    EVENT_TYPE: ClassVar[str] = "message.sent"


@dataclass
class ThreadReply(Event):
    EVENT_TYPE: ClassVar[str] = "thread.reply"


@dataclass
class MessageEdited(Event):
    EVENT_TYPE: ClassVar[str] = "message.edited"


@dataclass
class MessageDeleted(Event):
    EVENT_TYPE: ClassVar[str] = "message.deleted"


@dataclass
class MessageReactionAdded(Event):
    EVENT_TYPE: ClassVar[str] = "message.reaction.added"


@dataclass
class MessageReactionRemoved(Event):
    EVENT_TYPE: ClassVar[str] = "message.reaction.removed"


@dataclass
class MessageReaction(Event):
    EVENT_TYPE: ClassVar[str] = "message.reaction"


@dataclass
class MessageRead(Event):
    EVENT_TYPE: ClassVar[str] = "message.read"


@dataclass
class MessageUiStateUpdated(Event):
    """Emitted when a message's inline-Ripple UI state is patched.

    Carries ``message_id``, ``spec_id``, ``state``, plus the routing keys
    needed by the audience resolver (``group_id`` for group messages,
    ``user_id`` for session/pocket messages).
    """

    EVENT_TYPE: ClassVar[str] = "message.ui_state.updated"


@dataclass
class UnreadUpdate(Event):
    EVENT_TYPE: ClassVar[str] = "unread.update"


# Presence
@dataclass
class PresenceOnline(Event):
    EVENT_TYPE: ClassVar[str] = "presence.online"


@dataclass
class PresenceOffline(Event):
    EVENT_TYPE: ClassVar[str] = "presence.offline"


@dataclass
class TypingStart(Event):
    EVENT_TYPE: ClassVar[str] = "typing.start"


@dataclass
class TypingStop(Event):
    EVENT_TYPE: ClassVar[str] = "typing.stop"


# Files
@dataclass
class FileReady(Event):
    EVENT_TYPE: ClassVar[str] = "file.ready"


@dataclass
class FileDeleted(Event):
    EVENT_TYPE: ClassVar[str] = "file.deleted"


# Sessions
@dataclass
class SessionCreated(Event):
    EVENT_TYPE: ClassVar[str] = "session.created"


@dataclass
class SessionUpdated(Event):
    EVENT_TYPE: ClassVar[str] = "session.updated"


@dataclass
class SessionDeleted(Event):
    EVENT_TYPE: ClassVar[str] = "session.deleted"


# Agent
@dataclass
class AgentThinking(Event):
    EVENT_TYPE: ClassVar[str] = "agent.thinking"


@dataclass
class AgentToolStart(Event):
    EVENT_TYPE: ClassVar[str] = "agent.tool_start"


@dataclass
class AgentToolResult(Event):
    EVENT_TYPE: ClassVar[str] = "agent.tool_result"


@dataclass
class AgentError(Event):
    EVENT_TYPE: ClassVar[str] = "agent.error"


@dataclass
class AgentStreamStart(Event):
    EVENT_TYPE: ClassVar[str] = "agent.stream_start"


@dataclass
class AgentStreamChunk(Event):
    EVENT_TYPE: ClassVar[str] = "agent.stream_chunk"


@dataclass
class AgentStreamEnd(Event):
    EVENT_TYPE: ClassVar[str] = "agent.stream_end"


@dataclass
class AgentToolUse(Event):
    EVENT_TYPE: ClassVar[str] = "agent.tool_use"


# Pockets
@dataclass
class PocketCreated(Event):
    EVENT_TYPE: ClassVar[str] = "pocket.created"


@dataclass
class PocketUpdated(Event):
    EVENT_TYPE: ClassVar[str] = "pocket.updated"


@dataclass
class PocketDeleted(Event):
    EVENT_TYPE: ClassVar[str] = "pocket.deleted"


# Pocket outcomes — RFC 05 M2b.2. Emitted AFTER a write action's
# `run_action` returns `ok:true` AND the binding declared a non-null
# `outcome`. A write completing is an *output*; a named `outcome` is the
# business event the operator cares about ("renewal_completed"). The
# outcomes ledger subscriber appends each event to a workspace-scoped
# JSONL file so `GET /outcomes` can count them.
#
# Payload (`data`):
#   outcome             — the binding's `outcome` name (str)
#   pocket_id           — the pocket the write ran on
#   workspace_id        — tenancy
#   action              — the action name from `rippleSpec.actions`
#   actor               — the user who triggered (direct) or approved
#                          (gated) the write
#   via_instinct        — True when the write was routed through Instinct
#   instinct_action_id  — the Instinct Action id, or None for a direct write
#   occurred_at         — ISO-8601 UTC timestamp
#   outcome_value       — reserved for Layer 4 billing; always None here
#   outcome_unit        — reserved for Layer 4 billing; always None here
@dataclass
class PocketOutcomeEvent(Event):
    EVENT_TYPE: ClassVar[str] = "pocket.outcome"


# Tasks (Mission Control work-item primitive)
@dataclass
class TaskProposed(Event):
    EVENT_TYPE: ClassVar[str] = "task.proposed"


@dataclass
class TaskUpdated(Event):
    EVENT_TYPE: ClassVar[str] = "task.updated"


@dataclass
class TaskClaimed(Event):
    EVENT_TYPE: ClassVar[str] = "task.claimed"


@dataclass
class TaskResolved(Event):
    EVENT_TYPE: ClassVar[str] = "task.resolved"


@dataclass
class TaskBlocked(Event):
    EVENT_TYPE: ClassVar[str] = "task.blocked"


# Notifications
@dataclass
class NotificationNew(Event):
    EVENT_TYPE: ClassVar[str] = "notification.new"


@dataclass
class NotificationRead(Event):
    EVENT_TYPE: ClassVar[str] = "notification.read"


@dataclass
class NotificationCleared(Event):
    EVENT_TYPE: ClassVar[str] = "notification.cleared"


# Cycles — Mission Control time-boxed work windows.
# The daily-snapshot job (``ee.cloud.cycles.snapshot_job``) emits
# ``CycleSnapshotted`` after appending a new point to a cycle's daily
# series; the frontend's burnup chart subscribes and patches the active
# cycle without a full refetch.
@dataclass
class CycleCreated(Event):
    EVENT_TYPE: ClassVar[str] = "cycle.created"


@dataclass
class CycleUpdated(Event):
    EVENT_TYPE: ClassVar[str] = "cycle.updated"


@dataclass
class CycleClosed(Event):
    EVENT_TYPE: ClassVar[str] = "cycle.closed"


@dataclass
class CycleSnapshotted(Event):
    EVENT_TYPE: ClassVar[str] = "cycle.snapshotted"


# Projects — Linear-style scoping primitive for Mission Control.
# Pocket, Task, and Cycle entities carry an optional ``project_id``
# pointer; mutating a project doesn't cascade-delete its children, it
# just soft-unassigns them. Listeners (search index, dashboards) use
# these events to refresh project-grouped views.
@dataclass
class ProjectCreated(Event):
    EVENT_TYPE: ClassVar[str] = "project.created"


@dataclass
class ProjectUpdated(Event):
    EVENT_TYPE: ClassVar[str] = "project.updated"


@dataclass
class ProjectArchived(Event):
    EVENT_TYPE: ClassVar[str] = "project.archived"


@dataclass
class ProjectDeleted(Event):
    EVENT_TYPE: ClassVar[str] = "project.deleted"


# Planner — fires after ``ee.cloud.planner.service.agent_plan_project``
# finishes materializing the OSS PlannerResult into cloud primitives
# (PRD file, plan.json, goal.md, tasks, agent-gap list). Listeners use
# it to refresh the Mission Control Plan tab without polling.
@dataclass
class PlanGenerated(Event):
    EVENT_TYPE: ClassVar[str] = "plan.generated"


# Planner — fires after ``ee.cloud.planner.service.agent_resolve_gap``
# reassigns the human-fallback tasks for a previously-missing agent spec
# to a newly-created cloud Agent. Listeners refresh the Plan tab's
# agent-gap card (removing the resolved spec) and the Mission Control
# feed (rows whose assignee changed).
@dataclass
class PlanGapResolved(Event):
    EVENT_TYPE: ClassVar[str] = "plan.gap_resolved"


# Composio — per-user OAuth integrations (Gmail, Slack, GitHub, …)
# verified via per-toolkit identity probes. ``ComposioConnectionVerified``
# fires when a probe succeeds and the stored identity matches (or first
# time storing). ``ComposioConnectionMismatch`` fires when a fresh probe
# returns a different external identity than the stored one — surfaces
# to the chat as a "confirm this is what you intended" prompt rather
# than silently overwriting.
@dataclass
class ComposioConnectionVerified(Event):
    EVENT_TYPE: ClassVar[str] = "composio.connection.verified"


@dataclass
class ComposioConnectionMismatch(Event):
    EVENT_TYPE: ClassVar[str] = "composio.connection.mismatch"


# Instinct approval queue (RFC 03 v2 / Wave 3a). Fires on every
# ``instinct_approvals.service`` state-mutating call. Created on the
# initial gate-result persistence; approved / rejected on operator
# decision. ``data`` carries the approval id + workspace + pocket
# context so a downstream WS fan-out can target the right operator
# inbox without re-reading the doc.
@dataclass
class InstinctApprovalCreated(Event):
    EVENT_TYPE: ClassVar[str] = "instinct.approval.created"


@dataclass
class InstinctApprovalApproved(Event):
    EVENT_TYPE: ClassVar[str] = "instinct.approval.approved"


@dataclass
class InstinctApprovalRejected(Event):
    EVENT_TYPE: ClassVar[str] = "instinct.approval.rejected"


# Bulk action dispatch (RFC 03 v2 / Wave 3b). Emitted by
# ``pockets.service.dispatch_bulk_action`` after a fan-out call resolves —
# one event per dispatch, regardless of how many rows were processed.
# ``data`` carries: ``workspace_id``, ``pocket_id``, ``action_name``,
# ``total_rows``, ``executed`` (count of EXECUTE / NOTIFY_AND_EXECUTE
# rows fired), ``blocked`` (count of BLOCK rows), ``approval_needed``
# (count of rows that escalated into the batch approval), and an
# optional ``batch_approval_id`` (str | None) — set when ``approval_needed``
# is non-zero.
@dataclass
class BulkActionDispatched(Event):
    EVENT_TYPE: ClassVar[str] = "pocket.bulk_action.dispatched"


# Outcome event emission (RFC 03 v2 / Wave 3c). Fires AFTER a write
# action's ``run_action`` returns ``ok:true`` on the HTTP 2xx success
# path AND the action's ``outcomes_emitted[]`` declares one or more
# names. One event per declared name (bulk emits per row). Feeds the
# downstream Outcomes meter (out of scope for Wave 3c — bus emit is
# the seam).
#
# Payload (``data``):
#   event_name             — the declared outcome name (str), one of
#                            ``actions[].outcomes_emitted``.
#   workspace_id           — tenancy.
#   pocket_id              — the pocket the action ran on.
#   action_name            — the action that fired.
#   row_id                 — the stable row identifier (empty when the
#                            action does not bind to a row, e.g. a
#                            page-level action).
#   row_context_snapshot   — the row dict at emit time. Captures
#                            payload state for downstream audit.
#   emitted_at             — ISO-8601 UTC timestamp.
#   template_name          — pocket template name (provenance for the
#                            outcomes catalog).
#   template_version       — pocket template version.
#
# Distinct from ``PocketOutcomeEvent`` (M2b.2): the M2b.2 event is
# binding-driven (single ``binding.outcome`` field), this event is
# template-driven (per-action ``outcomes_emitted[]`` list — multiple
# emits per action allowed). Both coexist.
@dataclass
class OutcomeEmitted(Event):
    EVENT_TYPE: ClassVar[str] = "pocket.outcome_emitted"


# Temporal sweep completion event (RFC 03 v2 Wave 3d).
# Fired by ``temporal_sweeps.service.upsert_state`` once per (workspace,
# pocket) sweep, with the sweep's tally: edges_fired (how many rising-
# edge transitions dispatched), blocked (how many Instinct blocked),
# escalated (how many escalated to approval), errors (per-row eval
# failures), and the wall-clock duration. Downstream listeners (audit,
# dashboards) key off this single event instead of N per-row events,
# the same way ``BulkActionDispatched`` is one event per dispatch.
#
# Payload (carried under ``Event.data``):
#   workspace_id        — tenancy.
#   pocket_id           — pocket whose temporal triggers were swept.
#   edges_fired         — count of rising edges that dispatched.
#   blocked             — count of edges whose Instinct gate returned
#                         BLOCK (action_executor never called).
#   escalated           — count of edges whose Instinct gate returned
#                         ESCALATE_APPROVAL (one InstinctApproval row
#                         persisted per row).
#   errors              — count of per-row CEL eval failures.
#   sweep_duration_ms   — wall-clock ms the sweep ran for. Forensic /
#                         dashboard signal; long sweeps deserve a look.
@dataclass
class TemporalSweepCompleted(Event):
    EVENT_TYPE: ClassVar[str] = "pocket.temporal_sweep_completed"
