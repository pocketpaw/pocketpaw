<!-- docs/internal/2026-06-belt-mandates.md — the MANDATE primitive and the
     craft factory built on it: anatomy, charter (cadence, checks, recipes),
     patrols (incl. upstream), the headless develop station and its security
     posture, landing and re-develop, endpoints (incl. the digest), env vars,
     and the remaining demo-bar concessions. -->

# Belt Mandates — the standing JOB primitive

A **mandate** is a standing job the Belt holds over time — the FDE-retainer
counterpart to the Belt's one-shot develop-station runs. Instead of a human
handing the station a task, the mandate **senses** its surface, **judges** what
(if anything) is worth doing, routes that judgment through a **human gate**,
and only then dispatches work.

The **craft factory** is mandates running unattended: a cadence scheduler fires
shifts, patrols (including `upstream`, which watches pinned GitHub engines)
feed the foreman, and the headless develop station turns each approved task into
a checked, reviewed diff that still waits at the per-diff Instinct gate. A
digest route reports the day. Nothing lands on its own: every plan and every
diff passes a human gate, and nothing merges.

## Anatomy

```
MANDATE  (charter: goal, KPIs, says_no, boundaries, budget, cadence, checks,
          recipes; surface: repo; upstream: pinned GitHub deps)
   │
   ├── PATROLS sense the surface (scoped by the
   │   mandate's `patrols` toggles) ───────────► SIGHTINGS (deduped)
   │     • deps      — manifest scan (pyproject/package.json) vs advisory table
   │     • issues    — open issues on the repo's GitLab project (connector)
   │     • upstream  — commits on each pinned GitHub dep since its pin
   │     • feedback  — human intake (POST .../feedback)
   │
   └── SHIFT (manual trigger, or the cadence scheduler: daily / weekly)
         1. sense   — run patrols, persist new sightings (deduped)
         2. judge   — the FOREMAN makes ONE LLM call over:
                      charter (verbatim, BOUNDARIES first) + sighting digest
                      since last shift + last 3 shifts' outcomes + soul recall
         3. validate — machine checks on ACTION fields ONLY
                      (budget cap, evidence refs, boundary phrases in
                      title/expected_outcome — the `why` narration is NEVER
                      scanned: a good foreman names forbidden things when
                      refusing them)
         4. gate    — PlanProposal lands as an Instinct `belt_plan` Action
                      (the third blob peer of `_pocket_write`/`_code_change`)
            ── or ── stood_down: empty plan, a SUCCESS state; the chain opens
                      and closes in the trigger, no human gate for a no-op
         5. dispatch — on human APPROVE, the plan executor re-validates
                      (mandate active, budget unchanged) and dispatches each
                      task as a Belt run; on REJECT the router closes the
                      chain and the shift records the reason
         6. develop — with the headless dispatcher, the develop station
                      produces each run's diff (see below); the diff waits
                      at the per-diff Instinct gate
```

### The charter: cadence, checks, recipes

- **`cadence`** is `daily`, `weekly` or `manual`. The cadence scheduler
  (`mandates/scheduler.py`, one sweeper loop, every
  `POCKETPAW_MANDATE_SCHEDULER_INTERVAL` seconds, default 3600) fires a shift
  for each ACTIVE mandate whose last shift is older than 1 day (`daily`) or 7
  days (`weekly`); a mandate that never shifted is due at once, and `manual`
  is never fired. It starts under `POCKETPAW_CLOUD_SCHEDULER_ENABLED=true`,
  through the lease, like the other background loops.
- **`checks`** are commands that must pass before a headless diff is attached,
  e.g. `["uv run pytest -q", "uv run ruff check ."]`.
- **`recipes`** map a name to a deterministic command, e.g.
  `{"bump-photo": "node scripts/bump-craft-engine.mjs photo"}`. The foreman may
  name a recipe on a plan task (the validator rejects an unknown name); the
  station then runs that command instead of an LLM develop, and the checks
  still gate it.

