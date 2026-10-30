# ee/pocketpaw_ee/cloud/studio/fal_music.py — direct fal.ai MUSIC/AUDIO client.
#
# The /studio movie-maker surface needs a soundtrack, and the LiteLLM gateway
# serves text-to-image + text/audio (TTS) models but has no route for fal's
# MUSIC-generation endpoints (``fal-ai/elevenlabs/music``,
# ``fal-ai/ace-step-1.5``, ``fal-ai/ace-step/prompt-to-audio``). Those run
# DIRECTLY against fal here — the same decision as the canvas edit ops
# (``fal_edit``), video (``fal_video``), and the Kling panels (``fal_elements`` /
# ``fal_motion``): one credential path (``FAL_AI_API_KEY``), one SDK, and the
# output persists through media storage.
#
# This module is the audio twin of ``fal_video``: resolve the model id → fal
# endpoint, build the arguments (prompt / lyrics / instrumental / duration),
# run the endpoint through the OFFICIAL fal-client SDK
# (``fal_client.AsyncClient``), download the result audio file, and return the
# bytes for the studio service to persist. Argument building + result extraction
# are pure so they unit-test without the SDK, and ``_run_fal`` / ``_download``
# are the seams tests monkeypatch (exactly like the other fal_* modules).
#
# Created 2026-08-25 (studio-music-generation): direct fal music dispatch.

from __future__ import annotations

import logging
from typing import Any

import httpx

from . import fal_edit

logger = logging.getLogger(__name__)

# ── The music model catalog (curated subset of fal's audio endpoints) ───────
#
# Each entry maps a stable catalog key → the fal endpoint + capabilities the
# movie-maker composer needs. ``duration`` handling differs per model:
#   * elevenlabs_music — fal maps ``music_length_ms`` (MILLISECONDS); the studio
#     composer speaks seconds, so we convert here.
#   * ace_step_1_5 / ace_step — fal takes a bare ``duration`` in SECONDS.
# ``instrumental`` handling differs too:
#   * elevenlabs_music — ``force_instrumental`` boolean.
#   * ace_step — ``instrumental`` boolean.
#   * ace_step_1_5 — NO instrumental flag; fal's documented way to force no
#     vocals is ``lyrics`` = "[Instrumental]" (leaving it unset lets the model
#     auto-write vocals).

MUSIC_MODELS: dict[str, dict[str, Any]] = {
    "elevenlabs_music": {
        "id": "fal-ai/elevenlabs/music",
        "name": "ElevenLabs Music",
        "vendor": "ElevenLabs",
        "max_duration": 600,
        "default_duration": 60,
        "formats": ["mp3"],
        "supports_lyrics": False,
        "supports_instrumental": True,
    },
    "ace_step_1_5": {
        "id": "fal-ai/ace-step-1.5",
        "name": "ACE-Step 1.5",
        "vendor": "ACE Studio",
        "max_duration": 600,
        "default_duration": 60,
        "formats": ["wav"],
        "supports_lyrics": True,
        "supports_instrumental": True,
    },
    "ace_step": {
        "id": "fal-ai/ace-step/prompt-to-audio",
        "name": "ACE-Step",
        "vendor": "ACE Studio",
        "max_duration": 240,
        "default_duration": 60,
        "formats": ["wav"],
        "supports_lyrics": False,
        "supports_instrumental": True,
    },
}

DEFAULT_MUSIC_MODEL = "elevenlabs_music"

# Human labels for a future /studio music-model picker (mirrors EDIT_MODEL_LABELS).
MUSIC_MODEL_LABELS: dict[str, str] = {m["id"]: m["name"] for m in MUSIC_MODELS.values()}

# The full set of endpoints we will route to (the override allow-list).
MUSIC_MODEL_IDS: frozenset[str] = frozenset(m["id"] for m in MUSIC_MODELS.values())

