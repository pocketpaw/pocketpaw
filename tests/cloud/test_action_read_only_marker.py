# tests/cloud/test_action_read_only_marker.py
# Created: 2026-06-28 (AW-6 — read_only action marker) — pins the
# safe-by-definition read-only exemption added to the EE action_executor:
#
#   * VALIDATION: `read_only=True` is only valid for a genuine read —
#     a GET/HEAD method with NO `instinct_policy`. A mutating verb
#     (POST/PUT/PATCH/DELETE) marked read_only fails parse; a read_only
#     binding carrying an `instinct_policy` fails parse. A plain
#     `read_only=True` GET parses cleanly.
#   * GATE 7: a `read_only=True` GET binding SKIPS the deny-by-default
#     Instinct park (it proceeds ungated, falling through to the HTTP
#     path) — so the executor never returns `instinct_pending` for it.
#   * REGRESSION: a normal mutating binding (`read_only=False`, the
#     default) still parks at gate 7 under deny-by-default, exactly as
#     today — the marker changed nothing for real writes.
#
# Gate 7 sits AFTER the DNS pre-resolve guard (gate 6), so both gate-7
# tests monkeypatch `_assert_host_external` to a no-op — otherwise the
# bogus base_url is rejected at gate 6 before the park decision is ever
# reached, and the test would pass vacuously. With gate 6 bypassed:
#   * the mutating POST reaches gate 7 and PARKS (`instinct_pending`,
#     no HTTP call) — the deny-by-default regression;
#   * the read_only GET reaches gate 7, does NOT park, and falls into
#     the gate-8 HTTP call (which fails against the bogus base_url) —
#     proving the suppression actually let it past the gate, not that
#     an earlier guard rejected it.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.pockets import action_executor
from pocketpaw_ee.cloud.pockets.action_executor import ActionBinding
from pydantic import ValidationError

pytestmark = pytest.mark.usefixtures("mongo_db")


# ---------------------------------------------------------------------------
# Validator — read_only is only valid for a genuine read (GET/HEAD, no policy)
# ---------------------------------------------------------------------------


def test_read_only_get_binding_parses() -> None:
    """A read_only GET with no policy is the canonical safe-read shape."""
    binding = ActionBinding.model_validate(
        {"kind": "write_binding", "method": "GET", "path": "/items", "read_only": True}
    )
    assert binding.read_only is True
    assert binding.method == "GET"


def test_read_only_head_binding_parses() -> None:
    """HEAD is the other non-mutating method the marker allows."""
    binding = ActionBinding.model_validate(
        {"kind": "write_binding", "method": "HEAD", "path": "/items", "read_only": True}
    )
    assert binding.read_only is True


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_read_only_on_mutating_method_fails_validation(method: str) -> None:
    """A mutating verb must NOT be labeled read_only — that would un-gate a
    real write. Parse must reject it with a clear message."""
    with pytest.raises(ValidationError) as exc:
        ActionBinding.model_validate(
            {
                "kind": "write_binding",
                "method": method,
                "path": "/items",
                "read_only": True,
            }
        )
    assert "read_only" in str(exc.value)
    assert "mutating" in str(exc.value) or "GET/HEAD" in str(exc.value)


def test_read_only_with_instinct_policy_fails_validation() -> None:
    """`read_only` says 'no gate'; `instinct_policy` says 'gate this way' —
    the two contradict, so the binding must be rejected at parse time."""
    with pytest.raises(ValidationError) as exc:
        ActionBinding.model_validate(
            {
                "kind": "write_binding",
                "method": "GET",
                "path": "/items",
                "read_only": True,
                "instinct_policy": "approve_per_row",
            }
        )
    assert "read_only" in str(exc.value)
    assert "instinct_policy" in str(exc.value)


# ---------------------------------------------------------------------------
# Gate 7 — read_only skips the park; a normal mutating write still parks
# ---------------------------------------------------------------------------


async def _noop_host_external(_hostname: str) -> None:
    """Bypass gate 6 (DNS pre-resolve) so the test reaches gate 7."""
    return None


async def test_read_only_get_skips_gate_7_park(recording_bus, monkeypatch) -> None:
    """A read_only GET proceeds UNGATED: gate 7's deny-by-default park does
    NOT fire, so the executor never returns `instinct_pending`. With gate 6
    bypassed it falls through to the gate-8 HTTP call, which fails against
    the bogus base_url — proving the write got PAST gate 7, not that an
    earlier guard rejected it."""
    monkeypatch.setattr(action_executor, "_assert_host_external", _noop_host_external)

    result = await action_executor.run_action(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u1",
        action="read_thing",
        raw_action={
            "kind": "write_binding",
            "method": "GET",
            "path": "/items",
            "read_only": True,
        },
        path="/items",
        params={},
        base_url="https://example.test",
        auth_type="bearer",
        auth_header=None,
        token="t",
        allowed_writes=[{"method": "GET", "path_pattern": "/items*"}],
    )

    # The defining assertion: NO Instinct park for a safe read.
    assert result.get("code") != "instinct_pending"
    assert "_park" not in result
    # It got past gate 7 into the HTTP call — which fails on the bogus host.
    # The point is it REACHED the call (a park would have short-circuited).
    assert result["ok"] is False
    assert result["code"] == "request_failed"


async def test_normal_mutating_binding_still_parks_at_gate_7(recording_bus, monkeypatch) -> None:
    """Regression: a normal write (read_only defaults False) still parks at
    gate 7 under deny-by-default — the marker changed nothing for real
    writes. With gate 6 bypassed, the park sentinel returns BEFORE any HTTP
    call (the bogus host is never contacted)."""
    monkeypatch.setattr(action_executor, "_assert_host_external", _noop_host_external)

    result = await action_executor.run_action(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u1",
        action="write_thing",
        raw_action={"kind": "write_binding", "method": "POST", "path": "/items"},
        path="/items",
        params={"x": 1},
        base_url="https://example.test",
        auth_type="bearer",
        auth_header=None,
        token="t",
        allowed_writes=[{"method": "POST", "path_pattern": "/items*"}],
    )

    assert result["ok"] is True
    assert result["code"] == "instinct_pending"
    # The park blob carries the resolved write for a post-approval replay.
    assert result["_park"]["method"] == "POST"
    assert result["_park"]["path"] == "/items"
