<!-- docs/internal/2026-06-belt-mandates.md — the MANDATE primitive and the
     craft factory built on it: anatomy, charter (cadence, checks, recipes,
     issue labels), Pulley blocks installed through recipes, patrols (incl.
     upstream and GitHub issues), the crew (cloud Agents as workers: roster,
     seat rule, per-worker model, instructions and setup; the tester), the
     headless develop station (strict and owner Claude setups, the trust
     restore, ORIENT) and its security posture, the cloud develop station
     (claude --cloud, PR tracking through gh, fixers, silent workers, the
     tester and its PR, snapshot screenshots), the realtime events, the
     architecture context the foreman and review get, the mandate's line
     branch (base sync, landing, re-develop, one PR, its state on the wire and
     the UI surfaces), the run feed (live over SSE and stored per stage, with
     its wire contract), the run on its blueprint (C4 `paths`, the file join,
     `file_touched`), the auto plan gate and the continuous cadence, endpoints
     (incl. the digest), env vars, and the remaining demo-bar concessions. -->

# Belt Mandates — the standing JOB primitive

A **mandate** is a standing job the Belt holds over time — the FDE-retainer
counterpart to the Belt's one-shot develop-station runs. Instead of a human
handing the station a task, the mandate **senses** its surface, **judges** what
(if anything) is worth doing, routes that judgment through a **human gate**,
and only then dispatches work.

The **craft factory** is mandates running unattended: a cadence scheduler fires
shifts, patrols (including `upstream`, which watches pinned GitHub engines)
feed the foreman, and the headless develop station turns each approved task into
a checked diff (reviewed by an LLM seat too, unless it came from a recipe) that
still waits at the per-diff Instinct gate. A digest route reports the day. Every
diff passes a gate and nothing merges on its own. Every plan passes a human
gate too, unless the auto plan gate is on (`POCKETPAW_BELT_PLAN_GATE=auto`, see
*The auto plan gate*), which lets a plan Clef scores as safe through and leaves
the rest for a human.

## Anatomy

```
MANDATE  (charter: goal, KPIs, says_no, boundaries, budget, cadence, checks,
          recipes; surface: repo; upstream: pinned GitHub deps;
          crew: cloud Agents seated as dev / reviewer / foreman / tester)
   │
   ├── PATROLS sense the surface (scoped by the
   │   mandate's `patrols` toggles) ───────────► SIGHTINGS (deduped)
   │     • deps      — manifest scan (pyproject/package.json) vs advisory table
   │     • issues    — open issues on the repo's tracker: GitHub through gh,
   │                   taken by the charter's issue_labels; else GitLab
   │                   (connector)
   │     • upstream  — commits on each pinned GitHub dep since its pin
   │     • feedback  — human intake (POST .../feedback)
   │
   └── SHIFT (manual trigger, or the cadence scheduler: daily / weekly /
             continuous)
         1. sense   — run patrols, persist new sightings (deduped)
         2. judge   — the FOREMAN makes ONE LLM call over:
                      charter (verbatim, BOUNDARIES first) + the OPEN
                      sightings (the backlog: any sighting no landed task has
                      resolved; capped at 30; new vs carried over; the tasks
                      citing each, in-flight ones flagged) + last 3 shifts'
                      outcomes with each task's run result and failure
                      reason + soul recall
         3. validate — machine checks on ACTION fields ONLY
                      (budget cap, evidence refs, boundary phrases in
                      title/expected_outcome — the `why` narration is NEVER
                      scanned: a good foreman names forbidden things when
                      refusing them)
         4. gate    — PlanProposal lands as an Instinct `belt_plan` Action
                      (the third blob peer of `_pocket_write`/`_code_change`)
            ── or ── stood_down: empty plan, a SUCCESS state; the chain opens
                      and closes in the trigger, no human gate for a no-op
         5. dispatch — on APPROVE (a human, or the auto plan gate as
                      `system:belt-policy`), the plan executor re-validates
                      (mandate active, budget unchanged) and dispatches each
                      task as a Belt run; on REJECT the router closes the
                      chain and the shift records the reason
         6. develop — with the headless dispatcher, the develop station
                      produces each run's diff (see below), with the model and
                      instructions of the crew dev seated on the task; the
                      diff waits at the per-diff Instinct gate
```

### The charter: cadence, checks, recipes

- **`cadence`** is `daily`, `weekly`, `continuous` or `manual`. The cadence
  scheduler (`mandates/scheduler.py`, one sweeper loop, every
  `POCKETPAW_MANDATE_SCHEDULER_INTERVAL` seconds, default 3600) fires a shift
  for each ACTIVE mandate whose last shift is older than 1 day (`daily`) or 7
  days (`weekly`); a mandate that never shifted is due at once, and `manual`
  is never fired. A **`continuous`** mandate is due when all of these hold
  (`service._continuous_due`):
  - at least `POCKETPAW_MANDATE_CONTINUOUS_MIN_GAP_MINUTES` (default 5) have
    passed since its last shift;
  - that shift is finished: `done`, or `stood_down` with a sighting filed since
    (a foreman that planned nothing is not asked again about the same backlog).
    A plan waiting `in_gate` for a human always holds the next shift;
  - none of that shift's runs is still in flight (queued, developing, pending at
    the diff gate). A shift older than `POCKETPAW_MANDATE_CONTINUOUS_STALL_HOURS`
    (default 3) stops holding the next one this way, and also releases a shift
    stuck in `planning` (a plan machine validation refused) or `executing`;
  - the foreman did not fail 3 shifts in a row. A failed foreman call
    (timeout, CLI error, unparseable plan) finishes its shift as `done` with
    no plan and the outcome `foreman call failed: <reason>`, so the mandate is
    due again after the minimum gap; after 3 such shifts in a row it waits the
    stall timeout;
  - it has an open sighting that no landed or in-flight task covers.

  While any ACTIVE continuous mandate exists, the loop ticks every
  `POCKETPAW_MANDATE_SCHEDULER_CONTINUOUS_INTERVAL` seconds (default 60)
  instead. A due mandate fires through the same `trigger_shift` as the shift
  route, as `system:scheduler`. The scheduler starts under
  `POCKETPAW_CLOUD_SCHEDULER_ENABLED=true` with every other background loop, or
  alone under `POCKETPAW_MANDATE_SCHEDULER_ENABLED=true`, through the lease
  either way. Pair `continuous` with the auto plan gate, or every shift waits
  for a human at the plan gate.
- **`issue_labels`** (optional, at most 20) scopes the `issues` patrol on a
  GitHub-bound repo: an open issue is sighted when it carries one of these
  labels, compared case-insensitively (see *The `issues` patrol on GitHub*).
  Empty means the GitHub patrol takes nothing.
- **`checks`** are commands that must pass before a headless diff is attached,
  e.g. `["uv run pytest -q", "uv run ruff check ."]`.
- **`recipes`** map a name to a deterministic command, e.g.
  `{"bump-photo": "node scripts/bump-craft-engine.mjs photo"}`. The foreman may
  name a recipe on a plan task (the validator rejects an unknown name); the
  station then runs that command instead of an LLM develop, and the checks
  still gate it. A recipe run skips ORIENT, FIX and REVIEW: the command lands
  certified block code, so no LLM seat reads or edits it, and a red check fails
  the run. The human diff gate still reviews every recipe diff.