# Client + server-side deadlines for the fal call. Music renders in seconds to a
# couple of minutes, so these are generous but bounded so a hung upstream fails
# fast instead of pinning a worker forever.
_CLIENT_TIMEOUT = 300.0
_START_TIMEOUT = 180.0
_DOWNLOAD_TIMEOUT = 120.0


class FalMusicError(Exception):
    """A fal.ai music call failed (missing SDK, upstream error, malformed
    result, or no audio data). The studio service maps this to a 502."""


# ── Catalog helpers ──────────────────────────────────────────────────────────


def resolve_endpoint(model_id: str | None) -> tuple[str, str]:
    """Map a requested model id onto a real fal endpoint + registry key.

    ``fal-ai/...`` ids pass straight through (the caller already picked an
    endpoint) with the matching registry key inferred best-effort; a known
    catalog KEY (``elevenlabs_music``) resolves to its endpoint; anything else
    falls back to ``DEFAULT_MUSIC_MODEL``'s endpoint.
    """
    m = (model_id or "").strip()
    if m in MUSIC_MODELS:
        return MUSIC_MODELS[m]["id"], m
    if m.startswith("fal-ai/"):
        for key, cfg in MUSIC_MODELS.items():
            if cfg["id"] == m:
                return m, key
        return m, DEFAULT_MUSIC_MODEL
    return MUSIC_MODELS[DEFAULT_MUSIC_MODEL]["id"], DEFAULT_MUSIC_MODEL


def clamp_duration(requested: int | None, model_key: str) -> int:
    """Clamp a requested duration (seconds) to the model's accepted range."""
    cfg = MUSIC_MODELS[model_key]
    if not requested or requested <= 0:
        return cfg["default_duration"]
    return max(1, min(int(requested), cfg["max_duration"]))


# ── Argument building ────────────────────────────────────────────────────────


def build_arguments(
    *,
    model_key: str,
    prompt: str,
    lyrics: str | None = None,
    instrumental: bool = True,
    duration_sec: int | None = None,
    steps: int | None = None,
) -> dict[str, Any]:
    """Build the fal ``arguments`` dict for one music call.

    Pure + side-effect free so it is unit-testable in isolation. Per-model
    contract differences (duration units, instrumental/lyrics encoding) live
    here — see the MUSIC_MODELS table for the rationale.
    """
    duration = clamp_duration(duration_sec, model_key)

    if model_key == "elevenlabs_music":
        args: dict[str, Any] = {
            "prompt": prompt,
            # fal's ElevenLabs adapter takes milliseconds.
            "music_length_ms": duration * 1000,
            "force_instrumental": instrumental,
        }
        return args

    if model_key == "ace_step_1_5":
        args = {"prompt": prompt, "duration": duration}
        # No instrumental flag; force no vocals via the documented lyrics token.
        if lyrics and lyrics.strip():
            args["lyrics"] = lyrics.strip()
        elif instrumental:
            args["lyrics"] = "[Instrumental]"
        if steps and steps > 0:
            args["num_inference_steps"] = int(steps)
        return args

    if model_key == "ace_step":
        args = {
            "prompt": prompt,
            "duration": duration,
            "instrumental": instrumental,
            "number_of_steps": steps or 27,
            "scheduler": "euler",
            "guidance_type": "apg",
        }
        return args

    raise ValueError(f"unknown music model key '{model_key}'")


# ── Result handling ──────────────────────────────────────────────────────────


def _extract_audio_url(result: dict[str, Any]) -> str | None:
    """Pull the output audio URL from a fal music result.

    Handles the common shapes: ``audio: {url, …}`` (elevenlabs / ace-step),
    ``audio_url: "…"``, or a bare ``audio: "…"`` / top-level ``url``.
    """
    audio = result.get("audio")
    if isinstance(audio, dict) and isinstance(audio.get("url"), str):
        return audio["url"]
    if isinstance(audio, str) and audio.startswith(("http://", "https://")):
        return audio
    audio_url = result.get("audio_url")
    if isinstance(audio_url, str) and audio_url:
        return audio_url
    if isinstance(result.get("url"), str):
        return result["url"]
    return None


