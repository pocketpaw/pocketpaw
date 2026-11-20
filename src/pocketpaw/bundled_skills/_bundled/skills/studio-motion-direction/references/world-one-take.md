# One take

## Contents

- [Identity](#identity)
- [Use it for, not for](#use-it-for-not-for)
- [Rules](#rules)
- [Must change between uses](#must-change-between-uses)
- [Fitting the renderer](#fitting-the-renderer)
- [Example](#example)

## Identity

One continuous shot with no cuts. The product's workflow is laid out as small interface stations on a stage far larger than the frame, and one hero object (a ball, a cursor, a file tile, a coin) travels from station to station. Every station visibly reacts when the hero touches it: a toggle switches, a slider fills, a notification pops, a button presses. The camera follows the hero, framing each station while it reacts, and the piece pays off by pulling far out so the viewer sees the whole course at once. The story is told by the stations' own labels; there is no caption layer.

## Use it for, not for

Use it for: how a product works in three to six steps, onboarding flows, "from idea to live" stories, any process where cause and effect is the point, playful product teasers.

Not for: a single statement or quote (use a type-led world), data that needs reading time, a logo sting under four seconds, anything with more than six steps (split it into two graphics), or abstract ideas that have no interface to touch.

## Rules

### Pacing and beat map

Length 10 to 16 seconds; 12 is the default. More steps make it longer, never slower. Each contact (a landing, a press) is a beat, and the beats roughly double in spacing as the story builds, then the pull-out takes a long, slow stretch. As fractions of D:

| Span | What happens |
| --- | --- |
| 0 to 0.08 | Open tight on station 1. The hero is already part of it (the toggle knob, the slider thumb, the caret) and acts at once. |
| 0.08 to 0.5 | The middle stations, one contact every 0.04 to 0.1 D. Hops between stations take 0.05 to 0.07 D. |
| 0.5 to 0.58 | The last station fires: a button press, a toast, a live URL. Hold on the result for 0.04 D, fully framed. |
| 0.58 to 0.8 | The launch. The camera starts pulling out a beat before the hero leaves, with a fast-out ease (`power3.out`), so the hero is never launched out of frame; then the hero arcs high and slow back toward the start over the wide course. |
| 0.8 to 1 | The hero lands. The wide shot holds as the end frame, with a slow push of a few percent so it never freezes dead. |

### Camera

- The camera is two real elements: `.cam`, a zero-size box pinned at the frame centre that carries `scale` (and an optional small `rotation`), and `.world` inside it, a stage about 300 by 200 vmin that carries `xPercent`/`yPercent`. With the world 300 vmin wide, `xPercent:-x/3` puts world point x (in vmin) at the frame centre; the same for y over 2.
- Move both with one helper so a framing is three numbers: centre x, centre y, scale. Camera moves start a little before the hero leaves a station and land a little after it arrives, with `power2.inOut` or `sine.inOut`, never linear.
- Interaction framing is tight: scale 1.2 to 1.6, the station filling about half the frame width. Between stations, widen slightly (scale 0.9 to 1.1) so the next station is already in view.
- The reveal framing centres on the middle of the course's bounding box and must fit the whole course at the frame's short side, so it survives 9:16 (scale near 0.35 to 0.45; for a course 250 vmin wide, at most 0.38 including any end push). A 2D `rotation` of 2 to 4 degrees on the pull-out adds depth; take it back to 0 by the end.
- The hero never leaves the middle half of the frame during an interaction.

```js
const shot=(x,y,s,at,dur,ease="power2.inOut")=>{
  tl.to(".world",{xPercent:-x/3,yPercent:-y/2,duration:dur,ease},at);
  tl.to(".cam",{scale:s,duration:dur,ease},at);};
```

### Type

A geometric sans for station values and the one name the piece carries (Sora, Outfit or Lexend at 600 to 700), with a mono for labels, card titles and tooltips (IBM Plex Mono, Martian Mono or DM Mono at 400 to 500). Labels are uppercase mono at about 2.2 to 2.6 vmin in world units, set `white-space:nowrap`. Every word on screen belongs to an interface element: a label, a card title, a button. No headline floats over the course.

### Colour

A deep, quiet stage (dark green, ink blue, aubergine, or a warm light grey for a light variant) with a faint dot grid in `--mg-muted`; the grid is what makes camera moves readable, so never drop it. Station surfaces are tints of `--mg-fg` over `--mg-bg` through `color-mix`. The hero is `--mg-accent` and must be the brightest, most saturated thing on screen at every moment; a station that turns "on" fills with an accent tint (about 30% accent over the background), never the full accent, or the hero vanishes into it. The grid lives on a `::before` that overhangs the world on every side, so the stage edge never shows during a pull-out.

```css
.st{background:color-mix(in srgb,var(--mg-fg) 7%,var(--mg-bg))}
.world::before{content:"";position:absolute;inset:-150vmin;background:radial-gradient(circle,color-mix(in srgb,var(--mg-muted) 45%,transparent) 0.25vmin,transparent 0.32vmin) 0 0/5vmin 5vmin}
```

### Motion vocabulary

- **Hero position** lives on `.rider`, a transparent box the size of the world that is translated with `xPercent`/`yPercent`; the visible hero sits at its top-left corner with negative margins. So the hero, the stations and the camera share one coordinate system in vmin.
- **Hop**: x moves with `ease:"none"` (or `sine.inOut` for a long arc); y rises with `power2.out` to the apex and falls with `power2.in`. Separate tweens, same total duration. That is a parabola without physics.
- **Squash and stretch** on the visible hero only: stretch on take-off (`scaleY:1.15, scaleX:0.88`), squash on every landing (`scaleX:1.3, scaleY:0.72` for 0.07 s), then spring back with `back.out(3)`. Set `transform-origin:50% 100%` so it squashes onto the surface.
- **Roll**: along a track with `power1.inOut`; the station's fill tweens `scaleX` over the same span and its value counts with the counter helper. Give the hero a small mark on an inner `.spin` element and rotate that, not the hero: the hero's bottom transform-origin (for squash) would swing it off the track.
- **Station reaction**: a pop (`scale:0.6, autoAlpha:0` to full with `back.out(2)`) 0.2 s before the hero lands, and a push-down (`yPercent:12`, then back) on the contact.

```js
const P=(x,y)=>({xPercent:x/3,yPercent:y/2});
const land=t=>{tl.to(".hero",{scaleX:1.3,scaleY:0.72,duration:0.07,ease:"power1.out"},t)
  .to(".hero",{scaleX:1,scaleY:1,duration:0.3,ease:"back.out(3)"},t+0.07);};
const hop=(x,y,apex,t,d)=>{tl.to(".rider",{xPercent:x/3,duration:d,ease:"none"},t)
  .to(".rider",{yPercent:apex/2,duration:d*0.45,ease:"power2.out"},t)
  .to(".rider",{yPercent:y/2,duration:d*0.55,ease:"power2.in"},t+d*0.45);land(t+d);};
```

### Banned moves

Cuts, fades to black, a second hero, the hero vanishing or being replaced, stations that do not react, a centred title over a flat background, caption text that explains what is happening, floaty arcs (a hop longer than 0.9 s that is not the launch), camera moves with linear ease, layout in px.

## Must change between uses

Two one-take graphics in the same project must not look alike. Change all five of these from the previous one and say which you picked.

- **Layout of the course**: a zig-zag (right along the top, down a staircase, back left along the bottom); a spiral inward that ends at the centre; a single descending diagonal; a vertical tower climbed upward (good for 9:16); a loop that ends where it began.
- **Hero device**: a ball that starts as a toggle knob; a text caret that types, then hops; a document tile that gets stamped and filed; a chat bubble that rides in and gets replied to; a coin that drops through a payment flow; a map pin that hops between places.
- **Palette**: deep evergreen with marigold; ink blue `#121a33` with coral `#ff7a59`; aubergine `#231629` with mint `#5ef2b0`; light warm grey `#ecebe6` with a cobalt hero `#2448ff`. Always one saturated hero colour against a quiet stage.
- **Entry move**: the hero is already part of station 1 and acts at once; a cursor clicks station 1 and then gets out of the way; the hero drops in from above the frame and lands on station 1; the shot starts wide for 0.05 D and dives in.
- **End frame**: the pulled-out course held, with a slow push; a dive back into station 1, whose label now carries the product name; the camera settles on the final result station with the hero resting on it; the course rotates a few degrees and the hero rests, scaled up, in its starting slot.

## Fitting the renderer

The upstream style computes the hero's path and a spring-smoothed camera frame by frame, bakes the camera history at load, renders with real motion blur, tilts the reveal in 3D, loops seamlessly, and plays a synthesised soundtrack in which every contact is a note. Our substitutes:

- Physics and the baked camera become GSAP tweens: a hop is two y tweens with `power2.out` then `power2.in` under one linear x tween, and the camera is eased tweens on two wrapper elements that overlap the hero's moves. The lag of a spring camera is faked by starting the camera move slightly late and ending it slightly after the landing.
- No motion blur: stretch the hero along its path in flight instead.
- No 3D tilt (perspective transforms are unreliable through the renderer); a 2 to 4 degree 2D rotation on the pull-out stands in for it.
- No sound: the render has no audio track. Tell the user the contacts are where sound effects belong and lay audio in with `place_audio`.
- The graphic ends on a held end frame instead of looping, because a timeline clip plays once.

## Example

This is one instance of the world, built for an imaginary notes product. Do not reuse its course layout, its stations, its labels or its palette; design a new course from the user's own workflow and change everything listed under "Must change between uses".

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=1920, height=1080">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Sora:wght@700&family=IBM+Plex+Mono:wght@400;500&display=swap">
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:100%;height:100%;overflow:hidden}
#root{position:relative;width:100%;height:100%;overflow:hidden;background:var(--mg-bg);color:var(--mg-fg);font-family:var(--mg-font-body)}
.clip{position:absolute;inset:0}
.cam{position:absolute;left:50%;top:50%;width:0;height:0;transform-origin:0 0}
.world{position:absolute;left:0;top:0;width:300vmin;height:200vmin}
.world::before{content:"";position:absolute;inset:-150vmin;background:radial-gradient(circle,color-mix(in srgb,var(--mg-muted) 45%,transparent) 0.25vmin,transparent 0.32vmin) 0 0/5vmin 5vmin}
.st,.note,.toast{position:absolute;background:color-mix(in srgb,var(--mg-fg) 7%,var(--mg-bg));border:0.25vmin solid color-mix(in srgb,var(--mg-fg) 14%,transparent);border-radius:3vmin}
.lab{position:absolute;left:3vmin;top:2.6vmin;font-size:2.4vmin;letter-spacing:0.08em;text-transform:uppercase;color:var(--mg-muted);white-space:nowrap}
.lab b{color:var(--mg-fg);font-weight:500}
.abs{position:absolute}
.pill{left:33vmin;top:34.5vmin;width:18vmin;height:9vmin;border-radius:5vmin;background:color-mix(in srgb,var(--mg-fg) 16%,var(--mg-bg))}
.on{background:color-mix(in srgb,var(--mg-accent) 30%,var(--mg-bg))}
.slot{left:43.5vmin;top:36vmin;width:6vmin;height:6vmin;border-radius:50%;border:0.35vmin dashed var(--mg-bg)}
.track{left:88vmin;top:40.4vmin;width:54vmin;height:1.2vmin;border-radius:1vmin;background:color-mix(in srgb,var(--mg-fg) 16%,var(--mg-bg))}
.fill{background:var(--mg-accent);transform-origin:0 50%}
.val{right:4vmin;top:1.6vmin;font-family:var(--mg-font-display);font-weight:var(--mg-display-weight);font-size:6vmin;font-variant-numeric:tabular-nums}
.note{width:40vmin;height:12vmin;border-radius:2vmin;display:flex;align-items:center;gap:2vmin;padding:0 2.4vmin;font-size:2.1vmin;white-space:nowrap}
.note i{width:5vmin;height:5vmin;border-radius:1.2vmin;background:var(--mg-accent);flex:none}
.note b{display:block;font-weight:500}.note span{color:var(--mg-muted)}
.btn{left:197vmin;top:146vmin;width:34vmin;height:10vmin;border-radius:5vmin;background:var(--mg-fg);color:var(--mg-bg);display:grid;place-items:center;font-family:var(--mg-font-display);font-weight:var(--mg-display-weight);font-size:3.6vmin}
.toast{left:187vmin;top:160vmin;width:54vmin;height:8vmin;border-radius:4vmin;display:grid;place-items:center;font-size:2.4vmin;color:var(--mg-accent);white-space:nowrap}
.rider{position:absolute;inset:0}
.hero{position:absolute;left:0;top:0;width:6vmin;height:6vmin;margin:-3vmin 0 0 -3vmin;border-radius:50%;background:var(--mg-accent);transform-origin:50% 100%}
.spin{position:absolute;inset:0}
.spin::after{content:"";position:absolute;left:1.2vmin;top:1.2vmin;width:1.4vmin;height:1.4vmin;border-radius:50%;background:var(--mg-bg);opacity:0.5}
</style>
</head>
<body><div id="root" data-composition-id="mg" data-start="0" data-width="1920" data-height="1080" data-duration="12" data-world="one-take"
  style="--mg-bg:#10241f;--mg-fg:#eef3ea;--mg-accent:#ffc23d;--mg-muted:#86a398;--mg-font-display:'Sora',sans-serif;--mg-font-body:'IBM Plex Mono',monospace;--mg-display-weight:700">
<section class="clip" data-start="0" data-duration="12">
<div class="cam"><div class="world">
<div class="st" style="left:20vmin;top:22vmin;width:44vmin;height:28vmin"><p class="lab"><b>Draft</b> &#183; ready</p></div>
<div class="abs pill"></div><div class="abs pill on"></div><div class="abs slot"></div>
<div class="st" style="left:78vmin;top:22vmin;width:74vmin;height:28vmin"><p class="lab"><b>Checks</b> &#183; spelling, links</p><p class="abs val"><span class="n">0</span>%</p></div>
<div class="abs track"></div><div class="abs track fill"></div>
<div class="note n1" style="left:148vmin;top:58vmin"><i></i><p><b>Review &#183; 2 approvals</b><span>Mina signed off</span></p></div>
<div class="note n2" style="left:190vmin;top:78vmin"><i></i><p><b>Build &#183; 14 pages</b><span>Images compressed</span></p></div>
<div class="note n3" style="left:232vmin;top:98vmin"><i></i><p><b>Preview &#183; ready</b><span>Looks good on mobile</span></p></div>
<div class="st" style="left:179vmin;top:128vmin;width:70vmin;height:44vmin"><p class="lab"><b>Ship</b></p></div>
<div class="abs btn">Publish</div>
<div class="toast">Live at notes.page/launch</div>
<div class="rider"><div class="hero"><i class="spin"></i></div></div>
</div></div>
</section>
</div>
<script>(()=>{
const D=12;
const tl=gsap.timeline({paused:true,defaults:{ease:"power2.inOut",duration:0.6}});
const shot=(x,y,s,at,dur,ease="power2.inOut")=>{tl.to(".world",{xPercent:-x/3,yPercent:-y/2,duration:dur,ease},at);tl.to(".cam",{scale:s,duration:dur,ease},at);};
const land=t=>{tl.to(".hero",{scaleX:1.3,scaleY:0.72,duration:0.07,ease:"power1.out"},t).to(".hero",{scaleX:1,scaleY:1,duration:0.3,ease:"back.out(3)"},t+0.07);};
const lift=t=>tl.to(".hero",{scaleY:1.15,scaleX:0.88,duration:0.12,ease:"power1.out"},t).to(".hero",{scaleY:1,scaleX:1,duration:0.25},t+0.12);
const hop=(x,y,apex,t,d)=>{lift(t);tl.to(".rider",{xPercent:x/3,duration:d,ease:"none"},t).to(".rider",{yPercent:apex/2,duration:d*0.45,ease:"power2.out"},t).to(".rider",{yPercent:y/2,duration:d*0.55,ease:"power2.in"},t+d*0.45);land(t+d);};
const pop=(el,t)=>{tl.from(el,{scale:0.6,autoAlpha:0,duration:0.35,ease:"back.out(2)"},t);};
const push=(el,t)=>{tl.to(el,{yPercent:12,duration:0.08,ease:"power1.out"},t).to(el,{yPercent:0,duration:0.3,ease:"back.out(3)"},t+0.08);};
tl.set(".world",{xPercent:-42/3,yPercent:-36/2},0).set(".cam",{scale:1.5},0).set(".rider",{xPercent:37.5/3,yPercent:39/2},0);
tl.from(".pill.on",{autoAlpha:0,duration:0.4},0.35);
tl.to(".rider",{xPercent:46.5/3,duration:0.45,ease:"power2.inOut"},0.3);
shot(115,36,1.3,0.9,1.1);
hop(88,41,24,1,0.7);
tl.from(".fill",{scaleX:0,duration:1.6,ease:"power1.inOut"},1.8);
tl.to(".rider",{xPercent:142/3,duration:1.6,ease:"power1.inOut"},1.8);
tl.to(".spin",{rotation:540,duration:1.6,ease:"power1.inOut"},1.8);
const n=document.querySelector(".n"),o={v:0};tl.to(o,{v:100,duration:1.6,ease:"power1.inOut",onUpdate:()=>{n.textContent=Math.round(o.v);}},1.8);
shot(176,64,1.05,3.2,1.2);
pop(".n1",3.6);
tl.to(".rider",{xPercent:160/3,duration:0.55,ease:"none"},3.45).to(".rider",{yPercent:55/2,duration:0.55,ease:"power2.in"},3.45);land(4);push(".n1",4);
pop(".n2",4.35);hop(202,75,62,4,0.6);push(".n2",4.6);
shot(232,96,1.05,4.4,0.9);
pop(".n3",4.95);hop(244,95,82,4.6,0.6);push(".n3",5.2);
shot(214,150,1.35,5.2,1);
hop(214,143,86,5.2,0.8);push(".btn",6);
pop(".toast",6.15);
shot(146,89,0.36,6.9,1.5,"power3.out");
tl.to(".cam",{rotation:-3,duration:1.5,ease:"power2.out"},6.9).to(".cam",{rotation:0,duration:2,ease:"power2.inOut"},8.8);
lift(7);
tl.to(".rider",{xPercent:46.5/3,duration:2.6,ease:"sine.inOut"},7).to(".rider",{yPercent:6/2,duration:1.35,ease:"power3.out"},7).to(".rider",{yPercent:39/2,duration:1.25,ease:"power3.in"},8.35);
tl.to(".hero",{scale:2.2,duration:1,ease:"power2.out"},7.2).to(".hero",{scale:1.4,duration:0.6,ease:"power2.in"},9);
land(9.6);
tl.to(".cam",{scale:0.38,duration:2.2,ease:"sine.inOut"},9.8);
tl.set({},{},D);
window.__timelines=window.__timelines||{};
window.__timelines["mg"]=tl;
})();</script>
</body>
</html>
```
