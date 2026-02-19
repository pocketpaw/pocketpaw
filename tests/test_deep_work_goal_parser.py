# Tests for Deep Work Goal Parser module.
# Created: 2026-02-18
#
# Tests cover:
#   - GoalAnalysis dataclass: from_dict, to_dict, defaults, properties
#   - GoalParser.parse_raw(): valid JSON, fenced JSON, invalid input
#   - GoalParser._strip_code_fences(): edge cases
#   - Validation helpers: domain, complexity, research depth, clamp
#   - GoalParser.parse(): full flow with mocked _run_prompt

import json
from unittest.mock import MagicMock, patch

import pytest

from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.deep_work.goal_parser import (
    VALID_COMPLEXITIES,
    VALID_DOMAINS,
    VALID_RESEARCH_DEPTHS,
    GoalAnalysis,
    GoalParser,
    _clamp,
    _sanitize_str_list,
    _validate_complexity,
    _validate_domain,
    _validate_research_depth,
)

# ============================================================================
# Sample data
# ============================================================================

VALID_GOAL_JSON = json.dumps(
    {
        "goal": "Build a REST API for a todo application",
        "domain": "code",
        "sub_domains": ["web-development", "python", "fastapi"],
        "complexity": "M",
        "estimated_phases": 4,
        "ai_capabilities": ["Generate boilerplate code", "Write tests", "Create API docs"],
        "human_requirements": ["Decide on database schema", "Provide deployment credentials"],
        "constraints_detected": ["No budget mentioned"],
        "clarifications_needed": ["Which database do you prefer?"],
        "suggested_research_depth": "quick",
        "confidence": 0.85,
    }
)

CREATIVE_GOAL_JSON = json.dumps(
    {
        "goal": "Write a children's book about space exploration",
        "domain": "creative",
        "sub_domains": ["writing", "illustration-prompts"],
        "complexity": "L",
        "estimated_phases": 6,
        "ai_capabilities": ["Draft story outline", "Generate illustration prompts"],
        "human_requirements": ["Final story approval", "Hire illustrator"],
        "constraints_detected": [],
        "clarifications_needed": ["Target age group?", "Preferred art style?"],
        "suggested_research_depth": "standard",
        "confidence": 0.72,
    }
)


# ============================================================================
# GoalAnalysis dataclass tests
# ============================================================================


class TestGoalAnalysisDefaults:
    """Test GoalAnalysis default values."""

    def test_default_fields(self):
        analysis = GoalAnalysis()
        assert analysis.goal == ""
        assert analysis.domain == "code"
        assert analysis.sub_domains == []
        assert analysis.complexity == "M"
        assert analysis.estimated_phases == 1
        assert analysis.ai_capabilities == []
        assert analysis.human_requirements == []
        assert analysis.constraints_detected == []
        assert analysis.clarifications_needed == []
        assert analysis.suggested_research_depth == "standard"
        assert analysis.confidence == 0.7

    def test_needs_clarification_false(self):
        analysis = GoalAnalysis()
        assert analysis.needs_clarification is False

    def test_needs_clarification_true(self):
        analysis = GoalAnalysis(clarifications_needed=["What framework?"])
        assert analysis.needs_clarification is True

    def test_domain_label(self):
        assert GoalAnalysis(domain="code").domain_label == "Software & Code"
        assert GoalAnalysis(domain="business").domain_label == "Business & Strategy"
        assert GoalAnalysis(domain="creative").domain_label == "Creative & Content"
        assert GoalAnalysis(domain="education").domain_label == "Learning & Education"
        assert GoalAnalysis(domain="events").domain_label == "Events & Logistics"
        assert GoalAnalysis(domain="home").domain_label == "Home & Physical"
        assert GoalAnalysis(domain="hybrid").domain_label == "Multi-Domain"

    def test_domain_label_unknown_fallback(self):
        analysis = GoalAnalysis(domain="unknown")
        assert analysis.domain_label == "Unknown"


