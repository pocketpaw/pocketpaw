---
name: pocketpaw-pocket-planner
description: |
  Plan a complex PocketPaw pocket before building it. Invoke when the
  pocket_specialist create path returns a ``plan_kit`` (template-match
  failed and the request looks like a custom multi-widget app).
  The skill teaches the agent to call
  ``mcp__pocketpaw_pocket_planner__plan_pocket``, render the brief in chat
  as markdown, iterate with the user, then walk the todos to build via
  ``POST /api/v1/pockets/<id>/spec/merge``. Sibling to
  ``pocketpaw-pocket-specialist`` — that skill applies edits; this one
  plans the initial build.
---

# Pocket Planner Workflow

You're invoked because ``pocket_specialist__create`` returned a
``plan_kit`` payload. The user asked for something the bundled
templates do not cover — a custom multi-widget pocket — so we plan
the build before we touch any rippleSpec. The plan is ephemeral: no
DB row, no project. The chat session owns the iteration state.

## When to use this skill

Use it when ALL of these hold:

- The user asked to create a NEW pocket (not edit an existing one).
- The ``pocket_specialist__create`` response carried
  ``action: "plan_kit"`` with a ``draft_kit`` whose ``skill_name``
  field is ``pocketpaw-pocket-planner``.
- The brief is custom and multi-widget — not a one-liner that fits a
  built-in template.

DO NOT use this skill when:

- The brief matches a built-in template (kanban, todo, etc.). Those
  short-circuit to the one-shot create path and never produce a
  plan_kit.
- The user is asking to EDIT a pocket they're already looking at. Use
  the ``pocketpaw-edit-pocket`` skill for edits.
- The brief is trivial ("a single chart of X"). Plan-then-build is
  overhead for one-widget pockets — let the agent compose it directly.

## The four-phase flow

### Phase 1 — Research and draft the plan

Call the planner tool with the user's verbatim brief:

```
mcp__pocketpaw_pocket_planner__plan_pocket({
  intent: "<the user's original ask>",
  deep_research: false
})
```

The tool returns ``{ok: true, brief}`` where ``brief`` is the schema:

```
{
  "narrative":   "<2-3 paragraph design rationale>",
  "widgets":     [ {"type": "kanban", "purpose": "deal stages"}, ... ],
  "state":       { "<key>": {"type": "<py-ish>", "purpose": "..."} },
  "sources":     [ {"connector": "crm", "feeds": "<METHOD path>"} ],
  "actions":     [ {"trigger": "<UI element>", "effect": "<op on state>"} ],
  "todos":       [
    { "id": "t1",
      "label": "<imperative one-line action>",
      "description": "...",
      "success_criteria": ["concrete verifiable statement", ...],
      "preconditions": ["..."],
      "depends_on": ["..."] },
    ...
  ],
  "research_notes": "<the planner's research output>"
}
```

### Phase 2 — Render the brief in chat as markdown

Lay the brief out so the user can read it at a glance. Use this exact
shape (the desktop client renders standard markdown — no special
widgets needed):

```markdown
## Plan: <one-line summary of the pocket>

<narrative>

### Widgets
- **kanban** — deal stages
- **stat** — total pipeline value
- ...

### State
- **cards** *(list[dict])* — pipeline cards, each carrying id, title, status, value
- **draft** *(str)* — text input buffer for the add-deal composer
- ...

### Sources
- **crm** → feeds `state.cards` via `GET /leads`
- ...

### Actions
- **Add Deal button** → push on `state.cards`
- ...

### Build steps
- [ ] **t1** — Seed state.cards with pipeline data
  - depends on: none
  - done when: state.cards has 8+ entries
- [ ] **t2** — Add kanban widget bound to state.cards
  - depends on: t1
  - done when: a kanban node exists at the ui root with columns [lead, qualified, proposal, won]
- ...

**Look right? Say "build it" to ship, or tell me what to change.**
```

Use ``- [ ]`` for unticked todos and ``- [x]`` after a todo's
``/spec/merge`` lands. The two-space-indented sub-bullets carry
``depends on`` and ``done when`` so the user can sanity-check the
order and the criteria.

### Phase 3 — Iterate with the user

The user replies in one of three shapes:

- **Build trigger** — "build it" / "go" / "ship it" / "make it" /
  "looks good" / a thumbs-up emoji. Jump to Phase 4.
- **Revision** — "drop the chart" / "add a forecast widget" / "use
  a feed pattern instead of dashboard" / "rebuild this from scratch
  as a wizard". Call the planner again with the prior plan + the
  delta:

  ```
  mcp__pocketpaw_pocket_planner__plan_pocket({
    intent: "<the original brief>",
    prior_plan: <the brief object from the previous call>,
    iteration_delta: "<the user's verbatim revision request>"
  })
  ```

  Re-render the new brief and ask again. Iterate up to ~3 rounds
  before suggesting the user accept the current shape (each round
  is real LLM time).

