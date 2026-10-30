# Cloud agents default to `pydantic_ai`

2026-08-01. What changed, what deliberately did not, and what has to be true
before the rest of it happens.

## What changed

A **new** cloud agent that does not name a backend now gets `pydantic_ai`
instead of `claude_agent_sdk`. The value lives in one place,
`pocketpaw_ee.cloud.agents.defaults.CLOUD_DEFAULT_AGENT_BACKEND`, and the four
schemas an agent can arrive through read it: the Beanie document
(`models/agent.py`), the domain value object (`agents/domain.py`), the
create-request DTO (`agents/dto.py`) and the planner's agent creation.

They were four independent literals before, which is how a default ends up
depending on which caller created the agent.

## What did not change, and why

**The self-hosted default.** `Settings.agent_backend` is still
`claude_agent_sdk`, and `pocketpaw_ee` is not importable from OSS core anyway.
`pydantic_ai` is dispatch-only by design — no shell, no filesystem — because one
process serves every tenant and PocketPaw's builtin tools jail against a
process-global path. That trade is right in a multi-tenant cloud and wrong on a
laptop, where "a self-hosted AI agent that runs locally" is the product. These
are two different correct answers, not one someone forgot to unify.

**Existing agents.** Every `AgentDoc` in Mongo stores its backend explicitly, so
a default change is invisible to them. They keep running `claude_agent_sdk`
until something rewrites the field, and nothing here does. That is the safe
behaviour, and it is also a decision deferred rather than made.

**The pool's fallback.** `AgentPool._build` still reads
`config.get("backend", "claude_agent_sdk")`. `AgentConfig` carries a default, so
`model_dump()` always includes the key and that fallback only fires for a
document written before the field existed — which is to say a document from when
`claude_agent_sdk` was the default. Answering with today's default would
silently re-home the oldest agents in the estate.

## Before migrating existing agents

Two things are unfinished, and both bear on whether a migration is a good idea.

**PA-1 has not been run.** The concurrency measurement that justifies this
backend over `deep_agents` — 250 concurrent runs, memory and throughput — is
still unrun; there are no results under `scripts/loadtest/`. The premise is
sound (a CLI subprocess per run at 300-500 MB RSS does not fit a box) but it is
a premise. New agents are a small, reversible population to test it on. Every
existing agent is not.

**Composio tools do not reach this backend.** `composio_tools_for()` is called
by `deep_agents`, `google_adk` and `openai_agents`, and not by the pydantic-ai
tool builder. A Composio-configured workspace that migrates an existing agent
would lose those integrations. Fixing it needs a decision, because Composio tool
names are dynamic (`GMAIL_SEND_EMAIL`) and the backend's tool allowlist approves
by name — they would have to be approved by provenance instead.

## The migration, when it is wanted

Not written as a script on purpose: it is a one-line update against a
production collection and it should be run by someone who has read the two
caveats above.

```js
// Agents that never chose a backend explicitly cannot be distinguished from
// those that chose the old default — the field records the value, not the
// intent. So this migrates BOTH, which is why it wants a decision rather than
// a cron job.
db.agents.updateMany(
  { "config.backend": "claude_agent_sdk" },
  { $set: { "config.backend": "pydantic_ai" } }
)
```

Do it per workspace first, not estate-wide. `POCKETPAW_PYDANTIC_AI_MODEL` has to
be set for the deployment, and the surfaces those agents run on should be
checked against the gating work in PR #1815 — the other six non-Claude backends
still crash on a surface that sets `deny_mcp_tool_ids`, so a workspace with a
fallback chain configured needs that looked at first.

## Rollback

Change `CLOUD_DEFAULT_AGENT_BACKEND` back. Nothing else stores a copy, which was
the point of introducing it. Agents already created keep whatever they were
given, in both directions.
