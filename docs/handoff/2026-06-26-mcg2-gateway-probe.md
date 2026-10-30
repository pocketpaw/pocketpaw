<!--
MCG-2 GATE findings note (NEW FILE, 2026-06-26).
Records the live-proxy probe results for routing Claude Code through the
LiteLLM proxy: prompt-cache survival, tool/stream fidelity, spend logging,
and the captain-config gaps found. Durable deliverable for slice MCG-2.
-->

# MCG-2 — Claude Code through LiteLLM: gateway probe findings

**Date:** 2026-06-26
**Proxy:** `https://litellm.hzd.interacly.com` (key alias `paw-enterprise`, `sk-...2qyw`)
**Slice:** MCG-2 (the GATE) — does routing the spawned `claude` CLI through the
LiteLLM proxy preserve the economics (prompt cache) and fidelity (tools +
streaming) we depend on, and does the proxy meter the spend?

---

## GATE answer (up front)

**Does Anthropic prompt-cache survive the LiteLLM hop? → BLOCKED on a proxy-config
gap, but the proxy itself preserves cache metadata (proven via DeepSeek).**

- We could **not** measure Anthropic prompt cache through the proxy because
  **every Anthropic model group on the proxy fails with `authentication_error:
  invalid x-api-key`** — the error is raised by Anthropic's own API and relayed
  through LiteLLM, i.e. the proxy's *upstream Anthropic credential is invalid or
  expired*. This is a captain proxy-config gap, not a code or design problem.
  Tested: `claude-3-haiku`, `claude-3-opus`, `anthropic/claude-3-5-haiku`,
  `anthropic/claude-haiku-4-5`, and the `claude-sonnet-4-5` fallback — all 401.
- **Fallback proof that caching survives the hop:** DeepSeek automatic caching
  through `{BASE}/chat/completions` returned **`prompt_cache_hit_tokens: 2944`**
  (of 3056 prompt tokens) on a repeated large prefix via model group
  `deepseek/deepseek-chat`. The proxy passes the cache-usage fields through
  faithfully — it does **not** strip cache metadata. So once a valid Anthropic
  key is configured on the proxy, Anthropic `cache_creation_input_tokens` /
  `cache_read_input_tokens` should survive the same way. (The proxy even records
  the cache split in its spend log; see below.)
- **Per-model-group caveat:** the bare alias `deepseek-chat` returned
  `cached_tokens: 0` (routes to a non-native-DeepSeek deployment that doesn't
  surface cache fields), while `deepseek/deepseek-chat` returned the hit.
  Cache behaviour is per-deployment on this proxy, not universal — the model
  group chosen matters.

### What unblocks the Anthropic cache measurement
Configure a **valid Anthropic API key** on the proxy for the `claude-*` /
`anthropic/*` model groups, then re-run `scratchpad/probe_gate.py claude-3-haiku`
(sends a >1024-token `cache_control: ephemeral` system block twice and reads
`cache_creation_input_tokens` on call 1 / `cache_read_input_tokens` on call 2).

---

## Probe results

### 1. Prompt cache (the GATE)
| Path | Model group | Result |
|------|-------------|--------|
| Anthropic `/v1/messages` cache_control | `claude-3-haiku` & 4 others | **401 invalid x-api-key** (proxy upstream key bad) |
| DeepSeek auto-cache `/chat/completions` | `deepseek/deepseek-chat` | **PASS — `prompt_cache_hit_tokens: 2944`** |
| DeepSeek auto-cache `/chat/completions` | `deepseek-chat` (bare alias) | cached_tokens 0 (different deployment) |

**Takeaway:** caching survives the proxy hop; the Anthropic-specific measurement
is gated only by the missing/invalid upstream Anthropic key.

### 2. Tool-calling + streaming fidelity — **PASS**
`{BASE}/chat/completions` with a `tools` definition + `stream: true` on
`deepseek/deepseek-chat`:
- 12 streamed SSE chunks received.
- A `tool_call` came back and assembled cleanly across chunks:
  `get_weather({"city": "Paris"})`.
- No format-translation issues observed on the OpenAI-compat surface.
- `gpt-4o` could not be used as a second witness: the proxy's OpenAI group is
  **quota-exhausted (429 "exceeded your current quota")** and its
  `claude-sonnet-4-5` fallback hits the same invalid Anthropic key. Separate
  config gaps; fidelity is already proven via DeepSeek.

