from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import ClassVar


@dataclass
class Event:
    type: str = ""
    data: dict = field(default_factory=dict)
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        cls_type = getattr(type(self), "EVENT_TYPE", "")
        if cls_type:
            self.type = cls_type


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
