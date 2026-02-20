"""Tests for output redaction module."""

import pytest

from pocketpaw.security.redact import redact_output


class TestRedactOutput:
    """Test suite for output redaction."""

    def test_redact_openai_key(self):
        """Test redaction of OpenAI API keys."""
        text = "My OpenAI key is sk-abc123def456ghi789jkl012mno345"
        result = redact_output(text)
        assert "sk-abc123def456ghi789jkl012mno345" not in result
        assert "[REDACTED]" in result

    def test_redact_anthropic_key(self):
        """Test redaction of Anthropic API keys."""
        text = "Use this key: sk-ant-api03-abc123def456ghi789jkl012mno345pqr678stu901vwx234yz567abc890def123ghi456jkl789mno012pqr345stu678vwx"
        result = redact_output(text)
        assert "sk-ant-" not in result
        assert "[REDACTED]" in result

    def test_redact_aws_access_key(self):
        """Test redaction of AWS access keys."""
        text = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
        result = redact_output(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "[REDACTED]" in result

    def test_redact_aws_secret_key(self):
        """Test redaction of AWS secret keys."""
        text = "AWS_SECRET=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        result = redact_output(text)
        assert "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" not in result
        assert "[REDACTED]" in result

    def test_redact_bearer_token(self):
        """Test redaction of Bearer tokens."""
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        result = redact_output(text)
        assert "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in result
        assert "[REDACTED]" in result

    def test_redact_basic_auth_url(self):
        """Test redaction of basic auth credentials in URLs."""
        text = "Connect to https://user:secretpassword123@example.com/api"
        result = redact_output(text)
        assert "secretpassword123" not in result
        assert "[REDACTED]" in result

    def test_redact_github_token(self):
        """Test redaction of GitHub personal access tokens."""
        text = "Token: ghp_abcdefghijklmnopqrstuvwxyz123456"
        result = redact_output(text)
        assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in result
        assert "[REDACTED]" in result

    def test_redact_private_key_header(self):
        """Test redaction of private key headers."""
        text = """
        -----BEGIN RSA PRIVATE KEY-----
        MIIEpAIBAAKCAQEA...
        """
        result = redact_output(text)
        assert "BEGIN RSA PRIVATE KEY" not in result
        assert "[REDACTED]" in result

    def test_redact_jwt_token(self):
        """Test redaction of JWT tokens."""
        text = "JWT: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.TJVA95OrM7E2cBab30RMHrHDcEfxjoYZgeFONFh7HgQ"
        result = redact_output(text)
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in result
        assert "[REDACTED]" in result

    def test_redact_env_var_secret(self):
        """Test redaction of secrets in environment variable format."""
        text = "DB_PASSWORD=SuperSecret123Pass"
        result = redact_output(text)
        assert "SuperSecret123Pass" not in result
        assert "[REDACTED]" in result

    def test_redact_slack_token(self):
        """Test redaction of Slack tokens."""
        # Construct token dynamically to avoid triggering GitHub secret scanner
        prefix = "xo" + "xb-"
        fake_token = f"{prefix}0000000000-0000000000000-TESTTESTTESTTESTTEST"
        text = f"Token: {fake_token}"
        result = redact_output(text)
        assert prefix + "0000000000" not in result
        assert "[REDACTED]" in result

    def test_redact_google_api_key(self):
        """Test redaction of Google API keys."""
        # Construct key dynamically to avoid triggering GitHub secret scanner
        prefix = "AIza" + "Sy"
        fake_key = f"{prefix}DaGmWKa4JsXZ-HjGw7ISLn_3namBGewQe"
        text = f"GOOGLE_API_KEY={fake_key}"
        result = redact_output(text)
        assert fake_key not in result
        assert "[REDACTED]" in result

    def test_redact_stripe_key(self):
        """Test redaction of Stripe API keys."""
        # Construct key dynamically to avoid triggering GitHub secret scanner
        prefix = "sk_" + "live_"
        fake_key = f"{prefix}XXXXXXXXXXXXXXXXXXXXXXXX"
        text = f"STRIPE_KEY={fake_key}"
        result = redact_output(text)
        assert fake_key not in result
        assert "[REDACTED]" in result

    def test_redact_api_key_parameter(self):
        """Test redaction of generic API key parameters."""
        text = "api_key=abcdef123456789012345678"
        result = redact_output(text)
        assert "abcdef123456789012345678" not in result
        assert "[REDACTED]" in result

    def test_redact_token_parameter(self):
        """Test redaction of generic token parameters."""
        text = "access_token=longtoken123456789012345678"
        result = redact_output(text)
        assert "longtoken123456789012345678" not in result
        assert "[REDACTED]" in result

    def test_multiple_secrets_in_text(self):
        """Test redaction of multiple different secrets in same text."""
        text = """
        Here's my config:
        OPENAI_KEY=sk-abc123def456ghi789jkl012mno345
        AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE
        DATABASE_PASSWORD=SuperSecretPass123
        """
        result = redact_output(text)
        assert "sk-abc123def456ghi789jkl012mno345" not in result
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "SuperSecretPass123" not in result
        assert result.count("[REDACTED]") >= 3

    def test_no_false_positives_on_safe_text(self):
        """Test that safe text is not incorrectly redacted."""
        text = "This is a normal message with no secrets."
        result = redact_output(text)
        assert result == text
        assert "[REDACTED]" not in result

    def test_empty_string(self):
        """Test that empty strings are handled correctly."""
        result = redact_output("")
        assert result == ""

    def test_none_input(self):
        """Test that None input is handled correctly."""
        result = redact_output(None)
        assert result is None

    def test_preserve_context_around_secrets(self):
        """Test that context around secrets is preserved."""
        text = "My API key is sk-abc123def456ghi789jkl012mno345 and it works!"
        result = redact_output(text)
        assert "My API key is" in result
        assert "and it works!" in result
        assert "[REDACTED]" in result

    def test_case_insensitive_patterns(self):
        """Test that patterns work case-insensitively where appropriate."""
        text = "API_KEY=abcdef123456789012345678"
        result = redact_output(text)
        assert "abcdef123456789012345678" not in result
        assert "[REDACTED]" in result

    def test_redact_env_file_content(self):
        """Test redaction of a typical .env file content."""
        # Construct Stripe key dynamically to avoid triggering GitHub secret scanner
        stripe_prefix = "sk_" + "live_"
        stripe_key = f"{stripe_prefix}XXXXXXXXXXXXXXXXXXXXXXXX"

        text = f"""
        # Database config
        DB_HOST=localhost
        DB_PORT=5432
        DB_PASSWORD=MyDatabasePassword123

        # API Keys
        OPENAI_API_KEY=sk-abc123def456ghi789jkl012mno345
        STRIPE_SECRET_KEY={stripe_key}

        # AWS
        AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE
        AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
        """
        result = redact_output(text)
        # Should preserve structure and comments
        assert "# Database config" in result
        assert "DB_HOST=localhost" in result
        assert "DB_PORT=5432" in result
        # Should redact secrets
        assert "MyDatabasePassword123" not in result
        assert "sk-abc123def456ghi789jkl012mno345" not in result
        assert stripe_key not in result
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" not in result
        # Should have multiple redactions
        assert result.count("[REDACTED]") >= 5

    def test_agent_cat_env_file_scenario(self):
        """Test the specific scenario mentioned in the issue: agent cats a .env file."""
        text = """
        $ cat .env
        OPENAI_API_KEY=sk-proj-abc123def456ghi789
        ANTHROPIC_API_KEY=sk-ant-api03-longkeyhere12345
        DATABASE_URL=postgresql://user:password123@localhost/db
        JWT_SECRET=SuperSecretJWTKey123456789
        """
        result = redact_output(text)
        # Command should remain
        assert "$ cat .env" in result
        # Variable names should remain
        assert "OPENAI_API_KEY=" in result
        assert "ANTHROPIC_API_KEY=" in result
        assert "DATABASE_URL=" in result
        assert "JWT_SECRET=" in result
        # Secrets should be redacted
        assert "sk-proj-abc123def456ghi789" not in result
        assert "sk-ant-api03-longkeyhere12345" not in result
        assert "password123" not in result
        assert "SuperSecretJWTKey123456789" not in result
        assert "[REDACTED]" in result
