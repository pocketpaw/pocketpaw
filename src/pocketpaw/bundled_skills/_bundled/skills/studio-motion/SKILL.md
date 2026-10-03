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
vocabulary on top of it: one root shape, six styles, five text effects and
five presets that the editor's Graphics gallery emits in exactly the same
shape. Learn the shape once and you can write, read and restyle any of them.

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

## The house contract

Every house graphic has this skeleton. Only the head's font link, the root's
attributes, the clip's contents and the script body change between presets.

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter+Tight:wght@800&family=Inter:wght@400;600&display=swap">
<style>
  html, body { margin: 0; height: 100%; background: #000; }
  #root { position: relative; width: 100%; height: 100%; overflow: hidden;
    background: var(--mg-bg); color: var(--mg-fg);
    font-family: var(--mg-font-body), sans-serif; }
  .clip { position: absolute; inset: 0; box-sizing: border-box; padding: 8vmin;
    display: flex; flex-direction: column; justify-content: center; }
  .w, .ch { display: inline-block; }
  .w { white-space: nowrap; }
</style>
</head>
<body>
<div id="root" data-composition-id="mg" data-start="0" data-width="1920" data-height="1080"
  data-duration="4" data-preset="title" data-style="swiss-pulse"
  style="--mg-bg:#f4f4f0;--mg-fg:#111111;--mg-accent:#e63312;--mg-muted:#6b6b6b;--mg-font-display:'Inter Tight';--mg-font-body:'Inter';--mg-display-weight:800">
  <section class="clip" data-start="0" data-duration="4">
    <!-- the preset's content -->
  </section>
</div>
<script>
  const EASE = "power3.out";
  const tl = gsap.timeline({ paused: true });
  // build every tween here, synchronously
  window.__timelines["mg"] = tl;
</script>
</body>
</html>
```

The rules behind it:

- **Root.** `data-composition-id="mg"`, `data-start="0"`, the project's frame
  size as `data-width` / `data-height` (read it from the timeline block, never
  assume 1920x1080), `data-duration` in seconds, plus `data-preset` and
  `data-style`. The seven `--mg-*` variables live in the root's `style` and
  every visible colour and font reads from them. Apart from the font link and
  the `EASE` constant, no other line names a colour, a font or an ease.
- **One clip.** A single `<section class="clip">` spanning the whole
  duration. Never tween the clip itself; animate its children.
- **Fluid layout.** Size and space with `%`, `vmin` and `clamp()`, never
  fixed px, so the same graphic fits 16:9, 9:16, 4:5 and 1:1. Center with
  flex, not `translate(-50%,-50%)`. A tween owns `transform` on any node it
  moves, so give those nodes no CSS transform.
- **One paused timeline**, built synchronously at the end of the body and
  registered as `window.__timelines["mg"] = tl`. No `tl.play()`.
- **Deterministic.** No `Math.random`, `Date` or `performance.now`, no
  `repeat: -1`. Text is split from the DOM's own `textContent`, so editing the
  words never means editing the script.
- **Real text in the markup.** Write the user's words as plain text in the
  HTML and let the script split them. No `<br>`: one element per line.

### Splitting text

Paste this helper into the script when an effect works per word or per
character. It keeps each word unbreakable so a line never wraps mid-word.

```js
function split(el) {
  const words = [], chars = [];
  const list = el.textContent.trim().split(/\s+/);
  el.textContent = "";
  list.forEach((word, i) => {
    const w = el.appendChild(document.createElement("span"));
    w.className = "w";
    for (const c of word) {
      const ch = w.appendChild(document.createElement("span"));
      ch.className = "ch";
      ch.textContent = c;
      chars.push(ch);
    }
    words.push(w);
    if (i < list.length - 1) el.appendChild(document.createTextNode(" "));
  });
  return { words, chars };
}
```

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

The Google Fonts `family=` part for each style:

- `swiss-pulse`: `family=Inter+Tight:wght@800&family=Inter:wght@400;600`
- `velvet`: `family=Playfair+Display:wght@600&family=Inter:wght@400;600`
- `maximalist`: `family=Anton&family=Inter:wght@400;600`
- `data-drift`: `family=Space+Grotesk:wght@400;700`
- `soft-signal`: `family=Fraunces:wght@600&family=DM+Sans:wght@400;600`
- `shadow-cut`: `family=Bebas+Neue&family=Inter:wght@400;600`

The full href is `https://fonts.googleapis.com/css2?` + that + `&display=swap`.

