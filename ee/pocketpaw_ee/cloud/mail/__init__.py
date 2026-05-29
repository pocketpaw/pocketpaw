"""Central mail module — sends invite (and future transactional) emails via Mailtrap.

Usage::

    from pocketpaw_ee.cloud.mail import send_invite_email

    await send_invite_email(
        to_email="bob@example.com",
        workspace_name="Acme Corp",
        invite_token="abc123",
        inviter_name="Alice",
    )
"""

from __future__ import annotations

from pocketpaw_ee.cloud.mail.service import send_invite_email

__all__ = ["send_invite_email"]
