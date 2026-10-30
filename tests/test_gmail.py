# Tests for integrations/gmail.py and tools/builtin/gmail.py
# Created: 2026-02-07
# 2026-06-08: added TestGmailClientPerUser to lock GmailClient(user_id=...)
#   resolving its token from that user's bucket end-to-end (VIP Onboarding
#   Phase B), proving the user_id threads constructor -> OAuthManager ->
#   TokenStore.

import base64
import time
from unittest.mock import patch

import pytest

from pocketpaw.clients.gmail import GmailClient
from pocketpaw.clients.token_store import OAuthTokens, TokenStore
from pocketpaw.tools.builtin.gmail import GmailReadTool, GmailSearchTool, GmailSendTool

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
        "pocketpaw.clients.gmail.GmailClient._get_token",
        side_effect=RuntimeError("Not authenticated"),
    ):
        result = await tool.execute(query="test")
        assert "Error" in result
        assert "authenticated" in result.lower()


async def test_gmail_read_no_auth():
    tool = GmailReadTool()
    with patch(
        "pocketpaw.clients.gmail.GmailClient._get_token",
        side_effect=RuntimeError("Not authenticated"),
    ):
        result = await tool.execute(message_id="abc123")
        assert "Error" in result


async def test_gmail_send_no_auth():
    tool = GmailSendTool()
    with patch(
        "pocketpaw.clients.gmail.GmailClient._get_token",
        side_effect=RuntimeError("Not authenticated"),
    ):
        result = await tool.execute(to="x@x.com", subject="Hi", body="Test")
        assert "Error" in result


# ---------------------------------------------------------------------------
# GmailClient per-user token resolution (VIP Onboarding Phase B)
# ---------------------------------------------------------------------------


@pytest.fixture
def oauth_dir(tmp_path, monkeypatch):
    """Point the token store at a temp oauth dir for both store + manager."""
    monkeypatch.setattr("pocketpaw.clients.token_store._get_oauth_dir", lambda: tmp_path)
    return tmp_path


async def test_gmail_client_resolves_per_user_token(oauth_dir):
    """GmailClient(user_id='alice') pulls alice's fresh token, not bob's.

    End-to-end through the real OAuthManager + TokenStore — proves the
    user_id threads constructor -> get_valid_token -> store.load(user_id=...).
    """
    store = TokenStore()
    store.save(
        OAuthTokens(
            service="google_gmail", access_token="alice_tok", expires_at=time.time() + 3600
        ),
        user_id="alice",
    )
    store.save(
        OAuthTokens(service="google_gmail", access_token="bob_tok", expires_at=time.time() + 3600),
        user_id="bob",
    )

    assert await GmailClient(user_id="alice")._get_token() == "alice_tok"
    assert await GmailClient(user_id="bob")._get_token() == "bob_tok"


async def test_gmail_client_default_user_is_shared_bucket(oauth_dir):
    """GmailClient() (no user_id) keeps reading the legacy shared bucket."""
    store = TokenStore()
    store.save(
        OAuthTokens(
            service="google_gmail", access_token="shared_tok", expires_at=time.time() + 3600
        )
    )

    assert await GmailClient()._get_token() == "shared_tok"
    # A per-user client must NOT see the shared token.
    with pytest.raises(RuntimeError, match="not authenticated"):
        await GmailClient(user_id="alice")._get_token()
