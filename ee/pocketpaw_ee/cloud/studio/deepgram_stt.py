# ee/pocketpaw_ee/cloud/studio/deepgram_stt.py — direct Deepgram STT client.
#
# The /studio editor needs "put a video on the timeline, get its transcript".
# The LiteLLM gateway has no speech-recognition route usable by this surface
# (its ``audio_transcription`` modality is wired only to the chat MCP tool,
# which takes a LOCAL FILE PATH and returns bare text with no timings), so
# transcription runs DIRECTLY against Deepgram here — the same decision as the
# canvas edit ops (``fal_edit``), video (``fal_video``), the Kling panels
# (``fal_elements`` / ``fal_motion``) and music (``fal_music``).
#
# Deepgram's Prerecorded API is SYNCHRONOUS:
#     POST https://api.deepgram.com/v1/listen   (raw audio bytes as the body)
#       -> {"metadata": {"request_id": ..., "duration": ...},
#           "results": {"channels": [{"alternatives": [
#                        {"transcript": "...", "words": [...]}]}]}}
# One request, one body, results inline. Asynchronous operation exists but is
# opt-in via a ``callback`` URL, which needs a publicly reachable endpoint we do
# not have on this surface — and it is fire-and-callback, NOT submit-and-poll.
#
# Word timings arrive by default on ``alternatives[0].words[]``, each with
# ``start``/``end`` in SECONDS (floats), ``word``, ``punctuated_word`` and
# ``confidence``. The editor's CaptionWord wants MILLISECONDS, so the conversion
# happens HERE, at the provider boundary — neither the service nor the frontend
# ever sees seconds.
#
# Like the other provider modules, argument building and response extraction are
# PURE functions (``_build_query``, ``_extract_transcript``) so they unit-test
# without network, and ``_listen`` is the single seam tests monkeypatch.
#
# Created 2026-09-02 (studio-transcribe): direct Deepgram speech-to-text.
# Updated 2026-09-03 (studio-transcribe-502): rewritten against the REAL wire
#   contract. The first cut targeted an invented async submit+poll protocol
#   (``async=true``, ``GET /v1/requests/{id}``, ``results.metadata.status``,
#   ``results.channel_detections``), none of which exist. Deepgram ignores
#   unknown query params, so the submit returned a finished transcript, the code
#   looked for a root ``request_id`` to poll, found none, and raised — surfacing
#   as a 502 on every single call. Also: read ``punctuated_word`` for captions,
#   drop the invented ``utterance_split``/``alternatives`` params, and actually
#   honour POCKETPAW_DEEPGRAM_STT_MODEL, which was configured but never read.

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEEPGRAM_BASE_URL = "https://api.deepgram.com/v1"

#: Fallback recognition model when neither the caller nor Settings names one.
#: nova-3 is Deepgram's current general-English model.
DEFAULT_MODEL = "nova-3"

#: The call blocks for the whole transcription, so this bounds a long upload
#: rather than a network hiccup. Deepgram transcribes far faster than realtime
#: (a 4s clip returns in well under a second), but a 100MB podcast is minutes of
#: audio and the connection must outlive it.
REQUEST_TIMEOUT_SECONDS = 300.0

#: Rejected with a clear message rather than a 413 fight with whatever body
#: limit sits in front of us. Deepgram itself accepts much more; this is a
#: product limit for a short-form editor.
MAX_AUDIO_BYTES = 100 * 1024 * 1024


class DeepgramError(Exception):
    """A Deepgram transcription attempt failed (missing key, non-2xx, or a
    malformed result). Raised so the studio service can wrap it into
    ``StudioUpstreamError`` (router →502) and keep the router free of provider
    detail."""


def _settings_key() -> str | None:
    """The namespaced key from Settings, or None if it is not configured.

    A separate function because ``get_settings()`` is an lru_cache singleton:
    reading it inline makes the precedence rules untestable without clearing a
    global cache that other tests depend on. This is the seam they patch.
    """
    from pocketpaw.config import get_settings

    return get_settings().deepgram_api_key


def _settings_model() -> str | None:
    """The configured STT model from Settings. Seam, for the same reason as
    ``_settings_key``."""
    from pocketpaw.config import get_settings

    return get_settings().deepgram_stt_model


