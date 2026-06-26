# media.py — in-process MCP server exposing the STUDIO media-generation actions
# (image + audio + video) to the claude_agent_sdk cloud chat backend.
#
# Created: 2026-06-10 (feat/studio-code-migration).
# 2026-06-26 (MCG-6 + MCG-7): media generation now routes through the self-hosted
# LiteLLM proxy via its OpenAI-compatible endpoints instead of calling Google /
# Replicate directly. One gateway means one credential path, one spend log, and
# any model the proxy serves (gpt-image-1, dall-e-*, imagen-*, tts-1, whisper-*,
# sora/veo/kling) is reachable by catalog model id with no per-provider code:
#   * image → POST {proxy}/v1/images/generations  (no response_format param —
#     gpt-image-1 / the newer dall-e API reject it; we accept either b64_json
#     OR url in the response, confirmed against the live proxy 2026-06-26)
#   * audio TTS → POST {proxy}/v1/audio/speech       (raw audio bytes back)
#   * audio STT → POST {proxy}/v1/audio/transcriptions (multipart upload)
#   * video → POST {proxy}/videos                    (async job, GET-poll)
# The model id comes from the CALLER (a catalog model id passed as `model`), not a
# hardcoded settings field. When the caller passes nothing we fall back to a
# per-modality default (image keeps settings.image_model for backward-compat with
# the existing studio skill / preamble, which pass only prompt + aspect_ratio).
# Proxy base/key resolution + the httpx style are REUSED from the catalog entity
# (ee.pocketpaw_ee.catalog.config + its LiteLLMClient header/transport shape) — we
# do NOT re-resolve the proxy URL/key here. The tenant is tagged on every request
# via the OpenAI `user` field (the proxy keys per-end-customer spend off it) so
# the existing proxy spend log attributes cost to the workspace; we do not block
# on metering — the proxy already logs it.
# 2026-06-26 (MCG-8): each proxy call now Bearers the workspace's PROVISIONED
# per-tenant LiteLLM virtual key (resolved best-effort via
# ee.cloud.llm_provisioning.service.get_tenant_key) instead of the deployment
# master key, so the proxy enforces the tenant's budget + rate caps and attributes
# spend to the tenant key. Falls back to the master key when a workspace has no
# key yet (provisioning hasn't run) or the lookup fails — media never breaks on
# key resolution, and the `user=workspace_id` tag still attributes spend either
# way. See ``_resolve_auth_key``.
#
# What this file does: clones the sites.py / sites_create.py shape — a single
# ``create_sdk_mcp_server`` with an SDK import-guard, ``SERVER_NAME`` /
# ``*_TOOL_ID`` allowlist constants, ContextVar-sourced identity (the same
# ``current_workspace_id`` / ``current_user_id`` accessors in
# ``ee.cloud.chat.agent_service`` the sites / pocket-specialist servers read),
# and the ``_error_response`` / ``_success_response`` helpers. Tool ids namespace
# as ``mcp__pocketpaw_media__image_generate`` / ``__audio_generate`` /
# ``__audio_transcribe`` / ``__video_generate`` so the Claude Code allowlist
# machinery matches them.
#
# Four SDK @tool defs (image / audio_generate (TTS) / audio_transcribe (STT) /
# video). Generated assets (image bytes, TTS audio bytes, the video output URL)
# land in a STUDIO gallery pocket via the TRUSTED-CREATE path
# (``agent_create(... type_="studio", pattern="gallery", engine="ripple",
# ripple_spec=<gallery spec>, trusted=True)``) so the strict catalog gate is
# bypassed (image / video-player widgets lag the published manifest), then binds
# the session + emits ``pocket_created`` exactly like sites_create.py
# (``_bind_session_and_emit``). The gallery spec uses ONLY existing widget types
# (grid / card / image / video-player / text). Transcription (STT) is an input
# op, not a generated asset, so it returns the text directly and does NOT touch
# the gallery. Media accumulates across the session (per-session state holds the
# gallery's media list); each generation re-creates the gallery with the full
# list and emits the refreshed pocket, so the canvas always shows every asset
# made this session.
#
# EE→OSS boundary: this module lives in pocketpaw_ee; the surface service loads
# MEDIA_TOOL_IDS as a plain frozenset[str] inside a try/except (never importing a
# pocketpaw_ee symbol into src/pocketpaw).
"""Agent-side MCP surface for STUDIO image + audio + video generation."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_media"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
# Allowlist entries must use this exact form.
IMAGE_GENERATE_TOOL_ID = f"mcp__{SERVER_NAME}__image_generate"
AUDIO_GENERATE_TOOL_ID = f"mcp__{SERVER_NAME}__audio_generate"
AUDIO_TRANSCRIBE_TOOL_ID = f"mcp__{SERVER_NAME}__audio_transcribe"
VIDEO_GENERATE_TOOL_ID = f"mcp__{SERVER_NAME}__video_generate"

MEDIA_TOOL_IDS = (
    IMAGE_GENERATE_TOOL_ID,
    AUDIO_GENERATE_TOOL_ID,
    AUDIO_TRANSCRIBE_TOOL_ID,
    VIDEO_GENERATE_TOOL_ID,
)

# Per-modality default model ids used when the caller passes no ``model``. THE
# REAL PATH is the caller passing a catalog model id (the picker / studio skill
# selects one) — these defaults are only a backward-compat floor for callers
# that pass nothing, and they only route if the proxy actually serves that id.
# Image prefers a known-served default and falls back to ``settings.image_model``
# (resolved at call time); audio/video use proxy-style ids an operator is
# expected to front. An operator can always pick any served model explicitly.
_DEFAULT_IMAGE_MODEL = "gpt-image-1"  # an OpenAI-compatible id the proxy serves
_DEFAULT_AUDIO_TTS_MODEL = "tts-1"
_DEFAULT_AUDIO_STT_MODEL = "whisper-1"
_DEFAULT_VIDEO_MODEL = "sora"  # a proxy-style id; NOT a Replicate owner/name slug

# Map the studio skill's aspect_ratio hint onto an OpenAI-compatible ``size``
# (the images/generations + videos endpoints take ``size``, not aspect_ratio).
# An explicit ``size`` arg always wins; this only fills the gap when the caller
# passed aspect_ratio (the existing bundled-skill contract) and no size.
_ASPECT_RATIO_TO_SIZE: dict[str, str] = {
    "1:1": "1024x1024",
    "16:9": "1792x1024",
    "9:16": "1024x1792",
}

# LiteLLM's async video endpoint returns a job; poll its status until terminal,
# bounded so a stuck job can't hang the chat turn (video can be slow).
_VIDEO_POLL_INTERVAL_SECONDS = 3.0
_VIDEO_POLL_MAX_SECONDS = 180.0


def _error_response(message: str) -> dict[str, Any]:
    """Build an MCP error response in the shape Claude's SDK expects. The agent
    reads ``text`` and surfaces the reason."""
    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "is_error": True,
    }


def _success_response(body: dict[str, Any]) -> dict[str, Any]:
    """Build an MCP success response carrying ``body`` as JSON."""
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(body, separators=(",", ":"), default=str),
            }
        ]
    }


def _str_arg(args: dict, key: str, default: str | None = None) -> str | None:
    """Return ``args[key]`` when it is a non-empty string, else ``default``.
    Keeps the optional-string arg handling (model / voice / size / format)
    uniform across the handlers."""
    val = args.get(key)
    if isinstance(val, str) and val.strip():
        return val
    return default


def _resolve_size(args: dict) -> str | None:
    """Resolve an OpenAI-compatible ``size`` from the request args. An explicit
    ``size`` wins; otherwise the studio skill's ``aspect_ratio`` hint is mapped
    via _ASPECT_RATIO_TO_SIZE so a user asking 16:9 doesn't silently get the
    model default. Returns None when neither is usable (model picks its default).
    """
    size = _str_arg(args, "size")
    if size:
        return size
    aspect = _str_arg(args, "aspect_ratio")
    if aspect:
        return _ASPECT_RATIO_TO_SIZE.get(aspect.strip())
    return None


def _looks_like_replicate_slug(model: str) -> bool:
    """True when ``model`` looks like a Replicate ``owner/name`` slug rather than
    a proxy catalog id. Used so the legacy ``settings.video_model`` default
    (kwaivgi/kling-v2.0) is NOT POSTed to the proxy's /videos endpoint, which
    won't serve a raw Replicate slug. Heuristic: a single '/' with non-empty
    halves and no leading provider we recognise as proxy-style. Catalog ids are
    bare (``sora``) or ``provider/model`` where provider is an LLM provider; a
    Replicate slug is an account handle. We can't perfectly tell them apart, so
    we treat ANY ``a/b`` as a slug here — proxy callers that want a namespaced id
    should pass it explicitly via ``model`` rather than rely on the video_model
    setting, which exists for the old direct-Replicate path."""
    return model.count("/") == 1 and all(part.strip() for part in model.split("/"))


def _default_image_model() -> str:
    """The image model to use when the caller passes none. Prefer the known-served
    default; honour ``settings.image_model`` ONLY if the operator deliberately
    changed it from its field default (``gemini-2.5-flash-image``, which only
    routes if the proxy aliases that exact id). Keeps the setting meaningful for
    an operator who pointed it at a served id, without shipping the unroutable
    field default as the de-facto proxy default."""
    from pocketpaw.config import Settings, get_settings

    configured = get_settings().image_model
    field_default = Settings.model_fields["image_model"].default
    if configured and configured != field_default:
        return configured
    return _DEFAULT_IMAGE_MODEL


def _default_video_model() -> str:
    """The video model to use when the caller passes none. The legacy
    ``settings.video_model`` default is a Replicate ``owner/name`` slug
    (kwaivgi/kling-v2.0) that the proxy's /videos endpoint won't serve, so we do
    NOT forward a slug: prefer a proxy-style default, and use
    ``settings.video_model`` only when an operator set it to a NON-slug proxy id."""
    from pocketpaw.config import get_settings

    configured = get_settings().video_model
    if configured and not _looks_like_replicate_slug(configured):
        return configured
    return _DEFAULT_VIDEO_MODEL


def _identity() -> tuple[str | None, str | None]:
    """Resolve the active workspace + user id from the per-stream ContextVars set
    by the cloud chat agent runtime. Returns ``(workspace_id, user_id)``."""
    try:
        from pocketpaw_ee.cloud.chat.agent_service import current_user_id, current_workspace_id

        return current_workspace_id(), current_user_id()
    except Exception:  # noqa: BLE001
        return None, None


# ── LiteLLM proxy transport (REUSED from the catalog entity) ─────────────────
# Proxy base + key resolution lives in ee.pocketpaw_ee.catalog.config; the header
# + httpx style mirrors catalog.litellm_client.LiteLLMClient. We do NOT
# re-implement proxy URL/key handling here — one config path for the whole
# deployment. The ``user`` field on each request tags the tenant so the proxy's
# spend log attributes cost to the workspace (meter-friendly; we never block on
# metering — the proxy already logs spend).


def _proxy_base() -> str:
    """The LiteLLM proxy base URL (trailing slash trimmed), from catalog config."""
    from pocketpaw_ee.catalog import config

    return config.litellm_proxy_url()


def _proxy_key() -> str | None:
    """The LiteLLM proxy admin/MASTER key (or None), from catalog config. The
    fallback Bearer when a workspace has no provisioned per-tenant virtual key."""
    from pocketpaw_ee.catalog import config

    return config.litellm_proxy_api_key()


async def _resolve_auth_key(workspace_id: str | None) -> str | None:
    """Resolve the Bearer key for a tenant's media proxy call (MCG-8).

    Prefer the workspace's PROVISIONED LiteLLM virtual key (so the proxy enforces
    the tenant's budget + attributes spend to the key, not just the ``user`` tag);
    fall back to the deployment master key when the workspace has no key yet (or
    the lookup fails). Best-effort + non-fatal: media must keep working on the
    master key when provisioning hasn't run — the ``user=workspace_id`` tag still
    attributes spend in the proxy log either way. Importing the provisioning
    service lazily keeps the agent MCP import graph free of Beanie at module load.
    """
    if workspace_id:
        try:
            from pocketpaw_ee.cloud.llm_provisioning import service as provisioning

            tenant_key = await provisioning.get_tenant_key(workspace_id)
            if tenant_key:
                return tenant_key
        except Exception:  # noqa: BLE001 — never let key resolution break a generation
            logger.debug(
                "media: tenant key lookup failed for workspace=%s; using master key",
                workspace_id,
                exc_info=True,
            )
    return _proxy_key()


def _proxy_headers(*, json_content: bool = True, auth_key: str | None = None) -> dict[str, str]:
    """Bearer the proxy key (when set) the same way the catalog client does.
    ``auth_key`` is the per-tenant virtual key resolved by ``_resolve_auth_key``;
    when None we fall back to the deployment master key (the pre-MCG-8 behaviour).
    ``json_content`` adds the JSON content-type for the OpenAI-compatible JSON
    endpoints; multipart uploads (STT) let httpx set the boundary itself."""
    headers: dict[str, str] = {}
    key = auth_key if auth_key is not None else _proxy_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if json_content:
        headers["Content-Type"] = "application/json"
    return headers


def _proxy_client(timeout: float = 120.0) -> httpx.AsyncClient:
    """An httpx.AsyncClient pointed at the proxy. Media generation is slow
    (image/audio seconds, video much longer), so the default timeout is generous
    relative to the catalog client's 15s reads. Tests inject a MockTransport via
    ``_PROXY_TRANSPORT``."""
    return httpx.AsyncClient(transport=_PROXY_TRANSPORT, timeout=timeout)


# Tests set this to an httpx.MockTransport so the proxy HTTP calls are exercised
# without a live proxy (the same injection seam catalog.litellm_client exposes as
# the ``_transport`` ctor arg). Production leaves it None (real network).
_PROXY_TRANSPORT: httpx.BaseTransport | None = None


# ── Per-session gallery state ───────────────────────────────────────────────
# Keyed by session mongo id, holds the running list of media items produced this
# session so subsequent generations append to the same gallery rather than
# starting empty. ``pocket_id`` is the most-recently-created gallery pocket for
# the session. Best-effort, in-process — a fresh process just starts a new
# gallery, which is acceptable (the asset files persist regardless).
_GALLERY_STATE: dict[str, dict[str, Any]] = {}


def _generated_dir() -> Path:
    """Get (and create) the directory for generated media assets — same location
    the OSS ImageGenerateTool uses (``get_config_dir()/generated``)."""
    from pocketpaw.config import get_config_dir

    d = get_config_dir() / "generated"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _build_gallery_spec(media: list[dict[str, Any]]) -> dict[str, Any]:
    """Assemble a STUDIO gallery rippleSpec from the accumulated media list.

    Uses ONLY existing widget types (``grid`` / ``card`` / ``image`` /
    ``video-player`` / ``text``) so it renders today. Each media item becomes a
    card tile in a responsive grid; an ``image`` item carries a local file path,
    a ``video`` item carries the provider output URL, an ``audio`` item carries a
    text note with its local path (no audio widget exists yet — degrade to a
    labelled text tile rather than introduce an unrendered widget). A leading
    ``text`` node titles the gallery. ``media`` items:
    ``{kind: "image"|"video"|"audio", src, prompt}``.
    """
    tiles: list[dict[str, Any]] = []
    for i, item in enumerate(media):
        kind = item.get("kind")
        src = item.get("src", "")
        prompt = item.get("prompt", "")
        if kind == "video":
            inner = {"type": "video-player", "props": {"src": src, "controls": True}}
        elif kind == "audio":
            # No audio-player widget in the manifest yet — show the audio as a
            # labelled text tile so the gallery still renders with known widgets.
            inner = {"type": "text", "props": {"text": f"Audio: {src}"}}
        else:
            inner = {"type": "image", "props": {"src": src, "alt": prompt}}
        tiles.append(
            {
                "id": f"tile-{i}",
                "type": "card",
                "children": [
                    inner,
                    {"type": "text", "props": {"text": prompt}},
                ],
            }
        )
    # version + a non-dashboard intent make pocketsStore.toRippleEnvelope pass
    # the spec through untouched, and Ripple render it in NODE mode (a bare
    # {"ui": [...]} got intent="dashboard" stamped on, which makes Ripple read
    # `widgets` — always empty here — and render "No widgets yet"). `ui` must
    # be a single root node, not a list: NodeRenderer takes one node.
    return {
        "version": "2.0",
        "intent": "gallery",
        "ui": {
            "id": "studio-root",
            "type": "container",
            "children": [
                {
                    "id": "studio-title",
                    "type": "text",
                    "props": {"text": "Generated media"},
                },
                {
                    "id": "studio-grid",
                    "type": "grid",
                    "props": {"columns": 3, "gap": "1rem"},
                    "children": tiles,
                },
            ],
        },
    }


async def _bind_session_and_emit(pocket_id: str, view: dict[str, Any], user_id: str) -> None:
    """Bind the active chat session to the new gallery pocket and push the
    ``pocket_created`` SSE event so the canvas auto-opens — the same atomic
    post-create side effects ``sites_create.py`` runs. Best-effort: a bind / SSE
    failure must never undo a successful create (the pocket already exists in
    Mongo, which is the primary contract)."""
    try:
        from pocketpaw_ee.cloud.chat.agent_service import (
            current_session_mongo_id,
            push_sse_event,
        )
        from pocketpaw_ee.cloud.sessions import service as sessions_service

        session_mongo_id = current_session_mongo_id()
        if session_mongo_id:
            await sessions_service.attach_pocket_to_session_doc(
                session_mongo_id, user_id, pocket_id
            )
        push_sse_event(
            "pocket_created",
            {"pocket_id": pocket_id, "pocket": view, "session_id": session_mongo_id},
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "media: post-create side effects failed (non-fatal)",
            exc_info=True,
        )


async def _land_in_gallery(
    *,
    workspace_id: str,
    user_id: str,
    kind: str,
    src: str,
    prompt: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Append a freshly generated asset to the session's STUDIO gallery and
    persist it via the TRUSTED-CREATE path.

    Tracks the running media list per session, rebuilds the full gallery spec,
    and calls ``agent_create(... type_="studio", pattern="gallery",
    ripple_spec=<spec>, trusted=True)`` so the strict catalog gate is bypassed
    (the image / video-player widgets lag the published manifest, same reason
    sites_create.py uses ``trusted=True``). Then binds the session + emits
    ``pocket_created``. Returns ``({pocket_id, count}, None)`` on success or
    ``(None, error)`` on failure.
    """
    from pocketpaw_ee.cloud.chat.agent_service import current_session_mongo_id
    from pocketpaw_ee.cloud.pockets.service import agent_create

    session_key = current_session_mongo_id() or f"{workspace_id}:{user_id}"
    state = _GALLERY_STATE.setdefault(session_key, {"media": [], "pocket_id": None})
    state["media"].append({"kind": kind, "src": src, "prompt": prompt})

    ripple_spec = _build_gallery_spec(state["media"])

    try:
        view, new_pocket_id, err = await agent_create(
            workspace_id=workspace_id,
            owner_id=user_id,
            name="Studio gallery",
            description="Media generated in /studio",
            type_="studio",
            pattern="gallery",
            engine="ripple",
            ripple_spec=ripple_spec,
            trusted=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("media: gallery persist raised", exc_info=True)
        return None, f"gallery persist failed: {exc}"

    if err is not None or view is None or new_pocket_id is None:
        return None, f"gallery persist failed: {err or 'create returned no view'}"

    state["pocket_id"] = new_pocket_id
    await _bind_session_and_emit(new_pocket_id, view, user_id)
    return {"pocket_id": new_pocket_id, "count": len(state["media"])}, None


# ── Image (POST {proxy}/v1/images/generations) ──────────────────────────────


async def _proxy_image(
    *, model: str, prompt: str, size: str | None, user: str, auth_key: str | None = None
) -> tuple[bytes | None, str | None]:
    """Generate an image via the proxy's OpenAI-compatible image endpoint.

    POSTs ``{model, prompt, n:1, size?, user}`` to ``/v1/images/generations`` and
    accepts EITHER return shape: ``data[0].b64_json`` (gpt-image-1 always returns
    base64) or ``data[0].url`` (dall-e fetched and its bytes returned). We do NOT
    send ``response_format`` — gpt-image-1 / the newer dall-e API reject it
    ("Unknown parameter: 'response_format'"), confirmed against the live proxy —
    so the caller always gets raw image bytes to save regardless of model.
    Returns ``(bytes, None)`` or ``(None, error)``.
    """
    base = _proxy_base()
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "user": user,
    }
    if size:
        payload["size"] = size
    try:
        async with _proxy_client() as client:
            resp = await client.post(
                f"{base}/v1/images/generations",
                headers=_proxy_headers(auth_key=auth_key),
                json=payload,
            )
            resp.raise_for_status()
            body = resp.json()
            data = (body.get("data") or [{}])[0]
            b64 = data.get("b64_json")
            if b64:
                return base64.b64decode(b64), None
            url = data.get("url")
            if url:
                img_resp = await client.get(url)
                img_resp.raise_for_status()
                return img_resp.content, None
            return None, "proxy returned no image data (no b64_json or url)"
    except httpx.HTTPStatusError as exc:
        return None, _http_error_detail("image generation", exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("media: proxy image generation failed", exc_info=True)
        return None, f"image generation failed: {exc}"


async def _image_generate_handler(args: dict) -> dict:
    """MCP handler for ``media__image_generate``.

    The REAL path is the caller passing a catalog model id in ``args['model']``
    (the picker / studio skill selects one). Routes through the LiteLLM proxy's
    ``/v1/images/generations``; ``aspect_ratio`` is mapped to ``size`` (an
    explicit ``size`` wins) so the bundled skill's aspect hint is honoured rather
    than silently dropped. Saves the PNG under get_config_dir()/generated, then
    lands it in the STUDIO gallery. When no model is passed it falls back to a
    known-served default, then ``settings.image_model``. Sets ``is_error`` when
    identity is missing or the proxy call fails.
    """
    workspace_id, user_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "image_generate requires workspace and user context (call from a cloud chat session)."
        )

    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _error_response("image_generate requires a non-empty `prompt`.")
    size = _resolve_size(args)

    model = _str_arg(args, "model")
    if not model:
        model = _default_image_model()

    auth_key = await _resolve_auth_key(workspace_id)
    image_bytes, err = await _proxy_image(
        model=model, prompt=prompt, size=size, user=workspace_id, auth_key=auth_key
    )
    if err is not None or image_bytes is None:
        return _error_response(err or "image generation returned no data")

    try:
        out_path = _generated_dir() / f"{uuid.uuid4()}.png"
        out_path.write_bytes(image_bytes)
        logger.info("media: generated image %s via proxy model %s", out_path, model)
    except Exception as exc:  # noqa: BLE001
        logger.warning("media: writing generated image failed", exc_info=True)
        return _error_response(f"could not save the generated image: {exc}")

    result, err = await _land_in_gallery(
        workspace_id=workspace_id,
        user_id=user_id,
        kind="image",
        src=str(out_path),
        prompt=prompt,
    )
    if err is not None or result is None:
        return _error_response(err or "could not add the image to the gallery")

    return _success_response(
        {
            "ok": True,
            "kind": "image",
            "model": model,
            "path": str(out_path),
            "pocket_id": result["pocket_id"],
            "gallery_count": result["count"],
        }
    )


