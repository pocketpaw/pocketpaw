# Meetings — deployment-global transcription settings.
#
# transcript_provider selects the Recall.ai transcription path:
#   * `meeting_captions` or a `*_streaming` provider — realtime: the
#     transcript is configured on the bot at creation and runs live.
#   * a `*_async` provider — the bot records only; transcription runs
#     post-call via POST /recording/{id}/create_transcript/.
# transcript_model is the provider's model name (e.g. Deepgram nova-3) —
# used only by async providers that accept one.
#
# A stored MeetingsSettings row wins; the RECALL_TRANSCRIPT_PROVIDER /
# RECALL_TRANSCRIPT_MODEL environment variables are the fallback.

from __future__ import annotations

import logging
import os

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.meetings.dto import (
    MeetingsSettingsResponse,
    UpdateMeetingsSettingsRequest,
)
from pocketpaw_ee.cloud.models.meeting import MeetingsSettings as _SettingsDoc

logger = logging.getLogger(__name__)

_DEFAULT_PROVIDER = "meeting_captions"


def is_async_provider(provider: str) -> bool:
    """True when the provider transcribes post-call (Recall ``*_async`` keys)."""
    return provider.endswith("_async")


def _is_valid_provider(provider: str) -> bool:
    """Structural check — accepts ``meeting_captions`` + any ``*_streaming`` / ``*_async``."""
    return (
        provider == "meeting_captions"
        or provider.endswith("_streaming")
        or provider.endswith("_async")
    )


def _mode(provider: str) -> str:
    return "async" if is_async_provider(provider) else "realtime"


async def resolve() -> dict[str, str]:
    """Return the effective transcription config — stored row, else env.

    Shape: ``{"provider": ..., "model": ...}``. Used by recall_client to
    pick the bot's transcription path.
    """
    # global-config: deployment-wide singleton, not tenant-scoped.
    doc = await _SettingsDoc.find_all().first_or_none()
    if doc is not None:
        return {"provider": doc.transcript_provider, "model": doc.transcript_model}
    return {
        "provider": os.environ.get("RECALL_TRANSCRIPT_PROVIDER", "").strip() or _DEFAULT_PROVIDER,
        "model": os.environ.get("RECALL_TRANSCRIPT_MODEL", "").strip(),
    }


async def get_settings() -> MeetingsSettingsResponse:
    """Current transcription settings (provider + model + derived mode)."""
    resolved = await resolve()
    return MeetingsSettingsResponse(
        transcript_provider=resolved["provider"],
        transcript_model=resolved["model"],
        mode=_mode(resolved["provider"]),  # type: ignore[arg-type]
    )


async def update_settings(body: UpdateMeetingsSettingsRequest) -> MeetingsSettingsResponse:
    """Set the transcription provider + model. Upserts the singleton row."""
    body = UpdateMeetingsSettingsRequest.model_validate(body)
    provider = body.transcript_provider.strip()
    if not _is_valid_provider(provider):
        raise ValidationError(
            "meetings.invalid_transcript_provider",
            f"'{provider}' is not a recognised transcription provider — expected "
            "meeting_captions or a *_streaming / *_async provider key.",
        )
    model = body.transcript_model.strip()

    doc = await _SettingsDoc.find_all().first_or_none()
    if doc is None:
        doc = _SettingsDoc(transcript_provider=provider, transcript_model=model)
        await doc.insert()
    else:
        doc.transcript_provider = provider
        doc.transcript_model = model
        await doc.save()

    # no-event: deployment-global config, no downstream consumers.
    logger.info("Meetings transcription set to provider=%s model=%s", provider, model)
    return MeetingsSettingsResponse(
        transcript_provider=provider,
        transcript_model=model,
        mode=_mode(provider),  # type: ignore[arg-type]
    )
