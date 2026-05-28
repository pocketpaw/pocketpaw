"""Cloud document models — re-exports for Beanie init.

Updated: 2026-05-21 (PR #1177 security pass) — dropped PocketBackendCredential
from ``__all__`` so it cannot be star-imported into routers/DTOs/domains; it
remains registered in ``get_all_documents()`` for Beanie init.
"""

from __future__ import annotations

from pocketpaw_ee.cloud.models.agent import Agent, AgentConfig
from pocketpaw_ee.cloud.models.api_key import APIKey
from pocketpaw_ee.cloud.models.audit_event import AuditEvent
from pocketpaw_ee.cloud.models.audit_webhook import AuditWebhook
from pocketpaw_ee.cloud.models.auth_session import AuthSession
from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc
from pocketpaw_ee.cloud.models.comment import Comment, CommentAuthor, CommentTarget
from pocketpaw_ee.cloud.models.composio_connection import ComposioConnection
from pocketpaw_ee.cloud.models.connector import WorkspaceConnector
from pocketpaw_ee.cloud.models.cycle import Cycle, CycleDailyPoint
from pocketpaw_ee.cloud.models.file import FileObj
from pocketpaw_ee.cloud.models.group import Group, GroupAgent
from pocketpaw_ee.cloud.models.instinct_approval import InstinctApproval
from pocketpaw_ee.cloud.models.invite import Invite
from pocketpaw_ee.cloud.models.message import Attachment, Mention, Message, Reaction
from pocketpaw_ee.cloud.models.notification import Notification, NotificationSource
from pocketpaw_ee.cloud.models.planner import PlanSession, PlanSessionAgentGap
from pocketpaw_ee.cloud.models.pocket import Pocket, Widget, WidgetPosition
from pocketpaw_ee.cloud.models.pocket_backend import PocketBackendCredential
from pocketpaw_ee.cloud.models.project import Project
from pocketpaw_ee.cloud.models.read_state import ReadState
from pocketpaw_ee.cloud.models.session import Session
from pocketpaw_ee.cloud.models.task import Task, TaskAssignee, TaskSource
from pocketpaw_ee.cloud.models.temporal_sweep_state import TemporalSweepStateDoc
from pocketpaw_ee.cloud.models.user import OAuthAccount, User, WorkspaceMembership
from pocketpaw_ee.cloud.models.workspace import Workspace, WorkspaceSettings

# Lazy import to avoid circular imports
FileUpload: type = None  # type: ignore[assignment]
FileFolder: type = None  # type: ignore[assignment]
_CalendarDoc: type = None  # type: ignore[assignment]
_EventDoc: type = None  # type: ignore[assignment]


def _ensure_file_upload():
    global FileUpload, FileFolder
    if FileUpload is None:
        from pocketpaw_ee.cloud.uploads.models import FileFolder as _FileFolder
        from pocketpaw_ee.cloud.uploads.models import FileUpload as _FileUpload

        FileUpload = _FileUpload
        FileFolder = _FileFolder
    return FileUpload


def _ensure_calendar_docs():
    # Why: calendar.__init__ eagerly imports the router which transitively
    # imports cloud.auth.current_active_user. Deferred to break the cycle
    # when cloud.models is loaded during cloud.auth's own init.
    global _CalendarDoc, _EventDoc
    if _CalendarDoc is None:
        from pocketpaw_ee.calendar.models import _CalendarDoc as _CD
        from pocketpaw_ee.calendar.models import _EventDoc as _ED

        _CalendarDoc = _CD
        _EventDoc = _ED
    return _CalendarDoc, _EventDoc


__all__ = [
    "APIKey",
    "Agent",
    "AgentConfig",
    "Attachment",
    "AuditEvent",
    "AuditWebhook",
    "AuthSession",
    "ChatRunDoc",
    "Comment",
    "CommentAuthor",
    "CommentTarget",
    "ComposioConnection",
    "Cycle",
    "CycleDailyPoint",
    "FileFolder",
    "FileObj",
    "FileUpload",
    "Group",
    "GroupAgent",
    "InstinctApproval",
    "Invite",
    "Mention",
    "Message",
    "Notification",
    "NotificationSource",
    "OAuthAccount",
    "PlanSession",
    "PlanSessionAgentGap",
    "Pocket",
    "Project",
    "Reaction",
    "ReadState",
    "Session",
    "Task",
    "TaskAssignee",
    "TaskSource",
    "TemporalSweepStateDoc",
    "User",
    "Widget",
    "WidgetPosition",
    "Workspace",
    "WorkspaceConnector",
    "WorkspaceMembership",
    "WorkspaceSettings",
]


def get_all_documents():
    """Get all Beanie documents, with lazy FileUpload loading."""
    _ensure_file_upload()
    cal_doc, evt_doc = _ensure_calendar_docs()
    return [
        User,
        Agent,
        Pocket,
        PocketBackendCredential,
        Session,
        Comment,
        Notification,
        FileObj,
        FileUpload,
        FileFolder,
        Workspace,
        WorkspaceConnector,
        ComposioConnection,
        Invite,
        Group,
        InstinctApproval,
        Message,
        ReadState,
        Task,
        TemporalSweepStateDoc,
        Cycle,
        Project,
        PlanSession,
        ChatRunDoc,
        AuditEvent,
        AuditWebhook,
        AuthSession,
        APIKey,
        cal_doc,
        evt_doc,
    ]


# For backward compat, expose as lazy-loading list
class _LazyAllDocuments(list):
    """Lazy-loads ALL_DOCUMENTS on first access."""

    def __init__(self):
        super().__init__()
        self._loaded = False

    def _ensure_loaded(self):
        if not self._loaded:
            docs = get_all_documents()
            self.clear()
            self.extend(docs)
            self._loaded = True

    def __getitem__(self, index):
        self._ensure_loaded()
        return super().__getitem__(index)

    def __iter__(self):
        self._ensure_loaded()
        return super().__iter__()

    def __len__(self):
        self._ensure_loaded()
        return super().__len__()

    def __contains__(self, item):
        self._ensure_loaded()
        return super().__contains__(item)


ALL_DOCUMENTS = _LazyAllDocuments()
