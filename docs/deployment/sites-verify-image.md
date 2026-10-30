<!-- Operator setup for the Paw Sites preview / verify sandbox image: what to bake into
     it so a draft build skips installs, and how to point the build lane at it. -->
# Sites: the preview and verify sandbox image

Every draft preview build and every `verify_site` call runs in a fresh Daytona
sandbox (`ee/pocketpaw_ee/sites/daytona_runner.py`). Edits no longer wait on that
sandbox (they run the static check and return; see "Draft verification" in
`docs/api-reference.md`), but the draft preview, and the verdict the agent gets on
its next tool call, still wait for it. With the default image most of that time is
setup that repeats on every build:

| Step | Default image | Baked image |
|---|---|---|
| Sandbox create | image resolved per create | snapshot already on the runner |
| `bun install` of the site's toolchain (svelte / react / vite / tailwind) | full network install | linked from bun's warmed cache |
| Browser harness `bun install --frozen-lockfile` (`/home/daytona/paw-harness`) | full network install | linked from bun's warmed cache |
| Chromium | `playwright-core install --with-deps chromium`, about 100 s, only when the first harness run reports no browser | already installed |

Code cannot remove these steps. The image has to carry them, so ops builds it once
per toolchain change.

## What goes in the image

1. **bun and node.** The build wrapper runs `bun install` and `bun run build`; the
   harness runs under `node` when it is on the path, else `bun`.
2. **A warmed bun install cache.** Generate one site per engine with the vendored
   generator and run `bun install` in each, so `~/.bun/install/cache` holds the
   svelte, react, vite and tailwind toolchain at the versions the generator pins.
   Then delete the generated projects and keep the cache. The sandbox `bunfig.toml`
   (`bun_supply_chain.SANDBOX_BUNFIG`) does not disable the cache, so installs
   link from it.
3. **The harness dependencies.** Copy `paw-sites/harness/package.json` and its
   lockfile into a scratch dir, run `bun install --frozen-lockfile`, and keep the
   cache it filled. The harness files themselves are still uploaded per run, so a
   harness change needs no image rebuild unless its lockfile changes.
4. **Chromium for Playwright 1.62.1.** Install it with that Playwright version's
   CLI (`npx playwright@1.62.1 install --with-deps chromium`) and set
   `PLAYWRIGHT_BROWSERS_PATH` in the image to where it landed. The harness finds it
   there, so the in-sandbox install never runs.
5. **The sandbox user's home layout.** Builds run as the `daytona` user under
   `/home/daytona`; the cache and the browsers must be readable by that user.

Rebuild the image when the generator's toolchain pins change (a paw-sites re-vendor
that moves them), when the harness lockfile changes, or when the Playwright version
the harness pins changes.

## Register it and point the lane at it

1. Build for `linux/amd64`, tag it with an explicit version (Daytona refuses
   `latest`), and push it to a registry the Daytona org can pull from.
2. Register it as a snapshot so runners hold it ready:
   `daytona snapshot create paw-sites-verify-<version> --image <registry>/paw-sites-verify:<version>`.
   A snapshot that is not used for two weeks goes inactive, so keep the lane busy
   or re-activate it after a quiet period.
3. Set `PAW_SITES_VERIFY_IMAGE=<registry>/paw-sites-verify:<version>` on the
   process that runs the sites arq worker (the preview and html-verify jobs read
   it through `browser_check.verify_image()`). Unset, the lane uses the default
   Paw dev image and installs everything per build.

The lane passes the value to `create_sandbox(image=...)`. It does not create
sandboxes from a snapshot name yet, so the snapshot keeps the image warm on the
runners rather than replacing the image reference. Creating straight from the
snapshot is a small follow-up in `daytona_runner.run_build` if image resolution
still shows up in the timings below.

## Checking that it worked

The worker and the API log one line per step (`sites.verify: layer=...`):

```
sites.verify: layer=queue_wait lane=preview pocket=<id> elapsed_ms=...
sites.verify: layer=build pocket=<id> status=built elapsed_ms=...
sites.verify: layer=browser pocket=<id> elapsed_ms=...
```

`layer=build` covers create, upload, install, build and download. On the baked
image it should drop to roughly the in-sandbox build time (about 9 s for react, 15 s
for svelte) plus create and teardown. `layer=browser` should no longer include a
chromium install; if it is still around 100 s, the image's `PLAYWRIGHT_BROWSERS_PATH`
is not being picked up.
