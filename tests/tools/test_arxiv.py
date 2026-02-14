import pytest
from unittest.mock import MagicMock, patch
from pocketclaw.tools.builtin.arxiv import ArxivTool

@pytest.fixture
def arxiv_tool():
    return ArxivTool()

@pytest.mark.asyncio
async def test_arxiv_search_basic(arxiv_tool):
    with patch("arxiv.Client") as MockClient, \
         patch("arxiv.Search") as MockSearch:
        
        # Mock client and results
        mock_client_instance = MockClient.return_value
        
        mock_result = MagicMock()
        mock_result.title = "Test Paper"
        author_a = MagicMock()
        author_a.name = "Author A"
        author_b = MagicMock()
        author_b.name = "Author B"
        mock_result.authors = [author_a, author_b]
        mock_result.published.strftime.return_value = "2023-01-01"
        mock_result.summary = "This is a summary."
        mock_result.pdf_url = "http://arxiv.org/pdf/1234.5678"
        
        mock_client_instance.results.return_value = [mock_result]
        
        result = await arxiv_tool.execute("query")
        
        assert "Test Paper" in result
        assert "Author A" in result
        assert "http://arxiv.org/pdf/1234.5678" in result

@pytest.mark.asyncio
async def test_arxiv_search_no_results(arxiv_tool):
    with patch("arxiv.Client") as MockClient:
        mock_client_instance = MockClient.return_value
        mock_client_instance.results.return_value = []
        
        result = await arxiv_tool.execute("nonexistent query")
        
        assert "No results found" in result

@pytest.mark.asyncio
async def test_arxiv_search_error(arxiv_tool):
    with patch("arxiv.Client") as MockClient:
        MockClient.side_effect = Exception("API Error")
        
        result = await arxiv_tool.execute("query")
        
        assert "Error" in result
        assert "API Error" in result
