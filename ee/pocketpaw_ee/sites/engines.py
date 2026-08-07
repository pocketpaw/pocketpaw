# Canonical engine-capability module for Paw Sites.
#
# Created 2026-07-10 (HE-2): the single source of truth for "what can this site
# engine do", replacing scattered ``== "svelte"`` / ``!= "svelte"`` string-equality
# checks across the sites + cloud/surface code. Workspace-charter principle: one
# canonical reference module beats phased propagation.
#
# Edited 2026-08-07 (RX-1 — the react engine): registered ``"react"`` across the
# four predicates and added a FIFTH, :func:`emits_server_worker`. react is the first
# engine that needs a per-site Node build yet emits NO server entry, which broke the
# assumption ``workers_deploy`` had been making — that ``needs_node_build`` could
# stand in for "there is a ``_worker.js`` to deploy". Those were the same fact for
# ripple/svelte/html and are not for react, so the deploy shape now reads its own
# predicate. That is this module's whole design: capabilities stay orthogonal, and a
# new engine that combines them in a new way adds a predicate rather than overloading
# one.
"""Engine capability predicates for Paw Sites.

Four site-generation engines are modeled:

* ``"ripple"`` — the DEFAULT. The pocket's authored content is a ``rippleSpec`` (a
  widget tree). Publishing runs a per-site Node build (``bun install`` + Vite +
  a workerd smoke render) and emits ``.svelte-kit/cloudflare``.
* ``"svelte"`` — the pocket's content is a ``{path: contents}`` source map of
  hand-written SvelteKit files. Publishing runs the SAME per-site Node build and
  emits ``.svelte-kit/cloudflare``.
* ``"html"`` — the pocket's content is a ``{path: contents}`` source map of raw
  HTML/CSS/JS. Publishing runs NO Node build; the generator emits a plain static
  directory. *Not yet wired* into the publish / deploy / authoring paths
  (HE-1/3/4/6) — this module already KNOWS about it (that is the point) so the rest
  of the codebase can branch on a capability instead of a string as html lands, but
  no behaviour changes for html today.
* ``"react"`` — the pocket's content is a ``{path: contents}`` source map of
  hand-written React files. Publishing runs a per-site Node build (``bun install`` +
  Vite), but that build is a plain **SSG**: it emits a static ``dist/`` with the
  markup prerendered by ``react-dom/server`` and NO server entry. So react is the
  first engine that is source-map-backed AND build-requiring AND server-less — the
  combination that motivated :func:`emits_server_worker`.

The five predicates split the engine question into orthogonal capabilities:

* :func:`is_source_engine` — is the content a ``{path: contents}`` source map
  (svelte, html, react) or a rippleSpec (ripple)?
* :func:`content_key` — which pocket-dict key holds the authored content
  (``"source"`` vs ``"rippleSpec"``)?
* :func:`needs_node_build` — does publishing run a per-site Node build (ripple,
  svelte, react) or not (html)?
* :func:`static_output_rel` — where, relative to the generated project dir, do the
  deployable static assets land?
* :func:`emits_server_worker` — does that output include a ``_worker.js`` server
  entry that must be deployed as a Worker *script* (ripple, svelte), or is it a
  purely static asset tree deployed as an assets-only Worker (html, react)?

Unknown-engine policy — **fall back to ripple, never raise.** The codebase reads
``pocket.get("engine") or "ripple"`` in roughly a dozen places: an empty or missing
engine has ALWAYS meant ripple, and these predicates sit on hot read/publish paths
where raising would turn a bad-data pocket into a 500 instead of degrading to the
historical default. So both an empty/``None`` engine AND an unknown non-empty string
(a typo, or a future engine a given build predates) normalize to ``"ripple"``.
Ripple is the safe fallback precisely because it is the LEAST-capable shape — a
rippleSpec behind a full Node build — so an unknown engine treated as ripple never
skips a build or an editing guard it should have run. Callers that need strict
validation should validate the engine string at their own entry point.
"""

from __future__ import annotations

_DEFAULT_ENGINE = "ripple"

# Engines whose pocket content is a {path: contents} source map (as opposed to a
# rippleSpec). The one place the "source map vs rippleSpec" fact is encoded.
_SOURCE_MAP_ENGINES: frozenset[str] = frozenset({"svelte", "html", "react"})

# Engines that require a per-site Node build (bun install + a Vite/SvelteKit build +
# for the SvelteKit tracks a workerd smoke render) before deploy. Note html is
# source-map-backed yet needs NO build — "source map" and "node build" are
# deliberately distinct capabilities.
_NODE_BUILD_ENGINES: frozenset[str] = frozenset({"ripple", "svelte", "react"})

