# Deep Work planner prompt templates.
# Created: 2026-02-12
# Updated: 2026-02-18 — Added GOAL_PARSE_PROMPT for structured goal analysis
#   (domain detection, complexity estimation, clarification questions).
# Updated: 2026-02-12 — Added RESEARCH_PROMPT_QUICK and RESEARCH_PROMPT_DEEP
#   for configurable research depth.
# Updated: 2026-05-21 (feat/taskspec-success-criteria) — TASK_BREAKDOWN_PROMPT
#   now instructs the planner to emit machine-verifiable success_criteria
#   and preconditions per task, with explicit discipline: criteria must be
#   objectively checkable (no "works as expected" / "should work
#   correctly"). Feeds the new first-class TaskSpec fields.
# Updated: 2026-05-21 (PR #1164 review) — TASK_BREAKDOWN_PROMPT JSON
#   example no longer says the description holds "acceptance criteria";
#   criteria belong in the dedicated success_criteria array.
# Updated: 2026-05-25 (feat/pocket-planner-skill) — added the POCKET_*
#   prompt family for the plan-before-create gate. The three prompts
#   parallel the project planner family but emit pocket-flavored
#   sections (NARRATIVE / WIDGETS / STATE / SOURCES / ACTIONS) and
#   ordered todos where each todo is one /spec/merge call. Consumed by
#   the ``plan_pocket`` MCP tool — no project_id, no DB persistence,
#   no team-assembly phase.
#
# Prompt templates:
#   GOAL_PARSE_PROMPT — structured goal analysis (domain, complexity, roles)
#   RESEARCH_PROMPT — domain research (standard depth)
#   RESEARCH_PROMPT_QUICK — minimal research (skip web search)
#   RESEARCH_PROMPT_DEEP — thorough research (extensive web search)
#   PRD_PROMPT — PRD generation
#   TASK_BREAKDOWN_PROMPT — task decomposition to JSON
#   TEAM_ASSEMBLY_PROMPT — team recommendation to JSON
#   POCKET_RESEARCH_PROMPT — pocket research (canonical patterns +
#     fabrics + connectors + similar pockets)
#   POCKET_PRD_PROMPT — pocket PRD with the 5 structured sections
#     (NARRATIVE / WIDGETS / STATE / SOURCES / ACTIONS) the brief
#     parser consumes
#   POCKET_TASK_BREAKDOWN_PROMPT — ordered todos where each is one
#     /spec/merge call

GOAL_PARSE_PROMPT = """\
You are an expert project analyst. Analyze the user's goal and produce a \
structured JSON assessment. This is the first step before planning — you need \
to understand WHAT the user wants, WHICH domain it falls into, HOW complex \
it is, and WHAT needs clarification.

USER INPUT:
{user_input}

Analyze the input and output ONLY a valid JSON object (no commentary). \
You may wrap it in ```json fences. The JSON must have exactly these fields:

{{
  "goal": "A clear, one-sentence restatement of the user's goal",
  "domain": "One of: code, business, creative, education, events, home, hybrid",
  "sub_domains": ["Array of specific sub-domains, e.g. 'web-development', 'react', 'aws'"],
  "complexity": "One of: S, M, L, XL",
  "estimated_phases": 1-10,
  "ai_capabilities": ["What AI can do for this project — be specific"],
  "human_requirements": ["What the human MUST do — things AI cannot"],
  "constraints_detected": ["Any budget, timeline, or technical constraints mentioned"],
  "clarifications_needed": ["Questions to ask BEFORE planning — only if truly ambiguous"],
  "suggested_research_depth": "One of: none, quick, standard, deep",
  "confidence": 0.0 to 1.0
}}

DOMAIN DEFINITIONS:
- code: Software development, APIs, apps, websites, scripts, data pipelines
- business: Market research, business plans, accounting, legal, marketing strategy
- creative: Writing, design, music, video, art, content creation
- education: Learning plans, courses, study guides, skill development
- events: Weddings, conferences, parties, travel planning, logistics
- home: Renovation, moving, organization, DIY projects, gardening
- hybrid: Projects spanning multiple domains (set sub_domains to clarify)

COMPLEXITY RULES:
- S: Single deliverable, < 1 hour, no dependencies
- M: 2-5 tasks, 1-4 hours, minimal dependencies
- L: 5-15 tasks, days to weeks, multiple phases and dependencies
- XL: 15+ tasks, weeks to months, multiple phases, team needed

CLARIFICATION RULES:
- Only ask if the answer would CHANGE the plan significantly
- Maximum 4 clarification questions
- Skip clarifications for obvious or standard approaches
- Never ask about things you can reasonably assume

Keep confidence between 0.5 (very vague input) and 1.0 (crystal clear goal).
"""

