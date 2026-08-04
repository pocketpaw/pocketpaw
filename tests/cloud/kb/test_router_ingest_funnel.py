# tests/cloud/kb/test_router_ingest_funnel.py — REST ingest routes use the funnel.
# Created: 2026-08-04 — ingest hardening follow-up. POST /kb/ingest/text and
# POST /kb/ingest/url used to call ``_kb("ingest", ...)`` directly, bypassing
# the hardened ``KnowledgeService.ingest_text_to_scope`` funnel: no
# agent-backend compile on keyless boxes, no verbatim-fallback rejection —
# the original silent-poisoning hole, re-opened through the REST door.
# These tests drive the routes end-to-end through the REAL funnel and spy at
# the subprocess boundary: whatever hits the kb binary must be one of the two
# funnel argv shapes (plain ingest with a key, --article-json without one),
# never a bare direct call; and fallback-marker rejection must surface as the
# route's 500, not a stored article.
"""REST ingest routes are pinned to the hardened ingest funnel."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import AsyncMock, patch

import pytest
from pocketpaw_ee.cloud.agents import knowledge
from pocketpaw_ee.cloud.kb import backend_adapter
from pocketpaw_ee.cloud.kb import router as kb_router
from pocketpaw_ee.cloud.kb.dto import IngestTextRequest, IngestUrlRequest
from pocketpaw_ee.cloud.shared.errors import CloudError

WORKSPACE = "w1"
CALLER = "memberA"


def _patch_candidates():
    return patch(
        "pocketpaw_ee.cloud.kb.service._candidate_scopes",
        AsyncMock(return_value=[f"workspace:{WORKSPACE}"]),
    )


class _SubprocessSpy:
    """Records kb argv + stdin; delegates non-kb subprocess calls."""

    def __init__(self, responses: list) -> None:
        self.calls: list[dict] = []
        self._responses = list(responses)
        self._real_run = subprocess.run

    def __call__(self, cmd, input=None, timeout=None, **kwargs):  # noqa: A002
        if not cmd or cmd[0] != knowledge.KB_BIN:
            return self._real_run(cmd, input=input, timeout=timeout, **kwargs)
        self.calls.append({"cmd": list(cmd), "input": input, "timeout": timeout})
        returncode, stdout, stderr = self._responses.pop(0)
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


_ARTICLE = {
    "title": "T",
    "summary": "S.",
    "content": "# T\n\n- fact",
    "concepts": ["t"],
    "categories": ["c"],
}


def _install_compiler(monkeypatch) -> None:
    async def fake_complete(self, prompt: str, system_prompt: str = "") -> str:
        return json.dumps(_ARTICLE)

    monkeypatch.setattr(backend_adapter.PocketPawCompilerBackend, "complete", fake_complete)


@pytest.mark.asyncio
async def test_ingest_text_without_key_uses_article_json_funnel(monkeypatch):
    """Keyless box: the route must produce the pre-compiled --article-json call."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _install_compiler(monkeypatch)
    spy = _SubprocessSpy([(0, json.dumps({"id": "art-r1"}), "")])
    monkeypatch.setattr(knowledge.subprocess, "run", spy)

    with _patch_candidates():
        result = await kb_router.ingest_text(
            IngestTextRequest(text="workspace note", source="manual"),
            workspace_id=WORKSPACE,
            user_id=CALLER,
        )

    assert result["id"] == "art-r1"
    assert len(spy.calls) == 1
    cmd = spy.calls[0]["cmd"]
    assert cmd[1:] == ["ingest", "--article-json", "--scope", f"workspace:{WORKSPACE}", "--json"]
    payload = json.loads(spy.calls[0]["input"])
    assert payload["raw_text"] == "workspace note"
    assert payload["article"]["title"] == _ARTICLE["title"]


@pytest.mark.asyncio
async def test_ingest_text_with_key_uses_plain_funnel(monkeypatch):
    """With a key, the route emits the funnel's plain-ingest argv, no bare call."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    spy = _SubprocessSpy([(0, json.dumps({"id": "art-r2", "compiled_with": "llm"}), "")])
    monkeypatch.setattr(knowledge.subprocess, "run", spy)

    with _patch_candidates():
        result = await kb_router.ingest_text(
            IngestTextRequest(text="note", source="manual"),
            workspace_id=WORKSPACE,
            user_id=CALLER,
        )

    assert result["id"] == "art-r2"
    cmd = spy.calls[0]["cmd"]
    assert cmd[1:] == [
        "ingest",
        "--scope",
        f"workspace:{WORKSPACE}",
        "--source",
        "manual",
        "--json",
    ]
    assert spy.calls[0]["input"] == "note"


@pytest.mark.asyncio
async def test_ingest_url_routes_through_funnel(monkeypatch):
    """The URL route extracts here but ingests through the funnel."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _install_compiler(monkeypatch)
    spy = _SubprocessSpy([(0, json.dumps({"id": "art-r3"}), "")])
    monkeypatch.setattr(knowledge.subprocess, "run", spy)

    with (
        _patch_candidates(),
        patch.object(kb_router, "_extract_url", AsyncMock(return_value="page text")),
    ):
        result = await kb_router.ingest_url(
            IngestUrlRequest(url="https://example.test/page"),
            workspace_id=WORKSPACE,
            user_id=CALLER,
        )

    assert result["id"] == "art-r3"
    cmd = spy.calls[0]["cmd"]
    assert cmd[1:] == ["ingest", "--article-json", "--scope", f"workspace:{WORKSPACE}", "--json"]
    payload = json.loads(spy.calls[0]["input"])
    assert payload["raw_text"] == "page text"
    assert payload["article"]["source"] == "https://example.test/page"


@pytest.mark.asyncio
async def test_ingest_text_fallback_marker_maps_to_500(monkeypatch):
    """Fallback rejection applies at the REST door: 500, not a stored article."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    spy = _SubprocessSpy(
        [(0, json.dumps({"id": "art-r4", "compiled_with": "none (fallback)"}), "")]
    )
    monkeypatch.setattr(knowledge.subprocess, "run", spy)

    with _patch_candidates():
        with pytest.raises(CloudError) as exc:
            await kb_router.ingest_text(
                IngestTextRequest(text="doc", source="manual"),
                workspace_id=WORKSPACE,
                user_id=CALLER,
            )

    assert exc.value.code == "kb.ingest_failed"
    assert "verbatim fallback" in exc.value.message
