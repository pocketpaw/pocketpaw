---
name: ship
description: |
  Deploy and run apps on real infrastructure from inside a pocket/room that has
  the Ship connector bound. Invoke when the user wants something built to
  actually RUN — "deploy this app", "put it on a server", "point my domain at
  it", "give it a database", "why is it down", "show me the logs", "how much is
  this box costing". This skill teaches the /ship verb surface (provision a box,
  register and deploy an app, route a domain with TLS, attach a database, read
  logs and metrics) and the safety rule that governs it: reads and reversible
  writes run immediately, but tearing anything down only ever files a proposal
  for a human to approve. Auto-surfaced into a room when the Ship connector is
  bound to that pocket, so you only see it when /ship is actually available.
---

# Shipping from a Room

This room has the **Ship connector** bound. You can provision servers, deploy
apps onto them, route domains, attach databases, and read what's happening —
all through the `ship_*` tools. The connector holds this workspace's
infrastructure credential; you never see it and never need it.

Everything you do here happens on **real infrastructure that costs real money
and serves real traffic.** That shapes how you work.

## The two halves of this surface

**Things you just do.** Reads and reversible writes run immediately:

| Tool | What it does |
|------|--------------|
| `ship_list_boxes` | The workspace's servers — provider, IP, status, monthly price |
| `ship_provision_box` | Create a new server (boots in the background) |
| `ship_list_apps` | Apps registered on the boxes (`box_id` filters) |
| `ship_create_app` | Register an app on a box |
| `ship_deploy_app` | Deploy an app's image (see the caveat below) |
| `ship_add_domain` | Route a domain to an app and issue TLS |
| `ship_create_db` | Attach a database (postgres, redis, or mongo) and link it to an app |
| `ship_set_scale` | Set how many containers run per process type (`web=2 worker=1`; `0` stops one) |
| `ship_set_checks` | Toggle zero-downtime deploy checks and set the healthcheck path |
| `ship_logs` | An app's recent log lines |
| `ship_metrics` | A box's live CPU / memory / disk |

**Things you may only propose.** `ship_request_destroy` does **not** destroy
anything. It files the teardown in The Tray and returns
`{status: "proposed", proposal_id}`. A human approves it; only then does
anything get torn down. The same applies when you deploy to an app flagged
**production** — `ship_deploy_app` returns `status: "proposed"` instead of
deploying.

## The rule that matters most

**Never tell the user something was destroyed, deleted, or deployed to
production when the tool returned `proposed`.**

Say what actually happened:

> I've filed a request to tear down the `staging` box. It's waiting for your
> approval in The Tray — nothing has been removed yet.

Not "I've deleted the box." The proposal may sit for hours, or be rejected. A
user who believes a teardown already happened will make decisions on a false
picture of their infrastructure.

The same honesty applies to failures. If a deploy comes back failed, say so and
show the log lines — don't summarize a broken deploy as "shipped".

## The normal flow

Provisioning a box and getting an app live:

1. **`ship_list_boxes`** — reuse a `ready` box if one exists. Don't provision a
   second server when the workspace already has capacity; each one bills
   monthly.
2. **`ship_provision_box`** if you genuinely need one. It returns immediately
   with status `provisioning`; the server boots and installs its deploy engine
   in the background. Poll `ship_list_boxes` until it reads `ready` — deploying
   to a box that isn't ready will fail.
3. **`ship_create_app`** with a lowercase-alphanumeric name (hyphens allowed)
   and the `image` you want to run.
4. **`ship_deploy_app`** — returns immediately; the deploy runs in the
   background. Poll `ship_list_apps` and watch the app's status walk
   `deploying → live`, or `failed`.
5. **`ship_add_domain`** to put it on a real hostname with TLS.
6. **`ship_create_db`** if it needs one. Pick the engine with `db_type` —
   `postgres`, `redis`, or `mongo` (defaults to mongo). You get back the service
   name and the **name** of the environment variable holding the connection
   string — never the credential itself. The app reads it from its own
   environment; you don't need the value and shouldn't ask for it.
7. **`ship_set_scale`** / **`ship_set_checks`** to tune how it runs. Scale gives
   a process type more containers (`{"web": 2, "worker": 1}`; `0` stops one);
   checks turn on zero-downtime deploys (Dokku settles and drains the old
   container before cutting over) and set the HTTP healthcheck path. Both apply
   to the next deploy — set checks before a production deploy, not after.

## Guardrails

- **Check before you create.** Boxes cost money per month. List first, reuse
  what's there, and tell the user what a new box will cost when you provision
  one (`price_monthly` comes back on every box).
- **Deploys are backgrounded.** A tool returning successfully means the work was
  *accepted*, not finished. Poll before reporting a result. "Deploy queued" and
  "app is live" are different claims.
- **Read the logs before guessing.** When something is down, `ship_logs` and
  `ship_metrics` beat speculation. A box at 98% disk explains more than a theory
  about the framework.
- **Secrets stay out of the conversation.** Database connection strings, SSH
  keys, and API tokens never come back through these tools, by design. If a user
  pastes one into the chat, don't echo it back.
- **One thing at a time on production.** Sequential deploys, verified between
  steps. Don't fan out changes across several live apps in one turn.

## When you're not sure

Ask. "Should this go on the existing box or a fresh one?" costs one turn.
Provisioning a server nobody wanted, or tearing down the wrong app, costs
considerably more — and the teardown proposal you file is a request for exactly
that kind of judgement. Make it easy to answer: say what you're proposing to
destroy, and why.