# ── Audio TTS (POST {proxy}/v1/audio/speech) ─────────────────────────────────


async def _proxy_audio_speech(
    *,
    model: str,
    text: str,
    voice: str,
    response_format: str,
    user: str,
    auth_key: str | None = None,
) -> tuple[bytes | None, str | None]:
    """Synthesize speech via the proxy's OpenAI-compatible speech endpoint.

    POSTs ``{model, input, voice, response_format, user}`` to
    ``/v1/audio/speech``; the body is the raw audio bytes (not JSON). Returns
    ``(bytes, None)`` or ``(None, error)``.
    """
    base = _proxy_base()
    payload = {
        "model": model,
        "input": text,
        "voice": voice,
        "response_format": response_format,
        "user": user,
    }
    try:
        async with _proxy_client() as client:
            resp = await client.post(
                f"{base}/v1/audio/speech",
                headers=_proxy_headers(auth_key=auth_key),
                json=payload,
            )
            resp.raise_for_status()
            return resp.content, None
    except httpx.HTTPStatusError as exc:
        return None, _http_error_detail("audio synthesis", exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("media: proxy audio synthesis failed", exc_info=True)
        return None, f"audio synthesis failed: {exc}"


async def _audio_generate_handler(args: dict) -> dict:
    """MCP handler for ``media__audio_generate`` (text-to-speech).

    Routes through ``/v1/audio/speech`` with the catalog model id from
    ``args['model']`` (default ``tts-1``), saves the audio under
    get_config_dir()/generated, then lands it in the STUDIO gallery as an audio
    tile. Sets ``is_error`` when identity is missing or the proxy call fails.
    """
    workspace_id, user_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "audio_generate requires workspace and user context (call from a cloud chat session)."
        )

    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        return _error_response("audio_generate requires non-empty `text` to speak.")
    voice = _str_arg(args, "voice", "alloy")
    response_format = _str_arg(args, "response_format", "mp3")
    model = _str_arg(args, "model", _DEFAULT_AUDIO_TTS_MODEL)

    auth_key = await _resolve_auth_key(workspace_id)
    audio_bytes, err = await _proxy_audio_speech(
        model=model,
        text=text,
        voice=voice,
        response_format=response_format,
        user=workspace_id,
        auth_key=auth_key,
    )
    if err is not None or audio_bytes is None:
        return _error_response(err or "audio synthesis returned no data")

    try:
        out_path = _generated_dir() / f"{uuid.uuid4()}.{response_format}"
        out_path.write_bytes(audio_bytes)
        logger.info("media: generated audio %s via proxy model %s", out_path, model)
    except Exception as exc:  # noqa: BLE001
        logger.warning("media: writing generated audio failed", exc_info=True)
        return _error_response(f"could not save the generated audio: {exc}")

    result, err = await _land_in_gallery(
        workspace_id=workspace_id,
        user_id=user_id,
        kind="audio",
        src=str(out_path),
        prompt=text,
    )
    if err is not None or result is None:
        return _error_response(err or "could not add the audio to the gallery")

    return _success_response(
        {
            "ok": True,
            "kind": "audio",
            "model": model,
            "path": str(out_path),
            "pocket_id": result["pocket_id"],
            "gallery_count": result["count"],
        }
    )


