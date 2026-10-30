<!-- chat-shell.md — the default chat view (#/chat): sidebar session list,
     New Chat, composer. Handles seen on screen while authoring; no evidence
     saved yet. Read-only drive: never press Send, never delete or rename a
     session; the sessions belong to the user. -->

# Chat shell

Loading the dashboard lands on the chat view: a sidebar with `Chats` /
`Projects` tabs, a `New Chat` button, a `Search sessions...` box and the
existing sessions, and a main pane with the composer (`Chat message input`,
`Agent mode` checkbox, `Send message`). The top bar carries the view tabs
`Chat`, `Activity`, `Terminal`, `Deep Work`.

## Sub-features

- `chat-default-route` `/` and `/#/chat` both render the chat view with the
  `Chat` tab active.
- `chat-composer` the `Chat message input` textbox is present and
  `Send message` is disabled while the input is empty.
- `chat-agent-mode` the `Agent mode` checkbox is present (checked by default).
- `chat-sessions` the sidebar lists prior sessions as buttons, each with a
  `Session actions` button.
- `chat-view-tabs` `Activity`, `Terminal` and `Deep Work` switch the hash to
  `#/activity`, `#/terminal`, `#/crew`.

## How to get to it (user POV)

- Load `http://localhost:<port>/` (default view).
- Load `http://localhost:<port>/#/chat`, or click `Chat` in the top bar from
  any other view.
- Sidebar `New Chat` starts an empty session and stays on this view.

## Driving it with agent-browser

Preconditions:

- Launch done and Doctor printed its `drivable:` line.
- Fresh `agent-browser open http://localhost:<port>/`, then wait about 4 s.

- **Default route.** `agent-browser get url` is `http://localhost:<port>/`;
  `agent-browser snapshot -i` lists `button "Chat"`, `button "Activity"`,
  `button "Terminal"`, `button "Deep Work COMMAND CENTER"`, `button "New
  Chat"`, `textbox "Search sessions..."`.
- **Composer.** The snapshot lists `textbox "Chat message input"`,
  `checkbox "Agent mode" [checked=true]` and `button "Send message"
  [disabled]`. Type nothing; do not press Send.
- **Typing enables Send (optional, non-mutating).** `agent-browser fill
  "textarea[aria-label='Chat message input']" "verify"` then snapshot:
  `Send message` loses `[disabled]`. Clear it with `fill ... ""` before
  moving on; sending would create a real session and call the agent.
- **View tabs.** `agent-browser find role button click --name "Activity"`;
  `agent-browser get url` ends in `#/activity`. Repeat for `Terminal`
  (`#/terminal`) and `Deep Work` (`#/crew`), then click `Chat` to return.
- **Proof.** Save `agent-browser get text body` to `<run-dir>/chat-shell.txt`
  (first line: URL and `chat-shell`), the snapshot to
  `<run-dir>/chat-shell.snapshot.txt`, and
  `agent-browser screenshot <run-dir>/chat-shell.png`.

## Gotchas

- Session buttons carry the first message as their name and may be long;
  never click one by a guessed name, and never touch `Session actions`
  (rename, export, delete are the user's data).
- `Terminal` and `Deep Work` tabs are hidden below the `sm` breakpoint; a
  missing tab means a narrow viewport, not a regression.
- The health pill at the top of the sidebar reads `System running, but AI
  features disabled. Please add API key.` without an Anthropic key; that is
  expected and not a chat-shell failure.
- The Welcome wizard covers the view only when no backend is configured.
