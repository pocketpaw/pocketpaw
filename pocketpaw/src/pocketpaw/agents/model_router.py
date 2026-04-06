# Smart Model Router — heuristic classifier for automatic model selection.
# Created: 2026-02-07
# Part of Phase 2 Integration Ecosystem

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum

from pocketpaw.config import Settings

logger = logging.getLogger(__name__)


class TaskComplexity(StrEnum):
    SIMPLE = "simple"  # Haiku: greetings, simple facts
    MODERATE = "moderate"  # Sonnet: coding, analysis
    COMPLEX = "complex"  # Opus: multi-step reasoning, planning


@dataclass
class ModelSelection:
    """Result of model routing decision."""

    complexity: TaskComplexity
    model: str
    reason: str


# ---------------------------------------------------------------------------
# Signal patterns — no API call, pure heuristic
# ---------------------------------------------------------------------------

_SIMPLE_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"^(hi|hello|hey|thanks|thank you|bye|goodbye|ok|yes|no|sure)[.!?\s]*$",
        r"^(good morning|good evening|good night|how are you)[.!?\s]*$",
    ]
]

# Patterns that suggest a message needs tools even if it looks simple.
# These prevent false-positive SIMPLE classification for questions that
# require web search, code execution, or file creation.
_NEEDS_TOOLS_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\b(stock|price|market|forecast|predict|data)\b",
        r"\b(create|make|build|generate|write)\b.*(file|excel|csv|chart|report|document)",
        r"\b(search|find|look up|google|browse)\b",
        r"\b(install|download|fetch|scrape|extract)\b",
        r"\b(run|execute|calculate|compute|code)\b",
        r"\b(send|email|message|post|upload)\b",
        r"\b(remind me|set.*reminder)\b",
        r"\?(.*\b(how|why|explain|what does|how does)\b)",
    ]
]

_COMPLEX_SIGNALS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\b(plan|architect|design|strategy|refactor)\b",
        r"\b(debug|investigate|diagnose|root\s*cause)\b",
        r"\b(implement|build|create) .{20,}",
        r"\b(analyze|compare|evaluate|trade-?off)\b",
        r"\b(multi-?step|step.by.step|detailed)\b",
        r"\b(optimize|performance|scale|security audit)\b",
        r"\b(research|deep dive|comprehensive)\b",
    ]
]

# Short messages are likely simple
_SHORT_THRESHOLD = 30
# Long messages are likely complex
_LONG_THRESHOLD = 200


class ModelRouter:
    """Heuristic-based model router for automatic complexity classification.

    Rules:
    - Short messages + simple patterns -> SIMPLE (Haiku)
    - Complex signals (plan, debug, architect) + long messages -> COMPLEX (Opus)
    - Default -> MODERATE (Sonnet)
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    def classify(self, message: str) -> ModelSelection:
        """Classify a message and return the recommended model.

        Returns ModelSelection with complexity, model name, and reason.
        """
        message = message.strip()
        msg_len = len(message)

        # Empty / whitespace-only → trivially simple
        if msg_len == 0:
            return ModelSelection(
                complexity=TaskComplexity.SIMPLE,
                model=self.settings.model_tier_simple,
                reason="Empty message",
            )

        # Check if the message needs tools (web search, code execution, etc.)
        # This takes priority over simple patterns to avoid stripping tools
        # from questions like "what is Apple's stock price?"
        needs_tools = any(p.search(message) for p in _NEEDS_TOOLS_PATTERNS)

        # Check complex signals first (so short technical messages stay complex)
        complex_hits = sum(1 for p in _COMPLEX_SIGNALS if p.search(message))

        if complex_hits >= 2 or (complex_hits >= 1 and msg_len > _SHORT_THRESHOLD):
            return ModelSelection(
                complexity=TaskComplexity.COMPLEX,
                model=self.settings.model_tier_complex,
                reason=f"{complex_hits} complex signal(s), message length {msg_len}",
            )

        # Very long messages default to complex
        if msg_len > _LONG_THRESHOLD * 2:
            return ModelSelection(
                complexity=TaskComplexity.COMPLEX,
                model=self.settings.model_tier_complex,
                reason=f"Very long message ({msg_len} chars)",
            )

        # Check explicit simple patterns (only pure greetings/acknowledgments)
        # Never classify as SIMPLE if the message needs tools
        if msg_len <= _SHORT_THRESHOLD and not needs_tools:
            for pattern in _SIMPLE_PATTERNS:
                if pattern.search(message):
                    return ModelSelection(
                        complexity=TaskComplexity.SIMPLE,
                        model=self.settings.model_tier_simple,
                        reason="Short greeting/acknowledgment",
                    )

        # Default: moderate
        return ModelSelection(
            complexity=TaskComplexity.MODERATE,
            model=self.settings.model_tier_moderate,
            reason="Default moderate complexity",
        )