Checks and recipes are argv strings split with `shlex` and never run through a
shell. The create DTO rejects (422) one that does not split, or whose program
(argv[0]) is not on `POCKETPAW_FACTORY_ALLOWED_COMMANDS`: by basename for a bare
name or an absolute path, and never a relative path, which would resolve into
the agent's worktree. The default list is `uv, uvx, bun, bunx, belt, node, npm,
pnpm, python, python3, pytest, cargo, make, go`: no shells, `env`, `sudo`,
`curl`, `wget` or `git`. Create also rejects (422) a `surface.repo_id` that does
not resolve inside the workspace's belt allowlist roots. Only `belt.manage`
(admin) can write a charter.

### The auto plan gate

With `POCKETPAW_BELT_PLAN_GATE=auto` (default `manual`: unchanged), the shift
trigger awaits Clef's plan score inline instead of leaving it in the background
(the `belt/clef.shadow_plan` task, shielded, up to
`POCKETPAW_BELT_PLAN_GATE_CLEF_TIMEOUT` seconds, default 45; the verdict is
still recorded on the blob as `clef`, even when the wait times out), then asks the pure
`mandates/plan_policy.decide`. The plan is approved only when all of these hold:

- every task cites at least one evidence ref that is an open sighting of this
  mandate;
- the plan fits the charter budget (`max_tasks_per_shift`);
- Clef scores `in_scope` >= 0.80 and `low_risk` >= 0.75.

It escalates the whole plan, leaving it pending for a human exactly as with the
gate off, when Clef is off, fails, times out or truncated the plan,
`tasks_independent` < 0.40, `too_big` > 0.65, or any task fails a rule above.
The policy never rejects: a rejection would file a teaching sighting no human
wrote. Auto mode needs Clef on (`POCKETPAW_BELT_CLEF=shadow` plus its token and
account id); without it every plan escalates.

An approval runs `service.resolve_plan`, the function the `plan/resolve` route
calls, with `system:belt-policy` as the actor, so the chain reads
`agent.proposed → human.corrected → decision.completed` like a console approval.
Either way the decision goes on the `_belt_plan` blob as `policy`
(`decision`, `rule`, `reason` with Clef's four numbers, `approver`, the
thresholds, `at`), and an escalation is logged with its reason.

The thresholds come from the supervisor's gate log of the first unattended
pilot (seven plan decisions, pinned in `tests/cloud/test_belt_plan_policy.py`).
Six match. The miss is a plan the supervisor trimmed for a file overlap Clef
does not score (independence .52, above a plan approved in full at .50), which
the policy approves as proposed. Each threshold has an env override:
`POCKETPAW_BELT_POLICY_MIN_IN_SCOPE`, `..._MIN_LOW_RISK`,
`..._MIN_INDEPENDENT`, `..._MAX_TOO_BIG`.

### Pulley blocks through recipes

Pulley (`qbtrix/pulley`) supplies certified copy-in building blocks (auth, org,
roles, notify, files, audit, hello) and the `belt` CLI that installs them into
a SvelteKit app made from its `template/`. A mandate assembling such an app (the
"Pulley app line" template in paw-enterprise) drives that CLI as charter
commands, nothing else:

- **recipes** `add-<block>` → `belt add <block> --app . --json` for the six
  universal blocks. `add` resolves the dependency closure and checks every
  block's bytes against the integrity pinned in `registry/index.json` of the
  same pulley checkout. That proves the bytes match that checkout's index, not
  that they were certified: what lands is the certified set only while the
  host's checkout sits at a certified commit, and nothing pins it there.
  `add` on a block the line already has changes nothing, so the station
  refuses the run with `DONE: the change produced an empty diff`. One recipe
  can land more than one block: `add-roles` on a bare app brings auth and org
  with it.
- **checks**, in this order: `bun install --frozen-lockfile`, then
  `belt doctor --app . --json --env-advisory`. A recipe's lockfile changes
  come from `belt add` itself: pulley installs the npm deps the blocks it adds
  declare, so `package.json` and `bun.lock` move together in the same diff. The
  frozen install fails a run whose lockfile drifted from `package.json`. Doctor
  then checks that lock and disk agree, no drift, the generated shell, routes,
  barrels, hooks and adapters regions match the installed blocks, npm
  requirements are recorded, framework floors hold, and every required port has
  at least one adapter to select.

Why the CLI and not Pulley's MCP server: the strict station runs every claude
seat with no MCP servers, and DONE refuses any diff touching `.mcp.json` (owner
setup restores it to base before every claude call). A recipe needs no LLM at
all: the station runs the command, skips ORIENT, FIX and REVIEW, and the checks
gate it, so an MCP server would add a process and a trust surface for nothing.

What the station needs from the factory host: `bun link` in a pulley checkout
puts `belt` on PATH (`belt` and `bun` are on the default allowlist, and the
station env keeps PATH), and `belt add` with no `--registry` reads that checkout's own
`registry/`, so a charter string carries no machine path. `--env-advisory`
exists because the station judges install state in a throwaway worktree with a
scrubbed env and no `.env`: without it doctor fails every install on the
deployment config a block declares (`DATABASE_URL`, `BETTER_AUTH_SECRET`,
`PULLEY_PORT_MAIL` for auth). With it, exactly two kinds become info: a
required env var that is unset (`env-required-unset`) and a port whose adapter
is not selected while one exists to select (`port-unconfigured`). A port that
no adapter implements is `port-no-adapter`, an error the flag never softens: no
env value fixes it, and the app cannot start. (`bin`, the registry default,
`--env-advisory` and `port-no-adapter` are pulley's contract 1.1.0, branch
`feat/belt-recipes`, not yet on GitHub.)

Pulley's certifier (`bun scripts/certify.ts --all`) is not a charter check: it
certifies the registry's blocks, not the line app, and needs `bun install` and
Postgres. It is pulley's promotion gate, upstream of any shift. Last run
2026-10-06: at pulley 99e5186 (dev), then again at `feat/belt-recipes` 4c2222f
(99e5186 + d897db2 + the `port-no-adapter` fix), both with a local Postgres 14
as `PULLEY_TEST_DB`. Both runs: 7 of 7 blocks certifiable, 49 of 49 checks
passed (spec, contract tests, solo install, pairwise combos, licence audit,
standalone build, removal), none failed or unverified. The second run's verdicts
are in `docs/internal/2026-10-06-pulley-certify.json` (trimmed from the
`--json-out` report: per-check status only).

`orient.block_component` maps a block manifest onto a C4 component (id, name,
description, technology) and its `deps` onto sync relationships; routes,
endpoints, the table prefix and events ride the description after the block's
own first sentence. It is a pure mapper and has no caller yet. It is meant for
blueprint drafting (BF-14) and the writer that puts installed blocks into a line
app's `docs/c4/model.json` (BF-15). Until that writer exists, ORIENT and the
foreman see a line's blocks only if someone writes the model by hand.

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
  model is `POCKETPAW_FACTORY_CLAUDE_MODEL`, else the CLI's default. The call
  times out after `POCKETPAW_MANDATE_FOREMAN_TIMEOUT` seconds (default 300). A
  failed call logs one warning naming the mandate, the shift and the prompt
  size in characters, finishes the shift (see the continuous cadence above),
  shows in pawprints as `failed`, and the shift route answers 502
  `mandate.foreman_failed`.
- `mock` — deterministic (one task per open sighting that is not in flight,
  severity-ranked, budget-capped; `no_action` when nothing is left to plan).
  Tests script it via `foreman.set_mock_plan`.

The prompt encodes every sim-validated rule: charter verbatim with BOUNDARIES
prominent; at most `budget.max_tasks_per_shift` tasks; every task cites
sighting ids and names an expected KPI direction; an empty plan with a reason
is correct and respected; boundaries override KPI opportunities; never repeat
a failed approach without stating what changed; tasks in one shift are
independent of each other (they develop from the same line tip and land
separately, so dependent follow-up work waits for a later shift; plan
validation cannot detect a dependency, so only the prompt says it); work on
the mandate's line is built and never planned again; a task in
flight is never planned again; a task extends an existing component and never
plans a duplicate of one (rule 8, below); strict JSON only.

The foreman also gets the bound repo's architecture: the shift trigger reads
`<repo>/docs/c4/model.json` (`belt/orient.c4_lines`) and the prompt lists the
repo's own containers and components, one line each with the first sentence of
its description, capped at about 3k characters (external systems the model
also names are left out). Rule 8 says a task extends a listed component
wherever one covers the work, names it in the task's `why`, and never plans a
new component, module or service that duplicates one listed. With no C4 model
the block says so. The foreman stays in the strict setup in both modes (it
reads third-party sighting text), so this injection is how it learns the
architecture.

#### What the foreman reads: the backlog and the run outcomes

A sighting is **open** until a task that cites it (its `evidence_refs`)
**lands**. A failed, rejected or still-running task leaves it open. The
foreman gets every open sighting, not just the ones filed since the last
shift, so work it skipped or that failed comes back on the next shift:

- **Order and cap.** Highest severity first, then oldest; at most 30
  (`_BACKLOG_CAP` in `mandates/service.py`). Past the cap the prompt says
  `(showing 30 of N open sightings: ...)`.
- **Per sighting.** `new` (filed since the last shift) or `carried over`, the
  tasks that cited it (`shift N "<title>" <status>`), and `IN FLIGHT` when one
  of them is still being worked.
- **Task status.** From the task's run row: `landed`, `failed`, `rejected`,
  `pending at gate` (diff waiting on a human), `approved` (landing),
  `developing` (headless develop running), `queued` (waiting for the develop
  station), or `develop failed` (headless develop failed; waits for a human
  and is not in flight). A task with no run takes the plan Action's status:
  `pending at plan gate` (in flight), `plan rejected`, `plan failed`,
  `dispatched` (announce-only dispatcher). In flight = queued, developing,
  approved, pending at gate, pending at plan gate.
- **History.** Each of the last 3 shifts lists its planned tasks with that
  status, the cited sighting ids and, on failure, the run's `error`. Prompt
  rule 7 says an in-flight task is never planned again, and the existing rule
  says a failed approach is not repeated without stating what changed.
- **The line.** `THE LINE` is read from git (`belt.executor.line_status`, the
  same view as the mandate detail's `line` block): `<line>: N commit(s) not in
  <base> yet, awaiting the captain's merge:` followed by those commits'
  subjects (merges left out, newest first, capped at 30), or `everything on
  <line> is merged into <base>`, or nothing landed yet. A commit no run
  recorded (a captain's fixup) counts too, and old landings never drop out of
  a runs window. Each landed task's history row is suffixed `on the line
  belt/line/<id>, awaiting merge into <base>` or `merged into <base>` once the
  base holds that run's commit (`belt.executor.line_merged`: an ancestor of
  `origin/<base>` as last fetched, or of the local base). Either way it is
  built: its sightings are resolved like any landed task's, and the prompt says
  never to plan that work again. A squash or rebase merge leaves the commits
  outside the base, so such a line keeps reading "awaiting merge"; merge line
  PRs with a merge commit.
- **Gate teaching.** A rejection or edit at the plan gate is filed as a
  feedback sighting with `evidence.source == "gate"`. Those with a `shift_no`
  are history, not backlog: they show under that shift as
  `at the gate: reject "<task>": <reason>` and never count as open. A rejected
  task is dropped from the plan, so this note is the only record of it.

**Resolution.** The run rows carry `plan_action_id` and `task_index` (1-based
into the plan's tasks as dispatched). `_backlog` joins the mandate's runs to
their plan tasks' `evidence_refs`; a sighting cited by a `landed` run is
resolved. The shift trigger writes `resolved_by_run` / `resolved_at` on the
sighting the first time it sees that, and resolved sightings stay out of the
backlog for good. Computing at shift time was chosen over a hook on the belt
executor's land path for two reasons: it also resolves runs that landed
before the join existed, and the belt stays mandate-agnostic. Persisting is
still needed, because the runs list reads only the workspace's newest 200
actions, and an old landed run would otherwise drop out and reopen its
sightings. Plans read per shift: those behind the mandate's runs, every plan
still at the plan gate, and the last 3 shifts'.

## Endpoints (`/api/v1/belt/mandates`, RBAC mirrors the belt console)

| Method | Path | Gate | What |
|--------|------|------|------|
| POST | `/belt/mandates` | `belt.manage` | Create (charter body + `patrols` senses toggles + optional `upstream` watch list + optional `crew`) → `{mandate}` |
| GET | `/belt/mandates` | `belt.read` | `{mandates}` + health (last shift state, open gate count, sighting count) |
| GET | `/belt/mandates/digest?since=<iso>` | `belt.read` | The workspace digest since `since` (default 24 hours ago); see *Digest* below |
| GET | `/belt/mandates/{id}` | `belt.read` | Bare detail: charter, patrols, upstream, crew, recent shifts (each with `approved_by`, the plan Action's approver such as a user id or `system:belt-policy`, and `policy`, the auto gate's `{decision, reason}`; both `null` when they do not apply), sightings-by-patrol, and the `line` block (see *The line: one branch per mandate*; the `{mandate}` that create, crew and autopilot return carries it too) |
| PUT | `/belt/mandates/{id}/crew` | `belt.manage` | Replace the crew roster: `{crew: [{agent_id, role: dev\|reviewer\|foreman\|tester, concurrency (1-8), setup?: owner\|strict}]}` → `{mandate}`; see *The crew* below |
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

**Realtime:** three mandate topics ride the workspace bus with the same
workspace fan-out as `belt_run_updated` (`_core/realtime/audience.py`), and the
mandates page subscribes to them:

| Topic | When | Payload (plus `workspace_id`) |
|---|---|---|
| `belt_plan` | a plan proposal lands at the gate | `{mandate_id, proposal}` |
| `belt_plan_resolved` | `service.resolve_plan` resolved a plan: the `plan/resolve` route or the auto plan gate | `{mandate_id, plan_action_id, shift_no, resolved_by, approved, rejected}`; `resolved_by` is the approver id (a user id or `system:belt-policy`), the counts are tasks kept and dropped |
| `belt_mandate_updated` | a shift opens (`planning`) and on every `mark_shift` write (`planning`, `in_gate`, `executing`, `done`, `stood_down`) | `{mandate_id, shift_no, state}` |

A plan resolved straight through `POST /instinct/actions/{id}/approve\|reject`
(the Tray, MCP) skips `resolve_plan`, so it sends no `belt_plan_resolved`; its
shift still moves, which sends `belt_mandate_updated`.

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

## The `issues` patrol on GitHub

When the bound repo's `origin` is on GitHub (`git -C <repo> remote get-url
origin`, the slug read with the cloud station's rules and refused when a part
is `.`, `..` or starts with `-`), the `issues` patrol reads
`gh issue list -R <owner/repo> --state open --limit 100 --json
number,title,body,labels,url` (argv, the scrubbed env, 60 s) instead of the
GitLab connector, and files one sighting per open issue carrying a label from
the charter's `issue_labels` (case-insensitive; no labels, nothing taken). A
`gh` failure or a malformed payload files nothing. Any other origin keeps the
GitLab path.

- **Severity** reuses the label map: `p1` and `bug` 4, `p2` 3 (`p0`,
  `critical`, `security`, `blocker` 5), anything else 2.
- **Sighting:** summary `GitHub issue #<n>: <title>` plus the start of the
  body (280 chars); evidence `{number, title, labels, url, text, source,
  dedup_key}` with `source = dedup_key = github:<owner>/<repo>#<n>`.
- **Dedup:** `run_patrols` skips a draft whose `dedup_key` is any existing
  sighting's `source` or `dedup_key`, whichever patrol filed it, so an issue
  already filed by hand through the feedback intake (the pilot files them with
  `source: github:<owner>/<repo>#<n>`) is not sighted again.
- **Closing:** sightings have no close path besides a landed run, so closed
  issues are simply never listed; a sighting whose issue a human closed stays
  open until a task citing it lands.
- **Schedule:** a daily or weekly shift senses at its start as before. A
  `continuous` mandate that toggles `issues` also runs it inside its due check
  (`_continuous_due`, after the gap, gate and in-flight holds, before the
  backlog read), so a new issue makes the mandate due without waiting for a
  shift: one `gh issue list` per mandate per tick.
- **Cross-line requests:** a line picks up any label it lists, so the engines
  line on `qbtrix/creative-core` lists `needs-core` and sees requests the app
  lines file there. They must be filed on creative-core.

Enable it with `"issues"` in `patrols` and `issue_labels` on the charter. No
route edits an existing mandate's charter or patrols yet, so a live mandate gets
them through a new mandate or a direct database edit.

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
lazy import in `cloud/models/__init__.py` (the calendar-doc pattern). A run's
feed is a `BeltRunFeed` row per stage owned by `belt/service.py` (see "The run
feed").

## Soul wiring (demo bar)

When a mandate binds `soul_path`, `soul_link.py` recalls up to 5 memories
before planning and appends an episodic shift summary after every terminal
(dispatched / rejected / stood_down) via the real soul-protocol API
(`Soul.awaken → recall/remember`, then `export` back into a `.soul` file, or
`save_local` for a soul directory). Best-effort throughout — a
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
  `/belt` station to a diff. No human approve path approves a queued run (a
  `code_change` with `station_pending` or no diff,
  `instinct.router._is_queued_station_run`): the single approve answers 409
  `belt.run_queued`, and the instinct and Mission Control bulk-approves leave it
  pending and report it in `missing`. The belt executor still refuses a
  `station_pending` blob that reaches it (`error_class="StationPending"`).
  Mission Control items carry `belt_change_ready` (`true` when a Belt run has
  a diff to approve, `false` for a queued run, `null` on anything else), so a
  bulk bar can leave the queued ones out.
- **`headless`** — `HeadlessTaskDispatcher` files the same queued run, then
  produces the diff with no human in the loop and attaches it; the run becomes
  a pending `code_change` at the per-diff gate. While the station works it the
  console shows `running / <stage>` (orient, develop, check, fix, review); the
  attached diff reads `proposed / gate`, a failed develop `queued / station`
  again, each with a `belt_run_updated`. In production the develop runs
  in a background task, one at a time, so plan approval returns at once. With
  no develop loop wired it degrades to `station`.
- **`bus`** — announce-only (`belt_run_updated(status="dispatched")` under a
  synthetic run id); no run record.

The develop loop for `headless` is `belt/develop_station.ClaudeCodeDevelop`,
wired by a cloud startup hook when `POCKETPAW_MANDATE_DISPATCHER=headless`
**and** `POCKETPAW_FACTORY_DEVELOP=claude` (and, in a process serving cloud
tenants, `POCKETPAW_FACTORY_DEDICATED_HOST=1`; see Security posture; in the
owner setup, an existing `POCKETPAW_FACTORY_WORKTREE_ROOT`). One run walks a
fixed sequence:

```
PREPARE  screen the task text (InjectionScanner, HIGH refuses); refuse any
         charter check whose program is not allowed; resolve the bound repo
         inside POCKETPAW_BELT_REPO_ALLOWLIST (empty = refuse); git worktree
         add --detach at origin/<base> (after a fetch) when an origin exists,
         else the local <base>, in a temp dir (owner setup: under
         POCKETPAW_FACTORY_WORKTREE_ROOT); snapshot the worktree's .git file
ORIENT   LLM work only (recipes skip it): the repo's architecture brief from
         loom (else its C4 list) for the develop and review prompts
WORK     a recipe task runs the charter's recipe command; otherwise
         `claude -p` develops (--permission-mode acceptEdits,
         --output-format stream-json --verbose)
CHECK    run every charter check
FIX ≤2   a red check, or a failed review, sends the failure back to
         `claude -p`, then CHECK again; at most 2 attempts (recipes skip it:
         a red check fails the run)
REVIEW   LLM work only (recipes skip it; the human diff gate still applies):
         an independent read-only `claude -p` judges the diff against the
         task and fails a duplicate of existing code:
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
`headless_state: "queued"`, each station stage then overwrites it with the
stage's name, until it attaches or fails, so a run a restart dropped stays
visible as stuck in the digest (nothing re-drives it yet), and a background
task that crashes is logged at ERROR. The develop request aims at
the blob's `expected_outcome`; the attached run's `summary` becomes the
station's report (checks, setup, orient source, review verdict and notes, fix
attempts), and its `files_changed` is the station's count (else the diff's
`+++` headers).

### The cloud develop station

`POCKETPAW_FACTORY_DEVELOP=cloud` (with `POCKETPAW_MANDATE_DISPATCHER=headless`)
is for a host that should not build or test: nothing agent-written runs on it.
`belt/cloud_station.CloudTaskDispatcher` takes the headless dispatcher's seat
(`headless.set_production_dispatcher`). An approved task is filed as the usual
queued run, then in the background:

```
LAUNCH  task text injection-screened; bound repo inside the allowlist; the
        launch clone (POCKETPAW_FACTORY_CLOUD_LAUNCH_DIR, never the bound repo)
        is re-pointed at the bound repo's origin if it names another one (see
        below); fetch + detach at origin/<base> (a cloud
        session starts from the local commit); `claude --cloud "<brief>"` under
        belt/pty_run.py (the CLI refuses a non-TTY), which streams the CLI's
        output, so a launch that times out (300 s) still shows what it printed;
        the session URL is read from that output. If the CLI stops at its
        folder-trust prompt, pty_run exits and the run fails with `LAUNCH: the
        claude CLI does not trust <dir>`; open the clone in claude once and
        accept the prompt (Belt never answers it). One launch at a time.
TRACK   gh finds the PR whose body has the line `Belt-Run: <run id>`, then polls
        it every POCKETPAW_FACTORY_CLOUD_POLL seconds:
          merged   -> approved as `system:fast-lane` (audited), mark_executed:
                      the run is landed, with pr_url, head branch, merge sha
                      and the PR's diff (so the UI can show touched files;
                      the diff Clef scored is reused if the head has not
                      moved since); then `git fetch -q origin` in the bound
                      repo, since nothing else refreshes its origin/<base>
                      in cloud mode and the blueprint reads the C4 model
                      there (a failed fetch is logged, never fails the land);
                      then the screenshots and the tester (below)
          refused  -> closed unmerged, or a `Fast lane: not eligible` comment:
                      `gh pr diff` is attached as the run's diff, which waits
                      at the per-diff Instinct gate (approving it lands it on
                      the line like any run; close the PR then)
          stuck    -> mergeable=CONFLICTING, or a failed check on the head
                      commit, for POCKETPAW_FACTORY_CLOUD_FIX_GRACE seconds:
                      one fixer session (see below), at most
                      POCKETPAW_FACTORY_CLOUD_FIX_ATTEMPTS per run, then the
                      PR escalates like a refusal
          no PR    -> none by POCKETPAW_FACTORY_CLOUD_NO_PR_TIMEOUT after the
                      newest launch: one fresh worker, then the run is
                      released (rejected); see Silent workers below
          timeout  -> headless_error, the run stays queued
