# tests/agents/test_image_attachments_reach_the_model.py — an attached image is
# handed to the model AS AN IMAGE, in each backend's own native shape.
# Created: 2026-09-12 (fix/chat-image-attachments). New file.
#
# Under test: ``claude_sdk.build_streaming_user_message``,
# ``claude_sdk.stream_one_message`` and ``pydantic_ai.build_multimodal_prompt``.
#
# WHY. ``AgentBackend.run``'s first parameter was ``message: str``, and that one
# annotation is the whole bug: both SDKs underneath take images natively, but a
# turn was flattened to a string before reaching either. The cloud chat path
# filled the gap by running OCR over the pixels — which returns nothing for a
# photo of a person or a place — so "the agent cannot see my image" was true
# while every test passed.
#
# These assert the shapes the VENDOR DOCS specify, because that is the thing we
# can be wrong about without any error being raised: a malformed content block
# does not throw, it just quietly stops being an image.
#
#   * Claude Agent SDK streaming input:
#     {"type": "user", "message": {"role": "user", "content": [
#         {"type": "text", ...},
#         {"type": "image", "source": {"type": "base64", "media_type", "data"}}]}}
#     (single-message mode explicitly does NOT support image attachments)
#   * pydantic-ai: a LIST prompt mixing strings with BinaryContent entries.
#
# The no-image cases matter as much as the image ones: a text turn must stay
# byte-identical, or every existing run pays for the multimodal path.

from __future__ import annotations

import base64

import pytest

from pocketpaw.agents import claude_sdk
from pocketpaw.agents.backend import ImageAttachment

_PNG = b"\x89PNG\r\n\x1a\n" + b"not really a png, but bytes are bytes" * 3
_IMG = ImageAttachment(data=_PNG, media_type="image/png", filename="logo.png")


class TestClaudeAgentSDKShape:
    def test_a_text_only_turn_is_left_alone(self) -> None:
        """No images means the string path, untouched. The multimodal branch must
        not tax the turns that never had an attachment."""
        msg = claude_sdk.build_streaming_user_message("just text", ())
        assert msg["message"]["content"] == [{"type": "text", "text": "just text"}]

    def test_an_image_becomes_a_base64_content_block(self) -> None:
        """The block the SDK documents — and the bytes must survive the trip."""
        msg = claude_sdk.build_streaming_user_message("what is this?", (_IMG,))

        assert msg["type"] == "user"
        assert msg["message"]["role"] == "user"
        text, image = msg["message"]["content"]

        assert text == {"type": "text", "text": "what is this?"}
        assert image["type"] == "image"
        assert image["source"]["type"] == "base64"
        assert image["source"]["media_type"] == "image/png"
        # Decodes back to exactly what was attached. A truncated or re-encoded
        # payload still looks like a valid block and shows the model nothing.
        assert base64.b64decode(image["source"]["data"]) == _PNG

    def test_the_data_is_base64_text_not_raw_bytes(self) -> None:
        """``json.dumps`` cannot serialize bytes, and the failure would surface
        deep in the SDK as a serialization error rather than here."""
        msg = claude_sdk.build_streaming_user_message("x", (_IMG,))
        assert isinstance(msg["message"]["content"][1]["source"]["data"], str)

    def test_every_image_in_the_turn_is_carried(self) -> None:
        """Five attachments must be five blocks — a loop that overwrites instead
        of appending shows the model only the last one."""
        imgs = tuple(
            ImageAttachment(data=bytes([i]) * 8, media_type="image/jpeg", filename=f"{i}.jpg")
            for i in range(5)
        )
        msg = claude_sdk.build_streaming_user_message("five", imgs)
        blocks = [b for b in msg["message"]["content"] if b["type"] == "image"]
        assert len(blocks) == 5
        assert [base64.b64decode(b["source"]["data"]) for b in blocks] == [i.data for i in imgs]

    @pytest.mark.asyncio
    async def test_the_payload_is_offered_as_an_async_iterable(self) -> None:
        """``ClaudeSDKClient.query`` takes an async iterable for streaming input.
        A bare dict is not one — and streaming input is the ONLY mode that
        accepts images, so this wrapper is what keeps them attached."""
        payload = claude_sdk.build_streaming_user_message("hi", (_IMG,))
        got = [m async for m in claude_sdk.stream_one_message(payload)]
        assert got == [payload]


class TestPydanticAIShape:
    def test_a_text_only_turn_stays_a_bare_string(self) -> None:
        """Not a one-element list. The prompt type is what every existing run
        sends, and widening it for turns with nothing attached is a change with
        no reason and a blast radius."""
        from pocketpaw.agents.pydantic_ai import build_multimodal_prompt

        assert build_multimodal_prompt("just text", ()) == "just text"

    def test_an_image_becomes_binarycontent_in_a_list_prompt(self) -> None:
        """pydantic-ai's documented multimodal shape."""
        from pydantic_ai import BinaryContent

        from pocketpaw.agents.pydantic_ai import build_multimodal_prompt

        prompt = build_multimodal_prompt("what is this?", (_IMG,))

        assert isinstance(prompt, list)
        assert prompt[0] == "what is this?"
        assert isinstance(prompt[1], BinaryContent)
        assert prompt[1].data == _PNG
        assert prompt[1].media_type == "image/png"

    def test_it_never_builds_a_url_part_from_an_attachment(self) -> None:
        """pydantic-ai warns that providers fetch cloud-storage URLs (s3://,
        gs://) with OUR credentials, so a URL part built from user-controlled
        input is SSRF-shaped. We hold the bytes; we send the bytes."""
        from pydantic_ai import ImageUrl

        from pocketpaw.agents.pydantic_ai import build_multimodal_prompt

        prompt = build_multimodal_prompt("x", (_IMG,))
        assert not any(isinstance(p, ImageUrl) for p in prompt)
