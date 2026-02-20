# Identity router — get/put agent identity files.
# Created: 2026-02-20
#
# Extracted from dashboard.py transparency endpoints.

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from pocketpaw.api.deps import require_scope
from pocketpaw.api.v1.schemas.identity import (
    IdentityResponse,
    IdentitySaveRequest,
    IdentitySaveResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Identity"], dependencies=[Depends(require_scope("admin"))])


@router.get("/identity", response_model=IdentityResponse)
async def get_identity():
    """Get agent identity context (all 5 identity files)."""
    from pocketpaw.bootstrap import DefaultBootstrapProvider
    from pocketpaw.config import get_config_path

    provider = DefaultBootstrapProvider(get_config_path().parent)
    context = await provider.get_context()
    return IdentityResponse(
        identity_file=context.identity,
        soul_file=context.soul,
        style_file=context.style,
        instructions_file=context.instructions,
        user_file=context.user_profile,
    )


@router.put("/identity", response_model=IdentitySaveResponse)
async def save_identity(body: IdentitySaveRequest):
    """Save edits to agent identity files. Changes take effect on the next message."""
    from pocketpaw.config import get_config_path

    identity_dir = get_config_path().parent / "identity"
    identity_dir.mkdir(parents=True, exist_ok=True)

    file_map = {
        "identity_file": "IDENTITY.md",
        "soul_file": "SOUL.md",
        "style_file": "STYLE.md",
        "instructions_file": "INSTRUCTIONS.md",
        "user_file": "USER.md",
    }
    updated = []
    data = body.model_dump(exclude_none=True)
    for key, filename in file_map.items():
        if key in data and isinstance(data[key], str):
            (identity_dir / filename).write_text(data[key])
            updated.append(filename)

    return IdentitySaveResponse(updated=updated)
