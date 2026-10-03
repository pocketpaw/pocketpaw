---
name: studio-motion
description: |
  The house motion-graphics vocabulary for the /studio/editor timeline: six
  named styles, five text effects and five presets (title, statement, logo,
  stat, chart), written for the browser renderer behind
  add_motion_graphic. Load it together with `hyperframes-core` whenever the
  user asks for a title card, kinetic type, a logo reveal, an animated
  number, a bar chart or any other motion graphic on the editor, and before
  restyling or editing one listed under MOTION GRAPHICS. It does not cover
  arranging clips; that is `studio-editor`.
---

# Studio Motion — house motion graphics

`hyperframes-core` is the composition contract. This skill is the house
vocabulary on top of it: one document shape, six styles, five text effects
and five presets. The editor's Graphics gallery emits exactly this shape, so
learn it once and you can write, read and restyle any house graphic.

Full working examples for every preset: [references/presets.md](references/presets.md).

## What the renderer can and cannot do

The user's browser renders your HTML in a sandboxed iframe. Each frame is
seeked on the paused timeline, serialized as DOM into an SVG
`foreignObject`, painted onto a canvas and encoded as H.264 MP4. That chain
sets hard limits:

- **The output is opaque.** Frames are painted on black with no alpha, so a
  graphic cannot sit over footage. Design every graphic full frame, with its
  own background. Lower thirds over video are not possible yet; offer a full
  frame card instead and say why.
- **DOM, SVG and CSS only, animated by GSAP.** No `<canvas>`, WebGL or
  Three.js: canvases are snapshotted once per frame and come out unreliable.
- **One self-contained HTML document.** GSAP from
  `https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js`, fonts through
  one Google Fonts `<link>`, images only as `<img src="https://…">`. Linked
  stylesheets, fonts and images are inlined at render; anything that fails
  to load is reported back as missing.
- **No audio.** The render has no audio track. Lay music in with
  `place_audio` through `edit_timeline`.
- **Keep it under about 20 KB.** The tool accepts 200,000 characters, but
  MOTION GRAPHICS replays every graphic's source inside a 100K budget. A
  bloated graphic crowds out the others and can end up too large to show
  you, which means it can no longer be edited in place.

You have no shell, so skip every `npx hyperframes` step. The tool validates
the root and the timeline; fix whatever it names.

## The house shape

Gallery output arrives minified on a few lines; the examples in
`presets.md` are the same documents with line breaks added. Attribute
quotes may arrive as `&#39;` and `&` in the font URL as `&amp;`; both are
ordinary HTML escapes and mean the same thing.

**Head**, in this order: `<meta charset="utf-8">`,
`<meta name="viewport" content="width=W, height=H">`, the fonts `<link>`,
the GSAP `<script>`, one `<style>`.

**Root and clip:**

```html
<div id="root" data-composition-id="mg" data-start="0" data-width="1920" data-height="1080"
  data-duration="4" data-preset="title" data-style="swiss-pulse"
  style="--mg-bg:#f4f4f0;--mg-fg:#111111;--mg-accent:#e63312;--mg-muted:#6b6b6b;--mg-font-display:'Inter Tight',sans-serif;--mg-font-body:'Inter',sans-serif;--mg-display-weight:800">
  <section class="clip" data-start="0" data-duration="4">
    <div class="mg-stage"><!-- the preset's markup --></div>
  </section>
</div>
```

- `data-width` / `data-height` are the project's frame size from the
  timeline block; never assume 1920x1080. `data-duration` is seconds and the
  clip carries the same value.
- Every colour and font in the stylesheet reads from the seven `--mg-*`
  variables. Nothing else names a colour or a font.
- Size with `%`, `vmin` and `clamp()`, never fixed px, so one graphic fits
  16:9, 9:16, 4:5 and 1:1. Center with grid or flex, not
  `translate(-50%,-50%)`.
- Never tween the clip; tween its children.

