# Editorial collage

## Contents

- [Identity](#identity)
- [Use it for, not for](#use-it-for-not-for)
- [Rules](#rules)
- [Must change between uses](#must-change-between-uses)
- [Fitting the renderer](#fitting-the-renderer)
- [Example](#example)

## Identity

A documentary explainer made of paper. Each shot is a collage: strips and cards with torn edges, a halftone dot texture, bold condensed type set as solid blocks, a highlighter swipe or a marker circle that points at the one thing that matters. Everything moves on twos, holding each pose for two frames like hand-made animation, so nothing glides. Every name, date and number on screen is a fact with a small source line under it. The tone is a curious "why", answered in a few shots, closing on a takeaway of five words or fewer.

## Use it for, not for

Use it for: "why does X happen" answers, a surprising fact or two with their sources, before and after comparisons, history in a few dates, myth versus fact, a newsletter or report promo.

Not for: hype launches (use a beat-cut world), product walkthroughs (use a one-take world), anything without real facts to show, luxury or very calm brands, or claims the user cannot source.

## Rules

### Pacing and beat map

Length 8 to 15 seconds; 10 is the default. Three to five shots, each held long enough to read aloud. Shot 1 asks the question big within the first second. As fractions of D, for three shots:

| Span | Shot |
| --- | --- |
| 0 to 0.3 | The question, on torn strips that step in one after another, with a texture element (halftone disc, clipped photo, map scrap) beside it. |
| 0.3 to 0.65 | The facts: one to three torn cards drop in, each with a date chip, one big number or word, one line of explanation, and a source line. A number counts up in steps. A highlighter swipes behind the key phrase. |
| 0.65 to 1 | The takeaway, five words or fewer, on a new sheet that steps in over the last shot. A marker circle draws around the punch word, then the sources line, then hold for at least 0.12 D. |

Shots change by a new sheet of paper stepping in over the previous one, from a side or from below, never by a fade.

### Camera

Static. The frame is a tabletop seen from above. Depth comes from paper layers: each card or strip carries an offset shadow in a tint of `--mg-fg`, and pieces sit at small angles (between -4 and 4 degrees, set with `tl.set(el,{rotation:n},0)`, never a CSS transform). Compose asymmetrically: left-aligned strips, a texture element bleeding off one edge. Never centre a lone title.

### Type

A condensed sans at 700 to 800 for every headline and number, set tight (line-height 0.9 to 0.95) as solid blocks (Barlow Condensed, Oswald, Antonio or Fjalla One). A text serif for explanation and source lines (Source Serif 4, Literata, Newsreader or Crimson Pro). Source lines are small (about 1.8 vmin), start with "Source:", and sit directly under the fact they support.

### Colour

Paper, ink, a marker colour and a texture colour: `--mg-bg` is the paper, `--mg-fg` the ink (strips, sheets, type), `--mg-accent` the highlighter and marker (always a saturated, light colour so ink text stays readable on it), `--mg-muted` the halftone and source text. Shadows and tints come from `color-mix`. A fine paper grain over the whole frame is allowed.

```css
.ht{border-radius:50%;background:radial-gradient(circle,var(--mg-muted) 32%,transparent 34%) 0 0/2.2vmin 2.2vmin}
.t1{clip-path:polygon(0 6%,4% 0,9% 5%,15% 1%,22% 7%,30% 2%,100% 5%,100% 94%,70% 93%,46% 100%,0 99%)}
```

### Motion vocabulary

- **On twos**: every tween uses `ease:"steps(N)"` with `N = round(duration * 12)`, so a 0.5 s move takes six held poses. Put it in the timeline `defaults`. There is no smooth easing in this world, except none.
- **Step in**: a strip or sheet arrives from off its edge, `xPercent:-105` to 0 or `yPercent:100` to 0, in 0.4 to 0.5 s.
- **Drop**: a card falls from `yPercent:-30` with `autoAlpha:0` in four steps, already at its angle.
- **Highlighter**: an `<i>` behind the phrase scales `scaleX` 0 to 1 from the left in six steps.
- **Marker circle**: an SVG ellipse drawn on with `strokeDashoffset`, using a dash length a little longer than its real perimeter. Do not use `pathLength` for this: dash animation against `pathLength` came out blank or wrong through the renderer in both engines.
- **Stepped count**: the counter helper with `ease:"steps(12)"`, so the number ticks, not spins.
- **One `from` per element**: two `tl.from()` tweens that both reach the same element (a shared class such as `.ht` used in two shots) leave it invisible, because the second records the first one's hidden start as its end. Scope each selector to its shot (`.a .ht`).

```js
const tl=gsap.timeline({paused:true,defaults:{ease:"steps(6)",duration:0.5}});
tl.from(".strip",{xPercent:-105},0.3);
tl.from(".card1",{yPercent:-30,autoAlpha:0,ease:"steps(4)",duration:0.33},3.6);
tl.from(".hl",{scaleX:0},5);
tl.fromTo(".ring",{strokeDashoffset:260},{strokeDashoffset:0,ease:"steps(8)",duration:0.67},7.6);
```

### Banned moves

Smooth eases, fades between shots, typewriter text, bouncing, glows, gradients on text, a centred title on a flat background, any number, name or date without a source line, "the reason" (say "one reason"), real brand logos drawn from memory, more than one highlighter per shot.

## Must change between uses

Two editorial-collage graphics in the same project must not look alike. Change all five and say which you picked.

- **Layout**: strips stacked down the left with texture right; a two-card spread; a single big card with a date chip that flips between two dates (before and after); a grid of identical tiles where one breaks the pattern; a map or photo scrap with a labelled call-out.
- **Hero device**: a halftone disc; a torn photo scrap (absolute https image) clipped to a torn edge; a date chip that flips; a stamped label ("CONFIRMED", "MYTH"); a ruler or timeline strip with ticks.
- **Palette**: cream, navy, marigold, umber; newsprint grey `#e9e7e1`, black ink, signal red `#e8412c` (highlight with red only behind light type), slate; kraft brown `#d8c3a0`, deep green `#183a2e`, lemon `#f6e65a`, rust; pale blue `#e3ecf2`, oxblood `#5a1520`, coral `#ff8a65`, steel.
- **Entry move**: a strip steps in from the left; a card drops onto the table; a sheet slides up from below; the halftone disc stamps in first, then the question.
- **End frame**: takeaway on a dark sheet with a marker circle; takeaway on a light strip with a highlighter; the fact cards pushed into a neat stack with the takeaway on top; a stamp lands over the last card.

## Fitting the renderer

Upstream draws this look per frame in a canvas, with paper grain, halftone and film grain over the whole frame, colour fringing at the frame edges, a vignette, a narration track and a score. Our substitutes:

- Torn edges are a CSS `clip-path: polygon(...)` with jagged points. It renders the same in Chromium and WebKit through the renderer. Clip-path also clips box shadows, so each piece has a sibling shadow element with the same clip, offset by about 1 vmin.
- Halftone is a `radial-gradient` dot pattern on a round element; paper grain is an inline SVG `feTurbulence` layer at low opacity (renders in both engines).
- "On twos" is `ease:"steps(N)"` per tween.
- Colour fringing is dropped: it needs blend modes or filters per channel, which we keep out of this world.
- No narration or score in the render. Tell the user the shots are timed to be read aloud and lay voice and music in with `place_audio`.

## Example

This is one instance of the world, answering one question with two sourced facts. Do not reuse its question, its facts, its layout or its palette; take the user's own question and sourced facts, and change everything listed under "Must change between uses". Never put a fact on screen that the user or a source you checked did not give you.

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=1920, height=1080">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@800&family=Source+Serif+4:wght@400;600&display=swap">
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:100%;height:100%;overflow:hidden}
#root{position:relative;width:100%;height:100%;overflow:hidden;background:var(--mg-bg);color:var(--mg-fg);font-family:var(--mg-font-body)}
.clip{position:absolute;inset:0}
.shot{position:absolute;inset:0;display:flex;flex-direction:column;justify-content:center;align-items:flex-start;padding:8vmin}
.b{background:var(--mg-bg);clip-path:polygon(3% 0,100% 0,100% 100%,2% 100%,0 94%,3% 86%,1% 78%,4% 70%,1% 61%,3% 52%,0 44%,3% 35%,1% 26%,4% 17%,1% 8%);flex-direction:row;flex-wrap:wrap;align-items:center;justify-content:center;gap:5vmin}
.c{background:var(--mg-fg);color:var(--mg-bg);clip-path:polygon(0 3%,7% 0,15% 2.5%,24% 0.5%,33% 3%,44% 0,55% 2.5%,66% 0.5%,77% 3%,88% 0,100% 2%,100% 100%,0 100%);padding-left:12vmin}
.disp{font-family:var(--mg-font-display);font-weight:var(--mg-display-weight);line-height:0.92;text-transform:uppercase}
.kick{font-size:2.4vmin;font-weight:600;letter-spacing:0.12em;text-transform:uppercase;color:var(--mg-muted);margin-bottom:3vmin}
.piece{position:relative}
.sh,.pp{clip-path:polygon(0 6%,4% 0,9% 5%,15% 1%,22% 7%,30% 2%,37% 6%,45% 0,53% 5%,61% 1%,69% 6%,77% 2%,85% 7%,92% 1%,100% 5%,100% 94%,94% 100%,86% 95%,78% 99%,70% 93%,62% 98%,54% 94%,46% 100%,38% 95%,30% 99%,22% 93%,14% 98%,7% 94%,0 99%)}
.sh{position:absolute;inset:0;left:1vmin;top:1.2vmin;background:color-mix(in srgb,var(--mg-fg) 22%,transparent)}
.pp{position:relative;background:var(--mg-fg);color:var(--mg-bg);padding:4vmin 5vmin}
.q{font-size:min(12vw,15vmin)}
.q span{display:block}
.scrap{margin:-1.5vmin 0 0 6vmin}
.scrap .pp{background:var(--mg-accent);color:var(--mg-fg);padding:3vmin 4.5vmin}
.ht{position:absolute;right:-12vmin;bottom:-14vmin;width:62vmin;height:62vmin;border-radius:50%;background:radial-gradient(circle,var(--mg-muted) 32%,transparent 34%) 0 0/2.2vmin 2.2vmin;opacity:0.7}
.card{width:min(42vw,62vmin)}
.card .pp{background:color-mix(in srgb,var(--mg-fg) 4%,var(--mg-bg));color:var(--mg-fg);padding:5vmin 4.5vmin 4.5vmin;border:0}
.card .sh{background:color-mix(in srgb,var(--mg-fg) 30%,transparent)}
.chip{display:inline-block;background:var(--mg-fg);color:var(--mg-bg);font-weight:600;font-size:2.3vmin;padding:0.6vmin 1.6vmin;letter-spacing:0.06em}
.big{font-size:min(15vw,20vmin);margin:2vmin 0 1vmin;white-space:nowrap;font-variant-numeric:tabular-nums}
.big small{font-size:0.45em}
.txt{font-size:min(3.6vw,3.3vmin);line-height:1.3}
.mk{position:relative;z-index:0;white-space:nowrap}
.hl{position:absolute;left:-0.2em;right:-0.2em;top:0.15em;bottom:0;background:var(--mg-accent);z-index:-1;transform-origin:0 50%}
.src{font-size:1.8vmin;color:var(--mg-muted);margin-top:2.4vmin}
.take{font-size:min(15vw,19vmin)}
.l2{margin-top:3vmin}
.ring{position:absolute;left:-6%;top:-4%;width:112%;height:112%;overflow:visible;stroke:var(--mg-accent)}
.h2{right:-8vmin;top:-10vmin;bottom:auto;opacity:0.45;background-image:radial-gradient(circle,var(--mg-accent) 32%,transparent 34%)}
.c .src{color:var(--mg-bg);opacity:0.7;margin-top:6vmin;font-size:2vmin}
.grain{position:absolute;inset:0;width:100%;height:100%;opacity:0.14}
</style>
</head>
<body><div id="root" data-composition-id="mg" data-start="0" data-width="1920" data-height="1080" data-duration="10" data-world="editorial-collage"
  style="--mg-bg:#f1e6d0;--mg-fg:#1b2440;--mg-accent:#f2c230;--mg-muted:#8a7558;--mg-font-display:'Barlow Condensed',sans-serif;--mg-font-body:'Source Serif 4',serif;--mg-display-weight:800">
<section class="clip" data-start="0" data-duration="10">
<div class="shot a"><div class="ht"></div>
<p class="kick">Explainer &#183; why things are the way they are</p>
<div class="piece strip"><div class="sh"></div><div class="pp"><p class="disp q"><span>Why is every</span><span>shipping box</span></p></div></div>
<div class="piece scrap"><div class="sh"></div><div class="pp"><p class="disp q">the same size?</p></div></div>
</div>
<div class="shot b">
<div class="piece card card1"><div class="sh"></div><div class="pp"><span class="chip">26 April 1956</span><p class="disp big"><span class="n" data-to="58">0</span> <small>boxes</small></p><p class="txt">The Ideal X carries them from Newark to Houston.</p><p class="src">Source: Wikipedia, &#8220;SS Ideal X&#8221;</p></div></div>
<div class="piece card card2"><div class="sh"></div><div class="pp"><span class="chip">1968</span><p class="disp big">8 <small>ft wide</small></p><p class="txt">ISO 668 fixes <span class="mk"><i class="hl"></i>every box's width</span> at 2.438 m.</p><p class="src">Source: ISO 668</p></div></div>
</div>
<div class="shot c"><div class="ht h2"></div>
<p class="disp take l1">Same box,</p>
<p class="disp take l2"><span class="mk">every port.<svg class="ring" viewBox="0 0 100 40" preserveAspectRatio="none"><ellipse cx="50" cy="20" rx="48" ry="18" fill="none" stroke-width="2.2" stroke-dasharray="260" stroke-dashoffset="260"/></svg></span></p>
<p class="src">Sources: Wikipedia, &#8220;SS Ideal X&#8221;; ISO 668</p>
</div>
<svg class="grain" xmlns="http://www.w3.org/2000/svg"><filter id="pg"><feTurbulence type="fractalNoise" baseFrequency="0.7" numOctaves="2" seed="4"/></filter><rect width="100%" height="100%" filter="url(#pg)"/></svg>
</section>
</div>
<script>(()=>{
const D=10;
const tl=gsap.timeline({paused:true,defaults:{ease:"steps(6)",duration:0.5}});
const q=s=>document.querySelector(s);
tl.set(".strip",{rotation:-2},0).set(".scrap",{rotation:2.5},0).set(".card1",{rotation:-3},0).set(".card2",{rotation:2},0).set(".l1",{rotation:-1.5},0);
tl.from(".kick",{autoAlpha:0,ease:"steps(2)",duration:0.17},0.15);
tl.from(".strip",{xPercent:-105},0.3);
tl.from(".scrap",{yPercent:-60,autoAlpha:0,ease:"steps(4)",duration:0.33},1);
tl.from(".a .ht",{scale:0.5,autoAlpha:0},1.3);
tl.from(".b",{xPercent:100},3.1);
tl.from(".card1",{yPercent:-30,autoAlpha:0,ease:"steps(4)",duration:0.33},3.6);
const n=q(".n"),o={v:0};tl.to(o,{v:58,duration:1,ease:"steps(12)",onUpdate:()=>{n.textContent=Math.round(o.v);}},3.8);
tl.from(".card2",{yPercent:-30,autoAlpha:0,ease:"steps(4)",duration:0.33},4.6);
tl.from(".hl",{scaleX:0},5.1);
tl.from(".c",{yPercent:100},6.5);
tl.from(".l1",{yPercent:40,autoAlpha:0,ease:"steps(3)",duration:0.25},7);
tl.from(".l2",{yPercent:40,autoAlpha:0,ease:"steps(3)",duration:0.25},7.25);
tl.to(".ring ellipse",{strokeDashoffset:0,ease:"steps(8)",duration:0.67},7.6);
tl.from(".h2",{scale:0.5,autoAlpha:0},7.9);
tl.from(".c .src",{autoAlpha:0,ease:"steps(2)",duration:0.17},8.3);
tl.set({},{},D);
window.__timelines=window.__timelines||{};
window.__timelines["mg"]=tl;
})();</script>
</body>
</html>
```
