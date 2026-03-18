"""Tests for GuardianAgent - security/guardian.py"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketpaw.security.guardian import GuardianAgent


@pytest.fixture
def guardian():
    with (
        patch("pocketpaw.security.guardian.get_settings"),
        patch("pocketpaw.security.guardian.get_audit_logger"),
    ):
        agent = GuardianAgent()
        agent.client = MagicMock()
        return agent


class TestGuardianEmptyResponse:
    """Tests for fix: issue #636 - empty API response causes IndexError."""

    @pytest.mark.asyncio
    async def test_empty_content_returns_dangerous(self, guardian):
        """Empty response.content should default to DANGEROUS (fail-closed)."""
        mock_response = MagicMock()
        mock_response.content = []
        guardian.client.messages.create = AsyncMock(return_value=mock_response)

        is_safe, reason = await guardian.check_command("rm -rf /")

        assert is_safe is False
        assert "empty" in reason.lower()

    @pytest.mark.asyncio
    async def test_empty_content_does_not_raise(self, guardian):
        """Empty response.content must not raise IndexError."""
        mock_response = MagicMock()
        mock_response.content = []
        guardian.client.messages.create = AsyncMock(return_value=mock_response)

        try:
            await guardian.check_command("ls -la")
        except IndexError:
            pytest.fail("IndexError raised on empty response.content")

    @pytest.mark.asyncio
    async def test_valid_safe_response_still_works(self, guardian):
        """Normal SAFE response should still be allowed after the fix."""
        mock_content = MagicMock()
        mock_content.text = '{"status": "SAFE", "reason": "Read-only command"}'
        mock_response = MagicMock()
        mock_response.content = [mock_content]
        guardian.client.messages.create = AsyncMock(return_value=mock_response)

        is_safe, reason = await guardian.check_command("ls -la")

        assert is_safe is True
        assert reason == "Read-only command"

    @pytest.mark.asyncio
    async def test_valid_dangerous_response_still_blocked(self, guardian):
        """Normal DANGEROUS response should still be blocked after the fix."""
        mock_content = MagicMock()
        mock_content.text = '{"status": "DANGEROUS", "reason": "Destructive command"}'
        mock_response = MagicMock()
        mock_response.content = [mock_content]
        guardian.client.messages.create = AsyncMock(return_value=mock_response)

        is_safe, reason = await guardian.check_command("rm -rf /")

        assert is_safe is False
        assert reason == "Destructive command"
