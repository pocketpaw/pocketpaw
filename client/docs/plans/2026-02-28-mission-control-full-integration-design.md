# Mission Control Full Integration Design

**Date:** 2026-02-28
**Status:** Approved
**Scope:** All 3 tiers — Agent Management, Task Execution, Deep Work Projects, Collaboration, Notifications

---

## Problem

The Command Center dashboard panels (Kanban, Table, Feed, Metrics) have basic CRUD (create task, change status, delete, create/delete documents) but the entire AI agent interaction layer is missing. The backend has 39 MC + 8 Deep Work endpoints; the client wires only 5. Users cannot:

- See, create, or manage AI agents
- Run a task with an agent and watch it execute
- Assign agents to tasks
- Create projects from natural language goals
- Review AI-generated plans and approve them
- Monitor project execution with dependency scheduling
- See agent-to-user notifications
- Have discussion threads on tasks
- View or edit document content
- See daily standup summaries

---

## Architecture

### New Stores

- **`mcStore`** (`stores/mission-control.svelte.ts`) — agents list, running tasks set, active execution state (task + streaming log), notifications
- **`projectStore`** (`stores/projects.svelte.ts`) — project list, active project, planning progress state

### New API Methods (~25 methods)

**Mission Control (`mcRequest` helper → `/api/mission-control`):**

| Method | Endpoint |
|--------|----------|
| `mcListAgents(status?)` | `GET /agents` |
| `mcCreateAgent(name, role, desc, specialties, backend, level)` | `POST /agents` |
| `mcUpdateAgent(id, fields)` | `PATCH /agents/{id}` |
| `mcDeleteAgent(id)` | `DELETE /agents/{id}` |
| `mcAssignTask(taskId, agentIds)` | `POST /tasks/{id}/assign` |
| `mcRunTask(taskId, agentId)` | `POST /tasks/{id}/run` |
| `mcStopTask(taskId)` | `POST /tasks/{id}/stop` |
| `mcGetRunningTasks()` | `GET /tasks/running` |
| `mcGetTaskMessages(taskId, limit?)` | `GET /tasks/{id}/messages` |
| `mcPostTaskMessage(taskId, content, fromAgentId?)` | `POST /tasks/{id}/messages` |
| `mcListNotifications(unreadOnly?)` | `GET /notifications` |
| `mcMarkNotificationRead(id)` | `POST /notifications/{id}/read` |
| `mcGetDocument(id)` | `GET /documents/{id}` |
| `mcUpdateDocument(id, content)` | `PATCH /documents/{id}` |
| `mcGetStandup()` | `GET /standup` |
| `mcListProjects(status?)` | `GET /projects` |
| `mcGetProject(id)` | `GET /projects/{id}` |
| `mcDeleteProject(id)` | `DELETE /projects/{id}` |

**Deep Work (`dwRequest` helper → `/api/deep-work`):**

| Method | Endpoint |
|--------|----------|
| `dwParseGoal(goal)` | `POST /parse-goal` |
| `dwStartProject(goal, desc?)` | `POST /start` |
| `dwGetPlan(projectId)` | `GET /projects/{id}/plan` |
| `dwApproveProject(projectId)` | `POST /projects/{id}/approve` |
| `dwPauseProject(projectId)` | `POST /projects/{id}/pause` |
| `dwResumeProject(projectId)` | `POST /projects/{id}/resume` |
| `dwCancelProject(projectId)` | `POST /projects/{id}/cancel` |
| `dwSkipTask(projectId, taskId)` | `POST /projects/{id}/tasks/{tid}/skip` |
| `dwRetryTask(projectId, taskId)` | `POST /projects/{id}/tasks/{tid}/retry` |

### WebSocket Events

Add to `WSEvent` type union and handle in stores:

| Event | Store | Action |
|-------|-------|--------|
| `mc_task_started` | mcStore | Add to `runningTasks`, set `activeExecution` |
| `mc_task_output` | mcStore | Append to `activeExecution.log` |
| `mc_task_completed` | mcStore | Remove from `runningTasks`, clear execution, reload data |
| `mc_task_retry` | mcStore | Update retry info in log |
| `mc_activity_created` | mcStore | Trigger kit data reload |
| `dw_planning_phase` | projectStore | Update `planningProgress` (phase indicator) |
| `dw_planning_complete` | projectStore | Set project status, stop planning animation |
| `dw_project_cancelled` | projectStore | Update project status |

---

## Tier 1: Agent Management + Task Execution

### Agent Roster Panel (`AgentRoster.svelte`)

- New panel type `agent-roster` in `PanelRenderer.svelte`
- Kits include `{ type: "agent-roster", source: "api:agents" }` in layout YAML
- Renders grid of agent cards: initials avatar, name, role, status badge (idle=gray, active=green pulse, blocked=orange, offline=red), level tag, specialty chips
- "+" button → create agent dialog: name, role, description, specialties (comma-separated), backend select, level select
- Card click → edit dialog (same fields + delete button)
- Backend: add `api:agents` source resolver in `kits.py`

### Enhanced Kanban Card Detail

Extend existing `KanbanBoard.svelte` card detail dialog:

- **Assign section:** Shows assigned agent chips (from `item.assignee_ids`). Dropdown populated from `mcStore.agents` to add/change assignees. Calls `mcAssignTask()`.
- **Run button:** Visible when task has assignees and status is `assigned`/`inbox`. Picks first assignee. Calls `mcRunTask(taskId, agentId)`. Opens execution slide-over.
- **Stop button:** Visible when task is in `mcStore.runningTasks`. Calls `mcStopTask()`.
- **Running indicator:** Pulsing green dot on kanban cards that are currently executing.

### Task Execution Slide-over (`TaskExecutionPanel.svelte`)

