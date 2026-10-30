"""LiveKit recording — composite egress lifecycle for the LiveKit provider.

Wraps the existing ``livekit.service.start_room_recording`` and
``stop_room_recording`` so the provider in ``provider.py`` calls through
this module instead of reaching directly into ``livekit.service``.

This is a thin delegation layer — the actual LiveKit SDK calls, S3 config,
and in-memory egress tracking live in ``livekit/service.py``. This module
exists to colocate the recording entry points with the provider and give
the webhook handler (``webhooks.py``, when it lands) a clean import target.
"""

from __future__ import annotations

import logging
from typing import Any

from pocketpaw_ee.cloud.livekit import service as livekit_service

logger = logging.getLogger(__name__)


async def start_composite_egress(group_id: str) -> dict[str, Any]:
    """Start a composite room recording via LiveKit Egress.

    Delegates to ``livekit.service.start_room_recording`` which handles
    the LiveKit API call, S3 output config, and in-memory egress tracking.

    Returns the egress metadata dict (egress_id, room_name, group_id,
    output_path, status, started_at).

    Raises ``RuntimeError`` if a recording is already active for this group.
    """
    return await livekit_service.start_room_recording(group_id)


async def stop_egress(group_id: str) -> dict[str, Any]:
    """Stop an active egress for a group.

    Delegates to ``livekit.service.stop_room_recording`` which stops the
    egress via the LiveKit API and returns final output file info.

    Raises ``RuntimeError`` if no recording is active for this group.
    """
    return await livekit_service.stop_room_recording(group_id)


__all__ = [
    "start_composite_egress",
    "stop_egress",
]