# ── Audio STT (POST {proxy}/v1/audio/transcriptions) ─────────────────────────


async def _proxy_audio_transcription(
    *, model: str, audio_path: Path, user: str, auth_key: str | None = None
) -> tuple[str | None, str | None]:
    """Transcribe an audio file via the proxy's OpenAI-compatible transcription
    endpoint.

    POSTs a multipart upload (``file`` + ``model`` + ``user``) to
    ``/v1/audio/transcriptions`` and returns ``body['text']``. httpx sets the
    multipart boundary, so the JSON content-type header is NOT sent. Returns
    ``(text, None)`` or ``(None, error)``.
    """
    base = _proxy_base()
    try:
        data = audio_path.read_bytes()
    except OSError as exc:
        return None, f"could not read audio file {audio_path}: {exc}"
    try:
        async with _proxy_client() as client:
            resp = await client.post(
                f"{base}/v1/audio/transcriptions",
                headers=_proxy_headers(json_content=False, auth_key=auth_key),
                files={"file": (audio_path.name, data)},
                data={"model": model, "user": user},
            )
            resp.raise_for_status()
            body = resp.json()
            text = body.get("text")
            if not isinstance(text, str):
                return None, "transcription returned no text"
            return text, None
    except httpx.HTTPStatusError as exc:
        return None, _http_error_detail("audio transcription", exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("media: proxy audio transcription failed", exc_info=True)
        return None, f"audio transcription failed: {exc}"


async def _audio_transcribe_handler(args: dict) -> dict:
    """MCP handler for ``media__audio_transcribe`` (speech-to-text).

    Routes through ``/v1/audio/transcriptions`` with the catalog model id from
    ``args['model']`` (default ``whisper-1``). Transcription is an INPUT op (not a
    generated asset), so it returns the text directly and does NOT touch the
    gallery. ``args['path']`` is a local audio file path. Sets ``is_error`` when
    identity is missing, the path is absent, or the proxy call fails.
    """
    workspace_id, user_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "audio_transcribe requires workspace and user context (call from a cloud chat session)."
        )

    path = args.get("path")
    if not isinstance(path, str) or not path.strip():
        return _error_response("audio_transcribe requires a `path` to a local audio file.")
    audio_path = Path(path)
    if not audio_path.is_file():
        return _error_response(f"audio file not found: {path}")

    model = _str_arg(args, "model", _DEFAULT_AUDIO_STT_MODEL)

    auth_key = await _resolve_auth_key(workspace_id)
    text, err = await _proxy_audio_transcription(
        model=model, audio_path=audio_path, user=workspace_id, auth_key=auth_key
    )
    if err is not None or text is None:
        return _error_response(err or "transcription returned no text")

    return _success_response({"ok": True, "kind": "transcription", "model": model, "text": text})


