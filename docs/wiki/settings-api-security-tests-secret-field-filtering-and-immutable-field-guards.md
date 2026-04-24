---
{
  "title": "Settings API Security Tests — Secret Field Filtering and Immutable Field Guards",
  "summary": "This test module validates two critical security properties of the `/api/v1/settings` router: that GET never returns API keys or tokens (regression for a v0.4.16 leak), and that PUT rejects writes to security-critical immutable fields like `file_jail_path` and `bypass_permissions`. It also verifies normal settings read and write paths work correctly for non-secret, non-immutable fields.",
  "concepts": [
    "settings API",
    "secret field filtering",
    "SECRET_FIELDS",
    "immutable fields",
    "privilege escalation prevention",
    "GET /api/v1/settings",
    "PUT /api/v1/settings",
    "credential exposure regression",
    "bypass_permissions",
    "file_jail_path",
    "pydantic validation",
    "settings.save"
  ],
  "categories": [
    "testing",
    "security",
    "API",
    "configuration management",
    "test"
  ],
  "source_docs": [
    "47640edba0faf3f6"
  ],
  "backlinks": null,
  "word_count": 673,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`test_api_v1_settings.py` guards the runtime configuration API against two categories of security failure: credential exposure and privilege escalation through configuration mutation. The file was initially created on 2026-02-20 and received a significant addition on 2026-04-09 — the `test_get_settings_never_returns_secrets` test — which was added as a regression test for a real vulnerability discovered in v0.4.16.

## The v0.4.16 Regression

Before the fix, the GET `/settings` handler filtered out fields whose names started with `_` (underscore). The assumption was that secret fields followed a private-member naming convention. In practice, `SECRET_FIELDS` — the authoritative list of sensitive fields (API keys, bot tokens, passwords) — uses plain names like `telegram_bot_token` or `anthropic_api_key`. None of these start with `_`, so they all passed through the filter unredacted to any caller holding the `settings:read` scope.

The regression test encodes this lesson structurally: it creates a mock settings object whose `model_fields` includes every entry from the imported `SECRET_FIELDS` set, sets each to a `"SECRET_<fieldname>"` sentinel value, calls GET, and then:
1. Asserts that no `SECRET_FIELDS` key appears in the response JSON dict.
2. Scans the raw response text for the sentinel value strings, catching cases where a field might be renamed mid-filter but the value still leaks via a different key.

This two-layer check (key presence + value presence in raw text) is robust against future refactors that rename how secrets are serialized.

## `TestGetSettings`

**`test_get_settings_returns_dict`** validates the happy path: a settings object with two safe fields (`agent_backend`, `web_port`) is fully serialized. This baseline test exists so the secret-stripping logic can be changed without accidentally stripping every field.

**`test_get_settings_never_returns_secrets`** is the regression guard described above. It is parameterized implicitly over all `SECRET_FIELDS` and is the definitive statement of the security contract: secrets are filtered out entirely (not masked, not present at all).

## `TestUpdateSettings`

**`test_update_settings`** sends `{"settings": {"agent_backend": "openai_agents"}}` and confirms HTTP 200, that `setattr` was actually applied to the settings object (`settings.agent_backend == "openai_agents"`), and that `settings.save()` was called exactly once. The `save()` assertion is important: if it is not called, the change is in-memory only and will be lost on restart.

**`test_update_ignores_private_fields`** sends a payload mixing a valid field with `_internal`. The router is expected to skip any key starting with `_` and write only `agent_backend`. Without this guard, the API would expose internal-state fields to mutation via REST.

**`test_rejects_immutable_field`** is parameterized over every entry in `_IMMUTABLE_FIELDS`. For each one, a PUT with that field name should return HTTP 403 with the field name present in the `detail` message. The parameterization ensures that as new immutable fields are added to `_IMMUTABLE_FIELDS`, the test coverage expands automatically — no test code change needed.

**`test_rejects_multiple_immutable_fields`** sends both `file_jail_path` and `bypass_permissions` in one payload and checks that both names appear in the single 403 error detail. This tests that the router does not short-circuit on the first blocked field and silently apply the second. An implementation that raised an error on the first match and then fell through to apply remaining fields would pass `test_rejects_immutable_field` but fail here.

**`test_safe_field_still_accepted`** confirms that after all the immutable-field rejection logic, normal fields still work. This is the "and the good path still works" sanity check — without it, an overly aggressive block could silently reject all writes.

## Why Immutable Fields Matter

`file_jail_path` controls which filesystem paths the agent can access. `bypass_permissions` is a kill-switch for the permission model entirely. If either could be changed via REST, any caller with `settings:write` scope could escape the security sandbox. The 403 response (rather than 422) is intentional: the request is structurally valid but policy-forbidden.

## Known Gaps

- **`SECRET_FIELDS` is imported at module load time** — if new secret fields are added dynamically at runtime, the regression test would not catch their exposure until the next test run.
- There is no test for `PATCH` semantics (partial update) — all mutation tests use `PUT`, implying the endpoint may not support partial updates.
- No tests exist for concurrent settings writes, where two callers updating different fields could race and produce a merged or lost-write result.
