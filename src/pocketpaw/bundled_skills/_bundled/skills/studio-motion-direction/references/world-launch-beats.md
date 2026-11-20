# Launch beats

## Contents

- [Identity](#identity)
- [Use it for, not for](#use-it-for-not-for)
- [Rules](#rules): pacing and beat map, camera, type, colour, motion vocabulary, banned moves
- [Must change between uses](#must-change-between-uses)
- [Fitting the renderer](#fitting-the-renderer)
- [Example](#example)

## Identity

A launch-day announcement that says one sentence and sells one thing. The sentence is cut into three or four fragments, and each fragment owns a beat with its own layout: a big serif hook low on the frame, a second fragment high on the other side while proof cards slide in beneath it, the product working inside a frosted glass panel where a prompt types itself, then the closing fragment with a small confirmation. Colour sits in soft fields of light behind dark glass, so the frame feels deep without any blur. Every fragment lands with one short, confident move and leaves the same way. It is steady, not frantic: a beat every 2.5 to 3.5 s, never a cut on every half second.

## Use it for, not for

Use it for:

- A product, feature or version launch with one clear promise.
- Announcing a course, cohort, event or offer, when the user gives the real details.
- A teaser before a longer demo, or the first 10 to 15 s of a launch reel.

Not for:

- A slow walkthrough of how the interface works. Use calm-demo.
- A chart or a single number as the subject. Use data-morph.
- Anything without a sentence to split. If the brief has no promise, ask for one or pick another world.
- Pieces under about 7 s: three beats need at least 2 s each.

## Rules

### Pacing and beat map

Write the sentence first, split it into 3 or 4 fragments, then give each fragment one beat. The product beat sits between fragments and carries no headline. For a 12 s piece:

| Fraction of D | Beat | What happens |
| --- | --- | --- |
| 0 to 0.21 | Hook | First fragment, large, on open background. A stranger should know what is launching by the end of it. |
| 0.22 to 0.43 | Proof | Second fragment in a new position while proof cards (real items the user gave, or believable example items) slide in. |
| 0.45 to 0.76 | Product | A glass panel: the prompt types inside the input, a button presses, the output appears line by line. |
| 0.78 to 1.0 | Close | The last fragment and a confirmation chip, then the name. Holds for at least 1.5 s; a call to action holds at least 3 s. |

- Each fragment enters in about 0.6 to 0.8 s and leaves in about 0.4 to 0.5 s. Words stagger by 0.08 to 0.12 s.
- Beats overlap by about 0.2 s: the next fragment starts while the last one is still leaving, so nothing goes blank.
- Typing runs at a fixed 0.04 to 0.05 s per character inside the input only. Never stretch it to fill the beat.
- For a 20 to 30 s version, add a "how it works" beat (three steps as one visual) and a number beat, both only with facts the user gave.
- Times in the snippets come from `const at=f=>+(f*D).toFixed(3);`, and `q` is `document.querySelector`; both are defined in the example's script.
- Contrast with a high-energy promo: there, something lands every half second on a music grid. Here the fragments breathe. If the result feels like a hype reel, slow it down.

### Camera

- No camera wrapper on text beats. The fragments move; the frame does not.
- One slow push on the product beat: the panel's layer scales from 1 to about 1.05 over the whole beat with `sine.inOut`.
- The background fields drift continuously across the full duration, a few percent of their size, `ease:"none"`. That drift is what keeps the frame alive between beats.
- No 3D tilts: `perspective` renders flat in one of the two engines. Use small 2D rotations (2 to 6 degrees) on cards instead.

### Type

- A high-contrast serif for the fragments, a characterful grotesk for UI, labels and the name. The example uses Instrument Serif (upright and italic) with Bricolage Grotesque. Other pairings: Young Serif with Manrope, DM Serif Display with Outfit, Newsreader with Onest.
- Fragments are big: around `clamp(8vmin,12vw,16vmin)`, tight leading (1.0), and allowed to wrap. The italic is for the closing fragment or one emphasised word only.
- UI text is set in `em` from one font size on the panel so the panel scales as one piece.
- Size small text as `min(Nvw,Mvmin)` with the `vw` term the larger one on a wide frame. The `vmin` term wins on 16:9 and the `vw` term keeps labels readable on 9:16.
- Sentence case. No all-caps headlines; the small kicker label may be uppercase and tracked.

### Colour

- A dark ink background, a warm off-white for type, one saturated accent, one muted tint.
- Two or three large radial fields in the accent and the muted tint, at 25 to 40% opacity, partly off-frame. They are the light that the glass catches.
- Glass is `color-mix()` of `--mg-fg` at 4 to 14% over transparent, a 1px border at about 18%, and a faint top-left gradient. It reads as frosted because the fields behind it are soft, not because anything is blurred.
- The accent has three jobs and no others: one background field, the kicker, and the action (the button, the bullet dots, the closing full stop).

### Motion vocabulary

A fragment lands: words rise a little and settle with `power3.out`. No overshoot.

```js
tl.from(".f1 .w",{yPercent:60,autoAlpha:0,duration:0.7,stagger:0.12,ease:"power3.out"},at(0.03));
tl.to(".f1",{yPercent:-30,autoAlpha:0,duration:0.45,ease:"power2.in"},at(0.2));
```

Proof cards cascade in from one side with a slight 2D rotation:

```js
tl.from(".card",{xPercent:-70,rotation:-8,autoAlpha:0,duration:0.8,stagger:0.15,ease:"power3.out"},at(0.24));
```

Typing inside the input only: one span per character, toggled with `display`. Put the text and the caret in the same inline wrapper (`<span class="typed"><span class="tx">…</span><b class="caret"></b></span>`); if the caret is a separate flex item, the row's `gap` pushes it away from the text.

```js
const type=(el)=>{const s=[...el.textContent];el.textContent="";return s.map(ch=>{const k=document.createElement("span");k.className="k";k.textContent=ch;el.append(k);return k;});};
type(q(".tx")).forEach((k,i)=>tl.set(k,{display:"inline"},T+i*0.045));
```

Button press and output: a quick dip, then lines arrive one by one.

```js
tl.to(".go",{scale:0.93,duration:0.1,ease:"power1.in"},t).to(".go",{scale:1,duration:0.25,ease:"power2.out"},t+0.1);
tl.from(".out li",{yPercent:60,autoAlpha:0,duration:0.5,stagger:0.25,ease:"power2.out"},t+0.3);
```

Background drift across the whole piece: `tl.to(".fa",{xPercent:-18,yPercent:8,duration:D,ease:"none"},0);`

### Banned moves

- Typewriter reveals on headlines. Typing belongs only inside the product's input.
- Two beats with the same layout, or a beat that is just a centred line on a flat background.
- Bouncing, elastic or `back` eases; glows; gradients on text; stock-photo people.
- More than one accent colour, or the accent used for anything beyond its three jobs.
- Any price, date, count, result, testimonial or "spots left" the user did not give you.
- Hard cuts between beats, and fading the whole frame to black at the end.
- Blur-based glass (`backdrop-filter`): it is not verified in the renderer, so do not depend on it.

## Must change between uses

A second launch piece on the same timeline must differ in all five of these. Pick from the lists or invent your own in the same spirit.

**Layout** (the sequence of positions across beats)

- Hook bottom-left, proof fragment top-right with cards bottom-left, product centred, close mid-left (the example).
- Hook top-left, proof fragment bottom-right over a single wide card, product panel cropped by the right edge, close bottom-left.
- Fragments always at the same left margin but at three different heights, with the product panel taking the full right half.
- Hook centred low with a single word set huge, proof as a horizontal row of cards across the middle, close top-right.

**Hero device**

- A prompt typed into a glass input, then output lines (the example).
- A toggle flicked on, a progress bar filling in the accent, then the button turning to a "Done" state with a check.
- A notification card sliding in from the top-right with the real message the product sends.
- A before and after pair of cards, the "after" card sliding over the "before" one.
- Three glass step cards that light up one after another as a path draws between them.

**Palette** (bg, fg, accent, muted)

- Ink and tangerine: `#12101f`, `#f4f1ea`, `#ff8a3d`, `#8e88a8` (the example).
- Night teal: `#0b1517`, `#eef6f3`, `#2dd4bf`, `#6f8f8a`.
- Plum and rose: `#1a0f1d`, `#fbeff4`, `#ff5c8a`, `#9a7f95`.
- Navy and lime: `#0c1222`, `#f1f4ea`, `#c3f53c`, `#7d8aa6`.
- Brand: the user's real colours sampled from their site or graphic. Never present a default palette as their brand.

**Entry move**

- Words rise and settle, staggered (the example).
- Each fragment slides in sideways behind a mask (`overflow:hidden` wrapper, `xPercent:-105` to 0).
- The fragment starts slightly larger (scale 1.08, low opacity) and settles to size.
- A thin accent bar wipes across, and the fragment is revealed behind it as it passes.

**End frame**

- Close fragment in italic with a confirmation chip and the name small in a corner (the example).
- The product panel shrinks into a card beside the closing fragment and holds.
- The name large with the call to action beneath it, exactly as the user wrote it, held for 3 s.
- The full sentence reassembled on one frame, the fragments returning to sit together.

## Fitting the renderer

| Upstream did | We cannot because | We do instead |
| --- | --- | --- |
| Frosted, liquid glass with background blur | `backdrop-filter` is not verified through the renderer | Glass from `color-mix()` fills, a 1px light border and a faint gradient over soft radial colour fields. |
| Real product screenshots with slow push-ins | Images only by https URL, and screenshots date fast | Rebuild the part of the product that matters in HTML; use an `<img>` only if the user gives an https URL. |
| Proof from real posts and results the user attached | No attachments in a composition | Proof cards hold text the user gave; with nothing given, use example items inside the UI and no claims. |
| Camera moves computed in a custom `seek(t)` | The GSAP timeline is the clock | Fragment tweens plus one slow scale on the product layer. |
| A soundtrack under the beats | No audio in the render | Suggest `place_audio` for a backing track; never rely on sound to land a beat. |
| A separate 4:5 export | One composition per size | The layout is fluid: set `data-width`/`data-height` and the viewport to the project's size. |

## Example

This is one instance of the world, not a template. Do not reuse its layout, its sentence or its product; change the five things above and split the user's own sentence. The product name and items in it are made up for the example. 12 s at 1920x1080.

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=1920, height=1080">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:wght@400;600&family=Instrument+Serif:ital@0;1&display=swap">
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  html,body{width:100%;height:100%;overflow:hidden}
  #root{position:relative;width:100%;height:100%;overflow:hidden;background:var(--mg-bg);color:var(--mg-fg);font-family:var(--mg-font-body)}
  .clip{position:absolute;inset:0;overflow:hidden}
  .fld{position:absolute;border-radius:50%}
  .fa{width:80vmax;height:80vmax;left:-25vmax;top:-30vmax;background:radial-gradient(closest-side,var(--mg-accent),transparent);opacity:.32}
  .fb{width:70vmax;height:70vmax;right:-30vmax;bottom:-35vmax;background:radial-gradient(closest-side,var(--mg-muted),transparent);opacity:.4}
  .kick{position:absolute;left:6vmin;top:6vmin;font-size:min(2.4vw,2.6vmin);letter-spacing:.18em;text-transform:uppercase;color:var(--mg-muted)}
  .kick b{color:var(--mg-accent);font-weight:600}
  .frag{position:absolute;font-family:var(--mg-font-display);font-weight:var(--mg-display-weight);font-size:clamp(8vmin,12vw,16vmin);line-height:1;letter-spacing:-.01em;max-width:88%}
  .w{display:inline-block}
  .f1{left:6vmin;bottom:10vmin}
  .f2{right:6vmin;top:14vmin;text-align:right;max-width:70%}
  .close{position:absolute;left:6vmin;top:28%;display:flex;flex-direction:column;align-items:flex-start;gap:4vmin;max-width:88%}
  .f4{position:static;font-style:italic;max-width:none}
  .f4 i{color:var(--mg-accent);font-style:normal}
  .glass{background:linear-gradient(150deg,color-mix(in srgb,var(--mg-fg) 14%,transparent),color-mix(in srgb,var(--mg-fg) 4%,transparent));border:1px solid color-mix(in srgb,var(--mg-fg) 18%,transparent);border-radius:1.1em}
  .cards{position:absolute;left:6vmin;bottom:8vmin;display:flex;flex-direction:column;gap:1em;font-size:min(2.4vw,2.8vmin)}
  .card{display:flex;align-items:center;gap:.8em;padding:.9em 1.3em;width:22em}
  .card:nth-child(2){margin-left:2.5em}.card:nth-child(3){margin-left:5em}
  .card code{font-family:var(--mg-font-body);color:var(--mg-muted);font-size:.85em}
  .dot{width:.6em;height:.6em;border-radius:50%;background:var(--mg-accent);flex:none}
  .s3{position:absolute;inset:0;display:grid;place-items:center}
  .ui{width:min(88vw,110vmin);font-size:min(2.6vw,3.3vmin);display:flex;flex-direction:column;gap:1em}
  .bar{display:flex;align-items:center;gap:.8em;padding:.8em .8em .8em 1.3em}
  .typed{font-size:1.15em;white-space:nowrap;display:flex;align-items:center}
  .k{display:none;white-space:pre}
  .caret{display:inline-block;width:.07em;height:1.1em;background:var(--mg-fg);margin-left:.05em}
  .go{margin-left:auto;background:var(--mg-accent);color:var(--mg-bg);font-weight:600;padding:.55em 1.1em;border-radius:.7em}
  .out{padding:1.2em 1.4em;display:flex;flex-direction:column;gap:.7em}
  .out h3{font-weight:600;font-size:.8em;letter-spacing:.14em;text-transform:uppercase;color:var(--mg-muted)}
  .out ul{list-style:none;display:flex;flex-direction:column;gap:.55em}
  .out li{display:flex;gap:.7em;align-items:center}
  .chip{display:flex;align-items:center;gap:.7em;padding:.7em 1.2em;font-size:min(2.8vw,3vmin)}
  .chip svg{width:1.1em;height:1.1em;fill:none;stroke:var(--mg-accent);stroke-width:2.4;stroke-linecap:round;stroke-linejoin:round}
  .name{position:absolute;right:6vmin;bottom:6vmin;font-weight:600;font-size:min(3.6vw,4.2vmin);display:flex;align-items:center;gap:.4em}
</style>
</head>
<body>
<div id="root" data-composition-id="mg" data-start="0" data-width="1920" data-height="1080" data-duration="12" data-world="launch-beats"
  style="--mg-bg:#12101f;--mg-fg:#f4f1ea;--mg-accent:#ff8a3d;--mg-muted:#8e88a8;--mg-font-display:'Instrument Serif',serif;--mg-font-body:'Bricolage Grotesque',sans-serif;--mg-display-weight:400">
<section class="clip" data-start="0" data-duration="12">
<div class="fld fa"></div><div class="fld fb"></div>
<p class="kick"><b>New</b> &#160; Tidelog for teams</p>
<h1 class="frag f1"><span class="w">Ship</span> <span class="w">the</span> <span class="w">changelog</span></h1>
<h2 class="frag f2"><span class="w">straight</span> <span class="w">from</span> <span class="w">your</span> <span class="w">commits,</span></h2>
<div class="cards">
  <div class="card glass"><span class="dot"></span><span>Retry uploads on a 502</span><code>fix</code></div>
  <div class="card glass"><span class="dot"></span><span>Dark mode for exports</span><code>feat</code></div>
  <div class="card glass"><span class="dot"></span><span>New API keys page</span><code>docs</code></div>
</div>
<div class="s3"><div class="ui">
  <div class="bar glass"><span class="dot"></span><span class="typed"><span class="tx">Draft notes for v2.4 from main</span><b class="caret"></b></span><span class="go">Generate</span></div>
  <div class="out glass"><h3>v2.4 draft</h3><ul>
    <li><span class="dot"></span>Uploads now retry by themselves after a server error.</li>
    <li><span class="dot"></span>Exports follow your dark mode setting.</li>
    <li><span class="dot"></span>API keys have their own page in settings.</li>
  </ul></div>
</div></div>
<div class="close"><h2 class="frag f4"><span class="w">before</span> <span class="w">anyone</span> <span class="w">asks<i>.</i></span></h2>
<div class="chip glass"><svg viewBox="0 0 12 12"><path d="M2.5 6.4l2.4 2.4 4.6-5"/></svg>Published to your changelog page</div></div>
<p class="name"><span class="dot"></span>Tidelog</p>
</section>
</div>
<script>(()=>{
const D=12,at=f=>+(f*D).toFixed(3);
const tl=gsap.timeline({paused:true,defaults:{ease:"power3.out",duration:0.7}});
const q=(s)=>document.querySelector(s);
const type=(el)=>{const s=[...el.textContent];el.textContent="";return s.map(ch=>{const k=document.createElement("span");k.className="k";k.textContent=ch;el.append(k);return k;});};
const land=(sel,t)=>tl.from(sel+" .w",{yPercent:60,autoAlpha:0,stagger:0.11},t);
const leave=(sel,t)=>tl.to(sel,{yPercent:-30,autoAlpha:0,duration:0.45,ease:"power2.in"},t);
tl.to(".fa",{xPercent:-18,yPercent:8,duration:D,ease:"none"},0);
tl.to(".fb",{xPercent:12,yPercent:-10,duration:D,ease:"none"},0);
tl.from(".kick",{autoAlpha:0,yPercent:-60,duration:0.6},0.1);
land(".f1",at(0.03));leave(".f1",at(0.2));
land(".f2",at(0.22));
tl.from(".card",{xPercent:-70,rotation:-8,autoAlpha:0,duration:0.8,stagger:0.15},at(0.24));
leave(".f2",at(0.43));
tl.to(".card",{xPercent:-40,autoAlpha:0,duration:0.45,stagger:0.06,ease:"power2.in"},at(0.43));
tl.from(".ui",{yPercent:12,autoAlpha:0,duration:0.8},at(0.45));
tl.fromTo(".s3",{scale:1},{scale:1.05,duration:at(0.3),ease:"sine.inOut"},at(0.45));
const T=at(0.47),ks=type(q(".tx")),rate=0.045,done=T+ks.length*rate;
ks.forEach((k,i)=>tl.set(k,{display:"inline"},T+i*rate));
tl.to(".go",{scale:0.93,duration:0.1,ease:"power1.in"},done+0.1).to(".go",{scale:1,duration:0.25,ease:"power2.out"},done+0.2);
tl.to(".caret",{autoAlpha:0,duration:0.1},done+0.15);
tl.from(".out",{yPercent:8,autoAlpha:0,duration:0.5},done+0.25);
tl.from(".out li",{yPercent:60,autoAlpha:0,duration:0.5,stagger:0.2,ease:"power2.out"},done+0.4);
tl.to(".ui",{yPercent:-10,autoAlpha:0,duration:0.45,ease:"power2.in"},at(0.76));
land(".f4",at(0.78));
tl.from(".chip",{xPercent:-12,autoAlpha:0,duration:0.6},at(0.83));
tl.from(".name",{autoAlpha:0,yPercent:50,duration:0.6},at(0.86));
tl.set({},{},D);
window.__timelines=window.__timelines||{};window.__timelines["mg"]=tl;
})();</script>
</body>
</html>
```
