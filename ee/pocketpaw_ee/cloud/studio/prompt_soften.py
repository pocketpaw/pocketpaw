# ee/pocketpaw_ee/cloud/studio/prompt_soften.py — OUR-backend content moderation
# / prompt softening for the direct /studio generation paths.
#
# When an upstream media provider rejects a prompt ("content checker", NSFW /
# unsafe-content flags, stochastic "did not generate the expected output"
# hits), the generation ladder retries the SAME request with a REWRITTEN
# prompt. The rewrite is produced by OUR OWN LLM through our own LiteLLM
# gateway (`/v1/chat/completions`) — deliberately NOT by any provider-side
# moderation endpoint (fal has none worth using, and moderation policy is ours
# to own). The gateway fronts whatever chat models the deployment already
# serves, so softening works everywhere the studio works.
#
# Two pieces, ported from the openstory project's battle-tested implementation
# (src/lib/ai/content-rejection.ts + its "phase/soften-image-prompt-chat"
# workflow prompt), adapted to free-form studio prompts:
#
#   * ``is_content_rejection`` — anchored regexes over the provider error so an
#     unrelated transient failure (timeout, 5xx, network) is NEVER misclassified
#     as a content hit and silently rewritten away.
#   * ``run_with_softening`` — the retry ladder: run → classify → soften →
#     retry, up to ``max_softens`` rewrites, returning a provenance dict
#     ({softened, originalPrompt}) the caller persists alongside the result.
#
# The soften call itself is pure HTTP against our gateway; ``_chat_completion``
# is the seam tests monkeypatch (same convention as the fal_* modules'
# ``_run_fal``).
#
# Created 2026-08-26 (studio-content-softening): ported from openstory.

from __future__ import annotations

import logging
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx

from pocketpaw_ee.catalog import config as catalog_config
from pocketpaw_ee.catalog import service as catalog_service
from pocketpaw_ee.catalog.models import Modality

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ── Content-rejection classification ─────────────────────────────────────────
# Phrases that mark a generation error as a content-filter / model-rejection
# rather than an infrastructure fault. Matched case-insensitively against the
# provider message. Kept ANCHORED to observed provider wording (fal 422s,
# veo/kling/seedance/flux samples) so an unrelated transient error is never
# misclassified as a content rejection and silently retried away.

CONTENT_REJECTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"content checker",
        r"flagged by a content",
        r"did not generate the expected output",
        r"could not generate images?",
        r"unexpected (?:result|output)",
        r"unsafe content",
        r"sensitive content",
        r"content could not be processed",
        r"content (?:filter|policy|moderation)",
        r"\bnsfw\b",
    )
)


def is_content_rejection(message: str) -> bool:
    """True when a provider error message looks like a content-filter /
    model-rejection hit (not an infrastructure fault)."""
    text = message or ""
    return any(pattern.search(text) for pattern in CONTENT_REJECTION_PATTERNS)


# ── The softening prompt (ported from openstory, adapted to studio prompts) ──

SOFTEN_SYSTEM_PROMPT = """You rewrite a cinematic media-generation prompt that an image/video model rejected, so a retry can succeed. Read <REJECTION> and pick the rewrite that matches it.

Two rejection classes:
- POLICY — content checker / NSFW / unsafe / sensitive / flagged. Soften graphic violence, gore, sexual/nude wording, self-harm, real-person likeness instructions, and explicit crime into cinematic implication (aftermath, tension, silhouette, tasteful coverage). A name that identifies a real person or a well-known franchise / trademarked character trips likeness and IP checks on its own: drop the name and describe the look generically (age, build, hair, wardrobe, demeanour) — never name the franchise.
- UNEXPECTED OUTPUT — "did not generate the expected output", "could not generate images", "unexpected result". The model often rejects its own sample because the prompt's grammar is broken or it stacks unusual word combinations. Rewrite into plain, grammatical cinematic English: short clauses, common collocations, no jammed modifiers or contradictory descriptors. Do not invent safer-sounding plot; the scene stays the same.

### CRITICAL OUTPUT RULES
1. Return one rewritten prompt and nothing else — plain text, no headers, bullets, quotation marks around it, or commentary.
2. Keep the same scene: subjects, setting, camera, lighting, wardrobe, mood, and style. Do not add new characters, props, locations, text, logos, or plot.
3. Preserve every explicit parameter the user stated (aspect ratio hints, lens, era, palette) verbatim where possible.
4. If the rejection is ambiguous, do both: clean the grammar AND soften any policy-risky wording.
5. Never return the original unchanged."""

SOFTEN_USER_TEMPLATE = """Rewrite this media prompt so the model will accept it.

<ORIGINAL_PROMPT>
{prompt}
</ORIGINAL_PROMPT>

<REJECTION>
{rejection}
</REJECTION>"""

# Env var that pins the softening chat model; unset → the first CHAT-capable
# model the deployment's own gateway serves is used.
SOFTEN_MODEL_ENV = "POCKETPAW_STUDIO_SOFTEN_MODEL"

_CHAT_TIMEOUT = 30.0

