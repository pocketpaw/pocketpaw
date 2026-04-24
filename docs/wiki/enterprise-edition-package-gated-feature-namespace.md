---
{
  "title": "Enterprise Edition Package — Gated Feature Namespace",
  "summary": "The `pocketpaw.ee` package is the root namespace for PocketPaw's Enterprise Edition, housing features gated behind team, business, and enterprise plans under the Functional Source License (FSL 1.1). It organizes five EE subsystems: guards (RBAC/ABAC), automations (trigger-action workflows), fabric (workspace mesh), instinct (proactive behaviors), and audit (compliance trail).",
  "concepts": [
    "enterprise edition",
    "EE namespace",
    "guards",
    "automations",
    "fabric",
    "instinct",
    "audit",
    "FSL 1.1",
    "RBAC",
    "ABAC",
    "gated features",
    "plan tiers"
  ],
  "categories": [
    "enterprise-edition",
    "licensing",
    "package-structure",
    "access-control"
  ],
  "source_docs": [
    "ab013e24a4ab6494"
  ],
  "backlinks": null,
  "word_count": 445,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`pocketpaw.ee` is a top-level namespace that signals a hard boundary: code in this package requires a paid plan. The package itself contains only a module map comment and license declaration — no runtime code. Its value is organizational, not functional.

## Module Map

```
guards/       — RBAC + ABAC authorization primitives and FastAPI dependencies
automations/  — Trigger-action workflows (time, event, webhook)
fabric/       — Workspace resource mesh and cross-pocket orchestration
instinct/     — Proactive agent behaviors and autonomous action policies
audit/        — Enterprise audit trail, compliance exports, and retention policies
```

### guards

Authorization primitives. RBAC (Role-Based Access Control) and ABAC (Attribute-Based Access Control) enforce that only authorized users can access EE features. FastAPI dependencies are provided so EE endpoints can require the right plan tier without implementing auth logic inline.

### automations

Rule-based workflows that fire on time schedules, metric thresholds, or data change events. This is the trigger-action automation engine — similar to Zapier-style rules but running inside the agent context, able to trigger agent actions rather than just HTTP calls.

### fabric

Workspace mesh for multi-pocket environments. `fabric` manages resource sharing and cross-pocket orchestration — relevant when a business account has multiple pockets (departments, projects) that need to share agents, data, or context.

### instinct

Proactive agent behaviors. Where standard PocketPaw responds to user requests, `instinct` enables agents to take autonomous actions based on conditions (e.g., "if revenue drops below threshold, draft a report and notify the team"). This is the highest-trust EE feature — it requires explicit policy configuration to avoid unintended autonomous actions.

### audit

Enterprise compliance infrastructure. Audit trail recording, compliance-format exports (SOC 2, GDPR), and data retention policy enforcement. Required for enterprise customers in regulated industries.

## Licensing

The module comment declares FSL 1.1 (Functional Source License). This is different from the core PocketPaw license — EE code is source-available but not open source. FSL transitions to permissive open source after a defined period (typically 2-4 years), so EE features eventually become community-accessible without requiring an immediate open-source commitment that would undermine the commercial model.

## Known Gaps

- As of the module comment (updated 2026-04-10), `guards` is the newest addition. `fabric`, `instinct`, and `audit` are declared in the module map but may be partially or fully unimplemented stubs.
- There is no runtime feature-flag check in `__init__.py` — plan enforcement is done within individual EE modules, not at package import time.
- The FSL 1.1 license is declared in a comment, not enforced programmatically. There is no runtime check that prevents core-tier users from importing EE modules directly.
- No `__all__` is defined, so `from pocketpaw.ee import *` would export nothing. Consumers must import from subpackages directly.
