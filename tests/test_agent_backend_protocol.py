"""Tests for the AgentBackend protocol and BaseAgentBackend mixin.

The mixin gives non-deep_agents backends a clear NotImplementedError when
the specialist runtime calls attach_specialist_tools() on them, instead of
an unhelpful AttributeError. The runtime uses this signal to gracefully
exclude incompatible backends from the pocket_specialist_backend set.
"""

import pytest


class TestBaseAgentBackendNotImplemented:
    """Non-deep_agents backends raise NotImplementedError from
    attach_specialist_tools — used by the specialist runtime to gracefully
    exclude incompatible backends."""

    def test_claude_sdk_raises_not_implemented(self):
        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

        # Build minimal — we never call .run(), just check the method exists
        # and raises with a useful message.
        backend = ClaudeSDKBackend.__new__(ClaudeSDKBackend)
        with pytest.raises(NotImplementedError, match="does not support dynamic tool attachment"):
            backend.attach_specialist_tools([])

    def test_openai_agents_raises_not_implemented(self):
        from pocketpaw.agents.openai_agents import OpenAIAgentsBackend

        backend = OpenAIAgentsBackend.__new__(OpenAIAgentsBackend)
        with pytest.raises(NotImplementedError, match="does not support dynamic tool attachment"):
            backend.attach_specialist_tools([])

    def test_google_adk_raises_not_implemented(self):
        from pocketpaw.agents.google_adk import GoogleADKBackend

        backend = GoogleADKBackend.__new__(GoogleADKBackend)
        with pytest.raises(NotImplementedError, match="does not support dynamic tool attachment"):
            backend.attach_specialist_tools([])

    def test_codex_cli_raises_not_implemented(self):
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend.__new__(CodexCLIBackend)
        with pytest.raises(NotImplementedError, match="does not support dynamic tool attachment"):
            backend.attach_specialist_tools([])

    def test_opencode_raises_not_implemented(self):
        from pocketpaw.agents.opencode import OpenCodeBackend

        backend = OpenCodeBackend.__new__(OpenCodeBackend)
        with pytest.raises(NotImplementedError, match="does not support dynamic tool attachment"):
            backend.attach_specialist_tools([])

    def test_copilot_sdk_raises_not_implemented(self):
        from pocketpaw.agents.copilot_sdk import CopilotSDKBackend

        backend = CopilotSDKBackend.__new__(CopilotSDKBackend)
        with pytest.raises(NotImplementedError, match="does not support dynamic tool attachment"):
            backend.attach_specialist_tools([])

    def test_error_message_includes_class_name(self):
        """The error message should name the offending class so callers know
        which backend rejected the attach call."""
        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

        backend = ClaudeSDKBackend.__new__(ClaudeSDKBackend)
        with pytest.raises(NotImplementedError, match="ClaudeSDKBackend"):
            backend.attach_specialist_tools([])
