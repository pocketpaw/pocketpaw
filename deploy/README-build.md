<!--
  deploy/README-build.md -- how to build the PocketPaw Enterprise image with the
  Paw Sites publish toolchain bundled.
  Created 2026-06-25 (feat/paw-sites-prod-deploy, DEP-4): documents the
  vendor/clone source switch for the paw-sites generator + the @ripple-ui/svelte
  tarball that Dockerfile.enterprise needs so a live `POST /sites/publish` works in
  the deployed image (it previously 500'd — the image shipped no bun / generator /
  ripple).
-->

# Building the PocketPaw Enterprise image (with Paw Sites publishing)

`Dockerfile.enterprise` builds the sovereign per-tenant enterprise image. A live
**Paw Site publish** shells out, at publish time, to:

1. `paw-sites-gen build ...` — the site generator CLI,
2. `bun install` + `bun run build` — on the generated SvelteKit project.

The generated ripple-track site also installs `@ripple-ui/svelte`. So the runtime
image must carry **bun**, the **paw-sites-gen** binary, and a resolvable
**`@ripple-ui/svelte`** package. Both `paw-sites` and `ripple` are private SIBLING
repos OUTSIDE the pocketpaw build context, so each has a `vendor` / `clone` source
switch — the same pattern as `paw-enterprise/deploy/Dockerfile`.

## 1. Build without private-repo access (vendored — the default)

`PAW_SITES_SOURCE=vendor` (default) and `RIPPLE_SOURCE=vendor` (default) COPY
prebuilt trees from the build context. Stage them once before building:

```bash
# Generator → deploy/paw-sites/ (dist/ + templates/ + package.json)
scripts/vendor-paw-sites.sh
# @ripple-ui/svelte tarball → deploy/ripple/ripple-ui-svelte-0.5.0.tgz
scripts/vendor-ripple-tarball.sh
```

Both scripts vendor from the sibling `../paw-sites` / `../ripple` checkouts by
default; override the source dir with `PAW_SITES_DIR=...` / `RIPPLE_DIR=...` (e.g. a
CI-downloaded checkout). Then build:

```bash
docker build -f Dockerfile.enterprise -t pocketpaw-ee .
# or: docker compose -f docker-compose.enterprise.yml build
```

`deploy/paw-sites/` and `deploy/ripple/` are gitignored build artifacts (mirroring
the FE's `deploy/ripple/`) — distribute them as a released tarball / OCI layer to
customers who don't have the private source.

## 2. Internal CI / Coolify (clone — needs private-repo access)

The Coolify app clones pocketpaw `dev` and builds `Dockerfile.enterprise`. The
vendored trees are gitignored, so they are **not** in that clone — Coolify must
build with the `clone` source, which clones + builds the sibling repos in-image.
Set these two build args in the Coolify app's build configuration (Coolify owns the
`docker build`; the CI `deploy-coolify` job only fires the deploy webhook):

```
PAW_SITES_SOURCE=clone
RIPPLE_SOURCE=clone
```

Optionally pin the refs (defaults: repos below, ref `main`):

```
PAW_SITES_REPO=https://github.com/qbtrix/paw-sites.git
PAW_SITES_REF=main
RIPPLE_REPO=https://github.com/qbtrix/ripple-iui.git
RIPPLE_REF=main
```

Equivalent direct build:

```bash
docker build -f Dockerfile.enterprise \
  --build-arg PAW_SITES_SOURCE=clone \
  --build-arg RIPPLE_SOURCE=clone \
  -t pocketpaw-ee .
```

We default the Dockerfile to `vendor` (the local / released-build path) and put
`clone` on Coolify rather than the reverse, because the clone path needs network +
private-repo credentials at build time — a property the internal CI runner has and
an air-gapped customer build does not.

## 3. Why this is the lower-risk choice for Coolify

The alternative — having the CI `deploy-coolify` job run the vendor scripts and
commit/push the artifacts before the webhook — was rejected: it would commit large
build artifacts to `dev`, and Coolify rebuilds from its own clone anyway, so the
artifacts would have to be pushed to a branch Coolify sees. The `clone` source keeps
the artifacts out of git entirely and lets Coolify build the exact pinned refs, at
the cost of a longer first build (cached afterward).

## 4. Runtime env (baked, overridable)

The image bakes these so a publish resolves with no extra config (see
`.env.enterprise.example` for the full list):

- `PAW_SITES_RIPPLE_DEP=file:/opt/ripple-ui-svelte-0.5.0.tgz` — the bundled ripple
  tarball the publish path rewrites the generated site's dep to.
- `PAW_SITES_MOTION_DEP=^12.40.0` — kept in lockstep with ripple's motion pin.
- `paw-sites-gen` is on `PATH` (a wrapper at `/usr/local/bin/paw-sites-gen` that
  execs `node /opt/paw-sites/dist/cli.js`), so `PAW_SITES_GEN_CMD` stays unset.

A LIVE Cloudflare deploy additionally needs the `PAW_CF_*` group (account id, API
token, zone, dispatch namespace) — see `.env.enterprise.example`. Without them the
publish degrades to the local static-serve path (dev only).
