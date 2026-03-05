"""Tests for config.py API key validation."""

from pocketpaw.config import validate_api_key


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
