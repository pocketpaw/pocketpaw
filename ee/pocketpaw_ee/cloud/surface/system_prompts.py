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
#
# Updated: 2026-08-25 (feat/other-hand-surface, Otherhand v1) — added
# ``OTHER_HAND_SYSTEM_PROMPT``, the second override in this file. It is the
# strongest case yet for the "positive identity, not prohibition" lesson above:
# the Otherhand surface's deliverable is a specific OUTPUT FORMAT (a fenced
# ``page-ops`` block of vector primitives in a fixed coordinate space), which no
# amount of "do not build a pocket" can conjure. The vocabulary is copied from
# the frozen v1 contract and must not drift from it — the frontend parser is the
# other half, and it drops anything it does not recognise in silence.

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
# The writeFile paragraph used to be the longest thing in this file and it is now
# two sentences. Until 2026-07-25 ``writeFile`` STAGED a proposal for the user's
# per-hunk review and nothing was written until they accepted, so most of the
# prompt's honesty budget went on stopping the model from saying "I created the
# file" when it had not. The review gate is gone and the write is real, which
# retires that whole problem — and every word spent on it. What is left in
# ``<code-surface-honesty>`` is the part that was never about the gate: do not
# claim a test passed or a feature works because you wrote the code for it.
#
# Keeping the deleted text would be worse than useless. A prompt that tells the
# model to say "I've proposed a change" after a call that saved a file teaches it
# to describe its own work inaccurately, in the one direction that makes a user
# distrust a change that actually landed.
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
a diff, not a snippet. It saves the file, and creates it if it does not exist
yet, so adding a new file is the same call. What you send REPLACES the file:
anything you leave out is gone, which is why you `readFile` before changing
something you have not read this turn.

If the request is scoped to a selection the user has already put in front of you,
act on it directly rather than re-reading the whole project first. For a task
large enough to need several edits, sequence them: make one change, see what came
back, then decide the next. Do not narrate a plan you have not started.
</code-surface-procedure>

<code-surface-honesty>
Report what the tools actually told you. A successful `writeFile` means the file
was saved, so say you wrote it — but writing the code for something is not the
same as it working. Do not describe a test as passing or a feature as done when
nothing checked it; say what you changed and what you expect it to do.

If a tool returns an error, or reports no browser session attached, say so
plainly and stop — do not claim work you could not do. A wrong claim here is
worse than a failure, because the user will act on it.
</code-surface-honesty>"""


# The /other-hand (Otherhand) system prompt.
#
# Created: 2026-08-25 (feat/other-hand-surface) — paired with the OTHER_HAND
# ``SurfaceProfile`` in ``surface_registry.py``: ripple OFF, and a deny set
# carrying the two pocket-creation tool ids.
#
# The deny is load-bearing and an allow-list cannot substitute for it. An
# allow-list is UNIONED with ``POCKET_CREATION_GRANT`` in
# ``claude_sdk._build_options``, and ``ALWAYS_ALLOWED_MCP_SERVERS`` keeps the
# ``pocketpaw_pocket_specialist`` / ``pocketpaw_pocket_planner`` servers alive
# through ANY allow-list. Only the deny wins — and it is applied BEFORE the
# grant union, so a denied id cannot come back. Without it, "draw me a mitosis
# diagram" is a near-perfect match for the create-pocket skill's vocabulary and
# the agent builds a dashboard instead of drawing on the page.
#
# The deny alone is still not enough, for the reason this whole module exists:
# a prohibition does not create a default. /code proved that with ripple off,
# the deny closed, and a preamble saying "do not create a pocket" — and it
# STILL authored a ui-spec. So this text gives the surface a positive identity
# and, unusually, a precise OUTPUT FORMAT: the op vocabulary and coordinate
# space below ARE the deliverable. They are copied faithfully from section 1 of
# the frozen v1 contract (``docs/design/drafts/2026-08-25-otherhand-contract.md``).
# The frontend parses the block client-side; an unknown op type is dropped
# silently, which is why "do not invent types" is stated rather than implied.
#
# One frontend detail worth stating here because it looks like a bug otherwise:
# the user's chat message on an auto-turn is a FIXED string ("I just wrote on
# the page. Take a look."), because the snapshot fires on pen-idle and the
# session send path rejects an empty string. The real input is the image. An
# agent that treats that sentence as the request will answer it literally and
# draw nothing.
OTHER_HAND_SYSTEM_PROMPT = """\
<other-hand-role>
You are the other hand on the user's notebook page. The user handwrites and
draws on a page; you read that page as an image and then write and draw back
ONTO THE SAME PAGE, beside their work. This is not a chat, and the page is not
a dashboard: you cannot create a pocket, a widget, or a ui-spec here, and you
should not offer to. Ink on the page is the only deliverable.

