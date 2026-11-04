# ee/pocketpaw_ee/site_ai/validate.py — parse and clamp one ``/ai/chat`` request.
#
# The body is untrusted JSON from a site (its Worker, or a visitor's browser). We
# never forward it: ``parse_chat`` rebuilds every message, tool and option from an
# allowlist, so an unknown key, a provider-specific field or a spoofed ``user``
# cannot reach LiteLLM. Limits (the pinned contract):
#   * body <= 400 KB (``read_body`` streams it and stops at the cap) -> 413
#   * <= 200 messages, <= 64 tools, <= 8 images, input tokens <= the alias context
#   * images only on vision aliases, in user messages. ``data:`` URLs must be
#     png/jpeg/gif/webp, are decoded strictly, re-checked by magic bytes against
#     the declared type, capped at 5 MB and re-encoded. Remote image URLs are
#     refused: LiteLLM would fetch them itself (following redirects), which is an
#     SSRF path from a visitor's request into our network. Callers inline images.
# ``estimate_tokens`` is an upper bound on input tokens: UTF-8 bytes of the text
# (no tokenizer yields more tokens than bytes), plus fixed overheads per message,
# per tool and for a response schema, and ``IMAGE_TOKENS`` per image (above any
# supported provider's per-image maximum). It sizes both the budget hold and the
# context check.

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request

from pocketpaw_ee.site_ai.aliases import ALIASES, CHAT_ALIASES
from pocketpaw_ee.site_ai.errors import AiError, invalid

MAX_BODY_BYTES = 400 * 1024
MAX_MESSAGES = 200
MAX_TOOLS = 64
MAX_IMAGES = 8
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_USER_CHARS = 256
IMAGE_TOKENS = 6_000
MESSAGE_OVERHEAD_TOKENS = 16
TOOL_OVERHEAD_TOKENS = 64
SCHEMA_OVERHEAD_TOKENS = 64

ROLES = ("system", "user", "assistant", "tool")
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DATA_URL_RE = re.compile(r"^data:(image/(?:png|jpeg|gif|webp));base64,(.*)$", re.DOTALL)


async def read_body(request: Request, limit: int = MAX_BODY_BYTES) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise AiError("request_too_large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise AiError("request_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def load_json(raw: bytes) -> dict[str, Any]:
    try:
        body = json.loads(raw) if raw.strip() else None
    except (ValueError, RecursionError):
        raise invalid("body", "The request body must be JSON.") from None
    if not isinstance(body, dict):
        raise invalid("body", "The request body must be a JSON object.")
    return body


@dataclass
class ParsedChat:
    model: str
    messages: list[dict[str, Any]]
    preset: str | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    tool_choice: Any = None
    response_format: dict[str, Any] | None = None
    max_output_tokens: int | None = None
    temperature: float | None = None
    stream: bool = True
    user: str | None = None
    images: int = 0


# ------------------------------------------------------------------ images


def _sniff(raw: bytes) -> str | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def check_image_url(url: Any, where: str) -> str:
    if not isinstance(url, str) or not url:
        raise invalid(where, "An image needs a URL.")
    m = _DATA_URL_RE.match(url)
    if m:
        declared, payload = m.group(1), re.sub(r"\s+", "", m.group(2))
        if len(payload) > (MAX_IMAGE_BYTES * 4) // 3 + 4:
            raise AiError("request_too_large", "An image is larger than 5 MB.")
        try:
            raw = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError):
            raise invalid(where, "An image isn't valid base64.") from None
        if len(raw) > MAX_IMAGE_BYTES:
            raise AiError("request_too_large", "An image is larger than 5 MB.")
        if _sniff(raw) != declared:
            raise invalid(where, "An image's content doesn't match its type.")
        return f"data:{declared};base64,{base64.b64encode(raw).decode('ascii')}"
    if url.startswith("data:"):
        raise invalid(where, "Only png, jpeg, gif or webp images are accepted.")
    raise invalid(where, "Send images inline as data: URLs; remote image URLs aren't fetched.")


# ------------------------------------------------------------------ messages


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise invalid(where, "Expected text.")
    return value