```

**One launch clone for every repo.** The claude CLI asks a folder-trust
question the first time it runs in a new folder, and pty_run fails the launch
there, so a per-repo launch folder would stall every new repo on that prompt.
Belt keeps the one clone the CLI already trusts and re-points it instead: when
the bound repo's GitHub slug differs from the clone's, `_launch` runs
`git remote set-url origin https://github.com/<slug>.git` before the fetch and
logs the switch (`launch clone origin <old> -> <new>`). The slug comes from the
bound repo's origin through `_GH_SLUG` and is refused if a part is `.`, `..` or
starts with `-`; git runs without a shell. The URL is always rebuilt from the
slug, never copied, so credentials in the bound repo's remote never reach the
clone. Every launch, worker or fixer, re-checks and re-points under the launch
lock, so a fixer opens on the right repo even if another repo launched in
between, and two launches never race on the remote.

**Fixers.** A cloud worker stops once its PR is open, so a PR can get stuck
after it: red CI on a runner the session never saw (an older Swift on macOS),
or a conflict after a sibling PR from the same shift merged first. The tracker
reads `mergeable` and `statusCheckRollup` from the same `gh pr view` poll and
classifies checks the way `scripts/fast-lane.sh` does (the lane's own Fast Lane
check and Copilot checks don't count; the rollup has no "required" flag, so any
failed check counts). A PR that stays stuck for the grace period (default 300
s, so the lane's own refresh or a running CI can settle first) gets ONE fixer,
launched through the same path as the worker (launch clone, `claude --cloud`,
one launch at a time) with a fix brief instead of the task brief:

- conflicting: check out the PR's branch, rebase onto the latest
  `origin/<base>`, keep both sides' behavior (shared lists keep both entries),
  run the charter checks until they pass, `--force-with-lease` to the same
  branch, comment on the PR;
- red CI: the failing check's name and the last 40 lines of
  `gh run view <run> --job <job> --log-failed` (fenced as data); fix minimal,
  run the charter checks, push to the same branch, stop (if the new head goes
  red, the tracker starts the next fixer with the new log, so two fixers never
  push to one branch at once).

Both say: no new PR, keep the `Belt-Run` line, no AI attribution, never merge.
The tracker keeps polling the same PR under the same overall timeout. A fixer
is launched at most once per head commit: the next one waits for a new head
(a fixer that pushes nothing leaves the run to the timeout). Every attempt is
appended to the blob's `fixes` list as `{kind: "rebase"|"ci", session_url, at}`,
or `{kind, error, at}` when the launch failed (that counts as an attempt and
tracking goes on), and emits `belt_run_updated` with `stage="fix"`. When the
attempts are used up and the PR is still stuck, its diff is attached for the
per-diff gate, with a summary saying how many fixers ran.

**Silent workers.** A cloud worker can stop without opening a PR: it decided
the issue cannot be done as written, or it simply stalled. Its run would then
sit in flight until the run timeout, and the continuous scheduler holds a
mandate's next shift while any of its runs is in flight (up to the stall
timeout), so one silent worker held the whole line for hours. The tracker now
watches for that. When no PR with the run's `Belt-Run` line exists
`POCKETPAW_FACTORY_CLOUD_NO_PR_TIMEOUT` seconds (default 3600) after the newest
launch (`cloud.launched_at`, or the last relaunch; a resumed run without
`launched_at` counts from the resume):

1. If the run was already resolved meanwhile (a human rejected it), tracking
   stops.
2. When the task cites `#n`, the tracker reads that issue on the bound repo
   (`gh issue view <n> --json comments`). The repo's CLAUDE.md tells a worker
   facing an impossible task to comment on the issue and stop, so any comment
   created after `cloud.launched_at` counts (cloud workers comment as the
   repo's own user, so the author is not checked). The run is released with
   that comment's first line as the reason, and the issue gets the label
   `needs-human` (created on the repo first if it is missing). Nothing is
   relaunched.
3. Otherwise ONE fresh worker is launched through the same `_launch` (launch
   lock, launch clone, the same brief plus a line saying a previous attempt
   produced no pull request, and to stop if a PR with the run's `Belt-Run` line
   already exists, since the first worker may still open one late), at most
   `POCKETPAW_FACTORY_CLOUD_RELAUNCHES` (default 1) per run. A relaunch that
   fails to launch fails the run like a first launch (`headless_error`, still
   queued, no longer in flight). Each is appended to the blob's `relaunches` as
   `{session_url, at}`, the run emits `belt_run_updated` (`running`, `cloud`),
   and the no-PR clock restarts. `cloud` keeps the first session and its
   `launched_at`, which still bounds the run timeout.
4. When the relaunches are used up, the run is RELEASED: rejected through the
   same `InstinctStore.reject` a human reject uses, as `system:belt-cloud`, with
   the reason `no PR from N worker(s) in Xm`. Its `headless_state` is cleared,
   it emits `belt_run_updated` (`rejected`, `done`), and the backend logs one
   line (`belt cloud: released run <id>: <reason>`). A rejected run is not in
   flight and its sighting stays open (only a landed task resolves one), so the
   next shift re-plans it.

The run timeout is checked before the no-PR timeout, so a run whose overall
timeout has also passed (a resume long after launch) still ends with the usual
`headless_error`.

**Lane nudge.** The fast lane is the repo's own workflow (`Fast Lane`, on
`pull_request_target`, `workflow_run` and a 5-minute cron), and it can go
quiet: GitHub's cron does not fire reliably on a new repo, and a merge made
with `GITHUB_TOKEN` starts no further workflow, so a green PR can sit unmerged
for an hour. The tracker reads `labels` on the same `gh pr view` poll. When the
PR is open, carries the lane label (`POCKETPAW_FACTORY_CLOUD_PR_LABEL`), is
`MERGEABLE` (not `CONFLICTING`, not still `UNKNOWN`) and every counted check
has finished green (the fixer's check rules; a pending check is not green), and
it stays that way for `POCKETPAW_FACTORY_CLOUD_LANE_NUDGE` seconds (default
300), the tracker runs `gh workflow run <POCKETPAW_FACTORY_CLOUD_LANE_WORKFLOW>
-R <slug>` (default `Fast Lane`; with no input the lane sweeps every open
fast-lane PR) through the scrubbed runner, no shell. It repeats at most once per
interval while the PR stays ready, and at most once per interval per REPO
however many of its PRs are tracked (a module-level last-dispatch time per
slug, stamped on the attempt so a broken `gh` is not retried every poll). Each
dispatch is appended to the blob's `lane_nudges` (wall-clock times, last 20). A
failed dispatch is logged and tracking goes on. Red and conflicting PRs never
get a nudge: those are the fixer's.

**Screenshots.** The app repos' PR CI uploads an artifact named `snapshots`
with PNGs of the app views. Once a run's PR is known (the first `gh pr view`
that is not already merged) and again when it lands, the tracker asks
`gh api repos/<slug>/actions/runs?head_sha=<head>` (the head must be a hex sha)
for the PR head's CI runs, checks the newest 5 with
`gh api repos/<slug>/actions/runs/<id>/artifacts` for an unexpired `snapshots`
artifact, and runs `gh run download <id> -R <slug> -n snapshots -D <temp dir>`.
From the temp dir it keeps only regular files (no symlinks, `os.walk` never
follows a linked dir) whose sanitized basename (`[A-Za-z0-9._-]`, others become
`_`, no dotfiles, first of a name wins) ends in `.png` and whose bytes start
with the PNG signature, at most 40 files and 20 MB per run. The run's dir
(`<POCKETPAW_FACTORY_SCREENSHOT_DIR>/<run id>/`, default
`~/.pocketpaw/belt/screenshots`) is emptied first, so it only ever holds what
the blob lists, and the blob gets `screenshots: [{name, bytes, sha}]` (sha256).
No artifact, a failed download or anything else is logged and never touches the
run. Nothing removes old run dirs yet.

