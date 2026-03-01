from __future__ import annotations

import re

# NOTE:
# Keep these patterns in sync with other redaction logic (for example
# PocketPaw Native's orchestrator) so that secrets are treated
# consistently across backends.
_REDACT_PATTERNS: list[str] = [
    r"(sk-[a-zA-Z0-9]{20,})",  # OpenAI / Anthropic-style keys
    r"(AKIA[A-Z0-9]{16})",  # AWS access key
    r"(ghp_[a-zA-Z0-9]{36})",  # GitHub token
    r"(xox[baprs]-[a-zA-Z0-9-]+)",  # Slack token
    r"password[\"']?\s*[:=]\s*[\"']([^\"']+)",  # password = "..."
    r"api[_-]?key[\"']?\s*[:=]\s*[\"']([^\"']+)",  # api_key = "..."
]


def redact_output(text: str) -> str:
    """Redact sensitive information from LLM output.

    This is intentionally conservative and pattern-based – it does not try
    to understand context, only remove obvious key / token shapes.
    """

    redacted = text
    for pattern in _REDACT_PATTERNS:
        redacted = re.sub(pattern, "[REDACTED]", redacted, flags=re.IGNORECASE)
    return redacted

