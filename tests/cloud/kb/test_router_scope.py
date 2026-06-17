# tests/cloud/kb/test_router_scope.py — REST-door scope-override leak-prevention.
#
# Created: 2026-06-08 (VIP Onboarding Phase B) — the SECOND door into the
# kb-go scope store. The chat-path gate (``_member_private_user_scope`` in
# chat/agent_service.py) is closed, but the ``/api/v1/kb/*`` REST router
# accepts a free-form ``scope`` override with no scope-to-caller binding. Any
# authenticated member could ``POST /api/v1/kb/search {"scope":"user:{victim}"}``
# and read another member's private Gmail/calendar KB, or ``ingest`` into it to
# poison the victim's own agent.
#
# This suite mirrors tests/cloud/test_agent_service_scope.py's leak matrix for
# the REST door: a cross-principal ``user:`` override is rejected for READ
# (search/lint) AND WRITE (ingest text/url), while every legitimate scope
# (own workspace, a visible pocket, a workspace agent, the caller's OWN
# ``user:``) still reaches kb-go. The cross-principal 403 tests are RED before
# the allowlist validator lands and GREEN after.
#
# The router endpoints are plain async functions, so the tests call them
# directly with the FastAPI ``Depends`` values supplied positionally. ``_kb``
# is patched to a spy (no kb-go binary on the runner) and ``_candidate_scopes``
# is patched to a fixed allowlist so the validator's decision — not Mongo —
# is what's under test.

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pocketpaw_ee.cloud.kb import router as kb_router
from pocketpaw_ee.cloud.kb.dto import (
    IngestTextRequest,
    IngestUrlRequest,
    LintRequest,
    SearchRequest,
)
from pocketpaw_ee.cloud.shared.errors import Forbidden

WORKSPACE = "w1"
CALLER = "memberA"
OTHER = "memberB"
VISIBLE_POCKET = "p_visible"
WORKSPACE_AGENT = "a_ws"

# The allowlist the candidate enumerator returns for CALLER in WORKSPACE:
# the workspace itself, one pocket the caller can see, one workspace agent.
# The caller's own ``user:`` is added by the validator on top of this set.
_ALLOWED_CANDIDATES = [
    f"workspace:{WORKSPACE}",
    f"pocket:{VISIBLE_POCKET}",
    f"agent:{WORKSPACE_AGENT}",
]


def _patch_candidates():
    """Patch ``_candidate_scopes`` to the fixed allowlist for CALLER."""
    return patch(
        "pocketpaw_ee.cloud.kb.service._candidate_scopes",
        AsyncMock(return_value=list(_ALLOWED_CANDIDATES)),
    )


def _patch_kb(spy):
    """Patch the kb-go subprocess wrapper the router calls.

    ``_kb`` is a SYNC function invoked without ``await`` in the router, so the
    spy is a plain ``MagicMock`` — an ``AsyncMock`` would record the call but
    return an un-awaited coroutine that the router's ``isinstance(.., list)``
    guard rejects, masking the happy-path assertions.
    """
    return patch.object(kb_router, "_kb", spy)


# ===========================================================================
# LEAK MATRIX — cross-principal ``user:`` override must 403 (RED → GREEN).
#
# These are the centerpiece. Before the allowlist validator, the override is
# passed straight to kb-go and the foreign user: scope is read/written. After,
# each endpoint raises Forbidden("kb.scope_forbidden") and never touches _kb.
# ===========================================================================


@pytest.mark.asyncio
async def test_search_rejects_foreign_user_scope():
    """READ leak: searching ``user:{other}`` is denied — never reaches kb-go."""
    spy = MagicMock()
    with _patch_candidates(), _patch_kb(spy):
        with pytest.raises(Forbidden) as exc:
            await kb_router.search_kb(
                SearchRequest(query="invoices", scope=f"user:{OTHER}"),
                workspace_id=WORKSPACE,
                user_id=CALLER,
            )
    assert exc.value.code == "kb.scope_forbidden"
    spy.assert_not_called()


@pytest.mark.asyncio
async def test_lint_rejects_foreign_user_scope():
    """Metadata oracle: linting ``user:{other}`` is denied."""
    spy = MagicMock()
    with _patch_candidates(), _patch_kb(spy):
        with pytest.raises(Forbidden) as exc:
            await kb_router.lint_kb(
                LintRequest(scope=f"user:{OTHER}"),
                workspace_id=WORKSPACE,
                user_id=CALLER,
            )
    assert exc.value.code == "kb.scope_forbidden"
    spy.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_text_rejects_foreign_user_scope():
    """WRITE/poison: ingesting text into ``user:{other}`` is denied."""
    spy = MagicMock()
    with _patch_candidates(), _patch_kb(spy):
        with pytest.raises(Forbidden) as exc:
            await kb_router.ingest_text(
                IngestTextRequest(text="poison", source="x", scope=f"user:{OTHER}"),
                workspace_id=WORKSPACE,
                user_id=CALLER,
            )
    assert exc.value.code == "kb.scope_forbidden"
    spy.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_url_rejects_foreign_user_scope():
    """WRITE/poison: ingesting a URL into ``user:{other}`` is denied — denied
    before the URL is ever fetched."""
    spy = MagicMock()
    with (
        _patch_candidates(),
        _patch_kb(spy),
        patch.object(kb_router, "_extract_url", AsyncMock(return_value="text")) as ex,
    ):
        with pytest.raises(Forbidden) as exc:
            await kb_router.ingest_url(
                IngestUrlRequest(url="https://evil.test", scope=f"user:{OTHER}"),
                workspace_id=WORKSPACE,
                user_id=CALLER,
            )
    assert exc.value.code == "kb.scope_forbidden"
    spy.assert_not_called()
    ex.assert_not_called()  # short-circuit before fetch


