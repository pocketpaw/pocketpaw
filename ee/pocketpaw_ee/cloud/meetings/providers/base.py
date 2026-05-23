"""MeetingProvider protocol + registry.

A ``MeetingProvider`` is the source-specific implementation behind the
unified meetings module. ``service.py`` looks up the provider for a
meeting's ``source`` and dispatches lifecycle calls to it. Recall and
LiveKit each ship one.

Capabilities — recording, post-call transcript fetch — are optional
sub-protocols. Providers declare which they support via duck typing
(``isinstance(provider, SupportsRecording)`` at the dispatch site). New
capabilities can land without forcing every provider to grow stub
methods.

The registry is a module-level dict; providers register themselves at
import time in ``providers/<source>/__init__.py``. Phase 1 ships with no
providers registered — ``service.create_meeting`` raises
``ProviderNotRegistered`` until a provider lands.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from pocketpaw_ee.cloud._core.errors import CloudError

Source = Literal["recall", "livekit"]


# ---------------------------------------------------------------------------
# Provider results — shared shapes returned by every implementation.
# ---------------------------------------------------------------------------


class ProviderCreateResult(BaseModel):
    """What ``MeetingProvider.create`` returns to the service layer.

    ``provider_payload`` is stashed verbatim on ``MeetingDoc.provider_payload``
    — the provider owns its schema. ``join_url`` lands on the meeting row;
    None is legal when the URL doesn't exist until ``start``.
    """

    provider_payload: dict[str, Any] = Field(default_factory=dict)
    join_url: str | None = None


class ProviderStartResult(BaseModel):
    """What ``MeetingProvider.start`` returns.

    ``provider_payload_updates`` is merged onto the existing
    ``provider_payload`` (e.g. to add ``started_at``, an active egress id,
    the agent's PID). ``join_url`` may change from create-time (e.g. a
    LiveKit join token expires; start mints a fresh one) — None means
    keep the existing one.
    """

    provider_payload_updates: dict[str, Any] = Field(default_factory=dict)
    join_url: str | None = None


class RecordingRef(BaseModel):
    """Pointer to a recording artefact produced for a meeting.

    ``file_id`` is None while the artefact is still being rendered/uploaded;
    a webhook populates it later. ``external_id`` is the provider's own
    handle (Recall recording id, LiveKit egress id) — used by webhook
    handlers to correlate back to the meeting.
    """

    provider: str  # "recall" | "livekit"
    external_id: str
    status: Literal["recording", "rendering", "ready", "failed"]
    started_at: datetime
    file_id: str | None = None


class TranscriptArtefact(BaseModel):
    """A completed transcript — what ``SupportsTranscript.fetch_transcript``
    returns. ``vtt`` is the WebVTT text. ``language`` is the detected
    language code (or "multi" for code-switched recordings).
    """

    vtt: str
    entry_count: int
    speaker_count: int
    language: str | None = None


# ---------------------------------------------------------------------------
# Forward references — typed at the call site to avoid import cycles.
# ---------------------------------------------------------------------------

# ``Meeting`` and ``CreateMeetingRequest`` live in domain.py / dto.py,
# both of which we don't import here so the protocol module stays
# dependency-free.
_Meeting = Any  # ee.cloud.meetings.domain.Meeting
_CreateMeetingRequest = Any  # ee.cloud.meetings.dto.CreateMeetingRequest
_RequestContext = Any  # ee.cloud.shared.deps.RequestContext (or the (ws,user) tuple)


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class MeetingProvider(Protocol):
    """Source-specific implementation behind the unified meetings module.

    Lifecycle: ``create`` → (scheduled) → ``start`` → ``end``. ``cancel``
    can fire any time before ``end``. All methods are idempotent — the
    reminder loop, manual user actions, and webhook retries may invoke
    them more than once.
    """

    name: str  # "recall" | "livekit"

    async def create(
        self, ctx: _RequestContext, body: _CreateMeetingRequest
    ) -> ProviderCreateResult: ...

    async def start(self, ctx: _RequestContext, meeting: _Meeting) -> ProviderStartResult: ...

    async def cancel(self, ctx: _RequestContext, meeting: _Meeting) -> None: ...

    async def end(self, ctx: _RequestContext, meeting: _Meeting) -> None: ...


@runtime_checkable
class SupportsRecording(Protocol):
    """Optional capability — provider can produce a recording artefact."""

    async def request_recording(self, ctx: _RequestContext, meeting: _Meeting) -> RecordingRef: ...

    async def stop_recording(self, ctx: _RequestContext, meeting: _Meeting) -> None: ...


@runtime_checkable
class SupportsTranscript(Protocol):
    """Optional capability — provider can fetch a post-call transcript.

    Returns ``None`` when no transcript is ready yet. Idempotent —
    callers may poll.
    """

    async def fetch_transcript(
        self, ctx: _RequestContext, meeting: _Meeting
    ) -> TranscriptArtefact | None: ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ProviderNotRegistered(CloudError):
    """No provider is registered for the requested source.

    Raised by ``resolve`` when ``MeetingDoc.source`` points at a provider
    that hasn't been registered (e.g. a deployment that disabled LiveKit
    but received a meeting created when it was enabled).
    """

    def __init__(self, source: str) -> None:
        super().__init__(
            503,
            "meeting.provider_not_registered",
            f"No meeting provider registered for source='{source}'. "
            "Ensure the corresponding ee.cloud.meetings.providers.<source> "
            "package is imported at startup.",
        )


_REGISTRY: dict[str, MeetingProvider] = {}


def register(provider: MeetingProvider) -> None:
    """Register a provider implementation. Idempotent (same name = replace).

    Called from ``providers/<source>/__init__.py`` at import time. Mount
    the package from ``mount_cloud()`` so registration runs at startup.
    """
    _REGISTRY[provider.name] = provider


def resolve(source: str) -> MeetingProvider:
    """Look up the provider for a source. Raises ``ProviderNotRegistered``
    if no provider is registered for that source."""
    try:
        return _REGISTRY[source]
    except KeyError as exc:
        raise ProviderNotRegistered(source) from exc


def registered_sources() -> list[str]:
    """List sources with a registered provider. Used by startup health
    checks + the ``/meetings`` capability response."""
    return sorted(_REGISTRY)


def _clear_registry_for_tests() -> None:
    """Reset the registry — tests only. Production code never calls this."""
    _REGISTRY.clear()


__all__ = [
    "MeetingProvider",
    "ProviderCreateResult",
    "ProviderNotRegistered",
    "ProviderStartResult",
    "RecordingRef",
    "Source",
    "SupportsRecording",
    "SupportsTranscript",
    "TranscriptArtefact",
    "_clear_registry_for_tests",
    "register",
    "registered_sources",
    "resolve",
]