**Shared classes:** `.mg-stage` (centered grid; `is-wordmark` and `is-chart`
modify it), `.mg-display` (display font and weight), `.mg-sub` (muted body
text), `.mg-accent` (accent colour), `.mg-mask` (`overflow:hidden` slot for
reveals), `.mg-w` / `.mg-c` (word and character spans made by `split`).

**Script.** One IIFE at the end of the body:

```js
(()=>{
const D=4;
const tl=gsap.timeline({paused:true,defaults:{ease:"power3.out",duration:0.7}});
// helpers: q, qa, split, fmt, count (copy them from presets.md)
// the preset's tweens
tl.to(".mg-stage",{autoAlpha:0,duration:0.4,ease:"power1.in"},Math.max(0,D-0.45));
tl.set({},{},D);
window.__timelines=window.__timelines||{};
window.__timelines["mg"]=tl;
})();
```

- `D` repeats `data-duration`. The style's ease lives in the timeline's
  `defaults`, so tweens name an ease only when they need a different one.
- The last two tweens fade the stage out at the end and pad the timeline to
  exactly `D`.
- Text stays plain in the markup. `split(el,"chars"|"words")` empties the
  element and rebuilds it as one `.mg-w` span per word (inline-block,
  nowrap, so a line never breaks mid-word), with `.mg-c` spans per
  character in chars mode. It returns the units to animate.
- `count(el,at,dur)` counts an element up to its `data-to`, with
  `data-decimals` places. It tweens a plain object and writes the formatted
  value in `onUpdate`. The renderer seeks with events on, so the number is
  right at any frame, forwards or backwards.
- Deterministic only: no `Math.random`, `Date` or `performance.now`, no
  `repeat: -1`, no `tl.play()`.

## Styles

`data-style` names one of six. The values are exact; the gallery uses the
same ones.

| id | bg | fg | accent | muted | display font | weight | body font | ease |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `swiss-pulse` | `#f4f4f0` | `#111111` | `#e63312` | `#6b6b6b` | Inter Tight | 800 | Inter | `power3.out` |
| `velvet` | `#14110f` | `#f3ece2` | `#c9a86a` | `#8c8378` | Playfair Display | 600 | Inter | `power2.inOut` |
| `maximalist` | `#ffde00` | `#0a0a0a` | `#ff2d55` | `#333333` | Anton | 400 | Inter | `back.out(1.6)` |
| `data-drift` | `#05060f` | `#e8f0ff` | `#00e5ff` | `#6c7a99` | Space Grotesk | 700 | Space Grotesk | `expo.out` |
| `soft-signal` | `#fbf3ec` | `#2b2420` | `#e07a5f` | `#8a7d74` | Fraunces | 600 | DM Sans | `sine.out` |
| `shadow-cut` | `#000000` | `#ffffff` | `#ff3b30` | `#7a7a7a` | Bebas Neue | 400 | Inter | `power4.out` |

Pick by mood: data or SaaS → `swiss-pulse`, premium → `velvet`, loud launch
→ `maximalist`, AI or tech → `data-drift`, warm or human → `soft-signal`,
dramatic → `shadow-cut`. When the user names none, choose by mood and say
which you chose.

Font variables are written `'Name',sans-serif`. The fonts link is
`https://fonts.googleapis.com/css2?` + one `family=` per family (spaces as
`+`) + `&display=swap`. The display font gets its weight, the body font
gets `400;600`, and one family used for both merges its weights in
ascending order:

- `swiss-pulse`: `family=Inter+Tight:wght@800&family=Inter:wght@400;600`
- `velvet`: `family=Playfair+Display:wght@600&family=Inter:wght@400;600`
- `maximalist`: `family=Anton:wght@400&family=Inter:wght@400;600`
- `data-drift`: `family=Space+Grotesk:wght@400;600;700`
- `soft-signal`: `family=Fraunces:wght@600&family=DM+Sans:wght@400;600`
- `shadow-cut`: `family=Bebas+Neue:wght@400&family=Inter:wght@400;600`

## Text effects

