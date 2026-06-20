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
