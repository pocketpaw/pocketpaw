# Tests for agents/model_router.py
# Created: 2026-02-07

from unittest.mock import MagicMock

import pytest

from pocketclaw.agents.model_router import ModelRouter, ModelSelection, TaskComplexity


@pytest.fixture
def settings():
    mock = MagicMock()
    mock.model_tier_simple = "claude-haiku-4-5-20251001"
    mock.model_tier_moderate = "claude-sonnet-4-5-20250929"
    mock.model_tier_complex = "claude-opus-4-6"
    return mock


@pytest.fixture
def router(settings):
    return ModelRouter(settings)


# ---------------------------------------------------------------------------
# Simple classification
# ---------------------------------------------------------------------------


class TestSimple:
    def test_greeting(self, router):
        result = router.classify("Hi")
        assert result.complexity == TaskComplexity.SIMPLE
        assert "haiku" in result.model.lower()

    def test_hello(self, router):
        result = router.classify("Hello")
        assert result.complexity == TaskComplexity.SIMPLE

    def test_thanks(self, router):
        result = router.classify("Thanks!")
        assert result.complexity == TaskComplexity.SIMPLE

    def test_short_question(self, router):
        result = router.classify("What is Python?")
        assert result.complexity == TaskComplexity.SIMPLE

    def test_reminder_request(self, router):
        result = router.classify("Remind me to call mom")
        assert result.complexity == TaskComplexity.SIMPLE

    def test_good_morning(self, router):
        result = router.classify("Good morning")
        assert result.complexity == TaskComplexity.SIMPLE


# ---------------------------------------------------------------------------
# Complex classification
# ---------------------------------------------------------------------------


class TestComplex:
    def test_plan_request(self, router):
        result = router.classify(
            "Plan the architecture for a microservices system with authentication"
        )
        assert result.complexity == TaskComplexity.COMPLEX
        assert "opus" in result.model.lower()

    def test_debug_request(self, router):
        result = router.classify(
            "Debug and investigate why the login flow is failing with a 500 error. "
            "Check the authentication middleware and database connections."
        )
        assert result.complexity == TaskComplexity.COMPLEX

    def test_refactor_request(self, router):
        result = router.classify(
            "Refactor the user service to use a strategy pattern and analyze "
            "the trade-offs between different approaches"
        )
        assert result.complexity == TaskComplexity.COMPLEX

    def test_very_long_message(self, router):
        # Very long message (>400 chars) defaults to complex
        result = router.classify("a " * 250)
        assert result.complexity == TaskComplexity.COMPLEX

    def test_research_task(self, router):
        result = router.classify(
            "Research the best approach for implementing real-time updates "
            "and provide a comprehensive comparison of WebSockets vs SSE"
        )
        assert result.complexity == TaskComplexity.COMPLEX


# ---------------------------------------------------------------------------
# Moderate classification (default)
# ---------------------------------------------------------------------------


class TestModerate:
    def test_coding_question(self, router):
        result = router.classify("Write a function that reverses a string")
        assert result.complexity == TaskComplexity.MODERATE
        assert "sonnet" in result.model.lower()

    def test_medium_question(self, router):
        result = router.classify("How do I set up a virtual environment in Python?")
        assert result.complexity == TaskComplexity.MODERATE

    def test_file_operation(self, router):
        result = router.classify("Read the file at /home/user/project/main.py")
        assert result.complexity == TaskComplexity.MODERATE


# ---------------------------------------------------------------------------
# ModelSelection dataclass
# ---------------------------------------------------------------------------


def test_model_selection_fields():
    sel = ModelSelection(
        complexity=TaskComplexity.SIMPLE,
        model="claude-haiku-4-5-20251001",
        reason="test",
    )
    assert sel.complexity == TaskComplexity.SIMPLE
    assert sel.model == "claude-haiku-4-5-20251001"
    assert sel.reason == "test"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_message(router):
    result = router.classify("")
    assert result.complexity == TaskComplexity.MODERATE


def test_whitespace_message(router):
    result = router.classify("   ")
    assert result.complexity == TaskComplexity.MODERATE