class TestGoalAnalysisFromDict:
    """Test GoalAnalysis.from_dict() with various inputs."""

    def test_valid_code_goal(self):
        data = json.loads(VALID_GOAL_JSON)
        analysis = GoalAnalysis.from_dict(data)
        assert analysis.goal == "Build a REST API for a todo application"
        assert analysis.domain == "code"
        assert analysis.sub_domains == ["web-development", "python", "fastapi"]
        assert analysis.complexity == "M"
        assert analysis.estimated_phases == 4
        assert len(analysis.ai_capabilities) == 3
        assert len(analysis.human_requirements) == 2
        assert analysis.suggested_research_depth == "quick"
        assert analysis.confidence == 0.85

    def test_valid_creative_goal(self):
        data = json.loads(CREATIVE_GOAL_JSON)
        analysis = GoalAnalysis.from_dict(data)
        assert analysis.domain == "creative"
        assert analysis.complexity == "L"
        assert analysis.estimated_phases == 6
        assert len(analysis.clarifications_needed) == 2

    def test_empty_dict(self):
        analysis = GoalAnalysis.from_dict({})
        assert analysis.goal == ""
        assert analysis.domain == "code"
        assert analysis.complexity == "M"
        assert analysis.estimated_phases == 1
        assert analysis.confidence == 0.7

    def test_invalid_domain_falls_back_to_hybrid(self):
        analysis = GoalAnalysis.from_dict({"domain": "cooking"})
        assert analysis.domain == "hybrid"

    def test_invalid_complexity_falls_back_to_m(self):
        analysis = GoalAnalysis.from_dict({"complexity": "XXL"})
        assert analysis.complexity == "M"

    def test_invalid_research_depth_falls_back_to_standard(self):
        analysis = GoalAnalysis.from_dict({"suggested_research_depth": "extreme"})
        assert analysis.suggested_research_depth == "standard"

    def test_estimated_phases_clamped_low(self):
        analysis = GoalAnalysis.from_dict({"estimated_phases": -5})
        assert analysis.estimated_phases == 1

    def test_estimated_phases_clamped_high(self):
        analysis = GoalAnalysis.from_dict({"estimated_phases": 50})
        assert analysis.estimated_phases == 10

    def test_confidence_clamped_low(self):
        analysis = GoalAnalysis.from_dict({"confidence": -0.5})
        assert analysis.confidence == 0.0

    def test_confidence_clamped_high(self):
        analysis = GoalAnalysis.from_dict({"confidence": 1.5})
        assert analysis.confidence == 1.0

    def test_clarifications_truncated_to_4(self):
        analysis = GoalAnalysis.from_dict(
            {"clarifications_needed": ["Q1", "Q2", "Q3", "Q4", "Q5", "Q6"]}
        )
        assert len(analysis.clarifications_needed) == 4

    def test_estimated_phases_is_int(self):
        analysis = GoalAnalysis.from_dict({"estimated_phases": 3.7})
        assert isinstance(analysis.estimated_phases, int)
        assert analysis.estimated_phases == 3


class TestGoalAnalysisToDict:
    """Test GoalAnalysis.to_dict() serialization."""

    def test_round_trip(self):
        data = json.loads(VALID_GOAL_JSON)
        analysis = GoalAnalysis.from_dict(data)
        result = analysis.to_dict()
        assert result["goal"] == data["goal"]
        assert result["domain"] == data["domain"]
        assert result["complexity"] == data["complexity"]
        assert result["estimated_phases"] == data["estimated_phases"]
        assert result["confidence"] == data["confidence"]

    def test_default_to_dict(self):
        analysis = GoalAnalysis()
        d = analysis.to_dict()
        assert d["goal"] == ""
        assert d["domain"] == "code"
        assert d["complexity"] == "M"
        assert d["estimated_phases"] == 1
        assert d["suggested_research_depth"] == "standard"
        assert d["confidence"] == 0.7
        assert d["sub_domains"] == []
        assert d["ai_capabilities"] == []
        assert d["human_requirements"] == []
        assert d["constraints_detected"] == []
        assert d["clarifications_needed"] == []