def resolve_api_key() -> str | None:
    """The Deepgram key, preferring the namespaced setting.

    Read from ``POCKETPAW_DEEPGRAM_API_KEY`` via Settings, then fall back to a
    bare ``DEEPGRAM_API_KEY``. The fallback exists because that unprefixed name
    is what already ships in ``.env`` (the livekit agent reads it directly);
    requiring every deployment to duplicate the secret under a second name would
    be a pure footgun.

    Uses the ``get_settings()`` accessor rather than a module-level singleton —
    ``config.py`` exposes no ``settings`` object, and importing a name that does
    not exist raises at call time. Last-resorts to the environment only on an
    import failure, never on a missing key: conflating the two is how a
    configured deployment silently reads the wrong credential.
    """
    try:
        configured = _settings_key()
    except ImportError:  # pragma: no cover — pocketpaw always ships alongside ee
        logger.warning("deepgram: pocketpaw.config unavailable, using DEEPGRAM_API_KEY only")
        configured = None

    if configured and str(configured).strip():
        return str(configured).strip()

    from_env = os.environ.get("DEEPGRAM_API_KEY") or ""
    return from_env.strip() or None


def resolve_model(explicit: str | None) -> str:
    """Pick the recognition model: caller > Settings > ``DEFAULT_MODEL``.

    The per-request override wins because the editor exposes a model picker;
    Settings is the deployment default (``POCKETPAW_DEEPGRAM_STT_MODEL``). That
    setting existed but nothing read it, so a deployment that configured nova-2
    silently kept getting nova-3.
    """
    if explicit and explicit.strip():
        return explicit.strip()

    try:
        configured = _settings_model()
    except ImportError:  # pragma: no cover
        configured = None

    if configured and str(configured).strip():
        return str(configured).strip()

    return DEFAULT_MODEL


def _headers(api_key: str) -> dict[str, str]:
    """Deepgram authenticates with a token scheme, not a bearer scheme."""
    return {"Authorization": f"Token {api_key}"}


def _build_query(*, model: str, language: str | None) -> dict[str, str]:
    """Query params for the listen call.

    Every key here is a parameter Deepgram actually accepts. That is not a
    truism: Deepgram SILENTLY IGNORES unknown query params, so an invented one
    never errors — it just quietly does nothing while the code reads as though
    it works. The first cut of this module shipped three of them
    (``utterance_split=punctuation``, ``alternatives=1``, ``async=true``).

    ``smart_format`` and ``punctuate`` are what populate ``punctuated_word``,
    which is the caption-ready token. Word timings need no flag — they come by
    default. ``diarize`` is left off deliberately: speaker labels have nowhere to
    go in the editor's caption model yet, and enabling it changes the response
    shape we would have to carry.

    Utterance segmentation would be ``utterances=true`` plus ``utt_split=<float
    seconds>``; it is omitted because nothing downstream reads
    ``results.utterances`` — the caption track is built from word timings.
    """
    query: dict[str, str] = {
        "model": model,
        "smart_format": "true",
        "punctuate": "true",
    }
    if language:
        query["language"] = language
    return query


def _ms(seconds: float) -> int:
    """Seconds (Deepgram) → integer milliseconds (TimelineDoc).

    Rounds rather than truncates: a word reported at 0.9999s should not land a
    millisecond early and overlap its neighbour after forty of them accumulate.
    """
    return int(round(float(seconds) * 1000))


def _word_text(entry: dict[str, Any]) -> str:
    """The caption-ready token for one word.

    ``punctuated_word`` ("Hello,") over the raw ``word`` ("hello"): we ask for
    smart_format and punctuate, so the transcript string comes back punctuated,
    and reading the raw form would render a caption track that visibly disagrees
    with the transcript above it. Falls back to ``word`` for any model that does
    not report the punctuated form.
    """
    return str(entry.get("punctuated_word") or entry.get("word") or "").strip()