# Tests install an httpx.MockTransport here to exercise the gateway wire shape
# without a live proxy (the same seam studio.service exposes).
_TRANSPORT: httpx.BaseTransport | None = None


class SoftenUnavailableError(Exception):
    """No softening model is available on our gateway (nothing chat-capable in
    the catalog and no env override). Callers surface the ORIGINAL rejection."""


async def resolve_soften_model() -> str | None:
    """Pick which of OUR OWN models performs the rewrite.

    An explicit env override wins; otherwise the first chat-capable entry in
    the deployment's LiteLLM catalog serves (best-effort — an unreachable
    catalog just means softening is unavailable this round)."""
    override = (os.environ.get(SOFTEN_MODEL_ENV) or "").strip()
    if override:
        return override
    try:
        entries = await catalog_service.list_models()
    except Exception:  # noqa: BLE001 — catalog outage must not break softening resolution
        logger.warning("studio: soften model lookup failed (catalog unreachable)", exc_info=True)
        return None
    for entry in entries:
        if entry.modality == Modality.CHAT:
            return entry.id
    return None


async def _chat_completion(*, model: str, system: str, user: str) -> str | None:
    """One chat completion against OUR OWN LiteLLM gateway. Returns the message
    content, or None when the gateway fails/malforms (caller keeps the original
    rejection rather than failing twice)."""
    base = catalog_config.litellm_proxy_url().rstrip("/")
    headers: dict[str, str] = {"Content-Type": "application/json"}
    key = catalog_config.litellm_proxy_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
    }
    try:
        async with httpx.AsyncClient(transport=_TRANSPORT, timeout=_CHAT_TIMEOUT) as client:
            resp = await client.post(f"{base}/v1/chat/completions", headers=headers, json=payload)
            resp.raise_for_status()
            body = resp.json()
        content = body["choices"][0]["message"]["content"]
        return content if isinstance(content, str) and content.strip() else None
    except Exception:  # noqa: BLE001 — softening must degrade, never double-fail a render
        logger.warning("studio: soften completion failed via %s", model, exc_info=True)
        return None


def _clean_rewrite(content: str) -> str | None:
    """Strip the wrapper junk LLMs add (code fences, surrounding quotes) from a
    rewrite; empty results mean the model gave us nothing usable."""
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith(("text\n", "prompt\n")):
            text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    return text or None


async def soften_prompt(
    *,
    prompt: str,
    rejection: str,
    model: str | None = None,
) -> str | None:
    """Rewrite one rejected prompt via our own gateway LLM. Returns the cleaned
    rewrite, or None when softening is unavailable (no model / gateway error /
    empty answer) — callers then surface the ORIGINAL failure."""
    chosen = (model or "").strip() or await resolve_soften_model()
    if not chosen:
        logger.warning("studio: no soften model available (set %s)", SOFTEN_MODEL_ENV)
        return None
    content = await _chat_completion(
        model=chosen,
        system=SOFTEN_SYSTEM_PROMPT,
        user=SOFTEN_USER_TEMPLATE.format(prompt=prompt, rejection=rejection),
    )
    if content is None:
        return None
    return _clean_rewrite(content)


async def run_with_softening(
    run_fn: Callable[[str], Awaitable[T]],
    *,
    prompt: str,
    max_softens: int = 2,
) -> tuple[T, dict[str, Any]]:
    """Run one generation with the soften-on-rejection ladder wrapped around it.

    ``run_fn(prompt_text)`` performs the actual upstream call (fal image /
    fal video / proxy generation) and may raise anything. On a classified
    CONTENT rejection the prompt is rewritten via :func:`soften_prompt` and the
    call retried — same model, same params, softer words — up to
    ``max_softens`` times. Any NON-content failure, an exhausted budget, or a
    round where softening is unavailable/no-progress re-raises the LAST
    original exception untouched.

    Returns ``(result, provenance)`` where provenance carries what the caller
    should persist next to the asset::

        {"softened": True|False, "originalPrompt": "<only when softened>"}
    """
    current = prompt
    softened = False
    last_exc: Exception | None = None
    for attempt in range(max_softens + 1):
        try:
            result = await run_fn(current)
            return result, {
                "softened": softened,
                "originalPrompt": prompt if softened else None,
            }
        except Exception as exc:  # noqa: BLE001 — classified below, re-raised untouched otherwise
            last_exc = exc
            if not is_content_rejection(str(exc)):
                raise
            if attempt == max_softens:
                raise
            rewrite = await soften_prompt(prompt=prompt, rejection=str(exc))
            if rewrite is None or rewrite.strip() == current.strip():
                raise
            logger.info(
                "studio: content rejection — retrying with softened prompt (attempt %d/%d)",
                attempt + 1,
                max_softens,
            )
            current = rewrite
            softened = True
    assert last_exc is not None  # pragma: no cover — loop always returns or raises
    raise last_exc


__all__ = [
    "CONTENT_REJECTION_PATTERNS",
    "SOFTEN_SYSTEM_PROMPT",
    "SOFTEN_MODEL_ENV",
    "SoftenUnavailableError",
    "is_content_rejection",
    "resolve_soften_model",
    "soften_prompt",
    "run_with_softening",
]
