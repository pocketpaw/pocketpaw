---
{
  "title": "Output Redaction Tests: API Keys, Tokens, and Secret Pattern Detection",
  "summary": "PocketPaw's output redaction module (`pocketpaw.security.redact`) scans agent output for API keys, tokens, credentials, and other secrets before they are logged or sent to external systems. These tests validate detection of over a dozen secret patterns across major providers, verify that safe text is not falsely flagged, and confirm the specific scenario of an agent reading a `.env` file.",
  "concepts": [
    "output redaction",
    "API key detection",
    "secret scanning",
    "bearer token",
    "AWS access key",
    "JWT token",
    "Stripe key",
    "GitHub token",
    ".env file",
    "false positive prevention",
    "context preservation"
  ],
  "categories": [
    "testing",
    "security",
    "secret management",
    "test"
  ],
  "source_docs": [
    "32046696ccec496d"
  ],
  "backlinks": null,
  "word_count": 443,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

An agent that can read files can accidentally read a `.env` file and then output its contents in a response or log entry. Without output redaction, this would expose API keys, database passwords, and OAuth tokens. The redact module is the last line of defense before agent output leaves the process.

## Secret Pattern Coverage

`TestRedactOutput` validates detection and redaction of:

- **OpenAI keys** (`sk-...`)
- **OpenRouter keys**
- **Anthropic keys** (`sk-ant-...`)
- **AWS access keys** (`AKIA...`) and **AWS secret keys** (40-char base64 strings)
- **Bearer tokens** in `Authorization:` headers
- **Basic auth URLs** (`https://user:password@host`)
- **GitHub tokens** (`ghp_...`, `ghs_...`)
- **PEM private key headers** (`-----BEGIN PRIVATE KEY-----`)
- **JWT tokens** (three-part base64 structure)
- **Environment variable assignments** (`SECRET_KEY=<value>`)
- **Slack tokens** (`xoxb-...`, `xoxp-...`)
- **Google API keys** (`AIza...`)
- **Stripe keys** (`sk_live_...`, `sk_test_...`)
- **Generic `api_key=` and `token=` URL parameters**

Each pattern has its own test to ensure a pattern regression is immediately visible rather than hidden in a combined test.

## Context Preservation

`test_preserve_context_around_secrets` verifies that redaction replaces only the secret value, not surrounding text:

```
Input:  "My API key is sk-abcd1234 and the project is called foo"
Output: "My API key is [REDACTED] and the project is called foo"
```

Full-line redaction would destroy context needed for debugging. Precise redaction preserves the log entry's usefulness.

## False Positive Prevention

`test_no_false_positives_on_safe_text` validates that ordinary prose, code identifiers, and URLs without credentials do not trigger redaction. False positives would silently corrupt agent output in ways that are hard to debug.

## Edge Cases

- **Empty string**: returns empty string, no crash.
- **`None` input**: handled gracefully (returns `None` or empty string).
- **Multiple secrets in one text**: all are redacted, not just the first match.
- **Case-insensitive patterns**: `API_KEY=`, `api_key=`, and `Api_Key=` all match.

## The .env File Scenario

`test_redact_env_file_content` and `test_agent_cat_env_file_scenario` explicitly test the dangerous scenario:

```python
def test_agent_cat_env_file_scenario(self):
    env_output = """OPENAI_API_KEY=sk-abc123\nDATABASE_URL=postgres://user:pass@host/db"""
    redacted = redact_output(env_output)
    assert "sk-abc123" not in redacted
    assert "pass" not in redacted
```

This test documents a known attack vector: if an agent is asked to debug environment issues, it might run `cat .env` and display the output. The redact layer ensures the actual values never appear in the response.

## Known Gaps

- No test for secrets that span multiple lines (e.g., multi-line PEM keys beyond the header).
- No test for secrets embedded in JSON values (`{"api_key": "sk-..."}`). JSON value context may not be recognized by the current patterns.
- No test for base64-encoded secrets — an attacker could encode a key to bypass regex patterns.
- No test for very short secrets that might appear in normal words (minimum length thresholds are untested).