- **Pivot / cancel** — "actually, never mind" / "I want something
  different entirely". Stop. Ask what they'd like instead and route
  the new ask back through ``pocket_specialist__create``.

### Phase 4 — Build by walking the todos

The chat agent walks ``brief.todos`` in dependency order. The build
endpoint is the same merge endpoint the
``pocketpaw-pocket-specialist`` skill uses — see that sibling skill
for the rippleSpec shape, action verbs, and the four interactivity
conventions. The differences here are:

1. **First todo creates the pocket.** The first ``POST /spec/merge``
   against an id that does not exist yet creates the pocket; every
   later todo merges into that same id. Generate a fresh pocket id
   for the first call (the response carries the id back; reuse it).

2. **One todo per merge.** Do not bundle two todos into a single
   request — the brief's todo boundaries are tested units. Each
   merge body should satisfy the todo's ``success_criteria`` and no
   more.

3. **Auth headers from the plan_kit.** Use the exact headers the
   ``plan_kit.auth_headers`` block carried — ``X-PocketPaw-Internal:
   true`` plus the workspace and user id. The endpoint runs at
   ``http://localhost:8888/api/v1/pockets/<id>/spec/merge``.

4. **Tick as you go.** After each successful merge, update the
   markdown plan in chat — change ``- [ ]`` to ``- [x]`` for the
   completed todo. The user wants to see progress.

5. **Halt on failure.** If a merge returns ``{ok: false}`` with
   warnings, surface the warnings to the user and STOP. Do NOT
   retry the todo more than once on your own. Ask the user whether
   to fix-and-retry, skip, or abandon the build.

## Conventions (carry over from pocketpaw-pocket-specialist)

The merge bodies you build in Phase 4 must follow the same four
conventions the ``pocketpaw-pocket-specialist`` skill documents in
full:

1. **Client-side actions, not chat round-trips.** Use ``push`` /
   ``set`` / ``validate`` in ``on_click`` sequences, never
   ``{action: "emit", target: "chat.send"}``.
2. **Selects: value/label split.** Bound state holds the ``value``
   (machine id); the ``label`` is display-only.
3. **Lowercase column ids.** A kanban column ``{id: "lead"}`` matches
   cards whose ``status: "lead"`` — case-sensitive.
4. **Validate → push → clear → increment.** The canonical add-item
   action sequence is validate the input is non-empty, push to the
   collection, clear the draft field, then increment the id counter.

If a todo's ``success_criteria`` is satisfied by a different
rippleSpec than what you'd naturally compose, prefer the one the
``pocketpaw-pocket-specialist`` skill would build — it's been
hardened. Load that skill if you need to refresh on the shape.

## Hard rules

- **NEVER call ``pocket_specialist__create`` again** for a brief
  this skill is already planning. The plan_kit is the handoff; from
  here you own the build. Calling create back would loop.
- **NEVER skip the brief render.** Even if you think the brief is
  obviously correct, render it so the user sees what they're
  approving.
- **NEVER walk the todos before the user says "build it".** A
  rendered plan is not consent — the user has to confirm.
- **NEVER write a todo's success_criteria yourself.** They come from
  the planner. If the criteria are vague, that's a planner bug to
  file, not something to paper over by gold-plating the build.
- **NEVER post to /spec/merge without the auth headers** from
  ``plan_kit.auth_headers``. They are how the loopback-internal
  endpoint trusts the workspace and user.

## Known MVP limitations

These are real friction points that the MVP cuts; if you bump into
them often in real use, surface that to the captain so we know to
prioritise the follow-up.

- **Widget / state / source / action IDs are NOT stable across
  iteration calls.** Each ``plan_pocket`` call re-derives the brief
  from scratch, so a revision that adds one widget can renumber every
  existing widget (``w_3`` → ``w_4``, etc.). When you walk the todos
  in Phase 4 you key off ``brief.todos[i].id`` for tracking, but the
  underlying widget refs inside the merge bodies may not line up with
  what the user thinks they approved on a prior round. Surface
  changed IDs in the rendered diff so the user is not surprised.
- **No end-to-end test of the handler with a mocked LLM yet.** The
  parser layer is unit-tested but the full ``_plan_pocket_handler``
  → pipeline → brief path is exercised only against a live model.

## When done

Report back in chat with:

- The pocket id you built (link to it in the desktop client).
- The number of todos completed vs. total.
- Any ``ok: true`` warnings from the merge calls (verbatim).
- A one-line summary of what the user can now do in the pocket.

Example:

> Built **Sales Command Center** (pocket id `p_abc123`). 8/8 todos
> complete. The pipeline kanban is on the canvas with seeded deals,
> the add-deal composer pushes new cards, and the forecast chart
> updates as you change the period. Open it from the sidebar.
