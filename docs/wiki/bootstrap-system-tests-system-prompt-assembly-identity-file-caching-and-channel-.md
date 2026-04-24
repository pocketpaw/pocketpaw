---
{
  "title": "Bootstrap System Tests: System Prompt Assembly, Identity File Caching, and Channel Hints",
  "summary": "This test suite validates PocketPaw's bootstrap pipeline, which reads identity configuration files (IDENTITY.md, SOUL.md, STYLE.md, USER.md) and assembles them into a structured system prompt for the AI agent. Tests cover the XML-tag layout contract of `BootstrapContext`, the mtime-based file cache in `DefaultBootstrapProvider`, and the channel-specific formatting hints added by `AgentContextBuilder`.",
  "concepts": [
    "BootstrapContext",
    "DefaultBootstrapProvider",
    "AgentContextBuilder",
    "system prompt",
    "identity files",
    "mtime cache",
    "channel hints",
    "IDENTITY.md",
    "USER.md",
    "XML tags"
  ],
  "categories": [
    "bootstrap",
    "testing",
    "system prompt",
    "identity management",
    "test"
  ],
  "source_docs": [
    "ec960e4fef3fb4af"
  ],
  "backlinks": null,
  "word_count": 479,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

When PocketPaw starts an agent session, it must produce a system prompt that gives the agent its persona, values, and contextual knowledge. The bootstrap subsystem handles this by reading a set of markdown files from a configurable directory, caching them for performance, and then assembling them into a precisely ordered XML-tagged prompt. This test file validates the correctness of every step.

## `BootstrapContext.to_system_prompt()` — Layout Contract

```python
class TestBootstrapContext:
    def test_identity_appears_after_instructions(self):
        prompt = ctx.to_system_prompt()
        instructions_pos = prompt.index("Tool docs go here.")
        identity_pos = prompt.index("<identity>")
        assert instructions_pos < identity_pos

    def test_user_profile_inside_identity_block(self):
        # USER.md content must be inside <identity>...</identity>
```

The ordering tests exist because large language models are sensitive to system prompt structure. PocketPaw places tool documentation (instructions) first so the model's attention is primed with capability context before it encounters the persona. The `user_profile` must be inside the `<identity>` block so the model associates it with the agent's self-knowledge rather than treating it as external context. Violating either ordering produces degraded agent behavior that is hard to diagnose.

## `DefaultBootstrapProvider` — File Cache with mtime Invalidation

```python
async def test_get_context_uses_cache(self, temp_identity_path):
    ctx1 = await provider.get_context()
    cached_snapshot = dict(dp._identity_file_cache)
    ctx2 = await provider.get_context()
    assert dp._identity_file_cache == cached_snapshot  # no re-read

async def test_cache_invalidates_on_file_change(self, temp_identity_path):
    future = time.time() + 10
    os.utime(identity, (future, future))  # force mtime forward
    ctx2 = await provider.get_context()
    assert ctx2.identity == "Updated identity"
```

The cache exists because `get_context()` is called on every agent turn. Without caching, every message would trigger disk I/O for up to four files. The `test_cache_invalidates_on_file_change` test uses `os.utime` with a future timestamp rather than relying on the filesystem's natural mtime progression. This prevents a flaky test caused by filesystems with 1-second mtime resolution: if the file write and the cache check happen within the same second, mtime appears unchanged and the invalidation fails. The explicit future mtime is a deliberate workaround for this filesystem limitation.

The missing-file test guards against a startup error if the user hasn't created USER.md. Rather than raising, the provider returns an empty string, which is the correct no-op behavior.

## `AgentContextBuilder` — Memory and Channel Hint Integration

```python
async def test_build_with_channel_hint(self):
    prompt = await builder.build_system_prompt(channel=Channel.WHATSAPP)
    assert "# Response Format" in prompt
    assert "WhatsApp" in prompt

async def test_build_passthrough_channel_no_hint(self):
    prompt = await builder.build_system_prompt(channel=Channel.CLI)
    assert "# Response Format" not in prompt
```

`AgentContextBuilder` is the orchestration layer that combines bootstrap context, memory context, and channel-specific formatting hints. The channel hint tests enforce the rule that messaging channels (WhatsApp, Telegram, Discord) receive formatting guidance (e.g., "keep responses short, avoid markdown tables"), while passthrough channels like CLI receive the prompt verbatim. This prevents the agent from outputting WhatsApp-inappropriate markdown.

## Known Gaps

No TODOs or FIXMEs are flagged. The `test_defaults_creation` test verifies that default files are created but does not assert on the exact default content of SOUL.md and STYLE.md — only IDENTITY.md is checked for the "You are PocketPaw" string.