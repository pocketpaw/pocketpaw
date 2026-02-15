# Smart Model Router — heuristic classifier for automatic model selection.
# Created: 2026-02-07
# Part of Phase 2 Integration Ecosystem

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum

from pocketpaw.config import Settings

logger = logging.getLogger(__name__)


class TaskComplexity(str, Enum):
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
        r"^(hi|hello|hey|thanks|thank you|bye|goodbye|ok|yes|no|sure)\b",
        r"^what (is|are|was|were) .{3,30}\??$",
        r"^(who|when|where) .{3,40}\??$",
        r"^(good morning|good evening|good night|how are you)",
        r"^remind me ",
        r"^(set|create) (a )?reminder",
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

    When agent_backend is 'claude_agent_sdk', only routes between Claude models.
    """

    # Claude model identifiers (official model names and aliases)
    _CLAUDE_MODELS = {
        "claude",  # Matches any claude-* model
        "sonnet",
        "opus",
        "haiku",
        "sonnet[1m]",
        "opusplan",
    }

    def __init__(self, settings: Settings):
        self.settings = settings

    @staticmethod
    def is_claude_model(model: str) -> bool:
        """Check if a model identifier is a Claude model.

        Args:
            model: Model name or alias (e.g., "claude-sonnet-4-5", "sonnet", "gpt-4o")

        Returns:
            True if it's a Claude model
        """
        if not model:
            return False
        model_lower = model.lower()
        # Check if it starts with "claude" or matches known aliases
        return model_lower.startswith("claude") or model_lower in ModelRouter._CLAUDE_MODELS

    def classify(self, message: str) -> ModelSelection:
        """Classify a message and return the recommended model.

        Returns ModelSelection with complexity, model name, and reason.
        """
        message = message.strip()
        msg_len = len(message)

        # Determine complexity and select model
        selected_complexity = TaskComplexity.MODERATE
        selected_model = self.settings.model_tier_moderate
        reason = "Default moderate complexity"

        # Check simple patterns first
        if msg_len <= _SHORT_THRESHOLD:
            for pattern in _SIMPLE_PATTERNS:
                if pattern.search(message):
                    selected_complexity = TaskComplexity.SIMPLE
                    selected_model = self.settings.model_tier_simple
                    reason = "Short message with simple pattern"
                    break

        # Check complex signals (if not already classified as simple)
        if selected_complexity != TaskComplexity.SIMPLE:
            complex_hits = sum(1 for p in _COMPLEX_SIGNALS if p.search(message))

            if complex_hits >= 2 or (complex_hits >= 1 and msg_len > _SHORT_THRESHOLD):
                selected_complexity = TaskComplexity.COMPLEX
                selected_model = self.settings.model_tier_complex
                reason = f"{complex_hits} complex signal(s), message length {msg_len}"
            # Very long messages default to complex
            elif msg_len > _LONG_THRESHOLD * 2:
                selected_complexity = TaskComplexity.COMPLEX
                selected_model = self.settings.model_tier_complex
                reason = f"Very long message ({msg_len} chars)"

        # Validate: When using claude_agent_sdk backend, ensure Claude model is selected
        if self.settings.agent_backend == "claude_agent_sdk":
            if not self.is_claude_model(selected_model):
                # Fallback to safe Claude defaults by complexity
                fallback_map = {
                    TaskComplexity.SIMPLE: "haiku",
                    TaskComplexity.MODERATE: "sonnet",
                    TaskComplexity.COMPLEX: "opus",
                }
                fallback = fallback_map[selected_complexity]
                logger.warning(
                    "Smart routing with claude_agent_sdk: model '%s' is not a Claude model. "
                    "Falling back to '%s' for %s complexity.",
                    selected_model,
                    fallback,
                    selected_complexity.value,
                )
                selected_model = fallback
                reason += f" (fallback to Claude {fallback})"

        return ModelSelection(
            complexity=selected_complexity,
            model=selected_model,
            reason=reason,
        )
