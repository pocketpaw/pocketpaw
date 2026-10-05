<!-- skills.md — the Skills modal (components/modals/skills.html). Handles seen
     on screen while authoring (heading, My Skills / Library, filter box, Run
     buttons); no evidence saved yet. Read-only drive: open, switch tabs,
     filter; never click Run or Install. -->

# Skills

A user opens Skills from the sidebar and sees a modal with a `Skills` heading,
two tabs (`My Skills`, `Library`), a `Filter installed skills...` box, and one
row per installed skill with a `Run` button. The `Library` tab searches
skills.sh and offers `Install`.

## Sub-features

- `skills-open` the sidebar button (named `Skills` plus the installed count,
  e.g. `Skills 50`) opens the modal with the `Skills` H2.
- `skills-installed` `My Skills` lists installed skills; each row has `Run`.
- `skills-filter` typing in `Filter installed skills...` narrows the rows;
  no match shows `No skills match your filter`.
- `skills-library` `Library` shows a `Search skills.sh...` box and remote
  results with `Install`.

## How to get to it (user POV)

- Sidebar, `TOOLS & CONFIG` group: the `Skills` button (its name carries the
  installed count).
- No hash route; the modal opens over whichever view is active.

## Driving it with agent-browser

Preconditions:

- Launch done and Doctor printed its `drivable:` line.
- Fresh `agent-browser open http://localhost:<port>/`, snapshot shows the
  sidebar buttons.

- **Open.** Run `agent-browser find role button click --name "Skills"`. Within
  2 s `agent-browser snapshot -i` shows `heading "Skills" [level=2]`,
  `button "My Skills"`, `button "Library"`, `textbox "Filter installed
  skills..."`, and at least one `button "Run"` when skills are installed.
- **Count matches the badge.** The number in the sidebar button name
  (`Skills 50`) equals the number of `button "Run"` entries in the snapshot
  (`agent-browser snapshot -i | grep -c 'button "Run"'`).
- **Filter.** `agent-browser fill "input[placeholder='Filter installed
  skills...']" "zzzz-no-such-skill"`; `agent-browser get text body` contains
  `No skills match your filter`. Clear with `fill ... ""`.
- **Library tab.** `agent-browser find role button click --name "Library"`;
  the snapshot shows `textbox "Search skills.sh..."`. Do not search or
  install; the library calls the network.
- **Proof.** Save `agent-browser get text body` to `<run-dir>/skills.txt` (first
  line: URL and `skills`), the snapshot to `<run-dir>/skills.snapshot.txt`,
  and `agent-browser screenshot <run-dir>/skills.png`.

## Gotchas

- `Run` executes the skill through the agent and writes to the user's
  session history; `Install` writes under `~/.pocketpaw`. Neither is part of
  a verify drive.
- `find ... --name "Skills"` is a substring match and also matches the
  `Skills` heading once the modal is open; run it only from a fresh `open`.
- With zero installed skills the modal shows `No skills installed` and no
  `Run` buttons; the count check then expects 0, not a failure.
- The modal does not close on Escape; re-`open` the URL to leave it.
