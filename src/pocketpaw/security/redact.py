"""
Output-level redaction for API keys, tokens, and secrets.

This module prevents accidental leakage of sensitive data in agent responses.
It operates at the message bus level, making it backend-agnostic.
"""

import re

# Regex patterns for common secret formats
REDACT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # OpenAI API keys (sk-...)
    (
        "OpenAI API Key",
        re.compile(r"\bsk-[a-zA-Z0-9]{20,}\b", re.IGNORECASE),
    ),
    # Anthropic API keys (sk-ant-...)
    (
        "Anthropic API Key",
        re.compile(r"\bsk-ant-[a-zA-Z0-9_-]{95,}\b", re.IGNORECASE),
    ),
    # AWS Access Key IDs (AKIA...)
    (
        "AWS Access Key",
        re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    ),
    # AWS Secret Access Keys (scoped to env var format to avoid false positives)
    (
        "AWS Secret Key",
        re.compile(
            r"AWS_SECRET_ACCESS_KEY\s*[=:]\s*['\"]?([A-Za-z0-9/+=]{40})['\"]?",
            re.IGNORECASE,
        ),
    ),
    # Generic API keys (api_key=..., apikey=..., api-key=...)
    (
        "API Key Parameter",
        re.compile(
            r"(?:api[_-]?key|apikey)\s*[=:]\s*['\"]?([a-zA-Z0-9_\-]{16,})['\"]?",
            re.IGNORECASE,
        ),
    ),
    # Bearer tokens in Authorization headers
    (
        "Bearer Token",
        re.compile(r"\bBearer\s+[a-zA-Z0-9_\-\.]{20,}\b", re.IGNORECASE),
    ),
    # Basic auth in URLs (http://user:pass@host, postgresql://user:pass@host, etc.)
    (
        "Basic Auth in URL",
        re.compile(
            r"(?:https?|postgresql|mysql|mongodb|redis|ftp|sftp)://[a-zA-Z0-9_\-]+:([^@\s]{3,})@[a-zA-Z0-9\-\./:]+"
        ),
    ),
    # GitHub Personal Access Tokens (ghp_..., gho_..., ghu_...)
    (
        "GitHub Token",
        re.compile(r"\bgh[pousr]_[a-zA-Z0-9]{36,}\b"),
    ),
    # Generic tokens (token=..., access_token=...)
    (
        "Token Parameter",
        re.compile(
            r"(?:token|access_token|auth_token)\s*[=:]\s*['\"]?([a-zA-Z0-9_\-\.]{20,})['\"]?",
            re.IGNORECASE,
        ),
    ),
    # Private keys (RSA, SSH, etc.)
    (
        "Private Key",
        re.compile(
            r"-----BEGIN\s+(?:RSA|DSA|EC|OPENSSH)?\s*PRIVATE KEY-----",
            re.IGNORECASE,
        ),
    ),
    # JWT tokens (three base64 segments separated by dots)
    (
        "JWT Token",
        re.compile(r"\beyJ[a-zA-Z0-9_\-]+\.eyJ[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\b"),
    ),
    # Generic secrets in environment variable format (SECRET=..., PASSWORD=...)
    (
        "Environment Variable Secret",
        re.compile(
            r"(?:SECRET|PASSWORD|PASSWD|PWD|CREDENTIAL)=['\"]?([^\s'\"]{8,})['\"]?",
            re.IGNORECASE,
        ),
    ),
    # Slack tokens (xoxb-..., xoxp-...)
    (
        "Slack Token",
        re.compile(r"\bxox[baprs]-[0-9]{10,13}-[0-9]{10,13}-[a-zA-Z0-9]{24,}\b"),
    ),
    # Google API keys
    (
        "Google API Key",
        re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ),
    # Stripe API keys
    (
        "Stripe API Key",
        re.compile(r"\b[rs]k_live_[0-9a-zA-Z]{24,}\b"),
    ),
    # PocketPaw API keys (pp_...)
    (
        "PocketPaw API Key",
        re.compile(r"\bpp_[a-zA-Z0-9_\-]{20,}\b"),
    ),
    # PocketPaw OAuth access tokens (ppat_...)
    (
        "PocketPaw OAuth Access Token",
        re.compile(r"\bppat_[a-zA-Z0-9_\-]{20,}\b"),
    ),
    # PocketPaw OAuth refresh tokens (pprt_...)
    (
        "PocketPaw OAuth Refresh Token",
        re.compile(r"\bpprt_[a-zA-Z0-9_\-]{20,}\b"),
    ),
]


def redact_output(text: str) -> str:
    """
    Redact sensitive data from text using pattern matching.

    Args:
        text: The text to scan and redact

    Returns:
        Text with sensitive data replaced by [REDACTED]

    Example:
        >>> redact_output("My API key is sk-abc123def456")
        'My API key is [REDACTED]'
    """
    if not text:
        return text

    redacted = text

    for pattern_name, pattern in REDACT_PATTERNS:
        # Check if pattern has capture groups
        if pattern.groups > 0:
            # For patterns with capture groups, only replace the captured group
            def replace_captured(match):
                result = match.group(0)
                for i in range(1, pattern.groups + 1):
                    group_value = match.group(i)
                    if group_value:
                        result = result.replace(group_value, "[REDACTED]")
                return result

            redacted = pattern.sub(replace_captured, redacted)
        else:
            # For patterns without capture groups, replace entire match
            redacted = pattern.sub("[REDACTED]", redacted)

    return redacted