def _extract_transcript(body: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Pull ``(text, words)`` out of a Deepgram listen response.

    The real path is ``results.channels[0].alternatives[0]`` — first channel,
    best alternative. Returns milliseconds, ready for CaptionWord.

    Raises DeepgramError on a shape we cannot read, because silently returning
    an empty transcript would render as "this video has no speech" — the worst
    possible failure mode for a button whose entire job is finding speech.
    """
    results = body.get("results")
    if not isinstance(results, dict):
        raise DeepgramError("Deepgram response had no `results` object")

    channels = results.get("channels") or []
    text = ""
    raw_words: list[dict[str, Any]] = []

    if channels and isinstance(channels[0], dict):
        alternatives = channels[0].get("alternatives") or []
        if alternatives and isinstance(alternatives[0], dict):
            chosen = alternatives[0]
            text = str(chosen.get("transcript") or "")
            candidate = chosen.get("words") or []
            if isinstance(candidate, list):
                raw_words = [w for w in candidate if isinstance(w, dict)]

    words: list[dict[str, Any]] = []
    for entry in raw_words:
        token = _word_text(entry)
        if not token:
            continue
        start = entry.get("start")
        end = entry.get("end", start)
        if start is None:
            continue
        try:
            start_ms = _ms(start)
            end_ms = _ms(end if end is not None else start)
        except (TypeError, ValueError) as exc:
            raise DeepgramError(f"Deepgram returned a non-numeric word timing: {exc}") from exc
        words.append(
            {
                "text": token,
                "startMs": max(0, start_ms),
                # A zero-length word is unusable downstream, and the caption
                # track clamps cues anyway — guarantee at least 1ms.
                "endMs": max(start_ms + 1, end_ms),
                "confidence": entry.get("confidence"),
            }
        )

    if not text and not words:
        raise DeepgramError("Deepgram returned an empty transcript")

    # Prefer the joined words when the transcript field is absent — some
    # configurations return only the word array.
    if not text:
        text = " ".join(str(word["text"]) for word in words)

    return text, words


async def _listen(
    *, api_key: str, audio_bytes: bytes, content_type: str, query: dict[str, str]
) -> dict[str, Any]:
    """POST the audio and return the parsed response body. Seam for tests.

    The single HTTP call in this module. Audio goes up as the raw request body
    with its own media type (``audio/wav``, ``audio/mpeg``, …) — the JSON body
    form is only for transcribing a remote ``{"url": ...}``, which is not what
    the editor has: it holds bytes decoded in the browser.
    """
    headers = _headers(api_key)
    headers["Content-Type"] = content_type
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS)) as client:
            response = await client.post(
                f"{DEEPGRAM_BASE_URL}/listen",
                params=query,
                headers=headers,
                content=audio_bytes,
            )
            response.raise_for_status()
            body = response.json()
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code if exc.response is not None else "?"
        detail = exc.response.text[:400] if exc.response is not None else ""
        raise DeepgramError(f"Deepgram request failed ({code}): {detail}") from exc
    except Exception as exc:  # noqa: BLE001
        raise DeepgramError(f"Deepgram request failed: {exc}") from exc

    if not isinstance(body, dict):
        raise DeepgramError("Deepgram returned a non-object response")
    return body


async def transcribe_bytes(
    *,
    audio_bytes: bytes,
    content_type: str = "audio/wav",
    model: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    """Transcribe an audio buffer via Deepgram. Returns ``{text, words, model}``.

    ``words`` entries are ``{text, startMs, endMs, confidence}``.
    """
    if not audio_bytes:
        raise DeepgramError("No audio data to transcribe")
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise DeepgramError(
            f"Audio is too large to transcribe ({len(audio_bytes)} bytes; limit {MAX_AUDIO_BYTES})"
        )

    api_key = resolve_api_key()
    if not api_key:
        raise DeepgramError(
            "Deepgram is not configured — set POCKETPAW_DEEPGRAM_API_KEY or DEEPGRAM_API_KEY"
        )

    resolved_model = resolve_model(model)
    query = _build_query(model=resolved_model, language=language)

    body = await _listen(
        api_key=api_key, audio_bytes=audio_bytes, content_type=content_type, query=query
    )

    request_id = (body.get("metadata") or {}).get("request_id")
    text, words = _extract_transcript(body)
    logger.info(
        "deepgram: transcribed %d bytes into %d words (model=%s, request_id=%s)",
        len(audio_bytes),
        len(words),
        resolved_model,
        request_id,
    )
    return {"text": text, "words": words, "model": resolved_model}


__all__ = [
    "DEFAULT_MODEL",
    "DeepgramError",
    "resolve_api_key",
    "resolve_model",
    "transcribe_bytes",
]
