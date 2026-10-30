"""The backend a cloud agent gets when nobody chooses one.

Created 2026-08-01.

One name instead of the five literals this replaced (``agents/domain.py``,
``agents/dto.py``, ``models/agent.py``, ``planner/service.py``, and the pool's
fallback). They had drifted apart in kind if not in value: three were schema
defaults, one was a call-site argument, one was a ``dict.get`` fallback, and
nothing tied them together, so changing the default meant finding all five.

**Cloud only, deliberately.** ``Settings.agent_backend`` — the OSS
self-hosted default — stays ``claude_agent_sdk`` and is not imported here.
PocketPaw self-hosted is a local agent, and ``pydantic_ai`` is dispatch-only by
design: no shell, no filesystem, because one process serves every tenant and
the builtin jail is process-global. That trade is right in a multi-tenant cloud
and wrong on a laptop, so the two defaults are genuinely different values rather
than one value someone forgot to unify.

**Existing agents do not move.** Every ``AgentDoc`` already in Mongo stores its
backend explicitly, so a default change is invisible to them; they keep running
``claude_agent_sdk`` until something rewrites the field. That is the safe
behaviour and it is also a decision deferred, not a decision made — see
``docs/handoff/2026-08-01-cloud-backend-default.md`` for the migration and the
measurement that should precede it.
"""

from __future__ import annotations

CLOUD_DEFAULT_AGENT_BACKEND = "pydantic_ai"
"""Backend for a NEW cloud agent that does not name one.

``pydantic_ai`` rather than ``claude_agent_sdk`` because the cloud's binding
constraint is concurrency: the Claude SDK backend spawns a CLI subprocess per
concurrent run at roughly 300-500 MB RSS, which is most of a box before any
other cost, while this one runs the agent loop in-process and pays roughly the
conversation context per run.
"""
