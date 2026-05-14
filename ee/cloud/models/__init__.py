"""Cloud document models — re-exports for Beanie init."""

from __future__ import annotations

from ee.cloud.models.agent import Agent, AgentConfig
from ee.cloud.models.comment import Comment, CommentAuthor, CommentTarget
from ee.cloud.models.connector import WorkspaceConnector
from ee.cloud.models.cycle import Cycle, CycleDailyPoint
from ee.cloud.models.file import FileObj
from ee.cloud.models.group import Group, GroupAgent
from ee.cloud.models.invite import Invite
from ee.cloud.models.message import Attachment, Mention, Message, Reaction
from ee.cloud.models.notification import Notification, NotificationSource
from ee.cloud.models.pocket import Pocket, Widget, WidgetPosition
from ee.cloud.models.read_state import ReadState
from ee.cloud.models.session import Session
from ee.cloud.models.task import Task, TaskAssignee, TaskSource
from ee.cloud.models.user import OAuthAccount, User, WorkspaceMembership
from ee.cloud.models.workspace import Workspace, WorkspaceSettings

# Lazy import to avoid circular imports
FileUpload: type = None  # type: ignore[assignment]
FileFolder: type = None  # type: ignore[assignment]


def _ensure_file_upload():
    global FileUpload, FileFolder
    if FileUpload is None:
        from ee.cloud.uploads.models import FileFolder as _FileFolder
        from ee.cloud.uploads.models import FileUpload as _FileUpload

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
    "Pocket",
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
        Session,
        Comment,
        Notification,
        FileObj,
        FileUpload,
        FileFolder,
        Workspace,
        WorkspaceConnector,
        Invite,
        Group,
        Message,
        ReadState,
        Task,
        Cycle,
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