### 3. Spend logging — **PASS (fully working)**
- `GET {BASE}/spend/logs?api_key=<key>` returns **per-call rows**. Our exact
  DeepSeek probe call is logged with `model: deepseek/deepseek-chat`,
  `total_tokens: 3082`, `prompt_cache_hit_tokens: 2944`, and a full
  `cost_breakdown` (input/output/total cost). Cache metering is preserved in
  the spend log too.
- `GET {BASE}/key/info` shows cumulative spend for our key
  (`spend: 0.00457…`, `last_active` updated to the probe time).
- `GET {BASE}/global/spend` returns `{"spend": 0.000975…}`.
- Note: `GET {BASE}/spend/logs` **without** the `?api_key=` filter times out
  (it tries to dump all logs) — always pass the filter.

---

## Code: verified vs added

**Verified (wiring already complete — no redundant code added):**
The seam that routes the spawned `claude` CLI through the proxy is intact:
- `src/pocketpaw/llm/providers/litellm.py` — `LiteLLMAdapter.resolve_config`
  reads `settings.litellm_api_base` (→ `base_url`) and `settings.litellm_api_key`
  (→ `api_key`); `build_env_dict` emits `ANTHROPIC_BASE_URL` + `ANTHROPIC_API_KEY`.
- `src/pocketpaw/llm/client.py` — `resolve_llm_client(force_provider="litellm")`
  builds an `LLMClient` carrying the proxy base (stored in
  `openai_compatible_base_url`) and key; `to_sdk_env()` → `build_env_dict`
  produces the subprocess env.
- `src/pocketpaw/agents/claude_sdk.py` — `_build_options` calls
  `resolve_llm_client(force_provider=self.settings.claude_sdk_provider)`,
  injects `llm.to_sdk_env()` into `options_kwargs["env"]` (the spawned
  subprocess env), and — critically — when the provider is non-Anthropic
  (`is_litellm` included) **smart routing is skipped** and the model is set
  from `llm.model` (lines ~1215, 1547-1548, 1590). So `smart_routing_enabled`
  does **not** clobber the litellm-chosen model. No gap.

**Added (the genuine coverage gap):** two unit tests in
`tests/test_llm_client.py` asserting the litellm-provider subprocess env points
`ANTHROPIC_BASE_URL` at the configured base **and** carries the key:
- `TestResolveLLMClient::test_resolve_litellm_routes_sdk_env_to_proxy` —
  end-to-end `Settings → resolve_llm_client → to_sdk_env`.
- `TestToSdkEnv::test_to_sdk_env_litellm` — direct `LLMClient` seam check.

**Test run:**
```
uv run pytest tests/test_llm_client.py -q  →  25 passed
uv run pytest <2 new tests> tests/test_provider_adapters.py -q  →  32 passed
uv run ruff check tests/test_llm_client.py  →  All checks passed!
```

---

## Captain-config gaps (action items, not code)
1. **Proxy Anthropic upstream key is invalid/expired** — all `claude-*` /
   `anthropic/*` model groups 401. Blocks the Anthropic prompt-cache GATE and
   any production routing of Claude Code through this proxy. **Fix this to fully
   close MCG-2.**
2. **Proxy OpenAI key is quota-exhausted** — `gpt-4o` returns 429 "exceeded your
   current quota". Lower priority (DeepSeek path works), but the OpenAI groups
   are unusable until topped up.
3. **`/spend/logs` (unfiltered) times out** — operational note: always query
   with `?api_key=` (or a date filter) rather than dumping all logs.

## How to re-run (probe scripts, in scratchpad — not committed)
```
export $(grep -v '^#' /Users/prakash-1/Documents/paw-workspace/.env | grep POCKETPAW_LITELLM | xargs)
python3 scratchpad/probe_gate.py claude-3-haiku        # Anthropic cache GATE (after key fix)
python3 scratchpad/probe_deepseek_cache.py deepseek/deepseek-chat   # cache-survives-hop proof
python3 scratchpad/probe_tools_stream.py deepseek/deepseek-chat     # tool + stream fidelity
# spend: curl "$BASE/spend/logs?api_key=$KEY" / "$BASE/key/info" / "$BASE/global/spend"
```