def _content(
    value: Any, role: str, where: str, vision: bool, counter: list[int]
) -> str | list[dict[str, Any]] | None:
    if value is None:
        if role == "assistant":
            return None
        raise invalid(where, "A message needs content.")
    if isinstance(value, str):
        return value
    if not isinstance(value, list) or not value:
        raise invalid(where, "Content must be text or a list of parts.")
    parts: list[dict[str, Any]] = []
    for i, part in enumerate(value):
        at = f"{where}[{i}]"
        if not isinstance(part, dict):
            raise invalid(at, "A content part must be an object.")
        kind = part.get("type")
        if kind == "text":
            parts.append({"type": "text", "text": _text(part.get("text"), at + ".text")})
        elif kind == "image_url":
            if role != "user":
                raise invalid(at, "Only user messages can carry images.")
            if not vision:
                raise AiError("model_not_allowed", "Images need the vision model.", field="model")
            counter[0] += 1
            if counter[0] > MAX_IMAGES:
                raise invalid(at, f"At most {MAX_IMAGES} images per request.")
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else None
            parts.append(
                {"type": "image_url", "image_url": {"url": check_image_url(url, at + ".url")}}
            )
        else:
            raise invalid(at, "Unknown content part type.")
    return parts


def _tool_calls(value: Any, where: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > MAX_TOOLS:
        raise invalid(where, "tool_calls must be a non-empty list.")
    out = []
    for i, call in enumerate(value):
        at = f"{where}[{i}]"
        fn = call.get("function") if isinstance(call, dict) else None
        if not isinstance(fn, dict) or call.get("type", "function") != "function":
            raise invalid(at, "A tool call must be a function call.")
        call_id = call.get("id")
        if not isinstance(call_id, str) or not 0 < len(call_id) <= 128:
            raise invalid(at + ".id", "A tool call needs an id.")
        name = fn.get("name")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise invalid(at + ".function.name", "A tool name must be 1-64 chars of A-Z0-9_-.")
        args = fn.get("arguments", "")
        if not isinstance(args, str):
            raise invalid(at + ".function.arguments", "Tool arguments must be a JSON string.")
        out.append(
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}
        )
    return out


def _message(raw: Any, i: int, vision: bool, counter: list[int]) -> dict[str, Any]:
    where = f"messages[{i}]"
    if not isinstance(raw, dict):
        raise invalid(where, "A message must be an object.")
    role = raw.get("role")
    if role not in ROLES:
        raise invalid(where + ".role", "Unknown role.")
    msg: dict[str, Any] = {"role": role}
    content = _content(raw.get("content"), role, where + ".content", vision, counter)
    if role in ("system", "tool") and isinstance(content, list):
        if any(p["type"] != "text" for p in content):
            raise invalid(where + ".content", "Only text here.")
    msg["content"] = content
    if raw.get("tool_calls") is not None:
        if role != "assistant":
            raise invalid(where + ".tool_calls", "Only assistant messages carry tool calls.")
        msg["tool_calls"] = _tool_calls(raw["tool_calls"], where + ".tool_calls")
    if role == "assistant" and content is None and "tool_calls" not in msg:
        raise invalid(where + ".content", "A message needs content.")
    if role == "tool":
        call_id = raw.get("tool_call_id")
        if not isinstance(call_id, str) or not 0 < len(call_id) <= 128:
            raise invalid(where + ".tool_call_id", "A tool message needs tool_call_id.")
        msg["tool_call_id"] = call_id
    name = raw.get("name")
    if name is not None:
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise invalid(where + ".name", "A name must be 1-64 letters, digits, _ or -.")
        msg["name"] = name
    return msg


# ------------------------------------------------------------------ options


def _tools(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_TOOLS:
        raise invalid("tools", f"tools must be a list of at most {MAX_TOOLS}.")
    out, seen = [], set()
    for i, tool in enumerate(value):
        at = f"tools[{i}]"
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict) or tool.get("type") != "function":
            raise invalid(at, "A tool must be a function.")
        name = fn.get("name")
        if not isinstance(name, str) or not _NAME_RE.match(name) or name in seen:
            raise invalid(at + ".function.name", "Tool names must be unique, 1-64 chars.")
        seen.add(name)
        params = fn.get("parameters", {"type": "object", "properties": {}})
        if not isinstance(params, dict):
            raise invalid(at + ".function.parameters", "parameters must be a JSON schema object.")
        entry: dict[str, Any] = {"name": name, "parameters": params}
        desc = fn.get("description")
        if desc is not None:
            if not isinstance(desc, str) or len(desc) > 4_000:
                raise invalid(at + ".function.description", "description is too long.")
            entry["description"] = desc
        out.append({"type": "function", "function": entry})
    return out


def _tool_choice(value: Any, tools: list[dict[str, Any]]) -> Any:
    if value is None:
        return None
    if value in ("auto", "none"):
        return value
    kind = value.get("type") if isinstance(value, dict) else None
    fn = value.get("function") if isinstance(value, dict) else None
    name = fn.get("name") if isinstance(fn, dict) else None
    if kind != "function" or not name:
        raise invalid("tool_choice", "tool_choice must be auto, none or a function.")
    if name not in {t["function"]["name"] for t in tools}:
        raise invalid("tool_choice", "tool_choice names a tool that isn't in tools.")
    return {"type": "function", "function": {"name": name}}


