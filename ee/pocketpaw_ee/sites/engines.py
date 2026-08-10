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
#
# Edited 2026-08-10 (SL-2 slice 2 — the build lane got its first caller): added
# :func:`expects_server_worker`, a SIXTH predicate, for the question a caller holding only
# a finished ARTIFACT has to ask: not "would a worker be deployed" but "is its absence a
# problem". Those were one question until SL-1 split the svelte track across two adapters,
# and a cross-check that keeps conflating them warns on every healthy static svelte build.
# It returns a tri-state — ``None`` means the engine name genuinely cannot say — which is
# the honest shape and the reason it is a new predicate rather than an edit to either
# existing one.
#
# Edited 2026-08-10 (SL-1 — the static svelte landing lane): added
# :func:`resolve_static_output_rel` and :func:`resolve_emits_server_worker`, the
# ARTIFACT-resolving siblings of the last two predicates. The svelte track now builds
# on adapter-static for a static landing site (output ``build``, no ``_worker.js``) and
# adapter-cloudflare for a dynamic/auth one — a property of the SITE, not of the engine
# string, so for the first time a capability here is NOT a function of the engine name
# and the name-only predicates genuinely cannot answer it.
#
# This follows RX-1's design rather than departing from it: a new combination adds a
# predicate instead of overloading one. What is new is that these two read the
# filesystem, which no other predicate here does. That exception is deliberate and
# narrow — see :func:`resolve_static_output_rel` for why reading the artifact beats
# threading a static/dynamic flag through six call sites, two of which have no
# generate in scope to thread it from.
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
* :func:`expects_server_worker` — a tri-state sibling of the above, for a caller holding
  only a finished artifact: is a worker's ABSENCE an anomaly (ripple), is its PRESENCE one
  (react, html), or is either shape legitimate (svelte, since SL-1 split its adapters)?

SL-1 added ARTIFACT-RESOLVING siblings for the last two, and they are the ones to
reach for when a generated project dir is in hand:

* :func:`resolve_static_output_rel` — where did THIS build's output actually land?
* :func:`resolve_emits_server_worker` — did THIS build actually emit a worker?

Both exist because the svelte track no longer has one output shape. A static landing
site builds on ``adapter-static`` (``build``, no worker); a dynamic/auth site builds
on ``adapter-cloudflare`` (``.svelte-kit/cloudflare``, worker load-bearing). That is a
property of the SITE, not of the engine string, so the name-only predicates above
cannot answer it and the two resolvers read the artifact instead. They are also the
only functions in this module that touch the filesystem — see
:func:`resolve_static_output_rel` for why that exception is narrower than threading a
flag through every caller would be.

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

import os
from pathlib import Path

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

# SL-1 — where a STATIC svelte site's output lands instead. adapter-static's default.
#
# THIS IS WHY THE MAP ABOVE STOPPED BEING SUFFICIENT FOR SVELTE. The svelte track now
# builds on one of two adapters, chosen by a property of the SITE (does it read a
# per-tenant D1?) rather than of the engine:
#
#   * static landing site  → adapter-static      → ``build``, and NO ``_worker.js``
#   * dynamic / auth site  → adapter-cloudflare  → ``.svelte-kit/cloudflare``
#
# adapter-cloudflare emitted a ``_worker.js`` Server shell for EVERY build, including
# one with zero server routes, and that shell imports two files that sit OUTSIDE the
# deployable dir — so a static site shipped a worker that could not start. Dropping
# the adapter for static sites removed the worker; it also moved the output dir, which
# is what these two resolvers exist to absorb.
_SVELTE_STATIC_OUTPUT_REL = "build"


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


