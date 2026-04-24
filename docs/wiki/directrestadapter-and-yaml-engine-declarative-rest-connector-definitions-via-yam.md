---
{
  "title": "DirectRESTAdapter and YAML Engine: Declarative REST Connector Definitions via YAML Files",
  "summary": "`yaml_engine.py` enables third-party REST API connectors to be defined entirely in YAML without writing Python code. The `DirectRESTAdapter` reads a `ConnectorDef` parsed from a YAML file and executes actions as HTTP requests via `httpx`, templating parameters from the agent's action invocation into the request body, headers, and URL.",
  "concepts": [
    "DirectRESTAdapter",
    "ConnectorDef",
    "YAML connector definition",
    "httpx",
    "REST adapter",
    "parameter templating",
    "declarative integration",
    "parse_connector_yaml",
    "sync block",
    "authentication injection"
  ],
  "categories": [
    "connectors",
    "architecture",
    "REST integration"
  ],
  "source_docs": [
    "c6e662129758acdd"
  ],
  "backlinks": null,
  "word_count": 452,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`yaml_engine.py` is the mechanism that makes PocketPaw's connector system extensible without code changes. A developer (or operator) drops a YAML file into the `connectors/` directory describing a REST API — authentication scheme, available actions, and HTTP request templates — and `DirectRESTAdapter` handles the rest.

## ConnectorDef

```python
@dataclass
class ConnectorDef:
    name: str
    display_name: str
    type: str = "generic"
    icon: str = "plug"
    auth: dict[str, Any] = field(default_factory=dict)
    actions: list[dict[str, Any]] = field(default_factory=list)
    sync: dict[str, Any] = field(default_factory=dict)
```

`parse_connector_yaml` loads a YAML file into a `ConnectorDef`, defaulting missing fields (the `name` falls back to the filename stem if not specified). Providing defaults rather than raising on missing fields lets minimal YAML files work out of the box.

## YAML Action Schema

A typical YAML action definition specifies:

```yaml
actions:
  - name: list_repos
    description: "List repositories for the authenticated user"
    method: GET
    url: "https://api.github.com/user/repos"
    trust_level: auto
    parameters:
      per_page:
        type: integer
        default: 30
```

`DirectRESTAdapter.execute` maps the `method` field to an `httpx` call, substitutes parameter values into the URL, query string, or JSON body (depending on the HTTP method), and returns the parsed response as an `ActionResult`.

## Authentication Handling

The `auth` section of the YAML defines how credentials are injected. The adapter resolves credential values from the pocket's stored configuration (passed to `connect()`) and inserts them as headers (e.g., `Authorization: Bearer {token}`) before each request. Credentials are never stored in the YAML file itself — only the parameter names are specified; values come from the encrypted credential store at runtime.

## Why YAML Instead of Code?

REST APIs follow predictable patterns: authenticate with a header, call a URL, parse JSON. Writing a Python class for each of hundreds of potential integrations would be unmaintainable. YAML definitions:

1. Allow non-engineers to add integrations without touching Python code.
2. Make connector definitions auditable as data files rather than executable code.
3. Enable the registry's hot-reload to pick up new connectors without a server restart.

## `sync` Section

The optional `sync` block in the YAML specifies endpoints for pulling data into Single Brain (list endpoint, cursor field for pagination, etc.). `DirectRESTAdapter.sync` implements pagination and hands records to the ingest pipeline. Most YAML connectors today have an empty `sync` block — it is defined for forward-compatibility.

## Known Gaps

- **No authentication type beyond Bearer**: The adapter currently supports `Bearer` token injection. OAuth flows, HMAC signing, and API key query parameters are not yet implemented in the YAML engine — connectors needing those must use a native Python adapter.
- **No response schema validation**: The adapter returns raw parsed JSON; it does not validate the response shape against an expected schema, so a breaking API change produces a confusing `ActionResult` rather than a typed error.