Checks and recipes are argv strings split with `shlex` and never run through a
shell. The create DTO rejects (422) one that does not split, or whose program
(argv[0]) is not on `POCKETPAW_FACTORY_ALLOWED_COMMANDS`: by basename for a bare
name or an absolute path, and never a relative path, which would resolve into
the agent's worktree. The default list is `uv, uvx, bun, bunx, node, npm, pnpm,
python, python3, pytest, cargo, make, go`: no shells, `env`, `sudo`, `curl`,
`wget` or `git`. Create also rejects (422) a `surface.repo_id` that does not
resolve inside the workspace's belt allowlist roots. Only `belt.manage`
(admin) can write a charter.

### Decision chains (RFC 09)

One shift = one chain, **exactly one `decision.completed`** per chain:

| Path        | Chain |
|-------------|-------|
| dispatched  | `agent.proposed → human.corrected(accepted\|edited) → decision.completed(passed=True, action_outcome="dispatched", task_count, run_refs)` |
| rejected    | `agent.proposed → human.corrected(rejected) → decision.completed(passed=False, action_outcome="rejected")` (router owns the close) |
| stood_down  | `agent.proposed → decision.completed(passed=True, action_outcome="stood_down")` (trigger owns both; no human event) |
| gate failure| `agent.proposed → human.corrected → decision.completed(passed=False, action_outcome="failed", error_class=…)` (executor `_fail` chokepoint) |

The executor mirrors `ee.cloud.belt.executor` exactly: a single `_fail`
chokepoint per failure path, success emits once at the end, chain emits are
best-effort and never break the approve response.

### The foreman

`ee/pocketpaw_ee/cloud/mandates/foreman.py`. One judgment call per shift
through a pluggable `PlanLlm` protocol, selected by `POCKETPAW_MANDATE_LLM`:

- `claude` (default) — runs the **system** Claude Code CLI
  (`claude -p --tools "" --output-format json`, prompt on stdin) in a fresh
  empty temp dir with the scrubbed env, since the prompt carries third-party
  sighting text (`foreman.run_claude_no_tools`, which the autopilot personas
  share); parses the envelope's `result`, tolerates fenced JSON. The binary is `POCKETPAW_FACTORY_CLAUDE_BIN`,
  else `claude` on PATH (never the SDK's bundled copy, which goes stale); the
  model is `POCKETPAW_FACTORY_CLAUDE_MODEL`, else the CLI's default.
- `mock` — deterministic (one task per sighting, severity-ranked, budget-capped;
  `no_action` on a quiet digest). Tests script it via `foreman.set_mock_plan`.

The prompt encodes every sim-validated rule: charter verbatim with BOUNDARIES
prominent; at most `budget.max_tasks_per_shift` tasks; every task cites
sighting ids and names an expected KPI direction; an empty plan with a reason
is correct and respected; boundaries override KPI opportunities; never repeat
a failed approach without stating what changed; tasks in one shift are
independent of each other (they develop from the same base and land
separately, so dependent follow-up work waits for a later shift; plan
validation cannot detect a dependency, so only the prompt says it); strict
JSON only.

## Endpoints (`/api/v1/belt/mandates`, RBAC mirrors the belt console)

| Method | Path | Gate | What |
|--------|------|------|------|
| POST | `/belt/mandates` | `belt.manage` | Create (charter body + `patrols` senses toggles + optional `upstream` watch list) → `{mandate}` |
| GET | `/belt/mandates` | `belt.read` | `{mandates}` + health (last shift state, open gate count, sighting count) |
| GET | `/belt/mandates/digest?since=<iso>` | `belt.read` | The workspace digest since `since` (default 24 hours ago); see *Digest* below |
| GET | `/belt/mandates/{id}` | `belt.read` | Bare detail: charter, patrols, upstream, recent shifts, sightings-by-patrol |
| POST | `/belt/mandates/{id}/feedback` | `belt.manage` | Intake patrol → Sighting. TWO shapes, discriminated on `kind`: general `{text, severity?, source}` → sighting dict (autopilot keeps using this); teaching `{kind: reject\|edit\|plan, reason, shift_no?, task_title?}` → `{ok: true}` (the gate UI's channel) |
| GET | `/belt/mandates/{id}/sightings` | `belt.read` | `{sightings}`, newest-first |
| POST | `/belt/mandates/{id}/shift` | `belt.manage` | Run a shift now → `{shift: {shift_id, no, state, plan_action_id, task_count, no_action_reason}}` |
| POST | `/belt/mandates/{id}/plan/resolve` | `belt.manage` | The console's gate action: `{shift_no, decisions: [{index (0-based), decision: approve\|reject\|edit, edited_title?, reason?}]}` → `{shift}`. Every task needs exactly one decision. |
| POST | `/belt/mandates/{id}/autopilot` | `belt.manage` | Start/stop Foresight-seeded simulated users feeding the feedback patrol: `{action: start\|stop, users?: int (default 3, max 10)}` → `{mandate}`. START persists `autopilot={on, users}`, runs ONE cycle immediately, spawns the background loop; STOP cancels it. |
| GET | `/belt/mandates/{id}/pawprints` | `belt.read` | `{pawprints}` past-tense feed; item shape `{id, mandate_id, shift_no, kind, summary, evidence_refs, ts}` |

Pawprint `kind`s: the UI consumes `executed` / `rejected` / `edited` /
`stood_down`; the feed also emits `proposed` / `approved` / `failed` /
`planning` (a documented superset, same item shape). `edited` fires when the
approval carried human edits (Corrections exist on the plan Action).

**The Instinct gate stays the single chain authority.** `plan/resolve` maps the
console's per-task verdicts onto the REAL instinct paths: any approved/edited
subset becomes an approve-WITH-EDITS (the blob's task list filtered/retitled —
the standard Corrections machinery; the executor dispatches the kept tasks),
and an all-reject becomes a plain reject (the router closes the chain).
Rejected tasks are recorded as teaching sightings. Direct
`POST /instinct/actions/{id}/approve|reject` (the Tray, MCP, bulk endpoints)
keeps working unchanged — both surfaces hit the same transition, so the chain
still closes exactly once.

**Realtime:** when a plan proposal lands at the gate, the service emits a
`belt_plan` event on the workspace bus (payload `{workspace_id, mandate_id,
proposal}`), mirroring `belt_run_updated`'s audience fan-out; the mandates page
subscribes to that topic.

## The `upstream` patrol

Watches pinned GitHub dependencies, such as the craft engines a Cargo.toml pins
by `rev`. Configure it on create with a top-level `upstream` list and enable it
by including `"upstream"` in `patrols`:

```json
{
  "patrols": ["upstream", "feedback"],
  "upstream": [
    {"repo": "storytold/photocraft", "pin_file": "crates/craft-engines/photo/Cargo.toml"}
  ]
}
```

`repo` must be `owner/name`; `pin_file` is relative to the mandate's bound repo
and may not leave it. Per watch, the patrol:

1. Parses `pin_file` as TOML and takes the `rev` of the first dependency whose
   `git` URL is `https://github.com/<repo>` (several crates from one repo, or a
   `[patch]` block, share one rev). The rev must be a hex sha.