# ── Video (POST {proxy}/videos) ──────────────────────────────────────────────


async def _poll_proxy_video(
    client: httpx.AsyncClient, base: str, job_id: str, *, auth_key: str | None = None
) -> dict[str, Any]:
    """Poll ``GET {proxy}/videos/{job_id}`` until the job reaches a terminal
    status (``completed`` / ``succeeded`` / ``failed`` / ``cancelled``), bounded
    by ``_VIDEO_POLL_MAX_SECONDS``. Returns the final job dict (caller inspects
    ``status`` / ``data`` / ``error``)."""
    waited = 0.0
    job: dict[str, Any] = {}
    while waited < _VIDEO_POLL_MAX_SECONDS:
        resp = await client.get(
            f"{base}/videos/{job_id}", headers=_proxy_headers(auth_key=auth_key)
        )
        resp.raise_for_status()
        job = resp.json()
        status = (job.get("status") or "").lower()
        if status in ("completed", "succeeded", "failed", "cancelled", "canceled", "error"):
            return job
        await asyncio.sleep(_VIDEO_POLL_INTERVAL_SECONDS)
        waited += _VIDEO_POLL_INTERVAL_SECONDS
    job["status"] = job.get("status") or "timed_out"
    return job


def _extract_video_url(job: dict[str, Any]) -> str | None:
    """Pull the first output URL out of a (terminal) video job. LiteLLM/OpenAI
    video jobs vary in shape; check the common fields: ``url`` / ``output`` /
    ``data[].url``."""
    if isinstance(job.get("url"), str):
        return job["url"]
    output = job.get("output")
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        url = next((u for u in output if isinstance(u, str)), None)
        if url:
            return url
    data = job.get("data")
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("url"), str):
                return item["url"]
    return None


