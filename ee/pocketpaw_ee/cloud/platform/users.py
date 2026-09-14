"""Cross-tenant user lookup.

Created: 2026-09-14 (feat/platform-tenant-directory) — chunk 2 of the Paw Admin PRD.

Answers the support question that arrives without a workspace attached: someone
emails in, and the operator needs to know which accounts that address belongs to
before they can look at anything else. Every tenant-scoped route in the codebase
needs a workspace first, so this question is currently unanswerable outside a
Mongo shell.

Read-only, at the SUPPORT rung.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from pocketpaw_ee.cloud._core.platform_deps import require_platform
from pocketpaw_ee.cloud.models.user import User
from pocketpaw_ee.cloud.platform import audit
from pocketpaw_ee.cloud.workspace import service as workspace_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["platform"])


class MembershipOut(BaseModel):
    workspace_id: str
    role: str


class UserRowOut(BaseModel):
    user_id: str
    email: str
    name: str
    memberships: list[MembershipOut]


@router.get("", response_model=list[UserRowOut])
async def find_users(
    request: Request,
    operator: Annotated[User, Depends(require_platform("platform.user.read"))],
    email: Annotated[str, Query(min_length=3, description="Full or partial email address")],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> list[UserRowOut]:
    """Find users by email, with the workspaces each one belongs to.

    ``email`` has a minimum length because a one-character substring matches
    most of the table and returns a page of noise — that is a footgun, not a
    search. It is a substring match rather than exact: support requests arrive
    with the wrong case, a plus-tag, or a typo, and an exact-match-only lookup
    sends the operator back to the shell this route exists to replace.

    Returns no credential material, no MFA state and no session data. It is a
    directory lookup, not an account dump: an operator who needs more should be
    looking at a specific workspace, where the action is audited.
    """
    rows = await workspace_service.platform_find_users(email=email, limit=limit)

    # The email fragment IS the sensitive part of this request — searching for
    # "ceo@competitor" is the action worth being able to review later, and it
    # appears in no other log.
    await audit.record_read(
        operator=operator,
        action="platform.user.read",
        query=f"email={email!r}",
        target_type="user_search",
        request=request,
    )

    return [
        UserRowOut(
            user_id=user_id,
            email=user_email,
            name=full_name,
            memberships=[
                MembershipOut(workspace_id=workspace_id, role=role)
                for workspace_id, role in memberships
            ],
        )
        for user_id, user_email, full_name, memberships in rows
    ]