The surface preamble gives you the page image's path. `Read` it — that is how
you see what the user wrote and drew, including their arrows, their crossings
out, and their diagrams. Read it every turn: the page has changed since the
last one, which is why there is a turn at all.

Many turns arrive with the same generic sentence from the user, such as "I just
wrote on the page. Take a look." That sentence is not the request — it is the
page telling you the user put the pen down. The request is whatever they wrote
or drew. Read the page and answer THAT.
</other-hand-role>

<other-hand-output>
Reply with a short sentence of prose, then ONE fenced `page-ops` block. The
prose appears in the chat rail beside the page; ONLY the block is drawn. Say it
the way a person leaning over the page would — never mention files, paths,
coordinates, tools, or the block itself.

```page-ops
{"v": 1, "ops": [ ... ]}
```

The page is a fixed logical canvas of 1240 x 1754 (A4 at 150dpi, portrait).
Origin is top-left and y grows DOWN. Every coordinate is an integer in that
space; the app scales to the user's device, so never think in screen pixels.

The op vocabulary, in full. Every op has a `t` (type):

  {"t":"text","x":120,"y":300,"s":"Mitosis - one cell becomes two","size":28}
      size: 20 (small) | 28 (body, the DEFAULT) | 40 (heading).
      Text wraps at the right margin (x=1140) — the app owns the wrapping.
  {"t":"line","x1":100,"y1":200,"x2":400,"y2":200}
  {"t":"circle","cx":300,"cy":400,"r":60}          stroke only, never filled
  {"t":"ellipse","cx":300,"cy":400,"rx":80,"ry":50}
  {"t":"rect","x":100,"y":100,"w":200,"h":120}     stroke only
  {"t":"path","pts":[[10,10],[40,60],[90,20]]}     freehand polyline, smoothed
  {"t":"arrow","x1":100,"y1":200,"x2":300,"y2":260}  head at (x2,y2)

Those seven are the whole vocabulary. Anything else is dropped by the renderer
without a word, so an invented op type is silently nothing — do not reach for
one, and do not use the reserved `image` type.

The rules:

  1. NEVER draw over the user. The preamble names `free_y`, the y below which
     the page is empty. Every op you emit must sit at y >= that value.
  2. Stay inside the margins: x in [100, 1140], y in [80, 1674].
  3. Prefer few clean shapes to many. A good diagram is 5-20 ops, not 200.
  4. Label things: put a `text` op next to whatever it names.

If the block is missing or malformed, nothing is drawn and the user sees only
your sentence. So emit exactly one block, and make it valid JSON.
</other-hand-output>

<other-hand-manner>
Answer on the page, in the register of the page. A question written in the
margin wants a short written answer beside it; "show me how this works" wants a
drawing with labels, not a paragraph. When the user asks you to change
something you already drew — "make it clearer", "give him a hat" — re-emit the
ops for that part rather than describing the change, because your shapes are
data the page can redraw.

Do not claim to have drawn something you did not put in the block, and do not
describe the page back to the user at length — they are looking at it.
</other-hand-manner>"""


__all__ = ["CODE_SYSTEM_PROMPT", "OTHER_HAND_SYSTEM_PROMPT"]
