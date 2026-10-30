# test_kb_resolve_scopes.py — _resolve_kb_scopes priority tests.
# Created: 2026-05-03 — Stage 3.E "Files as Knowledge". Verifies the
# per-request scope resolver builds the right scope list ahead of the
# static settings fallback. Most-specific wins.
# Updated: 2026-06-08 (VIP Onboarding Phase B) — added the optional
# ``KbContext.user_id`` tier. A set ``user_id`` emits ``user:{id}`` at the
# HEAD of the scope list (highest priority, ahead of pocket/agent/workspace)
# so a member's private mail/calendar KB outranks shared scopes in that
# member's own session. Absent ``user_id`` keeps the legacy ordering
# byte-identical — the regression guard tests below pin that.
"""``_resolve_kb_scopes(KbContext, settings)`` priority + fallback."""

from __future__ import annotations

from types import SimpleNamespace

from pocketpaw.bootstrap.context_builder import KbContext, _resolve_kb_scopes


def _settings(kb_scopes: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(kb_scopes=kb_scopes or [])


def test_pocket_agent_workspace_priority_order():
    """Most-specific wins: pocket > agent > workspace."""
    ctx = KbContext(pocket_id="P", agent_id="A", workspace_id="W")
    out = _resolve_kb_scopes(ctx, _settings(["fallback"]))
    assert out == ["pocket:P", "agent:A", "workspace:W"]


# --- VIP Onboarding Phase B: optional user: scope (highest priority) ---


def test_user_scope_is_highest_priority_when_set():
    """A set user_id emits ``user:{id}`` ahead of pocket/agent/workspace —
    the member's private mail/calendar KB outranks every shared scope."""
    ctx = KbContext(user_id="U", pocket_id="P", agent_id="A", workspace_id="W")
    out = _resolve_kb_scopes(ctx, _settings(["fallback"]))
    assert out == ["user:U", "pocket:P", "agent:A", "workspace:W"]


def test_user_scope_alone():
    """user_id set, nothing else → just the user scope."""
    ctx = KbContext(user_id="U")
    out = _resolve_kb_scopes(ctx, _settings(["fallback"]))
    assert out == ["user:U"]


def test_user_scope_with_workspace_only():
    ctx = KbContext(user_id="U", workspace_id="W")
    out = _resolve_kb_scopes(ctx, _settings())
    assert out == ["user:U", "workspace:W"]


def test_no_user_scope_when_user_id_absent():
    """Regression guard: without user_id the ordering is byte-identical to
    the pre-Phase-B behavior — no ``user:`` scope ever appears."""
    ctx = KbContext(pocket_id="P", agent_id="A", workspace_id="W")
    out = _resolve_kb_scopes(ctx, _settings(["fallback"]))
    assert all(not s.startswith("user:") for s in out)
    assert out == ["pocket:P", "agent:A", "workspace:W"]


def test_only_workspace_set():
    ctx = KbContext(workspace_id="W")
    out = _resolve_kb_scopes(ctx, _settings(["fallback"]))
    assert out == ["workspace:W"]


def test_only_pocket_set():
    ctx = KbContext(pocket_id="P")
    out = _resolve_kb_scopes(ctx, _settings(["fallback"]))
    assert out == ["pocket:P"]


def test_pocket_and_agent_no_workspace():
    ctx = KbContext(pocket_id="P", agent_id="A")
    out = _resolve_kb_scopes(ctx, _settings())
    assert out == ["pocket:P", "agent:A"]


def test_none_ctx_falls_back_to_settings_list():
    """No context → use the static settings list verbatim."""
    out = _resolve_kb_scopes(None, _settings(["workspace:legacy", "agent:legacy"]))
    assert out == ["workspace:legacy", "agent:legacy"]


def test_empty_ctx_falls_back_to_settings_list():
    """A KbContext with all fields None should still fall through."""
    out = _resolve_kb_scopes(KbContext(), _settings(["workspace:cli"]))
    assert out == ["workspace:cli"]


def test_empty_everything_returns_empty():
    out = _resolve_kb_scopes(KbContext(), _settings([]))
    assert out == []


def test_settings_list_is_copied_not_aliased():
    """The returned list is independent of the source so callers can mutate."""
    base = ["workspace:w1"]
    out = _resolve_kb_scopes(None, _settings(base))
    out.append("agent:a1")
    assert base == ["workspace:w1"]


def test_kb_context_is_immutable():
    """Frozen dataclass — accidental mutation raises so callers see the bug fast."""
    ctx = KbContext(pocket_id="P")
    try:
        ctx.pocket_id = "Q"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("KbContext should be frozen")