# ============================================================================
# Validation helper tests
# ============================================================================


class TestValidateDomain:
    """Test _validate_domain helper."""

    def test_all_valid_domains(self):
        for domain in VALID_DOMAINS:
            assert _validate_domain(domain) == domain

    def test_case_insensitive(self):
        assert _validate_domain("CODE") == "code"
        assert _validate_domain("Business") == "business"
        assert _validate_domain("CREATIVE") == "creative"

    def test_strips_whitespace(self):
        assert _validate_domain("  code  ") == "code"

    def test_invalid_returns_hybrid(self):
        assert _validate_domain("cooking") == "hybrid"
        assert _validate_domain("") == "hybrid"
        assert _validate_domain("xyz") == "hybrid"


class TestValidateComplexity:
    """Test _validate_complexity helper."""

    def test_all_valid_complexities(self):
        for c in VALID_COMPLEXITIES:
            assert _validate_complexity(c) == c

    def test_case_insensitive(self):
        assert _validate_complexity("s") == "S"
        assert _validate_complexity("xl") == "XL"

    def test_strips_whitespace(self):
        assert _validate_complexity("  M  ") == "M"

    def test_invalid_returns_m(self):
        assert _validate_complexity("XXL") == "M"
        assert _validate_complexity("") == "M"


class TestValidateResearchDepth:
    """Test _validate_research_depth helper."""

    def test_all_valid_depths(self):
        for d in VALID_RESEARCH_DEPTHS:
            assert _validate_research_depth(d) == d

    def test_case_insensitive(self):
        assert _validate_research_depth("DEEP") == "deep"
        assert _validate_research_depth("Quick") == "quick"

    def test_invalid_returns_standard(self):
        assert _validate_research_depth("extreme") == "standard"
        assert _validate_research_depth("") == "standard"


class TestClamp:
    """Test _clamp helper."""

    def test_within_range(self):
        assert _clamp(5, 0, 10) == 5.0

    def test_below_minimum(self):
        assert _clamp(-5, 0, 10) == 0.0

    def test_above_maximum(self):
        assert _clamp(15, 0, 10) == 10.0

    def test_at_boundaries(self):
        assert _clamp(0, 0, 10) == 0.0
        assert _clamp(10, 0, 10) == 10.0

    def test_non_numeric_returns_minimum(self):
        assert _clamp("not a number", 0, 10) == 0
        assert _clamp(None, 1, 10) == 1

    def test_float_input(self):
        assert _clamp(0.85, 0.0, 1.0) == 0.85


# ============================================================================
# GoalParser._strip_code_fences tests
# ============================================================================


class TestStripCodeFences:
    """Test GoalParser._strip_code_fences static method."""

    def test_no_fences(self):
        assert GoalParser._strip_code_fences('{"key": "value"}') == '{"key": "value"}'

    def test_json_fence(self):
        text = '```json\n{"key": "value"}\n```'
        assert GoalParser._strip_code_fences(text) == '{"key": "value"}'

    def test_plain_fence(self):
        text = '```\n{"key": "value"}\n```'
        assert GoalParser._strip_code_fences(text) == '{"key": "value"}'

    def test_surrounding_text(self):
        text = 'Here is the analysis:\n```json\n{"domain": "code"}\n```\nDone.'
        assert GoalParser._strip_code_fences(text) == '{"domain": "code"}'

    def test_empty_string(self):
        assert GoalParser._strip_code_fences("") == ""

    def test_whitespace_only(self):
        assert GoalParser._strip_code_fences("   ") == ""


# ============================================================================
# GoalParser.parse_raw() tests
# ============================================================================


