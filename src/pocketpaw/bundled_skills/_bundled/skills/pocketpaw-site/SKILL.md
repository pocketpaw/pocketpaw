---
name: pocketpaw-site
description: |
  Build a NEW Paw Site: a repo started from a paw-sites template, `static`
  (no build) for content pages or a framework template (Astro, TanStack Start,
  Vite + React + Hono, Next, SvelteKit) for apps, games, npm packages, server
  logic or a named framework. The platform contract: the build loop,
  integrations (paw.js and the gateway), secrets and plan rules.
---

# Build a site

Every tool below is on `mcp__pocketpaw_sites_manager__`. A site is a repo of
files you edit: no rippleSpec, pocket specialist or `create_*_site` tool.

## Pick the template

Call `list_site_templates` and match `when_to_use`.
- `static` only for content pages: landing, marketing, portfolio, brochure,
  event or waitlist pages, a little vanilla JS. No npm packages, no bundler.
- A framework template when ANY hold: the site is an app, not a page (a game,
  3D/WebGL or canvas, simulation, editor, tool, dashboard, client routing or
  real state); it needs npm packages (three, @react-three/fiber, charts,
  physics); it needs server logic, accounts or a database; or the user
  named the framework. The named framework wins; for a client-heavy app with
  no server needs, prefer `vite-react-hono`.
- For something interactive, when unsure between `static` and a framework,
  pick the framework.
- State the template you chose and a one-line reason in your reply before
  calling `start_site_from_template`.

## The loop

1. `start_site_from_template(slug, brief, name?)` ONCE. It returns `pocket_id`
   and the repo's AGENTS.md. Never call it again for a change: it makes
   a second site.
2. Follow AGENTS.md over your habits: where pages, data, auth and tokens go,
   which files are generated. Re-read it after a recipe adds a section.
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
   `get_site_build_log`), fix, build again: three rounds at most, then tell the
   user what still fails.
8. `verify_site` once at the end; call the site ready only on `passed`.
   `preview_site` to look at it. If `preview_mode` is `static`, say server
   routes run after publish.
9. Everything saves to the DRAFT. Call `publish` only when the user asks; it
   deploys the last build, so build first.

## Integrations

Services are connected, never keyed. `list_site_integrations` lists the
providers; `connect_site_integration(pocket_id, provider, purpose)` puts a
Connect card in chat (built-ins like `newsletter` are on at once). Its reply
says how to wire that provider: follow it. Never ask for an API key or use
`request_site_secret` for one.

Client, any page: the platform injects paw.js when a page uses it; never add the
script yourself.
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

A plan refusal (a tool, or a recipe above the site's plan) means the site needs
a higher plan: tell the user, never work around it.

## Framework and library knowledge

AGENTS.md first. For a framework or library API, or a provider's embed markup,
search the web and fetch the current docs; never guess.

## Do not

- Edit generated files AGENTS.md names, or write under `_paw/`.
- Put resource ids or secret values in wrangler.jsonc or any file.
- Call the site ready before a `built` build and a `passed` `verify_site`.
