# PII Detection and Masking Protocol Layer.
# Created: 2026-02-16
# Part of security hardening — detects and masks PII in messages, logs, and memory.
# Follows injection_scanner.py patterns: pre-compiled regex, dataclass results, singleton.

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class PIIType(str, Enum):
    """Recognized PII categories."""

    SSN = "ssn"
    EMAIL = "email"
    PHONE = "phone"
    CREDIT_CARD = "credit_card"
    IP_ADDRESS = "ip_address"
    DATE_OF_BIRTH = "date_of_birth"


class PIIAction(str, Enum):
    """Action to take when PII is detected."""

    LOG = "log"  # Flag in audit only, don't modify text
    MASK = "mask"  # Replace with [REDACTED-TYPE]
    HASH = "hash"  # Replace with sha256 partial


@dataclass
class PIIMatch:
    """A single PII detection result."""

    pii_type: PIIType
    original: str
    start: int
    end: int
    replacement: str
    action: PIIAction


@dataclass
class PIIScanResult:
    """Result of a PII scan on a text string."""

    original_text: str
    sanitized_text: str
    matches: list[PIIMatch] = field(default_factory=list)
    scan_source: str = "unknown"

    @property
    def has_pii(self) -> bool:
        return len(self.matches) > 0

    @property
    def pii_types_found(self) -> set[PIIType]:
        return {m.pii_type for m in self.matches}


# ---------------------------------------------------------------------------
# Pre-compiled regex patterns for common PII types
# Format: (pattern_str, PIIType, re_flags)
# ---------------------------------------------------------------------------
_PII_PATTERNS: list[tuple[str, PIIType, int]] = [
    # SSN: 123-45-6789 (dashed format only — bare 9-digit has too many false positives)
    (r"\b\d{3}-\d{2}-\d{4}\b", PIIType.SSN, 0),
    # Email addresses
    (r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b", PIIType.EMAIL, 0),
    # US phone: (555) 123-4567, 555-123-4567, 555.123.4567, +1 555-123-4567
    (r"\b\+?1?[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b", PIIType.PHONE, 0),
    # International phone: +44 7911 123456, +91-98765-43210
    (r"\b\+\d{1,3}[-.\s]?\d{4,14}\b", PIIType.PHONE, 0),
    # Visa
    (r"\b4\d{3}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b", PIIType.CREDIT_CARD, 0),
    # MasterCard
    (r"\b5[1-5]\d{2}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b", PIIType.CREDIT_CARD, 0),
    # Amex
    (r"\b3[47]\d{2}[-\s]?\d{6}[-\s]?\d{5}\b", PIIType.CREDIT_CARD, 0),
    # Discover
    (r"\b6(?:011|5\d{2})[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b", PIIType.CREDIT_CARD, 0),
    # IPv4 addresses
    (
        r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b",
        PIIType.IP_ADDRESS,
        0,
    ),
    # Date of birth (only near context keywords to reduce false positives)
    (
        r"\b(?:born|dob|birthday|date of birth)\b.{0,20}\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b",
        PIIType.DATE_OF_BIRTH,
        re.IGNORECASE,
    ),
]

_COMPILED_PII: list[tuple[re.Pattern, PIIType]] = [
    (re.compile(p, flags) if flags else re.compile(p), pii_type)
    for p, pii_type, flags in _PII_PATTERNS
]


class PIIScanner:
    """Regex-based PII detection and masking.

    Pre-compiled patterns detect common PII types. Each type can have a
    configurable action: log (flag only), mask ([REDACTED-TYPE]), or
    hash (sha256 partial).
    """

    def __init__(
        self,
        default_action: PIIAction = PIIAction.MASK,
        type_actions: dict[PIIType, PIIAction] | None = None,
    ):
        self._default_action = default_action
        self._type_actions = type_actions or {}

    def _get_action(self, pii_type: PIIType) -> PIIAction:
        return self._type_actions.get(pii_type, self._default_action)

    @staticmethod
    def _apply_action(match_text: str, pii_type: PIIType, action: PIIAction) -> str:
        if action == PIIAction.LOG:
            return match_text
        elif action == PIIAction.HASH:
            h = hashlib.sha256(match_text.encode()).hexdigest()[:12]
            return f"[PII-{pii_type.value.upper()}:{h}]"
        # MASK is the default / fallback
        return f"[REDACTED-{pii_type.value.upper()}]"

    def scan(self, text: str, source: str = "unknown") -> PIIScanResult:
        """Synchronous regex-based PII scan.

        Returns PIIScanResult with matches and sanitized text.
        Thread-safe (no shared mutable state modified).
        """
        if not text:
            return PIIScanResult(original_text=text, sanitized_text=text, scan_source=source)

        matches: list[PIIMatch] = []

        for pattern, pii_type in _COMPILED_PII:
            for m in pattern.finditer(text):
                action = self._get_action(pii_type)
                replacement = self._apply_action(m.group(), pii_type, action)
                matches.append(
                    PIIMatch(
                        pii_type=pii_type,
                        original=m.group(),
                        start=m.start(),
                        end=m.end(),
                        replacement=replacement,
                        action=action,
                    )
                )

        # Deduplicate overlapping ranges, apply replacements in reverse order
        sorted_matches = sorted(matches, key=lambda m: m.start, reverse=True)
        seen_ranges: set[tuple[int, int]] = set()
        deduped: list[PIIMatch] = []
        sanitized = text

        for match in sorted_matches:
            key = (match.start, match.end)
            if key not in seen_ranges:
                seen_ranges.add(key)
                deduped.append(match)
                if match.action != PIIAction.LOG:
                    sanitized = (
                        sanitized[: match.start] + match.replacement + sanitized[match.end :]
                    )

        return PIIScanResult(
            original_text=text,
            sanitized_text=sanitized,
            matches=list(reversed(deduped)),
            scan_source=source,
        )


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------
_scanner: PIIScanner | None = None


def get_pii_scanner() -> PIIScanner:
    """Get the singleton PII scanner, configured from settings."""
    global _scanner
    if _scanner is None:
        try:
            from pocketpaw.config import get_settings

            settings = get_settings()
            _scanner = PIIScanner(
                default_action=PIIAction(settings.pii_default_action),
                type_actions=_parse_type_actions(settings.pii_type_actions),
            )
        except Exception:
            # Fallback if settings not available (e.g. during early import)
            _scanner = PIIScanner()
    return _scanner


def reset_pii_scanner() -> None:
    """Reset the singleton (for testing)."""
    global _scanner
    _scanner = None


def _parse_type_actions(raw: dict[str, str]) -> dict[PIIType, PIIAction]:
    """Parse type_actions dict from config (e.g. {"email": "mask", "ssn": "hash"})."""
    result: dict[PIIType, PIIAction] = {}
    for type_str, action_str in raw.items():
        try:
            result[PIIType(type_str)] = PIIAction(action_str)
        except ValueError:
            logger.warning("Unknown PII config: type=%s action=%s", type_str, action_str)
    return result
