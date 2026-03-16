"""Tests for config.py API key validation and numeric field constraints."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pocketpaw.config import Settings, validate_api_key


class TestValidateApiKey:
    """Test suite for validate_api_key() function."""

    # ==================== Valid Keys ====================

    def test_valid_anthropic_key(self):
        """Valid Anthropic API key should pass."""
        is_valid, warning = validate_api_key("anthropic_api_key", "sk-ant-api03-abc123")
        assert is_valid is True
        assert warning == ""

    def test_valid_openai_key(self):
        """Valid OpenAI API key should pass."""
        is_valid, warning = validate_api_key("openai_api_key", "sk-proj-abc123")
        assert is_valid is True
        assert warning == ""

    def test_valid_openai_legacy_key(self):
        """Valid legacy OpenAI API key should pass."""
        is_valid, warning = validate_api_key("openai_api_key", "sk-abc123")
        assert is_valid is True
        assert warning == ""

    def test_valid_telegram_token(self):
        """Valid Telegram bot token should pass."""
        is_valid, warning = validate_api_key(
            "telegram_bot_token", "123456789:AAH1234567890abcdefghijklmnopqrstuv"
        )
        assert is_valid is True
        assert warning == ""

    # ==================== Invalid Prefixes ====================

    def test_invalid_anthropic_key_wrong_prefix(self):
        """Anthropic key with wrong prefix should fail."""
        is_valid, warning = validate_api_key("anthropic_api_key", "sk-wrong-abc123")
        assert is_valid is False
        assert "Anthropic API key" in warning
        assert "expected format: sk-ant-..." in warning
        assert "Double-check for typos or truncation" in warning

    def test_invalid_anthropic_key_no_prefix(self):
        """Anthropic key without prefix should fail."""
        is_valid, warning = validate_api_key("anthropic_api_key", "abc123")
        assert is_valid is False
        assert "Anthropic API key" in warning

    def test_invalid_openai_key_wrong_prefix(self):
        """OpenAI key with wrong prefix should fail."""
        is_valid, warning = validate_api_key("openai_api_key", "pk-abc123")
        assert is_valid is False
        assert "OpenAI API key" in warning
        assert "expected format: sk-..." in warning

    def test_invalid_telegram_token_wrong_format(self):
        """Telegram token with wrong format should fail."""
        is_valid, warning = validate_api_key("telegram_bot_token", "123456789:invalid")
        assert is_valid is False
        assert "Telegram bot token" in warning
        assert "expected format: 123456789:AAH..." in warning

    def test_invalid_telegram_token_missing_colon(self):
        """Telegram token without colon separator should fail."""
        is_valid, warning = validate_api_key("telegram_bot_token", "123456789AAH1234567890")
        assert is_valid is False
        assert "Telegram bot token" in warning

    def test_invalid_telegram_token_no_aa_prefix(self):
        """Telegram token without AA prefix after colon should fail."""
        is_valid, warning = validate_api_key(
            "telegram_bot_token", "123456789:XYH1234567890abcdefghijklmnopqrstuv"
        )
        assert is_valid is False
        assert "Telegram bot token" in warning

    # ==================== Empty Values ====================

    def test_empty_string_allowed(self):
        """Empty string should be allowed (for unsetting keys)."""
        is_valid, warning = validate_api_key("anthropic_api_key", "")
        assert is_valid is True
        assert warning == ""

    def test_whitespace_only_allowed(self):
        """Whitespace-only string should be allowed (treated as empty)."""
        is_valid, warning = validate_api_key("openai_api_key", "   ")
        assert is_valid is True
        assert warning == ""

    def test_none_value_allowed(self):
        """None value should be allowed."""
        is_valid, warning = validate_api_key("anthropic_api_key", None)
        assert is_valid is True
        assert warning == ""

    # ==================== Unknown Fields ====================

    def test_unknown_field_passes_through(self):
        """Unknown field names should pass through without validation."""
        is_valid, warning = validate_api_key("unknown_field", "any_value_here")
        assert is_valid is True
        assert warning == ""

    def test_unvalidated_api_key_passes_through(self):
        """API key fields without validation patterns should pass through."""
        is_valid, warning = validate_api_key("google_api_key", "any_format")
        assert is_valid is True
        assert warning == ""

    def test_unvalidated_with_empty_value(self):
        """Empty values for unvalidated fields should pass."""
        is_valid, warning = validate_api_key("tavily_api_key", "")
        assert is_valid is True
        assert warning == ""

    # ==================== Edge Cases ====================

    def test_key_with_leading_whitespace(self):
        """Key with leading whitespace should be validated after stripping."""
        is_valid, warning = validate_api_key("anthropic_api_key", "  sk-ant-api03-abc123")
        assert is_valid is True
        assert warning == ""

    def test_key_with_trailing_whitespace(self):
        """Key with trailing whitespace should be validated after stripping."""
        is_valid, warning = validate_api_key("openai_api_key", "sk-proj-abc123  ")
        assert is_valid is True
        assert warning == ""

    def test_key_with_surrounding_whitespace(self):
        """Key with surrounding whitespace should be validated after stripping."""
        is_valid, warning = validate_api_key("anthropic_api_key", "  sk-ant-api03-abc123  ")
        assert is_valid is True
        assert warning == ""

    def test_very_long_valid_key(self):
        """Very long but valid key should pass."""
        long_key = "sk-ant-" + "a" * 1000
        is_valid, warning = validate_api_key("anthropic_api_key", long_key)
        assert is_valid is True
        assert warning == ""

    def test_anthropic_key_catches_openai_prefix(self):
        """Anthropic validator should reject keys that look like OpenAI keys."""
        is_valid, warning = validate_api_key("anthropic_api_key", "sk-proj-abc123")
        assert is_valid is False
        assert "Anthropic API key" in warning

    def test_return_type_is_tuple(self):
        """Function should always return a tuple of (bool, str)."""
        result = validate_api_key("anthropic_api_key", "sk-ant-abc")
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], str)


class TestNumericFieldConstraints:
    """Tests for gt/ge constraints on numeric settings in Settings (issue #629)."""

    # ─── compaction_recent_window ───────────────────────────────────────────

    def test_compaction_recent_window_zero_rejected(self):
        """compaction_recent_window=0 must raise a ValidationError."""
        with pytest.raises(ValidationError):
            Settings(compaction_recent_window=0)

    def test_compaction_recent_window_negative_rejected(self):
        """compaction_recent_window=-1 must raise a ValidationError."""
        with pytest.raises(ValidationError):
            Settings(compaction_recent_window=-1)

    def test_compaction_recent_window_positive_accepted(self):
        """compaction_recent_window=1 must be accepted."""
        s = Settings(compaction_recent_window=1)
        assert s.compaction_recent_window == 1

    # ─── compaction_char_budget ─────────────────────────────────────────────

    def test_compaction_char_budget_zero_rejected(self):
        """compaction_char_budget=0 must raise a ValidationError."""
        with pytest.raises(ValidationError):
            Settings(compaction_char_budget=0)

    def test_compaction_char_budget_negative_rejected(self):
        """compaction_char_budget=-100 must raise a ValidationError."""
        with pytest.raises(ValidationError):
            Settings(compaction_char_budget=-100)

    def test_compaction_char_budget_positive_accepted(self):
        """compaction_char_budget=1 must be accepted."""
        s = Settings(compaction_char_budget=1)
        assert s.compaction_char_budget == 1

    # ─── compaction_summary_chars ───────────────────────────────────────────

    def test_compaction_summary_chars_zero_rejected(self):
        """compaction_summary_chars=0 must raise a ValidationError."""
        with pytest.raises(ValidationError):
            Settings(compaction_summary_chars=0)

    def test_compaction_summary_chars_negative_rejected(self):
        """compaction_summary_chars=-5 must raise a ValidationError."""
        with pytest.raises(ValidationError):
            Settings(compaction_summary_chars=-5)

    def test_compaction_summary_chars_positive_accepted(self):
        """compaction_summary_chars=1 must be accepted."""
        s = Settings(compaction_summary_chars=1)
        assert s.compaction_summary_chars == 1

    # ─── session_token_ttl_hours ────────────────────────────────────────────

    def test_session_token_ttl_hours_zero_rejected(self):
        """session_token_ttl_hours=0 must raise a ValidationError.

        Zero TTL means all session tokens are immediately expired.
        """
        with pytest.raises(ValidationError):
            Settings(session_token_ttl_hours=0)

    def test_session_token_ttl_hours_negative_rejected(self):
        """session_token_ttl_hours=-1 must raise a ValidationError."""
        with pytest.raises(ValidationError):
            Settings(session_token_ttl_hours=-1)

    def test_session_token_ttl_hours_positive_accepted(self):
        """session_token_ttl_hours=1 must be accepted."""
        s = Settings(session_token_ttl_hours=1)
        assert s.session_token_ttl_hours == 1

    # ─── api_rate_limit_per_key ─────────────────────────────────────────────

    def test_api_rate_limit_per_key_zero_rejected(self):
        """api_rate_limit_per_key=0 must raise a ValidationError.

        Zero capacity causes the rate limiter to reject every request.
        """
        with pytest.raises(ValidationError):
            Settings(api_rate_limit_per_key=0)

    def test_api_rate_limit_per_key_negative_rejected(self):
        """api_rate_limit_per_key=-10 must raise a ValidationError."""
        with pytest.raises(ValidationError):
            Settings(api_rate_limit_per_key=-10)

    def test_api_rate_limit_per_key_positive_accepted(self):
        """api_rate_limit_per_key=1 must be accepted."""
        s = Settings(api_rate_limit_per_key=1)
        assert s.api_rate_limit_per_key == 1

    # ─── media_max_file_size_mb ─────────────────────────────────────────────

    def test_media_max_file_size_mb_zero_accepted(self):
        """media_max_file_size_mb=0 must be accepted (documented as unlimited)."""
        s = Settings(media_max_file_size_mb=0)
        assert s.media_max_file_size_mb == 0

    def test_media_max_file_size_mb_negative_rejected(self):
        """media_max_file_size_mb=-1 must raise a ValidationError."""
        with pytest.raises(ValidationError):
            Settings(media_max_file_size_mb=-1)

    def test_media_max_file_size_mb_positive_accepted(self):
        """media_max_file_size_mb=100 must be accepted."""
        s = Settings(media_max_file_size_mb=100)
        assert s.media_max_file_size_mb == 100

    # ─── max_concurrent_conversations ───────────────────────────────────────

    def test_max_concurrent_conversations_zero_rejected(self):
        """max_concurrent_conversations=0 must raise a ValidationError (zero limit deadlocks)."""
        with pytest.raises(ValidationError):
            Settings(max_concurrent_conversations=0)

    def test_max_concurrent_conversations_negative_rejected(self):
        """max_concurrent_conversations=-3 must raise a ValidationError."""
        with pytest.raises(ValidationError):
            Settings(max_concurrent_conversations=-3)

    def test_max_concurrent_conversations_positive_accepted(self):
        """max_concurrent_conversations=1 must be accepted."""
        s = Settings(max_concurrent_conversations=1)
        assert s.max_concurrent_conversations == 1
