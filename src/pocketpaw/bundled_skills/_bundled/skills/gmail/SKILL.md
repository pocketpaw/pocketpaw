---
name: gmail
description: |
  Work with the user's Gmail inside a pocket/room that has the Gmail
  connector bound. Invoke when the user wants to find, read, triage, or
  send email from the chat — "search my inbox for the invoice from Acme",
  "what did Sarah email me yesterday", "reply to the last message from
  legal", "draft and send a follow-up to the candidate", "label these as
  Done". This skill teaches the Gmail action surface (search, read, send,
  labels, trash) and the safe-by-default workflow: read before you act,
  confirm before you send or destroy. It is auto-surfaced into a room when
  the Gmail connector is bound to that pocket, so you only see it when
  Gmail is actually available.
---

<!--
  Updated: 2026-07-16 (feat/senses-render-convention / SR-8) — added the
  "Render results as a live pocket" section (the Ripple fusion): after a
  connector_execute read, render the result as TYPED Ripple widgets pushed to
  the pocket via POST /api/v1/pockets/<id>/spec/merge, instead of pasting raw
  JSON into chat. Documents the two hard rules (typed widgets only / never raw
  HTML from attacker-controllable email payloads; value/label split) and a
  worked example (recent inbox -> each+card cards with a value/label select
  filter, follow-up wired via invoke_tool -> connector_execute). The static
  widget-recipes home-rail fallback is untouched. Read/send safety guidance
  below is unchanged.

  Updated: 2026-06-08 (feat/connector-mcp-execution / keystone) — the skill now
  instructs the agent to invoke Gmail actions through the agent-callable
  ``connector_execute`` tool (the new pocketpaw_connectors MCP server) instead
  of the vague "call named actions". Read actions (search/read/list/summary)
  run via connector_execute; the confirm-trust actions (send/trash/modify/...)
  are BLOCKED in v1 by the tool, so the skill's confirm-before-act guidance now
  also notes those are not executable from chat yet. The safety/workflow
  guidance is otherwise unchanged.
-->

# Gmail in a Room

This room has the **Gmail connector** bound to it. You can search, read,
and triage email on the user's behalf through the connector's actions.
The connector handles OAuth, MIME, and the Gmail API.

You reach those actions through two tools:

- **`list_connector_actions`** — call this FIRST. It lists the connectors
  bound to this pocket and the actions you can run. For Gmail it shows the
  read actions you may execute and the write actions blocked in v1.
- **`connector_execute(connector_name, action, params)`** — run one action.
  For Gmail, `connector_name` is `"gmail"`. Example:
  `connector_execute(connector_name="gmail", action="gmail_search",
  params={"query": "from:acme.com subject:invoice", "max_results": 5})`.

Treat the mailbox as the user's real inbox. Reading is cheap and safe;
**sending and destroying are not** — and in v1 those write actions are
blocked by the tool (see Guardrails). Default to caution.

## The action surface

| Action | What it does | Trust |
|--------|--------------|-------|
| `gmail_search` | Find messages by Gmail query syntax | auto |
| `gmail_read` | Read one message's full body by id | auto |
| `gmail_send` | Send a new email | confirm |
| `gmail_list_labels` | List all mailbox labels | auto |
| `gmail_create_label` | Create a new label | confirm |
| `gmail_modify` | Add/remove labels on one message | confirm |
| `gmail_trash` | Move one message to Trash | confirm |
| `gmail_batch_modify` | Apply label changes to many messages | confirm |
| `gmail_summary` | Inbox stats (unread / today) | auto |

"Trust = confirm" actions are **writes, and they are BLOCKED in v1.** When
you call `connector_execute` for a send/trash/label/create-label action, the
tool refuses it with a "needs approval — coming in v2" message and does NOT
execute it. So today you can read and triage-by-reading, but you cannot send,
trash, or relabel from chat. Don't pretend a blocked action succeeded — tell
the user that Gmail writes aren't available from chat yet. (The "read before
you act" workflow below still applies for when v2 lands.)

## Core workflow: read before you act

Almost every request starts with a search, then a read. Run each through
`connector_execute(connector_name="gmail", action=..., params=...)`.

1. **Search** to find the candidate messages — `action="gmail_search"`,
   `params={"query": <Gmail search syntax>, "max_results": <≤20>}`
   (default 5). It returns a list of message stubs, each with an `id`,
   `from`, `subject`, and snippet.
2. **Read** the specific message — `action="gmail_read"`,
   `params={"message_id": <id from the search result>}` — when you need the
   full body to summarize, quote, or draft a reply.
3. **Act** (send / label / trash) is a v1-blocked write: draft it in chat
   and tell the user it can't be sent from here yet. (When writes unblock in
   v2, the rule becomes: show the user, get a yes, then execute.)

