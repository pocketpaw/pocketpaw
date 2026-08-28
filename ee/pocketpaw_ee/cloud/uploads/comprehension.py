# ee/pocketpaw_ee/cloud/uploads/comprehension.py — one model call that says what
# an uploaded file IS.
#
# Created 2026-08-28 (FC-2 "File comprehension").
#
# WHY THIS EXISTS. ``uploads/tagging.py`` ranks words by frequency, on purpose:
# FL-6's rule was "reuse what extraction already produced, never call a new
# external LLM". That buys a board deck the tags ``q3``, ``revenue``, ``slide``
# — every one of them true, and not one of them the answer to "what is this
# file". Frequency counting cannot produce "a board deck", because the phrase
# does not appear in the deck. Understanding what a document IS needs a model
# that has read documents. So this module is the deliberate exception to FL-6's
# no-LLM rule, and it is scoped to exactly that: one short call, one summary,
# up to three categories from a fixed list.
#
# THE VOCABULARY IS CONTROLLED AND ``tags`` STAYS FREE-FORM. They answer
# different questions. ``tags`` is "what words are in here" and benefits from
# being open; ``collections`` is "which shelf does this go on" and is worthless
# unless two files that belong together get the SAME value. A model asked for
# free-form shelf names returns "board deck", "board-deck", "Board Presentation"
# and "quarterly deck" for four copies of the same thing, and the filter that
# rides on it silently shows one of the four.
#
# THE BEARER, AND ONLY THE BEARER. This is a PLATFORM call: it runs on ingest,
# nobody asked for it, and the platform pays. The LiteLLM proxy in front of us
# runs with ``forward_llm_provider_auth_headers: true`` (shipped 2026-08-28 for
# BYOK), which means an ``x-api-key`` header on a request is FORWARDED UPSTREAM
# and the holder of that key is billed. Attaching one here — even by copying a
# header dict from the turn path — would quietly charge a user's own Anthropic
# account to summarise their own uploads. ``_headers()`` therefore builds its
# dict from nothing and carries exactly ``Authorization`` + ``Content-Type``,
# and a test asserts the absence, because "we just won't add it" is not a
# control.
#
# EVERY FAILURE IS ``None``. The caller's contract is fail-OPEN: the user asked
# to store a file, not to have it understood. A 404 model id, a dead proxy, a
# model that answers in prose instead of JSON, a category nobody has ever heard
# of — all of it collapses to ``None`` and the file stays indexed, tagged and
# usable. The only thing this module must never do is raise into an upload.

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

CATEGORIES: tuple[str, ...] = (
    "contract",
    "invoice",
    "deck",
    "spec",
    "research",
    "design",
    "correspondence",
    "data",
    "media",
    "other",
)
"""The controlled vocabulary for ``collections``.

APPEND-ONLY. Removing or renaming a value STRANDS every row already carrying
it: the string stays in Mongo, the library filter stops offering it, and those
files quietly drop out of a shelf they are still on. Adding a value is free —
old rows simply never carry it. If a category turns out to be wrong, add the
right one and leave the wrong one in place; a migration that rewrites rows is a
deliberate, separate act, not a side effect of editing this tuple.

``other`` is here so the model has somewhere to put a file that genuinely is
none of the rest. Without it the pressure is to pick the nearest wrong shelf.
"""

MAX_CATEGORIES = 3
"""At most three shelves per file. A file on eight shelves is on none of them."""

MAX_INPUT_CHARS = 6000
"""How much extracted text the model sees.

A 400-page PDF extracts to megabytes. Sending it would cost more than the
feature is worth and, on a long document, tells the model nothing the first few
pages did not — what a file IS is established in its opening, not its
appendices. Truncation happens BEFORE the request is built, so the cap is a
property of the payload rather than a hope about the model's context window.
"""

MAX_SUMMARY_CHARS = 600
"""Hard ceiling on the returned summary. The prompt asks for one or two
sentences; this is the backstop for a model that ignores it and starts
narrating. Trimmed rather than rejected — a too-long summary is still useful."""

_ENV_MODEL = "POCKETPAW_FILE_COMPREHENSION_MODEL"

