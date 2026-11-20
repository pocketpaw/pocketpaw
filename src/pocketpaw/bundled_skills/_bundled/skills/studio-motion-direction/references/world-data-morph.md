# Data morph

## Contents

- [Identity](#identity)
- [Use it for, not for](#use-it-for-not-for)
- [Rules](#rules): pacing and beat map, camera, type, colour, motion vocabulary, banned moves
- [Must change between uses](#must-change-between-uses)
- [Fitting the renderer](#fitting-the-renderer)
- [Example](#example)

## Identity

One chart that changes shape in front of the viewer. Bars pull up into their own top edges and become the points of a line, a number grows out of a bar, a pair of columns slides into a before and after. Nothing fades out and gets replaced: every mark on screen at the end is a mark that was there at the start, transformed. The frame is set like a printed data page: a tall condensed headline off to one side, mono labels, hairline gridlines, one bright accent for the thing that changes. The title, labels and axis never move; only the data does. It runs in four states (start, change, hold, return) and ends exactly where it began, so it can sit as a still or loop cleanly.

## Use it for, not for

Use it for:

- One result the user gives you: a trend, a before and after, a share of a whole, a single milestone number.
- A data beat inside a longer edit, 6 to 10 s.
- Social posts where the clip may loop.

Not for:

- Numbers the user did not give you. With no data, either ask, or build it with sample values and the "Illustrative data" label on every frame.
- More than one chart or more than about eight data points. Split it into two graphics.
- Product walkthroughs (calm-demo) or launch announcements (launch-beats).

## Rules

### Pacing and beat map

Four states. For an 8 s piece:

| Fraction of D | State | What happens |
| --- | --- | --- |
| 0 to 0.12 | Start | The chart in its first form, complete and readable. Nothing moves except a slow drift you may add to the background. |
| 0.12 to 0.42 | Change | The one transformation, about 2 to 2.5 s, staggered left to right. |
| 0.42 to 0.78 | Hold | The finished form with the headline value labelled. About 3 s; this is where the viewer reads. |
| 0.78 to 0.96 | Return | The change runs backwards in about 1.5 s, in reverse order. |
| 0.96 to 1.0 | End | The start state, held. The last frame matches the first. |

- Times in the snippets come from `const at=f=>+(f*D).toFixed(3);`, and `q` is `document.querySelector`; both are defined in the example's script. `count` is the house helper from `studio-motion/references/presets.md`.
- One change per graphic. Bars to a line, a number to a bar, a share to a whole: pick one.
- Stagger left to right in the change and right to left in the return, 0.06 to 0.12 s per mark.
- For a non-looping use, the start state may build in (bars rising from the baseline in the first 0.12 D) and the return may be replaced by a hold on the changed form. Say which you chose.

### Camera

- None. The chart is the subject and the axis is the reference; a moving camera makes the values unreadable.
- The only scale change allowed is on the callout or number that is revealed, from its own anchor point.

### Type

- A condensed display face for the headline, a mono for every number, label and axis tick. The example uses Big Shoulders Display with IBM Plex Mono. Other pairings: Oswald with JetBrains Mono, Barlow Condensed with Space Mono, Saira Condensed with DM Mono.
- Every value in the mono, with tabular figures, so counting digits do not jitter.
- Values written exactly as given: no rounding, no added "+" or "k", no recomputed percentages unless the user asks.
- The headline sits off to one side (left column on wide frames, top on tall ones), never centred over the chart.

### Colour

- A dark ground, a light ink, one accent for the changing element, one muted tone for axis, gridlines and unselected marks.
- Bars in the start state are muted with a thin accent cap; the accent grows with the change (the dots, the line, the callout).
- Never tween a colour to or from `var(--mg-*)`: GSAP cannot interpolate a variable and the element turns transparent mid-tween. Change colour by cross-fading two stacked elements, or keep it constant and change shape.
- Gridlines are hairlines in `color-mix()` of the muted tone at 30 to 40%.
- No gradients on marks, no glow.

### Motion vocabulary

A bar collapses up into its own cap, and the cap becomes a dot. The bar keeps its place; only its form changes.

```js
tl.to(".bar",{scaleY:0,transformOrigin:"50% 0%",duration:0.7,stagger:0.08,ease:"power2.inOut"},at(0.12));
tl.fromTo(".cap",{width:"2.4em",height:"0.45em",borderRadius:"0.1em"},{width:"0.9em",height:"0.9em",borderRadius:"0.45em",duration:0.6,stagger:0.08,ease:"power2.inOut"},at(0.16));
```

The line draws through the dots. Use a `clip-path` reveal on the SVG, which works in both engines, with `vector-effect="non-scaling-stroke"` so the stroke stays even.

```js
tl.fromTo(".line",{clipPath:"inset(0% 100% 0% 0%)"},{clipPath:"inset(0% 0% 0% 0%)",duration:1.1,ease:"power1.inOut"},at(0.26));
```

A number arrives with a scale reveal from its anchor and counts to its exact value (counting is the one place a proxy object with `onUpdate` is fine):

```js
tl.from(".call",{scale:0.5,autoAlpha:0,transformOrigin:"100% 50%",duration:0.5,ease:"power3.out"},at(0.4));
count(q(".call .n"),at(0.4),0.6);
```

The return mirrors the change in reverse order, using `fromTo` so the end state is exact. Give every return `fromTo` `immediateRender:false`; without it GSAP paints its from-state when the script runs, and frame 0 shows the changed chart instead of the start:

```js
tl.fromTo(".line",{clipPath:"inset(0% 0% 0% 0%)"},{clipPath:"inset(0% 100% 0% 0%)",duration:0.6,ease:"power1.in",immediateRender:false},at(0.8));
tl.to(".bar",{scaleY:1,duration:0.6,stagger:{each:0.06,from:"end"},ease:"power2.out"},at(0.85));
```

Other changes in the same spirit:

- **Number becomes a bar:** the number sits on the baseline, its box scales up on `scaleY` from the bottom into a bar of the right height, and the digits ride on top as its label.
- **Share of a whole:** a row of 100 small squares (a 10 by 10 grid of DOM cells), with the share's cells turning to the accent in order.
- **Before and after:** two bars side by side; the "after" bar grows past the "before" bar, and a bracket between them states the difference the user gave.

### Banned moves

- Fade out one chart and fade in another. Every mark must transform.
- Moving the title, labels or axis.
- Inventing, rounding or "cleaning up" a number. Adding a "+" to a milestone.
- Sample data without the "Illustrative data" label visible in every frame.
- Bounce, overshoot, confetti, sparkle bursts, glows, gradients on bars.
- 3D bars or tilted charts. `perspective` renders flat in one of the two engines.
- More than one accent colour.

## Must change between uses

Two data graphics on one timeline must differ in all five of these. Pick from the lists or invent your own in the same spirit.

**Layout**

- Headline column on the left, chart taking the right two thirds (the example). On tall frames the headline moves to the top.
- Chart full width along the bottom half, headline and source line stacked top-left.
- Headline top-right, right-aligned, chart bottom-left with its axis on the right side.
- One giant number filling the left half, a small supporting chart in the right half.

**Hero device**

- Bars collapse into dots and a line draws through them (the example).
- A number on the baseline grows into its bar.
- A 10 by 10 grid of cells fills to the share in the accent.
- Two bars, before and after, with a bracket stating the difference.
- Digits assembled from many small marks (see below), for one milestone number.

**Palette** (bg, fg, accent, muted)

- Forest and lime: `#0e1a16`, `#e8efe4`, `#c6f432`, `#6f8a80` (the example).
- Newsprint: `#efeae0`, `#1b1a17`, `#d4380d`, `#8a8478`.
- Slate and amber: `#141820`, `#eceff4`, `#ffb020`, `#6b7689`.
- Chalk and cobalt: `#f4f5f7`, `#101418`, `#1f5eff`, `#8b929c`.

**Entry move**

- No entry: the start state is complete at frame 0, so the clip loops (the example).
- Bars rise from the baseline, left to right, in the first 0.12 D.
- Gridlines draw left to right, then the marks appear on them.
- The headline value types in digit by digit at a fixed rate in the mono, then the chart appears beside it.

**End frame**

- Back to the exact start state (the example).
- Hold on the changed form with the callout.
- The callout grows to become the headline number while the chart dims to the muted tone.
- The source line appears under the chart and everything holds.

## Fitting the renderer

| Upstream did | We cannot because | We do instead |
| --- | --- | --- |
| Thousands of canvas particles pulling together into a number | No `<canvas>`; thousands of DOM nodes are too slow and too large | A few hundred small DOM or SVG marks at most, placed by a seeded LCG and tweened into the digit cells of a monospace grid; or a count with a scale reveal, as in the example. |
| Sampling a number's pixels to find particle targets | No pixel reads; fonts load after the script runs | Lay the digits out on a fixed grid (for example a 5 by 7 dot matrix per digit) and send each mark to a cell. |
| Pure `seek(t)` functions with a frame-exact loop seam | The GSAP timeline is the clock | `fromTo` on every return tween, so the last frame equals the first. |
| Export to MP4 and GIF at a fixed size | One composition per graphic, sized by the project | Fluid layout with a portrait media query; set `data-width`/`data-height` and the viewport. |
| A shimmer while the number holds | Continuous random motion is not deterministic | Hold still, or one slow highlight sweep across the number. |

A seeded generator for the mark-assembly variant (never `Math.random`):

```js
let seed=7;const rnd=()=>(seed=(seed*1664525+1013904223)>>>0)/4294967296;
marks.forEach((m,i)=>tl.fromTo(m,{xPercent:(rnd()-0.5)*4000,yPercent:(rnd()-0.5)*2400,autoAlpha:0.25},{xPercent:0,yPercent:0,autoAlpha:1,duration:1.6,ease:"power3.inOut"},at(0.12)+rnd()*0.6));
```

Keep the mark count under about 300 so the composition stays under its size budget and every frame paints quickly.

## Example

This is one instance of the world, not a template. Do not reuse its layout, its title or its data; change the five things above and use the user's own values. The values here are illustrative and labelled so on every frame. 8 s at 1920x1080.

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=1920, height=1080">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Big+Shoulders+Display:wght@800&family=IBM+Plex+Mono:wght@400;500&display=swap">
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  html,body{width:100%;height:100%;overflow:hidden}
  #root{position:relative;width:100%;height:100%;overflow:hidden;background:var(--mg-bg);color:var(--mg-fg);font-family:var(--mg-font-body)}
  .clip{position:absolute;inset:0;display:grid;grid-template-columns:minmax(0,.8fr) minmax(0,1.6fr);gap:7vmin;padding:9vmin 7vmin 8vmin}
  .tag{position:absolute;right:4vmin;top:4vmin;font-size:min(1.7vw,2.4vmin);letter-spacing:.14em;text-transform:uppercase;color:var(--mg-muted);border:1px solid color-mix(in srgb,var(--mg-muted) 60%,transparent);padding:.45em .8em;border-radius:.3em}
  .head{display:flex;flex-direction:column;justify-content:flex-end;gap:2.5vmin}
  .head h1{font-family:var(--mg-font-display);font-weight:var(--mg-display-weight);font-size:clamp(7vmin,8vw,13vmin);line-height:.92;text-transform:uppercase}
  .head p{font-size:min(2vw,2.8vmin);color:var(--mg-muted);line-height:1.4}
  .chart{display:flex;flex-direction:column;font-size:min(1.9vw,2.6vmin);padding-left:3.2em;min-height:0}
  .plot{position:relative;flex:1;min-height:0}
  .grid{position:absolute;left:0;right:0;height:0;border-top:1px solid color-mix(in srgb,var(--mg-muted) 35%,transparent)}
  .grid span{position:absolute;right:100%;top:0;transform:translateY(-50%);padding-right:.8em;font-size:.8em;color:var(--mg-muted);font-variant-numeric:tabular-nums}
  .bar{position:absolute;bottom:0;width:2.4em;margin-left:-1.2em;background:color-mix(in srgb,var(--mg-muted) 55%,transparent)}
  .line{position:absolute;inset:0;width:100%;height:100%;overflow:visible}
  .line polyline{fill:none;stroke:var(--mg-accent);stroke-width:.3vmin;stroke-linejoin:round}
  .pt{position:absolute;width:0;height:0;display:flex;align-items:center;justify-content:center}
  .cap{flex:none;width:2.4em;height:.45em;border-radius:.1em;background:var(--mg-accent)}
  .call{position:absolute;right:1.6em;bottom:-1em;white-space:nowrap;display:flex;align-items:baseline;gap:.5em;padding:.35em .7em;border-radius:.3em;background:var(--mg-accent);color:var(--mg-bg);font-weight:500}
  .call .n{font-size:1.5em;font-variant-numeric:tabular-nums}
  .xl{display:flex;height:2.4em;align-items:flex-end}
  .xl span{flex:1;text-align:center;font-size:.85em;color:var(--mg-muted);letter-spacing:.06em}
  @media (max-aspect-ratio:1/1){.clip{grid-template-columns:1fr;grid-template-rows:auto minmax(0,1fr);gap:6vmin;padding:14vmin 7vmin 9vmin}.chart{font-size:3vw}.head h1{font-size:11vw}.head p{font-size:3.2vw}.tag{font-size:2.6vw}}
</style>
</head>
<body>
<div id="root" data-composition-id="mg" data-start="0" data-width="1920" data-height="1080" data-duration="8" data-world="data-morph"
  style="--mg-bg:#0e1a16;--mg-fg:#e8efe4;--mg-accent:#c6f432;--mg-muted:#6f8a80;--mg-font-display:'Big Shoulders Display',sans-serif;--mg-font-body:'IBM Plex Mono',monospace;--mg-display-weight:800">
<section class="clip" data-start="0" data-duration="8">
<p class="tag">Illustrative data</p>
<div class="head"><h1>Teams on shared drafts</h1><p>Monthly active teams,<br>January to June</p></div>
<div class="chart">
  <div class="plot">
    <div class="grid" style="bottom:0"><span>0</span></div>
    <div class="grid" style="bottom:44.44%"><span>200</span></div>
    <div class="grid" style="bottom:88.89%"><span>400</span></div>
    <div class="bar" style="left:8.33%;height:26.67%"></div>
    <div class="bar" style="left:25%;height:40%"></div>
    <div class="bar" style="left:41.67%;height:36.67%"></div>
    <div class="bar" style="left:58.33%;height:53.33%"></div>
    <div class="bar" style="left:75%;height:68.89%"></div>
    <div class="bar" style="left:91.67%;height:91.56%"></div>
    <svg class="line" viewBox="0 0 600 100" preserveAspectRatio="none"><polyline vector-effect="non-scaling-stroke" points="50,73.33 150,60 250,63.33 350,46.67 450,31.11 550,8.44"/></svg>
    <div class="pt" style="left:8.33%;bottom:26.67%"><i class="cap"></i></div>
    <div class="pt" style="left:25%;bottom:40%"><i class="cap"></i></div>
    <div class="pt" style="left:41.67%;bottom:36.67%"><i class="cap"></i></div>
    <div class="pt" style="left:58.33%;bottom:53.33%"><i class="cap"></i></div>
    <div class="pt" style="left:75%;bottom:68.89%"><i class="cap"></i></div>
    <div class="pt" style="left:91.67%;bottom:91.56%"><i class="cap"></i><span class="call"><span class="n" data-to="412">412</span>teams in June</span></div>
  </div>
  <div class="xl"><span>Jan</span><span>Feb</span><span>Mar</span><span>Apr</span><span>May</span><span>Jun</span></div>
</div>
</section>
</div>
<script>(()=>{
const D=8,at=f=>+(f*D).toFixed(3);
const tl=gsap.timeline({paused:true,defaults:{ease:"power2.inOut",duration:0.6}});
const q=(s)=>document.querySelector(s);
const fmt=(v,dp)=>v.toLocaleString("en-US",{minimumFractionDigits:dp,maximumFractionDigits:dp});
const count=(el,at,dur)=>{const to=Number(el.dataset.to)||0,dp=Number(el.dataset.decimals)||0,o={v:0};el.textContent=fmt(0,dp);tl.to(o,{v:to,duration:dur,ease:"power2.out",onUpdate:()=>{el.textContent=fmt(o.v,dp);}},at);};
const bar={width:"2.4em",height:"0.45em",borderRadius:"0.1em"},dot={width:"0.9em",height:"0.9em",borderRadius:"0.45em"};
tl.to(".bar",{scaleY:0,transformOrigin:"50% 0%",duration:0.7,stagger:0.08},at(0.12));
tl.fromTo(".cap",bar,{...dot,stagger:0.08},at(0.16));
tl.fromTo(".line",{clipPath:"inset(0% 100% 0% 0%)"},{clipPath:"inset(0% 0% 0% 0%)",duration:1.1,ease:"power1.inOut"},at(0.26));
tl.fromTo(".call",{scale:0.5,autoAlpha:0},{scale:1,autoAlpha:1,transformOrigin:"100% 50%",duration:0.5,ease:"power3.out"},at(0.4));
count(q(".call .n"),at(0.4),0.6);
tl.to(".call",{scale:0.5,autoAlpha:0,duration:0.35,ease:"power2.in"},at(0.78));
tl.fromTo(".line",{clipPath:"inset(0% 0% 0% 0%)"},{clipPath:"inset(0% 100% 0% 0%)",duration:0.6,ease:"power1.in",immediateRender:false},at(0.8));
tl.fromTo(".cap",dot,{...bar,duration:0.5,stagger:{each:0.06,from:"end"},immediateRender:false},at(0.83));
tl.to(".bar",{scaleY:1,duration:0.6,stagger:{each:0.06,from:"end"},ease:"power2.out"},at(0.85));
tl.set({},{},D);
window.__timelines=window.__timelines||{};window.__timelines["mg"]=tl;
})();</script>
</body>
</html>
```