def expects_server_worker(engine: str | None) -> bool | None:
    """Whether a build of ``engine`` SHOULD have emitted a ``_worker.js`` — or ``None``
    when the engine name cannot say, because either answer is legitimate.

    * ripple → ``True``. adapter-cloudflare, always a worker.
    * react / html → ``False``. Purely static trees; a worker turning up is an anomaly.
    * svelte → ``None``. Since SL-1 the track spans two adapters chosen by a property of
      the SITE: a static landing site emits none, a dynamic/auth one emits one. Both are
      correct builds.

    ADDED 2026-08-10 (SL-2 slice 2) for the one question :func:`emits_server_worker`
    cannot answer: not "would a worker be deployed" but "is its ABSENCE a problem". Those
    were the same question until SL-1 split the svelte track, and conflating them makes a
    cross-check warn on every healthy static svelte build — which is worse than not
    checking, because a warning that fires on correct builds is one people learn to
    ignore, and it is the same warning that would fire on a genuinely truncated artifact.

    A THIRD PREDICATE RATHER THAN A CHANGE TO THE OTHER TWO, per this module's design:
    :func:`emits_server_worker` still honestly answers the deploy-shape question from the
    name, and :func:`resolve_emits_server_worker` still answers it from an artifact on
    disk. This one is for a caller that holds NEITHER a project dir nor a deploy decision
    — only an artifact and a question about whether it looks complete.
    """
    normalized = normalize_engine(engine)
    if normalized == "svelte":
        return None
    return normalized in _SERVER_WORKER_ENGINES


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

    NOT AUTHORITATIVE FOR SVELTE SINCE SL-1. The svelte row is the DYNAMIC shape; a
    static landing site writes ``build``. Whenever a generated project dir is in hand,
    call :func:`resolve_static_output_rel` instead. This function stays for callers
    that legitimately want the nominal per-engine value with no filesystem in scope
    (config templates, docs, tests) and for the three engines that still have exactly
    one output shape each.
    """
    return _STATIC_OUTPUT_REL[normalize_engine(engine)]


def resolve_static_output_rel(project_dir: str | os.PathLike[str], engine: str | None) -> str:
    """Where THIS build's deployable output actually landed, relative to ``project_dir``.

    Prefer this over :func:`static_output_rel` wherever a generated project dir exists.
    Since SL-1 the svelte track spans two adapters with two different output dirs, and
    which one ran is not recoverable from the engine name — so this reads the answer
    off disk rather than predicting it.

    ONLY svelte is probed. ripple / html / react each still have exactly one output
    shape, so they fall straight through to the nominal map and their behaviour is
    byte-for-byte unchanged.

    PROBE ORDER IS LOAD-BEARING: ``build`` first, so a project dir carrying a stale
    ``.svelte-kit/cloudflare`` tree from a pre-SL-1 build cannot shadow what the
    current build emitted. Reversing these two silently serves the old artifact.

    When NEITHER exists we return the nominal map value rather than raising. The caller
    is then reporting a missing build against a concrete path, which is a truer error
    than ``engines.py`` refusing to decide — and it keeps this predicate total, the way
    every other one in this module is.

    NOTE: this and :func:`resolve_emits_server_worker` are the ONLY functions here that
    touch the filesystem. Every other predicate is pure. That is a deliberate, narrow
    exception: the fact being resolved is a property of an artifact on disk, and the
    alternative — threading a static/dynamic flag through six call sites, two of which
    reconstruct a path from a pocket id with no generate in scope — creates a second
    source of truth that can disagree with the artifact. Reading the artifact cannot.
    """
    normalized = normalize_engine(engine)
    if normalized != "svelte":
        return _STATIC_OUTPUT_REL[normalized]
    root = Path(project_dir)
    for candidate in (_SVELTE_STATIC_OUTPUT_REL, _STATIC_OUTPUT_REL["svelte"]):
        if (root / candidate).is_dir():
            return candidate
    return _STATIC_OUTPUT_REL["svelte"]


def resolve_emits_server_worker(project_dir: str | os.PathLike[str], engine: str | None) -> bool:
    """Whether THIS build actually emitted a ``_worker.js`` server entry.

    The deploy-shape half of the same SL-1 problem, and the half with teeth. A static
    svelte site emits NO worker, so :func:`emits_server_worker` — which answers from
    the engine name and therefore still says True — would have ``workers_deploy``
    point ``main`` at a ``build/_worker.js`` that does not exist and pick the
    server-entry ``.assetsignore`` over the assets-only one. That fails the deploy
    outright, which is the exact failure mode RX-1 introduced this predicate to
    prevent; SL-1 just reopened it from the other side.

    A static svelte site must deploy assets-only, the same way react already does.

    ``_worker.js`` is tested for EXISTENCE, not for being a file: adapter-cloudflare
    emits it as a DIRECTORY (``_worker.js/chunks/0.js``) once an app is large enough,
    so an ``is_file()`` check would report "no worker" for a big dynamic site and
    silently deploy it assets-only — a working site replaced by a broken one.
    """
    if normalize_engine(engine) not in _SERVER_WORKER_ENGINES:
        return False
    out_dir = Path(project_dir) / resolve_static_output_rel(project_dir, engine)
    return (out_dir / "_worker.js").exists()