## Text effects

Five named effects. Each is one tween on the output of `split` (or on line
wrappers for `mask-up`), placed at time `at`. Swap the tween to swap the
effect; the markup does not change.

- **rise**: characters slide up into place.
  `tl.from(split(el).chars, { yPercent: 100, opacity: 0, duration: 0.6, stagger: 0.03, ease: EASE }, at);`
- **blur-in**: characters come into focus.
  `tl.fromTo(split(el).chars, { filter: "blur(16px)", opacity: 0 }, { filter: "blur(0px)", opacity: 1, duration: 0.8, stagger: 0.03, ease: EASE }, at);`
- **typewriter**: characters appear one at a time.
  `tl.from(split(el).chars, { opacity: 0, duration: 0.01, stagger: 0.05, ease: "none" }, at);`
- **pop**: words spring in from nothing.
  `tl.from(split(el).words, { scale: 0, opacity: 0, duration: 0.5, stagger: 0.08, ease: "back.out(2.2)" }, at);`
- **mask-up**: each line rises out of a hidden slot. Markup is one
  `<div class="mask"><div class="mask-in">Line</div></div>` per line, with
  `.mask { overflow: hidden; }`.
  `tl.from(".mask-in", { yPercent: 110, duration: 0.8, stagger: 0.12, ease: EASE }, at);`

`rise`, `blur-in` and `mask-up` read best on display type. `typewriter` suits
a single short line. `pop` wants three to six words.

## Presets

`data-preset` names what the graphic is. Each has a full example in
[references/presets.md](references/presets.md); start from it rather than a
blank page.

| preset | what it shows | default length |
| --- | --- | --- |
| `title` | eyebrow, headline, subtitle, accent rule | 4s |
| `statement` | 2 to 4 lines landing one at a time, word by word, last line in accent | 6s |
| `logo` | mark scales and settles, name reveals, tagline; wordmark only when there is no mark | 4s |
| `stat` | one number counting up, with a label | 4s |
| `chart` | horizontal bars growing in a stagger, values counting up, largest bar in accent | 6s |

Two techniques recur:

- **Count-ups.** Tween a plain object and write the formatted value in
  `onUpdate`. The renderer seeks with events on, so the text is right at any
  frame, forwards or backwards. Format with an explicit locale
  (`toLocaleString("en-US", …)`), and use `font-variant-numeric: tabular-nums`
  so the width does not jitter.
- **Data in markup, not script.** Numbers live in `data-value` attributes and
  the script derives everything else (bar widths, which bar is largest). To
  change the data, change the attributes.

Hold the finished frame for at least a second before the end. A short fade
of the content on its last 0.4s is fine; the background stays.

## Restyle and edit

The MOTION GRAPHICS block lists every motion graphic on the timeline, with
its id and source. That includes the ones the user made from the editor's
Graphics button: those carry `data-preset` and `data-style` on the root, and
they are yours to edit exactly like the ones you wrote.

- **Restyle** ("make it more premium", "try the dark one"): keep the
  structure. Swap the seven `--mg-*` values and `data-style` to the new
  style's row, the font link's `family=` part, and `EASE`. Nothing else
  needs to change, because nothing else names a colour or a font.
- **Edit content** ("change the number to 3.2M", "add a fourth bar"): change
  the text or the `data-value` attributes and leave the script alone.
- **Retime**: change `data-duration` on the root and the clip together.
- **Always replace in place.** Call `add_motion_graphic` with the edited
  HTML and `replace_asset_id` set to the graphic's id. Never add a second one
  alongside it.

Before you call the tool, check: one root with every `data-*` attribute
above, one clip, one paused timeline registered as `"mg"`, no canvas, no
audio, no relative URLs. Then tell the user it is rendering, not that it is
done.
