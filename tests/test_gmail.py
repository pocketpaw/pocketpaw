# Tests for integrations/gmail.py and tools/builtin/gmail.py
# Created: 2026-02-07

import base64
from unittest.mock import patch

from pocketclaw.integrations.gmail import GmailClient
from pocketclaw.tools.builtin.gmail import GmailReadTool, GmailSearchTool, GmailSendTool

# ---------------------------------------------------------------------------
# GmailClient._extract_body
# ---------------------------------------------------------------------------


class TestExtractBody:
    def test_plain_text_direct(self):
        payload = {
            "mimeType": "text/plain",
            "body": {"data": base64.urlsafe_b64encode(b"Hello world").decode()},
        }
        assert GmailClient._extract_body(payload) == "Hello world"

    def test_multipart(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": base64.urlsafe_b64encode(b"Text body").decode()},
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": base64.urlsafe_b64encode(b"<p>HTML</p>").decode()},
                },
            ],
        }
        assert GmailClient._extract_body(payload) == "Text body"

    def test_no_text_content(self):
        payload = {"mimeType": "multipart/mixed", "parts": []}
        assert GmailClient._extract_body(payload) == "(no text content)"

    def test_nested_multipart(self):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "body": {"data": base64.urlsafe_b64encode(b"Nested").decode()},
                        },
                    ],
                }
            ],
        }
        assert GmailClient._extract_body(payload) == "Nested"


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


class TestToolDefinitions:
    def test_gmail_search_tool(self):
        tool = GmailSearchTool()
        assert tool.name == "gmail_search"
        assert tool.trust_level == "high"
        assert "query" in tool.parameters["properties"]

    def test_gmail_read_tool(self):
        tool = GmailReadTool()
        assert tool.name == "gmail_read"
        assert "message_id" in tool.parameters["properties"]

    def test_gmail_send_tool(self):
        tool = GmailSendTool()
        assert tool.name == "gmail_send"
        assert "to" in tool.parameters["properties"]
        assert "subject" in tool.parameters["properties"]
        assert "body" in tool.parameters["properties"]


# ---------------------------------------------------------------------------
# Tool execution — error path (no OAuth token)
# ---------------------------------------------------------------------------


async def test_gmail_search_no_auth():
    tool = GmailSearchTool()
    with patch(
        "pocketclaw.integrations.gmail.GmailClient._get_token",
        side_effect=RuntimeError("Not authenticated"),
    ):
        result = await tool.execute(query="test")
        assert "Error" in result
        assert "authenticated" in result.lower()


async def test_gmail_read_no_auth():
    tool = GmailReadTool()
    with patch(
        "pocketclaw.integrations.gmail.GmailClient._get_token",
        side_effect=RuntimeError("Not authenticated"),
    ):
        result = await tool.execute(message_id="abc123")
        assert "Error" in result


async def test_gmail_send_no_auth():
    tool = GmailSendTool()
    with patch(
        "pocketclaw.integrations.gmail.GmailClient._get_token",
        side_effect=RuntimeError("Not authenticated"),
    ):
        result = await tool.execute(to="x@x.com", subject="Hi", body="Test")
        assert "Error" in result