RESEARCH_PROMPT_QUICK = """\
You are a senior technical researcher. Based ONLY on your existing knowledge \
(no web searches needed), provide brief research notes for the project below.

PROJECT DESCRIPTION:
{project_description}

OUTPUT FORMAT — plain text with these sections:
1. Domain Overview (1-2 sentences)
2. Key Technical Considerations (3-5 bullets)
3. Recommended Approach (1 paragraph)

Keep your response under 150 words. Be concise.
"""

RESEARCH_PROMPT = """\
You are a senior technical researcher. Your job is to research the domain \
described below and produce structured research notes that will inform a PRD \
and task breakdown.

PROJECT DESCRIPTION:
{project_description}

OUTPUT FORMAT — plain text with these sections:
1. Domain Overview (2-3 sentences)
2. Key Technical Considerations (bullet list)
3. Risks & Unknowns (bullet list)
4. Comparable Solutions / Prior Art (bullet list)
5. Recommended Approach (1 paragraph)

Keep your response under 400 words. Be specific and actionable.
"""

RESEARCH_PROMPT_DEEP = """\
You are a senior technical researcher. Your job is to do thorough research on \
the domain described below. Use web search extensively to find current best \
practices, existing solutions, and technical details. Produce comprehensive \
research notes that will inform a detailed PRD and task breakdown.

PROJECT DESCRIPTION:
{project_description}

OUTPUT FORMAT — plain text with these sections:
1. Domain Overview (3-5 sentences with current state of the art)
2. Key Technical Considerations (detailed bullet list)
3. Risks & Unknowns (bullet list with mitigation suggestions)
4. Comparable Solutions / Prior Art (detailed bullet list with links if found)
5. Architecture Patterns (bullet list of relevant patterns and frameworks)
6. Recommended Approach (2-3 paragraphs with justification)

Be thorough and specific. Include technical details and concrete recommendations.
"""

PRD_PROMPT = """\
You are a product manager. Generate a minimal PRD in markdown for the project \
described below. Use the research notes provided to inform your decisions.

PROJECT DESCRIPTION:
{project_description}

RESEARCH NOTES:
{research_notes}

OUTPUT FORMAT — markdown with exactly these sections:
## Problem Statement
(1-2 sentences)

## Scope
(What is in scope and what is not)

## Requirements
(Numbered list of functional requirements)

## Non-Goals
(Bullet list of things explicitly out of scope)

## Technical Constraints
(Bullet list of technical limitations or requirements)

Keep the entire PRD under 500 words. Be concise and specific.
"""

