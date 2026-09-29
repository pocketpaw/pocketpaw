# API capacity results (api_loadtest.py)

**Scope 1:** one uvicorn process running the real cloud app (`serve_sim.py`). These are per-process ceilings, not deploy capacity.
**Scope 2:** Redis runs in Docker Desktop on Windows (NAT overhead on every Redis call); MongoDB 8.2.5 runs as a native Windows service.
**Scope 3:** driver and server share one machine over loopback, so "bandwidth" below is server handling cost, not network throughput.

Two runs on 2026-09-29, same machine and commands, sim agent backend, no provider keys in the server's environment:

- **Baseline:** `origin/dev` at `6f15885c`. Everything from "Headline numbers" down describes this run.
- **After the fixes:** `origin/dev` at `dbb6d86b` (#2284-#2298), plus a multi-process run. Summarised in the next section.

## After the fixes (`dbb6d86b`)

One server process, idle machine:

| Scenario | Baseline | After |
|---|---|---|
| Mixed | 151 rps, healthy to 25 VUs | 355 rps, healthy to 100 VUs (200 VUs: p95 1091 ms) |
| Mixed, `--prod-middleware`, per-VU addresses | 83-91 rps | ~290 ok rps |
| Signup | 8.3 signups/s, fails at 25 VUs | ~30 signups/s, healthy to 50 VUs, server CPU ~250% |
| Upload 1 MB | 14/s, fails at 25 VUs | 16/s, healthy at 25, fails at 50 |
| Upload 256 KB | 27/s, fails at 50 VUs | 38/s, healthy at 50 |
| Chat, 64 concurrent sim runs | 3.9 runs/s, accept p95 489 ms | 4.4-4.8 runs/s, accept p95 ~280 ms |
| Realtime, spread arrivals | healthy at 2000; 4000: ticket p95 8.4 s | 8000 held, ticket p95 288 ms (with `--procs 4`; one driver process saturates first) |
| Soak, 20 VUs, 4 min | 118-164 rps | 362-372 rps, RSS 381-425 MB |
| Loop lag p99 | 28 -> 75 ms | 13 ms at every stage |

Single request, one VU: reads 2.6-3.5 ms p50, writes 5.5-6.5 ms, login/register/workspace create 26-28 ms (Argon2). `--prod-middleware` adds under 1 ms.

Several server processes (`serve_sim --workers N`, `POCKETPAW_REALTIME_BUS=redis-streams`, driver `--procs 4`):

| Workers | Mixed rps | Signups/s | 8000 sockets, ticket p95 | Server RSS idle | Box CPU (mixed) |
|---|---|---|---|---|---|
| 1 | 355 | ~30 | 288 ms | ~380 MB | 21% |
| 2 | 605-640 | ~47 | 31 ms | ~770 MB | 39% |
| 4 | 869 | ~58 | 10 ms | ~1.5 GB | 58% |
| 6 | 974 | ~58 | 8 ms | ~2.3 GB | 71% |

- **Paw Bar events at several workers.** Before this fix, the 4- and 6-worker mixed runs failed at 50 VUs on `pawbar.event` alone. Every event took SQLite's exclusive write lock (`BEGIN IMMEDIATE` in `admit_event`), even ones it refused, and the processes queued on it. WAL (#2301) did not remove that lock. With `POCKETPAW_REDIS_URL` set, the rate check now runs in Redis (`ee/pocketpaw_ee/paw_bar/admit.py`), and SQLite only takes a plain insert for admitted events. `pawbar.event` p95 at 6 workers, 429s included:

  | Setup | 25 VUs | 50 VUs | 100 VUs |
  |---|---|---|---|
  | WAL, limits 10M/min (every event admitted) | 457 ms | 2394 ms | 3396 ms |
  | Redis admit, limits 10M/min | 93 ms | 132 ms | 215 ms |
  | WAL, real limits (60/min, 10 per visitor) | 176 ms | 1274 ms | 3761 ms |
  | Redis admit, real limits | 59 ms | 78 ms | 135 ms |

  Both Redis runs are healthy to 100 VUs at 920-980 rps. SQLite's `events` rows equal the 2xx count (3288 and 61).
- **One worker without Redis, Paw Bar is still the knee.** At 1 worker without Redis (the fallback path), `pawbar.event` p95 is 1.6 s at 50 VUs. It is the same with `dev`'s code and with the store from before WAL, so it predates this change. With Redis at 1 worker it is 212 ms at 50 VUs. These runs used a fresh scratch HOME, whereas the earlier 1-worker run that stayed healthy to 100 VUs did not. Why a fresh HOME is slower was not investigated.
- **Signups flatten at ~58/s from 4 workers:** 8-9 cores of Argon2. That is the box, not a bug.
- **Request logs:** the ~1.2 Mongo inserts per request below are `request_logs` rows, already batched through `insert_many` with a 30-day TTL. Every row used to read `actor_id: "anonymous"`; fixed in #2300.
- **Sign-up limiter:** register keyed its (IP, email) bucket on an email read with `request.form()`, which is empty for a JSON body, so every signup from one address shared one 5-per-15-minutes bucket. Fixed in #2302.

## Machine

- AMD Ryzen 5 5600, 6 cores / 12 threads, 31.9 GB RAM, Windows 11 Pro (10.0.26200)
- Python 3.12 (the main checkout's venv), uvicorn single process, `--log-level warning`
- Idle baseline: the loop-lag probe reads 12-28 ms at rest because Windows timers tick at ~15.6 ms, and shows a ~260-290 ms max roughly once per window even when idle. Read the lag columns as relative to that floor.
- The box was shared with other dev sessions, so system CPU jumped around (15-60%) in some windows. Server CPU is per-process and stays trustworthy.

## Headline numbers

SLO: p95 under 1000 ms for every endpoint with at least 10 samples; capacity errors (5xx, timeouts, dropped connections) under 5%. Closed-loop VUs, no think time, so VUs are "requests in flight", not "users online".

| Scenario | Max healthy concurrency | Throughput there | Worst p95 there | First to fail | First bottleneck (evidence) |
|---|---|---|---|---|---|
| Mixed (signup, browse, read, write, upload 256 KB, paw bar) | 25 VUs | 151 rps (peak 175 rps at 10 VUs) | 569 ms | 50 VUs: `workspace.create` p95 1085 ms | One Python process of CPU. Throughput is flat from 10 to 50 VUs while every authenticated endpoint's latency rises together (auth.me, a trivial read, p50 40 ms -> 339 ms) and loop lag p99 climbs 28 -> 75 ms. Unauthenticated paw bar routes stay at 16-92 ms p50 on the same loop, so per-request auth overhead is the likely cost center. Server CPU 164-169%. |
| Signup only (register, login, workspace, set-active) | 5 VUs | 8.3 signups/s (33 rps) | 342 ms | 25 VUs: `workspace.create` p95 1947 ms | Argon2 on the event loop. Two 41 ms hashes per signup, loop lag p99 59 -> 122 ms. Throughput never grows past ~8 signups/s. See finding 1. |
| Upload 1 MB (upload, download, delete) | 10 VUs | 14 uploads/s: 13.9 MB/s in, 13.9 MB/s out | 570 ms | 25 VUs: `uploads.create` p95 1379 ms | One core of Python work (server CPU 104-107%). Loop lag stays near baseline, so the time is in the multipart and storage path, not a blocked loop. |
| Upload 256 KB | 25 VUs | 27 uploads/s: 6.8 MB/s each way | 654 ms | 50 VUs: `uploads.create` p95 1218 ms | Same single core (106-109%). Fixed cost per upload dominates small files: ~37 ms per 256 KB upload against ~71 ms per 1 MB. |
| Paw Bar public (widget.js, spec, event) | not separately ramped | 9-10 rps per route inside the mixed run | 190 ms (event) at 50 VUs | did not fail | Cheapest routes in the app: SQLite store, no auth. |
| Chat, sim backend (8 s turns) | 64 concurrent runs (all seeded pockets) | 3.9 completed runs/s | accept p95 489 ms, TTFT p50 1.6 s / p95 2.4 s | did not fail; pocket count capped the ramp | Server CPU reached 100% of a core at 64 streaming runs, so ~1.5% of a core per concurrent run just to relay SSE and persist. 0 empty completions, 0 errors. RSS +40 MB for 64 runs (~0.6 MB/run). |
| Realtime `/ws/cloud`, spread arrivals | 2000 held sockets | ticket p95 260 ms, connect p95 37 ms | 260 ms | 4000 sockets (133 new connects/s): ticket p95 8.4 s, connect p95 4.8 s, 1 dropped | Memory is the cost of staying online: ~0.2 MB RSS per open socket (378 -> 575 MB at 1000, 781 MB at 2000, 1250 MB at 4000). The 2000 stage, including its 1000 new connects, used 18% of a core. The 4000 failure is the connect rate, not the held count: loop lag max hit 731 ms and the driver itself was at 43% CPU. |
| Realtime, reconnect storm (all of a stage's connects at once) | 500 | 250 simultaneous connects | ticket p95 1.7 s | 1000 (500 at once): ticket p95 7.0 s | Same as above. A deploy that drops every socket should expect a multi-second reconnect wave above ~250 clients per process. |
| Soak: mixed at 20 VUs for 4 min | - | 118-164 rps per 30 s window | 557-725 ms | none | RSS 388.9 -> 392.3 MB across 8 windows: no leak visible at this length. |

## 429 policy ceilings

Each VU normally sends its own synthetic `X-Forwarded-For` (10.x.y.z). serve_sim's uvicorn trusts that header from 127.0.0.1, so the server sees N distinct clients. `--single-ip` sends one address for everyone.

| Limiter | Key | Measured ceiling | How it showed |
|---|---|---|---|
| Paw Bar public gate `_public_ip_gate` (`paw_bar/router.py:706`) | client IP + widget | 10 rps sustained, 300 burst, per IP per widget (spec + event share it; widget.js is not gated) | single-ip mixed, 25 VUs, 30 s: 34 x 429 on spec/event once the 300 burst drained |
| Paw Bar per-widget buckets | widget / customer_ref | default 60/min overall, 10/min per customer_ref | Not hit: the seeded widget sets both to 10,000,000 so they cannot pass for capacity. A real widget at the defaults caps events at 1/s. |
| OSS `api_limiter` in `AuthMiddleware` (only with `--prod-middleware`) | `request.client.host` | 10 rps, 30 burst, per client address, applied to EVERY cloud JWT request | One shared address: 29 ok rps out of 148 attempted, 3552 x 429 in 30 s, across every authenticated route (`auth.me` is exempt). Per-VU addresses: 2 x 429 in 20 s. See finding 2. |
| `login_limiter` | (IP, email) | 5 per 15 min per pair | Not hit: every signup uses a fresh email. |
| Cloud invite / slug / social-exchange limiters | actor or IP | not exercised | No scenario calls those routes. |

## Findings

1. **Password hashing blocks the event loop.** fastapi-users hashes and verifies synchronously inside `async def`: `fastapi_users/manager.py:141` (`create`, on register) and `:656` (`authenticate`, on every login). Argon2id at m=65536, t=3, p=4 costs 41 ms per call on this CPU, and nothing else on the loop runs meanwhile. A signup pays it twice. That caps a process at roughly 8 signups/s and puts 41 ms of stall on every login for everyone else on the process. The repo already does this correctly for API keys (`ee/pocketpaw_ee/cloud/auth/api_keys.py:369`, `asyncio.to_thread`). The cloud router's own verifies at `ee/pocketpaw_ee/cloud/auth/router.py:512` and `:538` have the same shape.
2. **Every cloud user request goes through the OSS per-IP limiter.** In the deployed app (`pocketpaw/api/serve.py:68-70` mounts `AuthMiddleware`), a cloud JWT never makes `is_valid` true in `_auth_dispatch`: it is not the master token, contains no `:`, and is neither `pp_` nor `ppat_`. `EEAuthBridgeMiddleware` sets `full_access` only for superusers (`ee_auth_bridge.py:127`). So every non-superuser cloud request runs `api_limiter.check(request.client.host)` at `dashboard_auth.py:668`: 10 rps, 30 burst. Measured above. What that means in production depends on one setting:
   - If uvicorn trusts the proxy's forwarded header, every client address is capped at 10 rps. An office behind one NAT shares one bucket, and the web app's parallel fetches on page load can use a large part of the 30 burst.
   - If it does not, `client.host` is the proxy's own address and every tenant shares one 10 rps bucket. `uvicorn.run` in `api/serve.py` passes no `forwarded_allow_ips`, so it falls back to `FORWARDED_ALLOW_IPS` or `127.0.0.1`. Neither `deploy/coolify/docker-compose.yaml` nor `.env.example` sets it. I could not see the live deploy's environment, so this needs checking there before anyone acts on it.
3. **The same middleware reloads settings on every request.** `_is_genuine_localhost` (`dashboard_auth.py:93`, reached from `:632` for every cloud JWT request) calls `Settings.load()`, which reads config and the encrypted credential store. It measured 5.1 ms per call, uncached. With `--prod-middleware` and per-VU addresses (no 429s), mixed throughput fell from 151-175 rps to 83-91 rps. The 5 ms is most of that per-request cost; the body-limit layer and a token-file read (`dashboard_auth.py:528`) are also in that stack. The deployed app therefore has roughly half the per-process headroom the numbers above show without it.
4. **Workspace creation waits on the LiteLLM key mint.** `workspace/service.py:479` awaits `ensure_tenant_key` inline, even though the comment above it (`:470`) calls it non-blocking. It never fails the create, but it does block it: one proxy round trip normally, up to the admin client's 30 s timeout if the proxy hangs. With no proxy listening, this Windows box spent ~2.3 s per workspace create on the refused connect. serve_sim now points it at an in-process stub so the numbers above do not carry that artifact. The stub is served by the same process, which slightly understates signup capacity.
5. **Signup calls a third-party API per unique password.** `password_policy.py:69` opens a fresh `httpx.AsyncClient` for each Have I Been Pwned lookup. serve_sim disables it (`--hibp` turns it back on), so the signup numbers exclude that round trip.
6. **Rig bug, fixed here:** serve_sim's scratch-database drop ran an async Motor call after uvicorn shut down, failed inside a `suppress(Exception)`, and left a `loadtest_*` database behind on every clean exit. It now drops with a synchronous client and prints a warning on failure.
7. **Rig caveat, fixed since:** after the chat ramp, `POST /__loadtest/shutdown` released the port but the scratch database was not dropped. It happened again after two later chat ramps and did not reproduce with 8 concurrent runs. The shutdown route now drops the database before asking uvicorn to exit, and graceful shutdown is capped at 15 s, so a hang or a kill after the request no longer leaves one behind.

## How this scales

Grounded in what moved the numbers above, most effective first:

- **Run more processes.** Every ceiling here is one process on one core of Python. The mixed mix saturates at ~150-175 rps per process with the box mostly idle (system CPU 23-28%). `uvicorn --workers N` or more replicas scales close to linearly up to core count. The in-memory limiters and WebSocket manager are per process, so check what per-process buckets and socket-to-process routing mean before adding workers.
- **Move Argon2 off the loop.** Wrapping hash/verify in `asyncio.to_thread` (or overriding `UserManager.password_helper` with one that does) lets signups and logins use other cores instead of stalling the loop. Expect signup throughput to follow core count and login spikes to stop hurting everyone else.
- **Fix the OSS middleware's per-request cost before tuning anything else.** Caching `Settings.load()` in `_is_genuine_localhost`, or skipping that check when an `Authorization: Bearer` JWT is present, should recover most of the ~45% throughput the deployed stack loses to it.
- **Decide what the per-IP limit is meant to cap.** Either treat a verified cloud JWT as authenticated in `_auth_dispatch`, or key the bucket on user id instead of address. Separately, set `forwarded_allow_ips` to the proxy's network so address-keyed limits see real clients. If this runs multi-replica, a Redis-backed limiter gives one budget per client instead of one per process.
- **Trim the authenticated request path.** The bridge and the route dependency each decode the JWT, check revocation in Redis and load the user (`ee_auth_bridge.py:158-160`, `auth/core.py:331`). That doubled work is the likely reason a trivial `auth.me` costs as much as `pockets.list` under load. Resolving once per request and caching on `request.state` is the obvious experiment. Measure it with this rig before claiming it.
- **Realtime sizing:** ~0.2 MB per online socket, so 10k online users needs ~2 GB across processes. Connect rate is the tighter limit: budget ~100 new connects/s per process and stagger client reconnects (jittered backoff) after deploys.
- **Uploads** use one core at ~14 MB/s per process for 1 MB files. Production uses S3, which moves storage writes off the box but keeps multipart parsing in the process. Presigned direct-to-bucket uploads (the multipart endpoints already exist) take large files off the API path entirely.

## Skipped, and why

- **`pawbar_chat` (public concierge chat): not run.** Seeding is cheap: a `Site` row needs only `workspace`, `pocket_id` and `owner` and can be inserted directly, with no Cloudflare publish. What blocks it is that the concierge never runs on the sim backend. Legacy concierge runs are forced onto pydantic_ai (`AgentPool._deny_by_default_backend`, see `chat/runs/run_core.py:1598`), and the v2 runtime makes a direct pydantic_ai model call (`paw_bar/concierge_runtime.py:811`). Either way a turn is a real provider call that spends tokens. It needs a sim model hook in the concierge path, or a budgeted run with a real key.
- **Server-side Mongo cost** is recorded (`mongo_ops_per_s` in stages.json: ~3 queries and ~1.2 inserts per request in the mixed run; `RequestLogMiddleware` writes an audit row per request). Mongo was never the limiter at these rates.
- **Upload extraction/KB cost:** the upload payload is whitespace, so text extraction comes back empty and the listener skips KB ingest (an LLM compile). Comprehension is off (`POCKETPAW_FILE_COMPREHENSION_DAILY=0`). Real text uploads add in-process extraction work these numbers do not include.

## Reproduce

Server (Windows, PowerShell; point HOME at a scratch dir so uploads and the Paw Bar SQLite store stay out of your real `~/.pocketpaw`):

```powershell
$env:USERPROFILE = "D:\lt-home"; $env:HOME = "D:\lt-home"
$env:PAW_SIM_DURATION_MS = "8000"   # sim chat turns: 1.2 s TTFT, 8 s total
uv run python scripts/loadtest/serve_sim.py --port 8099 --pockets 64 --seed-out out/loadtest_seed.json
# add --prod-middleware for the OSS AuthMiddleware runs
# stop with: curl -X POST http://127.0.0.1:8099/__loadtest/shutdown   (a graceful exit, so the scratch db is dropped)
```

Driver (another shell; restart the server between runs for a clean RSS baseline):

```bash
D=scripts/loadtest/api_loadtest.py
uv run python $D --scenarios mixed  --stages 10,25,50,100,200 --stage-seconds 20 --mongo-url mongodb://localhost:27017 --out out/a-mixed
uv run python $D --scenarios signup --stages 5,10,25,50,100 --stage-seconds 20 --out out/b-signup
uv run python $D --scenarios upload --upload-kb 1024 --stages 5,10,25,50,100 --stage-seconds 20 --out out/c-upload1mb
uv run python $D --scenarios upload --upload-kb 256 --stages 5,10,25,50 --stage-seconds 20 --out out/c2-upload256k
uv run python $D --scenarios mixed --single-ip --stages 25 --stage-seconds 30 --out out/d1-singleip
# server restarted with --prod-middleware:
uv run python $D --scenarios mixed --single-ip --stages 25 --stage-seconds 30 --out out/d3-prodmw-singleip
uv run python $D --scenarios mixed --stages 25,50 --stage-seconds 20 --out out/d2-prodmw-perip
# plain server again:
uv run python $D --scenarios realtime --stages 500,1000,2000,4000,6000 --stage-seconds 30 --out out/e-realtime
uv run python $D --scenarios chat --stages 4,8,16,32,64 --stage-seconds 30 --pool-users 2 --out out/f-chat
uv run python $D --scenarios mixed --mode soak --vus 20 --duration 240 --stage-seconds 30 --out out/g-soak
uv run python $D --self-test
```

Several processes (needs Redis). serve_sim seeds once, then serves `--workers` processes on consecutive ports from `--port`; the driver pins VU `v` to port `v % N` and `--procs` splits the VUs over that many driver processes, so the driver stops being the ceiling:

```bash
uv run python scripts/loadtest/serve_sim.py --port 8099 --pockets 64 --workers 6 --seed-out out/loadtest_seed.json
B=$(seq -s, -f "http://127.0.0.1:%g" 8099 8104)
uv run python $D --scenarios mixed --stages 25,50,100,200,400 --stage-seconds 20 --base-url $B --procs 4 --out out/w6-mixed
# stop: POST /__loadtest/shutdown to EVERY port; the parent drops the db once all have exited
```

The reconnect-storm row came from an earlier version of the realtime scenario that opened each stage's sockets all at once (`--stages 250,500,1000,2000,4000 --slo-p95-ms 2000`). The shipped scenario spreads connects over the first half of each stage.

Check for leftovers afterwards: `mongosh --quiet --eval "db.getMongo().getDBNames().filter(n => n.startsWith('loadtest_'))"` should print `[]`.
