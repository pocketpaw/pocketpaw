"""Central mail module — sends transactional emails via Mailtrap.

Usage::

    from pocketpaw_ee.cloud.mail import (
        send_feedback_email,
        send_invite_email,
        send_meeting_scheduled_email,
    )

    await send_invite_email(
        to_email="bob@example.com",
        workspace_name="Acme Corp",
        invite_token="abc123",
        inviter_name="Alice",
    )

    await send_meeting_scheduled_email(
        to_email="alice@example.com",
        to_name="Alice",
        title="Sprint sync",
        scheduled_start="2026-06-10T15:00:00Z",
        join_url="https://zoom.us/j/123456789",
        provider="Zoom",
        creator_name="Alice",
    )

    await send_feedback_email(
        user_email="alice@example.com",
        user_name="Alice",
        subject="Feature request",
        message="I would love to see...",
        workspace_name="Acme Corp",
    )
"""

from __future__ import annotations

from pocketpaw_ee.cloud.mail.service import (
    send_feedback_email,
    send_invite_email,
    send_meeting_scheduled_email,
)

__all__ = ["send_feedback_email", "send_invite_email", "send_meeting_scheduled_email"]
