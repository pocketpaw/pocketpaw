---
{
  "title": "Logging Setup: Rich Console Output with Secret and PII Scrubbing",
  "summary": "`logging_setup.py` configures PocketPaw's logging infrastructure with Rich-formatted console output and two security filters: `SecretFilter` (scrubs API key patterns from all log messages) and `PIILogFilter` (opt-in scrubbing of personally identifiable information). These filters prevent credential and user data leakage through log output regardless of log level.",
  "concepts": [
    "logging",
    "Rich",
    "SecretFilter",
    "PIILogFilter",
    "log scrubbing",
    "API key redaction",
    "PII",
    "logging.Filter",
    "setup_logging",
    "security"
  ],
  "categories": [
    "logging",
    "security",
    "observability",
    "developer tooling"
  ],
  "source_docs": [
    "195d37b29530e967"
  ],
  "backlinks": null,
  "word_count": 469,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw handles API keys, user messages, and potentially personal data. Without log filtering, a `DEBUG`-level log line might print a user's conversation or an API key — either in development terminals or production log aggregators. `logging_setup.py` addresses this at the framework level so individual modules don't need to manually redact sensitive data before logging.

## Rich Console Handler

`setup_logging()` replaces the default `StreamHandler` with Rich's `RichHandler`, which provides:
- Syntax-highlighted log levels (INFO in green, WARNING in yellow, ERROR in red)
- Timestamps with millisecond precision
- Automatic line wrapping and indentation for long messages
- Clickable file paths in supported terminals

The visual clarity matters because PocketPaw is a developer-facing tool — log output is a primary debugging surface. Unformatted logs in a terminal with tool calls, LLM responses, and event bus events would be nearly unreadable.

## SecretFilter

`SecretFilter` is a `logging.Filter` that intercepts every log record before it reaches the handler. It applies regex patterns to `record.getMessage()` and replaces matches with `[REDACTED]`.

The patterns target common API key formats:
- `sk-...` (OpenAI)
- `sk-ant-...` (Anthropic)
- Long alphanumeric strings prefixed by known credential markers

The filter operates on both the formatted message and the raw `msg` attribute, because `record.getMessage()` interpolates `%s` args — if an API key appears in the args rather than the message template, naive pattern matching on `record.msg` would miss it.

## PIILogFilter

`PIILogFilter` provides opt-in PII scrubbing. Unlike `SecretFilter` (always active), PII scrubbing is controlled by a settings flag so that development environments can see full user data for debugging while production deployments redact it.

PII patterns include email addresses, phone numbers, and name-like strings adjacent to known field names. The filter is added to the handler chain only when PII scrubbing is enabled.

## Why Filters, Not Formatters

A `logging.Formatter` runs after the handler decision (level filtering, propagation). A `logging.Filter` runs before the message is emitted. Using filters ensures that even `DEBUG`-level messages with API keys are scrubbed before any handler — file handler, network handler, or console — can emit them.

## Usage

```python
from pocketpaw.logging_setup import setup_logging

setup_logging(level="INFO")
```

`setup_logging()` is called once at application startup (in `main()` or the FastAPI lifespan). Subsequent calls to `logging.getLogger(__name__)` in any module inherit the configured handler and filters automatically.

## Known Gaps

- **Regex-based scrubbing has false positive risk**: an email address that happens to match an API key pattern, or a user message containing a long alphanumeric string, could be incorrectly redacted. This would truncate legitimate log messages without warning.
- **`PIILogFilter` is described as opt-in but the opt-in mechanism is not shown**: the module doc mentions a settings flag, but the implementation detail is not visible in the AST. If the flag is checked at filter-add time rather than per-record, toggling it at runtime requires a logging reconfiguration.