# Engines whose build emits a ``_worker.js`` SERVER entry that must be deployed as a
# Worker script (with ``main`` + ``nodejs_compat``), as opposed to a purely static
# tree deployed as an assets-only Worker.
#
# This is NOT a synonym for ``_NODE_BUILD_ENGINES``, and conflating the two is the
# specific bug RX-1 had to avoid: react runs a full Node build and still emits no
# server entry, because its build is an SSG that prerenders to static HTML. Pointing
# a wrangler config's ``main`` at a ``dist/_worker.js`` that a react build never
# writes fails the deploy outright. html reaches the same assets-only shape from the
# other direction (no build at all), which is why this predicate is about the OUTPUT
# and never about how the output was produced.
_SERVER_WORKER_ENGINES: frozenset[str] = frozenset({"ripple", "svelte"})

# Per-engine deployable static-output dir, relative to the generated project dir.
# ripple/svelte emit the SvelteKit Cloudflare adapter output (``_CF_OUTPUT_REL`` in
# ``sites/workers_deploy.py`` today); html has no build, so the project dir itself
# IS the static root (``"."``) — the emitted ``index.html`` is the deploy root and
# the served artifact stays byte-identical to the authored source (HE-1/HE-4);
# react emits Vite's default client-build dir (``dist``), which the generator's
# prerender pass rewrites in place so the served index.html carries the rendered
# markup. The react SSR bundle deliberately builds OUTSIDE this dir, so the
# prerenderer is never uploaded to the edge.
_STATIC_OUTPUT_REL: dict[str, str] = {
    "ripple": ".svelte-kit/cloudflare",
    "svelte": ".svelte-kit/cloudflare",
    "html": ".",
    "react": "dist",
}


def normalize_engine(engine: str | None) -> str:
    """Normalize a pocket's raw engine value to a known engine string.

    Empty / ``None`` / unknown → ``"ripple"`` (see the module docstring for why we
    fall back rather than raise). A known engine passes through unchanged. Every
    other predicate in this module routes through here, so the fallback policy lives
    in exactly one place.
    """
    if engine in _SOURCE_MAP_ENGINES or engine == _DEFAULT_ENGINE:
        return engine  # type: ignore[return-value]  # narrowed to a known non-None str
    return _DEFAULT_ENGINE


def is_source_engine(engine: str | None) -> bool:
    """True when the engine's content is a ``{path: contents}`` source map.

    ``"svelte"``, ``"html"`` and ``"react"`` are source engines; ``"ripple"`` is not
    (its content is a rippleSpec). Accepts ``str | None`` because most call sites pass
    ``pocket.get("engine")`` directly.
    """
    return normalize_engine(engine) in _SOURCE_MAP_ENGINES


def content_key(engine: str | None) -> str:
    """The pocket-dict key holding this engine's authored content.

    ``"source"`` for a source-map engine (svelte, html, react); ``"rippleSpec"`` for
    ripple.
    """
    return "source" if is_source_engine(engine) else "rippleSpec"


def needs_node_build(engine: str | None) -> bool:
    """True when publishing this engine runs a per-site Node build.

    ``"ripple"`` and ``"svelte"`` run ``bun install`` + a Vite/SvelteKit build + a
    workerd smoke render. ``"react"`` runs ``bun install`` + a Vite build (client
    bundle, SSR bundle, prerender pass) — a real build, but an SSG with no workerd
    step. ``"html"`` does not build at all — the generator emits a plain static dir.
    """
    return normalize_engine(engine) in _NODE_BUILD_ENGINES


def emits_server_worker(engine: str | None) -> bool:
    """True when this engine's build output includes a ``_worker.js`` server entry.

    ``"ripple"`` / ``"svelte"`` → True: adapter-cloudflare emits a SvelteKit worker
    inside the static-output dir, so the deploy config needs ``main`` pointed at it
    plus ``nodejs_compat``, and an ``.assetsignore`` that keeps wrangler from
    uploading the server entry as a public asset.

    ``"html"`` / ``"react"`` → False: the output is a purely static tree, deployed as
    an assets-only Worker (``assets.directory`` and nothing else).

    Deliberately SEPARATE from :func:`needs_node_build`. Those two agreed for every
    engine until react, which builds and yet emits no server entry — so a deploy path
    that asked "does it build?" when it meant "is there a script to run?" would point
    ``main`` at a ``dist/_worker.js`` that does not exist and fail the deploy.
    """
    return normalize_engine(engine) in _SERVER_WORKER_ENGINES


def static_output_rel(engine: str | None) -> str:
    """The deployable static-output dir, RELATIVE to the generated project dir.

    * ripple / svelte → ``".svelte-kit/cloudflare"`` (the SvelteKit Cloudflare
      adapter output).
    * html → ``"."`` — the generator writes the static site directly into the
      project dir (a plain ``{path: contents}`` tree, no framework build subdir), so
      the project dir IS the static root.
    * react → ``"dist"`` — Vite's client-build output, whose ``index.html`` the
      generator's prerender pass rewrites in place to carry the server-rendered
      markup. Explicitly NOT the SvelteKit path: nothing on the react track produces
      ``.svelte-kit/cloudflare``.
    """
    return _STATIC_OUTPUT_REL[normalize_engine(engine)]