2. Runs `gh api repos/<repo>/compare/<pin>...HEAD` (argv list, 60s timeout) and
   reads `ahead_by` and the first page of commits only.
3. Files one summary sighting, `<repo>: N commits since pin <short>`, with
   severity 2 for 1-20 commits, 3 for 21-100 and 4 above 100 (none when the pin
   is current), plus up to 5 area sightings (severity 2) that group commit
   titles by conventional scope (`fix(render): …` → `render`) or prefix
   (`photocraft-text: …`), each citing up to 8 short shas and titles.

Sightings dedupe on `evidence.dedup_key` = repo + pin + upstream head, so a
quiet day files nothing and a new upstream head files a fresh set. A broken
watch (gh not installed, a 404, an unreadable pin file) files one severity-1
sighting naming the problem instead of raising. The backend's `gh` must be
authenticated (`gh auth status`) for private repos and for rate limits.

## Plan-feature gating posture

The mandates router intentionally matches the belt console's posture: routes
are gated by license + RBAC (`belt.read` / `belt.manage`) but carry **no**
`require_plan_feature` tier gate at demo bar (the belt console router doesn't
either). Tighten both surfaces together before GA.

## Storage

4-file entity at `ee/pocketpaw_ee/cloud/mandates/` (+ `patrols.py`,
`foreman.py`, `executor.py`, `soul_link.py`, `events.py` supporting modules —
the same beyond-four shape the belt console uses). Beanie docs (`MandateDoc`,
`ShiftDoc`, `SightingDoc`; all workspace-keyed) live in `mandates/domain.py`,
imported ONLY by `mandates/service.py`, and register into `init_beanie` via a
lazy import in `cloud/models/__init__.py` (the calendar-doc pattern).

