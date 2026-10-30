<!-- deep-work.md — the Deep Work view (#/crew; components/missions.html and
     missions/). Handles seen on screen while authoring (Tasks / Projects,
     status filters, task cards); no evidence saved yet. Read-only drive: open
     the view, switch tabs and filters, open View Details; never Run, Delete,
     New Task, or New Agent. -->

# Deep Work

The `Deep Work` top-bar tab (badged `COMMAND CENTER`) switches to `#/crew`: a
task board with `Tasks` / `Projects` tabs, `New Task` and `New Agent` buttons,
status filters (`All`, `In Progress`, `Inbox`, `Done`), and one card per task
with `View Details`, `Delete`, and `Run Task` on tasks that have not run.

## Sub-features

- `deep-work-route` the tab and the `#/crew` URL both render the board.
- `deep-work-tabs` `Tasks` shows cards; `Projects` (name carries a count,
  e.g. `Projects 3`) shows the project list; `#/crew/projects` opens it
  directly.
- `deep-work-filters` `All` / `In Progress` / `Inbox` / `Done` narrow the
  cards.
- `deep-work-card` a card is named by its task title; `View Details` opens the
  task sheet.

## How to get to it (user POV)

- Top bar: the `Deep Work` tab (hidden below the `sm` breakpoint).
- Load `http://localhost:<port>/#/crew` (tasks) or `/#/crew/projects`.
- Sidebar `Projects` tab, then a project, also lands on `#/project/<id>`.

## Driving it with agent-browser

Preconditions:

- Launch done and Doctor printed its `drivable:` line.
- Fresh `agent-browser open http://localhost:<port>/`.

- **Tab.** `agent-browser find role button click --name "Deep Work"`;
  `agent-browser get url` ends in `#/crew` and `agent-browser snapshot -i`
  shows `button "Tasks"`, a `button "Projects ..."`, `button "New Task"`,
  `button "New Agent"`, and the four filter buttons.
- **Direct URL.** `agent-browser open http://localhost:<port>/#/crew`; same
  snapshot after about 4 s.
- **Cards.** Each task appears as a clickable element named by its title with
  nested `button "View Details"` and `button "Delete"`; tasks not yet run
  also carry `button "Run Task"`. Count cards with
  `agent-browser snapshot -i | grep -c 'button "View Details"'`.
- **Filter.** `agent-browser find role button click --name "Done"`; the card
  count changes or stays, but every remaining card is a finished task
  (`Run Task` absent). Click `All` to restore.
- **Details (read-only).** `agent-browser find role button click --name
  "View Details"` opens the first task's sheet; `agent-browser get text body`
  shows the task title. Re-`open` the URL to leave it.
- **Proof.** Save `agent-browser get text body` to `<run-dir>/deep-work.txt`
  (first line: URL and `deep-work`), the snapshot to
  `<run-dir>/deep-work.snapshot.txt`, and
  `agent-browser screenshot <run-dir>/deep-work.png`.

## Gotchas

- `Run Task` launches a real agent run against the user's Mission Control
  store; `Delete`, `New Task` and `New Agent` mutate it too. None are part of
  a verify drive.
- Mission Control state is shared with the user's live instance
  (`Mission Control loaded: N agents, M tasks` in the server log), so card
  counts are whatever the user has, not a fixture; assert structure, not
  numbers.
- The top-bar tab is hidden below the `sm` breakpoint; use the URL if the
  snapshot lacks it.
- `find ... --name "Done"` is a substring match; task titles containing
  `Done` would also match. Prefer the filter row's order from the snapshot.
