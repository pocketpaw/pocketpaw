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
