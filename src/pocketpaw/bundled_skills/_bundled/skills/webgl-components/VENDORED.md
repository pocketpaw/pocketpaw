<!-- New file 2026-08-21: provenance + refresh recipe for the vendored webgl-components skill. -->
# Vendored: webgl-components

- **Source:** https://github.com/flornkm/skills (MIT, Florian Kiem)
- **Vendored:** 2026-08-21 from commit `cf1c0094e182`
- **Local edits:** one "PocketPaw note" blockquote added under the H1 carrying
  the two constraints that override the upstream text — a static Paw Site prunes
  its client bundle (so nothing in this skill runs there), and a Paw Site cannot
  npm-import three/ogl/gsap. It defers to `pocketpaw-design-taste` §2.C as the
  authority on the canvas guardrail. Everything below the note is unmodified.
- **What it does:** production discipline for shader-driven UI rendered many
  times per page — one shared GL context blitted into per-instance 2D canvases
  (browsers cap a document at ~8-16 live contexts and evict the oldest
  silently), a single module-level rAF loop gated on IntersectionObserver and
  `visibilityState`, fragment cost budgeted by area, texture-cache eviction that
  skips mounted instances, mip bias for UV-warped sampling, and context loss
  treated as routine.
- **The part worth reading even without writing a shader:** the fallback
  section. `failIfMajorPerformanceCaveat: true` means the widget refuses to
  render on any machine with hardware acceleration off — a plain Chrome settings
  toggle — so the fallback is a production surface that never renders on the
  developer's machine. It calls a missing `position: relative` on an
  overflow-hidden frame a review blocker, since the fallback then paints across
  surrounding UI at full size.
- **`scripts/gpu-probe.html`:** self-contained page reporting which branch a
  machine takes and why. Audited before vendoring: no network calls, no `eval`
  or `new Function`, no external references, no storage access. It creates 8x8
  canvases, reads context parameters, and releases contexts via
  `WEBGL_lose_context`. `CONTEXT_OPTIONS` at the top must match the component's
  real options or the answer does not transfer.
- **Why it ships bundled:** `pocketpaw-design-taste` has WebGL canvas identities
  (Immersive WebGL, Aurora Mesh) and already requires a polished CSS fallback
  under every canvas. That skill owns the rule; this one supplies the mechanics
  and the failure modes. Applies on dynamic sites, sites that keep their client
  bundle, and app UI.
- **Not verified:** nothing has been built or reviewed under it yet, and the
  probe has not been run here. Upstream claims validation across multiple
  codebases; that is their claim, not our measurement. The `--disable-gpu`
  recipe gives the macOS Chrome path and needs the Windows equivalent.
- **To refresh:** re-fetch upstream `skills/webgl-components/`, re-apply the
  PocketPaw note, keep this file, note the new commit here.