TASK_BREAKDOWN_PROMPT = """\
You are a project architect. Break down the following project into atomic, \
executable tasks. Each task should have one clear deliverable.

PROJECT DESCRIPTION:
{project_description}

PRD:
{prd_content}

RESEARCH NOTES:
{research_notes}

RULES:
- Each task must be atomic (one clear deliverable)
- Mark tasks as "human" if they require physical actions, subjective decisions, \
or access to external systems that an AI agent cannot reach
- Mark tasks as "review" if they are quality gates or approval checkpoints
- All other tasks should be "agent"
- Ensure no cycles in blocked_by_keys (task A cannot depend on task B if B depends on A)
- Use short keys like "t1", "t2", etc.
- Keep estimated_minutes realistic (15-120 range for most tasks)

SUCCESS CRITERIA — every task MUST have a "success_criteria" array:
- Each entry is ONE concrete, objectively-verifiable statement that is true \
once the task is done. Someone who did not do the work must be able to check \
it without judgement calls.
- BANNED — never write vague criteria like "works as expected", "should work \
correctly", "is high quality", "looks good", or "is done properly". These \
cannot be verified and will be rejected.
- GOOD examples: "GET /health returns HTTP 200 with body {{\\"status\\":\\"ok\\"}}", \
"the invoice CSV has one row per overdue account", "pytest tests/ exits 0".
- Give 1-4 criteria per task — enough to confirm the deliverable, no filler.

PRECONDITIONS — every task MUST have a "preconditions" array:
- Each entry is ONE state/environment condition that must hold BEFORE the task \
can start, OR a condition under which the task should NOT run.
- These are conditions about the world — NOT dependencies on other tasks \
(those go in blocked_by_keys). Example preconditions: "a Postgres database \
is reachable", "the customer list has been uploaded to the workspace", \
"do not run if no invoices are overdue".
- Use an empty array [] when a task genuinely has no preconditions.

Output ONLY a valid JSON array. No markdown code fences. No commentary. \
Just the raw JSON array:

[
  {{
    "key": "t1",
    "title": "...",
    "description": "freeform context and approach for this task",
    "task_type": "agent",
    "priority": "medium",
    "tags": ["..."],
    "estimated_minutes": 30,
    "required_specialties": ["..."],
    "blocked_by_keys": [],
    "success_criteria": ["concrete verifiable statement", "..."],
    "preconditions": ["concrete state condition", "..."]
  }}
]
"""

TEAM_ASSEMBLY_PROMPT = """\
You are a team architect. Given the following task breakdown, recommend the \
minimal set of AI agents needed to execute this project efficiently. Each agent \
should cover one or more specialties required by the tasks.

TASKS:
{tasks_json}

RULES:
- Recommend the fewest agents that cover all required_specialties
- Each agent should have a clear, non-overlapping role
- Use "{agent_backend}" as the backend for all agents
- Agent names should be lowercase-hyphenated (e.g. "backend-dev", "qa-engineer")

Output ONLY a valid JSON array. No markdown code fences. No commentary. \
Just the raw JSON array:

[
  {{
    "name": "...",
    "role": "...",
    "description": "...",
    "specialties": ["..."],
    "backend": "{agent_backend}"
  }}
]
"""


# ============================================================================
# Pocket planner prompt family (feat/pocket-planner-skill).
#
# Mirrors the project planner family above but emits pocket-flavored
# structure. The output of POCKET_PRD_PROMPT is parsed by the brief
# extractor in ``pocketpaw_ee.agent.mcp_servers.planner`` — the section
# delimiters below are part of the contract (changing them will break
# the parser). The output of POCKET_TASK_BREAKDOWN_PROMPT is JSON the
# same ``PlannerAgent._parse_tasks`` consumes.
# ============================================================================