class TestParseRaw:
    """Test GoalParser.parse_raw() with various inputs."""

    def setup_method(self):
        self.parser = GoalParser()

    def test_valid_json(self):
        analysis = self.parser.parse_raw(VALID_GOAL_JSON)
        assert analysis.goal == "Build a REST API for a todo application"
        assert analysis.domain == "code"
        assert analysis.complexity == "M"
        assert analysis.confidence == 0.85

    def test_fenced_json(self):
        fenced = f"```json\n{VALID_GOAL_JSON}\n```"
        analysis = self.parser.parse_raw(fenced)
        assert analysis.goal == "Build a REST API for a todo application"
        assert analysis.domain == "code"

    def test_fenced_with_surrounding_text(self):
        wrapped = f"Here is my analysis:\n```json\n{CREATIVE_GOAL_JSON}\n```\nLet me know."
        analysis = self.parser.parse_raw(wrapped)
        assert analysis.domain == "creative"
        assert analysis.complexity == "L"

    def test_invalid_json_returns_default(self):
        analysis = self.parser.parse_raw("this is not json at all")
        assert analysis.goal == ""
        assert analysis.domain == "code"
        assert analysis.complexity == "M"

    def test_empty_string_returns_default(self):
        analysis = self.parser.parse_raw("")
        assert analysis.goal == ""
        assert analysis.domain == "code"

    def test_json_array_returns_default(self):
        analysis = self.parser.parse_raw('[{"key": "value"}]')
        assert analysis.goal == ""
        assert analysis.domain == "code"

    def test_json_number_returns_default(self):
        analysis = self.parser.parse_raw("42")
        assert analysis.goal == ""

    def test_partial_data(self):
        partial = json.dumps({"goal": "Build something", "domain": "business"})
        analysis = self.parser.parse_raw(partial)
        assert analysis.goal == "Build something"
        assert analysis.domain == "business"
        assert analysis.complexity == "M"  # default
        assert analysis.estimated_phases == 1  # default


# ============================================================================
# GoalParser.parse() integration test (mocked LLM)
# ============================================================================


class TestParseIntegration:
    """Test GoalParser.parse() with mocked _run_prompt."""

    @pytest.mark.asyncio
    async def test_parse_returns_goal_analysis(self):
        parser = GoalParser()

        async def mock_run_prompt(prompt: str) -> str:
            return VALID_GOAL_JSON

        parser._run_prompt = mock_run_prompt

        analysis = await parser.parse("Build a todo REST API")
        assert isinstance(analysis, GoalAnalysis)
        assert analysis.goal == "Build a REST API for a todo application"
        assert analysis.domain == "code"
        assert analysis.complexity == "M"
        assert analysis.confidence == 0.85

    @pytest.mark.asyncio
    async def test_parse_fills_empty_goal_with_input(self):
        parser = GoalParser()

        async def mock_run_prompt(prompt: str) -> str:
            return json.dumps({"domain": "business", "complexity": "L"})

        parser._run_prompt = mock_run_prompt

        analysis = await parser.parse("Plan a product launch")
        assert analysis.goal == "Plan a product launch"
        assert analysis.domain == "business"
        assert analysis.complexity == "L"

    @pytest.mark.asyncio
    async def test_parse_handles_fenced_response(self):
        parser = GoalParser()

        async def mock_run_prompt(prompt: str) -> str:
            return f"Here is the analysis:\n```json\n{CREATIVE_GOAL_JSON}\n```"

        parser._run_prompt = mock_run_prompt

        analysis = await parser.parse("Write a children's book")
        assert analysis.domain == "creative"
        assert analysis.complexity == "L"

    @pytest.mark.asyncio
    async def test_parse_handles_invalid_llm_output(self):
        parser = GoalParser()

        async def mock_run_prompt(prompt: str) -> str:
            return "I couldn't understand the request."

        parser._run_prompt = mock_run_prompt

        analysis = await parser.parse("Do something vague")
        # Should return default analysis with goal filled from input
        assert analysis.goal == "Do something vague"
        assert analysis.domain == "code"  # default
        assert analysis.complexity == "M"  # default

    @pytest.mark.asyncio
    async def test_parse_long_input_truncates_goal(self):
        parser = GoalParser()
        long_input = "x" * 500

        async def mock_run_prompt(prompt: str) -> str:
            return json.dumps({"domain": "code"})

        parser._run_prompt = mock_run_prompt

        analysis = await parser.parse(long_input)
        assert len(analysis.goal) == 200  # truncated to 200 chars

    @pytest.mark.asyncio
    async def test_parse_prompt_contains_user_input(self):
        parser = GoalParser()
        captured_prompt = None

        async def mock_run_prompt(prompt: str) -> str:
            nonlocal captured_prompt
            captured_prompt = prompt
            return VALID_GOAL_JSON

        parser._run_prompt = mock_run_prompt

        await parser.parse("Build a mobile app for cat tracking")
        assert "Build a mobile app for cat tracking" in captured_prompt


