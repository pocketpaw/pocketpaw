<!-- Smoke-test guide — CLI Agent Session Supervisor v1. Created 2026-06-30.
     Branch: integration/session-supervisor (= origin/dev + the 6 stacked commits).
     Run this before shipping the integration branch to dev. -->

# Session Supervisor v1 — Smoke Test Guide

Branch under test: **`integration/session-supervisor`** (origin/dev + SS-1..SS-5 + SS-7).
The feature is gated behind **`POCKETPAW_SESSION_SUPERVISOR`** (default off), so the
default path is unchanged; you opt in to test it.

## 0. Test-level integration (already green, re-run to confirm)

```bash
git fetch origin && git checkout integration/session-supervisor
cd pocketPaw && uv sync --dev --group ee   # if the worktree venv isn't reused
uv run pytest \
  tests/test_session_supervisor.py tests/test_claude_sdk_session_handle.py \
  tests/test_agent_session_store.py tests/test_multitenant_session_isolation.py \
  tests/cloud/test_session_transcript_store.py tests/cloud/test_agent_session_runtime_service.py \
  tests/cloud/test_multitenant_session_isolation.py tests/cloud/runs/ -q -o addopts=""
# expect: 180 passed
```

## 1. SS-1 live-confirm — native resume actually works (the one check tests can't make)

The automated tests spy on the wiring; this proves the CLI genuinely resumes and
honors a fresh system prompt. **PASS** = the second reply contains `BANANA`
(continuity) **and** ends with `[BETA]` (fresh system prompt honored on resume).

```bash
D=$(mktemp -d); cd "$D"
S=$(claude -p "Secret word is BANANA. Reply 'ok'." --output-format json | jq -r .session_id)
claude -p "What is the secret word?" --resume "$S" \
  --append-system-prompt "End every message with the marker [BETA]." \
  --output-format json | jq -r .result
```

If this fails, stop — the substrate assumption is wrong and SS-1 needs a rethink
before any of this ships.

## 2. Live stack — supervised resume end to end (flag ON)

Boot the backend with the flag on (full local boot recipe is unchanged; only the
env var is new):

```bash
cd pocketPaw
POCKETPAW_SESSION_SUPERVISOR=true uv run pocketpaw serve --port 8888
# frontend: cd paw-enterprise && VITE_API_URL=http://localhost:8888 ... bun run dev
```

**Resume continuity (the headline):**
1. Open a chat, send turn 1: *"My favorite color is teal. Just acknowledge."*
2. Send turn 2: *"What's my favorite color?"* → the agent answers **teal**.
3. Confirm it came from native resume, not history replay — check the runtime map
   got a native id for this session:
   ```bash
   mongosh paw-enterprise --quiet --eval \
     'db.agent_session_runtimes.find({}, {workspace:1, session_id:1, agent_id:1, cli_session_id:1}).sort({_id:-1}).limit(3)'
   ```
   Expect a row with a non-null `cli_session_id` for your workspace/session/agent.
   And the transcript store has the session:
   ```bash
   mongosh paw-enterprise --quiet --eval \
     'db.session_transcripts.countDocuments({})'   # > 0
   ```

**Durability across restart (the reason native resume beats a kept-alive process):**
4. Stop the backend (Ctrl-C), start it again with the flag on.
5. In the same chat, send turn 3: *"What was my favorite color again?"* → still
   **teal**. The warm process is gone, but the session resumed from the store.

## 3. Flag OFF — legacy path unchanged

6. Restart the backend WITHOUT the flag (`uv run pocketpaw serve --port 8888`).
7. Chat normally — everything behaves as it does today. No `agent_session_runtimes`
   rows are written for new chats; no behavior change. (This is the safety net:
   merging the branch changes nothing until you flip the flag.)

## 4. Multi-tenant isolation (covered by tests; spot-check optional)

The adversarial gate (`tests/.../test_multitenant_session_isolation.py`, 12 tests,
0 leaks) covers cross-tenant store reads, the runtime-map, quota cross-starvation,
and a leaked/guessed native id. If you want a live spot-check, run two workspaces
and confirm `db.agent_session_runtimes` keeps separate rows per `workspace` for the
same logical session/agent.

## What to watch / known v1 limits

- Every supervised turn resumes **cold** from the store (no warm hot-process reuse
  yet) — correct and durable, but you'll pay materialization each turn. WARM reuse
  is a follow-up.
- COLD `SessionRuntime`s stay resident in memory — fine for a smoke test, needs a
  prune for long SaaS uptime.
- Mongo `append`/upsert is read-modify-write — fine at low concurrency.
- Codex native resume is not wired yet (claude_sdk only).

## Ship-to-dev

Once §1–§3 pass, the branch is ready to ship to dev. Stacked-PR note: the 6 slice
PRs (#1590–#1595) are the per-slice review; this integration branch is the rollup.
Ship either by merging the integration branch to dev (squash) or by merging the
stack bottom-up with `--rebase`.