_FALLBACK_MODEL = "deepseek/deepseek-chat"
"""Last-resort model id when neither the env var nor ``settings.litellm_model``
names one.

A model id that the proxy does not serve is the WORST outcome available here:
every call 404s, every ``comprehend`` returns ``None``, the fail-open path
swallows it, and the feature looks installed while never once running. So the
resolution order below prefers, in turn, (1) what the operator set explicitly,
(2) what this deployment ALREADY talks to — ``settings.litellm_model`` is by
definition a group that resolves on this operator's own proxy — and only then
(3) this constant.

The constant is chosen from the evidence in-repo, not from a guess: the
2026-06-26 gateway probe (``docs/handoff/2026-06-26-mcg2-gateway-probe.md``)
recorded ``deepseek/deepseek-chat`` serving successfully through the proxy
while every ``claude-*``/``anthropic/*`` group returned 401 on a bad upstream
credential and the OpenAI group was quota-exhausted. That was two months ago
and may well have been fixed since; it is still the most recent DIRECT
observation this repo contains. Anyone with proxy access should confirm with
``GET {POCKETPAW_LITELLM_API_BASE}/v1/models`` and correct this line.
"""

_TIMEOUT_SECONDS = 30.0
"""One short completion. A slow proxy must not hold an ingest open for minutes;
the timeout expiring is just another ``None``."""

_PROXY_TRANSPORT: httpx.BaseTransport | None = None
"""Test seam, mirroring ``agent/mcp_servers/media.py``. Tests assign an
``httpx.MockTransport`` so the real request-building code runs — headers,
payload, truncation and all — without a live proxy. Production leaves it
``None`` and httpx uses the network."""


@dataclass(frozen=True)
class Comprehension:
    """What one model call understood about one file.

    Frozen because the caller merges it into existing row state; a mutable
    result invites "fix it up in place" at the call site, which is where the
    validation this module performs would get quietly undone.
    """

    summary: str
    categories: list[str]


def model_id() -> str:
    """Resolve the model this deployment should comprehend with.

    Order: the explicit env var, then whatever LiteLLM model group this
    deployment is already configured to talk to, then the fallback constant.
    See ``_FALLBACK_MODEL`` for why the middle step is worth the branch.
    """
    explicit = (os.environ.get(_ENV_MODEL) or "").strip()
    if explicit:
        return explicit
    try:
        from pocketpaw.config import get_settings

        configured = (get_settings().litellm_model or "").strip()
    except Exception:  # noqa: BLE001 — settings must never break an ingest
        configured = ""
    return configured or _FALLBACK_MODEL


def _proxy_base() -> str:
    """The LiteLLM proxy base URL, from catalog config — the same accessor the
    media MCP server uses, so there is one place a deployment points the proxy."""
    from pocketpaw_ee.catalog import config

    return config.litellm_proxy_url()


def _proxy_key() -> str | None:
    """The deployment's LiteLLM master/admin key, or None on a keyless proxy."""
    from pocketpaw_ee.catalog import config

    return config.litellm_proxy_api_key()


def _headers() -> dict[str, str]:
    """The complete header set for a PLATFORM proxy call.

    Built from an empty dict on purpose. The one header that must never appear
    is ``x-api-key``: the proxy forwards it upstream (BYOK header-forwarding),
    so setting it would bill whoever owns that key for a summary they never
    asked for. There is no code path here that could add it, and
    ``tests/cloud/uploads/test_comprehension.py`` asserts it stays that way.
    """
    headers = {"Content-Type": "application/json"}
    key = _proxy_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


_SYSTEM_PROMPT = (
    "You classify uploaded files for a document library. Given a file's name, "
    "type, and an excerpt of its contents, say what the file IS — not what it "
    "mentions.\n\n"
    "Reply with JSON only, no prose and no code fence:\n"
    '{"summary": "<one or two sentences>", "categories": ["<category>", ...]}\n\n'
    f"categories must be chosen from exactly this list: {', '.join(CATEGORIES)}.\n"
    f'Use at most {MAX_CATEGORIES}, most fitting first. Use "other" when none '
    "of the rest fit rather than stretching one.\n"
    "The summary names the kind of document and what it covers, e.g. "
    '"A board deck reviewing Q3 revenue and the 2027 hiring plan." Never begin '
    'with "This file" or "This document".'
)