## Soul wiring (demo bar)

When a mandate binds `soul_path`, `soul_link.py` recalls up to 5 memories
before planning and appends an episodic shift summary after every terminal
(dispatched / rejected / stood_down) via the real soul-protocol API
(`Soul.awaken → recall/remember → save_local`). Best-effort throughout — a
soul failure never wedges a shift.

## Autopilot — Foresight-seeded simulated users (feat/belt-autopilot)

`POST /belt/mandates/{id}/autopilot {action, users?}` turns a mandate's feedback
patrol into a self-feeding loop. When ON, a per-mandate background asyncio task
(`autopilot.start_autopilot`, registered in the module's process-local `_TASKS`
registry keyed by mandate id — the same create-task + cancel-and-await shape as
`decisions._action_sweeper`, but per-mandate so STOP cancels exactly one) runs a
cycle every `POCKETPAW_MANDATE_AUTOPILOT_INTERVAL` seconds (default 300); START
also runs ONE cycle immediately (synchronously, inside the request, so START's
response already reflects the first cycle's sightings).

Each cycle:

1. Reads the bound repo's surface — the README's first ~800 chars + up to 10
   recent commit titles (`git log`, argv-only subprocess).
2. Builds N personas (1-10, default 3) from a deterministic palette, each seeded
   with a **Foresight `OceanDrift`** temperament (`ee.foresight.persona.OceanDrift`
   — the genuine bridge to the sim module).
3. Each persona emits 1-3 structured `{text, severity 1-5}` feedback items
   through a pluggable `UserSim` interface, POSTed through the EXISTING
   `service.file_feedback` path (NOT raw HTTP) with `source="autopilot:<persona>"`
   — so they become feedback Sightings the next shift's foreman cites.

**Which Foresight path + why.** The brief allows a lighter persona LLM call when
foresight's scenario runner is too heavyweight per-cycle. We took the **lighter
path**: foresight's `run_scenario` / OASIS substrate is a tick-based *world*
simulation (CAMEL + OASIS + a YAML scenario config, anchors, prediction records)
built to rehearse a *decision across a population*, not "use a product and emit
free-text feedback" — spinning it up per cycle would pull in torch/igraph/pandas
and a multi-tick loop, and its action vocabulary (`action/rationale/put`) is the
wrong shape. Instead the persona transport reuses the **foreman's proven
pluggable pattern** (`POCKETPAW_MANDATE_LLM=claude|mock` — the SAME env) behind
the `UserSim` interface, and bridges foresight's `OceanDrift` value object for
the persona seed. The `claude` persona call goes through the foreman's
sandboxed `run_claude_no_tools` (no tools, empty temp cwd, scrubbed env, prompt
on stdin), since its prompt carries the repo's README and commit titles. Mock mode is deterministic + seeded (a per-persona RNG seeded
on the persona name) so tests get stable sightings. A later PR can swap the full
scenario runner in behind `UserSim` with no caller change.

**Resilience.** Autopilot never crashes a shift or the app: every persona call,
every feedback POST, and every cycle is wrapped — a failure is logged and
swallowed per-cycle; the loop sleeps and retries next interval. The persisted
`MandateDoc.autopilot = {on, users}` is the source of truth for whether
autopilot *should* run; the live task is process-local. State rides the detail +
list wire + the `MandateAutopilotChanged` event
(`{workspace_id, mandate_id, on, users}`).

**Lifespan wiring.** A restart re-derives the loops from the persisted flags:
`reconcile_autopilot_tasks` (lifespan startup) queries every ACTIVE mandate with
`autopilot.on=True` (via `service.list_autopilot_enabled` — a deliberate
cross-workspace system read, the stale-run-sweeper posture; paused mandates are
skipped) and restarts each loop with `run_immediate=False`, so a boot never
storms a cycle per mandate — the first cycle lands after the normal interval.
`shutdown_all_autopilot_tasks` (lifespan shutdown) cancels + awaits every
registered loop so the process exits without orphaned tasks. Both hooks are
registered in `cloud/__init__.mount_cloud` under the same
`POCKETPAW_CLOUD_SCHEDULER_ENABLED=true` gate as the decisions reconciler / run
sweeper, so pytest runs never spawn background loops that outlive the test.

