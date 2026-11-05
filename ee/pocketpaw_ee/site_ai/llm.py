# ee/pocketpaw_ee/site_ai/llm.py — how one site AI call reaches a model.
#
# Through the workspace's own agent stack, never a client of our own: ``complete``
# and ``stream`` call ``cloud.chat.side_completion.agent_complete`` /
# ``agent_stream`` on the agent and model the owner mapped to the alias
# (``aliases.choice``), as the site owner, so the agent's backend, the per-send
# model check, BYOK, who pays and the credit gates are exactly a chat turn's. The
# call is ``isolated``: tools off, no soul recall, none of the agent's skills or
# backend extras, so the system prompt is exactly what ``render`` builds from the
# site's (or its preset's) system messages.
#
# ``render`` turns the OpenAI-shaped history into one delimited transcript
# (``<turn role="...">`` blocks inside ``<conversation>``; a turn's own text cannot
# open or close a block, the concierge ``_data`` rule). Image parts (``data:`` URLs
# validate.py already checked and re-encoded) become ``[image N]`` markers in the
# transcript and ride, in order, as the side completion's ``images``. ``response_format`` json
# adds a "reply with one JSON value" instruction (plus the schema) to the system
# prompt and ``json_text`` pulls that value out of the reply. ``max_tokens`` is
# advisory here (the backends take no output cap), so it rides as an instruction.
# Tests fake ``side_completion.agent_stream`` / ``agent_complete`` / ``resolve_target``.

from __future__ import annotations

import base64
import json
import re
from collections.abc import AsyncIterator
from typing import Any

TIMEOUT_S = 120.0

_TAG_RE = re.compile(r"<\s*/?\s*(conversation|turn)\b", re.IGNORECASE)
_JSON_RULE = (
    "Reply with one JSON value and nothing else: no prose, no code fences, no "
    "comments. The site's code parses your whole reply as JSON."
)


def _neutral(text: str) -> str:
    return _TAG_RE.sub(lambda m: "‹" + m.group(0)[1:], text or "")


def _text_of(content: Any, counter: list[int] | None = None) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if part.get("type") == "text":
            parts.append(part.get("text", ""))
        elif part.get("type") == "image_url" and counter is not None:
            counter[0] += 1
            parts.append(f"[image {counter[0]}]")
    return "\n".join(parts)


def images(messages: list[dict[str, Any]]) -> list[Any]:
    """The request's images, in transcript order, as the chat turn's
    ``ImageAttachment`` (raw bytes + media type)."""
    from pocketpaw.agents.backend import ImageAttachment

    out = []
    for msg in messages:
        content = msg.get("content")
        for part in content if isinstance(content, list) else ():
            if part.get("type") != "image_url":
                continue
            head, _, payload = part["image_url"]["url"].partition(",")
            media_type = head.removeprefix("data:").removesuffix(";base64")
            out.append(ImageAttachment(data=base64.b64decode(payload), media_type=media_type))
    return out


def render(
    messages: list[dict[str, Any]],
    *,
    response_format: dict[str, Any] | None = None,
    max_tokens: int | None = None,
) -> tuple[str, str]:
    """``(system, prompt)`` for one call. System messages (the preset's first) become
    the system prompt; every other turn goes, in order, into the transcript."""
    system_parts = [_text_of(m.get("content")) for m in messages if m["role"] == "system"]
    rules: list[str] = []
    if response_format and response_format.get("type") in ("json_object", "json_schema"):
        rules.append(_JSON_RULE)
        schema = (response_format.get("json_schema") or {}).get("schema")
        if schema is not None:
            rules.append(
                "The JSON must match this JSON Schema:\n"
                + json.dumps(schema, separators=(",", ":"), ensure_ascii=False)
            )
    if max_tokens:
        rules.append(f"Keep the reply under about {max_tokens} tokens.")
    system = "\n\n".join(p for p in [*system_parts, *rules] if p.strip())
    counter = [0]
    turns = [
        f'<turn role="{m["role"]}">\n{_neutral(_text_of(m.get("content"), counter))}\n</turn>'
        for m in messages
        if m["role"] != "system"
    ]
    prompt = (
        "<conversation>\n"
        + "\n".join(turns)
        + "\n</conversation>\n\nWrite the assistant's next turn: only its text, without "
        "a role tag."
    )
    return system, prompt


def json_text(text: str) -> str | None:
    """The JSON value in a reply (bare, fenced, or wrapped in prose), re-serialised;
    None when there is none."""
    raw = (text or "").strip()
    fence = re.match(r"^```[a-zA-Z]*\s*(.*?)\s*```$", raw, re.DOTALL)
    if fence:
        raw = fence.group(1).strip()
    candidates = [raw]
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = raw.find(opener), raw.rfind(closer)
        if start != -1 and end > start:
            candidates.append(raw[start : end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return None


def _kwargs(call: Any) -> dict[str, Any]:
    system, prompt = render(
        call.messages, response_format=call.response_format, max_tokens=call.max_tokens
    )
    return {
        "workspace_id": call.workspace_id,
        "user_id": call.owner_id,
        "system": system,
        "prompt": prompt,
        "agent_id": call.agent_id,
        "model": call.model,
        "timeout_s": TIMEOUT_S,
        "images": images(call.messages),
        "isolated": True,
    }


async def complete(call: Any):
    """One answer as a ``SideCompletion``. Never raises."""
    from pocketpaw_ee.cloud.chat import side_completion

    return await side_completion.agent_complete(**_kwargs(call))


async def stream(call: Any) -> AsyncIterator[Any]:
    """``STARTED``, text deltas (``str``), then one ``SideCompletion``. Never raises."""
    from pocketpaw_ee.cloud.chat import side_completion

    async for item in side_completion.agent_stream(**_kwargs(call)):
        yield item


def is_started(item: Any) -> bool:
    from pocketpaw_ee.cloud.chat import side_completion

    return item is side_completion.STARTED


__all__ = ["TIMEOUT_S", "complete", "is_started", "images", "json_text", "render", "stream"]