# ============================================================================
# GoalParser._run_prompt error handling tests
# ============================================================================


class TestRunPromptErrors:
    """Test _run_prompt error handling with mocked AgentRouter."""

    @pytest.mark.asyncio
    async def test_raises_on_error_only_response(self):
        parser = GoalParser()

        mock_router = MagicMock()

        async def mock_run(prompt):
            yield AgentEvent(type="error", content="API key not configured")

        mock_router.run = mock_run

        with patch("pocketpaw.agents.router.AgentRouter", return_value=mock_router):
            with patch("pocketpaw.config.get_settings"):
                with pytest.raises(RuntimeError, match="API key not configured"):
                    await parser._run_prompt("test prompt")

    @pytest.mark.asyncio
    async def test_returns_content_with_messages(self):
        parser = GoalParser()

        mock_router = MagicMock()

        async def mock_run(prompt):
            yield AgentEvent(type="message", content='{"domain": "code"}')
            yield AgentEvent(type="done", content="")

        mock_router.run = mock_run

        with patch("pocketpaw.agents.router.AgentRouter", return_value=mock_router):
            with patch("pocketpaw.config.get_settings"):
                result = await parser._run_prompt("test prompt")
                assert result == '{"domain": "code"}'


# ============================================================================
# GOAL_PARSE_PROMPT template test
# ============================================================================


class TestGoalParsePrompt:
    """Test GOAL_PARSE_PROMPT template."""

    def test_has_user_input_placeholder(self):
        from pocketpaw.deep_work.prompts import GOAL_PARSE_PROMPT

        assert "{user_input}" in GOAL_PARSE_PROMPT

    def test_can_be_formatted(self):
        from pocketpaw.deep_work.prompts import GOAL_PARSE_PROMPT

        result = GOAL_PARSE_PROMPT.format(user_input="Build a todo app")
        assert "Build a todo app" in result
        assert "{user_input}" not in result

    def test_allows_markdown_fences(self):
        from pocketpaw.deep_work.prompts import GOAL_PARSE_PROMPT

        # Prompt should mention that fences are allowed (not prohibited)
        assert "```json" in GOAL_PARSE_PROMPT


# ============================================================================
# _sanitize_str_list tests
# ============================================================================


class TestSanitizeStrList:
    """Test _sanitize_str_list helper."""

    def test_valid_strings(self):
        assert _sanitize_str_list(["a", "b", "c"]) == ["a", "b", "c"]

    def test_filters_none(self):
        assert _sanitize_str_list(["valid", None, "also valid"]) == ["valid", "also valid"]

    def test_converts_numbers_to_str(self):
        result = _sanitize_str_list(["text", 123, 45.6])
        assert result == ["text", "123", "45.6"]

    def test_filters_empty_strings(self):
        assert _sanitize_str_list(["valid", "", "  ", "ok"]) == ["valid", "ok"]

    def test_not_a_list_returns_empty(self):
        assert _sanitize_str_list("not a list") == []
        assert _sanitize_str_list(42) == []
        assert _sanitize_str_list(None) == []

    def test_empty_list(self):
        assert _sanitize_str_list([]) == []


# ============================================================================
# GoalAnalysis.from_dict — sanitization and caps tests
# ============================================================================


