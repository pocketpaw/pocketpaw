# ee/pocketpaw_ee/cloud/studio/fal_errors.py — turn a fal failure into something
# a person can act on.
#
# Created 2026-09-05 (studio-surface-fal-errors).
#
# fal rejects a request with a structured body:
#
#   [{"loc": ["body", "image_urls"],
#     "msg":  "The images or videos provided may contain likenesses of real
#              people ...",
#     "type": "content_policy_violation",
#     "input": {"prompt": "...", "image_urls": ["data:image/jpeg;base64,/9j/4AAQ..."]}}]
#
# Every one of those failures used to reach the user as a 502 whose detail was
# `str(exception)` — the whole list above, base64 payload included. Nobody reads
# that, so in practice the user saw a generation fail with no reason at all while
# the actual reason sat in a server log behind a traceback.
#
# Two things this module fixes, and they are separate:
#
#   1. THE MESSAGE. Take `msg`, drop `input` (that is the base64), and add the
#      field it was raised against so "which of my three images" is answerable.
#
#   2. THE STATUS. A content-policy rejection is not an outage — the user can fix
#      it by changing the picture. Returning 502 tells them the opposite, and
#      tells our own monitoring that fal is down when it is working perfectly.
#      Requests fal REFUSED map to 4xx; requests fal FAILED map to 502.
#
# Deliberately dependency-free on fal_client: it is a lazy optional import in
# every other module here, and an error formatter that cannot be imported during
# an error is worthless.

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# fal error `type` values that mean "the caller sent something unacceptable".
# These are the user's to fix, so they must not be reported as an upstream
# outage. Anything not listed is treated as a genuine failure — erring toward
# 502 keeps a real fal incident visible instead of blaming the user for it.
_CLIENT_FAULT_TYPES: frozenset[str] = frozenset(
    {
        "content_policy_violation",
        "audio_duration_too_short",
        "audio_duration_too_long",
        "video_duration_too_short",
        "video_duration_too_long",
        "invalid_image",
        "invalid_audio",
        "invalid_video",
        "file_too_large",
        "too_many_files",
        "unsupported_format",
        "value_error",
        "missing",
    }
)

# Friendlier openings for the failures a studio user actually hits. fal's own
# `msg` still follows — this only adds the context fal cannot know, namely what
# the person was doing when it happened.
_PREFIX_BY_TYPE: dict[str, str] = {
    "content_policy_violation": "The provider rejected this content",
    "audio_duration_too_short": "That audio track is too short",
    "audio_duration_too_long": "That audio track is too long",
    "file_too_large": "That file is too large",
    "too_many_files": "Too many files attached",
    "unsupported_format": "Unsupported file format",
}

# Prefixes above that already name the input they are about. Appending the field
# label to these reads as a stutter — "That audio track is too short in the music
# track" — so the label is only added to prefixes that are generic about WHAT
# was rejected, like the policy one.
_SELF_DESCRIBING_TYPES: frozenset[str] = frozenset(
    {
        "audio_duration_too_short",
        "audio_duration_too_long",
        "video_duration_too_short",
        "video_duration_too_long",
        "file_too_large",
        "too_many_files",
        "unsupported_format",
    }
)

# Where a rejection landed → the words the user knows that input by. "body ->
# image_urls" means nothing to someone who dragged a node onto a canvas.
_FIELD_LABELS: dict[str, str] = {
    "image_urls": "the reference image",
    "image_url": "the reference image",
    "audio_urls": "the music track",
    "video_urls": "the reference video",
    "prompt": "the prompt",
    "duration": "the clip length",
    "aspect_ratio": "the aspect ratio",
    "resolution": "the resolution",
}


@dataclass(frozen=True)
class FalFailure:
    """A fal rejection, reduced to what is safe and useful to show.

    ``message`` never contains the request payload. ``client_fault`` decides
    whether this becomes a 4xx (the user can fix it) or a 502 (fal broke).
    """

    message: str
    code: str | None = None
    field: str | None = None
    client_fault: bool = False

    @property
    def log_line(self) -> str:
        """One line for the server log — no payload, no traceback needed.

        The point of this feature is that the base64 request body stops being
        written to logs, so this deliberately carries only the classification.
        """
        parts = [f"code={self.code or 'unknown'}"]
        if self.field:
            parts.append(f"field={self.field}")
        parts.append(f"msg={self.message}")
        return " ".join(parts)


def _records(exc: BaseException) -> list[dict[str, Any]]:
    """Pull fal's list-of-records out of an exception, however it is carried.

    The SDK puts the parsed body in ``args[0]`` when it could decode it and the
    raw text when it could not, so both shapes are handled rather than assumed.
    """
    for candidate in (getattr(exc, "args", None) or [None])[:1]:
        if isinstance(candidate, list):
            return [r for r in candidate if isinstance(r, dict)]
        if isinstance(candidate, dict):
            return [candidate]
        if isinstance(candidate, str):
            try:
                parsed = json.loads(candidate)
            except (ValueError, TypeError):
                return []
            if isinstance(parsed, list):
                return [r for r in parsed if isinstance(r, dict)]
            if isinstance(parsed, dict):
                # FastAPI-style {"detail": [...]} bodies.
                detail = parsed.get("detail")
                if isinstance(detail, list):
                    return [r for r in detail if isinstance(r, dict)]
                return [parsed]
    return []


def _field_from_loc(loc: Any) -> str | None:
    """The meaningful field name out of a ``loc`` path like ``["body",
    "audio_urls", 0]`` — the last string that is not the "body" wrapper."""
    if not isinstance(loc, (list, tuple)):
        return None
    for part in reversed(loc):
        if isinstance(part, str) and part != "body":
            return part
    return None


def parse_fal_error(exc: BaseException, *, action: str = "generation") -> FalFailure:
    """Reduce a fal exception to a showable message + a fault classification.

    ``action`` names what the user was doing ("video generation") so the message
    reads as a sentence rather than as a bare provider complaint.

    Never raises: this runs inside an exception handler, and a formatter that
    throws would replace a useful error with a confusing one.
    """
    try:
        records = _records(exc)
        if not records:
            # Nothing structured to read — fall back to the exception text, but
            # cap it. An un-parsed fal body can be an entire base64 payload, and
            # the whole point is that such a thing never reaches a user or a log.
            text = str(exc).strip() or "no reason given"
            if len(text) > 300:
                text = text[:300].rstrip() + "…"
            return FalFailure(message=f"{action.capitalize()} failed: {text}")

        first = records[0]
        code = first.get("type") if isinstance(first.get("type"), str) else None
        raw_msg = first.get("msg")
        msg = raw_msg.strip() if isinstance(raw_msg, str) and raw_msg.strip() else ""
        field = _field_from_loc(first.get("loc"))
        label = _FIELD_LABELS.get(field or "")

        prefix = _PREFIX_BY_TYPE.get(code or "")
        if prefix and label and code not in _SELF_DESCRIBING_TYPES:
            head = f"{prefix} in {label}."
        elif prefix:
            head = f"{prefix}."
        elif label:
            head = f"{action.capitalize()} was rejected because of {label}."
        else:
            head = f"{action.capitalize()} was rejected."

        message = f"{head} {msg}".strip() if msg else head
        if len(records) > 1:
            message = f"{message} (+{len(records) - 1} more)"

        return FalFailure(
            message=message,
            code=code,
            field=field,
            client_fault=bool(code and code in _CLIENT_FAULT_TYPES),
        )
    except Exception:  # noqa: BLE001 — an error formatter must never itself fail
        return FalFailure(message=f"{action.capitalize()} failed.")


__all__ = ["FalFailure", "parse_fal_error"]