Five named effects for a headline (`.mg-head` in the title preset). Each is
one tween at 0.3s; swap the tween to swap the effect.

- **rise**: `tl.from(split(q(".mg-head"),"chars"),{yPercent:80,autoAlpha:0,stagger:0.03},0.3);`
- **blur-in**: `tl.fromTo(split(q(".mg-head"),"chars"),{filter:"blur(0.3em)",autoAlpha:0},{filter:"blur(0em)",autoAlpha:1,duration:0.9,stagger:0.035},0.3);`
- **typewriter**: `const cs=split(q(".mg-head"),"chars");tl.from(cs,{autoAlpha:0,duration:0.01,ease:"none",stagger:Math.min(0.06,(D*0.45)/cs.length)},0.3);`
- **pop**: `tl.from(split(q(".mg-head"),"words"),{scale:0.3,autoAlpha:0,duration:0.6,ease:"back.out(2.2)",stagger:0.09},0.3);`
- **mask-up**: wrap the headline and the subline each in
  `<div class="mg-mask">…</div>`, then
  `tl.from(qa(".mg-mask > *"),{yPercent:110,duration:0.9,stagger:0.18},0.3);`.
  It reveals each block, not each wrapped line, and it replaces the
  subline's own tween.

`typewriter` suits one short line. `pop` wants three to six words.

## Presets

`data-preset` names what the graphic is. Start from its example in
[references/presets.md](references/presets.md) rather than a blank page.

| preset | markup inside `.mg-stage` | default length |
| --- | --- | --- |
| `title` | `.mg-rule` accent bar, `h1.mg-display.mg-head`, optional `p.mg-sub` | 4s |
| `statement` | `.mg-lines` holding one `p.mg-display.mg-line` per line (2 to 4); the last also has `mg-accent` | 6s |
| `logo` | optional `img.mg-mark`, `.mg-mask > h1.mg-display.mg-name`, optional `p.mg-sub.mg-tag` | 4s |
| `stat` | `p.mg-display.mg-num` holding accent prefix, `span.mg-count[data-to][data-decimals]`, accent suffix; optional `p.mg-sub.mg-label` | 4s |
| `chart` | `h2.mg-display.mg-title`, `.mg-rows` of `.mg-row` (label, track and bar, `span.mg-val[data-to][data-decimals]`) | 5s |

- **statement**: all lines share one grid cell. Each gets an equal slot of
  the duration, lands word by word, and every line but the last leaves
  before the next arrives.
- **logo**: with no logo image, the stage carries `is-wordmark` and the
  name is set larger. With one, add `<img class="mg-mark" src="https://…" alt="">`
  before the mask and drop `is-wordmark`; the script checks for the mark.
- **chart**: each row carries `style="--w:NN%"`, its value as a percentage
  of the largest, and the largest row has `is-max`, which gives its bar the
  accent. Bars grow from the left and values count up together, row by row.

## Restyle and edit

The MOTION GRAPHICS block lists every motion graphic on the timeline, with
its id and source. That includes the ones the user made from the editor's
Graphics button: those carry `data-preset` and `data-style` on the root, and
they are yours to edit exactly like the ones you wrote.

- **Restyle** ("make it more premium", "try the dark one"): keep the
  structure. Swap the seven `--mg-*` values and `data-style` to the new
  style's row, the fonts link, and the `ease` in the timeline's `defaults`.
- **Edit text**: change the words in the markup; `split` handles the rest.
- **Edit numbers**: change `data-to` (and `data-decimals`). For a chart,
  also recompute every row's `--w` against the new largest value and move
  `is-max` to that row.
- **Retime**: change `data-duration` on the root and the clip, and `D` in
  the script, together.
- **Always replace in place.** Call `add_motion_graphic` with the edited
  HTML and `replace_asset_id` set to the graphic's id. Never add a second one
  alongside it.

Before you call the tool, check: one root with every `data-*` attribute
above, one clip, one paused timeline registered as `"mg"`, no canvas, no
audio, no relative URLs. Then tell the user it is rendering, not that it is
done.