def _response_format(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    kind = value.get("type") if isinstance(value, dict) else None
    if kind in ("text", "json_object"):
        return {"type": kind}
    if kind == "json_schema":
        spec = value.get("json_schema")
        name = spec.get("name") if isinstance(spec, dict) else None
        schema = spec.get("schema") if isinstance(spec, dict) else None
        if not isinstance(name, str) or not _NAME_RE.match(name) or not isinstance(schema, dict):
            raise invalid("response_format", "json_schema needs a name and a schema object.")
        out = {"name": name, "schema": schema}
        if isinstance(spec.get("strict"), bool):
            out["strict"] = spec["strict"]
        return {"type": "json_schema", "json_schema": out}
    raise invalid("response_format", "response_format must be text, json_object or json_schema.")


def parse_chat(body: dict[str, Any]) -> ParsedChat:
    model = body.get("model")
    if model not in CHAT_ALIASES:
        raise invalid("model", "model must be fast, smart or vision.")
    preset = body.get("preset")
    if preset is not None and (not isinstance(preset, str) or not 0 < len(preset) <= 64):
        raise invalid("preset", "preset must be a preset id.")
    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise invalid("messages", "messages must be a non-empty list.")
    if len(raw_messages) > MAX_MESSAGES:
        raise invalid("messages", f"At most {MAX_MESSAGES} messages per request.")
    # Images are checked against the REQUESTED model here and again against the
    # model that will run (a preset can change it) by the caller.
    counter = [0]
    vision = ALIASES[model].vision or preset is not None
    messages = [_message(m, i, vision, counter) for i, m in enumerate(raw_messages)]
    tools = _tools(body.get("tools"))
    out = ParsedChat(
        model=model,
        messages=messages,
        preset=preset,
        tools=tools,
        tool_choice=_tool_choice(body.get("tool_choice"), tools),
        response_format=_response_format(body.get("response_format")),
        images=counter[0],
    )
    mot = body.get("max_output_tokens")
    if mot is not None:
        if isinstance(mot, bool) or not isinstance(mot, int) or mot < 1:
            raise invalid("max_output_tokens", "max_output_tokens must be a positive integer.")
        out.max_output_tokens = mot
    temp = body.get("temperature")
    if temp is not None:
        if isinstance(temp, bool) or not isinstance(temp, int | float) or not 0 <= temp <= 2:
            raise invalid("temperature", "temperature must be between 0 and 2.")
        out.temperature = float(temp)
    stream = body.get("stream", True)
    if not isinstance(stream, bool):
        raise invalid("stream", "stream must be true or false.")
    out.stream = stream
    user = body.get("user")
    if user is not None:
        if not isinstance(user, str) or not 0 < len(user) <= MAX_USER_CHARS:
            raise invalid("user", "user must be a short opaque string.")
        out.user = user
    return out


# ------------------------------------------------------------------ estimate


def _text_tokens(text: str) -> int:
    return len(text.encode("utf-8"))


def estimate_tokens(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    response_format: dict[str, Any] | None = None,
) -> int:
    total = 0
    for msg in messages:
        total += MESSAGE_OVERHEAD_TOKENS
        content = msg.get("content")
        if isinstance(content, str):
            total += _text_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if part["type"] == "text":
                    total += _text_tokens(part["text"])
                else:
                    total += IMAGE_TOKENS
        for call in msg.get("tool_calls") or ():
            total += _text_tokens(json.dumps(call, separators=(",", ":")))
    for tool in tools or ():
        total += TOOL_OVERHEAD_TOKENS + _text_tokens(json.dumps(tool, separators=(",", ":")))
    if response_format:
        total += SCHEMA_OVERHEAD_TOKENS + _text_tokens(
            json.dumps(response_format, separators=(",", ":"))
        )
    return total


def count_images(messages: list[dict[str, Any]]) -> int:
    return sum(
        1
        for m in messages
        if isinstance(m.get("content"), list)
        for p in m["content"]
        if p["type"] == "image_url"
    )


__all__ = [
    "MAX_BODY_BYTES",
    "MAX_IMAGES",
    "MAX_MESSAGES",
    "ParsedChat",
    "check_image_url",
    "count_images",
    "estimate_tokens",
    "load_json",
    "parse_chat",
    "read_body",
]
