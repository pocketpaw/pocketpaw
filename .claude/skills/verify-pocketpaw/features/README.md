<!-- README.md — index of the PocketPaw dashboard verification map. One file
     per user-facing feature; each follows the four-H2 contract below. All
     mapped features are read-only on purpose: the verify server shares
     ~/.pocketpaw with the user's live dashboard. Maintained by hand: touching a
     mapped feature means updating its file in the same PR. -->

# PocketPaw dashboard verification map

Maintained source for verifying user-facing dashboard behavior in PocketPaw.
Read this index, run the skill's Doctor, then follow the matching feature file.

## Baseline preconditions

- Server started by `scripts/verify.sh launch` on port 8889 or higher, never
  the user's own instance on 8888.
- Doctor run and its `drivable:` line read. `degraded` health (no Anthropic
  key) is fine for every feature here.
- `AGENT_BROWSER_EXECUTABLE_PATH` points at system Chrome.
- Desktop-width viewport (agent-browser's default). The sidebar is hidden
  below the `md` breakpoint; if the snapshot lacks `Settings` / `Skills` /
  `MCP BETA` buttons, the viewport is too narrow.
- The first-run `Welcome` modal appears only when no agent backend is
  configured (`isBackendUnconfigured()`); with `~/.pocketpaw/config.json`
  naming a backend it does not show.

## Driving conventions

- Start every recipe with `agent-browser open http://localhost:<port>/<hash>`;
  modals do not close on Escape and their close button is unnamed, so a
  leftover modal blocks the next `find`.
- Find controls by role and accessible name
  (`agent-browser find role button click --name "Skills"`); click `@eN` refs
  only right after the snapshot that produced them.
- Wait for an observable state (a heading, a tab button, a textbox) rather
  than a fixed sleep; the first paint after `open` takes about 4 s.
- Treat every quoted name as literal. Names with counts (`Skills 50`,
  `Projects 3`) match on the prefix; `find ... --name` is a substring match.
- Never send a message, delete or rename a session, run or delete a task,
  save a setting, or add a server: the data is the user's.
- Keep proof artifacts; cleanup only removes the server and scratch state.

## Proof and skip reporting

- Capture the action and the resulting state: `get text body` plus a screenshot,
  named `<feature-id>.txt` / `<feature-id>.png` in the run's evidence dir;
  add `<feature-id>.snapshot.txt` when the proof rests on roles and names.
- Record the feature ID and the entry point you used with every artifact.
- Report an unreachable path with the attempted step and the unmet
  precondition.
- Do not report a skipped entry point as verified through another path.

## Feature entry contract

Each feature file starts with an H1 and one paragraph of user-visible behavior,
then exactly four H2s in this order:

1. `Sub-features` lists short IDs with one line per behavior.
2. `How to get to it (user POV)` lists every user entry point.
3. `Driving it with agent-browser` starts with `Preconditions:` and uses
   labeled bullets pairing each user action with the exact command and the
   observable result.
4. `Gotchas` lists traps that waste or invalidate a run.

## Features

Proven live: `settings` (2026-10-05, agent-browser, port 8889, health
`degraded`; evidence `.verify-evidence/<run-id>/settings.*`). The handles in
`chat-shell`, `skills` and `deep-work` were seen on screen while authoring
(sidebar buttons, `Skills` heading with `My Skills` / `Library`, the `#/crew`
task list) but no evidence was saved for them; `mcp-servers` is code-derived
from `components/modals/mcp.html` and never seen on screen. Treat unproven
handles as leads until a run proves them, then move the feature to this line.

- [Chat shell](./chat-shell.md) — the default `#/chat` view: session list,
  `New Chat`, composer with `Agent mode` toggle and a disabled `Send message`.
- [Settings](./settings.md) — the `Settings` modal: search box, seven section
  tabs, agent backend and provider comboboxes.
- [Skills](./skills.md) — the `Skills` modal: `My Skills` / `Library` tabs,
  installed-skill filter, per-skill `Run`.
- [MCP servers](./mcp-servers.md) — the `MCP Servers` modal: configured
  servers list and the `Add Server` form.
- [Deep Work](./deep-work.md) — the `#/crew` view: `Tasks` / `Projects` tabs,
  status filters, task cards with `View Details`.
