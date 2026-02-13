# Tests for tools/builtin/research.py
# Created: 2026-02-07

from unittest.mock import AsyncMock, MagicMock, patch

from pocketclaw.tools.builtin.research import ResearchTool

# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------


class TestToolDefinition:
    def test_name(self):
        tool = ResearchTool()
        assert tool.name == "research"

    def test_trust_level(self):
        tool = ResearchTool()
        assert tool.trust_level == "standard"

    def test_parameters(self):
        tool = ResearchTool()
        assert "topic" in tool.parameters["properties"]
        assert "depth" in tool.parameters["properties"]
        assert "save_to_memory" in tool.parameters["properties"]


# ---------------------------------------------------------------------------
# URL extraction
# ---------------------------------------------------------------------------


class TestExtractUrls:
    def test_extracts_urls(self):
        text = (
            "1. **Title**\n   https://example.com/page1\n"
            "2. **Title2**\n   https://example.com/page2\n"
        )
        urls = ResearchTool._extract_urls(text)
        assert len(urls) == 2
        assert "https://example.com/page1" in urls
        assert "https://example.com/page2" in urls

    def test_deduplicates(self):
        text = "https://example.com\nhttps://example.com"
        urls = ResearchTool._extract_urls(text)
        assert len(urls) == 1

    def test_strips_trailing_punctuation(self):
        text = "Visit https://example.com/path."
        urls = ResearchTool._extract_urls(text)
        assert urls[0] == "https://example.com/path"

    def test_no_urls(self):
        urls = ResearchTool._extract_urls("No links here")
        assert urls == []


# ---------------------------------------------------------------------------
# Execution — search failure
# ---------------------------------------------------------------------------


async def test_research_search_failure():
    tool = ResearchTool()
    mock_search_tool = MagicMock()
    mock_search_tool.execute = AsyncMock(return_value="Error: No API key")
    with patch(
        "pocketclaw.tools.builtin.research.WebSearchTool",
        return_value=mock_search_tool,
    ):
        result = await tool.execute(topic="quantum computing")
        assert "Error" in result
        assert "Search failed" in result


# ---------------------------------------------------------------------------
# Execution — happy path (mocked)
# ---------------------------------------------------------------------------


async def test_research_happy_path():
    tool = ResearchTool()

    mock_search_tool = MagicMock()
    mock_search_tool.execute = AsyncMock(
        return_value=(
            "Search results for: quantum computing\n\n"
            "1. **Intro to QC**\n   https://example.com/qc\n   Quantum computing basics.\n"
        )
    )
    mock_extract_tool = MagicMock()
    mock_extract_tool.execute = AsyncMock(return_value="# Intro to QC\n\nQuantum computing is...")
    mock_router = MagicMock()
    mock_router.chat = AsyncMock(return_value="Summary of quantum computing research...")

    with (
        patch(
            "pocketclaw.tools.builtin.research.WebSearchTool",
            return_value=mock_search_tool,
        ),
        patch(
            "pocketclaw.tools.builtin.research.UrlExtractTool",
            return_value=mock_extract_tool,
        ),
        patch(
            "pocketclaw.tools.builtin.research.LLMRouter",
            return_value=mock_router,
        ),
        patch(
            "pocketclaw.tools.builtin.research.Settings.load",
            return_value=MagicMock(),
        ),
    ):
        result = await tool.execute(topic="quantum computing")
        assert "Research: quantum computing" in result