# ===========================================================================
# Defense-in-depth: an unauthorized pocket / agent override is denied too —
# the validator's allowlist is the full candidate set, not just user:.
# ===========================================================================


@pytest.mark.asyncio
async def test_search_rejects_unauthorized_pocket_scope():
    spy = MagicMock()
    with _patch_candidates(), _patch_kb(spy):
        with pytest.raises(Forbidden) as exc:
            await kb_router.search_kb(
                SearchRequest(query="q", scope="pocket:p_not_mine"),
                workspace_id=WORKSPACE,
                user_id=CALLER,
            )
    assert exc.value.code == "kb.scope_forbidden"
    spy.assert_not_called()


@pytest.mark.asyncio
async def test_search_rejects_foreign_workspace_scope():
    spy = MagicMock()
    with _patch_candidates(), _patch_kb(spy):
        with pytest.raises(Forbidden) as exc:
            await kb_router.search_kb(
                SearchRequest(query="q", scope="workspace:w_other"),
                workspace_id=WORKSPACE,
                user_id=CALLER,
            )
    assert exc.value.code == "kb.scope_forbidden"
    spy.assert_not_called()


# ===========================================================================
# Legitimate scopes still work — the gate must not break the happy path.
# ===========================================================================


@pytest.mark.asyncio
async def test_search_no_override_uses_caller_workspace():
    """No override → the caller's own active workspace, no candidate probe."""
    spy = MagicMock(return_value=[])
    with _patch_candidates(), _patch_kb(spy):
        await kb_router.search_kb(
            SearchRequest(query="q"),
            workspace_id=WORKSPACE,
            user_id=CALLER,
        )
    args = spy.call_args.args
    assert "--scope" in args
    assert args[args.index("--scope") + 1] == f"workspace:{WORKSPACE}"


@pytest.mark.asyncio
async def test_search_allows_own_workspace_override():
    spy = MagicMock(return_value=[])
    with _patch_candidates(), _patch_kb(spy):
        await kb_router.search_kb(
            SearchRequest(query="q", scope=f"workspace:{WORKSPACE}"),
            workspace_id=WORKSPACE,
            user_id=CALLER,
        )
    args = spy.call_args.args
    assert args[args.index("--scope") + 1] == f"workspace:{WORKSPACE}"


@pytest.mark.asyncio
async def test_search_allows_visible_pocket_override():
    spy = MagicMock(return_value=[])
    with _patch_candidates(), _patch_kb(spy):
        await kb_router.search_kb(
            SearchRequest(query="q", scope=f"pocket:{VISIBLE_POCKET}"),
            workspace_id=WORKSPACE,
            user_id=CALLER,
        )
    args = spy.call_args.args
    assert args[args.index("--scope") + 1] == f"pocket:{VISIBLE_POCKET}"


@pytest.mark.asyncio
async def test_search_allows_workspace_agent_override():
    spy = MagicMock(return_value=[])
    with _patch_candidates(), _patch_kb(spy):
        await kb_router.search_kb(
            SearchRequest(query="q", scope=f"agent:{WORKSPACE_AGENT}"),
            workspace_id=WORKSPACE,
            user_id=CALLER,
        )
    args = spy.call_args.args
    assert args[args.index("--scope") + 1] == f"agent:{WORKSPACE_AGENT}"


@pytest.mark.asyncio
async def test_search_allows_own_user_override():
    """The caller's OWN ``user:{caller}`` scope is allowed (their private KB)."""
    spy = MagicMock(return_value=[])
    with _patch_candidates(), _patch_kb(spy):
        await kb_router.search_kb(
            SearchRequest(query="q", scope=f"user:{CALLER}"),
            workspace_id=WORKSPACE,
            user_id=CALLER,
        )
    args = spy.call_args.args
    assert args[args.index("--scope") + 1] == f"user:{CALLER}"


@pytest.mark.asyncio
async def test_ingest_text_allows_own_user_override():
    """WRITE to the caller's OWN private KB is allowed."""
    spy = MagicMock(return_value={"ok": True})
    with _patch_candidates(), _patch_kb(spy):
        await kb_router.ingest_text(
            IngestTextRequest(text="note", source="manual", scope=f"user:{CALLER}"),
            workspace_id=WORKSPACE,
            user_id=CALLER,
        )
    args = spy.call_args.args
    assert args[args.index("--scope") + 1] == f"user:{CALLER}"
