---
{
  "title": "Output Redaction: Preventing Secret Leakage in Agent Responses",
  "summary": "This module provides output-level redaction that strips API keys, tokens, and other credentials from agent response text before it reaches clients. Operating at the message bus level makes it backend-agnostic — the same redaction applies regardless of whether the agent backend is Claude Code, Open Interpreter, or any future addition.",
  "concepts": [
    "output redaction",
    "secret leakage prevention",
    "API key redaction",
    "REDACT_PATTERNS",
    "redact_output",
    "safe_install_error",
    "message bus security",
    "backend-agnostic",
    "credential protection",
    "last-line defense"
  ],
  "categories": [
    "security",
    "agent runtime",
    "api"
  ],
  "source_docs": [
    "957e932a62a87fbc"
  ],
  "backlinks": null,
  "word_count": 415,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Why Output-Level Redaction?

Even well-designed systems can accidentally echo secrets. Consider: an agent executes a shell command that prints the current environment (`env`), which contains an `OPENAI_API_KEY`. Without redaction, that output flows directly to the API response and appears in the user's browser. A user might screenshot it, paste it into a support ticket, or log it client-side.

Output-level redaction is the last line of defense — it catches secrets that escaped all earlier controls.

## Redact Pattern Coverage

`REDACT_PATTERNS` covers the most common credential formats found in practice:

```python
REDACT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("OpenAI API Key", re.compile(r"\bsk-[a-zA-Z0-9]{20,}\b", re.IGNORECASE)),
    ("OpenRouter API Key", re.compile(r"\bsk-or-v1-[a-zA-Z0-9]{12,}\b", re.IGNORECASE)),
    ("Anthropic API Key", re.compile(r"\bsk-ant-[a-zA-Z0-9_-]{95,}\b", re.IGNORECASE)),
    ("AWS Access Key ID", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # ... additional patterns
]
```

Each pattern is paired with a human-readable name so redaction log entries can say "replaced OpenAI API Key" rather than just "replaced secret" — useful for debugging why output was truncated.

## redact_output: The Core Function

`redact_output(text)` iterates all patterns and substitutes matches with a placeholder (e.g., `[REDACTED:OpenAI API Key]`). The replacement includes the secret type name, helping developers understand what was stripped without exposing the value.

The function is designed to be called in the response serialization layer — after the agent backend produces output and before it is written to the API response body.

## safe_install_error: Installer-Specific Redaction

`safe_install_error(stderr)` serves a specific use case: when a skill or package installer fails, its stderr might contain the package manager's reproduction of the install command, which could include credential-bearing URLs (`pip install https://user:TOKEN@private.registry.com/pkg`). This function applies `redact_output` and additionally caps the stderr length before it reaches API clients — preventing both credential leakage and unbounded error payloads.

## Backend-Agnostic Design

The module has no imports from agent-specific code — only `re` from stdlib. This is intentional: redaction sits at the message bus level, so it can be applied uniformly regardless of the agent backend producing the output. Swapping backends does not require updating the redaction layer.

## Known Gaps

- **Pattern coverage is static**: Credentials for new AI providers, private registries, or enterprise systems will not be redacted until patterns are added manually.
- **No binary content handling**: The redactor operates on string text. Binary content or base64-encoded secrets embedded in JSON payloads would not be caught.
- **Replacement is visible to users**: The `[REDACTED:OpenAI API Key]` placeholder tells users that a secret was present. In some contexts it may be preferable to silently replace with whitespace to avoid signaling what was stripped.