**The tester.** With `POCKETPAW_FACTORY_CLOUD_TESTER=1` (off by default) and a
live `tester` seat on the mandate's crew (the first one, read like a dev seat
through `crew_seat_for_task(role="tester")`), a landed run gets ONE more cloud
session, launched through the same `_launch` as a fixer (launch lock, launch
clone re-pointed and detached at the new `origin/<base>`, the tester agent's
instructions fenced as data). Its brief: read the source issue's acceptance
test and the merged PR, exercise the landed feature the way a user would and
try to break it. For engine code it writes adversarial, edge-case and fuzz
tests; when the launch clone has a `Sources/*App` it is app UI, and the brief
names the `gh run download <id> -R <slug> -n snapshots` command (the CI run the
screenshots came from, when known) and asks it to look at every PNG against
`docs/design.md` and the issue: text, layout, spacing, light and dark, empty
states. Each bug becomes one GitHub issue with a repro, labelled `bug`, `gap`
and the line label copied from the source issue (the brief lists the charter's
`issue_labels`); it never fixes a bug itself. Tests worth keeping that pass go
in ONE PR labelled `fast-lane` whose body carries `Belt-Test: <run id>`; no AI
attribution, never merge. The run blob records `tester: {session_url, at}`
(`{error, at}` when the launch failed) and the run emits `belt_run_updated`
with `status: "landed"`, `stage: "test"`. One tester per run (a blob that
already has `tester` gets none); a tester problem never touches the landed run.
Its issues come back through the `issues` patrol when they carry a line's
labels.

**The tester's PR.** Once the tester is launched, the same tracker follows its
PR (`_track` with `kind="tester"`), found by the `Belt-Test: <run id>` line.
Each poll does what it does for a worker's PR: the lane nudge when it sits
green and mergeable, a fixer when it is red or conflicting past the grace
period (the fix brief keeps the `Belt-Test` line), and a merge ends it. The
results go on `tester`: `pr_url`, `state` (`open` once found, then `merged`,
`closed`, or `none` when no PR appeared within
`POCKETPAW_FACTORY_CLOUD_NO_PR_TIMEOUT`, since a tester may file only issues)
and `fixes`. The tester's fixers have their OWN cap: `tester.fixes` is counted
against `POCKETPAW_FACTORY_CLOUD_FIX_ATTEMPTS` on its own, so a hard worker PR
never starves its tester's PR, and a fixer's update reads `landed`/`test`, never
`running`. A refused tester PR, or one still stuck past the cap, is left open
for a human: there is nothing to escalate. The tracker's timeout is the run
timeout counted from `tester.at`; a failure or timeout is logged and ends it.
Lane nudges for the tester's PR go on the run's `lane_nudges` like the
worker's. Tester tracking never holds the line: it starts only after the land
is marked executed, so the run already reads `landed` (not in flight) to the
mandate's due check, and it never runs under the worker's failure guard, which
would re-queue the run. No screenshots are fetched for a tester PR: they would
replace the run's.

The brief tells the session to do the task, follow the repo's CLAUDE.md, run
the charter checks until they pass, open ONE PR to the base that says
`Closes #<issue>` (the first `#n` in the task text) and carries the
`Belt-Run` line, add the label (`POCKETPAW_FACTORY_CLOUD_PR_LABEL`, default
`fast-lane`), add no AI attribution, and stop without merging. The seated dev's
instructions ride the brief fenced as data, as in the local develop prompt.
The repo's own fast lane and CI are the diff gate for small PRs; the plan gate
is unchanged. Once the PR is known, its diff also goes to Clef's shadow scorer
(`belt/clef.shadow_diff`), which records a verdict and gates nothing. There is no local ORIENT, CHECK or REVIEW in this mode: the
session works in the cloud.

**Resume after a restart.** Tracking is process-local, so `wire_cloud` schedules
one background `resume_all` at startup. It reads the workspaces that hold a
mandate (`mandates.service.mandate_workspace_ids`, paused ones included) and,
in each, every pending run still marked `headless_state: "cloud"` with no diff.
Failure, escalation, release and land all clear that mark, so it means
"launched, tracker alive when the process died". A `cloud.session_url` alone is not enough: a
tracker that failed or timed out keeps the session but drops the mark, and those
runs stay as they are. Each resumed run goes back to `_track` (the bound repo's
slug, the stored session, the PR found by its `Belt-Run` line); nothing is
launched. The run timeout counts from `cloud.launched_at` (stored at launch), so
an expired run gets one poll and lands if its PR merged while the backend was
down; runs from before the field get a fresh timeout. Fixer attempts carry over
on `fixes`, and each entry records the head it was launched on, so a resumed
tracker does not start a second fixer on that head. Relaunches carry over on
`relaunches`, so a resumed run never gets more than
`POCKETPAW_FACTORY_CLOUD_RELAUNCHES`, and its no-PR clock counts from the last
relaunch. A released run is rejected, so it is never resumed. A landed run is
no longer pending, so its tester is never relaunched; with the tester on, its
PR tracker is resumed when `tester.state` is `open` or unknown (no PR seen yet)
and `tester.at` is inside the run timeout (the newest 200 executed runs per
workspace are read). A store error skips that workspace only, and startup logs
one line: `belt cloud: resumed N run tracker(s) and M tester tracker(s) after
startup`. A run whose launch was still in progress when the process died has no
mark and stays queued.

### The crew: cloud Agents as workers

A crew member IS a cloud Agent (`/agents`): its model, instructions (system
prompt) and soul live on the Agent and are edited in the agent editor. The
mandate stores only a roster, `MandateDoc.crew`, one seat per agent:

```
{agent_id, role: dev | reviewer | foreman | tester, concurrency: 1-8, setup: owner | strict | null,
 seated_by}
```

`PUT /belt/mandates/{id}/crew` replaces it (create takes the same `crew`
list). Vouching is per seat. A seat whose agent is already on the roster
carries over with its `seated_by` (the admin who seated it), so another admin
can change its role, concurrency or setup, or edit the rest of the roster,
without being able to read that agent; one deleted since doesn't block edits
either. A NEW agent must be one the caller can read, live in this workspace
(another workspace's public agent is refused) and be enabled
(`mandate.crew_agent_disabled` otherwise); the caller is stamped as its
`seated_by` (server-set). One seat per agent, at most 20 seats; anything else
is a 422. Removing a seat always works. The write emits
`mandate.crew_changed`.

**Seat rule.** When a plan task is dispatched, `StationTaskDispatcher` seats it
on a dev: the roster's `dev` seats in roster order, minus any whose agent is
gone, disabled or no longer in the workspace; task N (1-based, the plan's
order) goes to dev `(N - 1) % devs` (`mandates.service.pick_dev`). Two devs
split a two-task shift. N is the task's index in its own plan, so the
round-robin restarts at the first dev every shift: on one-task shifts only
the first live dev ever works. The seat rides the run blob as
`worker: {agent_id, name, setup, seated_by}`; a mandate with no live dev gets
`worker: {}` and the factory env decides everything, exactly as before crews.
A re-develop (moved base) keeps the run's seat.

**Per-worker settings.** The headless runner reads the seated agent when the
develop runs, not at dispatch (`mandates.service.crew_worker`), so an edit in
the agent editor reaches a queued run, and the agent's instructions never sit
on a run blob other members can read. Seating and this read both use the
roster route's visibility-checked read (`agents.service.get_for_viewer`) AS
`seated_by`, so an agent that is gone, disabled, or no longer readable by the
admin who seated it (its owner made it private) is a gone seat: the run uses
the factory env for everything, setup included, and the report says
`worker: <name> (seat unavailable: ...)`. `DevelopRequest` carries `worker`,
`model`, `instructions`, `setup` and `worker_note`:

- `model` sets `--model` on the DEVELOP and FIX calls, over
  `POCKETPAW_FACTORY_CLAUDE_MODEL`. Agent models are catalog ids, so
  `anthropic/claude-sonnet-4-5` becomes `claude-sonnet-4-5`. Only a Claude
  model passes (`claude-*`, optionally region-dotted, or a CLI alias: sonnet,
  opus, haiku, opusplan, fable, default); another provider's model (`gpt-4o`,
  `openai/gpt-4o`) or a value that reads as a flag falls back to the env
  (`foreman.cli_model`) and the report says `model default: 'gpt-4o' is not a
  Claude model`. REVIEW keeps the factory model: the reviewer stays
  independent of the worker.
- `instructions` (capped at 4,000 chars) ride the develop and fix prompts as
  style notes, after the charter, INSIDE the `<untrusted>` fence: an agent's
  owner edits them without `belt.manage` and they are re-read at every
  develop, so they can shape style but never the rules, tools or files. The
  agent's name stays out of the prompt for the same reason.
- `setup` (`owner` / `strict`) picks the Claude setup for the whole run, over
  `POCKETPAW_FACTORY_CLAUDE_SETUP`; unset follows the env. An `owner` seat
  still needs the operator's `POCKETPAW_FACTORY_WORKTREE_ROOT`, or the run
  fails at PREPARE, so a workspace admin can't turn on the owner setup where
  the operator never configured one.

The report gains `worker: <name> (model <id or default>)`.

A `tester` seat never takes plan tasks; the cloud station seats the first live
one on each landed run when its tester is on (see *The cloud develop station*).

Not wired yet: `reviewer` and `foreman` seats are stored and shown but the
review and foreman calls still use the factory defaults; `concurrency` is
stored and shown but not enforced (develops still run one at a time; it is
for the parallel-worker queue); a plan task can't name its worker (the seat
rule is round-robin only).

### Claude setup: strict and owner

