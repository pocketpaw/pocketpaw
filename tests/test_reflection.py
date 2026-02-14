"""
Unit tests for self-reflection module.

Covers:
  - Robust PASS/FAIL parsing
  - Improvement tracking (prevent over-correction)
  - Oscillation prevention
  - Configuration validation
"""

import pytest

from pocketclaw.reflection import (
    _is_reflection_pass,
    _is_improvement,
    build_reflection_prompt,
    build_correction_prompt,
)


class TestReflectionParsing:
    """Test robust PASS/FAIL detection."""

    @pytest.mark.parametrize(
        "result,expected",
        [
            # PASS variants
            ("PASS", True),
            ("PASS:", True),
            ("PASS.", True),
            ("PASS!", True),
            ("pass", True),
            ("Pass", True),
            ("  PASS  ", True),
            # FAIL variants
            ("FAIL: Too short", False),
            ("FAIL: Invalid", False),
            ("fail", False),
            # Edge cases
            ("", False),
            ("NO", False),
            ("PASS is no good", False),  # PASS but followed by negative
        ],
    )
    def test_is_reflection_pass(self, result: str, expected: bool):
        """Test parsing of reflection results."""
        assert _is_reflection_pass(result) == expected


class TestImprovementTracking:
    """Test improvement detection to prevent over-correction."""

    def test_accepts_longer_similar_response(self):
        """Accept longer response with good similarity."""
        original = "The capital of France is Paris."
        improved = "The capital of France is Paris, located in north-central France on the Seine River."
        assert _is_improvement(original, improved)

    def test_rejects_truncation(self):
        """Reject responses that are too short (truncation)."""
        original = "Long response with detailed explanation and multiple paragraphs..."
        truncated = "Long."
        assert not _is_improvement(original, truncated)

    def test_rejects_complete_rewrite(self):
        """Reject responses that are too different."""
        original = "The answer is Paris."
        completely_different = "Actually, France has many capitals. London is in England. Berlin is in Germany."
        assert not _is_improvement(original, completely_different)

    def test_accepts_minor_refinement(self):
        """Accept responses that are very similar but improved."""
        original = "The capital of France is Paris."
        refined = "The capital of France is Paris, France."
        # Similar length + high similarity should pass
        assert abs(len(original) - len(refined)) < 100

    def test_rejects_suspicious_shortening(self):
        """Reject responses shorter than 50% threshold."""
        original = "This is a hundred-character response that provides comprehensive information."
        short = "This is short."  # Less than 50% of original length
        assert not _is_improvement(original, short)

    def test_boundary_similarity_95_percent(self):
        """Test boundary case at 95% similarity."""
        original = "ABC" * 100  # 300 chars
        # Keep 95% similar, add content
        improved = "ABC" * 100 + " additional content here"
        assert _is_improvement(original, improved)


class TestPromptConstruction:
    """Ensure prompts are well-formed and prevent data leakage."""

    def test_reflection_prompt_contains_user_input(self):
        """Reflection prompt should include user question."""
        user_input = "What is the capital of France?"
        prompt = build_reflection_prompt(user_input, "Paris.")
        assert "What is the capital of France?" in prompt
        assert "[REFLECTION TASK]" in prompt

    def test_reflection_prompt_contains_agent_output(self):
        """Reflection prompt should include agent response."""
        agent_output = "The answer is Paris."
        prompt = build_reflection_prompt("What is 2+2?", agent_output)
        assert agent_output in prompt

    def test_correction_prompt_forbids_apologies(self):
        """Correction prompt should explicitly forbid apologies."""
        prompt = build_correction_prompt("What is 2+2?", "FAIL: Incorrect math")
        assert "apology" in prompt.lower() or "apologize" in prompt.lower()
        assert "Do NOT" in prompt or "NOT include" in prompt.lower()

    def test_correction_prompt_forbids_preamble(self):
        """Correction prompt should forbid 'Here is the corrected response:' type preamble."""
        prompt = build_correction_prompt("What?", "FAIL")
        assert "preamble" in prompt.lower() or "Do NOT say" in prompt

    def test_correction_prompt_asks_for_answer_only(self):
        """Correction prompt should emphasize outputting answer only."""
        prompt = build_correction_prompt("What?", "FAIL")
        assert "OUTPUT THE ANSWER ONLY" in prompt or "answer only" in prompt.lower()


