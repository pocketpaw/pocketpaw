# Mission Control — user flows

*Companion to the architecture spec and the backend audit. Concrete end-to-end journeys through Mission Control, written so Rohit (or any new joiner) can ground every UI choice in a real operator's day. Each flow names the primitives touched, the data path, and what success looks like.*

*Persona: **Shawn**, CTO/operator at a 25-person events-production company, $6–10M/year. Uses Mission Control daily. Co-operator: **Jess**, the ops manager — has 7 years of institutional knowledge, less computer-time than Shawn, will adopt or sabotage the tool.*

---

## The seven flows that matter

In rough order of frequency:

1. [Monday-morning sweep](#1-monday-morning-sweep) — most common, daily
2. [Approving a Nudge mid-day](#2-approving-a-nudge-mid-day) — multiple times daily
3. [Delegating new work to an agent](#3-delegating-new-work-to-an-agent) — daily to weekly
4. [Bulk-approving a batch](#4-bulk-approving-a-batch) — weekly
5. [Multi-coordinator handoff](#5-multi-coordinator-handoff) — weekly
6. [Investigating a Pawprint](#6-investigating-a-pawprint) — when something complaints
7. [Cycle + analytics review](#7-cycle--analytics-review) — Friday afternoon, weekly

Three of these (1, 2, 4) carry 80% of the daily-use weight. The others surface when the operator needs them. Build for the 80% first.

---

## 1. Monday-morning sweep

**Who · When.** Shawn, 8:45 AM Monday. Coffee in hand. Wants to know what changed over the weekend, what needs his eyes, anything broken.

**Trigger.** Opens Mission Control via the desktop client (paw-enterprise → `/mission-control`).

**Screen state on arrival.**

- Toolbar: `Mission Control` / search input (empty) / refresh.
- Tab bar: **Feed** active. Pending badge on Feed shows `4`.
- Outcomes strip: `47 shipped · 4 pending · 2 reverted · 1 failed` (24h window).
- WorkFeed sections, expanded by default:
  - **The Tray (4)** — *needs your eyes*
  - **Agents in flight (5)** — *autonomous work*
  - **Delegated (2)** — *with another human (Jess)*
  - **Pawprints (47)** — collapsed by default, *shipped today*
  - **Snags (1)** — *needs attention*
- Right rail: assistant sidebar with suggestion chips.
- Status bar: live activity ticker `● 10:42 sales-agent · Reading HubSpot deals`.

**What Shawn does.**

1. Glances at Outcomes strip. 47 shipped weekend → fine. 1 failed → flagged but not urgent yet.
2. Scrolls to **Snags (1)**. Sees `Stripe refund retry — gateway timeout`. Clicks → detail panel slides in.
3. Reads the timeline. Sees finance-agent tried twice, both 504. The summary explains contract clause 4.2 was honored, refund is valid. Decides: retry manually.
4. Closes detail (Esc). Opens **The Tray**. Reviews 4 Nudges. Approves the Stripe follow-up and the Greenline vendor confirm (separate clicks, ~10s each). Snoozes the HubSpot deal stage update — not urgent today.
5. Notices Tray badge drops from 4 → 1. Pending count in status bar updates live.
6. Glances at **Agents in flight (5)**. sales-agent is drafting Bluefin contract, events-agent is reading venue calendar. All healthy.
7. Total time: ~3 minutes. Mission Control closes; the desktop client switches to chat.

**Primitives touched.** Instinct (Tray approvals → Pawprints) · Soul (agent context surfaced in detail) · Pockets (sectioned by source pocket) · Fabric (the Bluefin Customer object referenced in the Stripe follow-up).

**Success state.** Every weekend agent action accounted for; everything pending is either approved, rejected, snoozed, or deliberately deferred. Mission Control screen takes 3 minutes, not 30.

**Failure modes to guard against.**

- The Snag should never be buried below collapsed sections. **Snags is expanded by default.**
- Tray items missing summaries make a single approval take 30s instead of 5s. Mock data already enforces summaries; backend write path must too.
- If overnight activity is voluminous (>50 items), Pawprints section opens collapsed so the screen isn't overwhelming.

---

## 2. Approving a Nudge mid-day

**Who · When.** Shawn, mid-day. Mission Control is in a background window or pinned tab.

**Trigger.** Tray-bound Nudge fires — sales-agent drafted an email reply to a $40K-deal lead. Notification surfaces via the existing notification surface (`ee/cloud/notifications/`).

**Screen state.**

- Live ticker in the status bar flashes the new event.
- Tab badge on `Feed` increments from `0` → `1`.
- New row appears at the top of **The Tray**, with a subtle highlight animation (no row reorder shuffle below it).

**What Shawn does.**

1. Switches windows. Clicks the row.
2. **Detail panel slides in.** Shows: status pill `awaiting approval`, title, full summary (the email draft + reasoning), meta (assignee = shawn, source = `@sales-agent`, pocket = Sales pipeline, priority = high), full activity timeline (the agent's tool calls leading to this proposal), Instinct callout: *"Routed to you because the proposing agent's policy requires human approval for this kind of action."*
3. Reads the draft. Edits one sentence inline (future feature — for v0.5, Reject + re-prompt via the assistant sidebar covers it).
4. Clicks **Approve**. The detail panel closes, the row moves from The Tray to Pawprints, the Tray badge decrements, a confirmation toast surfaces briefly.

**Primitives.** Instinct (propose → approve → Pawprint) · Notifications (mid-day surface) · Soul (sales-agent's context in the timeline).

**Backend round-trips.**
- `POST /mission-control/items/bulk-approve` with `{ids: [id]}` (single-item via the bulk endpoint — same contract).
- Server emits `work_item.resolved` SSE → frontend moves row + updates count.

**Success state.** Approval takes <15 seconds from notification to row-moved. No more than one click between "row" and "approve" once intent is formed.

---

## 3. Delegating new work to an agent

**Who · When.** Shawn, Tuesday morning. After his Monday sweep he wants events-agent to handle the May 23 wedding's day-of run-of-show drafting.

**Trigger.** Has the thought "events-agent should draft the run-of-show." Opens Mission Control.

**Screen state.**

- WorkFeed visible. Shawn hits `⌘N` (keyboard hint visible in left rail).
- Inline create form slides into the feed header.

**What Shawn does.**

1. Types in the title input: `Draft the May 23 wedding run-of-show, format like the Stripe summit one, ping me when done`.
2. Assignee dropdown: defaults to last-used. Picks `events-agent`.
3. Hits Enter.
4. Form collapses. New row appears at the top of **Agents in flight** with status `proposed`. Sub-second.
5. events-agent's runtime polls its task queue (or receives `work_item.proposed` SSE), claims the task. Status flips to `in_progress`. Status icon shifts from open-circle to dashed-circle in the row.
6. Activity ticker reflects new work: `● events-agent · Claimed task w_local_…`
7. Shawn closes Mission Control. Hours later, when events-agent completes the draft, a new Nudge appears in The Tray: `Run-of-show draft ready — review`. Click → detail panel shows the full draft. Approve → moves to Pawprints.

**Primitives.** Tasks entity (create → claim → complete) · Instinct (the optional review step at end) · Soul (events-agent picks up the task via its loop).

**Backend round-trips.**
- `POST /tasks` from the create form.
- Server emits `task.proposed` → events-agent runtime receives it.
- Agent calls `POST /tasks/{id}/claim`. Status updates.
- Agent works. Tool calls stream as `activity.recorded` events bound to task id.
- Agent calls `POST /tasks/{id}/complete` with a `next_action: 'request_approval'` flag → server transitions item to `awaiting_approval`, routes to creator (Shawn) as a Nudge.

**Success state.** Shawn types one sentence and hits Enter. Agent picks it up. Hours later he reviews + ships. He never opened the events Pocket directly to wire this up.

**Failure modes.**
- Agent fails to claim within N minutes → task stays in `proposed`, surfaces in **Snags** with `unclaimed` reason after a configurable timeout.
- Agent gets stuck mid-task → status moves to `blocked`, surfaces as a Snag with the blocker reason in the summary.

---

## 4. Bulk-approving a batch

**Who · When.** Shawn, Wednesday afternoon. Five vendor confirmations are pending — all events-agent proposed, all low-stakes formulaic emails to known vendors.

**Trigger.** Shawn opens Mission Control. Sees **The Tray (5)** in the badge.

**What Shawn does.**

1. Clicks the **Tray** filter pill in the WorkFeed header. View narrows to just the Tray items.
2. Hovers the first row — checkbox appears in the leading position.
3. Clicks the checkbox on the top row. The whole feed shifts into select-mode (all rows show checkboxes).
4. Shift-clicks the bottom row of the 5 vendor confirms. Range selected; the row count badge appears.
5. Sticky bulk-action bar slides into view: `5 selected · Approve · Reject · Snooze 24h · (clear)`.
6. Clicks **Approve**. Bar disappears, rows animate out of The Tray, badge resets to 0.
7. Pawprints count jumps by 5 (visible in the status bar / Outcomes strip).

**Primitives.** Instinct bulk approve (single audit transaction with shared `bulk_id`) · Notifications fan out per item.

**Backend round-trip.**
- One `POST /mission-control/items/bulk-approve` with `{ids: [5 ids]}`.
- Server creates 5 Pawprints (each carrying the shared `bulk_id` so the bulk action is queryable).
- Single SSE batch emits `work_item.resolved` per item.

**Success state.** Five approvals in ~10 seconds (one shift-click + one button) instead of five sequential row-click + approve cycles. The audit trail captures the bulk operation atomically.

---

## 5. Multi-coordinator handoff

**Who · When.** Friday, mid-afternoon. Shawn is heading out for a venue walkthrough. Notices three Tray items he can't get to. Wants Jess to handle them.

**What Shawn does.**

1. Filter pill: **Tray**. Selects all 3 items via checkboxes.
2. Bulk action bar: clicks `…` (more) → `Reassign to…` dropdown. Picks `jess`.
3. Items vanish from Shawn's **Tray** and appear in his **Delegated** section (now showing `5 · with another human`).
4. Jess opens her Mission Control 30 minutes later. Her notification surface has 3 pending items. Her **Tray** shows them at the top. She handles them.
5. Each approval Jess makes creates a Pawprint tagged with her actor name. Visible from Shawn's view in **Pawprints** with `@jess · Approved vendor confirm…`.

**Primitives.** Tasks (reassign endpoint) · Instinct (Pawprint with delegated-by reference) · Notifications (Jess gets pinged).

**Success state.** Three reassignments in <30 seconds. The handoff carries context — Jess sees the full agent-side reasoning timeline when she opens each item. She isn't operating blind.

**Failure modes.**

- Jess refuses an item (not her scope) → she rejects with reason → item moves to Snags with `rejected by delegate` status, surfaces back to Shawn as a Snag.
- Item has Fabric refs Jess doesn't have access to → access-control on Fabric kicks in. The detail panel shows a degraded view (title + summary, no typed-object preview). Defer the policy details to v1.5.

---

## 6. Investigating a Pawprint

**Who · When.** Tuesday. A customer (Bluefin) calls Shawn saying "we got the wrong invoice — your AI sent the May invoice with the wrong amount."

**Trigger.** Customer complaint. Shawn opens Mission Control.

**What Shawn does.**

1. In the toolbar search, types `Bluefin invoice`. Feed filters live to matching items.
2. Sees ~6 matching items in Pawprints (recent invoice-related actions). Scans by timestamp.
3. Finds `Email sent → Bluefin (May invoice)`. Clicks → detail panel.
4. Reads the full timeline: finance-agent ran the invoicing query at 10:42, generated the email, the *Instinct gate auto-approved it* per policy (low-stakes routine email under $1k threshold), sent at 10:43.
5. The Pawprint detail surfaces the Fabric reference: invoice object id, line items, total. Shawn sees the amount was wrong — agent pulled stale pricing from a cached Fabric snapshot.
6. Shawn captures the bug → creates a new Task assigned to himself: `Audit finance-agent's pricing source; cached Fabric snapshot stale`.
7. Sends an apology + correction to Bluefin manually.

**Primitives.** Instinct audit query (existing endpoint) · Fabric provenance · Soul (the agent's reasoning at the time, replayable from the timeline) · Tasks (creating the follow-up).

**Backend round-trips.**
- `GET /mission-control/items?q=bluefin invoice` → 6 results.
- `GET /mission-control/items/{id}` → full Pawprint with reasoning trace.
- `GET /fabric/objects/{invoice_id}` → the Fabric object referenced.
- `POST /tasks` → the follow-up Task.

**Success state.** Customer complaint → root cause identified in <2 minutes. The full provenance chain is intact: who proposed, who approved (Instinct policy), what data was read, what was sent, when. No archeology across 4 tools.

This flow is the C-suite-buyer-closer demo. Every enterprise buyer evaluating an agentic platform asks "what happens when the agent gets it wrong?" Mission Control's answer is this flow.

---

## 7. Cycle + analytics review

**Who · When.** Friday, 4 PM. Shawn prepping for the Monday operations call.

**What Shawn does.**

**Cycle review.**

1. Clicks **Cycles** tab.
2. Left list shows 4 cycles. Selects `Crestline · May 23 Wedding` (active).
3. Detail panel: header with `Current` pill, date range, scope/started/completed metrics, the burnup chart.
4. Reads the chart: completed line (blue) is slightly below the dashed ideal target. Started line (yellow) is well above completed → some accumulated WIP.
5. Scrolls to the items list. Filters mentally — sees 2 items overdue (due-date passed, still in-progress). Identifies blockers.
6. Captures one bullet for Monday call: "Crestline at 63% complete, slightly behind ideal pace, watch the venue-walkthrough Task."

**Analytics review.**

1. Clicks **Analytics** tab.
2. Reads the 4-stat band: 47 shipped (24h), 96% approval rate, p50 4m12s, p90 38m.
3. Glances at by-agent split — notices `finance-agent` has higher revert rate (5 of 39 = 13%) than the others.
4. Captures another bullet: "finance-agent revert rate trending up — investigate next week."

**Primitives.** Cycles entity (daily snapshots feed the burnup) · Instinct outcome telemetry (drives the analytics) · Tasks (item list scoped to cycle).

**Backend round-trips.**
- `GET /cycles` → list.
- `GET /cycles/{id}` → cycle + daily series.
- `GET /cycles/{id}/items` → items in that cycle.
- `GET /mission-control/outcomes?window=7d` → analytics card data.

**Success state.** Two bullets in 5 minutes from a single screen, ready for Monday's standup. No spreadsheet export, no manual filtering across pockets.

---

## Cross-cutting interactions

These show up across multiple flows; documenting once to avoid repetition.

### The assistant sidebar (right rail)

Always present. Three behaviors:
- **Idle:** shows the 4 suggestion chips (`What needs my eyes?`, `Filter the Tray`, `Create a task`, `Summarize today`).
- **Active conversation:** chat-style messages between the operator and the assistant. The assistant's responses can include shortcuts that, when clicked, set filters or open detail panels in the main canvas.
- **Mode shift in v1:** when an item is selected, the assistant context-switches to "talk about this item" mode. Suggestions become `Explain the agent's reasoning`, `Suggest an alternative`, `Find similar past Pawprints`.

### The keyboard shortcuts

Surfaced in the left rail. Most-used to least:

| Shortcut | Action |
|---|---|
| `⌘K` | Focus the search input |
| `⌘N` | New work item (create form) |
| `shift-click` | Range-select rows |
| `⌘A` | Select all visible rows (future) |
| `esc` | Close detail · clear filters · cancel create — in that order |
| `1` – `5` | Jump to filter pill (future) |
| `j` / `k` | Move row selection (future, Linear-style) |

For v0.5, ship the first four. The others are v1.

### Notifications fan-out

Mission Control creates entries in the existing notification surface for any item routed to the current user. Three policies:

| Item state | Notification |
|---|---|
| `awaiting_approval` routed to me | Yes (default) |
| Task `proposed` to me by another human | Yes |
| Task `proposed` to an agent (any) | No — agent's loop picks it up |
| Status `blocked` on an item I created | Yes |
| Status `done` on an item I delegated | Yes |
| Status `done` on an item I approved | No — noise |

Per-workspace policy can override these defaults (v1).

---

## What we deliberately are NOT building (yet)

- **Item comments / threaded discussion** — the comment input in the detail panel posts to the timeline today; richer commenting (mentions, replies, etc.) is v1.5.
- **Custom views / saved filters** — Linear has them; Mission Control v0/v1 has the standard 5 sections only. Save-view is v1.5.
- **Mobile responsive Mission Control** — desktop only. Channel adapters (Slack/Telegram inline approval) cover the mobile case better than a responsive screen.
- **Inline editing of agent-proposed content** — Shawn rejecting + re-prompting via the assistant is the v0.5 alternative.
- **Right-panel detail navigation** — the detail panel is one item at a time. Linear lets you navigate next/prev within a filtered list; we add this in v1.

---

## Cross-references

- [`2026-05-mission-control.md`](./2026-05-mission-control.md) — architectural spec
- [`2026-05-mission-control-backend-audit.md`](./2026-05-mission-control-backend-audit.md) — what exists, what needs building (the primitive-by-primitive audit)
- [`2026-05-pocket-specialist-and-ripple-mutation.md`](./2026-05-pocket-specialist-and-ripple-mutation.md) — the pocket-specialist subagent pattern that the agent task-claim flow (Flow 3) builds on
- `paw-enterprise/PR #181` — the mock UI implementing every flow above
