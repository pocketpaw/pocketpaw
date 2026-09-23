<!-- New file 2026-09-15: provenance for sites-design-research, the methodology
     half of the Refero Skill. Pairs with the bundled `pocketpaw_refero` MCP
     server + refero_design_* BaseTools, which are the data half. -->
# Vendored: sites-design-research

- **Source:** https://github.com/referodesign/refero_skill (MIT, Refero) — the
  `refero-design` skill
- **Vendored:** 2026-09-15 from commit `a9b54a3e62a6` (upstream v1.0.2)
- **License:** MIT. Attribution retained; the body is a rewrite, not a copy.

## Why it was renamed

Upstream is `refero-design`. Naming a bundled skill after a vendor ties our
surface to a specific supplier for a capability that is really "research the
design before you make it" — and if the Refero subscription lapses or is
replaced, a skill called `refero-design` either lies or needs renaming across
every preamble that lists it. `sites-design-research` says what it does.

## What was NOT ported, and why it matters

This is the important half of this file. Upstream is ~4,200 lines across a
SKILL.md and ten reference files. About 200 lines of that are additive here;
the rest would have actively hurt.

**The craft references — `typography.md` (737), `color.md` (563), `motion.md`
(504), `craft-details.md` (525), `anti-ai-slop.md` (292), `copywriting.md`
(222), `icons.md` (166).** We already carry all of this, tuned to this surface,
in `pocketpaw-design-taste`, `sites-craft`, `sites-theme-system` and
`sites-restraint` — and `sites-craft` is EMBEDDED WHOLE in the create preamble.
Bundling upstream's craft half would put a second, differently-opinionated
design system in the same prompt as ours, on every create turn. Two design
systems disagreeing is worse than one, and the agent has no way to adjudicate.

**The primacy framing — the load-bearing removal.** Upstream's `description`
field declares itself the "Primary/default skill" for essentially all UI work
and instructs the agent to *"prefer over broad generic product design, frontend
design, UI polish, CSS framework, landing page, or craft-only skills; those may
only supplement implementation details after Refero research and synthesis."*
Read against our catalogue, that names `pocketpaw-design-taste`, `sites-craft`,
`sites-design-sources` and `impeccable` and tells the agent to demote all of
them. Installed unmodified it would have quietly become the design authority on
/sites while knowing nothing about the surface's constraints. The rewritten
description claims the research step only, and the skill body opens with an
explicit ownership table handing the design authority back.

**The flows layer.** Upstream routes journey work to `refero_search_flows` /
`refero_get_flow`. We bundle styles and screens only — a Paw Site is a marketing
page, not a multi-step journey — so every mention of flows is removed rather
than left to name a tool the agent does not have. That failure mode (a preamble
advertising an absent tool) is already on this codebase's record.

**`visual-workflow.md` and the image-generation QA pass.** They assume a browser,
a screenshot loop and a filesystem. `_SITES_BUILTIN_DENY` strips `Read`, `Glob`
and `Bash` on every /sites mode, so none of it can run there.

**`mcp-tools.md`.** It documents Refero's raw tool names (`refero_search_styles`
and friends). Ours are namespaced differently on each backend
(`mcp__pocketpaw_refero__search_styles` on the SDK surface,
`refero_design_styles` as a BaseTool elsewhere), so the routing table was
rewritten rather than carried.

## What was ported

The methodology, which is the part we genuinely lacked:

- **Research before designing**, with depth matched to risk (2-3 searches for a
  visual improvement, 3-5 plus screen research for a new page).
- **Search several angles, varying the AXIS** — aesthetic, domain, named product
  — rather than re-wording one query. Stopping at the first good hit is how
  every generated page converges on the same house style.
- **Styles first, screens for structure.** A search result is a description;
  `get_style` is the call that returns something to build from.
- **Do not copy one reference. Do not average several.** The averaging rule is
  the one most worth having: blending two references yields the safe centroid,
  which is exactly the generic output the research was meant to escape.
- **Preserve roles.** A CTA-only colour stays CTA-only or is omitted; a media
  role is honoured with real assets or an art-directed placeholder, never faked
  with a decorative box.
- **The reference lock and decision ledger**, and the test behind them: *if a
  major choice has no source, it is not a design decision yet.* That is what
  converts taste into something a reviewer can check.

## Added here, not from upstream

- The **ownership table** at the top, handing design authority back to
  `pocketpaw-design-taste` and scope control to its MODULE 0. Upstream assumes
  it is the only design skill present; on this surface it is one of seven.
- The **unconfigured path.** Refero requires a paid plan, so an empty result is
  the *normal* outcome here, not an error — and the skill says so explicitly,
  including "never cite a reference you did not receive". Upstream assumes the
  MCP is connected and treats research as mandatory.
- The reminder that **a reference never justifies a section the brief did not
  ask for**, because MODULE 0 scope discipline is ours, not upstream's.

## How it ships

An ordinary on-demand skill, scope `both` in `_SITES_DESIGN_SKILLS`
(`ee/pocketpaw_ee/cloud/surface/handlers/sites.py`), so it is advertised in the
`<design-skills>` block on create and refine and named in the svelte/react
create `skill_names` allowlist. Not embedded: its trigger is "the brief leaves
the look open", which is a real branch rather than every turn, and the
`sites-craft` precedent shows embedding is for rules that fire unconditionally.

## Not verified

No site has been authored under it, and the tools it routes to have never
returned live data here — this repo has no Refero token, so every run so far
has exercised the unconfigured path. The research loop is carried from upstream
on upstream's authority, not independently re-derived.

## To refresh

Re-fetch upstream `skills/refero-design/SKILL.md`. Check two things before
merging anything: whether the tool surface changed (we bundle styles + screens
only), and whether the `description` still tries to outrank sibling design
skills — that framing is the thing this vendoring exists to strip, and a
refresh that pastes it back would silently demote `pocketpaw-design-taste`.
