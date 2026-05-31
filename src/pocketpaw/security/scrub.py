# Scrubbers for params, commands, and audit events — used to keep secrets
# out of audit logs, system-logger fallbacks, and dangerous-command records.
# Added: 2026-04-16 for security cluster C (#890, #893).

from __future__ import annotations

import re
from typing import Any

from pocketpaw.credentials import SECRET_FIELDS

_MASK = "***"

# Field-name heuristics. Anything matching these patterns gets masked, on
# top of the explicit SECRET_FIELDS list from credentials.py.
_SECRET_NAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i).*api[_-]?key$"),
    re.compile(r"(?i).*token$"),
    re.compile(r"(?i).*secret$"),
    re.compile(r"(?i).*password$"),
    re.compile(r"(?i)^authorization$"),
)

# Inline-secret patterns used by scrub_command. We mask the value while
# leaving the surrounding text intact so operators can still read what a
# blocked command was trying to do.
_INLINE_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Bearer / Token "Authorization" header values
    (re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._\-]{8,}"), rf"\g<1>{_MASK}"),
    (re.compile(r"(?i)(Token\s+)[A-Za-z0-9._\-]{8,}"), rf"\g<1>{_MASK}"),
    # OpenAI (sk-, sk-proj-) / Anthropic (sk-ant-) / generic sk-* API keys
    (re.compile(r"sk-(?:proj-|ant-|live-)?[A-Za-z0-9_\-]{10,}"), _MASK),
    # Slack bot / user / app tokens
    (re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}"), _MASK),
    # GitHub tokens (classic + fine-grained)
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), _MASK),
    # PocketPaw API keys + OAuth tokens
    (re.compile(r"pp_[A-Za-z0-9]{20,}"), _MASK),
    (re.compile(r"ppat_[A-Za-z0-9]{20,}"), _MASK),
    # Google OAuth client secrets
    (re.compile(r"GOCSPX-[A-Za-z0-9_\-]{10,}"), _MASK),
    # AWS access key prefix
    (re.compile(r"AKIA[0-9A-Z]{16}"), _MASK),
)


def _looks_like_secret_name(name: str) -> bool:
    if not isinstance(name, str):
        return False
    if name in SECRET_FIELDS:
        return True
    return any(p.match(name) for p in _SECRET_NAME_PATTERNS)


def scrub_params(params: Any) -> Any:
    """Return a copy of ``params`` with any secret-looking fields masked.

    Recurses into nested dicts and lists. Non-dict / non-list inputs are
    returned unchanged so callers can pass arbitrary shapes without a
    pre-check.
    """
    if isinstance(params, dict):
        out: dict[str, Any] = {}
        for key, value in params.items():
            if _looks_like_secret_name(key):
                out[key] = _MASK
            else:
                out[key] = scrub_params(value)
        return out
    if isinstance(params, list):
        return [scrub_params(item) for item in params]
    return params


def scrub_command(command: str) -> str:
    """Return ``command`` with embedded credential-looking substrings masked.

    Leaves the shape of the command intact so a truncated/masked record is
    still useful for triage.
    """
    if not isinstance(command, str):
        return command
    out = command
    for pattern, replacement in _INLINE_SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def scrub_event_dict(event: dict[str, Any]) -> dict[str, Any]:
    """Scrub an audit-event-shaped dict in place-safe fashion.

    Recognises the ``params`` and ``command`` fields specifically, plus
    any other secret-named keys at the top level or within ``context``.
    """
    if not isinstance(event, dict):
        return event
    out: dict[str, Any] = {}
    for key, value in event.items():
        if key == "command" and isinstance(value, str):
            out[key] = scrub_command(value)
        elif key in ("params", "context") and isinstance(value, dict):
            out[key] = scrub_params(value)
        elif _looks_like_secret_name(key):
            out[key] = _MASK
        else:
            out[key] = scrub_params(value) if isinstance(value, (dict, list)) else value
    return out
