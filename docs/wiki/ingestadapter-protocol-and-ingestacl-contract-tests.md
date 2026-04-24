---
{
  "title": "IngestAdapter Protocol and IngestACL Contract Tests",
  "summary": "This test module pins the wire-shape contract for `IngestAdapter` — a superset of `ConnectorProtocol` that adds a `permissions()` method for per-record access control — and validates the `IngestACL` dataclass that carries scope, visibility, and principal metadata. It was created as part of Move 7 PR-A to establish the ACL-aware connector interface before the fleet template runtime (PR-B) consumed it.",
  "concepts": [
    "IngestAdapter",
    "IngestACL",
    "ConnectorProtocol",
    "permissions method",
    "duck typing",
    "scope list",
    "visibility",
    "source_principal",
    "fleet template",
    "protocol contract",
    "public exports",
    "ACL"
  ],
  "categories": [
    "testing",
    "connectors",
    "access control",
    "protocol design",
    "data ingestion",
    "test"
  ],
  "source_docs": [
    "6dd13ce691b805c4"
  ],
  "backlinks": null,
  "word_count": 474,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Context and Motivation

`IngestAdapter` extends the standard `ConnectorProtocol` with one additional method: `permissions(pocket_id, record_id)`, which returns an `IngestACL` describing who can access a given record. The fleet template runtime (PR-B) discovers ACL-aware connectors by checking `hasattr(connector, "permissions")` — a duck-typing pattern rather than `isinstance` checking, because `ConnectorProtocol` is not decorated with `@runtime_checkable`.

These tests were written before PR-B landed to pin the contract that PR-B would depend on. This test-first approach prevents PR-B from silently shipping with a broken interface.

## `IngestACL` Dataclass Tests

`TestIngestACL` verifies three things:

- **Default state**: all fields (`scope`, `visibility`, `source_principal`, `metadata`) default to empty values (empty list, empty string, empty string, empty dict). This ensures ACL objects are safe to instantiate without arguments — connectors that don't implement per-record ACL can return `IngestACL()` and the runtime will treat it as maximally permissive.
- **Scope list propagation**: a scope like `"org:sales:leads"` set at construction time is readable at access time. The scope list is the primary mechanism for the fabric to filter record visibility.
- **Open metadata dict**: arbitrary key-value pairs can be stored in `metadata`, allowing connectors to pass channel-type, guest exclusion flags, or other implementation-specific context to downstream consumers without requiring schema changes.

## `FakeIngestAdapter` Reference Implementation

The `FakeIngestAdapter` class serves as a reference implementation — a duck-typed adapter that satisfies both `ConnectorProtocol` and the new `permissions()` contract. It demonstrates the intended behavioral pattern:

- Records without a `private_` prefix in their `record_id` return a public scope (`org:public:*`, visibility `"public"`).
- Records with a `private_` prefix return a restricted scope (`org:engineering:eyes-only`, visibility `"private"`).
- All calls are recorded in `permissions_calls` for assertion.

## `TestIngestAdapterContract`

These tests exercise the `FakeIngestAdapter` to verify the contract:

- **`test_fake_adapter_satisfies_connector_protocol`** — attribute presence checks for `connect`, `execute`, `sync`, `schema`. Because `ConnectorProtocol` is not `@runtime_checkable`, `isinstance` would always return `True` for any object; attribute checking is the documented structural verification pattern.
- **`test_fake_adapter_exposes_permissions_method`** — the critical gate: `getattr(adapter, "permissions", None)` must be callable. This is exactly the check PR-B's discovery logic performs.
- **Async ACL tests** — verify public vs. private scope routing and that call arguments (`pocket_id`, `record_id`) are recorded accurately.

## `TestPublicExports`

Verifies that `IngestAdapter` and `IngestACL` re-exported from `pocketpaw.connectors` (the public API surface) are the exact same objects as those in `pocketpaw.connectors.protocol` (the definition module). `is` identity checks prevent a scenario where a re-export creates a duplicate type that fails `isinstance` checks at runtime.

## Module-Level Protocol Test

`test_ingest_adapter_protocol_extends_connector_protocol` uses `hasattr` on the Protocol classes themselves to enumerate expected methods. This catches a scenario where a refactor removes a method from `ConnectorProtocol` or `IngestAdapter` without updating downstream consumers.

## Known Gaps

No tests cover concurrent `permissions()` calls or caching behavior. The test comments acknowledge that `ConnectorProtocol` is not `@runtime_checkable`, which means type-checker-friendly `isinstance` checks are not possible — a future improvement would add `@runtime_checkable` to enable proper protocol checking.