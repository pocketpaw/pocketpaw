---
{
  "title": "Enterprise Extensions (ee/) Package Overview",
  "summary": "The `ee/` package is the boundary layer separating PocketPaw's open-source core from its commercially-licensed Enterprise features. It groups capabilities like the instinct decision pipeline, audit/compliance logging, automations engine, and singleton API accessors under a single FSL 1.1 license gate.",
  "concepts": [
    "enterprise extensions",
    "FSL license",
    "instinct pipeline",
    "automations",
    "audit logging",
    "fabric ontology",
    "singleton pattern",
    "package namespace",
    "feature boundary",
    "compliance"
  ],
  "categories": [
    "architecture",
    "enterprise",
    "licensing"
  ],
  "source_docs": [
    "18a1a356641be653"
  ],
  "backlinks": null,
  "word_count": 407,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## What the ee/ Package Is and Why It Exists

PocketPaw ships with a two-tier architecture: a permissively-licensed open runtime (`src/pocketpaw/`) and a commercially-licensed Enterprise layer (`ee/`). The `ee/__init__.py` serves as the manifest and namespace root for the enterprise tier — it documents what lives there and ensures the package is importable by the rest of the system.

The separation exists for two reasons. First, it enforces a clean feature boundary: anything that a paying enterprise customer needs (compliance tooling, advanced agent behaviours, multi-tenant workspace management) lives under `ee/`, while the open runtime remains independently useful. Second, it makes the license boundary auditable — a scan for `from ee.` imports immediately reveals which features require an enterprise license.

## Module Structure

```
ee/
  api.py          — Singleton accessors (InstinctStore, PawPrintStore)
  instinct/       — Decision pipeline: actions, approvals, audit trail
  automations/    — Time/data-triggered workflows
  audit/          — Enhanced compliance logging (SOC2, GDPR)
  fabric/         — Ontology layer: objects, links, properties
  cloud/          — Domain-driven REST API surface (auth, workspace, agents, ...)
```

## The Singleton Pattern Rationale

`api.py` sits at the package root rather than deep in a submodule because the open-source runtime tools (e.g. `pocketpaw.tools.builtin.instinct_tools`) need to reach enterprise stores without creating circular import chains. By exporting `get_instinct_store()` and `get_paw_print_store()` from `ee.api`, consumers get a stable, shallow import path that doesn't change when the internal layout of `ee/instinct/` evolves.

## Domain Breakdown

**instinct/** implements the agent decision pipeline — approvals, action logging, and the audit trail that compliance teams inspect. It's the heart of the enterprise offering.

**automations/** will let users express rules like "when inventory drops below 10, alert me" or "every Monday, generate the weekly report pocket" — time and data-driven triggers that turn the assistant into a proactive actor rather than a reactive one.

**audit/** is planned to extend instinct's built-in logging with export formats (CSV, JSON), configurable retention windows, and compliance report templates targeting SOC2 and GDPR.

**fabric/** models the ontology layer — typed objects ("Customer", "Invoice"), typed links between them, and property schemas. It gives agents structured world knowledge rather than raw text.

**cloud/** is the domain-driven REST surface: each domain (`auth`, `workspace`, `chat`, `pockets`, `agents`, `kb`) owns its own `router.py`, `service.py`, and `schemas.py` triad.

## Known Gaps

- `automations/` and `audit/` are placeholders as of 2026-03-28. No executable code exists yet; only module-level docstrings describing the intended functionality.
- `fabric/` is referenced in the overview but its implementation status is not reflected in this init file.