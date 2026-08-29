# ee/pocketpaw_ee/cloud/uploads/transcription.py — turn an uploaded recording
# into text, so a media file becomes a readable, searchable, summarised
# document like everything else in the Files box.
#
# Created 2026-08-29 (T2 "Audio/video transcription at ingest").
#
# THE POINT IS THE PLUMBING, NOT THE MODEL. The transcript is handed back as an
# ordinary ``ExtractionResult`` and dropped into the ONE place the upload
# listener already puts extracted text (T0's ``persist_extracted_text``). From
# there the existing comprehension pass summarises it, the existing tagger tags
# it and the existing kb-go ingest indexes it — none of which needed a line of
# new code. A recording gets a summary, tags and content search because it now
# looks like a document to everything downstream.
#
# IT ALSO REPLACES ``chain.run`` FOR MEDIA, and that is a fix, not a
# side-effect. ``LocalExtractor`` advertises ``supports_mimes = {"*"}`` and its
# last branch is ``path.read_text(errors="replace")`` — so today an uploaded
# video is slurped whole into a string of replacement characters, and that
# string is persisted, summarised, tagged and pushed into the knowledge base.
# Routing media here instead means the binary is never read as text.
#
# ── THE MODEL, PROBED RATHER THAN ASSUMED (all figures 2026-08-29) ──────────
#
# ``fal-ai/wizper``. The gateway serves no transcription model at all (80
# models, zero speech-to-text), so this calls fal directly, the way
# ``other_hand/illustrate.py`` and ``studio/fal_edit.py`` already do.
#
# Measured on one 21.64 s Spanish clip, 3 calls per endpoint, price read as the
# delta on fal's ``billing/user_balance``:
#
#   fal-ai/wizper  + language:null   $0.0017/audio-min   0.37 s   correct
#   fal-ai/wizper  (default)         $0.0012/audio-min   0.28 s   WRONG (below)
#   fal-ai/whisper                   $0.0062/audio-min   1.69 s   correct
#   fal-ai/elevenlabs/.../scribe-v2  $0.008/audio-min documented, not probed
#   fal-ai/speech-to-text            no result in 120 s — queue-only
#
# A 15.8-minute file measured $0.00375 end to end, i.e. ~$0.0002/audio-min:
# fal bills compute time, so the per-minute rate FALLS with length and the
# short-clip figures above are the pessimistic end. Either way a file at the
# 30-minute ceiling costs between a fifth of a cent and five cents.
#
# ``language=None`` IS LOAD-BEARING. wizper's ``language`` parameter defaults
# to ``"en"`` — not to auto-detect (confirmed in its OpenAPI schema). With the
# default, Spanish audio came back as fluent, plausible English: a transcript
# of something nobody said, which would then be summarised, tagged and indexed
# as if it were true. Passing ``None`` explicitly buys auto-detection for ~50%
# more per minute and is still 3.6x cheaper than ``fal-ai/whisper``. This is
# the one parameter in this file that must never be dropped as "the default is
# fine", and there is a test pinning it.
#
# Verified by real call: wizper accepts a VIDEO file (an mp4 uploaded through
# ``fal_client.upload_file`` transcribed its audio track, language detected
# correctly), and a 15.8-minute upload completed through ``subscribe()`` in
# 43 s with segments covering the full duration — nothing truncated.
#
# ── THE CEILING COMES BEFORE THE SPEND ─────────────────────────────────────
#
# Two of them, and the order matters. ``media_duration`` reads the real length
# out of the container header for mp4/mov/m4a, wav and mp3 — free, exact, no
# ffmpeg, no new dependency. A 3-hour recording is refused after reading a few
# kilobytes, with the reason recorded on the file, instead of being discovered
# halfway through a bill. For the containers it cannot read (ogg/opus, webm,
# flac) it returns "unknown", never "short", and the byte ceiling governs
# instead. Residual exposure stated honestly: a 256 MB Ogg of continuous speech
# is ~4.5 hours ≈ $0.05 at the measured long-file rate, and the daily cap
# bounds how many of those a workspace can queue up.
#
# The ceiling is checked BEFORE the budget is claimed so an over-long file
# never burns a slot a transcribable one could have used.
#
# ── PRIVACY ────────────────────────────────────────────────────────────────
#
# ``fal_client.upload_file`` puts the user's recording on fal's CDN at an
# unguessable URL, which is a real thing to say out loud rather than leave
# implicit. It is the same trust boundary as sending the bytes to fal at all
# (``studio/fal_edit`` already ships user images there), and the listener's
# ``hide_from_ai`` gate returns before any of this runs, so a file the user
# marked private is never uploaded. Nothing here re-checks that gate because
# nothing here should be reachable without it.
#
# ── FAILURE SEMANTICS ──────────────────────────────────────────────────────
#
# Nothing in this module raises at its caller. It returns one of three things,
# and the split is between what we learned about the FILE and what we learned
# about the WORLD:
#
#   * an ``ExtractionResult`` with the transcript — the normal path;
#   * an ``ExtractionResult`` with EMPTY text and a recorded ``skipped``
#     reason — we know something durable about this file (it is longer than we
#     will pay for, or it contains no speech). The listener persists that, so
#     the reason survives, and every downstream pass treats it as a document
#     with no text: no tags, no summary, no KB article, and — importantly — no
#     later consumer falling back to the chain and slurping the binary;
#   * ``None`` — we learned nothing about the file, only that the world was
#     unavailable (no key, no SDK, today's budget spent, fal errored or timed
#     out). The listener persists NOTHING, so a re-ingest is a clean retry.

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from pocketpaw_ee.cloud.extraction.adapter import ExtractionResult

