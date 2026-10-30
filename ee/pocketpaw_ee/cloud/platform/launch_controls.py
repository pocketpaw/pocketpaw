# ee/pocketpaw_ee/cloud/platform/launch_controls.py: the operator read and write
# of the early-access launch controls, ``/api/v1/platform/launch-controls``.
#
# GET is ``platform.settings.read``; PUT is ``platform.settings.write`` and is
# audited like every other platform write. The switches live in Mongo, not in
# ``Settings``, so this is deliberately not the ``/platform/settings`` route: a
# flip here needs no restart and reaches every process within the 30 s cache TTL
# of ``launch_controls.service``.

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud._core.platform_deps import require_platform
from pocketpaw_ee.cloud.launch_controls import service
from pocketpaw_ee.cloud.launch_controls.domain import LaunchControls
from pocketpaw_ee.cloud.launch_controls.dto import (
    LaunchControlsResponse,
    LaunchControlsUpdateRequest,
)
from pocketpaw_ee.cloud.models.user import User
from pocketpaw_ee.cloud.platform import audit

router = APIRouter(prefix="/launch-controls", tags=["platform"])


def _out(controls: LaunchControls) -> LaunchControlsResponse:
    return LaunchControlsResponse.model_validate(controls, from_attributes=True)


@router.get("", response_model=LaunchControlsResponse)
async def get_launch_controls(
    _operator: Annotated[User, Depends(require_platform("platform.settings.read"))],
) -> LaunchControlsResponse:
    """The stored switches, read fresh (not the per-process cached snapshot)."""
    service.invalidate_cache()
    return _out(await service.get_launch_controls())


@router.put("", response_model=LaunchControlsResponse)
async def update_launch_controls(
    body: LaunchControlsUpdateRequest,
    request: Request,
    operator: Annotated[User, Depends(require_platform("platform.settings.write"))],
) -> LaunchControlsResponse:
    """Change one or more switches. Other processes see it within 30 s."""
    reason = body.reason.strip()
    if not reason:
        raise ValidationError(
            "platform.launch_controls.reason_required", "A reason is required to change controls"
        )
    changes = body.model_dump(exclude_unset=True, exclude_none=True, exclude={"reason"})
    if not changes:
        raise ValidationError("platform.launch_controls.no_changes", "No changes were supplied")

    service.invalidate_cache()
    before = await service.get_launch_controls()
    event = await audit.begin(
        operator=operator,
        action="platform.launch_controls.write",
        reason=reason,
        target_type="launch_controls",
        before={name: getattr(before, name) for name in changes},
        request=request,
    )
    try:
        after = await service.update_launch_controls(changes, operator_id=str(operator.id))
    except Exception:
        await audit.settle(event, ok=False)
        raise
    await audit.settle(event, ok=True, after={name: getattr(after, name) for name in changes})
    return _out(after)


__all__ = ["router"]
