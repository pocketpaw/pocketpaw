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

# Gmail in a Room

This room has the **Gmail connector** bound to it. You can search, read,
triage, and send email on the user's behalf through the connector's
actions. The connector handles OAuth, MIME, and the Gmail API — you call
named actions with simple parameters and get structured results back.

Treat the mailbox as the user's real inbox. Reading is cheap and safe;
**sending and destroying are not**. Default to caution.

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

"Trust = confirm" means **show the user what you're about to do and get a
yes before you do it.** Never send, trash, or relabel silently.

## Core workflow: read before you act

Almost every request starts with a search, then a read.

1. **Search** to find the candidate messages. `gmail_search` takes a
   `query` (Gmail's own search syntax) and an optional `max_results`
   (default 5, capped at 20). It returns a list of message stubs, each
   with an `id`, `from`, `subject`, and snippet.
2. **Read** the specific message with `gmail_read` (pass the `message_id`
   from the search result) when you need the full body — to summarize,
   quote, or draft a reply.
3. **Act** (send / label / trash) only after you've shown the user what
   you found and they've confirmed the action.

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
  → gmail_search query="from:boss@company.com is:unread newer_than:7d"

"the latest invoice from Acme"
  → gmail_search query="from:acme.com subject:invoice" max_results=5
  → then gmail_read on the most recent id
```

## Sending mail

`gmail_send` takes `to`, `subject`, and `body` (plain text). Before you
call it:

1. **Draft the full message in chat** — recipient, subject, and body.
2. **Show it to the user verbatim** and ask for explicit confirmation.
3. Only on a clear yes, call `gmail_send`.

For a reply, first `gmail_read` the original so your draft has the right
context (quote sparingly, match the thread's subject). If the user edits
your draft, re-show the final version before sending.

Never invent recipients. If the address is ambiguous, search for the
person's recent mail to confirm the right one, or ask.

## Triage: labels and trash

- `gmail_list_labels` first if you need a label's id — labels are
  referenced by id, not name, in `gmail_modify` / `gmail_batch_modify`.
- `gmail_modify` changes labels on one message; `gmail_batch_modify`
  takes a `message_ids` list for bulk relabeling (e.g. "archive all the
  newsletters" → search, then batch-modify removing `INBOX`).
- `gmail_trash` is reversible (Trash, not permanent delete) but still a
  destructive action — confirm the specific message first.
- `gmail_create_label` when the user asks for a label that doesn't exist
  yet; confirm the name.

## A quick read on the inbox

`gmail_summary` returns unread count and today's count cheaply — use it
when the user asks "how's my inbox" or "anything new" before doing a
heavier search.

## Guardrails

- **Confirm every `confirm`-trust action** (send, trash, label changes,
  create label). Read-only actions (search, read, list labels, summary)
  need no confirmation.
- **Read the message before quoting or replying** — never paraphrase from
  a search snippet alone for anything that matters.
- **Cap result volume** — `max_results` is capped at 20; for broad
  triage, search tightly rather than pulling everything.
- **Stay in this mailbox** — the connector is bound to one account; don't
  assume access to other inboxes.