## Dispatchers and the headless develop station

`POCKETPAW_MANDATE_DISPATCHER` selects what an approved plan task becomes:

- **`station`** (default) — `StationTaskDispatcher` files a real queued
  `code_change` run (`station_pending=True`, no diff, repo pre-bound; the blob
  carries the task's `title` and `expected_outcome`). The
  console shows it as `queued / station`; a human drives the interactive
  `/belt` station to a diff. The belt executor refuses a `station_pending` blob
  if it is ever approved (`error_class="StationPending"`).
- **`headless`** — `HeadlessTaskDispatcher` files the same queued run, then
  produces the diff with no human in the loop and attaches it; the run becomes
  a pending `code_change` at the per-diff gate. In production the develop runs
  in a background task, one at a time, so plan approval returns at once. With
  no develop loop wired it degrades to `station`.
- **`bus`** — announce-only (`belt_run_updated(status="dispatched")` under a
  synthetic run id); no run record.

The develop loop for `headless` is `belt/develop_station.ClaudeCodeDevelop`,
wired by a cloud startup hook when `POCKETPAW_MANDATE_DISPATCHER=headless`
**and** `POCKETPAW_FACTORY_DEVELOP=claude` (and, in a process serving cloud
tenants, `POCKETPAW_FACTORY_DEDICATED_HOST=1`; see Security posture). One run
walks a fixed sequence:

```
PREPARE  screen the task text (InjectionScanner, HIGH refuses); refuse any
         charter check whose program is not allowed; resolve the bound repo
         inside POCKETPAW_BELT_REPO_ALLOWLIST (empty = refuse); git worktree
         add --detach at origin/<base> (after a fetch) when an origin exists,
         else the local <base>; snapshot the worktree's .git file
WORK     a recipe task runs the charter's recipe command; otherwise
         `claude -p` develops (--permission-mode acceptEdits)
CHECK    run every charter check
FIX ≤2   a red check, or a failed review, sends the failure back to
         `claude -p`, then CHECK again; at most 2 attempts (recipes get none)
REVIEW   an independent read-only `claude -p` judges the diff against the task:
         strict {"verdict": "pass"|"fail", "notes": [...]}
DONE     git add -A; git diff --cached --binary against the base sha; refused
         if it touches .claude/, .mcp.json, .git or .gitmodules, or adds a
         line matching a credential pattern
CLEANUP  remove the temp dir, then git worktree prune, always
```

After every agent step the worktree's `.git` file must match its snapshot, or
the run fails with `INTEGRITY: worktree .git changed`. A dead end raises with
the failing step's name; the runner leaves the run queued and records the
reason (secrets redacted) as `headless_error` on the blob, where the console
and the digest show it. The station never commits to a branch, pushes or
merges. Background develops are process-local: the dispatcher marks each run
`headless_state: "queued"` until it attaches or fails, so a run a restart
dropped stays visible as stuck in the digest (nothing re-drives it yet), and a
background task that crashes is logged at ERROR. The develop request aims at
the blob's `expected_outcome`; the attached run's `summary` becomes the
station's report (checks, review verdict and notes, fix attempts), and its
`files_changed` is the station's count (else the diff's `+++` headers).

### Landing and re-develop

When a human approves a run at the per-diff gate, `belt/executor.py` applies it
in a throwaway worktree and commits it on `feat/belt-<id>`. A run that carries
a `title` (every mandate task) commits as `feat: <title>` (kept as written when
the title already has a Conventional-Commits type), trimmed to 72 chars; the
body is the task's why followed by the station report. The PR title and body on
the remote path are the same. A hand-proposed change (no title) keeps
`feat(belt): <summary>`. A local-only landing keeps the branch the worktree
created (linked worktrees share `refs/heads`); a run that does not land deletes
its branch, so a retry of the same action can branch again.

Two runs of one shift develop from the same base. Once the first lands and is
merged, the second's patch may no longer apply. For a headless run (blob
`headless`) the executor checks the patch with `git apply --check` before the
`--3way` apply; when both fail it does not fail the run. It **re-develops** it:

