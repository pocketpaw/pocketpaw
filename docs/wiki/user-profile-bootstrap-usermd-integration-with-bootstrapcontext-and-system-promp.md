---
{
  "title": "User Profile Bootstrap: USER.md Integration with BootstrapContext and System Prompt Injection",
  "summary": "The bootstrap system supports a `USER.md` file that stores human-readable user profile information (name, timezone, preferences) which is loaded into `BootstrapContext` and injected into the agent's system prompt under a `# User Profile` section. Tests verify the default empty state, template creation, idempotent non-overwrite, loading, system prompt injection, and graceful handling of a missing file.",
  "concepts": [
    "BootstrapContext",
    "DefaultBootstrapProvider",
    "USER.md",
    "user_profile",
    "system_prompt",
    "bootstrap",
    "idempotency",
    "get_context",
    "to_system_prompt"
  ],
  "categories": [
    "bootstrap",
    "testing",
    "personalization",
    "test"
  ],
  "source_docs": [
    "8fc4f0a7a526ff69"
  ],
  "backlinks": null,
  "word_count": 349,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

When an agent starts, the bootstrap system assembles a system prompt from several components: the agent's identity, soul state, style preferences, and — if present — a user profile. The user profile allows the human owner to describe themselves to the agent: their name, timezone, communication preferences, or any context that should persist across sessions without being stored in the soul's memory tiers.

## BootstrapContext and User Profile Field

`BootstrapContext` holds all the data that goes into the system prompt. The `user_profile` field defaults to an empty string. When empty, the `# User Profile` section is entirely omitted from `to_system_prompt()` — preventing an empty section header from confusing the agent.

```python
def test_user_profile_not_in_prompt_when_empty(self):
    ctx = BootstrapContext(name="Test", identity="id", soul="soul", style="style")
    assert "# User Profile" not in ctx.to_system_prompt()

def test_user_profile_in_prompt_when_set(self):
    ctx = BootstrapContext(..., user_profile="Name: Alice\nTimezone: PST")
    prompt = ctx.to_system_prompt()
    assert "# User Profile" in prompt
    assert "Name: Alice" in prompt
```

## DefaultBootstrapProvider and USER.md

`DefaultBootstrapProvider` manages the identity directory on disk. On initialization, if `USER.md` does not exist, it creates a template with `# User Profile`, `Name:`, and `Timezone:` fields, giving users a starting point to fill in.

Critically, if `USER.md` already exists, it is **not overwritten** — this is an idempotency guard that preserves user customizations across PocketPaw updates or reinstalls. The test verifies this explicitly:

```python
async def test_user_md_not_overwritten(self, temp_identity_path):
    (temp_identity_path / "USER.md").write_text("Name: Bob")
    DefaultBootstrapProvider(base_path=temp_identity_path)
    assert (temp_identity_path / "USER.md").read_text() == "Name: Bob"
```

## Loading and System Prompt Integration

`get_context()` reads `USER.md` and populates `BootstrapContext.user_profile`. The content is injected verbatim into the system prompt, allowing arbitrary markdown formatting. Tests confirm the full round-trip: write `USER.md` → `get_context()` → `to_system_prompt()` contains the expected content.

## Missing File Graceful Handling

If `USER.md` is deleted after the provider is initialized, `get_context()` returns an empty `user_profile` rather than raising `FileNotFoundError`. The system prompt then omits the profile section cleanly. This handles the case where a user manually deletes the file or it is removed during migration.

## Known Gaps

No TODOs. Some async test methods are defined without `@pytest.mark.asyncio`, which may require pytest-asyncio's `asyncio_mode = "auto"` setting to run correctly.