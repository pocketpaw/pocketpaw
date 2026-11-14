<!-- Provenance for sites-references: what came from the Refero Skill, what is
     local, and how to refresh it. The data half is the bundled
     `pocketpaw_refero` and `pocketpaw_inspo` MCP servers. -->
# Vendored: sites-references

`sites-references` is one file with two halves. Part 1, the research loop, is
adapted from an upstream skill (below). Part 2, the technique source list, is
local: it was written here against the /sites toolset and has no upstream.

## Part 1: the research loop

- **Source:** https://github.com/referodesign/refero_skill (MIT, Refero), the
  `refero-design` skill
- **Vendored from:** commit `a9b54a3e62a6` (upstream v1.0.2)
- **License:** MIT. Attribution retained; the body is a rewrite, not a copy.

### Why it is not named after the vendor

Upstream is `refero-design`. A bundled skill named after a vendor ties the
surface to one supplier for a capability that is really "research the design
before you make it". If the Refero subscription lapses, the name either lies
or needs renaming across every preamble that lists it. The loop also works on
Inspo, which ships on every deploy.

### What was NOT ported, and why it matters

Upstream is about 4,200 lines across a SKILL.md and ten reference files. About
200 lines of that are additive here; the rest would have hurt.

**The craft references** (`typography.md`, `color.md`, `motion.md`,
`craft-details.md`, `anti-ai-slop.md`, `copywriting.md`, `icons.md`). We carry
all of this, tuned to this surface, in `pocketpaw-design-taste`, `sites-craft`,
`sites-check` and `sites-restraint`. A second, differently-opinionated design
system in the same prompt is worse than one, and the agent has no way to
adjudicate between them.

**The primacy framing, the load-bearing removal.** Upstream's `description`
declares itself the primary skill for essentially all UI work and tells the
agent to demote frontend design, UI polish and craft skills to supplements.
Installed unmodified it would quietly become the design authority on /sites
while knowing nothing about the surface's constraints. The rewritten
description claims the research step only, and the body opens with an
ownership table handing design authority back.

**The flows layer.** We bundle styles and screens only (a Paw Site is a
marketing page, not a multi-step journey), so every mention of flows is removed
rather than left naming a tool the agent does not have.

**`visual-workflow.md` and the image-generation QA pass.** They assume a
browser, a screenshot loop and a filesystem. `_SITES_BUILTIN_DENY` strips
`Read`, `Glob` and `Bash` on every /sites mode, so none of it can run there.
`preview_site` is the screenshot step that does exist.

**`mcp-tools.md`.** It documents Refero's raw tool names. Ours are namespaced
per backend (`mcp__pocketpaw_refero__search_styles` on the SDK surface,
`refero_design_styles` as a BaseTool elsewhere), so the routing table was
rewritten.

### What was ported

- Research before designing, with depth matched to risk.
- Search several angles, varying the axis (aesthetic, domain, named product)
  rather than rewording one query.
- Styles first, screens for structure; open the picture, not only the text.
- Do not copy one reference. Do not average several: blending yields the safe
  centroid, the generic output research was meant to escape.
- Preserve roles: a CTA-only colour stays CTA-only; a media role is honoured
  with real assets, never faked with a decorative box.
- The reference lock and decision ledger, and the test behind them: a major
  choice with no source is not a design decision yet.

### Added here, not from upstream

- The ownership table, handing authority back to `pocketpaw-design-taste`
  and scope to its MODULE 0.
- The unconfigured path. Refero needs a paid plan, so an empty result is the
  normal outcome here, including "never cite a reference you did not receive".
- "A reference never justifies a section the brief did not ask for."
- A light product-led worked example in the lock, because design-taste's
  Clean-Tech family calls the dark-with-one-accent page the most generic look.

## Part 2: technique sources (local)

Written for this surface: a catalogue of design resources filtered to what can
be retyped as text (CSS, inline SVG, a GLSL string, a font URL), because
/sites has no shell and no download tool. Google Fonts is the one font source;
the list of faces it serves lives in `pocketpaw-design-taste` 2.F, not here.
Decorative grounds and effects are listed for when a brief asks, not as
defaults.

## How it ships

An on-demand skill, scope `both` in `_SITES_DESIGN_SKILLS`
(`ee/pocketpaw_ee/cloud/surface/handlers/sites.py`), so it is advertised in
the `<design-skills>` block on create and refine and named in the svelte/react
create `skill_names` allowlist. Not embedded: its triggers (the look is open,
or an effect is about to be hand-written) are real branches, not every turn.
Byte ceiling: `tests/test_sites_references_skill_budget.py`.

## Not verified

No site has been authored with a live Refero token here; runs so far exercised
the unconfigured path and Inspo. The research loop is carried on upstream's
authority, not independently re-derived.

## To refresh

Re-fetch upstream `skills/refero-design/SKILL.md`. Check two things before
merging anything: whether the tool surface changed (we bundle styles and
screens only), and whether the `description` still tries to outrank sibling
design skills. That framing is what this vendoring exists to strip; a refresh
that pastes it back would silently demote `pocketpaw-design-taste`.
