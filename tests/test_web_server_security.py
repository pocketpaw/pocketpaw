"""Tests for web_server.py security hardening.

Covers:
  - F-07 (#445): Bot token must not leak in /setup error responses.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def _fake_settings():
    """Create a minimal mock Settings for web_server.create_app."""
    s = MagicMock()
    s.telegram_bot_token = ""
    s.openai_api_key = ""
    s.anthropic_api_key = ""
    s.allowed_user_id = 0
    s.save = MagicMock()
    return s


class TestSetupTokenRedaction:
    """Ensure the /setup endpoint never leaks the bot token in error messages."""

    def test_bot_token_redacted_from_error_response(self, _fake_settings):
        """If bot.get_me() fails with a message that contains the token,
        the HTTP response must not expose it."""
        fake_token = "123456:AAFakeToken-TestOnly_1234567890abcdef"
        # Simulate the error python-telegram-bot raises when the API call fails;
        # the URL in the error message contains the full bot token.
        api_error_msg = (
            f"Conflict: terminated by other getUpdates request; "
            f"make sure that only one bot instance is running. "
            f"URL: https://api.telegram.org/bot{fake_token}/getMe"
        )

        with (
            patch("pocketpaw.web_server.Application") as mock_app_cls,
        ):
            mock_builder = MagicMock()
            mock_app_cls.builder.return_value = mock_builder
            mock_builder.token.return_value = mock_builder
            mock_app = MagicMock()
            mock_builder.build.return_value = mock_app
            mock_app.bot.get_me = AsyncMock(side_effect=Exception(api_error_msg))

            from pocketpaw.web_server import create_app

            app = create_app(_fake_settings)

            from starlette.testclient import TestClient

            client = TestClient(app)
            resp = client.post(
                "/setup",
                data={"bot_token": fake_token},
            )

            body = resp.json()
            assert "error" in body
            # The raw token must NOT appear anywhere in the response
            assert fake_token not in body["error"], "Bot token leaked in /setup error response"
            # The redaction marker should be present instead
            assert "[REDACTED]" in body["error"]

    def test_error_without_token_passes_through(self, _fake_settings):
        """If the error message does not contain the token, it should still
        be returned (no false-positive redaction)."""
        fake_token = "123456:AAFakeToken-TestOnly_1234567890abcdef"
        generic_error = "Network is unreachable"

        with (
            patch("pocketpaw.web_server.Application") as mock_app_cls,
        ):
            mock_builder = MagicMock()
            mock_app_cls.builder.return_value = mock_builder
            mock_builder.token.return_value = mock_builder
            mock_app = MagicMock()
            mock_builder.build.return_value = mock_app
            mock_app.bot.get_me = AsyncMock(side_effect=Exception(generic_error))

            from pocketpaw.web_server import create_app

            app = create_app(_fake_settings)

            from starlette.testclient import TestClient

            client = TestClient(app)
            resp = client.post(
                "/setup",
                data={"bot_token": fake_token},
            )

            body = resp.json()
            assert "error" in body
            assert generic_error in body["error"]
            assert "[REDACTED]" not in body["error"]
