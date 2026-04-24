---
{
  "title": "API v1 Schemas Package — Pydantic Schema Namespace",
  "summary": "The `schemas/__init__.py` file marks the `pocketpaw.api.v1.schemas` directory as a Python package. It contains no exports, serving purely as a namespace boundary that groups all Pydantic request and response models for the v1 API.",
  "concepts": [
    "Pydantic schemas",
    "package namespace",
    "API schemas",
    "schema organisation",
    "v1 API",
    "request models",
    "response models"
  ],
  "categories": [
    "schemas",
    "API",
    "architecture"
  ],
  "source_docs": [
    "9b5e454480bdf80e"
  ],
  "backlinks": null,
  "word_count": 267,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `schemas/` directory under `api/v1/` is a dedicated package for all Pydantic models used as request bodies and response types in PocketPaw's v1 REST API. The `__init__.py` is intentionally empty — it declares the package without re-exporting anything.

## Why a Dedicated Schemas Package

Separating schemas into their own subpackage rather than defining them inline in each router has several advantages:

**Discoverability** — Any developer looking for the shape of an API payload knows exactly where to look: `pocketpaw.api.v1.schemas.<domain>`. There is no need to scan router files for inline Pydantic models.

**Reuse** — Multiple routers can import the same schema without circular dependencies. For example, `ChatRequest` is used by both the main chat router and the pocket chat router. Placing it in `schemas/chat.py` means neither router has to know about the other.

**Testing** — Schema validation can be tested in isolation without instantiating a FastAPI app. A test can simply import `CreateKeyRequest` and assert that invalid payloads raise `ValidationError`.

**Code generation** — Tools that generate OpenAPI clients or documentation can target the schemas package directly.

## Namespace Organisation

Each file in the package corresponds to one API domain:

| File | Domain |
|---|---|
| `api_keys.py` | API key management |
| `auth.py` | Login and session tokens |
| `backends.py` | LLM backend info |
| `channels.py` | Channel adapter status |
| `chat.py` | Chat request/response |
| `common.py` | Shared base types |

The flat file-per-domain structure keeps individual schema files small and single-purpose.

## Known Gaps

No known gaps for the package itself — the `__init__.py` is intentionally minimal by design.
