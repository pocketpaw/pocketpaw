<!-- mcp-servers.md — the MCP Servers modal (components/modals/mcp.html).
     Code-derived: the handles below come from the template and were not seen
     on screen when the skill was generated. Read-only drive: open, switch
     My Servers / Catalog, read the list; never submit Add Server. -->

# MCP servers

A user opens MCP from the sidebar (`MCP BETA`) and sees a modal with an
`MCP Servers` heading, two view buttons (`My Servers`, `Catalog`), the list of
configured servers with their connection state, and an `Add Server` button that
reveals a form (server name, command, args, or a server URL). `Catalog` lists
preset servers grouped by category.

## Sub-features

- `mcp-open` the `MCP BETA` sidebar button opens the modal with the
  `MCP Servers` H2 and `My Servers` selected.
- `mcp-list` `My Servers` lists each configured server from
  `~/.pocketpaw/mcp_servers.json`; an empty config shows a `Browse Catalog`
  prompt instead.
- `mcp-add-form` `Add Server` toggles the form with placeholders
  `Server name (e.g. filesystem)`, `Command (e.g. npx)`,
  `Args (comma-separated, e.g. -y,@mcp/server-fs,/home)`,
  `Server URL (e.g. http://localhost:9000)`.
- `mcp-catalog` `Catalog` shows preset cards; a category with none shows
  `No presets in this category`.

## How to get to it (user POV)

- Sidebar, `TOOLS & CONFIG` group: the `MCP BETA` button (a connected-count
  badge appears when servers are up).
- No hash route; the modal opens over the active view.

## Driving it with agent-browser

Preconditions:

- Launch done and Doctor printed its `drivable:` line.
- Fresh `agent-browser open http://localhost:<port>/`, snapshot shows the
  sidebar buttons.

- **Open.** Run `agent-browser find role button click --name "MCP"`. Within
  2 s `agent-browser snapshot -i` shows `heading "MCP Servers" [level=2]`,
  `button "My Servers"`, `button "Catalog"`, `button "Add Server"`.
- **List.** `agent-browser get text body` lists the configured server names, or
  `Browse Catalog` when none are configured. Compare against
  `~/.pocketpaw/mcp_servers.json` read-only.
- **Add form (toggle only).** `agent-browser find role button click --name
  "Add Server"`; the snapshot now shows textboxes with the placeholders above.
  Fill nothing, submit nothing; click `Add Server` again or re-`open` to
  dismiss.
- **Catalog.** `agent-browser find role button click --name "Catalog"`; the
  text shows preset names or `No presets in this category`.
- **Proof.** Save `agent-browser get text body` to `<run-dir>/mcp-servers.txt`
  (first line: URL and `mcp-servers`), the snapshot to
  `<run-dir>/mcp-servers.snapshot.txt`, and
  `agent-browser screenshot <run-dir>/mcp-servers.png`.

## Gotchas

- Submitting the add form writes `~/.pocketpaw/mcp_servers.json` and starts
  a process; installing a preset does the same. Toggle, do not submit.
- The server log may show `Failed to start MCP server 'fabric': No such file
  or directory` on this machine; that is the user's config, not a modal
  failure.
- `find ... --name "MCP"` is a substring match; `MCP BETA` is the only
  sidebar button containing it, but `MCP Servers` (the heading) also matches
  once the modal is open. Fresh `open` first.
- The modal does not close on Escape; re-`open` the URL to leave it.
