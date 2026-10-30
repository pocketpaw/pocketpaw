<!-- settings.md — the Settings modal (components/modals/settings.html). The
     one feature proven live when the skill was generated. Read-only drive: open,
     switch section tabs, read values; never change a combobox or save. -->

# Settings

A user opens Settings from the sidebar or the top-right gear and sees a modal
with a search box, a column of section tabs (`General`, `API Keys`,
`Behavior & Safety`, `Memory`, `Search & Services`, `System`, `Soul`) and the
`General` section showing the agent backend and model provider as comboboxes.

## Sub-features

- `settings-open` either entry point opens the modal with the `Settings` H2
  and the `General` H3 visible.
- `settings-tabs` clicking a section tab swaps the H3 and the fields shown.
- `settings-backend` the first combobox shows the configured agent backend
  (`Claude Agent SDK (Recommended)` on a default install).
- `settings-search` typing in `Search settings...` filters the fields shown.

## How to get to it (user POV)

- Sidebar, `TOOLS & CONFIG` group: the `Settings` button near the bottom.
- Top-right of the top bar: the gear button named `Settings (Ctrl+,)`.
- Keyboard: `Ctrl+,` with the dashboard focused.

## Driving it with agent-browser

Preconditions:

- Launch done and Doctor printed its `drivable:` line.
- `agent-browser open http://localhost:<port>/` and a snapshot listing the
  `Settings` button (desktop width, no modal open).

- **Open from the sidebar.** Run
  `agent-browser find role button click --name "Settings"`. Within 2 s
  `agent-browser snapshot -i` shows `heading "Settings" [level=2]`, a
  `textbox "Search settings..."`, the seven tab buttons, `heading "General"
  [level=3]`, and a combobox whose current text is the configured backend.
- **Open from the gear.** After a fresh `open`, run
  `agent-browser find role button click --name "Settings (Ctrl+,)"`; same
  result as above.
- **Switch a section.** With the modal open, run
  `agent-browser find role button click --name "API Keys"`; the snapshot now
  shows `heading "API Keys" [level=3]` and the `General` fields are gone.
- **Read the backend.** `agent-browser get text body` contains
  `AGENT BACKEND` (the label renders uppercase) and the combobox text
  (`Claude Agent SDK (Recommended)` on this machine). Do not change it.
- **Proof.** Save `agent-browser get text body` to `<run-dir>/settings.txt` (first
  line: URL and `settings`), `agent-browser snapshot -i` to
  `<run-dir>/settings.snapshot.txt`, and
  `agent-browser screenshot <run-dir>/settings.png`.

## Gotchas

- The modal does not close on Escape, and its close button has no accessible
  name (`button [ref=eN]` right after the heading). Re-`open` the URL instead
  of hunting for it.
- `find ... --name "Settings"` is a substring match; the sidebar `Settings`
  button is listed before the gear (`Settings (Ctrl+,)`), so it wins, but a
  stale modal can make either hit the wrong element. Fresh `open` first.
- Comboboxes list backends marked `not installed`; selecting one writes to
  the user's `~/.pocketpaw/config.json`. Reading is the whole drive.
- The `Search settings...` textbox hides tabs whose fields do not match; clear
  it before asserting the seven tabs.