```
approved ──apply conflict──▶ blob: diff cleared, station_pending, redevelop=1,
                                   summary back to the expected outcome
                             status: approved → pending  (event action_redevelop)
                             belt_run_updated(queued, station)
         ──after cleanup──▶  headless dispatcher develop(run_ref): the station
                             regenerates the diff against the current base
                             ──▶ pending at the per-diff gate (fresh approval)
second apply conflict on the same run ──▶ failed: "base moved twice: …"
no develop loop wired              ──▶ failed: "… not wired to re-develop it"
```

The re-develop is not a terminal, so the Decision-Graph chain stays open and
the run keeps its `correlation_id`; it closes once, when the run lands or
fails. The status flip uses the store's `_update_status` with
`require_status=approved`, so a concurrent decision makes the flip a no-op
(the run then fails with that reason). The reopened row keeps its first
`approved_by` / `approved_at` until the next approval overwrites them.

### Runs read model

`GET /api/v1/belt/runs` and `GET /api/v1/belt/runs/{id}` rows carry, besides
status, stage and the landing fields: `title` (the task title, `null` on a
hand-driven run), `files_changed` (from the attached diff, replaced by the
staged count on landing), `redevelop` (how many times the run went back to the
develop station on a moved base: 0 or 1), and `error`: why the run failed, which is the
executor's reason (`Action.error`) or, for a develop that failed, the
`headless_error`. `null` when nothing failed.

## Security posture

The develop station runs code the agent wrote, on the host, **before** a human
sees the diff: the charter checks execute the worktree's own test files,
Makefile and package scripts. The per-diff Instinct gate decides what lands; it
does not contain what runs. So the station is for **a dedicated single-tenant
host running trusted repos**, and it refuses to wire in a process serving cloud
tenants unless the operator sets `POCKETPAW_FACTORY_DEDICATED_HOST=1`.

What the station scrubs or blocks:

- **Env.** Every subprocess (claude, checks, recipes, git) sees only `PATH`,
  `HOME`, `USER`, `LOGNAME`, `LANG`, `LC_ALL`, `TERM`, `TMPDIR`, `SHELL`. The
  Mongo URI, tokens, API keys and `POCKETPAW_*` secrets never reach it.
- **Programs.** Check and recipe argv[0] must be on
  `POCKETPAW_FACTORY_ALLOWED_COMMANDS`, enforced at create (422) and again
  before exec. No shells, `env`, `sudo`, downloaders, `git`, or relative paths.
- **Claude seats.** Every call (foreman, develop, fix, review) loads no
  settings files (`--setting-sources ""`), no MCP servers
  (`--strict-mcp-config`) and no hooks (`disableAllHooks`), so a `.claude/` or
  `.mcp.json` planted in the worktree never loads. Auth is the CLI's
  keychain/OAuth login; never `--bare`, which forces API-key auth. Tools are
  limited with `--tools` and the allow rules are scoped to the worktree
  (`Read(./**)`, `Edit(./**)`, `Write(./**)`, plus `Bash(<check>:*)` on the
  edit seats); WebFetch, WebSearch and Task are denied. The foreman and the
  autopilot personas get no tools at all and an empty temp dir as their cwd.
- **Git.** Station git calls run with `core.fsmonitor=false` and
  `core.hooksPath=/dev/null`; the worktree `.git` file is snapshotted and
  re-checked after every agent step.
- **The diff.** Refused if it touches `.claude/`, `.mcp.json`, `.git` or
  `.gitmodules`, or if an added line matches a `security.redact` credential
  pattern (the error never echoes the value).
- **Text.** Output tails, prompts' failure text and `headless_error` go through
  `security.redact`. Task text, check output and the diff sit in `<untrusted>`
  blocks the prompts tell the model to treat as data, and task text the
  heuristic InjectionScanner rates HIGH refuses the run.
- **Processes.** Each subprocess gets its own session; a timeout or a cancelled
  run kills the whole process group.
- **Repos.** Mandates bind only repos inside the workspace's allowlist roots;
  the station also requires an explicit `POCKETPAW_BELT_REPO_ALLOWLIST`.

Residual risks:

- Checks and recipes still execute agent-editable repo code with the host
  user's privileges: `HOME` (and with it `~/.ssh`, caches under `~/.cargo`,
  `~/.cache/uv`, `~/.bun`), the network, and anything else that user can reach.
  An allowed program like `python`, `npm` or `make` runs whatever the worktree
  tells it to. There is no OS sandbox yet; the next step is a container or
  `sandbox-exec` runner for checks and recipes.
