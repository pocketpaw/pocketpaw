---
{
  "title": "Paw Print Widget Layer — Package Entry Point",
  "summary": "The `ee.paw_print` package provides the backend for customer-facing embedded widgets that close the decision loop: customer interactions on a rendered widget flow into a Pocket in real time, Instinct nudges the owner, and approved actions feed back to the widget. This module re-exports every public symbol from the two sub-modules.",
  "concepts": [
    "Paw Print",
    "PawPrintWidget",
    "PawPrintSpec",
    "PawPrintBlock",
    "PawPrintEvent",
    "PawPrintEventMapping",
    "PawPrintStore",
    "customer-facing widget",
    "decision loop",
    "Fabric object mapping",
    "embedded widget"
  ],
  "categories": [
    "paw print",
    "enterprise edition",
    "customer engagement",
    "widget layer"
  ],
  "source_docs": [
    "ee17420dcf36b458"
  ],
  "backlinks": null,
  "word_count": 391,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Paw Print is described in the source comments as "the full-stack decision loop Palantir cannot offer." The premise is that enterprise AI platforms have dashboards for internal operators but no clean way to wire customer-facing touchpoints (chat widgets, forms, feedback buttons) directly back into the AI decision layer. Paw Print bridges that gap.

The flow is:
1. Widget.js embeds on a customer-facing page.
2. A customer interacts (clicks a button, submits a form).
3. The interaction is POSTed to the Paw Print ingest endpoint.
4. The backend maps the event to a Fabric object in the relevant Pocket.
5. Instinct sees the new Fabric object and may propose an action to the Pocket owner.
6. The owner approves, and the approved action result can optionally be surfaced back in the widget spec.

## Exported Symbols

### From ee.paw_print.models
- `PawPrintBlock` — minimal render primitive (text, image, list, button, form, divider). No raw HTML, no script paths.
- `PawPrintSpec` — the full payload the widget bundle fetches and renders. Capped at 64 blocks.
- `PawPrintWidget` — the widget configuration: allowed domains, access token, rate limits, event mappings.
- `PawPrintEventMapping` — describes how an inbound widget event type maps to a Fabric object type and field assignments.
- `PawPrintEvent` — one captured customer interaction event.

### From ee.paw_print.store
- `PawPrintStore` — async SQLite CRUD for widgets and events, plus rate-limit enforcement.

## Design Positioning

The comment "Palantir cannot offer" is a direct competitive positioning note. Palantir's Foundry product focuses on internal operator dashboards. Paw Print's differentiator is the customer-facing loop: real customer signals from embedded widgets flow directly into the AI agent's context, creating a feedback channel that internal-only analytics platforms lack.

## Module Structure

`__init__.py` is a pure re-export facade following the same pattern as `ee.instinct`. The file layout separates models (pure types, no I/O) from the store (all I/O), making each independently testable. The router (in `ee.paw_print.router`) is intentionally excluded from `__all__` — it is registered by the EE router registry at the application level, not imported by downstream consumers.

## Known Gaps

- The widget-to-action feedback path (approved action result surfacing back in the widget spec) is described in comments but not yet implemented — the current system is one-way (ingest only).
- No WebSocket or server-sent events support; widget spec updates require the customer's browser to poll.