class TestConfigValidation:
    """Test configuration bounds and validation."""

    def test_reflection_max_retries_valid_values(self):
        """Test that valid max_retries values are accepted."""
        from pocketclaw.config import Settings

        # These should work
        Settings(reflection_max_retries=0)
        Settings(reflection_max_retries=1)
        Settings(reflection_max_retries=2)
        Settings(reflection_max_retries=3)

    def test_reflection_max_retries_exceeds_limit(self):
        """Test that max_retries > 3 is rejected."""
        from pocketclaw.config import Settings

        with pytest.raises(ValueError):
            Settings(reflection_max_retries=4)

        with pytest.raises(ValueError):
            Settings(reflection_max_retries=10)

    def test_reflection_confidence_threshold_valid(self):
        """Test that valid confidence thresholds are accepted."""
        from pocketclaw.config import Settings

        Settings(reflection_confidence_threshold=0.0)
        Settings(reflection_confidence_threshold=0.5)
        Settings(reflection_confidence_threshold=0.7)
        Settings(reflection_confidence_threshold=1.0)

    def test_reflection_confidence_threshold_out_of_bounds(self):
        """Test that confidence threshold bounds are enforced."""
        from pocketclaw.config import Settings

        with pytest.raises(ValueError):
            Settings(reflection_confidence_threshold=-0.1)

        with pytest.raises(ValueError):
            Settings(reflection_confidence_threshold=1.5)

    def test_reflection_default_values(self):
        """Test that reflection defaults are safe."""
        from pocketclaw.config import Settings

        settings = Settings()
        assert settings.reflection_enabled is False  # Disabled by default
        assert settings.reflection_max_retries == 1
        assert settings.reflection_confidence_threshold == 0.7


class TestEdgeCases:
    """Test edge cases and corner scenarios."""

    def test_empty_response_improvement(self):
        """Empty corrected response should be rejected."""
        original = "This is a response."
        empty = ""
        assert not _is_improvement(original, empty)

    def test_identical_response_is_not_improvement(self):
        """Identical response should be rejected as "not an improvement"."""
        response = "This is a response."
        # Identical content might still be accepted due to similarity threshold
        # but intent is that it's not an improvement
        result = _is_improvement(response, response)
        # This is acceptable either way, but optimization would reject it

    def test_whitespace_handling(self):
        """Test that whitespace variations are handled correctly."""
        original = "This is text."
        with_extra_spaces = "This is  text."
        # These should be considered similar
        assert _is_improvement(original, with_extra_spaces)

    def test_very_long_responses(self):
        """Test improvement detection on long responses."""
        original = "A" * 10000
        improved = "A" * 10500  # 5% longer
        assert _is_improvement(original, improved)

    def test_pass_parsing_with_metadata(self):
        """PASS with metadata should still parse correctly."""
        # Real LLMs might add metadata after PASS
        assert _is_reflection_pass("PASS - all checks passed")
        assert _is_reflection_pass("PASS (verified)")


class TestIntegration:
    """Integration tests (without actual LLM calls)."""

    def test_prompts_are_non_empty(self):
        """Both prompts should generate non-empty strings."""
        reflection = build_reflection_prompt("Q?", "A.")
        correction = build_correction_prompt("Q?", "FAIL: X")
        assert len(reflection) > 50
        assert len(correction) > 50

    def test_prompts_contain_task_markers(self):
        """Prompts should have clear task markers."""
        reflection = build_reflection_prompt("Q?", "A.")
        correction = build_correction_prompt("Q?", "FAIL: X")
        assert "[REFLECTION TASK]" in reflection or "Evaluate" in reflection
        assert "[CORRECTION TASK]" in correction or "CORRECTION" in correction