POCKET_RESEARCH_PROMPT = """\
You are a PocketPaw design researcher. A pocket is a workspace canvas — \
a JSON ``rippleSpec`` tree of typed widget nodes (``{{type, props, \
children}}``) that renders into a polished UI on the PocketPaw \
dashboard. Pockets carry both UI structure and a state layer; widgets \
read state via ``bind`` and write state via ``on_click`` action \
sequences.

Your job is NOT to design the pocket. Your job is to surface what \
already exists that the designer downstream should reach for.

USER BRIEF:
{project_description}

Look at four buckets:

1. CANONICAL POCKET PATTERNS. Pockets fit into one of seven shapes — \
``dashboard`` (overview KPIs + charts), ``app`` (interactive tool the \
user operates: todo, kanban, planner), ``viewer`` (read-only \
inspection of one thing), ``composer`` (focused authoring), \
``browser`` (list + drill-in master-detail), ``wizard`` (multi-step \
linear flow), ``feed`` (reverse-chronological stream). Name the ONE \
pattern this brief fits. Do NOT default to ``dashboard`` — most briefs \
are not dashboards.

2. RICH FOCAL WIDGETS. PocketPaw ships 150 widgets including composed \
domain layouts (``pipeline-dashboard``, ``invoice-layout``, \
``entity-detail``, ``ops-dashboard``, ``project-dashboard``, \
``master-detail``, ``kanban``, ``calendar``, ``gantt``, \
``form-layout``, ``wizard-layout``). For this brief, name 2-4 of the \
RICHEST widgets that map directly to the user's intent — prefer \
composed layouts over assembling the same shape from primitives.

3. STATE & LIVE DATA. List the state keys the user will visibly read \
or write (cards, deals, tasks, filters, drafts). Tag each with whether \
it's seed data (one-time mock content) or live (needs a connector / \
backend source).

4. EXISTING PATTERNS TO MIRROR. If PocketPaw's bundled-recipes KB \
likely has a pattern that matches (sales-pipeline-dashboard, \
customer-support-app, recipe-viewer), name it so the designer can \
reach for it as a starter.

OUTPUT FORMAT — plain text with EXACTLY these sections, in order:

## Pattern
(one line: the canonical pattern name and a one-sentence justification)

## Focal Widgets
(2-4 bullet lines, ``- <widget-kind>: <why this one>``)

## State Shape
(bullet lines, ``- <key>: <type> — <seed | live: <source-hint>>``)

## Similar Pockets
(0-3 bullet lines naming canonical patterns or recipes the brief \
resembles; empty bullet list is fine when nothing obvious matches)

Keep the entire output under 300 words. No code blocks, no commentary \
outside the sections.
"""


POCKET_PRD_PROMPT = """\
You are a PocketPaw pocket designer. The researcher above named the \
pattern, the focal widgets, the state shape, and any prior art. Your \
job is to turn that into a structured pocket PRD the downstream \
planner can decompose into ``/spec/merge`` build steps.

USER BRIEF:
{project_description}

RESEARCH NOTES:
{research_notes}

OUTPUT FORMAT — markdown with EXACTLY these five sections, in this \
order, using EXACTLY these ``##`` headings. The brief parser splits \
on them, so do not rename, reorder, or skip any.

## NARRATIVE
2-3 paragraphs describing what the pocket is, who uses it, and the \
design approach. Reference the pattern from research. End with one \
sentence on the "happy path" — the single action the user does most \
often inside this pocket.

## WIDGETS
A bullet list, one widget per line, format ``- <kind>: <purpose>``. \
Use the focal widgets from research as the spine; add layout primitives \
(``flex``, ``grid``, ``app-shell``, ``sidebar``) only as needed for \
chrome. Aim for 4-10 widgets total — pockets do one thing well. \
EXAMPLES:
- kanban: deal stages, cards represent deals
- stat: total pipeline value, top-left
- form-layout: add-deal composer below the kanban
- chart: revenue forecast, bottom

## STATE
A bullet list, one state key per line, format \
``- <key>: <py-ish type> — <purpose>``. Use lowercase snake_case for \
keys. Include seed-data keys AND any draft/filter/counter keys the \
actions need. EXAMPLES:
- cards: list[dict] — pipeline cards each carrying id, title, status, value
- draft: str — text input buffer for the add-deal composer
- next_id: int — monotonic id counter for new cards
- filter: str — current pipeline stage filter

## SOURCES
A bullet list of live data sources, one per line, format \
``- <connector>: <feeds state.X via METHOD path>``. Use \
``<connector> = none`` for purely seeded pockets — but ALWAYS include \
this section even if the only entry is ``- none: pocket runs on \
seeded state``. EXAMPLES:
- crm: feeds state.cards via GET /leads
- analytics: feeds state.revenue_series via GET /metrics/revenue?period=q

## ACTIONS
A bullet list of user-triggered state changes, one per line, format \
``- <trigger>: <op> on state.<target> (<value or note>)``. The op is \
one of: ``push`` (append item), ``set`` (overwrite), ``remove`` \
(delete item), ``patch`` (merge). EXAMPLES:
- Add Deal button: push on state.cards (new card from state.draft fields)
- Stage filter select: set on state.filter (the chosen value)
- Mark Won kanban menu: patch on state.cards[id] (status -> "won")

Hard rules:
- Use the exact section headings above (``## NARRATIVE``, \
``## WIDGETS``, ``## STATE``, ``## SOURCES``, ``## ACTIONS``). \
Anything else breaks the parser.
- Bullet lines start with ``- `` (dash + space). Don't use ``*`` or \
numbered lists.
- Keep the whole PRD under 500 words.
- No introduction, no closing remarks — just the five sections.
"""