logger = logging.getLogger(__name__)

#: Mime families this module claims. Checked as a prefix because the wild is
#: full of ``audio/x-m4a``, ``video/quicktime``, ``audio/mpeg;codecs=...``.
_TRANSCRIBABLE_PREFIXES = ("audio/", "video/")

_ENV_MODEL = "POCKETPAW_FILE_TRANSCRIPTION_MODEL"
#: See the module header for the measured comparison that chose this.
DEFAULT_MODEL = "fal-ai/wizper"
#: The 3.6x-more-expensive endpoint, kept named so switching is a value, not a
#: search. It adds diarization and a prompt field if either is ever wanted.
FULL_WHISPER_MODEL = "fal-ai/whisper"

_ENV_MAX_MINUTES = "POCKETPAW_FILE_TRANSCRIPTION_MAX_MINUTES"
_DEFAULT_MAX_MINUTES = 30.0

_ENV_MAX_MB = "POCKETPAW_FILE_TRANSCRIPTION_MAX_MB"
_DEFAULT_MAX_MB = 256.0

_ENV_TIMEOUT = "POCKETPAW_FILE_TRANSCRIPTION_TIMEOUT_S"
#: Upload + transcribe, end to end. A measured 15.8-minute file took 62 s all
#: in; this is ~10x that headroom, and it exists so a hung upstream fails
#: instead of pinning a listener forever.
_DEFAULT_TIMEOUT_S = 600.0

#: ``backend`` on every ``ExtractionResult`` this module produces, so a stored
#: blob says what made it.
BACKEND = "fal-transcription"

#: Segments are kept for a future "jump to 12:31" affordance, out of ``text``
#: so search and summarisation see speech rather than timestamps. Bounded: a
#: 15.8-minute file produced 36, so this is a guard against a pathological
#: response, not a real limit.
_MAX_SEGMENTS = 2000


