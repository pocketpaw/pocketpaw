---
name: pocketpaw-create-project-site
description: |
  Build a FULL-STACK Paw Site as a project: a whole repo started from a paw-sites
  base template (Astro, TanStack Start, Vite + React + Hono, Next, SvelteKit on
  Cloudflare Workers), extended with backend recipes (D1 + Drizzle, accounts, KV,
  R2, Supabase) and your own code, built in a sandbox. Invoke when the site needs
  server logic, user accounts, its own database, or when the user names one of
  those frameworks ("build it in Next", "a SvelteKit app", "a SaaS with
  sign-in"). Plain marketing and landing pages stay on the html / react tracks.
  You edit files with the project file tools; there is no rippleSpec and no
  pocket specialist.
---

# Build a project site

A project site is a real repo the user owns: package.json, framework config,
wrangler.jsonc, src/, migrations/. It builds in a sandbox and publishes as a
Cloudflare Worker. Every tool below is on `mcp__pocketpaw_sites_manager__`.

## When to pick it

Pick the project engine for full-stack apps: server routes, accounts, a
database, a dashboard behind sign-in, or a framework the user asked for by
name. A brochure, landing page or portfolio with no backend stays on the html
track (or react when they asked for React).

## Pick the template

Call `list_site_templates` and match the request to `when_to_use`. The usual
routing:

| The request | Template |
|---|---|
| Content site, blog, docs, mostly static with a few forms | `astro` |
| SaaS or app with server logic | `tanstack-start` |
| Dashboard or internal tool behind sign-in | `vite-react-hono` |
| The user asked for Next.js | `next` |
| The user asked for Svelte / SvelteKit | `sveltekit` |

When two fit, the user's named framework wins, then `when_to_use`.

## The loop

1. `start_site_from_template(slug, brief, name?)`, ONCE. It creates the draft
   site and returns `pocket_id`, the repo's AGENTS.md and the next steps. Never
   call it again for a change: that makes a second site.
2. Read AGENTS.md (it came with the result; `read_file` it again after a recipe
   adds a section). It says where pages, server functions, data access, auth
   and tokens go, and which files are generated. Follow it over your framework
   habits.
3. Backend first. `list_site_recipes` (pass `template`), then
   `apply_site_recipe` for each one the app needs, `requires` before the recipe
   that needs it. On a conflict nothing was written: resolve the files it
   names, then apply again. A plan refusal means the site needs a higher plan:
   tell the user, do not work around it.
4. Secrets. For every secret name a recipe returns, call
   `request_site_secret(pocket_id, name, description)`; `list_site_secrets`
   shows which are set. If those tools are missing, tell the user which secrets
   the site needs. Never write a secret value into any file, `.env` or
   `.dev.vars`; only names go in `.dev.vars.example`.
5. Edit. Do the recipe's `glue_tasks` in order, then build the features:
   `list_files`, `read_files` before you change anything, `patch_file` with
   exact-once `{old, new}` blocks for edits, `write_files` for new or rewritten
   files, `delete_files` to remove. Paths are relative to the repo root. Keep
   to the template's structure and its shadcn tokens. Changing package.json
   dependencies drops the stale lockfile on its own.
6. `run_build`. It waits about 30 seconds. Still building: keep working and
   call it again.
7. On `failed`, read the log tail in the result (or `get_build_log`), fix what
   it names, build again. Three rounds at most, then tell the user what still
   fails.
8. On `built`, show the preview. If `preview_mode` is `static`, say that the
   preview shows the static pages and the server routes (API, actions, server
   pages) run after publish.

Everything saves to the DRAFT. Publish with
`mcp__pocketpaw_sites_manager__publish` only when the user asks; it deploys the
finished build of the current files, so build first.

## Do not

- Write a rippleSpec, call the pocket specialist, or call a `create_*_site` tool.
- Edit generated files AGENTS.md names (`worker-configuration.d.ts`, build
  output). Recipes and wrangler changes are the way to add bindings.
- Put resource ids or secret values in wrangler.jsonc or any file.
- Report the site as ready before `run_build` returns `built`.
