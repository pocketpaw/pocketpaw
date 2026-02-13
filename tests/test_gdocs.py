# Tests for Google Docs integration (Sprint 26)

from unittest.mock import AsyncMock, MagicMock, patch


class TestDocsToolSchemas:
    """Test Docs tool properties and schemas."""

    def test_docs_read_tool(self):
        from pocketclaw.tools.builtin.gdocs import DocsReadTool

        tool = DocsReadTool()
        assert tool.name == "docs_read"
        assert tool.trust_level == "high"
        assert "document_id" in tool.parameters["properties"]

    def test_docs_create_tool(self):
        from pocketclaw.tools.builtin.gdocs import DocsCreateTool

        tool = DocsCreateTool()
        assert tool.name == "docs_create"
        assert "title" in tool.parameters["properties"]
        assert "content" in tool.parameters["properties"]
        assert "title" in tool.parameters["required"]

    def test_docs_search_tool(self):
        from pocketclaw.tools.builtin.gdocs import DocsSearchTool

        tool = DocsSearchTool()
        assert tool.name == "docs_search"
        assert "query" in tool.parameters["properties"]


class TestDocIdParsing:
    """Test document ID extraction from URLs."""

    def test_plain_id(self):
        from pocketclaw.tools.builtin.gdocs import _parse_doc_id

        assert _parse_doc_id("abc123xyz") == "abc123xyz"

    def test_full_url(self):
        from pocketclaw.tools.builtin.gdocs import _parse_doc_id

        url = "https://docs.google.com/document/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms/edit"
        assert _parse_doc_id(url) == "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"

    def test_url_with_fragment(self):
        from pocketclaw.tools.builtin.gdocs import _parse_doc_id

        url = "https://docs.google.com/document/d/abc_123-XYZ/edit#heading=h.1"
        assert _parse_doc_id(url) == "abc_123-XYZ"


class TestDocsTextExtraction:
    """Test plain text extraction from Docs API response."""

    def test_simple_text(self):
        from pocketclaw.integrations.gdocs import DocsClient

        doc = {
            "body": {
                "content": [
                    {"paragraph": {"elements": [{"textRun": {"content": "Hello world\n"}}]}}
                ]
            }
        }
        assert DocsClient._extract_text(doc) == "Hello world"

    def test_multiple_paragraphs(self):
        from pocketclaw.integrations.gdocs import DocsClient

        doc = {
            "body": {
                "content": [
                    {"paragraph": {"elements": [{"textRun": {"content": "First paragraph\n"}}]}},
                    {"paragraph": {"elements": [{"textRun": {"content": "Second paragraph\n"}}]}},
                ]
            }
        }
        text = DocsClient._extract_text(doc)
        assert "First paragraph" in text
        assert "Second paragraph" in text

    def test_empty_doc(self):
        from pocketclaw.integrations.gdocs import DocsClient

        doc = {"body": {"content": []}}
        assert DocsClient._extract_text(doc) == ""

    def test_no_text_run(self):
        from pocketclaw.integrations.gdocs import DocsClient

        doc = {"body": {"content": [{"paragraph": {"elements": [{"inlineObjectElement": {}}]}}]}}
        assert DocsClient._extract_text(doc) == ""


async def test_docs_read_no_auth():
    from pocketclaw.tools.builtin.gdocs import DocsReadTool

    tool = DocsReadTool()
    with patch(
        "pocketclaw.integrations.gdocs.DocsClient._get_token",
        side_effect=RuntimeError("Not authenticated"),
    ):
        result = await tool.execute(document_id="abc123")
    assert result.startswith("Error:")
    assert "authenticated" in result.lower()


async def test_docs_create_no_auth():
    from pocketclaw.tools.builtin.gdocs import DocsCreateTool

    tool = DocsCreateTool()
    with patch(
        "pocketclaw.integrations.gdocs.DocsClient._get_token",
        side_effect=RuntimeError("Not authenticated"),
    ):
        result = await tool.execute(title="Test Doc")
    assert result.startswith("Error:")


async def test_docs_search_no_auth():
    from pocketclaw.tools.builtin.gdocs import DocsSearchTool

    tool = DocsSearchTool()
    with patch(
        "pocketclaw.integrations.gdocs.DocsClient._get_token",
        side_effect=RuntimeError("Not authenticated"),
    ):
        result = await tool.execute(query="meeting notes")
    assert result.startswith("Error:")


async def test_docs_search_success():
    from pocketclaw.tools.builtin.gdocs import DocsSearchTool

    tool = DocsSearchTool()
    mock_docs = [
        {
            "id": "doc1",
            "name": "Meeting Notes",
            "modifiedTime": "2026-02-09",
            "webViewLink": "https://...",
        }
    ]
    with patch(
        "pocketclaw.integrations.gdocs.DocsClient._get_token",
        new_callable=AsyncMock,
        return_value="fake-token",
    ):
        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"files": mock_docs}
            mock_resp.raise_for_status = MagicMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            result = await tool.execute(query="meeting")

    assert "Meeting Notes" in result
    assert "doc1" in result