async def _proxy_video(
    *,
    model: str,
    prompt: str,
    seconds: int,
    size: str | None,
    aspect_ratio: str | None,
    user: str,
    auth_key: str | None = None,
) -> tuple[str | None, str | None]:
    """Generate a video via the proxy's ``/videos`` endpoint.

    POSTs ``{model, prompt, seconds, size?, aspect_ratio?, user}``; the response
    is an async job (or, for a fast provider, an already-terminal job). If the job
    is pending it is GET-polled to terminal. Both ``size`` and ``aspect_ratio`` are
    sent when present — different video models accept different shape params and
    LiteLLM passes through what the model takes / ignores the rest. Returns the
    output URL or an error.
    """
    base = _proxy_base()
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "seconds": seconds,
        "user": user,
    }
    if size:
        payload["size"] = size
    if aspect_ratio:
        payload["aspect_ratio"] = aspect_ratio
    try:
        async with _proxy_client(timeout=_VIDEO_POLL_MAX_SECONDS + 30.0) as client:
            resp = await client.post(
                f"{base}/videos",
                headers=_proxy_headers(auth_key=auth_key),
                json=payload,
            )
            resp.raise_for_status()
            job = resp.json()
            status = (job.get("status") or "").lower()
            terminal = {"completed", "succeeded", "failed", "cancelled", "canceled", "error"}
            job_id = job.get("id")
            if status not in terminal and job_id:
                job = await _poll_proxy_video(client, base, str(job_id), auth_key=auth_key)
            status = (job.get("status") or "").lower()
            if status not in ("completed", "succeeded"):
                detail = job.get("error") or status or "unknown status"
                return None, f"video generation did not succeed: {detail}"
            url = _extract_video_url(job)
            if not url:
                return None, "video generation succeeded but returned no output URL"
            return url, None
    except httpx.HTTPStatusError as exc:
        return None, _http_error_detail("video generation", exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("media: proxy video generation failed", exc_info=True)
        return None, f"video generation failed: {exc}"


async def _video_generate_handler(args: dict) -> dict:
    """MCP handler for ``media__video_generate``.

    The REAL path is the caller passing a catalog model id in ``args['model']``.
    Routes through the LiteLLM proxy's ``/videos`` endpoint; ``aspect_ratio`` is
    both mapped to ``size`` and passed through (LiteLLM ignores params a model
    doesn't take). When no model is passed it defaults to a proxy-style id — it
    does NOT forward the legacy ``settings.video_model`` Replicate slug, which the
    proxy won't serve. Lands the resulting output URL in the STUDIO gallery. Sets
    ``is_error`` when identity is missing or the proxy call fails / does not
    succeed.
    """
    workspace_id, user_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "video_generate requires workspace and user context (call from a cloud chat session)."
        )

    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _error_response("video_generate requires a non-empty `prompt`.")
    duration = args.get("duration") if isinstance(args.get("duration"), int) else 5
    size = _resolve_size(args)
    aspect_ratio = _str_arg(args, "aspect_ratio")

    model = _str_arg(args, "model") or _default_video_model()

    auth_key = await _resolve_auth_key(workspace_id)
    output_url, err = await _proxy_video(
        model=model,
        prompt=prompt,
        seconds=duration,
        size=size,
        aspect_ratio=aspect_ratio,
        user=workspace_id,
        auth_key=auth_key,
    )
    if err is not None or output_url is None:
        return _error_response(err or "video generation returned no output URL")

    result, err = await _land_in_gallery(
        workspace_id=workspace_id,
        user_id=user_id,
        kind="video",
        src=output_url,
        prompt=prompt,
    )
    if err is not None or result is None:
        return _error_response(err or "could not add the video to the gallery")

    return _success_response(
        {
            "ok": True,
            "kind": "video",
            "model": model,
            "url": output_url,
            "pocket_id": result["pocket_id"],
            "gallery_count": result["count"],
        }
    )


