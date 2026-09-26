---
name: github
description: |
  Work with a GitHub repo, org, or account inside a pocket/room that has the
  GitHub connector bound. Invoke when the user wants to read from GitHub in the
  chat — "list the open issues on acme/api", "what PRs are waiting on review",
  "show me the latest release", "search the org for where we set the timeout",
  "is the build green". This skill teaches the GitHub read surface (issues, PRs,
  repos, code/issue search, Actions runs, releases) and the read-first workflow:
  you can read freely; writing (opening issues) needs approval and is blocked in
  v1. It is auto-surfaced into a room when the GitHub connector is bound to that
  pocket, so you only see it when GitHub is actually available.
---

<!--
  Updated: 2026-07-16 (feat/senses-render-convention / SR-8) — added the
  "Render results as a live pocket" section (the Ripple fusion): after a
  connector_execute read, render the result as TYPED Ripple widgets pushed to
  the pocket via POST /api/v1/pockets/<id>/spec/merge, instead of pasting raw
  JSON into chat. Documents the two hard rules (typed widgets only / never raw
  HTML from attacker-controllable connector payloads; value/label split), plus a
  worked example (open PRs -> data-grid with a follow-up Refresh button wired
  back via invoke_tool -> connector_execute). The static widget-recipes
  home-rail fallback is untouched; the read surface is otherwise unchanged.
-->

# GitHub in a Room

This room has the **GitHub connector** bound to it. You can read repositories,
issues, pull requests, releases, and CI runs, and search code and issues — all
on the user's behalf through the connector's actions. The connector holds the
GitHub token and talks to the GitHub REST API; you call named actions with
simple parameters and get structured JSON back.

You reach those actions through two tools:

- **`list_connector_actions`** — call this FIRST. It lists every connector bound
  to this pocket and the actions you can run. For GitHub it returns the read
  actions you may execute and the write actions that are blocked in v1.
- **`connector_execute(connector_name, action, params)`** — run one action. For
  GitHub, `connector_name` is `"github"`.

Reading is cheap and safe. **Writing is not** — and in v1 it is blocked
entirely (see Guardrails). Default to reading.

## The action surface

| Action | What it does | Trust |
|--------|--------------|-------|
| `list_repos` | List the authenticated user's repos | auto (read) |
| `list_org_repos` | List an org's repos (`org`) | auto (read) |
| `list_issues` | List a repo's issues (`owner`, `repo`, `state`) | auto (read) |
| `list_pull_requests` | List a repo's PRs (`owner`, `repo`) | auto (read) |
| `search_code` | Search code (`q`) | auto (read) |
| `search_issues` | Search issues/PRs (`q`) | auto (read) |
| `get_repo` | One repo's detail (`owner`, `repo`) | auto (read) |
| `list_actions_runs` | Recent Actions runs (`owner`, `repo`) | auto (read) |
| `list_releases` | A repo's releases (`owner`, `repo`) | auto (read) |
| `create_issue` | Open a new issue | confirm (**blocked in v1**) |

"Trust = auto" actions you may run directly. `create_issue` is "confirm" trust —
a write — and `connector_execute` will **refuse** it with a "needs approval —
coming in v2" message. Don't try to route around that; tell the user it's not
available yet.

## Core workflow: list, then read

Almost every request is: figure out the action, run it, summarize.

1. **Orient** — call `list_connector_actions` to confirm `github` is bound and
   to see the exact action names.
2. **Execute** the right read action with `connector_execute`. Most repo actions
   need `owner` and `repo` (e.g. `acme` / `api` for `acme/api`). Parse these
   from how the user names the repo ("acme/api" → owner `acme`, repo `api`).
3. **Summarize** — don't dump raw JSON. Give the user the issue numbers +
   titles, the PR titles + authors + review state, the release tag + date.
   Offer the obvious next step ("want the full body of #142?").

## Concrete examples

List the open issues on a repo:

```
connector_execute(
  connector_name="github",
  action="list_issues",
  params={"owner": "acme", "repo": "api", "state": "open"}
)
```

List the pull requests waiting on a repo:

```
connector_execute(
  connector_name="github",
  action="list_pull_requests",
  params={"owner": "acme", "repo": "api", "state": "open"}
)
```