def _env_float(name: str, default: float) -> float:
    """A positive float from the environment, or ``default``.

    Junk is a misconfiguration, not an instruction. Reading ``"thirty"`` as 0
    would switch transcription off for a whole deployment without saying so —
    the failure shape this whole track is written against.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s is not a number (%r) — using the default", name, raw)
        return default
    if value <= 0:
        logger.warning("%s must be positive (%r) — using the default", name, raw)
        return default
    return value


def max_minutes() -> float:
    """Longest recording we will pay to transcribe."""
    return _env_float(_ENV_MAX_MINUTES, _DEFAULT_MAX_MINUTES)


def max_bytes() -> int:
    """Largest file we will send, for the containers whose length we cannot read."""
    return int(_env_float(_ENV_MAX_MB, _DEFAULT_MAX_MB) * 1024 * 1024)


def timeout_seconds() -> float:
    """End-to-end deadline for one transcription."""
    return _env_float(_ENV_TIMEOUT, _DEFAULT_TIMEOUT_S)


def configured_model() -> str:
    """The fal endpoint this deployment transcribes with."""
    return (os.environ.get(_ENV_MODEL) or "").strip() or DEFAULT_MODEL


def is_transcribable(mime: str | None) -> bool:
    """Whether this mime is media we should transcribe instead of extract."""
    norm = (mime or "").split(";", 1)[0].strip().lower()
    return norm.startswith(_TRANSCRIBABLE_PREFIXES)


def build_arguments(audio_url: str) -> dict[str, Any]:
    """The fal ``arguments`` for one transcription. Pure, so it can be pinned.

    ``language`` is present and ``None`` ON PURPOSE — see the module header.
    wizper defaults it to ``"en"``, and the default silently returns fluent
    English for audio that was not in English. Dropping this key does not fail;
    it produces a confident transcript of something nobody said.
    """
    return {
        "audio_url": audio_url,
        "task": "transcribe",
        "chunk_level": "segment",
        "language": None,
    }


def _skipped(reason: str, **detail: Any) -> ExtractionResult:
    """A textless result that RECORDS why, for the listener to persist."""
    return ExtractionResult(
        text="",
        metadata={"transcription": {"skipped": reason, **detail}},
        backend=BACKEND,
    )


def _api_key() -> str | None:
    """The deployment's fal key, resolved by the one rule the repo already has.

    Delegates to ``studio.fal_edit.fal_api_key`` (``FAL_AI_API_KEY``, then
    ``FAL_KEY``, with an explicit ``load_dotenv`` because the serve process
    never merges ``.env`` into ``os.environ``). Imported lazily and wrapped in
    this function so there is exactly one resolution rule in the codebase and
    exactly one thing for a test to replace. If a third consumer appears, move
    the helper somewhere neutral rather than copying it a second time.
    """
    from pocketpaw_ee.cloud.studio.fal_edit import fal_api_key

    return fal_api_key()


def _extract_transcript(result: Any) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Pull ``(text, segments, languages)`` out of the endpoint's response.

    Reads wizper's documented shape — ``{"text", "chunks", "languages"}``,
    confirmed by a real call — but does not insist on it. fal endpoints vary
    and an override may point at a sibling model, so a missing field reads as
    "no transcript" rather than crashing a listener.
    """
    if not isinstance(result, dict):
        return "", [], []
    text = result.get("text")
    text = text.strip() if isinstance(text, str) else ""

    segments: list[dict[str, Any]] = []
    for chunk in (result.get("chunks") or [])[:_MAX_SEGMENTS]:
        if not isinstance(chunk, dict):
            continue
        piece = chunk.get("text")
        if not isinstance(piece, str) or not piece.strip():
            continue
        stamp = chunk.get("timestamp")
        start, end = (stamp + [None, None])[:2] if isinstance(stamp, list) else (None, None)
        segments.append({"start": start, "end": end, "text": piece.strip()})

    languages = [lang for lang in (result.get("languages") or []) if isinstance(lang, str)]
    return text, segments, languages


async def _call_fal(path: Path, model: str, key: str) -> Any:
    """Upload the media and run the endpoint. Raises on any failure."""
    import fal_client  # noqa: PLC0415 — lazy: EE pattern for optional runtime deps

    client = fal_client.AsyncClient(key=key)
    audio_url = await client.upload_file(str(path))
    # ``subscribe`` is the QUEUE path, not the synchronous ``run``. A
    # 30-minute file is ~90 s of inference; the sync endpoint is meant for
    # short calls and would put this feature's whole reason for existing —
    # meetings and podcasts — on the side of the boundary that times out.
    return await client.subscribe(model, arguments=build_arguments(audio_url))