def _truncate(text: str | None) -> str:
    """Cap the extracted text at :data:`MAX_INPUT_CHARS`, from the front."""
    if not text:
        return ""
    stripped = text.strip()
    if len(stripped) <= MAX_INPUT_CHARS:
        return stripped
    return stripped[:MAX_INPUT_CHARS]


def _build_user_message(
    *, title: str | None, text: str | None, captions: list[str] | None, mime: str
) -> str:
    """Assemble the one user turn. Captions carry the only signal an image has."""
    parts = [f"File type: {mime}"]
    if title:
        parts.append(f"Title: {title.strip()[:200]}")
    caption_blob = " ".join(c.strip() for c in (captions or []) if isinstance(c, str) and c.strip())
    if caption_blob:
        parts.append(f"Captions: {caption_blob[:1000]}")
    excerpt = _truncate(text)
    if excerpt:
        parts.append(f"Contents excerpt:\n{excerpt}")
    return "\n\n".join(parts)


def _parse_json_object(raw: str) -> dict[str, Any] | None:
    """Pull a JSON object out of a model reply, tolerating a code fence.

    Models fence JSON even when told not to. Rather than fail a whole
    comprehension on a pair of backticks, slice from the first ``{`` to the
    last ``}`` and parse that. Anything still unparseable is ``None``.
    """
    if not raw:
        return None
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def validate_categories(raw: Any) -> list[str]:
    """Keep only real categories, deduped, in order, capped at the maximum.

    DROP, never map. A category outside :data:`CATEGORIES` is worse than no
    category at all: it renders in the library as a shelf that looks like every
    other shelf, and the filter behind it matches one file forever. Guessing
    the nearest legal value would be worse still — a deck filed under
    ``contract`` because the model said "board contract" is a wrong answer
    presented with the same confidence as a right one.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        value = item.strip().lower()
        if value not in CATEGORIES or value in seen:
            continue
        seen.add(value)
        out.append(value)
        if len(out) >= MAX_CATEGORIES:
            break
    return out


async def comprehend(
    title: str | None,
    text: str | None,
    captions: list[str] | None,
    *,
    mime: str,
) -> Comprehension | None:
    """Ask the proxy what this file is. ``None`` on anything less than success.

    ``None`` covers: nothing to send, a proxy that is down or slow, a non-2xx
    response, a reply that is not JSON, a reply with no usable summary. The
    caller treats every one of them identically and leaves the file alone, so
    there is nothing to gain from distinguishing them beyond the log line.
    """
    user_message = _build_user_message(title=title, text=text, captions=captions, mime=mime)
    # Nothing but the mime line means there is no signal to classify on. The
    # model would confidently invent one; skip the spend instead.
    if not (title or (text or "").strip() or captions):
        return None

    payload = {
        "model": model_id(),
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        "max_tokens": 400,
        "temperature": 0,
    }

    base = _proxy_base().rstrip("/")
    try:
        async with httpx.AsyncClient(
            transport=_PROXY_TRANSPORT, timeout=_TIMEOUT_SECONDS
        ) as client:
            resp = await client.post(
                f"{base}/v1/chat/completions",
                headers=_headers(),
                json=payload,
            )
            resp.raise_for_status()
            body = resp.json()
    except Exception:  # noqa: BLE001 — every failure is the same failure here
        logger.info(
            "file comprehension call failed (model=%r); the file stays indexed "
            "and tagged, it just carries no summary",
            payload["model"],
            exc_info=True,
        )
        return None

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.info("file comprehension: proxy returned an unexpected body shape")
        return None

    parsed = _parse_json_object(content if isinstance(content, str) else "")
    if parsed is None:
        logger.info("file comprehension: model reply was not JSON; skipping")
        return None

    summary = parsed.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        # Categories without a summary is not worth a write — the summary is
        # the part a person reads.
        return None
    summary = summary.strip()[:MAX_SUMMARY_CHARS]

    return Comprehension(summary=summary, categories=validate_categories(parsed.get("categories")))


__all__ = [
    "CATEGORIES",
    "MAX_CATEGORIES",
    "MAX_INPUT_CHARS",
    "MAX_SUMMARY_CHARS",
    "Comprehension",
    "comprehend",
    "model_id",
    "validate_categories",
]