Get one repo's detail (stars, default branch, description):

```
connector_execute(
  connector_name="github",
  action="get_repo",
  params={"owner": "acme", "repo": "api"}
)
```

Search code across an org for where something is configured:

```
connector_execute(
  connector_name="github",
  action="search_code",
  params={"q": "timeout filename:config.yaml org:acme"}
)
```

Search issues by query (GitHub's issue search syntax):

```
connector_execute(
  connector_name="github",
  action="search_issues",
  params={"q": "repo:acme/api is:open label:bug"}
)
```

Check the latest CI runs or releases:

```
connector_execute(connector_name="github", action="list_actions_runs",
  params={"owner": "acme", "repo": "api"})
connector_execute(connector_name="github", action="list_releases",
  params={"owner": "acme", "repo": "api"})
```

## Search syntax — the operators you'll actually use

`search_code` and `search_issues` use GitHub's own query syntax:

- `repo:owner/name` — scope to one repo
- `org:name` / `user:name` — scope to an org or user
- `is:open` / `is:closed` / `is:pr` / `is:issue`
- `label:bug` / `author:octocat` / `assignee:octocat`
- `filename:config.yaml` / `extension:py` / `path:src/`
- `in:title` / `in:body` / `in:comments`

Combine freely: `repo:acme/api is:open is:pr review:required`.

## Pagination and volume

The list actions take `per_page` (issues default 25, most others 10–100) and
`page`. Don't pull everything — for "the open issues" the first page is usually
enough. If the user wants a count or asks for "all", say how many the first page
returned and offer to page further rather than fetching every page silently.

## Render results as a live pocket (the Ripple fusion)

Summarizing in chat is the floor, not the ceiling. When the result is a
**set of records** — open PRs, issues, releases, CI runs — don't stop at a
bulleted list and don't paste raw JSON. Render it as **typed Ripple widgets**
on the canvas: a live table the user can sort, cards they can scan, a chart of
counts. A table of PRs beats ten lines of text.

The flow is: **`connector_execute` → build a typed rippleSpec → merge it into a
pocket.**

1. Run the read action (you already know how — see the examples above).
2. Shape the returned records into a small state array (see value/label below).
3. Deliver a typed spec to a pocket. In a room you do this the normal way —
   the `pocketpaw-create-pocket` skill for a fresh canvas, or
   `pocketpaw-edit-pocket` to add to the one already open. Both apply the spec
   through the merge endpoint **`POST /api/v1/pockets/<id>/spec/merge`**; the
   pocket specialist subagent is what actually posts it (see the
   `pocketpaw-pocket-specialist` skill for the HTTP mechanics and the
   merge-vs-replace rule). The payloads below are exactly what lands on that
   wire — copy the shape.

### Two hard rules

**1. Typed widgets ONLY — never raw HTML.** A PR title, an issue body, a repo
description, a committer name — every string a connector returns is
**attacker-controllable**. Anyone can open a PR titled `<img src=x
onerror=...>` on a public repo. So connector strings go ONLY into typed-widget
props (`text`, `badge`, `data-grid` cell values, `stat`) where the Ripple
renderer escapes them. NEVER assemble an HTML string from connector data, and
NEVER route it into an `embed` (`mode: "srcdoc"`) node or any other HTML sink.
There is no "html" widget to reach for — the merge endpoint's catalog gate
rejects any node whose `type` isn't a known widget — but treat this as a
security invariant you never try to route around, not just a validator you
happen to trip. Raw HTML in the render path is a stored-XSS / injection vector.

**2. value/label split.** Machine ids are lowercase and live in the id slot;
human-facing text lives in the label. A `data-grid` column is
`{"key": "<lowercase field id>", "label": "<header text>"}` — `key` matches the
row-object field, `label` is what the header shows. A `select` filter's options
are `{"value": "<lowercase id>", "label": "<text>"}` and the **bound state holds
the value**, never the label. Same rule that governs kanban column ids: store
`"open"`, not `"Open"`. Get it backwards and rows silently fail to resolve.

### Worked example — "show my open PRs" → a live table

Read, then merge. First the read:

```
connector_execute(connector_name="github", action="list_pull_requests",
  params={"owner": "acme", "repo": "api", "state": "open"})
```

Then shape each PR into a row and merge a `data-grid`. This is the copyable
`/spec/merge` payload:

```json
{
  "merge": {
    "state": {
      "pr_rows": [
        {"number": 142, "title": "Fix timeout in the retry loop",
         "author": "octocat", "reviews": "changes_requested"},
        {"number": 139, "title": "Paginate the search endpoint",
         "author": "hubot", "reviews": "approved"}
      ]
    },
    "ui": {
      "id": "n_prroot01",
      "type": "flex",
      "props": {"direction": "column", "gap": "12px"},
      "children": [
        {"id": "n_prhdr01", "type": "page-header",
         "props": {"title": "Open PRs — acme/api"}},
        {
          "id": "n_prgrid01",
          "type": "data-grid",
          "props": {
            "columns": [
              {"key": "number",  "label": "#",      "align": "right", "width": "70px"},
              {"key": "title",   "label": "Title",  "sortable": true, "align": "left"},
              {"key": "author",  "label": "Author", "align": "left"},
              {"key": "reviews", "label": "Review", "align": "left"}
            ],
            "rows": "{state.pr_rows}",
            "dense": true
          }
        },
        {
          "id": "n_prrefresh01",
          "type": "button",
          "props": {
            "label": "Refresh",
            "on_click": [
              {
                "action": "invoke_tool",
                "tool": "connector_execute",
                "args": {
                  "connector_name": "github",
                  "action": "list_pull_requests",
                  "params": {"owner": "acme", "repo": "api", "state": "open"}
                },
                "on_success": [{"action": "set", "target": "pr_rows"}],
                "on_error": [{"action": "toast",
                  "message": "Couldn't refresh PRs", "variant": "error"}]
              }
            ]
          }
        }
      ]
    }
  }
}
```

Note the column `key`s (`number`, `title`, `author`, `reviews`) match the row
fields exactly and are lowercase; the `label`s carry the human text — that IS
the value/label split. The **Refresh** button wires a follow-up action back to
the sense: `invoke_tool` re-runs `connector_execute` through the pocket's
tool-run wire and `on_success` writes the fresh result into `pr_rows`, so the
grid updates without a chat round-trip. (`set` with no `value` falls back to the
tool result payload — make sure the sense returns the same row shape the columns
bind to, or add a mapping step. If the tool-run wire isn't enabled for the
pocket, drop the button and just re-run the read yourself and merge again — an
agent-driven refresh.)

For a second display kind: to show PR **counts by review state** instead of the
list, merge a `chart` (`{"type": "chart", "props": {"type": "bar", "data":
"{state.review_counts}", "title": "PRs by review state"}}`) over a
`review_counts` array you tally from the same read.

### This does not replace the static widget-recipes rail

The connector also ships **pre-baked widget recipes** — the "From connectors"
rail in the Add-Widget picker (`GET /api/v1/cloud/connectors/widget-recipes`),
which lets a user drop a default GitHub widget onto a home dashboard with no
agent involved. That no-agent fallback stays exactly as-is. The convention here
is the **agent-driven** path: you render a bespoke, live-typed view in response
to what the user actually asked for. Use both — they don't overlap.

## Guardrails

- **Reads are free; writes are blocked in v1.** Every action in the table marked
  "auto" you may run. `create_issue` (and any future write) is refused by
  `connector_execute` with a "needs approval — coming in v2" message. When the
  user asks to open an issue, comment, merge, or label, tell them GitHub writes
  aren't available from chat yet — don't pretend it worked.
- **The connector needs a token.** Reads only work if the GitHub connector has a
  personal access token in its config. If `connector_execute` returns an auth
  error, tell the user to add a GitHub token to the connector's config (the
  connector is bound to this pocket but has no credential yet).
- **Parse `owner`/`repo` carefully.** "acme/api" → owner `acme`, repo `api`. If
  the user names a repo ambiguously, ask or use `list_repos` / `list_org_repos`
  to find it rather than guessing.
- **Summarize, don't dump.** Turn raw API JSON into a short, scannable answer
  (numbers, titles, authors, dates) and offer the obvious follow-up.
- **Stay in this account.** The connector is bound to one token's access; don't
  assume reach into private repos the token can't see.