- The worktree's `CLAUDE.md` files still load (memory files are not a setting
  source), so text written in DEVELOP can steer FIX and REVIEW. That is not
  host exec, and the human gate sees the hunk.
- The secret scan is pattern-based: it misses unknown formats and can refuse a
  diff with fixture values such as `password="..."` or a URL with basic auth.
- The allowlist roots are global settings plus per-workspace console roots; a
  workspace can bind any repo under a shared root.
- A process that daemonizes out of its session survives the group kill.

## Digest — the morning report

`GET /belt/mandates/digest?since=<iso>` (default: 24 hours ago; a naive value
reads as UTC) returns:

```
{since, generated_at,
 mandates: [{id, name, status, cadence,
             sightings: {count, top: [{title, severity, patrol}]},   # top 5
             shifts: [{no, state, outcome, task_count}],             # since
             runs:   [{action_id, status, title, pr_url, branch,
                       commit_sha, headless_error, headless_state,
                       error}],                                      # since
             gates:  {plans: [{shift_no, plan_action_id, task_count}],
                      diffs: [<run row>]},                           # any age
             stuck:  [<run row>]}],      # queued with headless_error or a
                                         # leftover headless_state, any age
 totals: {mandates, new_sightings, shifts, runs, landed, failed, gates_waiting}}
```

Gates and stuck runs are listed whatever their age: an in-gate plan, a diff at
`proposed`, or a headless develop from last week that failed or never finished
still needs a human. The digest is composed from the existing
reads (`list_mandates`, `get_mandate`, `shift_wire`, `list_sightings`, the belt
runs list), so it cannot disagree with the console; shifts come from the
detail's 10 most recent.

`scripts/factory_digest.py` prints it as markdown (a mandates table, then
*Needs you*, *Failures* with each run's `error` or `headless_error`, *Landed*). Stdlib only; the token comes from
`--token-file` or `PAW_TOKEN` and is never printed:

```bash
uv run python scripts/factory_digest.py --base http://localhost:8893 \
    --token-file ~/.paw/token [--since 2026-10-05T06:00:00+00:00]
```

## Environment

| Variable | Default | What |
|---|---|---|
| `POCKETPAW_MANDATE_DISPATCHER` | `station` | `station` / `headless` / `bus` (above) |
| `POCKETPAW_FACTORY_DEVELOP` | unset | `claude` wires the develop station (needs `POCKETPAW_MANDATE_DISPATCHER=headless`) |
| `POCKETPAW_FACTORY_DEDICATED_HOST` | unset | `1` lets the station wire in a process serving cloud tenants; set it only on a dedicated single-tenant host |
| `POCKETPAW_FACTORY_ALLOWED_COMMANDS` | `uv,uvx,bun,bunx,node,npm,pnpm,python,python3,pytest,cargo,make,go` | Comma-separated program basenames a charter check or recipe may start |
| `POCKETPAW_BELT_REPO_ALLOWLIST` | empty | JSON list of repo roots; the develop station refuses to run while it is empty |
| `POCKETPAW_FACTORY_CLAUDE_BIN` | `claude` on PATH | The Claude Code CLI every factory LLM seat shells (foreman, develop, fix, review) |
| `POCKETPAW_FACTORY_CLAUDE_MODEL` | the CLI's built-in default | Passed as `--model` when set (user settings don't load, so a model set there is ignored) |
| `POCKETPAW_FACTORY_DEVELOP_TIMEOUT` | `900` | Seconds per `claude -p` call (develop, fix, review) |
| `POCKETPAW_FACTORY_CHECK_TIMEOUT` | `600` | Seconds per check or recipe command |
| `POCKETPAW_MANDATE_LLM` | `claude` | Foreman / autopilot transport: `claude` or `mock` |
| `POCKETPAW_MANDATE_SCHEDULER_INTERVAL` | `3600` | Cadence sweep interval, seconds |
| `POCKETPAW_MANDATE_AUTOPILOT_INTERVAL` | `300` | Autopilot cycle interval, seconds |
| `POCKETPAW_CLOUD_SCHEDULER_ENABLED` | off | Starts the cadence scheduler and autopilot loops |