async def _download(url: str) -> tuple[bytes, str]:
    """Download one fal-hosted result audio file and return ``(bytes, mime)``.

    fal media URLs are publicly accessible (no auth header needed) but expire per
    the account's media-expiration setting, so the service persists the bytes
    into media storage before the URL goes stale.
    """
    async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    mime = resp.headers.get("content-type", "audio/mpeg").split(";")[0].strip() or "audio/mpeg"
    return resp.content, mime


# ── The fal SDK seam (tests inject _run_fal) ────────────────────────────────


async def _run_fal(
    endpoint: str,
    arguments: dict[str, Any],
    *,
    key: str,
    client_timeout: float = _CLIENT_TIMEOUT,
    start_timeout: float = _START_TIMEOUT,
) -> dict[str, Any]:
    """Run a fal endpoint via the official SDK. Lazy-imports fal_client so the
    module imports even before the dep is installed (EE lazy-import pattern)."""
    try:
        import fal_client  # noqa: PLC0415 — lazy: SDK is an optional runtime dep
    except ImportError as exc:  # pragma: no cover — env with the dep installed
        raise FalMusicError(
            "fal-client is not installed — run `pip install fal-client` (pocketpaw-ee dep)"
        ) from exc
    try:
        client = fal_client.AsyncClient(key=key, default_timeout=client_timeout)
        result = await client.run(
            endpoint,
            arguments=arguments,
            timeout=client_timeout,
            start_timeout=start_timeout,
        )
    except Exception as exc:  # noqa: BLE001 — surface the upstream reason to the user
        logger.warning("studio: fal music '%s' failed", endpoint, exc_info=True)
        raise FalMusicError(f"fal music '{endpoint}' failed: {exc}") from exc
    if not isinstance(result, dict):
        raise FalMusicError(f"fal music '{endpoint}' returned an unexpected result")
    return result


# ── Public entry point ───────────────────────────────────────────────────────


async def run_fal_music(
    *,
    prompt: str,
    model: str | None = None,
    lyrics: str | None = None,
    instrumental: bool = True,
    duration_sec: int | None = None,
    steps: int | None = None,
    key: str | None = None,
) -> tuple[bytes, str]:
    """Run one fal music generation and return ``(audio_bytes, audio_mime)``.

    ``prompt`` is the style/mood instruction (required); ``model`` may be a
    catalog KEY (``elevenlabs_music``) or a ``fal-ai/...`` endpoint id. Raises
    ValueError (missing prompt) and FalMusicError (missing key / upstream
    failure / no output).
    """
    text = (prompt or "").strip()
    if not text:
        raise ValueError("prompt is required for music generation")

    endpoint, model_key = resolve_endpoint(model)
    arguments = build_arguments(
        model_key=model_key,
        prompt=text,
        lyrics=lyrics,
        instrumental=instrumental,
        duration_sec=duration_sec,
        steps=steps,
    )

    api_key = key if key is not None else fal_edit.fal_api_key()
    if not api_key:
        raise FalMusicError("fal.ai API key is not configured (set FAL_AI_API_KEY)")

    result = await _run_fal(endpoint, arguments, key=api_key)
    audio_url = _extract_audio_url(result)
    if not audio_url:
        raise FalMusicError(f"fal music '{endpoint}' returned no audio data")

    audio_bytes, audio_mime = await _download(audio_url)
    return audio_bytes, audio_mime


__all__ = [
    "MUSIC_MODELS",
    "DEFAULT_MUSIC_MODEL",
    "MUSIC_MODEL_IDS",
    "MUSIC_MODEL_LABELS",
    "FalMusicError",
    "resolve_endpoint",
    "clamp_duration",
    "build_arguments",
    "run_fal_music",
]
