---
{
  "title": "Fleet Module Entry Point — One-Command Bundle Installation",
  "summary": "The `ee/fleet/__init__.py` is the public entry point for PocketPaw's Fleet subsystem, re-exporting the models, installer functions, and report types so callers can manage installable bundles — FleetTemplates — with a single import. Fleet was introduced in Move 7 PR-B to let non-technical operators install a complete agent configuration (soul, pocket, connectors, scopes) in one command.",
  "concepts": [
    "FleetTemplate",
    "install_fleet",
    "load_fleet",
    "list_bundled_fleets",
    "FleetInstallReport",
    "soul template",
    "pocket",
    "connector",
    "scope tags",
    "one-command install"
  ],
  "categories": [
    "fleet",
    "module organisation",
    "orchestration",
    "installation"
  ],
  "source_docs": [
    "50b4410851b1d864"
  ],
  "backlinks": null,
  "word_count": 251,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## What Is a Fleet?

A Fleet is an installable YAML manifest that wires together four things that previously had to be set up independently:

1. **Soul** — the AI companion's identity and personality (via a bundled soul template).
2. **Pocket** — the agent's workspace and data container.
3. **Connectors** — external integrations to register at install time.
4. **Scopes** — the scope tags that control what data the agent can access.

The goal is to reduce a multi-step technical setup into a single operator action. The module comment captures this intent: "A non-technical operator can install the whole bundle in one step."

## Public API

The `__all__` list exports exactly what external callers need:

- `FleetTemplate` — the manifest model (YAML → Pydantic).
- `FleetConnector` — one connector registration within a manifest.
- `install_fleet` — the async orchestration function.
- `load_fleet` — load a template from disk or a bundled name.
- `list_bundled_fleets` — enumerate available bundled templates.
- `FleetInstallStep` + `FleetInstallReport` — progress and result reporting.

## Why a Separate Package?

Fleet could have been a function in the soul or pocket module. The separate package signals that Fleet is an orchestration layer — it calls into existing primitives (SoulFactory, ConnectorRegistry, Pocket service) without owning any of them. The installer is intentionally a "pure orchestration" module: no new runtime concepts, just coordination.

## Known Gaps

No known gaps in the entry point itself. See `installer.py` for the path-traversal clamp added in `fix/fleet-install-auth-guard` and the journal emission added in `feat/fleet-journal-emission`.