async def transcribe_media(
    *,
    path: Path,
    mime: str,
    file_id: str,
    workspace_id: str,
    filename: str = "",
) -> ExtractionResult | None:
    """Transcribe an uploaded recording into an ``ExtractionResult``.

    Returns the transcript, a textless result carrying a recorded ``skipped``
    reason, or ``None``. See the module header for which is which; the short
    version is that a result means "persist this", ``None`` means "we learned
    nothing, retry on the next ingest".

    Never raises.
    """
    # ── Gate 1: is it ours at all ──────────────────────────────────────────
    if not is_transcribable(mime):
        return None

    # ── Gate 2: can this deployment transcribe ─────────────────────────────
    try:
        key = _api_key()
    except Exception:
        logger.warning("transcription: could not resolve a fal key", exc_info=True)
        return None
    if not key:
        logger.info(
            "transcription: no fal key configured; file_id=%s (%s) keeps no transcript",
            file_id,
            mime,
        )
        return None

    # ── Gate 3: the ceiling, BEFORE any spend and before a budget slot ─────
    try:
        size = path.stat().st_size
    except OSError:
        logger.warning("transcription: cannot stat file_id=%s at %s", file_id, path)
        return None

    from pocketpaw_ee.cloud.uploads.media_duration import probe_duration_seconds

    duration = probe_duration_seconds(path)
    limit_minutes = max_minutes()
    if duration is not None and duration > limit_minutes * 60:
        logger.info(
            "transcription: file_id=%s is %.1f min, over the %.1f min ceiling; skipping",
            file_id,
            duration / 60,
            limit_minutes,
        )
        return _skipped(
            "too_long",
            duration_seconds=round(duration, 1),
            limit_minutes=limit_minutes,
        )

    limit_bytes = max_bytes()
    if size > limit_bytes:
        logger.info(
            "transcription: file_id=%s is %.1f MB, over the %.1f MB ceiling "
            "(duration %s); skipping",
            file_id,
            size / 1024 / 1024,
            limit_bytes / 1024 / 1024,
            f"{duration:.0f}s" if duration is not None else "unknown",
        )
        return _skipped(
            "too_large",
            size_bytes=size,
            limit_bytes=limit_bytes,
            duration_seconds=round(duration, 1) if duration is not None else None,
        )

    # ── Gate 4: today's budget. Fails CLOSED. ──────────────────────────────
    from pocketpaw_ee.cloud.uploads.transcription_budget import try_spend

    allowed, spent, cap = await try_spend(workspace_id)
    if not allowed:
        logger.warning(
            "transcription: refused by the daily budget (%d/%d) for workspace=%s "
            "file_id=%s; a re-ingest will retry",
            spent,
            cap,
            workspace_id,
            file_id,
        )
        return None

    # ── The spend ──────────────────────────────────────────────────────────
    model = configured_model()
    try:
        raw = await asyncio.wait_for(_call_fal(path, model, key), timeout=timeout_seconds())
    except TimeoutError:
        logger.warning(
            "transcription: %s did not answer within %.0fs for file_id=%s",
            model,
            timeout_seconds(),
            file_id,
        )
        return None
    except ImportError:
        # A missing SDK is a PACKAGING failure, and packaging failures in this
        # codebase have a habit of reading as "the feature is off" (pypdf was
        # absent from the production image for months behind exactly this kind
        # of except). fal-client is a base dependency of pocketpaw-ee and
        # tests/packaging pins it there; if this line ever fires, the image is
        # wrong, so it is logged as an ERROR that names the fix.
        logger.error(
            "transcription: fal-client is not installed — the image is missing a "
            "BASE dependency of pocketpaw-ee, not an optional one. No media file "
            "in this deployment will ever be transcribed until it is present.",
            exc_info=True,
        )
        return None
    except Exception:
        logger.warning(
            "transcription: %s failed for file_id=%s; a re-ingest will retry",
            model,
            file_id,
            exc_info=True,
        )
        return None

    text, segments, languages = _extract_transcript(raw)
    if not text:
        # We paid, and there was nothing to hear. That IS a fact about the
        # file, so it is recorded rather than retried: re-running would buy
        # the same silence again.
        logger.info("transcription: %s returned no speech for file_id=%s", model, file_id)
        return _skipped(
            "no_speech",
            model=model,
            duration_seconds=round(duration, 1) if duration is not None else None,
        )

    logger.info(
        "transcription: %d chars from file_id=%s (%s, %s) via %s — budget %d/%d",
        len(text),
        file_id,
        mime,
        f"{duration / 60:.1f} min" if duration is not None else "unknown length",
        model,
        spent,
        cap,
    )
    return ExtractionResult(
        text=text,
        metadata={
            "mime": mime,
            "filename": filename,
            "transcription": {
                "model": model,
                "languages": languages,
                "duration_seconds": round(duration, 1) if duration is not None else None,
                "segments": segments,
            },
        },
        backend=BACKEND,
    )


__all__ = [
    "BACKEND",
    "DEFAULT_MODEL",
    "FULL_WHISPER_MODEL",
    "build_arguments",
    "configured_model",
    "is_transcribable",
    "max_bytes",
    "max_minutes",
    "timeout_seconds",
    "transcribe_media",
]
