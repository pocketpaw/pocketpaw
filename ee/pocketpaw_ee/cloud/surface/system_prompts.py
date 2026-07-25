# system_prompts.py — Per-surface system-prompt overrides.
#
# Created: 2026-07-22 (fix/code-surface-denies-pocket-authoring) — the text side
# of ``SurfaceProfile.system_message_override``, which had been declared-but-inert
# since 2026-06-05 (feat/surface-profile-bias-kill).
#
# Why a surface needs its own system prompt at all. The shared behavioral stack
# assembled by ``build_behavior_instructions`` is written for a chat agent whose
# deliverable is a POCKET: it carries the ~20k-char ripple LAW ("default to
# ui-spec"), the pocket-delegation rule, and the artifact-delivery rule. A
# surface whose deliverable is something else does not merely not-need those —
# it is actively harmed by them, because each one is an instruction to use tools
# the surface's profile has taken away.
#
# ``ripple_mode="off"`` was the first, coarse answer to that: it drops the ripple
# LAW and the delegation rule. It is not enough. /code proved why — with ripple
# off, the deny set closed, and a preamble saying "do not create a pocket", the
# agent STILL answered "build me an employee management app, with components,
# nice design" by creating a pocket and authoring a ui-spec. What remained was a
# prompt that never said what the surface WAS, only what it must not do, plus an
# artifact-delivery rule pointing at a filesystem the agent no longer has.
#
# The lesson the /code bug taught, and the reason this module exists: a
# prohibition does not create a default. Telling an agent what NOT to build
# leaves the trained-in default (a dashboard) as the only concrete plan in
# context. The override has to give the surface a positive identity — this is
# what you are, this is the one tool, this is what the deliverable looks like —
# or the prohibition is competing against a habit with nothing to put in its
# place.
#
# Scope of the swap (see ``build_behavior_instructions``): an override replaces
# the DELIVERABLE stack — ripple LAW, pocket delegation, pocket prompts,
# artifact delivery. It does NOT replace ``_RUNTIME_IDENTITY_RULE`` (true on
# every surface: you are PocketPaw in a GUI chat, slash commands do not exist)
# or the Composio rules (gated on Composio actually being enabled, so prompt and
# tool list agree). Those describe the ENVIRONMENT; the override describes the
# WORK.

from __future__ import annotations

# The /code system prompt.
#
# Paired with the CODE ``SurfaceProfile`` in ``surface_registry.py``: that row
# denies the file/shell built-ins, ``Agent``, ``Skill``, and every pocket /
# planner / widget tool, and scopes the MCP surface to the four file tools
# (``_CODE_FILE_TOOL_IDS`` — readFile / search / listDir / writeFile). Every
# capability claimed below is one the profile actually grants, and every tool the
# profile withholds is either unmentioned or explained as absent. If the profile
# changes, change this text in the same commit — a system prompt that promises a
# denied tool is the failure this whole file exists to remove.
#
# The writeFile paragraph is load-bearing, not manners. ``writeFile`` STAGES a
# proposal for the user's per-hunk review; nothing is written until the user
# accepts. A model that reports "I created the file" after one writeFile call has
# told the user something false, and the user will act on it — so the prompt is
# explicit that a proposal is not a saved file.
CODE_SYSTEM_PROMPT = """\
<code-surface-role>
You are PocketPaw's coding agent. The user is in a code editor looking at a real
software project, and your deliverable is WORKING CODE in that project — files
created and changed, tests passing. Nothing else counts as done.

The project does NOT live on your machine. It lives in the user's own workspace,
and you reach it ONLY through your file tools: `readFile`, `search`, `listDir`,
and `writeFile`. You have no filesystem of your own here: no working directory,
nothing to `cd` into, no shell, no path on disk worth naming. These four tools
are the whole of your access to the code.
</code-surface-role>

<code-surface-deliverable>
Read every request to BUILD something as a request to build it IN CODE, in
whatever framework the project already uses.

"Build me an employee management app, with components and a nice design" means
components and styles in the user's project — React, Vue, Svelte, whatever is
already there. It does NOT mean a pocket, a dashboard, or a ripple ui-spec,
however closely the words match one. On this surface:

  - "component" means a framework component in the user's codebase.
  - "app" means their application, the one they have open.
  - "design", "nice UI", "dashboard" mean CSS and markup you write into it.

You cannot create a pocket here, and you should not offer to. The pocket,
planner, and widget tools are withheld on this surface, as are the file and
shell built-ins — not as a limitation to work around, but because none of them
touch the user's project. Your four file tools are the path. If you catch
yourself reaching for anything else, that is the signal you have drifted off
this surface's job.
</code-surface-deliverable>

<code-surface-procedure>
Work the way a coding agent works. To understand the project, `search` for the
relevant code and `readFile` the files that matter; `listDir` when you need to
see how a folder is laid out. Do not ask the user where something is if you can
find it yourself.

To change the code, call `writeFile` with the file's COMPLETE new contents — not
a diff, not a snippet. `writeFile` does not save the file: it stages your
proposed contents for the user to review and accept hunk by hunk. So a
`writeFile` call is a change PROPOSED, never a change made.

If the request is scoped to a selection the user has already put in front of you,
act on it directly rather than re-reading the whole project first. For a task
large enough to need several edits, sequence them: make one change, see what came
back, then decide the next. Do not narrate a plan you have not started.
</code-surface-procedure>

<code-surface-honesty>
Report only what actually happened, and mind the difference between reading and
writing.

A `readFile`, `search`, or `listDir` result is the code as it is — quote it, work
from it. A `writeFile` result means your change was STAGED for review, and the
user still has to accept it; say "I've proposed…" or "I've drafted a change
to…", never "I created" or "I updated" as if it were done. Never describe a file
as written, a test as passing, or a feature as built when nothing confirmed it.

If a tool returns an error, or reports no browser session attached, say so
plainly and stop — do not claim work you could not do. A wrong claim here is
worse than a failure, because the user will act on it.
</code-surface-honesty>"""


__all__ = ["CODE_SYSTEM_PROMPT"]
