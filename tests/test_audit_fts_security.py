# test_audit_fts_security.py — Security regression for audit FTS.
# Created: 2026-04-19 (Cluster C / PR4) — Proves the ``q`` parameter on
# /runtime/audit is fully parameter-bound and cannot corrupt the
# audit_log table even when the input is crafted for SQL injection. Also
# covers LIKE-wildcard escape so ``%``/``_`` inputs don't silently widen
# the query and leak row existence.

from __future__ import annotations

from pathlib import Path

import pytest

from pocketpaw.audit.store import AuditStore, _fts_escape


@pytest.fixture()
def store(tmp_path: Path) -> AuditStore:
    return AuditStore(tmp_path / "audit.db")


@pytest.mark.asyncio
async def test_injection_cannot_drop_table(store: AuditStore) -> None:
    # Seed a couple of real rows.
    await store.log_entry(
        actor="user:alice",
        action="tool_exec",
        category="decision",
        description="Alice ran the weekly digest",
        pocket_id="p1",
    )
    await store.log_entry(
        actor="user:bob",
        action="doc_publish",
        category="data",
        description="Bob published the onboarding guide",
        pocket_id="p1",
    )

    # The classic SQL injection string — a dangerous-looking q.
    evil = "'; DROP TABLE audit_log; --"
    results = await store.search_entries(q=evil)
    # Attack returns zero matches (the literal substring does not exist)
    # and — crucially — the table still contains its two rows.
    assert results == []

    all_rows = await store.search_entries()
    assert len(all_rows) == 2


@pytest.mark.asyncio
async def test_wildcard_inputs_are_escaped(store: AuditStore) -> None:
    """``q="admin_"`` must match only literal underscores.

    Without escaping, LIKE treats ``_`` as "any single char" and
    ``admin_`` would match ``admin1``, ``admin2``, etc. This escape
    prevents that class of existence-leak — a viewer can't probe whether
    any row with a similar-shape id exists.
    """
    await store.log_entry(
        actor="user:x",
        action="safe_action",
        category="data",
        description="admin_ literal underscore",
        pocket_id="p1",
    )
    await store.log_entry(
        actor="user:x",
        action="safe_action",
        category="data",
        description="admin1 substring",
        pocket_id="p1",
    )

    literal_hit = await store.search_entries(q="admin_")
    assert len(literal_hit) == 1
    assert "admin_" in literal_hit[0].description

    # A ``%`` input is escaped too — only literal percent matches.
    await store.log_entry(
        actor="user:x",
        action="safe_action",
        category="data",
        description="100% honest",
        pocket_id="p1",
    )
    percent_hit = await store.search_entries(q="100%")
    assert len(percent_hit) == 1
    assert "100%" in percent_hit[0].description


@pytest.mark.asyncio
async def test_search_is_case_insensitive(store: AuditStore) -> None:
    await store.log_entry(
        actor="user:x",
        action="Weekly_Report",
        category="data",
        description="WEEKLY report description",
        pocket_id="p1",
    )
    for term in ("weekly", "WEEKLY", "Weekly"):
        hits = await store.search_entries(q=term)
        assert len(hits) == 1, f"case-insensitive miss for q={term!r}"


@pytest.mark.asyncio
async def test_search_spans_action_description_context(store: AuditStore) -> None:
    await store.log_entry(
        actor="user:x",
        action="special_action",
        category="data",
        description="ordinary",
        pocket_id="p1",
    )
    await store.log_entry(
        actor="user:x",
        action="ordinary",
        category="data",
        description="look-here-token",
        pocket_id="p1",
    )
    await store.log_entry(
        actor="user:x",
        action="ordinary",
        category="data",
        description="ordinary",
        pocket_id="p1",
        context={"note": "deep-context-token"},
    )

    assert len(await store.search_entries(q="special_action")) == 1
    assert len(await store.search_entries(q="look-here-token")) == 1
    assert len(await store.search_entries(q="deep-context-token")) == 1


@pytest.mark.asyncio
async def test_workspace_id_matches_context_field(store: AuditStore) -> None:
    await store.log_entry(
        actor="user:x",
        action="ws_event",
        category="data",
        description="row for workspace A",
        pocket_id="p1",
        context={"workspace_id": "ws-alpha"},
    )
    await store.log_entry(
        actor="user:x",
        action="ws_event",
        category="data",
        description="row for workspace B",
        pocket_id="p2",
        context={"workspace_id": "ws-beta"},
    )
    await store.log_entry(
        actor="user:x",
        action="ws_event",
        category="data",
        description="legacy row without workspace_id",
        pocket_id="p3",
    )

    alpha_rows = await store.search_entries(workspace_id="ws-alpha")
    assert len(alpha_rows) == 1
    assert alpha_rows[0].description == "row for workspace A"

    beta_rows = await store.search_entries(workspace_id="ws-beta")
    assert len(beta_rows) == 1

    no_ws_rows = await store.search_entries(workspace_id="ws-never")
    assert no_ws_rows == []


def test_fts_escape_unit() -> None:
    # Backslash first so it doesn't double-escape later replacements.
    assert _fts_escape(r"a\b") == "%a\\\\b%"
    assert _fts_escape("a_b") == "%a\\_b%"
    assert _fts_escape("50%") == "%50\\%%"
    assert _fts_escape("MixedCASE") == "%mixedcase%"
