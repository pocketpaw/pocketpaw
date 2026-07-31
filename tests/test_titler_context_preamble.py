"""Session titles must come from what the USER typed, not the client's
context preamble.

Regression cover for the visible bug: the home chat prepends a machine-readable
snapshot of the user's pinned widgets/activity to the wire content, so every
home-started session was named "[Home Page Snapshot] Time Of Day: Afternoon…"
instead of the actual first message. The strip lives in the titler because both
title paths (Haiku and the offline fallback) flow through it.
"""

from __future__ import annotations

import pytest

from pocketpaw.memory.titler import (
    fallback_title,
    generate_title,
    strip_context_preamble,
)

# The exact shape paw-enterprise sends (see home-context.ts).
WRAPPED = (
    "[Home page snapshot]\n"
    "Time of day: afternoon\n"
    "Status: 6 pinned widgets\n"
    "Recent activity: 3 runs\n"
    "\n"
    "[User message]\n"
    "how do I add a chart to my home?"
)


class TestStripContextPreamble:
    def test_returns_only_the_user_text(self):
        assert strip_context_preamble(WRAPPED) == "how do I add a chart to my home?"

    def test_leaves_an_ordinary_message_untouched(self):
        assert strip_context_preamble("just a normal question") == "just a normal question"

    def test_handles_empty_and_missing_input(self):
        assert strip_context_preamble("") == ""
        assert strip_context_preamble("   ") == "   "

    def test_last_marker_wins_so_the_preamble_can_never_leak(self):
        # A user quoting the marker inside their own text must not cause the
        # snapshot above it to survive into the title.
        quoted = (
            "[Home page snapshot]\ncontext\n\n[User message]\nwhy does\n[User message]\nrepeat?"
        )
        assert strip_context_preamble(quoted) == "repeat?"

    def test_marker_at_the_very_end_yields_empty(self):
        assert strip_context_preamble("context\n[User message]\n") == ""


class TestFallbackTitle:
    def test_titles_the_user_text_not_the_snapshot(self):
        assert fallback_title(WRAPPED) == "how do I add a chart to my home?"

    def test_ordinary_message_is_unaffected(self):
        assert fallback_title("ship the release") == "ship the release"

    def test_empty_after_stripping_returns_none(self):
        # Nothing but a preamble — the caller skips the event entirely rather
        # than naming the session after the scaffolding.
        assert fallback_title("context\n[User message]\n   ") is None

    def test_long_user_text_still_truncates(self):
        long_msg = "context\n[User message]\n" + ("word " * 40)
        title = fallback_title(long_msg)
        assert title is not None
        assert title.endswith("…")
        assert len(title) <= 61
        assert "context" not in title


class TestGenerateTitle:
    @pytest.mark.asyncio
    async def test_falls_back_to_the_clean_user_text_without_an_api_key(self):
        # No api_key → fallback path, which is exactly the path that shipped
        # the bug in local/dev deployments.
        title = await generate_title(WRAPPED, model="claude-haiku-4-5", api_key=None)
        assert title == "how do I add a chart to my home?"

    @pytest.mark.asyncio
    async def test_preamble_only_message_produces_no_title(self):
        title = await generate_title(
            "[Home page snapshot]\nstuff\n\n[User message]\n   ",
            model="claude-haiku-4-5",
            api_key=None,
        )
        assert title is None

    @pytest.mark.asyncio
    async def test_the_model_never_sees_the_preamble(self, monkeypatch):
        """The Haiku prompt is built from the stripped text — otherwise the
        model titles the chat after the surface rather than the subject."""
        seen: dict[str, str] = {}

        class _FakeMessages:
            async def create(self, *, model, max_tokens, messages):  # noqa: ARG002
                seen["prompt"] = messages[0]["content"]

                class _Block:
                    text = "Adding A Home Chart"

                class _Resp:
                    content = [_Block()]

                return _Resp()

        class _FakeClient:
            def __init__(self, *args, **kwargs):
                self.messages = _FakeMessages()

        import anthropic

        monkeypatch.setattr(anthropic, "AsyncAnthropic", _FakeClient)

        title = await generate_title(WRAPPED, model="claude-haiku-4-5", api_key="sk-test")

        assert title == "Adding A Home Chart"
        assert "how do I add a chart to my home?" in seen["prompt"]
        assert "Home page snapshot" not in seen["prompt"]
        assert "pinned widgets" not in seen["prompt"]