A local unattended factory runs with `POCKETPAW_CLOUD_SCHEDULER_ENABLED=true`,
`POCKETPAW_MANDATE_DISPATCHER=headless`, `POCKETPAW_FACTORY_DEVELOP=claude`,
`POCKETPAW_FACTORY_DEDICATED_HOST=1` and a `POCKETPAW_BELT_REPO_ALLOWLIST`
covering the bound repos, on a dedicated box where those repos are checked out
and `claude` and `gh` are authenticated. Station subprocesses get the scrubbed
env, so the station's own `git fetch` has no `SSH_AUTH_SOCK`, `GH_TOKEN`,
`GIT_SSH_COMMAND` or `GIT_ASKPASS`: SSH keys must work without an agent (key
files under `~/.ssh`, or the macOS keychain via `UseKeychain`), and `gh`/HTTPS
auth must live in its config files, not in env vars.

## Demo-bar concessions (each marked in code)

1. **Deps patrol advisory data is a hardcoded table** (`patrols.KNOWN_STALE`).
   The manifest parsing and sighting plumbing are production-shaped; only the
   data source is stubbed.
2. **LLM transport is the `claude` CLI shell-out** behind the `PlanLlm`
   (foreman) and `UserSim` (autopilot) protocols, and the develop station shells
   the same CLI; another transport can replace either without touching callers.
3. **Pawprints read the store, not the journal.** The feed derives from
   ShiftDoc states and the plan Action's status/blob, the same facts the chain
   folded from; a journal-walking narrator can replace it later.
4. **Headless develops are process-local** (see above): a restart mid-develop
   leaves a queued run nothing re-drives; the digest lists it as stuck.

## Tests

`tests/cloud/test_belt_mandates.py`. The hard gate
(`test_full_shift_gate_one_clean_chain`) drives create → feedback → shift →
real-instinct-router approve → dispatch and asserts EXACTLY ONE
`decision.completed` (this repo's documented chain-doubling seam). Also pinned:
stood_down, budget cap, boundary-check-ignores-`why`, patrol intake, deps
patrol + dedup, tenant isolation, reject-closes-once, and the digest route
(sightings, shifts, runs, waiting gates, totals, the `since` window, tenant
scope; it also renders `scripts/factory_digest.py` against the real wire shape).

`tests/cloud/test_belt_upstream_patrol.py` runs the upstream patrol against a
tmp repo's Cargo.toml with a fake `gh`: summary and area sightings, the
severity scale, every failure path as one severity-1 sighting, DTO validation,
and dedup on the upstream head through `run_patrols`.
`tests/cloud/test_belt_develop_station.py` drives the develop station against a
real tmp git repo with real check commands and a faked `claude`, including the
hardening: claude argv flags, the scrubbed env, refused programs, the
multi-tenant wiring refusal, `.git` tampering, protected paths, secret diffs,
redaction, untrusted fencing, the injection screen, process-group kills and
logged background crashes;
`test_belt_headless.py` and `test_belt_scheduler.py` cover the runner and the
cadence scheduler; the headless file also lands a run (commit subject from the
title) and drives the re-develop against a real tmp repo: two diffs from one
base, the first landed and merged, the second re-developed once and back at the
gate, a second conflict failing with "base moved twice", and no develop loop
failing with its reason. `tests/mutations/belt_factory_runs.json` breaks each
of these on purpose. CI runs these in the "Belt mandates and the craft factory
develop station" step (`tests/cloud` is outside the default addopts).

`tests/cloud/test_belt_autopilot.py` (feat/belt-autopilot) pins both new pieces:
autopilot start persists state + runs an immediate cycle whose sightings carry
`source="autopilot:*"`; the background task start/stop lifecycle (asserted
directly against the module — a TestClient request runs on its own short-lived
loop, so it can't observe the task); a full loop autopilot → shift (the mock
foreman cites the autopilot sightings) → resolve-approve → the **real**
`StationTaskDispatcher` files one queued `code_change` station run per task
(`status=queued`, `station_pending=True`, repo pre-bound); the dispatcher env
selection (`station`/`bus`); the `bus` path's announce-only behaviour; the
startup reconciler (restarts exactly the ACTIVE autopilot-on mandates after a
simulated restart, skips off/paused, and the shutdown drain cancels every loop);
and tenant isolation on the autopilot endpoint. All run the deterministic mock
LLM/UserSim.