`POCKETPAW_FACTORY_CLAUDE_SETUP` (or a crew seat's `setup`) picks how the
develop, fix and review calls run the Claude Code CLI.

- **`strict`** (default; hosted deploys). Every call passes
  `--setting-sources ""`, `--strict-mcp-config` and
  `--settings '{"disableAllHooks":true}'`: no settings files, no MCP servers,
  no hooks. The worktree sits in a system temp dir. The agent codes with no
  CLAUDE.md beyond the repo's own and no skills.
- **`owner`** (a local factory on the owner's own machine). The factory codes
  with the owner's real Claude Code setup: workspace and repo CLAUDE.md files,
  skills, hooks, settings and MCP config. The worktree is created under
  `POCKETPAW_FACTORY_WORKTREE_ROOT`, which must be an existing directory
  outside the bound repo; the station refuses to wire without it, and a run
  refuses at PREPARE if it goes missing. Point it at a directory inside the
  owner's workspace, e.g. `<workspace>/paw-worktrees/factory-runs`, so CLAUDE.md
  discovery walks up from the worktree through the workspace. The three
  isolation flags are dropped; the tool surface is unchanged (`--tools`, the
  `./**`-scoped allow rules, WebFetch/WebSearch/Task denied). Never `--bare`.

**Trust restore** (owner setup only). Owner mode loads whatever agent config
sits in the worktree, and the agent can write to the worktree. So immediately
before every owner-mode claude call (DEVELOP, each FIX, REVIEW) the station
deletes every entry named `.claude`, `CLAUDE.md`, `CLAUDE.local.md`,
`AGENTS.md` or `.mcp.json`, at any depth and in any letter case (on a
case-insensitive volume such as macOS APFS the CLI opening `AGENTS.md` reads an
`agents.md`), whether tracked, untracked or gitignored (a symlink is unlinked,
never followed), then runs
`git checkout <base sha> -- <those paths tracked at base>`. The base sha is
the BASE's commit (`origin/<base>` with a remote, else the local base), never
the mandate's line: the line holds commits a gate approved but the captain has
not merged. The settings, hooks, MCP servers and instructions that load are
always the base's committed ones. The `.git`-file integrity check stays, and
DONE refuses any diff touching one of those names (`.claude/`, `.mcp.json`,
CLAUDE.md, CLAUDE.local.md, AGENTS.md, at any depth, any case) in both setups, so no
factory run puts agent instructions on a line. For LLM runs in owner setup the
restore before REVIEW already reverts any plant, so the DONE rule is defense in
depth there (a recipe, which has no claude step, still meets it). Two
consequences: no mandate run can change a CLAUDE.md or AGENTS.md file (do it
on the base by hand), and in owner setup a line whose agent config differs
from the base (only a hand commit on the line can do that) fails every run at
DONE, because the restore's revert is a protected path; merge the line or move
that change to the base.

The foreman and the autopilot personas stay strict in both setups: they read
untrusted third-party text, so they get the architecture by prompt injection
instead (see The foreman). The scrubbed env applies in both setups.

### ORIENT: the architecture as the source of truth

Between PREPARE and WORK (LLM work only) the station orients the agent in the
repo's existing architecture so it extends what exists instead of building a
second copy (`belt/orient.py`):

1. Resolve a loom world model: `<loom dir>/worldmodel-<repo dir name,
   lowercased>.json`, where the loom dir is `POCKETPAW_FACTORY_LOOM_DIR`, else
   the nearest ancestor of the bound repo that holds a `.loom/` directory (the
   workspace's, for repos checked out in it).
2. Run `loom orient -model <model> -json -- <task>` through the station's one
   runner (argv list, scrubbed env, 60s timeout; the `--` keeps a task that
   starts with `-` from being read as a flag). The binary is
   `POCKETPAW_FACTORY_LOOM_BIN`, else `loom` on PATH, else `~/go/bin/loom`.
3. Render the brief as an `EXISTING ARCHITECTURE` block, capped at about 4k
   characters: the components the task touches, the code that already exists
   for it (symbols grouped per file, or C4 components with their description
   when the model has no symbols), the blast radius, entrypoints, and the
   rules, ending with "reuse before you add; do not create a second copy of
   anything listed".
4. No world model, or loom failing: fall back to the repo's
   `docs/c4/model.json` list. Neither: no block. ORIENT never fails a run; the
   summary's `orient:` line names the source (`loom worldmodel-<x>.json`,
   `no world model; C4 docs/c4/model.json`, `no world model`, with
   `loom orient failed (exit N)` in front when loom was tried).

The block goes into the develop prompt (after the task and charter, outside the
`<untrusted>` fence: it is owner-authored repo data) and into the review
prompt. REVIEW must fail a diff that adds a module, class, component or helper
duplicating one that already exists, listed or found in the repo, and its notes
must name what is duplicated and the path of the existing one; the fix loop
then gets those notes like any other review failure.

World models are generated files under the workspace's `.loom/` (gitignored):
the `loom-sync.sh` Stop hook refreshes pocketpaw and soul-protocol. loom has
symbol extractors for Python and Go only, so `worldmodel-paw-enterprise.json`
and `worldmodel-ripple.json` are built from C4, kb and the shared soul alone
(`loom build <repo> --scope <scope> --out <model>` from the workspace root);
their briefs name components, not files, and nothing refreshes them yet.

### The line: one branch per mandate

Every mandate's runs land on one branch, its **line**: `belt/line/<mandate id>`.
The name comes from the mandate id alone, and only a Mongo ObjectId (24
lowercase hex) makes one (`belt.executor.line_branch`), so no task or user text
reaches a ref name. A run with no mandate (a hand-proposed change) keeps its
own `feat/belt-<id>` branch. Runs stack on the line; the captain merges the
line into the base when ready. The alternative, planning nothing while a
landing is unmerged, stalls the factory overnight.

**Develop** (`develop_station._resolve_base`). When the line exists, a run
starts from its tip: the local branch, or `origin/<line>` when the repo has a
remote and the pushed line is ahead (refetched each time; a tracking ref left
by a deleted remote branch never counts; diverged local and pushed tips stop
the run until a human merges them). Before that the line is synced with the
base:

```
line tip already in the base (merged)  ──▶ the line moves to the base
base has commits the line lacks        ──▶ base merged into the line
                                           (throwaway worktree, merge commit)
that merge conflicts                   ──▶ the run stands down (headless_error),
                                           a "line" sighting is filed, the line
                                           is untouched
```

Refs only move by compare-and-swap (`git update-ref <ref> <new> <old>`), and
history is never rewritten. The conflict sighting goes through
`mandates.service.file_station_sighting`, deduped on the line tip, so a
standing conflict files once. The develop report gains a `line:` row saying
which case ran.

**Landing** (`belt/executor.py`). The approved diff is applied in a throwaway
worktree detached at the line tip (or the base when there is no line yet),
committed, and the line ref moves by compare-and-swap from the sha it was read
at, so nothing needs the line checked out and two landings can't both win. A
run that carries a `title` (every mandate task) commits as `feat: <title>`
(kept as written when the title already has a Conventional-Commits type),
trimmed to 72 chars; the body is the task's why followed by the station
report. A hand-proposed change (no title) keeps `feat(belt): <summary>`. The
run's blob records `branch` (the line), `commit_sha` (the run's own commit)
and `pr_url`.

**Remote.** With an `origin` the line is pushed after every landing, and the
first landing opens its PR; `GhCliPrOpener` returns the branch's open PR when
there is one, so later pushes update the same PR and every run on the line
carries its url. Local-only repos keep the line local. Once the line ref has
moved the run **is** landed: a push or PR failure after that is written into
the outcome (`pr_url` stays empty) and the next landing pushes again. Failing
the run would make the Foreman re-plan work the line already holds. A per-run
`feat/belt-<id>` branch keeps the old rule: a push or PR failure fails the run
and deletes the local branch, so a retry can branch again.

**On the wire.** The mandate detail (`GET /belt/mandates/{id}`) carries a
`line` block read from git (`belt.executor.line_status`; no fetch, every git
call bounded by the executor's subprocess timeout):

```
line: {branch, base, exists, ahead, merged, pr_url, subjects}
  branch    belt/line/<mandate id> (null when the id is not an ObjectId)
  base      the repo's checked-out branch, the develop station's default base
  exists    the local line ref is there; false on any git error or timeout
  ahead     commits on the line that neither origin/<base> (as last fetched)
            nor the local base holds; the line's own base-sync merges count
  merged    ahead == 0: the tip is in the base
  subjects  those commits' subjects without the merges, newest first, max 30
  pr_url    the line's open PR (gh pr list), looked up only with an origin
            while ahead > 0; null without one or on any gh failure
```

`GET /belt/mandates` does not carry it: that would be several git calls and a
`gh` call per mandate. `belt_run_updated` names the landing `branch` (the
line, for a mandate run) next to `pr_url`, so the run chip updates from the
bus without a refetch.

**In the UI** (paw-enterprise) the line shows in three places: the per-diff
approve confirm names the line (`belt/line/<id>`) and says the captain merges
it into the base when ready; the run card and run page show the line as the
run's branch chip once it lands; the mandate page's Line panel shows the
branch, how many commits are ahead of the base or that it is merged, and the
line's PR, or the `git merge --no-ff belt/line/<id>` command to run on the
base when there is none.

**Merge line PRs with a merge commit** (`--no-ff`, GitHub's "Create a merge
commit"), not a squash or a rebase. A squash writes new commits, so the
line's own commits never become ancestors of the base: `ahead` never drops,
`merged` stays false, history rows keep reading "awaiting merge", and the
next run's base sync merges the squashed copy back into the line.

**Re-develop.** Two runs of one shift develop from the same line tip. Once the
first lands, the second's patch may no longer apply on the moved line (or,
for a hand-proposed run, the moved base). For a headless run (blob `headless`)
the executor checks the patch with `git apply --check` before the `--3way`
apply; when both fail it does not fail the run. The same happens when the line
moved between the read and the swap. It **re-develops** it:

```
approved ──apply conflict / line moved──▶ blob: diff cleared, station_pending,
                                   redevelop=1, summary back to the expected outcome
                             status: approved → pending  (event action_redevelop)
                             belt_run_updated(queued, station)
         ──after cleanup──▶  headless dispatcher develop(run_ref): the station
                             regenerates the diff against the current line
                             ──▶ pending at the per-diff gate (fresh approval)
second conflict on the same run ──▶ failed: "base moved twice: …"
no develop loop wired           ──▶ failed: "… not wired to re-develop it"
```

The re-develop is not a terminal, so the Decision-Graph chain stays open and
the run keeps its `correlation_id`; it closes once, when the run lands or
fails. The status flip uses the store's `_update_status` with
`require_status=approved`, so a concurrent decision makes the flip a no-op
(the run then fails with that reason). The reopened row keeps its first
`approved_by` / `approved_at` until the next approval overwrites them.

### The run feed

The run page shows what each station step did, as it happens and after a
reload. Every step is a stage of the run's feed: `orient`, `develop`, `check`,
`fix`, `review` (`feed.STAGES`). All three claude seats (develop, fix, review)
run with `--output-format stream-json --verbose`; the station's runner hands
each stdout line over as it arrives (the `Runner`'s optional `on_line`, read in
chunks so a line past 64 KiB is still one line), and the line becomes chat
frames at once. Checks, a recipe command and ORIENT are rows the station writes
itself. Path, per line:

- Scrub first: the station strips the worktree and the bound repo (which the
  worktree's `.git` file names) and writes the host account as `user` in an
  `ls -l` owner/group column or a home dir, on each live line, on the final
  stdout/stderr, and on every check, recipe and git error tail, so nothing
  downstream carries an absolute path. `<root>/x` becomes `x` and a bare
  `<root>` (`cd <wt> &&`, a `pwd` result, `working in <wt>.`) becomes `.`; only
  whole paths match (a `/app` root leaves `src/app/page.tsx` alone, a path
  opening a line in raw stream-json after a literal `\n` still counts, a
  sibling `<wt>-old` keeps its path), in both the station's spelling and the
  physical one the CLI reports (macOS `/private/var/...`). A charter command
  shown on a check or recipe row is scrubbed the same way.
- Parse: `belt/feed.py` `FrameReader` turns each line into `AgentEvent`s
  (`agents/protocol.py`) and those into chat frames through
  `steps.agent_event_frame`, the adapter the group/DM bridge uses: `thinking`
  `{content}`, `tool_start` `{tool, input, narration, call_id}`, `tool_result`
  `{tool, output, call_id}`. The developer's prose has no chat-step kind and is
  a `thinking` frame; back-to-back prose blocks are a blank line apart. A
  result pairs with its call by `call_id` (parallel calls of one tool can
  finish in any order). A row's narration keeps its verb: `Read <path>`, `Edit
  <path>`, `Write <path>`, `Run <first line of the command>`, `Grep <pattern>`,
  `Find <pattern>` (Glob), redacted and cut to 80 chars of subject. System
  lines, rate-limit events and the result envelope are skipped; the station
  reads the envelope separately (`foreman.claude_result_envelope`), so a
  seat's result text and its `is_error` check work in both formats.
- Live: `RunFeed.add` publishes the frame through `steps.scrub_frame` (the
  recorder's own caps and redaction: secret-named input keys masked, every
  input string and every output redacted with `security.redact`, outputs and
  thinking capped) with its `stage` added, on the run's chat-runs
  `RunStreamTransport` stream (Redis in production, key `run:belt:<id>:events`;
  the in-memory buffer without `POCKETPAW_REDIS_URL`). A publish failure is
  logged once and ends publishing for that call; the run goes on.
- Stored: after each round of a stage the station folds every frame that stage
  had in this call into a `StepRecorder` (the chat steps shape) and upserts its
  `BeltRunFeed` row, so `check` and `fix` rows hold both rounds. A row is
  stored before a failed seat raises (the open call reads `missing_result`),
  and a call empties all five rows when it starts, so an attempt that fails
  early never shows an earlier one. A fold or save failure is logged and never
  fails the run. A failed seat's `headless_error` carries what claude said,
  never raw stream-json.
- Station rows: ORIENT is one `Orient` tool row whose output is the note and
  the architecture block; each check is a `Bash` row narrated `Run <command>`
  that shows running, then the check's tail and `(exit N)`; a recipe is the
  same row under `develop`; each review ends with a `Review` row narrated
  `Review: pass` or `Review: fail`, its output the notes one per line (`no
  notes` when there are none). A recipe run has no `orient`, `fix` or `review`.
- Caps, per station call: 2,000 frames and 2 MB published
  (`FEED_MAX_STEPS`, `FEED_MAX_BYTES`; the control frames `start`, `stage` and
  `stream_end` are never dropped, and `stream_end.omitted` counts what was);
  stored rows share 2,000 steps and 2 MB across the five stages (each row's
  overflow in `steps_omitted`).
- Storage: `BeltRunFeed` (`belt_run_feeds`), one row per (workspace, run,
  stage), unique on that key and written with one atomic upsert. Only
  `belt/service.py` touches it (`save_run_feed` / `get_run_feed`). It lives
  outside the Instinct `code_change` blob because `GET /belt/runs` reads every
  blob. Measured against JSON files beside the worktree, 2,000 steps (2.6 MB of
  JSON): Mongo upsert 17 ms / read 5.6 ms, file write 12 ms / read 5 ms. Same
  order; Mongo is readable from every web process, a file is host-local.

#### Wire contract

| What | Shape |
|---|---|
| `GET /api/v1/belt/runs/{id}/stream?after=<cursor>` (`belt.read`) | `text/event-stream`; each frame `id: <cursor>`, `event: <name>`, `data: <json>`; a foreign or non-belt run is a 404 (`belt.run_not_found`) before the stream opens |
| `start` `{}` | a station call (one attempt) begins; a client clears its feed |
| `stage` `{stage, round}` | a step begins; `stage` in `orient\|develop\|check\|fix\|review`, `round` counts repeats of that stage in the call (a second check is round 2) |
| `thinking` `{stage, content}` | a whole block of reasoning or prose (not a delta) |
| `tool_start` `{stage, tool, input, narration, call_id}` | a call begins; `input` scrubbed and capped |
| `tool_result` `{stage, tool, output, output_truncated, call_id}` | that call's result |
| `file_touched` `{stage, path, component, tool, call_id}` | right after an `Edit`/`Write`/`MultiEdit` `tool_start`: the repo-relative file and the blueprint component that owns it (`null` when no glob does); see "The run on its blueprint" |
| `stream_end` `{ok, omitted}` | the attempt ended (`ok` false on a failed run); terminal |
| `stream_end` `{from_history: true}` | no live stream and the run is not being developed: read `GET /belt/runs/{id}/feed?stage=` for each stage |
| `error` `{code: "run.stream_timeout", message}` | the subscription hit the chat run stream's lifetime cap; reopen with `after=<last id>` |

Replay: `after=0` (the default) serves the newest attempt from its `start`,
so a reload shows what a viewer saw live; a cursor resumes right after it. A
cursor at an attempt's `stream_end` resumes into the next attempt; one inside
an earlier attempt ends at that attempt's `stream_end`, and the client reopens
with `after=0` on the next `belt_run_updated`. A run being developed
(`headless_state` set by the background dispatcher) whose stream does not
exist yet is waited on. The stream lives 6 hours from `start` and 1 hour after
`stream_end`. Each stage start also records the step as the run's
`headless_state` and emits `belt_run_updated` on the workspace bus with
`status: "running"` and `stage` set to the step, so a page can (re)open the
stream when a station picks a run up; `GET /belt/runs` reads the same
`running` / step. The attached diff emits `proposed` / `gate`, a failed
develop `queued` / `station`. Heartbeats are `: ping` comments between 15 s reads (the chat stream's
`transport.sse_tail`, which both routes use).

`GET /api/v1/belt/runs/{id}/feed?stage=<stage>` (`belt.read`) returns `{action_id,
stage, steps?, stepsOmitted?}`, the steps in the chat history wire shape
(`steps_wire_fields`), so the UI maps them with `persistedStepsToEntries` and
renders them with `ThinkingSteps`. No `steps` key when the stage recorded none;
the same tenancy 404 as `GET /belt/runs/{id}`.

### The run on its blueprint

A line's blueprint is its C4 model, `docs/c4/model.json` in the bound repo. The
Atlas draws a run on it, so every file the run touches has to land on a
component. c4-gen computes file membership but never writes it, and loom reads
Python and Go only, so the model carries it: a component may list `paths`,
repo-relative globs.

```json
{"id": "ledger", "name": "Ledger", "technology": "Python",
 "paths": ["chai_ledger/**"]}
```

- Globs: `*` and `?` stay inside one path segment, `**` spans any number of
  segments (`**/test_*.py` matches `test_a.py` too), a trailing `/` means
  everything under it, and the pattern must match the whole path. No `[...]`
  classes.
- Join (`belt/orient.py` `path_index` + `component_for`): the most specific
  matching glob wins: the most literal (non-wildcard) characters, then the
  fewest wildcards, then the component declared first. Only the model's own
  system (its `scope`, else the first with containers) owns files. A file no
  glob matches has `component: null`; BF-6 turns that into drift.
- Paths are normalised first (`./a//b` is `a/b`); an absolute path or one that
  climbs out with `..` is not a repo file and is never joined.
- c4-gen's `Component` has no `paths` field yet, so regenerating a model with
  c4-gen drops them; keep `paths` on hand-authored (`authored: true`) models.
  The chai-ledger toy's blueprint lives as a test fixture,
  `tests/cloud/fixtures/chai_ledger_c4.json`.

Live: at PREPARE, before any agent step, the station reads the worktree's
`docs/c4/model.json` (where the run starts: its line, else its base) into the
run's feed. Each `Edit`,
`Write` or `MultiEdit` call a seat makes (develop or fix; review is read-only)
then publishes `file_touched {stage, path, component, tool, call_id}` on the
run stream right after its `tool_start`. The path is the one the station
already made relative, redacted again; a path outside the worktree sends
nothing. `file_touched` counts against the live frame cap like any frame and
is not stored: the stored rows keep the edit call itself, which is what the
blueprint read below joins on a reload.

`GET /api/v1/belt/runs/{id}/blueprint` (`belt.read`, the same tenancy 404 as
`GET /belt/runs/{id}`) returns:

| Field | Shape |
|---|---|
| `action_id` | the run |
| `ref` | where the model was read: a mandate run's line (`belt/line/<id>`) when the local line exists, since the station moves it to the run's start; else `origin/<base>` when that ref exists in the bound repo, else `<base>` (the station's rule, without its fetch); `null` when the repo is outside the allowlist or the base is not a safe ref name |
| `model` | `{scope, model: {people, systems, relationships}}`, the c4-gen model as committed at `ref`, `paths` included; `null` when there is none, it is not a C4 model, or it is over 1 MB |
| `files` | `[{path, component}]`, first touch first: the `Edit`/`Write`/`MultiEdit` calls the develop then fix rows stored, then every file the run's diff writes (`+++ b/` and `diff --git` headers, so a binary patch and a recipe's files count) |

The model is read with `git cat-file blob <sha>:docs/c4/model.json` (fsmonitor
and hooks off; no textconv or filters), never from the owner's working tree,
and nothing from the repo runs. The repo is re-resolved inside
`POCKETPAW_BELT_REPO_ALLOWLIST` on every read (the executor's
`_re_resolve_repo`), and a base that is not a plain ref name (`-` first, `..`,
anything outside `[A-Za-z0-9._/-]`) never reaches git. A mandate run is
queued with its base already set (the repo's checked-out branch, the
station's default), so the blueprint is there from the moment the run is
filed, not only once its diff attaches. The read follows the line's or the
base branch's tip, so a model changed there after the run maps the run's
files by the new model; recording the run's start sha on the blob would pin
it.

### Runs read model

`GET /api/v1/belt/runs` and `GET /api/v1/belt/runs/{id}` rows carry, besides
status, stage and the landing fields: `title` (the task title, `null` on a
hand-driven run), `files_changed` (from the attached diff, replaced by the
staged count on landing), `branch` / `commit_sha` / `pr_url` (a mandate run's
`branch` is its line, `belt/line/<mandate id>`, and `pr_url` the line's one PR;
a hand-driven run's is `feat/belt-<id>`), `redevelop` (how many times the run
went back to the develop station on a moved base or line: 0 or 1), `plan_action_id` / `task_index` (the
mandate plan task it works, `null` on a hand-driven run), and `error`: why the
run failed, which is the executor's reason (`Action.error`) or, for a develop
that failed, the `headless_error`. `null` when nothing failed.

Every run row, on the list and the detail, carries `screenshots: [{name,
bytes}]` (the cloud station's kept snapshot PNGs, `[]` when none); the detail
also returns `tester` (`{session_url, at}`, `{error, at}`, or `null`). `GET /api/v1/belt/runs/{id}/screenshots/{name}`
(`belt.read`, the same tenancy 404) serves one PNG as `image/png` with
`X-Content-Type-Options: nosniff`; a name the run does not list, or one that
resolves anywhere but straight inside the run's dir (a symlink out included),
is a 404.

### The Review Room: try the app, review it

The fast lane merges green PRs with no human, so taste and behaviour are
reviewed after the merge, in paw-enterprise's Review Room. The wire contract is
paw-enterprise `docs/design/belt-review-room.md`; the code is
`belt/review.py` behind `belt/router.py`. Every route takes `belt.read` and the
run routes' tenancy 404; errors carry the cloud envelope plus `detail`.

Run detail rows also carry `issue_url`: the GitHub issue the run works. The
cloud station sets it at launch from the task's `#n` and the repo's slug;
other runs read `null`.

**Try it.** `POST /api/v1/belt/runs/{id}/try` opens the run's CI-built app on
the station's Mac; `GET .../try` polls the `TryState` (`idle`, `preparing`
with a `step`, `opened`, or `failed` with a `reason`). The state is
process-local, so a restart reads `idle`.

- macOS only: elsewhere 501 `belt.try_unsupported`. So is a `gh` that cannot
  read the PR or its CI (not logged in, offline).
- The commits come from the run's PR (`pr_url`, else `cloud.pr`): the merge
  commit when merged, then the PR head. The newest CI run of either with an
  unexpired `app-builds` artifact is found before the 202; none is 409
  `belt.no_artifact`, and so is a run with no PR.
- A try already `preparing` or `opened` is joined (200); a `failed` one is
  replaced (202). `preparing` for more than 10 minutes reads `failed`.
- In the background: `gh run download` into
  `POCKETPAW_FACTORY_APP_DIR/<run id>/artifact/`, then each `*.tar.gz` there
  (creative-apps ships one, `app-builds.tar.gz`, because upload-artifact drops
  the +x bit) is screened with `tarfile` (any absolute or `..` member, link
  leading out of the archive, or device/fifo member refuses the whole archive)
  and unpacked with `tar xzf` into `.../apps/`.
- Which bundle: the only one; else the one the mandate charter's `line:<x>`
  label names (`line:flare` is `FlareApp.app`); else the one the run title
  names. Otherwise nothing opens: the state is `failed`, its reason lists the
  bundles, and `apps` carries their names. `line:suiteui` (the shared UI kit)
  has no app of its own, so it lands here rather than guessing a host app.
- Then `xattr -dr com.apple.quarantine <bundle>` (ad-hoc signed builds; an
  absent attribute is fine) and `open <bundle>`, both through the scrubbed
  runner. `open` failing reads `failed` with "Gatekeeper refused the app" or
  "The app did not launch".

**Reviews.** `POST /api/v1/belt/runs/{id}/reviews` takes multipart `verdict`
(`looks_good` / `needs_changes`), `notes` (JSON `[{t_ms, text}]`, at most 200,
`t_ms` 0 to 7,200,000, text trimmed to 1 to 2000 chars, at least one for
`needs_changes`), an optional `video` (webm) and up to 60 `frames`, each
`<t_ms>.png` of a note.

- Limits are enforced while the body streams (`_ReviewParser`, a subclass of
  Starlette's parser): the video at 200 MB, a frame at 5 MB, the 61st frame,
  and the whole body at 512 MB (a declared `Content-Length` over it is refused
  before reading). Each is 413 `belt.review_too_large`. The global request
  ceiling (`security/body_limit.py`, ~1.27 GB by default) sits above it.
- A video must be declared `video/webm` and start with the EBML magic; a frame
  `image/png` and the PNG magic; else 415 `belt.review_bad_media`. Anything
  else malformed is 400 `belt.review_invalid`.
- Media land under `POCKETPAW_FACTORY_REVIEW_DIR/<run id>/<review id>/`
  (`video.webm`, `<t_ms>.png`). The record goes on the run blob's `reviews`:
  `{id, by, verdict, notes, video, frames, issue_url, issue_error, at, key}`,
  where `video` is a flag, `frames` the `t_ms` list and `key` the request's
  `Idempotency-Key`. A repeat with a stored key returns 200 with that review
  and does nothing else.
- `by` is `user:<email>` (the user id when there is no email). Cloud routes
  have no agent identity today, so `agent:<name>` is never minted.
- `needs_changes` then files an issue on the run's GitHub repo: title
  `Review: <run title> needs changes`; labels `review`, `gap`, `bug` and the
  line's (the charter's `issue_labels`, which is what the `issues` patrol
  watches, else the source issue's `line:*` labels), each created first; body
  with the reviewer's display name (never the email), the time, a link to
  `<POCKETPAW_FRONTEND_BASE_URL>/belt-next/runs/<id>/review`, the PR and
  commit, and each note as `**mm:ss** text` followed by its frame. `gh` cannot
  attach images, so frames are committed with `gh api` contents PUT to the
  repo's `belt-review-assets` branch (created off the default branch the first
  time) under `reviews/<review id>/<t_ms>.png` and linked by
  `https://github.com/<slug>/raw/belt-review-assets/...`, which renders for
  anyone who can read the repo. A failed upload leaves the frames out; a
  failed issue keeps the review with `issue_url: null` and `issue_error` set.
- `GET .../reviews` lists them newest first in the contract's `Review` shape
  (`video_url` and `frames` are API paths). `GET .../reviews/{review id}/video`
  (`video/webm`, `Range` answers 206) and `.../frames/{t_ms}.png` serve the
  media with `Cache-Control: private, max-age=31536000, immutable`, only for a
  review and file the blob lists, through the screenshot route's guard
  (`service.kept_file`: straight inside the dir, no symlink out).
- Not built: the optional `belt_run_review` bus event; the client refetches on
  open and after its own submit.

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
  Mongo URI, tokens, API keys and `POCKETPAW_*` secrets never reach it. The one
  exception is the claude CLI itself, which also gets `ANTHROPIC_API_KEY` and
  `CLAUDE_CONFIG_DIR` when set: a hosted factory serving other users must run
  claude on an API key (subscription OAuth is for the owner's own use), and
  checks never see either.
- **Programs.** Check and recipe argv[0] must be on
  `POCKETPAW_FACTORY_ALLOWED_COMMANDS`, enforced at create (422) and again
  before exec. No shells, `env`, `sudo`, downloaders, `git`, or relative paths.
- **Claude seats.** In the strict setup every call (foreman, develop, fix,
  review) loads no settings files (`--setting-sources ""`), no MCP servers
  (`--strict-mcp-config`) and no hooks (`disableAllHooks`), so a `.claude/` or
  `.mcp.json` planted in the worktree never loads. In the owner setup the
  develop, fix and review calls load the owner's config, and the trust restore
  puts the worktree's agent config back to the base commit (never the line)
  before each call;
  the foreman and autopilot stay strict. Auth is the CLI's
  keychain/OAuth login; never `--bare`, which forces API-key auth. Tools are
  limited with `--tools` and the allow rules are scoped to the worktree
  (`Read(./**)`, `Edit(./**)`, `Write(./**)`, plus `Bash(<check>:*)` on the
  edit seats); WebFetch, WebSearch and Task are denied. The foreman and the
  autopilot personas get no tools at all and an empty temp dir as their cwd.
- **Git.** Station git calls run with `core.fsmonitor=false` and
  `core.hooksPath=/dev/null`; the worktree `.git` file is snapshotted and
  re-checked after every agent step.
- **The diff.** Refused if it touches agent config (`.claude/`, `.mcp.json`,
  CLAUDE.md, CLAUDE.local.md, AGENTS.md), `.git` or `.gitmodules` at any
  depth and in any letter case, or if an added line matches a `security.redact` credential pattern
  (the error never echoes the value).
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
- Strict setup: the worktree's `CLAUDE.md` files can still load, so text
  written in DEVELOP can steer FIX and REVIEW within that run. That is not host
  exec, and DONE refuses the hunk, so it never reaches the line. The owner
  setup's trust restore closes it inside the run too.
- Owner setup: the owner's hooks, MCP servers and permission settings apply to
  every develop, fix and review call. User-level Stop hooks may fire on each
  call (session-log noise, rebuild triggers); whether workspace-level hooks
  load for a nested worktree has not been checked. The tool surface stays
  `--tools`-limited, but MCP tools the owner's settings allow are reachable.
  Use it only on the owner's own machine.
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
             backlog:   {count, top: [{title, severity, patrol,
                                       in_flight}]},     # open, any age, top 5
             shifts: [{no, state, outcome, task_count}],             # since
             runs:   [{action_id, status, title, pr_url, branch,
                       commit_sha, headless_error, headless_state,
                       error}],                                      # since
             gates:  {plans: [{shift_no, plan_action_id, task_count}],
                      diffs: [<run row>]},                           # any age
             stuck:  [<run row>]}],      # queued with headless_error or a
                                         # leftover headless_state, any age
 totals: {mandates, new_sightings, shifts, runs, landed, failed, gates_waiting,
          open_backlog}}
```

Gates, stuck runs and the backlog are listed whatever their age: an in-gate
plan, a diff at `proposed`, or a headless develop from last week that failed or
never finished still needs a human, and an open sighting is still waiting. The
backlog is the foreman's (same `_backlog` read, same order), so the report and
the next shift agree on what is open; the digest never writes resolution. The digest is composed from the existing
reads (`list_mandates`, `get_mandate`, `shift_wire`, `list_sightings`, the belt
runs list), so it cannot disagree with the console; shifts come from the
detail's 10 most recent.

`scripts/factory_digest.py` prints it as markdown (a mandates table with a
Backlog count, then *Needs you*, *Failures* with each run's `error` or
`headless_error`, *Landed*, *Backlog* with the top open items, in flight
marked). Stdlib only; the token comes from
`--token-file` or `PAW_TOKEN` and is never printed:

```bash
uv run python scripts/factory_digest.py --base http://localhost:8893 \
    --token-file ~/.paw/token [--since 2026-10-05T06:00:00+00:00]
```

## Environment

| Variable | Default | What |
|---|---|---|
| `POCKETPAW_MANDATE_DISPATCHER` | `station` | `station` / `headless` / `bus` (above) |
| `POCKETPAW_FACTORY_DEVELOP` | unset | `claude` wires the develop station (needs `POCKETPAW_MANDATE_DISPATCHER=headless`); `cloud` wires the cloud develop station instead (see *The cloud develop station*) |
| `POCKETPAW_FACTORY_CLOUD_LAUNCH_DIR` | unset | Cloud mode only, and required there: an existing git clone, already trusted by the claude CLI, that cloud sessions launch from; its origin is re-pointed to each bound repo per launch |
| `POCKETPAW_FACTORY_CLOUD_PR_LABEL` | `fast-lane` | The label the cloud brief tells the session to put on its PR |
| `POCKETPAW_FACTORY_CLOUD_POLL` | `60` | Seconds between `gh` polls of a cloud run's PR |
| `POCKETPAW_FACTORY_CLOUD_TIMEOUT` | `14400` | Seconds a cloud run waits for its PR to merge or be refused before it fails back to queued |
| `POCKETPAW_FACTORY_CLOUD_FIX_GRACE` | `300` | Seconds a cloud run's PR must stay conflicting or red before a fixer session launches |
| `POCKETPAW_FACTORY_CLOUD_FIX_ATTEMPTS` | `2` | Fixer sessions per cloud run; past it a still-stuck PR escalates to the per-diff gate |
| `POCKETPAW_FACTORY_CLOUD_LANE_NUDGE` | `300` | Seconds a cloud run's labelled PR must stay green and mergeable but unmerged before the tracker dispatches the lane workflow; also the minimum gap between dispatches per repo |
| `POCKETPAW_FACTORY_CLOUD_LANE_WORKFLOW` | `Fast Lane` | The workflow the lane nudge runs with `gh workflow run` (it needs a `workflow_dispatch` trigger) |
| `POCKETPAW_FACTORY_CLOUD_TESTER` | off | `1` (or `true`/`yes`/`on`) launches one tester cloud session per landed cloud run on a mandate with a `tester` crew seat, and tracks its PR |
| `POCKETPAW_FACTORY_CLOUD_NO_PR_TIMEOUT` | `3600` | Seconds after a cloud run's newest launch with no `Belt-Run` PR before the tracker relaunches a worker or releases (rejects) the run; for a tester, the seconds after which no `Belt-Test` PR means it is done |
| `POCKETPAW_FACTORY_CLOUD_RELAUNCHES` | `1` | Fresh workers launched per cloud run that opened no PR; past it the run is released (rejected) so the mandate's line moves |
| `POCKETPAW_FACTORY_SCREENSHOT_DIR` | `~/.pocketpaw/belt/screenshots` | Where the cloud station keeps each run's snapshot PNGs, one `<run id>/` dir per run |
| `POCKETPAW_FACTORY_APP_DIR` | `~/.pocketpaw/belt/apps` | Where a Review Room try downloads and unpacks a run's CI app build, one `<run id>/` dir per run (replaced by each new try) |
| `POCKETPAW_FACTORY_REVIEW_DIR` | `~/.pocketpaw/belt/reviews` | Where Review Room reviews keep their video and frames, `<run id>/<review id>/` |
| `POCKETPAW_FRONTEND_BASE_URL` | `http://localhost:1420` | The app origin a review issue links back to (`/belt-next/runs/<id>/review`) |
| `POCKETPAW_FACTORY_DEDICATED_HOST` | unset | `1` lets the station wire in a process serving cloud tenants; set it only on a dedicated single-tenant host |
| `POCKETPAW_FACTORY_ALLOWED_COMMANDS` | `uv,uvx,bun,bunx,belt,node,npm,pnpm,python,python3,pytest,cargo,make,go` | Comma-separated program basenames a charter check or recipe may start |
| `POCKETPAW_BELT_REPO_ALLOWLIST` | empty | JSON list of repo roots; the develop station refuses to run while it is empty |
| `POCKETPAW_FACTORY_CLAUDE_BIN` | `claude` on PATH | The Claude Code CLI every factory LLM seat shells (foreman, develop, fix, review) |
| `POCKETPAW_FACTORY_CLAUDE_MODEL` | the CLI's built-in default | Passed as `--model` when set (in the strict setup user settings don't load, so a model set there is ignored); a seated crew dev's own model wins on develop/fix |
| `POCKETPAW_FACTORY_CLAUDE_SETUP` | `strict` | `owner` runs develop/fix/review with the owner's Claude Code setup (CLAUDE.md, skills, hooks, settings, MCP) plus the trust restore; anything else is `strict`; a crew seat's `setup` wins |
| `POCKETPAW_FACTORY_WORKTREE_ROOT` | unset | Owner setup only, and required there: existing dir outside the bound repo that station worktrees are created under, e.g. `<workspace>/paw-worktrees/factory-runs` |
| `POCKETPAW_FACTORY_LOOM_DIR` | nearest ancestor `.loom/` of the bound repo | Where ORIENT looks for `worldmodel-<repo dir name>.json` |
| `POCKETPAW_FACTORY_LOOM_BIN` | `loom` on PATH, else `~/go/bin/loom` | The loom CLI ORIENT runs |
| `POCKETPAW_FACTORY_DEVELOP_TIMEOUT` | `900` | Seconds per `claude -p` call (develop, fix, review) |
| `POCKETPAW_FACTORY_CHECK_TIMEOUT` | `600` | Seconds per check or recipe command |
| `POCKETPAW_MANDATE_LLM` | `claude` | Foreman / autopilot transport: `claude` or `mock` |
| `POCKETPAW_MANDATE_FOREMAN_TIMEOUT` | `300` | Seconds the foreman's `claude -p` call may take before it fails and finishes its shift |
| `POCKETPAW_MANDATE_SCHEDULER_INTERVAL` | `3600` | Cadence sweep interval, seconds |
| `POCKETPAW_MANDATE_SCHEDULER_CONTINUOUS_INTERVAL` | `60` | Sweep interval while any active `continuous` mandate exists, seconds |
| `POCKETPAW_MANDATE_SCHEDULER_ENABLED` | off | `true` starts the cadence scheduler alone, without the other background loops |
| `POCKETPAW_MANDATE_CONTINUOUS_MIN_GAP_MINUTES` | `5` | Minimum time between two shifts of a `continuous` mandate |
| `POCKETPAW_MANDATE_CONTINUOUS_STALL_HOURS` | `3` | Age after which a `continuous` mandate's last shift stops holding the next one (in-flight runs, a stuck `planning`/`executing` state; never `in_gate`) |
| `POCKETPAW_BELT_PLAN_GATE` | `manual` | `auto` runs the auto plan gate (needs Clef on) |
| `POCKETPAW_BELT_PLAN_GATE_CLEF_TIMEOUT` | `45` | Seconds the auto gate waits for Clef's plan score |
| `POCKETPAW_BELT_POLICY_MIN_IN_SCOPE` | `0.80` | Auto gate: lowest Clef `in_scope` that approves |
| `POCKETPAW_BELT_POLICY_MIN_LOW_RISK` | `0.75` | Auto gate: lowest Clef `low_risk` that approves |
| `POCKETPAW_BELT_POLICY_MIN_INDEPENDENT` | `0.40` | Auto gate: below this `tasks_independent` the plan escalates |
| `POCKETPAW_BELT_POLICY_MAX_TOO_BIG` | `0.65` | Auto gate: above this `too_big` the plan escalates |
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

On the owner's own machine, add `POCKETPAW_FACTORY_CLAUDE_SETUP=owner` and
`POCKETPAW_FACTORY_WORKTREE_ROOT=<workspace>/paw-worktrees/factory-runs` (create
the directory first) so the factory codes with the workspace CLAUDE.md, the repo
CLAUDE.md and the workspace skills. Keep the default `strict` on hosted deploys.

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
(sightings, backlog, shifts, runs, waiting gates, totals, the `since` window,
tenant scope; it also renders `scripts/factory_digest.py` against the real wire
shape). The backlog tests replay the live run's gap through the real station
dispatcher: unaddressed and failed sightings carry into the next shift's
prompt, a landed task resolves (and persists) its sightings, a failed task's
reason shows in the history, in-flight runs and an in-gate plan are flagged,
a gate rejection lands in history not backlog, and the 30 cap.

`tests/cloud/test_belt_github_issues_patrol.py` runs the GitHub `issues`
patrol with a fake `gh`: label matching, severity, no labels, the GitLab
fallback, unsafe slugs, gh failure, the cross-patrol dedup through
`run_patrols`, and the continuous due check sensing first.
`tests/cloud/test_belt_queued_run_approve.py` pins that no human approve path
(Mission Control bulk, instinct bulk, single) approves or fails a queued run.
`tests/cloud/test_belt_cloud_station.py` also covers the tester and the
screenshots, `tests/cloud/test_belt_console.py` the screenshot route, and
`tests/mutations/belt_crew_testers.json` holds their mutations.
`tests/cloud/test_belt_review_room.py` covers the Review Room (try on and off
a Mac, artifact fallback, ambiguous apps, tar traversal, review limits and
media, the issue's labels, idempotency, tenancy); `tests/mutations/belt_review_room.json`
holds its mutations.
`tests/cloud/test_belt_upstream_patrol.py` runs the upstream patrol against a
tmp repo's Cargo.toml with a fake `gh`: summary and area sightings, the
severity scale, every failure path as one severity-1 sighting, DTO validation,
and dedup on the upstream head through `run_patrols`.
`tests/cloud/test_belt_develop_station.py` drives the develop station against a
real tmp git repo with real check commands and a faked `claude`, including the
hardening: claude argv flags, the scrubbed env, refused programs, the
multi-tenant wiring refusal, `.git` tampering, protected paths, secret diffs,
redaction, untrusted fencing, the injection screen, process-group kills and
logged background crashes. It also pins the owner setup (no isolation flags,
the tool rules kept, the worktree under the root, strict unchanged, the
worktree-root refusal) and the trust restore (planted `.claude/settings.json`,
`CLAUDE.md`, nested and gitignored plants, a symlinked `.claude` and an
untracked `.mcp.json` are all back to base before FIX and REVIEW; on a
mandate's line the seats get the base's CLAUDE.md, never the line's, and a line
whose agent config differs fails at DONE), DONE refusing CLAUDE.md, AGENTS.md
and CLAUDE.local.md diffs like `.claude/` and `.mcp.json`, ORIENT (the
loom argv, the block in the develop and review prompts, the review's duplicate
rule, the C4 fallback, the "no world model" note, recipes skipping it), the
foreman's C4 list (`test_belt_mandates.py` checks the shift wires it in) and the
Pulley app line (its `belt` recipes and both checks, the frozen `bun install`
and doctor, pass the default allowlist; a faked `belt` and `bun` land
`add-auth` as a diff with ORIENT and REVIEW skipped, a red frozen install or a
red doctor fails CHECK with no FIX, and a second `add-auth` ends as an empty
diff);
`test_belt_pulley_c4.py` maps the seven real Pulley manifests through
`block_component` and reads the result back with `c4_lines`;
`test_belt_headless.py` and `test_belt_scheduler.py` cover the runner and the
cadence scheduler; `test_belt_continuous_cadence.py` pins each `continuous` due
rule and the loop's tick interval; `test_belt_plan_policy.py` pins the auto plan
gate against the pilot's gate log and drives it through the real trigger, store
and executor (approve as `system:belt-policy` with one chain, escalate on a Clef
outage or low independence, unchanged with the flag off); the headless file also lands a run (commit subject from the
title) and drives the re-develop against a real tmp repo: two diffs from one
base, the first landed and merged, the second re-developed once and back at the
gate, a second conflict failing with "base moved twice", and no develop loop
failing with its reason. `tests/cloud/test_belt_feed.py` drives a queued run
through the real runner and station with a `claude` that prints captured-shape
stream-json: steps stored in order and served by the feed route, prose blocks
a blank line apart, relative paths (the run's temp dir reached through a
symlink whose physical path ends with it, and 24 such dirs for the prefix
order), secrets planted in a tool result and a tool input absent from storage
and the response, the result envelope read past the trailing system line, a
failed develop still storing its feed and recording claude's words (stderr or
its last prose) with no stream-json and no worktree path, a timed-out
re-develop replacing the earlier feed, parallel same-name calls answered out
of order pairing by id, a stream cut after `system/init` not read as a result,
a failing save not failing the run, the step and byte caps (by id too), row
labels with their verbs, and the route's tenancy 404.
`tests/cloud/test_belt_live_feed.py` pins the live half: the runner handing
lines over before the process exits (a 200 KB line and a 300 KB stdin
included) and a timeout returning no stdout; a run whose first develop fails
its check publishing orient, develop, check, fix, check and review in order
with every frame stage-tagged, scrubbed of secrets, paths and the host
account, a row per stage, and the same frames replayed by the route; a recipe
run's two stages; a failed run's `stream_end {ok: false}`; a broken transport
not failing the run; the live frame cap; and the route's newest-attempt
replay, cursor resume, `from_history`, wait-while-developing and tenancy 404.
`tests/cloud/test_belt_live_atlas.py` pins the blueprint: glob specificity and
segment rules, path normalisation, the chai-ledger fixture's files, a real
station run publishing `file_touched` after each edit with its component (and
`null` with no model), the scrub and skips on `file_touched`, and the route
reading the committed model (not the working tree), joining stored edits and
diff files in order, 404ing a foreign run, refusing a repo outside the
allowlist and never handing git an option-shaped base; a model that is
missing, not text or not C4 never fails the run
(`tests/mutations/belt_live_atlas.json`, 20 mutations).
`tests/cloud/test_belt_line.py` drives the line on real tmp repos (a bare
repo as origin, charter recipes as the develop work, a fake PR opener): two
runs of one mandate stack on the line with no re-added lines; a merged line
moves to the base; base commits the line lacks are merged in; a base conflict
stands the run down with one sighting and leaves the line alone; a line moved
between read and swap re-develops and keeps the other landing; the line is
pushed and one PR is reused; a line pushed ahead on origin is where the next
run starts; a PR failure after the swap keeps the landing; a run with no
mandate keeps its own branch (a PR failure fails it and deletes the branch);
the ref move is a compare-and-swap; the line name only comes from a real
mandate id; `line_status` reads ahead, merged and subjects from git (a base
commit the line lacks never counts), looks up the open PR only with an origin
while unmerged, and reads exists=false on a missing repo, a non-mandate id or a
timed-out git. `test_belt_mandates.py` checks the Foreman reads line work as
built, awaiting merge then merged, with THE LINE and the detail's `line` block
from git (a commit no run recorded shows too), a repo with no line reading
exists=false, and that a station sighting files once. `test_belt_console.py`
checks a landing's `belt_run_updated` names its branch.
`tests/mutations/belt_factory_runs.json` breaks each of these on purpose (the
`feed:` entries for the feed, the `line:` ones for the line). CI runs these in
the "Belt mandates and the craft factory develop station" step (`tests/cloud`
is outside the default addopts).

`tests/cloud/test_belt_crew.py` pins the crew: the roster through create, the
crew route, GET and a reload; the 422s (another workspace's agent, public or
not, someone's private agent, duplicates, bad role / concurrency / setup) and
the cross-tenant 404; per-seat vouching (another admin edits a roster holding a
private agent of the first admin's and a deleted one; a new unreadable or
disabled agent is still refused); the seat rule (two devs, two tasks, two workers;
reviewers and disabled agents skipped); the station running a worker's model
on develop and fix but not review, its instructions in those prompts, catalog
model ids mapped and non-claude or flag-shaped ones falling back to the env, a
seat's setup over the env's (and an owner seat with no worktree root failing
PREPARE with an error that names the seat); and end to end, roster → dispatch
→ runner → station argv, with the agent edited between dispatch and develop,
no crew and a disabled agent falling back to the env, the instructions fenced
as untrusted data (any case or spacing of the closing tag defanged), and
an agent the seating admin can no longer read becoming a gone seat.

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
