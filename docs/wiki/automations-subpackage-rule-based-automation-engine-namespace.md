---
{
  "title": "Automations Subpackage — Rule-Based Automation Engine Namespace",
  "summary": "The `pocketpaw.ee.automations` package is the namespace for PocketPaw's rule-based automation engine, grouping the models, bridge, evaluator, and store modules that together implement trigger-action workflows for enterprise pockets.",
  "concepts": [
    "automations package",
    "rule-based automation",
    "trigger-action workflows",
    "enterprise edition",
    "namespace package",
    "models",
    "bridge",
    "evaluator",
    "store",
    "router"
  ],
  "categories": [
    "enterprise-edition",
    "automations",
    "package-structure"
  ],
  "source_docs": [
    "1d3271bfe40fb417"
  ],
  "backlinks": null,
  "word_count": 437,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`pocketpaw.ee.automations` is a namespace package for the Enterprise Edition's rule-based automation engine. The `__init__.py` itself contains a single-line comment declaring creation date — no runtime code, no exports. Its value is entirely organizational: it makes the directory a Python package and marks the boundary of the automations subsystem.

## Package Contents

The automations subpackage groups five modules that together implement the full automation engine:

### `models.py`

Pydantic data models for automation rules. Defines `Rule` (the core automation entity), `RuleType` (`threshold`, `schedule`, `data_change`), `ExecutionMode` (`require_approval`, `auto_execute`, `notify_only`), and the `CreateRuleRequest` / `UpdateRuleRequest` API models. Everything else in the package depends on these models.

### `bridge.py`

The bridge syncs automation rules to core daemon Intentions. When a user creates a schedule rule, the bridge converts it to a cron Intention the daemon already knows how to run. When a rule is deleted, the bridge removes the linked Intention. This keeps the automations engine composing with — not duplicating — the core scheduling infrastructure.

### `evaluator.py`

A singleton background loop that periodically evaluates threshold and data_change rules. It checks live Fabric data against rule conditions and fires matched rules through the Instinct pipeline (for approval-required rules) or directly via the daemon (for auto-execute rules). Schedule rules do not go through the evaluator — they fire via the daemon's cron trigger.

### `store.py`

Persistence layer for rule CRUD. Rules are stored (typically in the same SQLite-backed store as other MC objects) and loaded by the evaluator on each evaluation cycle.

### `router.py`

FastAPI router that exposes CRUD endpoints for rules (`POST /automations/rules`, `GET /automations/rules`, `PATCH /automations/rules/{id}`, `DELETE /automations/rules/{id}`) and evaluator lifecycle endpoints (`POST /automations/evaluator/start`, `POST /automations/evaluator/stop`).

## Design Philosophy

Automations are built to compose with core primitives rather than replace them. Schedule rules become Intentions. Evaluator-fired rules go through the Instinct pipeline. This means automations inherit all the safety, auditability, and observability of the core systems without custom infrastructure.

## Import Pattern

Because `__init__.py` exports nothing, consumers import directly from submodules:

```python
from pocketpaw.ee.automations.models import Rule, RuleType
from pocketpaw.ee.automations.evaluator import get_evaluator
```

## Known Gaps

- No `__all__` or convenience re-export layer. Consumers must know the internal module structure.
- Created 2026-03-30; this is a relatively new subpackage. Some modules may be partially implemented stubs at time of writing.
- There is no `__version__` or version tracking at the subpackage level. Breaking changes to the automations API (e.g., rule schema changes) would not be surfaced through import metadata.
- The package does not validate at import time that the EE license is active for the current deployment. License enforcement is assumed to be handled at the API routing layer.
