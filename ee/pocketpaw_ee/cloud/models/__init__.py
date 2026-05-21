"""Cloud document models — re-exports for Beanie init.

Updated: 2026-05-21 (PR #1177 security pass) — dropped PocketBackendCredential
from ``__all__`` so it cannot be star-imported into routers/DTOs/domains; it
remains registered in ``get_all_documents()`` for Beanie init.
"""

from __future__ import annotations

from pocketpaw_ee.cloud.models.agent import Agent, AgentConfig
from pocketpaw_ee.cloud.models.comment import Comment, CommentAuthor, CommentTarget
from pocketpaw_ee.cloud.models.composio_connection import ComposioConnection
from pocketpaw_ee.cloud.models.connector import WorkspaceConnector
from pocketpaw_ee.cloud.models.cycle import Cycle, CycleDailyPoint
from pocketpaw_ee.cloud.models.file import FileObj
from pocketpaw_ee.cloud.models.group import Group, GroupAgent
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
from pocketpaw_ee.cloud.models.user import OAuthAccount, User, WorkspaceMembership
from pocketpaw_ee.cloud.models.workspace import Workspace, WorkspaceSettings

# Lazy import to avoid circular imports
FileUpload: type = None  # type: ignore[assignment]
FileFolder: type = None  # type: ignore[assignment]


def _ensure_file_upload():
    global FileUpload, FileFolder
    if FileUpload is None:
        from pocketpaw_ee.cloud.uploads.models import FileFolder as _FileFolder
        from pocketpaw_ee.cloud.uploads.models import FileUpload as _FileUpload

        FileUpload = _FileUpload
        FileFolder = _FileFolder
    return FileUpload


__all__ = [
    "Agent",
    "AgentConfig",
    "Attachment",
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
        Message,
        ReadState,
        Task,
        Cycle,
        Project,
        PlanSession,
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
