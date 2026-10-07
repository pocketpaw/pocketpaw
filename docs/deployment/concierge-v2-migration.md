<!-- Operator runbook for moving existing Paw Bar concierges from the legacy agent
     runtime to v2, and for opening the eval gate that allows it. -->
# Concierges: legacy → v2

Every Paw Bar concierge runs on one of two runtimes, stored per site as
`Site.concierge_runtime`. `legacy` answers through the site's concierge agent and
the full agent machinery. `v2` answers in one tool-free model call grounded in the
site's knowledge and catalog. See `docs/concepts/concierge-knowledge.mdx` for both.

A deployment asks for v2 by default (`POCKETPAW_PAWBAR_CONCIERGE_DEFAULT_RUNTIME`,
default `v2`; set it to `legacy` to opt out). The request only takes effect once the
eval gate is open, and the gate is the committed report, not the setting.

## Opening the gate

The gate opens when `ee/pocketpaw_ee/paw_bar/concierge_eval_gate.json` holds a real
model run, for the exact model the deployment is configured with, that clears every
threshold. Run the eval against that model, then promote the report:

```bash
# The model is POCKETPAW_PAWBAR_CONCIERGE_MODEL, or POCKETPAW_PYDANTIC_AI_MODEL when
# that is empty. A litellm: spec also needs POCKETPAW_LITELLM_API_BASE and
# POCKETPAW_LITELLM_API_KEY for the gateway.
uv run python -m tests.evals.concierge.run --real
uv run python -m tests.evals.concierge.run --promote tests/evals/concierge/reports/<file>-real.json
```

`--promote` refuses a report that is recorded, for another model, or under any
threshold. Commit the gate file in a PR; it ships inside the image. Changing the
deployment's concierge model shuts the gate again until a report for the new model
is promoted.

The gate covers the deployment's model and only decides the automatic move. Once a
site is on v2, a model its owner sets on the concierge agent is used at runtime
without a report of its own (see `docs/concepts/concierge-knowledge.mdx`).

## Do I need to run the move? No, it runs itself

`init_cloud_db` calls `sites.migrate_concierge_v2.migrate_on_boot()` on every cloud
start, right after the concierge-marker backfill. While the gate is shut it does
nothing. Once it is open, the next start moves every legacy concierge v2 can serve,
and logs each site it kept on legacy with the reason. It never blocks a boot: any
error is logged and every site keeps the runtime it had.

`backend` and `worker` both boot it. That is safe: each write repeats the "still
legacy" guard, so the second container finds nothing to do.

## Running it by hand

```bash
python -m pocketpaw_ee.sites.migrate_concierge_v2 --dry-run
python -m pocketpaw_ee.sites.migrate_concierge_v2
```

It reads `CLOUD_MONGODB_URI` (falling back to `POCKETPAW_MONGO_URL`), like the other
migrations, and the paw-bar store on the data volume. It is idempotent.

## What it moves, and what it keeps on legacy

It looks only at sites that have a concierge (`concierge_created_at` set) and are on
`legacy`. Sites with no concierge are left alone: the create route asks the gate
when one is made. A moved site gets `concierge_runtime: "v2"` and nothing else. Its
agent stays bound (the frame's conversation starters and the agent-scoped knowledge
v2 reads both come from it), and the owner can switch back from the settings.

A site stays on legacy, with the reason logged, when:

- **Its bar declares an action other than `add_to_cart` / `checkout`.** Legacy hands
  each declared verb to the agent as a tool that raises an Instinct proposal when
  the agent decides to. v2 has no tools; it can only offer such a verb as a form the
  visitor fills in, and one with no arguments not at all.
- **Its bar is bound to an agent other than the site's dedicated one**
  (`concierge-<site_id>`), which means an owner picked it by hand.
- **The owner changed the dedicated agent**: system prompt, model, tools, scopes,
  skills, plugins, a persona other than the generated one, or disabled it. v2 uses
  a fixed frame and no tools, so most of those would be lost. v2 would honour a
  changed model, but the gate only certified the deployment's model, so the move
  leaves that choice to the owner.
- **It has no bar, no bound agent, or an agent that no longer exists.** Those bars
  don't answer on legacy today, and moving one would switch it on unasked.
- **It could not be read.** It is retried on the next run.

Connectors are not a reason: a legacy concierge refuses every turn while its pocket
has one, so no live bar depends on them.

Skipped sites are checked again on every start, so one moves once its owner removes
what kept it.

## Verifying

```
INFO concierge v2 move dry run: 42 legacy concierge(s) examined, 37 would move, 5 kept on legacy
INFO concierge v2 move: kept site 6a1f… on legacy: declares server-run actions: book_table
```

A shut gate logs `the v2 gate is shut; nothing to do`. A second real run reports
`0` moved and the same skipped sites.