POCKET_TASK_BREAKDOWN_PROMPT = """\
You are a PocketPaw build planner. The PRD above describes a pocket. \
Your job is to turn it into an ordered list of build todos, where \
each todo is ONE cohesive ``POST /api/v1/pockets/<id>/spec/merge`` \
call that the chat agent will walk in order.

USER BRIEF:
{project_description}

PRD:
{prd_content}

RESEARCH NOTES:
{research_notes}

RULES:
- One todo per merge call. Don't bundle "add widget X and seed state Y \
and wire action Z" into a single todo unless they're literally one \
merge body.
- ORDER MATTERS. Seed state before the widgets that read it. Add the \
parent layout before its children. Wire on_click actions only after \
both the widget and the state key exist.
- Aim for 4-10 todos. Fewer is better — each merge call is a tested \
unit, not a click-by-click.
- Each todo is "agent" task_type (the chat agent walks the list — no \
human action between todos). Don't emit "human" or "review" todos.
- Use short keys: ``t1``, ``t2``, ``t3``, ...
- ``blocked_by_keys`` carries the inter-todo dependency graph and must \
NEVER have cycles.

SUCCESS CRITERIA — every todo MUST have a "success_criteria" array:
- Each entry is ONE concrete, objectively-verifiable statement about \
the pocket AFTER this merge runs. The chat agent will read these to \
decide whether to advance to the next todo.
- BANNED — never write "works", "looks good", "is correct", or any \
other vague predicate.
- GOOD examples: \
"state.cards is a list with at least 4 seed entries", \
"the kanban widget at the root has columns [lead, qualified, proposal, won]", \
"the add-deal button's on_click sequence pushes to state.cards then clears state.draft".
- Give 1-3 criteria per todo.

PRECONDITIONS — every todo MUST have a "preconditions" array:
- Each entry is ONE state condition that must already hold BEFORE \
this todo runs. These are conditions about the pocket's current \
shape, NOT inter-todo dependencies (those go in blocked_by_keys).
- Use empty array ``[]`` when the todo has no preconditions (typical \
for the very first seed todo).
- GOOD examples: "state.cards exists and is a list", \
"a node of type 'kanban' exists at the ui root".

Output ONLY a valid JSON array. No markdown code fences. No commentary. \
Just the raw JSON array:

[
  {{
    "key": "t1",
    "title": "<imperative one-line action, e.g. 'Seed state.cards with pipeline data'>",
    "description": "<one-paragraph note: which merge body to assemble, why this slice was chosen>",
    "task_type": "agent",
    "priority": "medium",
    "tags": ["state-seed" | "widget-add" | "action-wire" | ...],
    "estimated_minutes": 5,
    "required_specialties": ["pocket-spec"],
    "blocked_by_keys": [],
    "success_criteria": ["concrete verifiable statement about post-merge state", "..."],
    "preconditions": ["concrete pre-merge condition", "..."]
  }}
]
"""