- Fixed-position panel, right edge, w-96, full viewport height, z-40
- Header: task title, agent name + status badge, close button
- Body: scrolling log of execution output entries from WS `mc_task_output` events
  - `message` type → plain text paragraph
  - `tool_use` type → collapsible code block with tool name header
  - `tool_result` type → result block with status indicator
- Footer: "Stop Execution" button (red)
- Auto-scrolls to bottom on new entries
- State: `mcStore.activeExecution = { taskId, agentId, agentName, taskTitle, log: [], status }`
- Closes on `mc_task_completed` (with brief "Done" / "Error" indicator before auto-close)

---

## Tier 2: Deep Work Project Lifecycle

### Projects Page (`/projects` route)

Separate route, added as a workspace tab in `WorkspaceTabs.svelte`.

**Sub-views:**

#### Project List (default)
- Grid of project cards: title, status badge, progress bar (completed/total), team agent avatars, created date
- Status filter tabs: All / Active / Completed
- "New Project" button → switches to creator view

#### Project Creator
- **Step 1 — Goal:** Textarea for natural language goal. "Analyze" button → `POST /parse-goal` → shows: domain, complexity (S/M/L/XL), AI/human role split, clarification questions
- **Step 2 — Plan:** User clicks "Start Planning" → `POST /start` → planning progress view with phase indicators (Goal Analysis → Research → PRD → Tasks → Team) animated by `dw_planning_phase` WS events
- **Step 3 — Review:** Auto-navigates to project detail on `dw_planning_complete`

#### Project Detail
- **Header:** Title, status badge, lifecycle buttons:
  - `awaiting_approval` → "Approve" (green) + "Cancel" (red)
  - `executing` → "Pause" + "Cancel"
  - `paused` → "Resume" + "Cancel"
  - `completed/failed/cancelled` → read-only
- **PRD:** Collapsible markdown section showing generated PRD content
- **Task table:** Columns: title, type (agent/human/review), priority, status, assignee, est. time. Grouped by execution level (dependency layers). Per-row actions: Skip (for blocked tasks), Retry (for failed tasks)
- **Progress:** Horizontal stacked bar: done (green) / in-progress (blue) / blocked (orange) / pending (gray)
- **Team:** Small agent cards showing assigned agents

---

## Tier 3: Collaboration & Polish

### Task Messages

Enhance kanban card detail dialog:
- New "Discussion" section below status/priority
- Chronological message thread: agent name/id, content, timestamp
- Text input at bottom with "Send" button
- Calls `mcGetTaskMessages()` on dialog open, `mcPostTaskMessage()` on send

### Notification Bell

- `NotificationBell.svelte` added to `QuickActions.svelte` in titlebar
- Bell icon (from lucide) with red unread count badge
- Click → `NotificationDropdown.svelte`: scrollable list of notifications
  - Type icon, content text, relative timestamp, read/unread styling
  - Click notification → mark as read; if `source_task_id`, open that task
- `mcStore` polls notifications every 60s via `setInterval`

### Standup Summary

- New panel type `standup` in `PanelRenderer.svelte`
- `StandupPanel.svelte`: fetches from `GET /standup`, renders markdown
- Data source: `api:standup` (add resolver in `kits.py` backend)

### Document Viewer/Editor

Enhance `DataTable.svelte`:
- Click document row → opens dialog with document title + content
- Read mode: rendered markdown or plain text
- "Edit" toggle → textarea for editing content
- "Save" button → `PATCH /documents/{id}` (auto-increments version)

---

## File Impact

| # | File | Action |
|---|------|--------|
| 1 | `lib/types/pawkit.ts` | MODIFY — add AgentProfile, AgentStatus, AgentLevel, ProjectStatus, Notification, etc. |
| 2 | `lib/api/types.ts` | MODIFY — add MC/DW WS event types to WSEvent union |
| 3 | `lib/api/client.ts` | MODIFY — add ~25 new API methods + `dwRequest` helper |
| 4 | `lib/stores/mission-control.svelte.ts` | CREATE — agents, running tasks, execution, notifications |
| 5 | `lib/stores/projects.svelte.ts` | CREATE — project list, active project, planning progress |
| 6 | `lib/stores/index.ts` | MODIFY — export + init new stores |
| 7 | `lib/components/panels/AgentRoster.svelte` | CREATE — agent grid panel |
| 8 | `lib/components/panels/StandupPanel.svelte` | CREATE — standup markdown |
| 9 | `lib/components/panels/PanelRenderer.svelte` | MODIFY — add agent-roster + standup |
| 10 | `lib/components/panels/KanbanBoard.svelte` | MODIFY — assign, run, stop, messages, running indicator |
| 11 | `lib/components/panels/DataTable.svelte` | MODIFY — document viewer/editor on row click |
| 12 | `lib/components/command-center/TaskExecutionPanel.svelte` | CREATE — slide-over execution log |
| 13 | `lib/components/command-center/LayoutRenderer.svelte` | MODIFY — render execution panel |
| 14 | `lib/components/titlebar/QuickActions.svelte` | MODIFY — add NotificationBell |
| 15 | `lib/components/titlebar/NotificationBell.svelte` | CREATE — bell icon + badge |
| 16 | `lib/components/titlebar/NotificationDropdown.svelte` | CREATE — notification list |
| 17 | `routes/projects/+page.svelte` | CREATE — projects page |
| 18 | `routes/projects/+page.ts` | CREATE — load function |
| 19 | `lib/components/titlebar/WorkspaceTabs.svelte` | MODIFY — add Projects tab |
| 20 | Backend: `src/pocketpaw/api/v1/kits.py` | MODIFY — add api:agents + api:standup resolvers |

**New files: 8 | Modified files: 12 | Total: 20**
