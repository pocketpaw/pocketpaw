<!-- Smoke-test guide — WARM hot-process reuse (WH-1..WH-4). Created 2026-06-30.
     Branch: the warm-reuse integration branch (dev + WH-1..WH-4). Run before shipping. -->

# WARM hot-process reuse — Smoke Test Guide

Built on top of Session Supervisor v1 (#1596/#1597). Same flag —
**`POCKETPAW_SESSION_SUPERVISOR`** (default off). With it on, a supervised session
now keeps its `claude` client **warm** across turns: turn 2+ reuses the live
subprocess instead of resuming COLD (re-materialize + a fresh `claude` connect).

## 0. Test-level (already green, re-run to confirm)

```bash
cd pocketPaw && uv sync --dev --group ee   # if the worktree venv isn't reused
uv run pytest \
  tests/test_claude_sdk_warm_lease.py tests/test_session_supervisor.py \
  tests/cloud/runs/test_run_core_session_supervisor.py tests/cloud/runs/ -q -o addopts=""
# expect: green (WH-1..WH-4 + the full chat-runs lane)
```

## 1. Prerequisites (cold box)

Same as the v1 session-supervisor smoke (`docs/handoff/2026-06-30-session-supervisor-smoke.md`):
cloud features 401 without a **`POCKETPAW_LICENSE_KEY`**, and you need a provisioned
**workspace + `pocketpaw`-slug agent + a chat scope** (group/session/pocket).

```bash
cd pocketPaw
POCKETPAW_SESSION_SUPERVISOR=true POCKETPAW_LICENSE_KEY=<your-key> \
  uv run pocketpaw serve --port 8888
```

## 2. The headline check — turn 2 reuses the SAME `claude` process

This is the whole point: warm reuse means the same live subprocess serves turn 2,
not a fresh one.

1. Open a chat. **Before** sending, note the baseline `claude` pids:
   ```bash
   pgrep -fl claude | grep -v 'claude-code\|Claude.app\|cmux'
   ```
2. Send **turn 1** (*"My favorite color is teal. Acknowledge."*). A new `claude` pid
   appears (the per-session warm client). Record it:
   ```bash
   pgrep -fl claude | grep -v 'claude-code\|Claude.app\|cmux'
   ```
3. Send **turn 2** in the SAME chat (*"What's my favorite color?"* → "Teal"). Check the
   pids again:
   ```bash
   pgrep -fl claude | grep -v 'claude-code\|Claude.app\|cmux'
   ```
   **PASS:** the turn-1 `claude` pid is **still there and unchanged** — turn 2 reused
   it (no second `connect`, no re-materialize). A *new* pid for turn 2 means warm reuse
   didn't fire (regression).

## 3. Cold fallback after idle — a NEW pid that still answers

4. Wait past the warm TTL (default `warm_ttl=120s`; the idle reaper disconnects the
   warm client). Confirm the warm `claude` pid is gone:
   ```bash
   pgrep -fl claude | grep -v 'claude-code\|Claude.app\|cmux'
   ```
5. Send **turn 3** (*"Remind me my color?"* → still "Teal"). A **new** `claude` pid
   appears (cold) and the answer is still correct — it resumed from the store.
   **PASS:** new pid + correct answer = cold-recovery via resume works.

## 4. Latency note (warm vs cold)

Eyeball time-to-first-token: turn 2 (warm) should be noticeably faster than turn 1 /
turn 3 (cold) — no materialize + no process startup. Record rough numbers; this is the
payoff this tier exists for.

## 5. Quota cap — no unbounded `claude` processes

Under load (several active sessions in one workspace), the count of live `claude` pids
stays bounded by `max_warm_per_tenant` (default 8) — the supervisor LRU-evicts +
disconnects the oldest idle warm client when a tenant is over cap. This is asserted in
the test soak (`tests/test_session_supervisor.py::test_quota_cap_soak_*`), but you can
spot-check the live pid count stays ≤ the cap.

## 6. Flag OFF — unchanged

Restart without the flag. Chat behaves exactly as today; no warm clients are bound, the
per-agent path is byte-identical. Merging is inert until you flip the flag.

## Notes / known v1 limits

- **WARM is `claude_sdk` only** this iteration (codex etc. still cold).
- **Warm turns withhold the resume id** by design: the live client already carries the
  conversation natively, and threading resume would force a cold re-materialize (the
  backend's warm path is gated on `not resume_active`). Resume is the cold-recovery path
  only. A warm turn still keeps the durable `(workspace,session,agent)→cli_session_id`
  mapping for later cold resume.
- **Skills + leased clients — follow-up:** a leased-client turn that carries skills
  adopts a materialized skills dir that is reclaimed at the backend's `cleanup()` sweep
  rather than at the per-leased-client teardown. Benign retention gap (not a process
  leak; can't rug-pull since the flag is process-global). Fix: have the teardown also
  drop the dir — folds into a later touch of the WH-1/WH-2 surface.

## Ship

Once §2–§3 pass live, the branch is ready to ship to dev (squash, like v1).