class TestGoalAnalysisFromDictSanitization:
    """Test from_dict sanitization of list fields and complexity/phase consistency."""

    def test_sub_domains_capped_at_6(self):
        data = {"sub_domains": ["a", "b", "c", "d", "e", "f", "g", "h"]}
        analysis = GoalAnalysis.from_dict(data)
        assert len(analysis.sub_domains) == 6

    def test_ai_capabilities_with_nulls(self):
        data = {"ai_capabilities": ["Write code", None, 123, "", "Test code"]}
        analysis = GoalAnalysis.from_dict(data)
        assert analysis.ai_capabilities == ["Write code", "123", "Test code"]

    def test_human_requirements_with_nulls(self):
        data = {"human_requirements": ["Decide schema", None, "Approve design"]}
        analysis = GoalAnalysis.from_dict(data)
        assert analysis.human_requirements == ["Decide schema", "Approve design"]

    def test_constraints_detected_not_a_list(self):
        data = {"constraints_detected": "not a list"}
        analysis = GoalAnalysis.from_dict(data)
        assert analysis.constraints_detected == []

    def test_xl_complexity_minimum_3_phases(self):
        data = {"complexity": "XL", "estimated_phases": 1}
        analysis = GoalAnalysis.from_dict(data)
        assert analysis.estimated_phases == 3

    def test_l_complexity_minimum_2_phases(self):
        data = {"complexity": "L", "estimated_phases": 1}
        analysis = GoalAnalysis.from_dict(data)
        assert analysis.estimated_phases == 2

    def test_s_complexity_allows_1_phase(self):
        data = {"complexity": "S", "estimated_phases": 1}
        analysis = GoalAnalysis.from_dict(data)
        assert analysis.estimated_phases == 1

    def test_m_complexity_allows_1_phase(self):
        data = {"complexity": "M", "estimated_phases": 1}
        analysis = GoalAnalysis.from_dict(data)
        assert analysis.estimated_phases == 1


# ============================================================================
# _run_prompt — empty response test
# ============================================================================


class TestRunPromptEmptyResponse:
    """Test _run_prompt raises on empty LLM response."""

    @pytest.mark.asyncio
    async def test_raises_on_empty_response(self):
        parser = GoalParser()

        mock_router = MagicMock()

        async def mock_run(prompt):
            yield AgentEvent(type="done", content="")

        mock_router.run = mock_run

        with patch("pocketpaw.agents.router.AgentRouter", return_value=mock_router):
            with patch("pocketpaw.config.get_settings"):
                with pytest.raises(RuntimeError, match="empty response"):
                    await parser._run_prompt("test prompt")

    @pytest.mark.asyncio
    async def test_raises_on_only_empty_messages(self):
        parser = GoalParser()

        mock_router = MagicMock()

        async def mock_run(prompt):
            yield AgentEvent(type="message", content="")
            yield AgentEvent(type="message", content="")

        mock_router.run = mock_run

        with patch("pocketpaw.agents.router.AgentRouter", return_value=mock_router):
            with patch("pocketpaw.config.get_settings"):
                with pytest.raises(RuntimeError, match="empty response"):
                    await parser._run_prompt("test prompt")


# ============================================================================
# Prompt injection safety tests
# ============================================================================


class TestPromptInjection:
    """Test that curly braces in user input don't break prompt formatting."""

    @pytest.mark.asyncio
    async def test_curly_braces_in_input(self):
        parser = GoalParser()
        captured_prompt = None

        async def mock_run_prompt(prompt: str) -> str:
            nonlocal captured_prompt
            captured_prompt = prompt
            return VALID_GOAL_JSON

        parser._run_prompt = mock_run_prompt

        # Input with curly braces should not crash
        await parser.parse("Build a {React} app with {TypeScript}")
        assert captured_prompt is not None
        assert "{React}" in captured_prompt  # braces preserved in final prompt

    @pytest.mark.asyncio
    async def test_format_string_attack(self):
        parser = GoalParser()
        captured_prompt = None

        async def mock_run_prompt(prompt: str) -> str:
            nonlocal captured_prompt
            captured_prompt = prompt
            return VALID_GOAL_JSON

        parser._run_prompt = mock_run_prompt

        # Malicious format string should not cause KeyError
        await parser.parse("Build {__class__.__mro__[1]}")
        assert captured_prompt is not None
