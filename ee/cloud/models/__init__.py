"""Cloud document models — re-exports for Beanie init."""

from __future__ import annotations

from ee.cloud.models.agent import Agent, AgentConfig
from ee.cloud.models.comment import Comment, CommentAuthor, CommentTarget
from ee.cloud.models.file import FileObj
from ee.cloud.models.group import Group, GroupAgent
from ee.cloud.models.invite import Invite
from ee.cloud.models.message import Attachment, Mention, Message, Reaction
from ee.cloud.models.notification import Notification, NotificationSource
from ee.cloud.models.pocket import Pocket, Widget, WidgetPosition
from ee.cloud.models.session import Session
from ee.cloud.models.user import OAuthAccount, User, WorkspaceMembership
from ee.cloud.models.workspace import Workspace, WorkspaceSettings

__all__ = [
    "Agent",
    "AgentConfig",
    "Attachment",
    "Comment",
    "CommentAuthor",
    "CommentTarget",
    "FileObj",
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
    "Session",
    "User",
    "Widget",
    "WidgetPosition",
    "Workspace",
    "WorkspaceMembership",
    "WorkspaceSettings",
]

ALL_DOCUMENTS = [
    User,
    Agent,
    Pocket,
    Session,
    Comment,
    Notification,
    FileObj,
    Workspace,
    Invite,
    Group,
    Message,
]
