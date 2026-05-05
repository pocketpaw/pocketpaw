# test_kb_multi_scope.py — multi-scope tests for AgentContextBuilder._get_kb_context.
# Created: 2026-04-30 — Stage 1.B "Files as Knowledge". Verifies
#   _get_kb_context iterates settings.kb_scopes, divides the per-scope
#   limit, concatenates results under headers, isolates per-scope errors,
#   and respects the kb_scope deprecation shim.
"""Tests for the multi-scope KB context fetcher."""

from __future__ import annotations

import warnings
from typing import Any

import pytest

from pocketpaw.bootstrap.context_builder import AgentContextBuilder
from pocketpaw.config import Settings


def _stub_settings(monkeypatch, **overrides: Any) -> None:
    """Make ``get_settings()`` return a Settings instance with overrides."""
    base = Settings(_env_file=None)  # type: ignore[call-arg]
    for k, v in overrides.items():
        setattr(base, k, v)
    monkeypatch.setattr(
        "pocketpaw.config.get_settings",
        lambda force_reload=False: base,
    )


@pytest.mark.asyncio
async def test_multiple_scopes_each_queried_with_header(monkeypatch):
    calls: list[dict[str, Any]] = []

    async def fake_fetch(*, binary, query, scope, limit, query_vec_path=None):
        calls.append({"binary": binary, "query": query, "scope": scope, "limit": limit})
        return f"result-for-{scope}"

    monkeypatch.setattr(AgentContextBuilder, "_fetch_kb_scope", fake_fetch)
    _stub_settings(
        monkeypatch,
        kb_scopes=["workspace:w1", "agent:a1"],
        kb_limit=4,
    )

    out = await AgentContextBuilder._get_kb_context("hello")

    assert "### From workspace:w1" in out
    assert "### From agent:a1" in out
    assert "result-for-workspace:w1" in out
    assert "result-for-agent:a1" in out

    # 4 // 2 == 2 per scope.
    assert {c["scope"] for c in calls} == {"workspace:w1", "agent:a1"}
    assert all(c["limit"] == 2 for c in calls)


@pytest.mark.asyncio
async def test_per_scope_limit_floor_is_one(monkeypatch):
    """When ``kb_limit < len(scopes)`` per-scope budget floors at 1."""
    captured: list[int] = []

    async def fake_fetch(*, binary, query, scope, limit, query_vec_path=None):  # noqa: ARG001
        captured.append(limit)
        return ""

    monkeypatch.setattr(AgentContextBuilder, "_fetch_kb_scope", fake_fetch)
    _stub_settings(
        monkeypatch,
        kb_scopes=["a:1", "a:2", "a:3"],
        kb_limit=1,
    )

    await AgentContextBuilder._get_kb_context("query")

    assert captured == [1, 1, 1]


@pytest.mark.asyncio
async def test_one_scope_errors_other_returns(monkeypatch):
    async def fake_fetch(*, binary, query, scope, limit, query_vec_path=None):  # noqa: ARG001
        if scope == "workspace:w1":
            return "good content"
        return ""  # the other scope returned empty

    monkeypatch.setattr(AgentContextBuilder, "_fetch_kb_scope", fake_fetch)
    _stub_settings(
        monkeypatch,
        kb_scopes=["workspace:w1", "agent:broken"],
        kb_limit=2,
    )

    out = await AgentContextBuilder._get_kb_context("hello")

    assert "good content" in out
    assert "### From workspace:w1" in out
    # Empty scopes don't get a header section
    assert "### From agent:broken" not in out


@pytest.mark.asyncio
async def test_empty_scopes_returns_empty_string(monkeypatch):
    _stub_settings(monkeypatch, kb_scopes=[], kb_scope="")

    out = await AgentContextBuilder._get_kb_context("hello")
    assert out == ""


@pytest.mark.asyncio
async def test_empty_query_returns_empty_string(monkeypatch):
    _stub_settings(monkeypatch, kb_scopes=["workspace:w1"], kb_limit=3)

    out = await AgentContextBuilder._get_kb_context("")
    assert out == ""


@pytest.mark.asyncio
async def test_blank_scope_strings_filtered(monkeypatch):
    """Whitespace-only scope entries are dropped before the per-scope split."""
    captured: list[str] = []

    async def fake_fetch(*, binary, query, scope, limit, query_vec_path=None):  # noqa: ARG001
        captured.append(scope)
        return f"hit-{scope}"

    monkeypatch.setattr(AgentContextBuilder, "_fetch_kb_scope", fake_fetch)
    _stub_settings(
        monkeypatch,
        kb_scopes=["", "  ", "workspace:w1"],
        kb_limit=3,
    )

    out = await AgentContextBuilder._get_kb_context("q")

    assert captured == ["workspace:w1"]
    assert "hit-workspace:w1" in out


def test_kb_scope_deprecation_shim_copies_into_kb_scopes():
    """``kb_scope`` (string) → ``kb_scopes=[kb_scope]`` with DeprecationWarning."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        s = Settings(
            _env_file=None,  # type: ignore[call-arg]
            kb_scope="workspace:legacy",
            kb_scopes=[],
        )

    assert s.kb_scopes == ["workspace:legacy"]
    deprecation = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert deprecation, "expected DeprecationWarning when kb_scope is set"
    assert "POCKETPAW_KB_SCOPE" in str(deprecation[0].message)


def test_kb_scopes_wins_over_legacy_kb_scope():
    """When both fields are set the new list wins; no warning is emitted."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        s = Settings(
            _env_file=None,  # type: ignore[call-arg]
            kb_scope="workspace:legacy",
            kb_scopes=["workspace:new"],
        )

    assert s.kb_scopes == ["workspace:new"]
    deprecation = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert not deprecation, "should not warn when kb_scopes is already populated"


def test_no_warning_when_neither_set():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        Settings(_env_file=None)  # type: ignore[call-arg]
    deprecation = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert not deprecation
