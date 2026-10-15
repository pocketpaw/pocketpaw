---
name: pocketpaw-site
description: |
  Build a NEW Paw Site. Every new site is a repo started from a paw-sites
  template: `static` (hand-written HTML and CSS, no build step) by default, or a
  framework template (Astro, TanStack Start, Vite + React + Hono, Next,
  SvelteKit) when the site needs server logic, user accounts or its own
  database, or the user names a framework. This skill is the platform contract:
  the build loop, integrations (paw.js and the gateway), secrets and plan rules.
  Framework and library knowledge comes from the template's AGENTS.md and the web.
---

# Build a site

Every tool below is on `mcp__pocketpaw_sites_manager__`. A site is a repo of
files you edit. There is no rippleSpec, no pocket specialist and no
`create_*_site` tool.

## Pick the template

Call `list_site_templates` and match `when_to_use`.
- `static` is the default: landing and marketing pages, portfolios, brochures,
  event pages, a few pages with no backend. No build, instant preview.
- A framework template only when the site needs server logic, user accounts or
  its own database, or the user named the framework. The named framework wins,
  then `when_to_use`.

## The loop

1. `start_site_from_template(slug, brief, name?)` ONCE. It returns `pocket_id`
   and the repo's AGENTS.md. Never call it again for a change: that makes a
   second site.
2. Follow AGENTS.md over your habits: where pages, data, auth and tokens go, and
   which files are generated. `read_site_file` it again after a recipe adds a
   section.
3. Recipes (framework templates): `list_site_recipes` (pass `template`), then
   `apply_site_recipe` for each one the app needs, `requires` first. On a
   conflict nothing was written: fix the files it names and apply again. Do its
   `glue_tasks` in order.
4. Connect integrations and request secrets (below).
5. Design before the first UI file: run the research gate, state the locked
   direction in one line, load `pocketpaw-design-taste`, then retheme the tokens
   file AGENTS.md names with a real palette and font pairing. Never ship the
   starter greys.
6. Files: `list_site_files`, `read_site_files` before you change one,
   `patch_site_file` with exact-once `{old, new}` blocks, `write_site_files` for
   new or rewritten files, `delete_site_files`. Paths are from the repo root.
7. `run_site_build` (instant on `static`). On `failed`, read the log tail (or
   `get_site_build_log`), fix it, build again: three rounds at most, then tell
   the user what still fails.
8. `verify_site` once at the end; call the site ready only on `passed`.
   `preview_site` to look at it. If `preview_mode` is `static`, say server
   routes run after publish.
9. Everything saves to the DRAFT. Call `publish` only when the user asks; it
   deploys the last build, so build first.

## Integrations

Services are connected, never keyed. `list_site_integrations` lists the
providers; `connect_site_integration(pocket_id, provider, purpose)` puts a
Connect card in chat (built-ins like `newsletter` are on at once). Its reply
says how to wire that provider: follow it. Never ask for an API key and never
use `request_site_secret` for one.

Client, any page. The platform injects paw.js at preview and publish when a page
uses it; never add the script yourself.
- Buy button: `<button data-paw-checkout="<catalog product id>">` (Stripe;
  `data-qty` for a fixed quantity). The price comes from the catalog, so a
  printed price is display only.
- Signup: `<form data-paw-subscribe>` with `<input type="email" name="email">`.
- Anything else: `Paw.call(provider, action, args)` in a small script; set text
  with `textContent`, never `innerHTML`.

Server, framework templates: apply the `paw-integrations` recipe, then call
`pawCall(env, provider, action, args)` from server code. The platform sets
`env.PAW_GATEWAY_TOKEN`, `PAW_API_BASE` and `PAW_SITE_ID`; never request them.

## Secrets

For each secret NAME a recipe returns, call
`request_site_secret(pocket_id, name, description)`; `list_site_secrets` shows
which are set. If those tools are missing, tell the user which names to set.
Never write a secret value into any file (`.env`, `.dev.vars`, wrangler.jsonc,
source): only names go in `.dev.vars.example`.

## Plans

A plan refusal (from a tool, or a recipe above the site's plan) means the site
needs a higher plan. Tell the user; never work around it.

## Framework and library knowledge

AGENTS.md first. For a framework API, a library, or a provider's standard embed
markup, search the web and fetch the current docs instead of guessing.

## Do not

- Edit generated files AGENTS.md names, or write under `_paw/`.
- Put resource ids or secret values in wrangler.jsonc or any file.
- Report the site as ready before `run_site_build` returns `built` and
  `verify_site` returns `passed`.
