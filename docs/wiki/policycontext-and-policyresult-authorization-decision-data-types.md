---
{
  "title": "PolicyContext and PolicyResult: Authorization Decision Data Types",
  "summary": "`policy.py` defines the two frozen dataclasses that form the data contract for the ABAC evaluation pipeline: `PolicyContext` bundles everything a guard needs to make a decision, and `PolicyResult` carries the outcome with a machine-readable code. Their immutability and slot optimization make them safe to pass across async boundaries without defensive copying.",
  "concepts": [
    "PolicyContext",
    "PolicyResult",
    "frozen dataclass",
    "slots optimization",
    "ABAC data types",
    "plan tier",
    "agent ceiling",
    "authorization input/output",
    "machine-readable codes"
  ],
  "categories": [
    "security",
    "enterprise edition",
    "authorization",
    "data modeling"
  ],
  "source_docs": [
    "0d1f8476a82e0083"
  ],
  "backlinks": null,
  "word_count": 477,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`policy.py` (`src/pocketpaw/ee/guards/policy.py`) provides the data layer for PocketPaw's ABAC authorization system. It contains no logic — only two frozen dataclasses that define the input and output contract for `evaluate_policy()`.

## PolicyContext

```python
@dataclass(frozen=True, slots=True)
class PolicyContext:
    user_id: str
    workspace_id: str
    role: WorkspaceRole
    action: str
    resource_id: str | None = None
    resource_type: str | None = None
    pocket_access: PocketAccess | None = None
    plan: str = "team"
    agent_id: str | None = None
    agent_creator_role: WorkspaceRole | None = None
```

`PolicyContext` is the canonical representation of "who is doing what, where, under what conditions." The required fields — `user_id`, `workspace_id`, `role`, `action` — are sufficient for basic RBAC checks. The optional fields enable advanced policies:

- `resource_id` and `resource_type`: allow future policies to inspect the specific resource being acted on (e.g., deny deleting pockets with active sessions)
- `pocket_access`: carries the user's pocket-level access level for pocket-scoped actions
- `plan`: drives the plan feature gate — defaults to `"team"` so that missing plan context is treated as the lowest commercial tier rather than allowing enterprise features
- `agent_id` and `agent_creator_role`: support the agent permission ceiling check, preventing privilege escalation via agents

Using `frozen=True` means a `PolicyContext` instance cannot be mutated after creation. This prevents a class of bug where middleware or logging code accidentally modifies the context between construction and evaluation. Using `slots=True` reduces memory overhead since many contexts may be instantiated per second in a high-traffic deployment.

## PolicyResult

```python
@dataclass(frozen=True, slots=True)
class PolicyResult:
    allowed: bool
    code: str = ""
    detail: str = ""
```

`PolicyResult` carries the authorization outcome. On denial, `code` is a stable machine-readable string (e.g., `"plan.feature_denied"`, `"agent.ceiling_exceeded"`) that the frontend maps to localized error messages. `detail` provides a human-readable explanation for logging and debugging.

The `allowed: bool` field being first is intentional — callers pattern-match on it:

```python
result = evaluate_policy(ctx)
if not result.allowed:
    raise HTTPException(status_code=403, detail=result.code)
```

By separating the boolean outcome from the code, callers can propagate just the code to clients without exposing the detail string (which may contain internal role names or resource IDs).

## Why Separate from abac.py?

Separating the data types (`policy.py`) from the evaluation logic (`abac.py`) prevents circular imports. The policy types are imported by both `abac.py` (to declare the function signature) and `deps.py` (to construct `PolicyContext` from request state). If the types lived in `abac.py`, `deps.py` would import `abac.py`, which imports `rbac.py`, creating a triangle that Python's import system may resolve differently than expected in some environments.

## Known Gaps

- `plan` defaults to `"team"` when not explicitly set. If a new plan tier is introduced (e.g., `"free"`), all existing code that doesn't explicitly set `plan` would incorrectly grant team-tier access. A `None` default with an explicit check would be safer.
- `resource_type` is defined but not used by any existing policy rule in `abac.py`, making it a placeholder for future resource-scoped policies.