# Calm demo

## Contents

- [Identity](#identity)
- [Use it for, not for](#use-it-for-not-for)
- [Rules](#rules): pacing and beat map, camera, type, colour, motion vocabulary, banned moves
- [Must change between uses](#must-change-between-uses)
- [Fitting the renderer](#fitting-the-renderer)
- [Example](#example)

## Identity

A quiet product walkthrough on a bright, airy canvas. One short line of text sits small in open space, and the camera pushes in until it fills most of the width while a soft highlighter box sweeps behind the one word that matters. Then the text dissolves and the product's own interface, rebuilt in HTML and CSS, appears close up while it is being used: a prompt types at a steady rate, steps tick off, a result fills in, and the camera pulls back so the viewer sees where they are. Nothing bounces, nothing shouts. The feeling is a well-made homepage demo: restraint, generous space, one thing at a time.

## Use it for, not for

Use it for:

- Product demos and feature walkthroughs where the interface is the story.
- "How it works" moments: type something, get something back.
- Onboarding clips, changelog highlights, a calm intro before a longer video.

Not for:

- Hype, launch-day energy or anything beat-locked. Use launch-beats.
- Numbers as the hero. Use data-morph.
- Pieces shorter than about 6 s: the push and the UI moment each need room to breathe.
- Dark, moody or dramatic briefs. This world lives on a light canvas.

## Rules

### Pacing and beat map

Time everything as a fraction of `D` so a retime keeps the shape. One title beat, one UI beat, one settle. For a 12 s piece:

| Fraction of D | What happens |
| --- | --- |
| 0 to 0.12 | The small line fades up word by word in open space. |
| 0.10 to 0.30 | Camera pushes in and recentres on the line until it spans about 75% of the width. |
| 0.24 to 0.30 | The highlighter sweeps left to right behind the key word. Hold. |
| 0.33 to 0.40 | Blur-dissolve: the line scene leaves while the UI scene arrives already close and moving. |
| 0.40 to 0.54 | The UI moment at real speed: typing at a fixed rate, the camera riding along with it. |
| 0.55 to 0.64 | Pull back to the whole panel so the viewer knows where they are. |
| 0.62 to 0.84 | Results arrive one by one, each followed by a short pause to read it. |
| 0.86 to 1.0 | A slow push onto the result, then a still hold. That is the end frame. |

- Each title line holds about 2 s after it lands.
- Typing runs at a fixed rate of about 22 to 25 characters per second (0.04 to 0.045 s per character), never stretched to fit `D`. If the prompt does not fit, shorten the prompt.
- Leave about 0.5 s after each result before the next thing moves.
- Times in the snippets come from `const at=f=>+(f*D).toFixed(3);`, and `q` is `document.querySelector`; both are defined in the example's script.
- For longer pieces (20 to 30 s), repeat the title beat and UI beat pair once per feature; never add a second line of text on screen at the same time.

### Camera

- The camera is a wrapper element, one per scene, moved with `xPercent`, `yPercent` and `scale`. It is the editor: no hard cuts anywhere.
- Moves are smooth eases of 0.6 to 2.4 s (`power2.inOut`, `power3.inOut`, `sine.inOut`). Never an ease that overshoots.
- Three moves only: push in (on text or a control), ride along (with typing or a filling list), pull back (to reveal context).
- Zoom between 1 and about 2.4. Past that, small UI type gets soft.
- 2D only. 3D tilts with `perspective` render flat in one of the two engines.

### Type

- Two families: a clean grotesk for lines and UI, a mono for small labels and metadata. The example uses Geist and Geist Mono. Other pairings that fit: Figtree with JetBrains Mono, Onest with IBM Plex Mono, Instrument Sans with DM Mono.
- Title lines are set small, regular or medium weight, around `min(3.2vw,5vmin)`. They become big through the camera, not the font size.
- Slight negative tracking on the title line (`-0.02em`). `white-space:nowrap` on any line the camera pushes into.
- UI type is sized in `em` from one font size on the panel, so the whole panel scales as one object and the camera maths stays stable across aspect ratios.

### Colour

- A near-white canvas, a dark ink, one accent, one grey. One accent only.
- The canvas is `--mg-bg` mixed with a little `--mg-muted`, so panels in pure `--mg-bg` lift off it without a hard border.
- The highlighter is `--mg-accent` at 15 to 20% opacity behind the word. Borders are 1px `color-mix()` of `--mg-muted` at 20 to 30%. Skip drop shadows: the panel lifts off the canvas by its tint, and large soft shadows paint displaced in one engine.
- An optional soft wash of the accent in one corner, at about 10% opacity. Never behind the UI's text.

### Motion vocabulary

The camera helper. `look(x,y,s)` centres the point at `x%`,`y%` of the camera element at zoom `s`; the identity frame is `{xPercent:0,yPercent:0,scale:1}`.

```js
const look=(x,y,s)=>({xPercent:s*(50-x),yPercent:s*(50-y),scale:s});
tl.to(".c1",{...look(30,40,2.3),duration:at(0.2),ease:"power2.inOut"},at(0.1));
```

Highlighter sweep: a real element behind the word, not a pseudo-element, so it can be tweened.

```js
tl.from(".hl-bg",{scaleX:0,transformOrigin:"0% 50%",duration:0.6,ease:"power2.inOut"},at(0.24));
```

Typing at a fixed rate: every character, spaces included, is its own span hidden with `display:none`, and an inline caret after them follows the text for free. `set` calls are seekable in both directions.

```js
const type=(el)=>{const s=[...el.textContent];el.textContent="";return s.map(ch=>{const k=document.createElement("span");k.className="k";k.textContent=ch;el.append(k);return k;});};
type(q(".typed")).forEach((k,i)=>tl.set(k,{display:"inline"},T+i*0.045));
```

Blur-dissolve between scenes (one short blur, small radius):

```js
tl.to(".s1",{autoAlpha:0,filter:"blur(6px)",duration:0.5,ease:"power1.in"},at(0.33));
tl.fromTo(".s2",{autoAlpha:0,filter:"blur(6px)"},{autoAlpha:1,filter:"blur(0px)",duration:0.6},at(0.34));
```

Step done: a spinner turns once, then a check scales in without overshoot.

```js
tl.to(spin,{rotation:360,duration:0.5,ease:"none"},t).set(spin,{autoAlpha:0},t+0.5)
  .from(ok,{scale:0.6,autoAlpha:0,duration:0.3,ease:"power2.out"},t+0.5);
```

Result line written in: `tl.fromTo(line,{clipPath:"inset(0 100% 0 0)"},{clipPath:"inset(0 0% 0 0)",duration:0.5},t)`.

### Banned moves

- Bouncy or elastic eases, `back.out`, any overshoot.
- More than one line of text on screen, or text and UI sharing the frame as equals.
- Busy backgrounds behind the UI, patterns, grain.
- A second accent colour, gradient text, glows, heavy drop shadows.
- Typing used for headlines. Typing belongs inside the product's input.
- Stretching the typing rate to fill time.
- Screenshots of the product. Rebuild the part that matters in HTML.
- Invented metrics, counts, ratings or quotes inside the UI or on screen.
- Ending on a fade to black. End on a held frame.

## Must change between uses

Two calm demos in one timeline must not look like copies. Change all five of these from the last use; pick from the lists or invent your own in the same spirit.

**Layout**

- Small line off-centre in the upper-left third; panel centred (the example).
- Line low and right; panel anchored to the left edge and cropped by the frame.
- No title beat at all: open close on an empty input with suggestion chips below.
- A table or list panel filling the frame edge to edge, line placed above it.
- Two narrow panels side by side (before and after), camera travelling between them.

**Hero device**

- Highlighter box sweeping behind the key word (the example).
- A thin underline drawing left to right under the key phrase.
- A text-selection highlight: the word gets selected as if by a cursor drag, with handles.
- A drawn cursor arrow arriving a beat early, pressing (scale 0.92 and back) on a control, with a soft ring expanding from the press.
- A table cell shimmering (a light band sliding across) before its value appears.

**Palette** (bg, fg, accent, muted)

- Cool ink: `#fcfcfb`, `#17181c`, `#4f46e5`, `#8c8f99` (the example).
- Warm paper: `#faf7f2`, `#221d17`, `#d9480f`, `#9a8f82`.
- Mint: `#f6fbf8`, `#0f1f1a`, `#0e9f6e`, `#7d918a`.
- Sky: `#f7f9fd`, `#0d1626`, `#2f6fed`, `#8492a8`.
- Brand: the brand's own light background and main colour; keep exactly one accent.

**Entry move**

- Words fade up, then the camera pushes in and recentres (the example).
- The line types into an empty input, then the camera pulls back to reveal that it was a search field.
- Start pushed in on a single word, pull back to reveal the full line.
- The panel rises small from below the centre, then the camera finds the input.
- The canvas starts empty with only a blinking caret, which becomes the first character of the line.

**End frame**

- A slow push onto the result, held (the example).
- Pull back to the whole panel with the product name small beneath it.
- Back to the plain canvas with the same line, now with a different word highlighted.
- The panel slides out of frame, leaving the name and a URL the user gave you.
- Freeze on the cursor resting over the finished result's main button.

## Fitting the renderer

| Upstream did | We cannot because | We do instead |
| --- | --- | --- |
| Silk gradient bands drawn on a canvas | No `<canvas>` | One large radial gradient element in a corner, at low opacity, drifted slowly with `x`/`y` if it should move. |
| Camera from spring and keyframe maths in a pure `seek(t)` | No custom seek; the GSAP timeline is the clock | Camera tweens on a wrapper with `power2.inOut` and `power3.inOut`; the `look()` helper replaces the keyframe tables. |
| Caret follows typing by measuring text | Fonts load after the script runs, so measuring is wrong | Per-character spans toggled with `display`, and an inline caret that follows the text by layout. |
| Motion blur from sub-frame renders | One frame per seek | Keep moves eased and under about 2.5 s; use a short `filter: blur()` only in dissolves. |
| Depth of field blur on far result cards | Large blurs are slow and engine-dependent | Far cards drop to 50% opacity and scale 0.94 instead. |
| Soundtrack with key ticks and chimes | No audio in the render | Leave it out; suggest `place_audio` for a soft bed under the graphic. |
| Logo files and embedded fonts | Images only by https URL, fonts by one Google Fonts link | Write the product name in the display font; use a logo only if the user supplies a URL. |

## Example

This is one instance of the world, not a template. Do not reuse its layout, its copy or its panel; change the five things above and write your own words for the user's product. 12 s at 1920x1080.

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=1920, height=1080">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500&family=Geist+Mono:wght@400&display=swap">
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  html,body{width:100%;height:100%;overflow:hidden}
  #root{position:relative;width:100%;height:100%;overflow:hidden;background:color-mix(in srgb,var(--mg-muted) 7%,var(--mg-bg));color:var(--mg-fg);font-family:var(--mg-font-display)}
  .clip{position:absolute;inset:0;overflow:hidden}
  .wash{position:absolute;width:90vmax;height:90vmax;right:-42vmax;top:-48vmax;border-radius:50%;background:radial-gradient(closest-side,var(--mg-accent),transparent);opacity:.1}
  .sc{position:absolute;inset:0}
  .c1{position:absolute;inset:0}
  .anchor{position:absolute;left:30%;top:40%;width:0;height:0;display:flex;align-items:center;justify-content:center}
  .line{font-weight:var(--mg-display-weight);font-size:min(3.2vw,5vmin);letter-spacing:-.02em;white-space:nowrap}
  .w{display:inline-block}
  .hl{position:relative;display:inline-block;padding:0 .12em}
  .hl-bg{position:absolute;inset:.1em 0 .02em;background:var(--mg-accent);opacity:.18;border-radius:.12em}
  .hl-t{position:relative}
  .s2{display:grid;place-items:center}
  .app{width:min(76vw,120vmin);aspect-ratio:16/10;font-size:min(1.9vw,3vmin);background:var(--mg-bg);border:1px solid color-mix(in srgb,var(--mg-muted) 30%,transparent);border-radius:1em;display:flex;flex-direction:column;overflow:hidden}
  .top{display:flex;align-items:center;gap:.4em;padding:.8em 1.2em;border-bottom:1px solid color-mix(in srgb,var(--mg-muted) 22%,transparent)}
  .top i{width:.55em;height:.55em;border-radius:50%;background:color-mix(in srgb,var(--mg-muted) 35%,transparent)}
  .top span{margin-left:.8em}
  .mono{font-family:var(--mg-font-body);font-size:.72em;color:var(--mg-muted);letter-spacing:.02em}
  .main{flex:1;display:grid;grid-template-columns:1fr 15em;gap:1.4em;padding:1.5em 1.8em}
  .left{display:flex;flex-direction:column;gap:.8em}
  .input{position:relative;display:flex;align-items:center;height:3em;padding:0 1em;border:1px solid color-mix(in srgb,var(--mg-accent) 45%,transparent);border-radius:.6em;font-size:1.05em;white-space:nowrap}
  .ph{position:absolute;left:1em;color:var(--mg-muted)}
  .k{display:none;white-space:pre}
  .caret{display:inline-block;width:.08em;height:1.15em;margin-left:.04em;background:var(--mg-accent)}
  .chips{display:flex;gap:.5em}
  .chips span{padding:.35em .8em;border-radius:2em;border:1px solid color-mix(in srgb,var(--mg-muted) 30%,transparent)}
  .steps{list-style:none;display:flex;flex-direction:column;gap:.75em;margin-top:.4em}
  .steps li{display:flex;align-items:center;gap:.7em;color:var(--mg-muted)}
  .ic{position:relative;width:1.1em;height:1.1em}
  .spin{position:absolute;inset:0;border-radius:50%;border:.14em solid color-mix(in srgb,var(--mg-muted) 30%,transparent);border-top-color:var(--mg-accent)}
  .ok{position:absolute;inset:0;width:100%;height:100%;fill:none;stroke:var(--mg-accent);stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
  .out{align-self:start;border-radius:.7em;background:color-mix(in srgb,var(--mg-muted) 6%,var(--mg-bg));border:1px solid color-mix(in srgb,var(--mg-muted) 20%,transparent);padding:1.1em 1.2em;display:flex;flex-direction:column;gap:.6em}
  .out p{font-size:.92em;line-height:1.45}
  .out b{font-weight:500}
</style>
</head>
<body>
<div id="root" data-composition-id="mg" data-start="0" data-width="1920" data-height="1080" data-duration="12" data-world="calm-demo"
  style="--mg-bg:#fcfcfb;--mg-fg:#17181c;--mg-accent:#4f46e5;--mg-muted:#8c8f99;--mg-font-display:'Geist',sans-serif;--mg-font-body:'Geist Mono',monospace;--mg-display-weight:500">
<section class="clip" data-start="0" data-duration="12">
<div class="wash"></div>
<div class="sc s1"><div class="c1"><div class="anchor"><p class="line"><span class="w">Say</span> <span class="w">what</span> <span class="w">you</span> <span class="w">need</span> <span class="w hl"><i class="hl-bg"></i><span class="hl-t">once.</span></span></p></div></div></div>
<div class="sc s2"><div class="app">
  <div class="top"><i></i><i></i><i></i><span class="mono">notes / new post</span></div>
  <div class="main">
    <div class="left">
      <span class="mono">Prompt</span>
      <div class="input"><span class="ph">What should we write?</span><span class="typed">Release note for the export panel</span><b class="caret"></b></div>
      <div class="chips mono"><span>tone: plain</span><span>length: short</span></div>
      <ul class="steps">
        <li><span class="ic"><i class="spin"></i><svg class="ok" viewBox="0 0 12 12"><path d="M2.5 6.4l2.4 2.4 4.6-5"/></svg></span>Reading recent posts</li>
        <li><span class="ic"><i class="spin"></i><svg class="ok" viewBox="0 0 12 12"><path d="M2.5 6.4l2.4 2.4 4.6-5"/></svg></span>Matching your tone</li>
        <li><span class="ic"><i class="spin"></i><svg class="ok" viewBox="0 0 12 12"><path d="M2.5 6.4l2.4 2.4 4.6-5"/></svg></span>Writing the draft</li>
      </ul>
    </div>
    <div class="out">
      <span class="mono">Draft</span>
      <p class="r"><b>Exports run in the background now.</b></p>
      <p class="r">Pick a format, keep working,</p>
      <p class="r">and the file waits in your</p>
      <p class="r">downloads when it is ready.</p>
    </div>
  </div>
</div></div>
</section>
</div>
<script>(()=>{
const D=12,at=f=>+(f*D).toFixed(3);
const tl=gsap.timeline({paused:true,defaults:{ease:"power2.inOut",duration:0.7}});
const q=(s)=>document.querySelector(s),qa=(s)=>[...document.querySelectorAll(s)];
const look=(x,y,s)=>({xPercent:s*(50-x),yPercent:s*(50-y),scale:s});
const type=(el)=>{const s=[...el.textContent];el.textContent="";return s.map(ch=>{const k=document.createElement("span");k.className="k";k.textContent=ch;el.append(k);return k;});};
tl.from(".s1 .w",{yPercent:40,autoAlpha:0,duration:0.6,stagger:0.08,ease:"power2.out"},at(0.03));
tl.to(".c1",{...look(30,40,2.3),duration:at(0.2)},at(0.1));
tl.from(".hl-bg",{scaleX:0,transformOrigin:"0% 50%",duration:0.6},at(0.24));
tl.to(".s1",{autoAlpha:0,filter:"blur(6px)",duration:0.5,ease:"power1.in"},at(0.33));
tl.fromTo(".s2",{autoAlpha:0,filter:"blur(6px)"},{autoAlpha:1,filter:"blur(0px)",duration:0.6},at(0.34));
tl.fromTo(".app",look(16,27,2.3),{...look(22,27,2.2),duration:at(0.06),ease:"sine.out"},at(0.34));
const T=at(0.41),ks=type(q(".typed")),rate=0.045;
tl.to(".caret",{opacity:0,duration:0.01,repeat:3,yoyo:true,repeatDelay:0.25,ease:"none"},at(0.36));
tl.set(".ph",{autoAlpha:0},T);
ks.forEach((k,i)=>tl.set(k,{display:"inline"},T+i*rate));
tl.to(".app",{...look(36,27,2.2),duration:ks.length*rate,ease:"sine.inOut"},T);
tl.to(".app",{xPercent:0,yPercent:0,scale:1,duration:1.1,ease:"power3.inOut"},at(0.55));
tl.to(".caret",{autoAlpha:0,duration:0.2},at(0.6));
qa(".steps li").forEach((li,i)=>{const t=at(0.63)+i*0.6,sp=li.querySelector(".spin"),ok=li.querySelector(".ok");
  tl.from(sp,{autoAlpha:0,duration:0.15},t).to(sp,{rotation:360,duration:0.5,ease:"none"},t).set(sp,{autoAlpha:0},t+0.5)
    .from(ok,{scale:0.6,autoAlpha:0,duration:0.3,ease:"power2.out"},t+0.5).to(li,{color:"var(--mg-fg)",duration:0.3},t+0.5);});
tl.fromTo(".r",{clipPath:"inset(0 100% 0 0)"},{clipPath:"inset(0 0% 0 0)",duration:0.5,stagger:0.3,ease:"power1.inOut"},at(0.78));
tl.to(".app",{...look(76,40,1.6),duration:1.4},at(0.86));
tl.set({},{},D);
window.__timelines=window.__timelines||{};window.__timelines["mg"]=tl;
})();</script>
</body>
</html>
```