def _http_error_detail(what: str, exc: httpx.HTTPStatusError) -> str:
    """A compact, user-relayable message for a proxy HTTP error. Surfaces the
    status code and, when the proxy returned a JSON error body, its message —
    so a missing-model / no-quota / bad-key proxy response is legible rather than
    a bare 4xx/5xx."""
    status = exc.response.status_code
    detail: str | None = None
    try:
        body = exc.response.json()
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                detail = err.get("message")
            elif isinstance(err, str):
                detail = err
            detail = detail or body.get("message")
    except Exception:  # noqa: BLE001
        detail = None
    return f"{what} request failed: {status}" + (f" — {detail}" if detail else "")


def build_media_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for STUDIO media generation, or return
    ``None`` if the Claude Agent SDK isn't installed.

    Matches the shape returned by ``build_sites_manager_server`` (``(name,
    server)`` or ``None``) so the backend's MCP registration loop treats it
    identically.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_media MCP disabled")
        return None

    @tool(
        "image_generate",
        (
            "Generate an IMAGE from a text prompt (routed through the model "
            "gateway) and add it to the user's /studio gallery. Use this on the "
            "STUDIO surface when the user describes an image / poster / "
            "illustration to create. Args: `prompt` (required — describe the "
            "image), optional `model` (a catalog image-model id, e.g. "
            "'openai/gpt-image-1' or 'google/imagen-4'; omit to use the "
            "deployment default) and `size` (e.g. '1024x1024'). Returns {ok, "
            "kind:'image', model, path, pocket_id, gallery_count}; the image is "
            "laid out as a tile in the gallery pocket and the canvas opens "
            "automatically. ok=false with an error means relay the reason — do "
            "NOT claim a phantom image."
        ),
        {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Text description of the image to generate.",
                },
                "model": {
                    "type": "string",
                    "description": "Catalog image-model id (optional; default = deployment model).",
                },
                "aspect_ratio": {
                    "type": "string",
                    "description": "Aspect ratio hint (e.g. '1:1', '16:9', '9:16').",
                },
                "size": {
                    "type": "string",
                    "description": "Output resolution (e.g. '1024x1024', '1792x1024').",
                },
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    )
    async def image_generate(args):  # type: ignore[no-untyped-def]
        return await _image_generate_handler(args)

    @tool(
        "audio_generate",
        (
            "Generate SPEECH AUDIO from text (text-to-speech, routed through the "
            "model gateway) and add it to the user's /studio gallery. Use this on "
            "the STUDIO surface when the user wants narration / a voiceover / "
            "spoken audio. Args: `text` (required — what to say), optional `model` "
            "(a catalog TTS model id, default 'tts-1'), `voice` (e.g. 'alloy', "
            "default 'alloy') and `response_format` ('mp3' default, 'wav', "
            "'opus'). Returns {ok, kind:'audio', model, path, pocket_id, "
            "gallery_count}. ok=false with an error means relay the reason — do "
            "NOT claim phantom audio."
        ),
        {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The text to synthesize into speech.",
                },
                "model": {
                    "type": "string",
                    "description": "Catalog TTS model id (optional; defaults to 'tts-1').",
                },
                "voice": {
                    "type": "string",
                    "description": "Voice name (optional; defaults to 'alloy').",
                },
                "response_format": {
                    "type": "string",
                    "description": "Audio format: 'mp3' (default), 'wav', 'opus'.",
                },
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    )
    async def audio_generate(args):  # type: ignore[no-untyped-def]
        return await _audio_generate_handler(args)

    @tool(
        "audio_transcribe",
        (
            "Transcribe a local AUDIO file to text (speech-to-text, routed "
            "through the model gateway). Use when the user has an audio file they "
            "want transcribed. Args: `path` (required — path to a local audio "
            "file) and optional `model` (a catalog STT model id, default "
            "'whisper-1'). Returns {ok, kind:'transcription', model, text}. This "
            "does NOT add anything to the gallery (it is text, not a generated "
            "asset). ok=false with an error means relay the reason."
        ),
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Local filesystem path to the audio file to transcribe.",
                },
                "model": {
                    "type": "string",
                    "description": "Catalog STT model id (optional; defaults to 'whisper-1').",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    )
    async def audio_transcribe(args):  # type: ignore[no-untyped-def]
        return await _audio_transcribe_handler(args)

    @tool(
        "video_generate",
        (
            "Generate a VIDEO from a text prompt (routed through the model "
            "gateway) and add it to the user's /studio gallery. Use this on the "
            "STUDIO surface when the user describes a short video / clip / "
            "animation to create. Args: `prompt` (required — describe the video), "
            "optional `model` (a catalog video-model id, e.g. 'openai/sora' or a "
            "Veo/Kling id; omit to use the deployment default), `duration` "
            "(seconds, default 5) and `size` (e.g. '1280x720'). Generation is "
            "async and may take a few minutes. Returns {ok, kind:'video', model, "
            "url, pocket_id, gallery_count}. ok=false with an error means relay "
            "the reason — do NOT claim a phantom video."
        ),
        {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Text description of the video to generate.",
                },
                "model": {
                    "type": "string",
                    "description": "Catalog video-model id (optional; default = deployment model).",
                },
                "duration": {
                    "type": "integer",
                    "description": "Clip length in seconds (default 5).",
                },
                "aspect_ratio": {
                    "type": "string",
                    "description": "Aspect ratio hint (e.g. '16:9', '9:16').",
                },
                "size": {
                    "type": "string",
                    "description": "Output resolution (e.g. '1280x720', '720x1280').",
                },
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    )
    async def video_generate(args):  # type: ignore[no-untyped-def]
        return await _video_generate_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="2.0.0",
        tools=[image_generate, audio_generate, audio_transcribe, video_generate],
    )
    return SERVER_NAME, server


__all__ = [
    "IMAGE_GENERATE_TOOL_ID",
    "AUDIO_GENERATE_TOOL_ID",
    "AUDIO_TRANSCRIBE_TOOL_ID",
    "VIDEO_GENERATE_TOOL_ID",
    "MEDIA_TOOL_IDS",
    "SERVER_NAME",
    "build_media_server",
]
