# ee/pocketpaw_ee/cloud/other_hand/illustrate.py — generate an illustration and
# hand it back as pen strokes.
#
# Created 2026-08-28 (feat/other-hand-vector-illustration).
#
# Recraft's text-to-vector endpoint returns real SVG, which is why it is the
# right generator for this surface: ``svg_to_ink`` turns that into the page's
# own ``path`` ops, so the result is INK — one pen, erasable, it scrolls with
# the page and counts toward free_y. A raster generator would have given a
# picture pasted onto a handwriting page.
#
# TWO THINGS GATE EVERY CALL, and they are separate on purpose:
#
#   1. A key. No FAL_AI_API_KEY, no illustration — and the turn continues
#      without one rather than failing.
#   2. The user asking. Each generation costs real money (Recraft v4 pro is
#      $0.30/image at time of writing) and — unlike LLM tokens — BYOK does NOT
#      cover it: a user's own Anthropic key pays for their turns while every
#      illustration bills the platform. So this is opt-in PER TURN and the
#      agent may not reach for it on its own. See ``allowed`` below.
#
# The gate is enforced HERE rather than only in the prompt, because a prompt
# instruction is a request and this one has a bill attached.

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from pocketpaw_ee.cloud.other_hand.svg_to_ink import Box, SvgConvertError, svg_to_ops

logger = logging.getLogger(__name__)

# Recraft v4 text-to-vector — NOT the "pro" tier. Both return image/svg+xml
# with the same input schema, and the pro tier costs $0.30 an image against
# $0.08 for this one (fal pricing, checked 2026-08-28). The page converts every
# path to a single-weight pen stroke and throws the fills away, so most of what
# the premium tier charges for — colour judgement, fill quality, brand polish —
# is discarded before the reader ever sees it. We are paying for the line work,
# and the cheaper tier draws the same lines.
#
# Overridable per deployment so the tier can be tuned without a code change.
_ENV_MODEL = "POCKETPAW_OTHER_HAND_ILLUSTRATION_MODEL"
DEFAULT_MODEL = "fal-ai/recraft/v4/text-to-vector"
#: The premium tier, kept named so switching back is a value, not a search.
PRO_MODEL = "fal-ai/recraft/v4/pro/text-to-vector"


def configured_model() -> str:
    """The endpoint this deployment generates with."""
    return (os.environ.get(_ENV_MODEL) or "").strip() or DEFAULT_MODEL


# Line art, because a pen has no fills: a filled illustration converts to its
# outlines and loses whatever the fills were carrying. Asking for the style we
# can actually draw beats converting one we cannot.
STYLE_SUFFIX = (
    "clean black line art, single-weight outlines, no fills, no shading, "
    "no background, no text or lettering"
)
_FETCH_TIMEOUT_S = 30.0
_MAX_SVG_BYTES = 4_000_000


class IllustrateError(RuntimeError):
    """The illustration could not be produced. Never fatal to a turn."""


def is_available(api_key: str | None) -> bool:
    """Whether this deployment can illustrate at all."""
    return bool(api_key)


async def _fetch_svg(url: str) -> str:
    """Download the generated SVG. Size-capped: the converter's point budget
    bounds what we DRAW, but an unbounded download is its own problem."""
    async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT_S) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        if len(resp.content) > _MAX_SVG_BYTES:
            raise IllustrateError("the generated SVG was implausibly large; refusing it")
        return resp.text


def _extract_svg_url(result: dict[str, Any]) -> str | None:
    """Pull the SVG URL out of the endpoint's response.

    Reads the documented ``images[]`` shape but does not insist on it — fal
    endpoints vary, and failing to find a URL should read as "no illustration",
    not as a crash inside a turn.
    """
    images = result.get("images")
    if isinstance(images, list):
        for entry in images:
            if isinstance(entry, dict) and isinstance(entry.get("url"), str):
                return entry["url"]
    single = result.get("image")
    if isinstance(single, dict) and isinstance(single.get("url"), str):
        return single["url"]
    return None


async def illustrate_as_ops(
    prompt: str,
    box: Box,
    *,
    api_key: str | None,
    allowed: bool,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Generate an illustration for ``prompt`` and return it as ``path`` ops.

    ``allowed`` is the per-turn opt-in. The guarantee is simple and is the one
    worth testing: not allowed means no generator call, so an unauthorised turn
    cannot be billed. (An earlier version of this docstring claimed the ORDER of
    the guards mattered too. It does not — every guard returns the same empty
    list — and a mutation run proved the claim untestable. Removed rather than
    left as a comment no test could defend.)

    Returns an empty list when it cannot illustrate. Callers should treat that
    as "answer without a picture": a missing illustration is a smaller failure
    than a failed turn, and the user asked a question, not for an image.
    """
    if not allowed:
        return []
    if not is_available(api_key):
        logger.info("other-hand: illustration requested but no fal key is configured")
        return []
    if not prompt.strip():
        return []

    try:
        import fal_client  # noqa: PLC0415 — lazy: optional runtime dep (EE pattern)
    except ImportError:
        logger.info("other-hand: fal-client is not installed; skipping illustration")
        return []

    endpoint = (model or "").strip() or configured_model()
    try:
        client = fal_client.AsyncClient(key=api_key)
        result = await client.run(
            endpoint,
            arguments={"prompt": f"{prompt.strip()}. {STYLE_SUFFIX}"},
        )
    except Exception as exc:  # noqa: BLE001 — a generator outage must not fail the turn
        logger.warning("other-hand: fal '%s' failed", endpoint, exc_info=True)
        raise IllustrateError(f"the illustrator did not respond: {exc}") from exc

    if not isinstance(result, dict):
        raise IllustrateError("the illustrator returned an unexpected result")
    url = _extract_svg_url(result)
    if not url:
        raise IllustrateError("the illustrator returned no image")

    svg = await _fetch_svg(url)
    try:
        return svg_to_ops(svg, box)
    except SvgConvertError as exc:
        raise IllustrateError(f"the illustration could not be drawn: {exc}") from exc
