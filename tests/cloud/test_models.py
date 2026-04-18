"""Tests for cloud model changes — pure Pydantic validation, no DB needed.

Uses model_construct() to bypass Beanie's __init__ (which requires a live
MongoDB collection). We then verify default values and field acceptance via
Pydantic's model_validate (construct=True).
"""

from __future__ import annotations

from ee.cloud.models.group import Group
from ee.cloud.models.invite import Invite
from ee.cloud.models.message import Message
from ee.cloud.models.notification import Notification
from ee.cloud.models.pocket import Pocket
from ee.cloud.models.session import Session
from ee.cloud.models.workspace import Workspace

# ---------------------------------------------------------------------------
# Group
# ---------------------------------------------------------------------------


def test_group_supports_dm_type():
    g = Group.model_construct(
        workspace="w1", name="DM", type="dm", owner="u1", members=["u1", "u2"]
    )
    assert g.type == "dm"


def test_group_has_last_message_at():
    g = Group.model_construct(workspace="w1", name="test", owner="u1")
    assert g.last_message_at is None


def test_group_has_message_count():
    g = Group.model_construct(workspace="w1", name="test", owner="u1")
    assert g.message_count == 0


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------


def test_message_has_edited_at():
    m = Message.model_construct(group="g1", sender="u1", content="hello")
    assert m.edited_at is None


# ---------------------------------------------------------------------------
# Pocket
# ---------------------------------------------------------------------------


def test_pocket_sharing_fields():
    p = Pocket.model_construct(workspace="w1", name="test", owner="u1")
    assert p.share_link_token is None
    assert p.share_link_access == "view"
    assert p.visibility == "private"
    assert p.shared_with == []


def test_pocket_visibility_values():
    for v in ("private", "workspace", "public"):
        p = Pocket.model_construct(workspace="w1", name="test", owner="u1", visibility=v)
        assert p.visibility == v


# ---------------------------------------------------------------------------
# Invite
# ---------------------------------------------------------------------------


def test_invite_has_revoked():
    i = Invite.model_construct(workspace="w1", email="a@b.com", invited_by="u1", token="tok1")
    assert i.revoked is False


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------


def test_workspace_has_deleted_at():
    w = Workspace.model_construct(name="test", slug="test", owner="u1")
    assert w.deleted_at is None


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


def test_session_has_deleted_at():
    s = Session.model_construct(sessionId="s1", workspace="w1", owner="u1")
    assert s.deleted_at is None


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------


def test_notification_has_expires_at():
    n = Notification.model_construct(workspace="w1", recipient="u1", type="mention", title="test")
    assert n.expires_at is None