Don't dump raw search JSON at the user. Summarize: who, when, subject,
and the one-line gist. Offer the obvious next step ("want me to open the
Acme one?" / "want me to reply?").

## Gmail search syntax — the queries you'll actually use

`gmail_search` uses the same syntax as the Gmail search bar. The high-value
operators:

- `from:sarah@acme.com` — sender
- `to:me` — addressed to the user
- `subject:invoice` — subject contains
- `is:unread` / `is:read` / `is:important` / `is:starred`
- `newer_than:2d` / `older_than:1w` — relative time (d/w/m/y)
- `has:attachment`
- `label:Receipts`
- combine freely: `from:legal is:unread newer_than:3d`

Examples:

```
"unread email from my boss this week"
  → connector_execute(connector_name="gmail", action="gmail_search",
      params={"query": "from:boss@company.com is:unread newer_than:7d"})

"the latest invoice from Acme"
  → connector_execute(connector_name="gmail", action="gmail_search",
      params={"query": "from:acme.com subject:invoice", "max_results": 5})
  → then connector_execute(action="gmail_read",
      params={"message_id": <most recent id>})
```

## Sending mail (v1: blocked)

`gmail_send` is a confirm-trust write, so it is **blocked in v1** —
`connector_execute` will refuse it. You can still help the user prepare:

1. **Draft the full message in chat** — recipient, subject, and body.
2. **Show it to the user verbatim.**
3. Tell them sending from chat isn't available yet (coming in v2); they can
   copy the draft into Gmail.

For a reply, you can still `connector_execute(action="gmail_read", ...)` the
original so your draft has the right context (quote sparingly, match the
thread's subject).

Never invent recipients. If the address is ambiguous, search for the
person's recent mail to confirm the right one, or ask.

## Triage: labels and trash (v1: writes blocked)

Reading labels is allowed; changing them is a blocked write in v1.

- `gmail_list_labels` (read) is fine — run it via `connector_execute` if you
  need to see the mailbox's labels.
- `gmail_modify` / `gmail_batch_modify` / `gmail_trash` / `gmail_create_label`
  are confirm-trust writes and are **blocked in v1**. When the user asks to
  archive, label, trash, or create a label, explain it's not available from
  chat yet (coming in v2) rather than attempting it.

## A quick read on the inbox

`gmail_summary` (read) returns unread count and today's count cheaply — run it
via `connector_execute(connector_name="gmail", action="gmail_summary")` when
the user asks "how's my inbox" or "anything new" before a heavier search.

## Render results as a live pocket (the Ripple fusion)

Summarizing in chat is the floor, not the ceiling. When a search returns a
**set of messages**, don't stop at a bulleted list and don't paste raw JSON.
Render it as **typed Ripple widgets** on the canvas: a stack of inbox cards the
user can scan, a filter to narrow them, a count. Cards beat ten lines of text.

The flow is: **`connector_execute` → build a typed rippleSpec → merge it into a
pocket.**

1. Run the read action (`gmail_search`, then `gmail_read` if you need bodies).
2. Shape the returned stubs into a small state array (see value/label below).
3. Deliver a typed spec to a pocket. In a room you do this the normal way —
   the `pocketpaw-create-pocket` skill for a fresh canvas, or
   `pocketpaw-edit-pocket` to add to the one already open. Both apply the spec
   through the merge endpoint **`POST /api/v1/pockets/<id>/spec/merge`**; the
   pocket specialist subagent is what actually posts it (see the
   `pocketpaw-pocket-specialist` skill for the HTTP mechanics and the
   merge-vs-replace rule). The payload below is exactly what lands on that
   wire — copy the shape.

### Two hard rules

**1. Typed widgets ONLY — never raw HTML.** A subject line, a sender name, an
email body, a snippet — every string Gmail returns is
**attacker-controllable**. Anyone can email the user a subject full of
`<script>`. So email strings go ONLY into typed-widget props (`text`, `badge`,
`data-grid` cell values) where the Ripple renderer escapes them. NEVER assemble
an HTML string from message content, and NEVER route it into an `embed`
(`mode: "srcdoc"`) node or any other HTML sink. There is no "html" widget to
reach for — the merge endpoint's catalog gate rejects any node whose `type`
isn't a known widget — but treat this as a security invariant you never try to
route around, not just a validator you happen to trip. Rendering raw email HTML
is a stored-XSS / injection vector.

**2. value/label split.** Machine ids are lowercase and live in the id slot;
human-facing text lives in the label. The `select` filter below has options
`{"value": "unread", "label": "Unread"}` and the **bound state holds the
value** (`"unread"`), never the label (`"Unread"`). A `status-dot`'s `variant`
is a lowercase status id (`"info"`, `"neutral"`) while its `label` carries the
human text. Same rule that governs `data-grid` column keys and kanban column
ids: store `"unread"`, not `"Unread"`. Get it backwards and the filter matches
nothing.

### Worked example — "show my recent inbox" → live cards

Read, then merge. First the read:

```
connector_execute(connector_name="gmail", action="gmail_search",
  params={"query": "in:inbox newer_than:2d", "max_results": 10})
```

Then shape each stub into a message object and merge a filtered card list. This
is the copyable `/spec/merge` payload:

```json
{
  "merge": {
    "state": {
      "filter": "all",
      "inbox": [
        {"id": "18f2a1", "from": "Sarah Chen", "subject": "Q3 planning notes",
         "snippet": "Pulled together the notes from Tuesday…",
         "status": "unread", "dot": "info"},
        {"id": "18f0c9", "from": "Acme Billing", "subject": "Invoice #4471",
         "snippet": "Your July invoice is ready to view…",
         "status": "read", "dot": "neutral"}
      ]
    },
    "ui": {
      "id": "n_inboxroot",
      "type": "flex",
      "props": {"direction": "column", "gap": "10px"},
      "children": [
        {"id": "n_inboxhdr", "type": "page-header",
         "props": {"title": "Recent inbox"}},
        {
          "id": "n_inboxfilter",
          "type": "select",
          "bind": "filter",
          "props": {
            "options": [
              {"value": "all",    "label": "All"},
              {"value": "unread", "label": "Unread"}
            ]
          }
        },
        {
          "type": "each",
          "items": "{state.inbox}",
          "item_as": "msg",
          "children": [
            {
              "type": "card",
              "props": {"variant": "outlined", "density": "compact"},
              "children": [
                {
                  "type": "flex",
                  "props": {"direction": "row", "gap": "8px", "align": "center"},
                  "children": [
                    {"type": "status-dot",
                     "props": {"variant": "{msg.dot}", "label": "{msg.from}", "size": 8}},
                    {"type": "badge",
                     "props": {"text": "{msg.status}", "variant": "outline"}}
                  ]
                },
                {"type": "text", "props": {"text": "{msg.subject}", "weight": "bold"}},
                {"type": "text",
                 "props": {"text": "{msg.snippet}", "size": "sm", "color": "muted"}}
              ]
            }
          ]
        },
        {
          "id": "n_inboxrefresh",
          "type": "button",
          "props": {
            "label": "Refresh",
            "on_click": [
              {
                "action": "invoke_tool",
                "tool": "connector_execute",
                "args": {
                  "connector_name": "gmail",
                  "action": "gmail_search",
                  "params": {"query": "in:inbox newer_than:2d", "max_results": 10}
                },
                "on_success": [{"action": "set", "target": "inbox"}],
                "on_error": [{"action": "toast",
                  "message": "Couldn't refresh the inbox", "variant": "error"}]
              }
            ]
          }
        }
      ]
    }
  }
}
```

Note the `status` field and the `select` option `value`s are lowercase ids
(`"unread"`, `"read"`) while every human-facing string lives in a `label` or a
`text`/`badge` prop — that IS the value/label split, and it's what lets the
filter match. Nodes inside the `each` loop carry no `id` (they're templated
per-item); only top-level nodes get ids. The **Refresh** button wires a
follow-up action back to the sense: `invoke_tool` re-runs `connector_execute`
through the pocket's tool-run wire and `on_success` writes the fresh result into
`inbox`. (`set` with no `value` falls back to the tool result payload — make
sure the sense returns the same message shape the cards bind to, or add a
mapping step. If the tool-run wire isn't enabled for the pocket, drop the button
and just re-run the search yourself and merge again — an agent-driven refresh.)

Remember the read-before-you-act rule still holds: the snippet in a search stub
is enough for a card, but `gmail_read` the message before quoting its body.

### This does not replace the static widget-recipes rail

Gmail also ships **pre-baked widget recipes** — the three default home widgets
(Email Stats, etc.) surfaced in the Add-Widget picker's "From connectors" rail
(`GET /api/v1/cloud/connectors/widget-recipes`), which a user can drop onto a
home dashboard with no agent involved. That no-agent fallback stays exactly
as-is. The convention here is the **agent-driven** path: you render a bespoke,
live-typed view in response to what the user actually asked for. Use both —
they don't overlap.

## Guardrails

- **Writes are blocked in v1.** Read actions (search, read, list labels,
  summary) run via `connector_execute`. Confirm-trust actions (send, trash,
  label changes, create label) are refused by the tool with a "needs approval
  — coming in v2" message — never claim a blocked action succeeded.
- **Read the message before quoting or replying** — never paraphrase from
  a search snippet alone for anything that matters.
- **Cap result volume** — `max_results` is capped at 20; for broad
  triage, search tightly rather than pulling everything.
- **Stay in this mailbox** — the connector is bound to one account; don't
  assume access to other inboxes.
