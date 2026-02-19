# Deep Work planner prompt templates.
# Created: 2026-02-12
# Updated: 2026-02-18 — Added GOAL_PARSE_PROMPT for structured goal analysis
#   (domain detection, complexity estimation, clarification questions).
# Updated: 2026-02-12 — Added RESEARCH_PROMPT_QUICK and RESEARCH_PROMPT_DEEP
#   for configurable research depth.
#
# Prompt templates:
#   GOAL_PARSE_PROMPT — structured goal analysis (domain, complexity, roles)
#   RESEARCH_PROMPT — domain research (standard depth)
#   RESEARCH_PROMPT_QUICK — minimal research (skip web search)
#   RESEARCH_PROMPT_DEEP — thorough research (extensive web search)
#   PRD_PROMPT — PRD generation
#   TASK_BREAKDOWN_PROMPT — task decomposition to JSON
#   TEAM_ASSEMBLY_PROMPT — team recommendation to JSON

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

Output ONLY a valid JSON array. No markdown code fences. No commentary. \
Just the raw JSON array:

[
  {{
    "key": "t1",
    "title": "...",
    "description": "... with acceptance criteria",
    "task_type": "agent",
    "priority": "medium",
    "tags": ["..."],
    "estimated_minutes": 30,
    "required_specialties": ["..."],
    "blocked_by_keys": []
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
- Use "claude_agent_sdk" as the backend for all agents
- Agent names should be lowercase-hyphenated (e.g. "backend-dev", "qa-engineer")

Output ONLY a valid JSON array. No markdown code fences. No commentary. \
Just the raw JSON array:

[
  {{
    "name": "...",
    "role": "...",
    "description": "...",
    "specialties": ["..."],
    "backend": "claude_agent_sdk"
  }}
]
"""
