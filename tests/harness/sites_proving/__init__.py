"""SG-1 sites proving harness — render one spec through a renderer built ONCE.

WHAT THIS PROVES: publishing a Paw Site today generates a full SvelteKit project
per site and runs ``bun install`` + Vite + a workerd render (45-60s, inside the
HTTP request) — but the generated project is IDENTICAL for every site. Only the
spec, the brand CSS, and ~7 substituted tokens vary. So the renderer can be built
ONCE and reused, and this package is the harness that tests whether that holds.

Layout:
  bundle.py     the ``Bundle`` / ``BundleManifest`` contract later slices consume
  renderer.py   ``render(spec, tokens) -> Bundle``, sidecar + per-render drivers
  verify.py     ``verify(bundle)`` — lane-independent, replaces the workerd gate
  harness.py    scenario registry + machine-readable evidence report
  scenarios.py  A1 (renders and verifies) and A8 (fails closed)
  measure.py    resident sidecar vs short-lived process per render
  node/         the ONCE-built SSR renderer: build.mjs, the Svelte entry, drivers

SCOPE: a proving harness only. It does NOT touch ``ee/pocketpaw_ee/sites/``, does
not change any existing behaviour, and writes nothing to the paw-sites repo. The
renderer is built by ``node/build.mjs`` into the gitignored ``node/.build/``.

Build the renderer before running anything::

    node tests/harness/sites_proving/node/build.mjs
"""
