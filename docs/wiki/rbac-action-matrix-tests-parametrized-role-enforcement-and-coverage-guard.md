---
{
  "title": "RBAC Action Matrix Tests: Parametrized Role Enforcement and Coverage Guard",
  "summary": "A parametrized test matrix that exercises every entry in the canonical `ACTIONS` table against every role in the matching role family, verifying that `check_action` allows or raises `Forbidden` with the correct deny code. A meta-test enforces that no new action can be added without both an allow-path and a deny-path assertion, preventing silent coverage gaps.",
  "concepts": [
    "RBAC",
    "ACTIONS table",
    "check_action",
    "WorkspaceRole",
    "GroupRole",
    "PocketAccess",
    "Forbidden",
    "deny_code",
    "parametrized matrix",
    "role family",
    "coverage guard",
    "access control"
  ],
  "categories": [
    "testing",
    "access control",
    "enterprise edition",
    "security",
    "test"
  ],
  "source_docs": [
    "cbf275e61eb2c7c5"
  ],
  "backlinks": null,
  "word_count": 456,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's enterprise edition gates operations behind a role-based access control (RBAC) layer defined in `pocketpaw.ee.guards.actions`. The `ACTIONS` dict maps action strings (e.g., `workspace.update`, `pocket.delete`) to rules that specify a `minimum` role and a `deny_code`. `check_action` enforces those rules by comparing the actor's role level to the minimum.

## Why a Matrix Test

Role systems fail in subtle ways: a new action might accidentally be assigned the wrong minimum, a role family might be mixed up (passing a `GroupRole` to a workspace action), or the `deny_code` might not match what the API caller expects. Manual tests for each action×role pair would be ~O(actions × roles) test functions — unmanageable and easy to forget.

The matrix approach solves this: `_MATRIX` is dynamically built from `ACTIONS.items()` and `_peers_of(rule.minimum)`, producing one `pytest.param` per `(action, role_level)` combination. pytest parametrize runs them all. When an action is added to `ACTIONS`, the matrix automatically grows.

## Role Family Enforcement

`_peers_of(minimum)` determines which family of roles to test against by inspecting the type of `rule.minimum`. If a developer accidentally passes a `GroupRole` to a workspace-scoped action (or vice versa), `_peers_of` would raise `TypeError`. The dedicated test `test_mismatched_role_family_raises_type_error` ensures `check_action` itself also raises `TypeError` in this scenario, catching programmer errors at the call site rather than producing silent mis-authorization.

## Deny Code Assertions

The parametrized test doesn't just check that `Forbidden` is raised — it checks that `exc_info.value.code == rule.deny_code`. This matters because callers (e.g., the API layer) often switch on the deny code to return different HTTP status codes or error messages. A wrong deny code would be a silent contract violation undetectable without this assertion.

## Meta-Test: Coverage Guard

`test_every_action_has_allow_and_deny_coverage` walks every action and its peers to verify that at least one peer level satisfies `level >= minimum` (allow path) and at least one satisfies `level < minimum` (deny path). If an action's minimum is the lowest role in its family, there is no deny-capable peer — the test documents those as `lowest_level_only` and asserts that `missing_deny` equals exactly that set. Any entry outside that set is a true gap and the test fails.

This means: adding an action whose minimum is, say, `WorkspaceRole.OWNER` (highest level) would have no allow-path peers and the meta-test would flag `missing_allow`. You cannot silently introduce an unexercisable rule.

## Unknown Action Guard

`test_unknown_action_raises_key_error` imports `get_rule` directly and verifies that requesting a nonexistent action raises `KeyError`. This prevents callers from silently getting `None` and bypassing the check.

## Known Gaps

The test covers role-level enforcement but does not test that the `ACTIONS` dict itself is exhaustive — i.e., there is no assertion that every API endpoint that requires authorization has a corresponding entry in `ACTIONS`. That coverage gap is a policy concern, not a role-level concern.
