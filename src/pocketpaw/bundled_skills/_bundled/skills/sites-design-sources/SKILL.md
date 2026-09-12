---
name: sites-design-sources
description: |
  Named reference sources for GROUNDS, EFFECTS, SHADERS, MOTION and TYPE on a Paw
  Site, filtered to what this surface can physically build. Invoke when you are
  about to hand-write a background, a texture, a canvas shader, a CSS animation or
  a text effect and want a real technique behind it instead of a default gradient,
  or when the brief NAMES an effect (marquee, halo, gradient headline, dither,
  grain) and you want to build it WELL rather than reach for the first shape that
  comes to mind. Also the way out of the overused-font trap in
  `pocketpaw-design-taste` 2.F. Everything listed here is TEXT you author into the
  source map: CSS, inline SVG, or a GLSL string. Nothing here is installed and
  nothing here is downloaded, because this surface has neither a package manager
  nor a filesystem.
---

# Design sources for a Paw Site

## What this surface can actually take from a reference

Three facts decide whether a source is usable here, and together they rule out
most of the web's design catalogue:

1.  **No package manager.** `package.json` is generator-owned and your source map
    supplies FILES ONLY, so `npm i` is not a step you have. Every component
    library, every animation runtime (`motion`, `gsap`, `lenis`), every icon
    package and every shadcn registry entry is out, however good it is.
2.  **No filesystem and no binary ingest.** Your belt is `create_*`, `edit_*`,
    `read_site_source`, `list_site_assets`, `publish`, plus image and video
    generation. There is no download tool and no upload-from-URL, so a WOFF2
    file, an SVG pack, a texture PNG or a CC0 illustration set cannot be brought
    into the project no matter how permissive its licence.
3.  **What remains is technique.** A source is useful here when its value is
    something you RETYPE as text: a CSS rule, an inline `<svg>`, a data URI, a
    fragment-shader body, an easing curve, a font URL. Read the technique, author
    the code yourself.

So use these as reading, not as a dependency list. `WebFetch` is available on
this surface and these pages are fetchable. Fetch one when you are about to build
the thing it covers, never speculatively.

## Grounds, patterns and texture (feeds 2.B)

This section exists because "a tuned ground" has more answers than a gradient.
All of these are authored as CSS or an inline SVG data URI.

| Source | URL | Licence | What you retype |
| --- | --- | --- | --- |
| Pattern Craft | `patterncraft.fun` | MIT | Pure-CSS background patterns: grids, dots, rays, mesh. Copy the rule, retune the colours to your tokens. |
| Hero Patterns | `heropatterns.com` | CC-BY | Tiling SVG patterns delivered as `background-image: url("data:image/svg+xml,...")`. Set the fill to your own ink before using it. |
| Pattern Monster | `pattern.monster` | free | SVG pattern generator with stroke, scale and rotation controls. Take the emitted markup, not a file. |
| fffuel | `fffuel.co` | free tools | SVG generators for grain, noise, soft gradients, blobs and dot fields. The output is markup you paste. |
| Haikei | `haikei.app` | free tools | Layered waves, blobs and stacked shapes as SVG. |

Grain over a flat ground is the highest-return one on this list: a noise SVG at
very low opacity over a solid or two-stop gradient kills the plastic look a bare
CSS gradient has, and it costs one element.

**Licence discipline.** CC-BY means the author is credited somewhere real or you
do not ship it. Where a source is a *generator*, the output is yours and there is
nothing to attribute. Never paste something whose terms you did not read into a
page a business will publish under its own name.

## Canvas shaders (feeds 2.C)

2.C allows a hand-written WebGL canvas: raw `getContext('webgl')`, a pass-through
vertex shader, one fragment shader over a full-screen quad. This is where the
fragment shader itself comes from.

| Source | URL | Licence | Notes |
| --- | --- | --- | --- |
| Radiant Shaders | `radiant-shaders.com/gallery/all` | MIT | Self-contained fragment shaders. MIT means you can ship the maths. |
| Codrops | `tympanus.net/codrops` | per-article | The best technique writing on the web for canvas, scroll and transition work. Read the article, write your own implementation. |
| Grainient | `grainient.supply` | commercial tool | Grainy-gradient explorer. Use it to decide the LOOK, then reproduce it in your own shader or in CSS. Do not hotlink its output. |

Everything in 2.C still applies: the canvas sits over a finished CSS fallback,
the buffer is capped at 2x DPR, and the loop stops off-screen. A shader you
copied without understanding is a shader you cannot cap.

## Motion and effects (feeds 3.D)

| Source | URL | Licence | What you retype |
| --- | --- | --- | --- |
| Animista | `animista.net` | BSD | Pure-CSS keyframe animations with the timing already worked out. Take the `@keyframes`, then slow it down: the defaults are demo-speed. |
| Colorion Text Effects | `text-effects.colorion.co` | MIT | CSS-only text treatments. |
| Theme Toggle Effect | `theme-toggle.rdsx.dev` | free | View-Transitions light/dark switching, which is a real interaction rather than an ornament. |

## Type (feeds 2.F)

**Only one font-loading path works on this surface: a URL in CSS.** A foundry's
WOFF2 cannot be self-hosted here, because there is no way to get the file into
the project. So:

-   **Google Fonts** (`fonts.google.com`, OFL / Apache-2.0) is the practical
    library, loaded with a single `@import` or one `<link>` in the document head.
    It is far wider than its famous handful: Bricolage Grotesque, Instrument
    Sans, Familjen Grotesk, Schibsted Grotesk, Gabarito, Funnel Display,
    Geologica and Onest all live there, and none of them is Inter.
-   **Fontpair** (`fontpair.co`) and **Free Faces** (`freefaces.gallery`) are for
    CHOOSING. Pick a pairing there, then confirm the faces are servable from a
    CDN you can reference before committing to them.
-   **The independent OFL foundries** — Velvetyne (`velvetyne.fr`), Open Foundry
    (`open-foundry.com`), Uncut (`uncut.wtf`), Collletttivo
    (`collletttivo.it`), The League of Moveable Type — are where genuinely
    distinctive free type lives. Treat them as a reference for what a display
    face CAN look like, and as a real option only when the site owner supplies
    the file through their own assets. Never write a `@font-face` pointing at a
    foundry's own server: that is hotlinking someone else's bandwidth, and it
    breaks the day they move the file.

## Colour (feeds 2.G)

`derive_palette` on your own belt comes first, because it is instant and returns
values rather than a page to read. These cover what it does not:

-   **OKLCH picker** (`oklch.com`, MIT) when a ramp needs perceptually even
    steps. Lightness in OKLCH behaves the way the eye expects; HSL does not.
-   **Colour Contrast Checker** (`colourcontrast.cc`, MIT) to confirm a pair
    against the 4.5:1 body and 3:1 large/interface floors before shipping. You
    have no browser on this surface, so contrast is the one number worth
    confirming against a source rather than estimating.

## The failure this skill exists to prevent

A catalogue of hundreds of design resources is mostly a catalogue of things you
cannot use, and reading one as a menu is how a page ends up padded with effects
nobody asked for. `pocketpaw-design-taste` MODULE 0 still governs: the brief
decides what gets built. Come here once you already know you are building a
ground, a shader, an animation or a type system, and you want that ONE thing to
be good.
