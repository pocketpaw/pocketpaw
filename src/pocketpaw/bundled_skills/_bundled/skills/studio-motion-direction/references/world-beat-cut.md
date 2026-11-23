# Beat cut

## Contents

- [Identity](#identity)
- [Use it for, not for](#use-it-for-not-for)
- [Rules](#rules)
- [Must change between uses](#must-change-between-uses)
- [Fitting the renderer](#fitting-the-renderer)
- [Example](#example)

## Identity

A short, loud piece cut to a tempo grid. Something lands on every beat, yet it never reads as random cutting, because a shape carries the eye across each change: a spark stretches into a line, the line floods the frame into the next background, the full stop of a word detaches and swells into the scene after it. Over everything sits a thin viewfinder HUD (corner brackets, a scene counter, a category label, a running timecode), so the whole piece feels shot through one camera. Inside the kinetic type section the background swaps hard on the beat and the swaps themselves are the rhythm.

## Use it for, not for

Use it for: launch stings, teasers, hype intros, a value proposition in three words, the opening seconds of a showreel, social cutdowns that must grab in the first second.

Not for: anything the viewer must read slowly (a quote, a chart, a sourced fact), calm or premium brands, explaining a process step by step (use a one-take world), anything over about 12 seconds (the energy turns into noise).

## Rules

### Pacing and beat map

Pick a tempo between 120 and 140 BPM; 128 is the default. One beat is `B = 60 / BPM` seconds (0.46875 s at 128), and every position in the timeline is written as `b * B`. Big changes land on whole beats; type lands on half or quarter beats (`B/2`, `B/4`). Duration is a whole number of bars: 16 beats (7.5 s at 128) or 24 beats (11.25 s). For 16 beats:

| Beats | Fraction of D | Scene |
| --- | --- | --- |
| 0 to 3 | 0 to 0.19 | Spark: a dot appears on the dark frame, pulses on beat 1, stretches into a line with a small mono label on beat 1.5, then floods the frame vertically by beat 3. |
| 3 to 7 | 0.19 to 0.44 | Slam: the name on the flood colour, letters revealed on quarter beats, a mono kicker above. Its full stop pops on beat 4 and swells into the next background from beat 6. |
| 7 to 13 | 0.44 to 0.81 | Triplet: three short words, two beats each, each with its own background and treatment, swapped hard on the beat. The last word zooms through the camera in its final half beat. |
| 13 to 16 | 0.81 to 1 | End card: the name slams again, an accent dot pops, a mono line types in letter by letter, a hairline rule draws. Hold from beat 15. |

Six scenes at most in 16 beats. One idea per scene.

### Camera

There is no moving camera; the HUD is the camera. Corner brackets at about 4 vmin from each edge, a scene counter top right (`SC 03 / 06`), a category label bottom left (`KINETIC TYPE`), and a timecode bottom right with a small accent dot, all mono at about 1.8 to 2.2 vmin. The HUD is the one layer that never cuts; when the background flips between light and dark it flips its own colour with it, through a CSS variable.

```js
tl.set(".hud",{"--hud":"var(--mg-bg)"},3*B); // on a light scene
tl.set(".hud",{"--hud":"var(--mg-fg)"},9*B); // back on a dark one
```

### Type

A heavy display grotesk for the slams and triplet words (Archivo Black, Dela Gothic One, Bowlby One or Rubik Mono One), all caps or sentence case with a full stop, sized with `min(vw, vmin)` so the longest word fits a 9:16 frame. A mono for the HUD, kicker and typed line (JetBrains Mono, Space Mono or Azeret Mono). Triplet words are one to two words each; if a phrase does not fit at 22 vw, it is too long for this world.

### Colour

Four colours, all from the variables. `--mg-bg` is the ink scene colour, `--mg-fg` the light scene colour, `--mg-accent` the flood and highlight, and in this world `--mg-muted` is the second field colour (a saturated colour that cream type sits on), not a grey. At most three colours per scene. Every flat background gets a soft vignette and a fine grain layer.

```css
.vig{background:radial-gradient(ellipse at center,transparent 50%,color-mix(in srgb,var(--mg-bg) 40%,transparent))}
```

### Motion vocabulary

- **Grid positions**: `const B=60/128, at=b=>b*B;` and every tween's position is `at(n)`. Never type a raw second.
- **Hard cut**: stacked full-frame scenes, hidden in CSS (`visibility:hidden`), shown with `tl.set(scene,{autoAlpha:1},at(n))` and hidden the same way. A cut between whole scenes is allowed only on a downbeat, at most twice per piece; every other change is carried by a shape.
- **Flood**: a full-width bar in the accent colour grows `scaleX` from 0 into a line, then `scaleY` to about 400 with `power3.in`, finishing exactly on the next beat, when the next scene (same background colour) is shown underneath.
- **Swell**: a round element (the full stop) scales to about 140 with `power3.in` over one beat; the next scene uses its colour.
- **Slam**: letters in an `overflow:hidden` word span (not the whole line, or a swelling full stop gets clipped), `yPercent:110` to 0, `stagger:B/4`, `ease:"power4.out"`.
- **Kick**: a word enters in a quarter beat with overshoot, `ease:"back.out(2.4)"`, from a side or from scale 1.4.
- **Zoom through**: the last triplet word scales to about 14 in half a beat with `power3.in`, then the cut.
- **Type-on**: only for the end card's mono line, chars at `B/8` to `B/12`, finished at least a beat before D so the held end frame is complete.

```js
const B=60/128,at=b=>b*B;
tl.fromTo(".bar",{scaleX:0},{scaleX:1,duration:B/2,ease:"power3.out"},at(1.5));
tl.to(".bar",{scaleY:400,duration:B/2,ease:"power3.in"},at(2.5));
tl.set(".s1",{autoAlpha:1},at(3));
tl.from(split(q(".name"),"chars"),{yPercent:110,duration:B,ease:"power4.out",stagger:B/4},at(3.25));
```

### Banned moves

Fades between scenes, crossfades, any change off the grid, a scene with no motion, glows, more than three colours in a scene, a centred title on a flat background with no HUD, slow eases (`sine`) on type, text smaller than 1.8 vmin, emoji, more than two hard cuts between scenes.

## Must change between uses

Two beat-cut graphics in the same project must not look alike. Change all five and say which you picked.

- **Layout**: name slam centred with kicker above; name set huge and cropped off the left edge, reading as a texture; name stacked one letter per row down the side; split screen with the name on one half and the triplet on the other; the name on a diagonal band across the frame.
- **Hero device (the carried shape)**: a dot that floods vertically; a growing bar that wipes sideways; a ring that expands and leaves its colour inside; a word that zooms through the camera; a letter's counter (the hole in an O) that becomes a window into the next scene.
- **Palette**: ink indigo, cream, acid lime, electric violet; deep teal `#062a2c`, bone `#efe9dc`, hot orange `#ff6a1a`, cobalt `#2b4cff`; aubergine `#1e0f24`, pale pink `#f6dce4`, signal green `#1fe07a`, magenta `#d61f8c`. Never the house palettes.
- **Entry move**: a spark on dark; a full frame of the accent that a black bar splits open; the HUD alone for one beat, then the first word kicks in; the timecode counting fast, then the name slams.
- **End frame**: name with accent dot and typed URL; name only, HUD fades off on the final beat; the triplet words stacked as a summary; a ring pulse settling into the dot of the name.

## Fitting the renderer

Upstream builds this style with a per-frame canvas renderer, synthesised music with a kick on every beat and impacts on each flood, a canvas 3D signature scene (rippling cubes, particle rings), real motion blur, a third typeface (an italic serif for the human line) and a handwritten script word that draws itself. Our substitutes:

- The beat grid maps onto GSAP positions (`at(b)`), so everything still lands on the tempo even with no audio. Lay a track of the same BPM in with `place_audio` and say so to the user.
- No canvas, so the 3D signature scene is dropped; a kinetic triplet or a product micro-moment built from DOM takes its slot.
- Two font families only, so the italic serif line and the script word are gone; use the mono for the kicker and the human line.
- Grain is an inline SVG `feTurbulence` layer with a fixed seed, shifted on every beat with `tl.set` so it changes like film grain. It renders the same in Chromium and WebKit through the renderer. Keep its opacity at or under 0.18.
- No motion blur: overshoot and quarter-beat stagger carry the speed instead.

## Example

This is one instance of the world, for an imaginary product called Lantern. Do not reuse its words, its scene order, its carried shapes or its palette; build a new beat sheet from the user's own message and change everything listed under "Must change between uses".

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=1920, height=1080">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo+Black&family=JetBrains+Mono:wght@400;700&display=swap">
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:100%;height:100%;overflow:hidden}
#root{position:relative;width:100%;height:100%;overflow:hidden;background:var(--mg-bg);color:var(--mg-fg);font-family:var(--mg-font-body)}
.clip{position:absolute;inset:0}
.sc,.vig,.hud{position:absolute;inset:0;display:grid;place-items:center;align-content:center;text-align:center}
.sc{visibility:hidden}.s0{visibility:visible}
.ink{background:var(--mg-bg);color:var(--mg-fg)}
.lime{background:var(--mg-accent);color:var(--mg-bg)}
.cream{background:var(--mg-fg);color:var(--mg-bg)}
.vio{background:var(--mg-muted);color:var(--mg-fg)}
.s0{background:var(--mg-bg) radial-gradient(circle,color-mix(in srgb,var(--mg-fg) 18%,transparent) 0.15vmin,transparent 0.2vmin) 0 0/6vmin 6vmin}
.dot0{position:absolute;width:1.8vmin;height:1.8vmin;border-radius:50%;background:var(--mg-accent)}
.bar{position:absolute;left:0;width:100%;height:0.6vmin;background:var(--mg-accent)}
.tag{position:absolute;top:calc(50% - 6vmin);font-size:2vmin;letter-spacing:0.2em;text-transform:uppercase;color:var(--mg-fg)}
.disp{font-family:var(--mg-font-display);font-weight:var(--mg-display-weight);line-height:0.9;white-space:nowrap}
.name{font-size:min(15vw,24vmin)}
.mg-w{display:inline-block;overflow:hidden;padding:0.06em 0.02em}
.kick{font-size:2.4vmin;letter-spacing:0.3em;margin-bottom:3vmin;font-weight:700}
.stop{display:inline-block;width:0.2em;height:0.2em;border-radius:50%;background:var(--mg-fg);margin-left:0.06em}
.e .stop{background:var(--mg-accent)}
.word{font-size:min(22vw,30vmin)}
.s3 .word{color:var(--mg-accent);position:relative}
.pat{position:absolute;inset:-10% -40%;display:grid;align-content:space-around;font-family:var(--mg-font-display);font-size:16vmin;line-height:1;white-space:nowrap;color:transparent;-webkit-text-stroke:0.25vmin color-mix(in srgb,var(--mg-fg) 30%,transparent)}
.vf{position:absolute;inset:16% 12%}
.vf i{position:absolute;width:6vmin;height:6vmin;border:0.5vmin solid var(--mg-fg)}
.vf i:nth-child(1){left:0;top:0;border-width:0.5vmin 0 0 0.5vmin}.vf i:nth-child(2){right:0;top:0;border-width:0.5vmin 0.5vmin 0 0}
.vf i:nth-child(3){left:0;bottom:0;border-width:0 0 0.5vmin 0.5vmin}.vf i:nth-child(4){right:0;bottom:0;border-width:0 0.5vmin 0.5vmin 0}
.vf b{position:absolute;left:8vmin;top:1.6vmin;font-size:1.9vmin;letter-spacing:0.15em}
.url{font-size:3vmin;letter-spacing:0.35em;margin-top:4vmin;color:var(--mg-accent);font-weight:700}
.rule{width:min(60vw,70vmin);height:0.25vmin;margin-top:3vmin;background:var(--mg-fg);transform-origin:0 50%}
.grain{position:absolute;inset:-20%;width:140%;height:140%;opacity:0.16}
.vig{background:radial-gradient(ellipse at center,transparent 50%,color-mix(in srgb,var(--mg-bg) 40%,transparent))}
.hud{--hud:var(--mg-fg);color:var(--hud);font-size:2vmin;letter-spacing:0.14em;opacity:0.7;place-items:stretch}
.hud i{position:absolute;width:4vmin;height:4vmin;border:0 solid var(--hud)}
.hud i:nth-of-type(1){left:4vmin;top:4vmin;border-width:0.3vmin 0 0 0.3vmin}.hud i:nth-of-type(2){right:4vmin;top:4vmin;border-width:0.3vmin 0.3vmin 0 0}
.hud i:nth-of-type(3){left:4vmin;bottom:4vmin;border-width:0 0 0.3vmin 0.3vmin}.hud i:nth-of-type(4){right:4vmin;bottom:4vmin;border-width:0 0.3vmin 0.3vmin 0}
.hud p{position:absolute;white-space:nowrap}
.sn{right:9vmin;top:4.6vmin}.cat{left:9vmin;bottom:4.6vmin}.tc{right:9vmin;bottom:4.6vmin}
.tc::before{content:"";display:inline-block;width:1.2vmin;height:1.2vmin;border-radius:50%;background:var(--mg-accent);margin-right:1.2vmin}
.sn span,.cat span{position:absolute;right:0;visibility:hidden}.cat span{left:0;right:auto}
</style>
</head>
<body><div id="root" data-composition-id="mg" data-start="0" data-width="1920" data-height="1080" data-duration="7.5" data-world="beat-cut"
  style="--mg-bg:#15122b;--mg-fg:#f2efe6;--mg-accent:#c8ff3c;--mg-muted:#5a46ff;--mg-font-display:'Archivo Black',sans-serif;--mg-font-body:'JetBrains Mono',monospace;--mg-display-weight:400">
<section class="clip" data-start="0" data-duration="7.5">
<div class="sc s0"><p class="tag">lantern / launch &#183; 2026</p><div class="bar"></div><div class="dot0"></div></div>
<div class="sc s1 lime"><p class="kick">INTRODUCING / 2026</p><p class="disp name">LANTERN<span class="stop"></span></p></div>
<div class="sc s2 cream"><p class="disp word">Write.</p></div>
<div class="sc s3 ink"><div class="pat"><p>LINK LINK LINK LINK LINK</p><p>NK LINK LINK LINK LINK LI</p><p>LINK LINK LINK LINK LINK</p><p>NK LINK LINK LINK LINK LI</p><p>LINK LINK LINK LINK LINK</p></div><p class="disp word">Link.</p></div>
<div class="sc s4 vio"><div class="vf"><i></i><i></i><i></i><i></i><b>ISO 400 &#183; 1/50 &#183; F2.8</b></div><p class="disp word">Ship.</p></div>
<div class="sc s5 ink e"><p class="disp name n2">LANTERN<span class="stop"></span></p><p class="url">LANTERN.APP</p><div class="rule"></div></div>
<svg class="grain" xmlns="http://www.w3.org/2000/svg"><filter id="gr"><feTurbulence type="fractalNoise" baseFrequency="0.85" numOctaves="2" seed="7"/></filter><rect width="100%" height="100%" filter="url(#gr)"/></svg>
<div class="vig"></div>
<div class="hud"><i></i><i></i><i></i><i></i><p class="sn"></p><p class="cat"></p><p class="tc">00:00:00</p></div>
</section>
</div>
<script>(()=>{
const D=7.5,B=60/128,at=b=>b*B;
const tl=gsap.timeline({paused:true,defaults:{ease:"power3.out",duration:B/2}});
const q=s=>document.querySelector(s);
const split=(el,by)=>{const words=el.textContent.split(/\s+/).filter(Boolean);el.textContent="";const out=[];words.forEach((word,i)=>{if(i)el.append(" ");const w=document.createElement("span");w.className="mg-w";el.append(w);if(by==="words"){w.textContent=word;out.push(w);return;}for(const ch of word){const c=document.createElement("span");c.className="mg-c";c.style.display="inline-block";c.textContent=ch;w.append(c);out.push(c);}});return out;};
const chars=el=>{const s=el.querySelector(".stop");const cs=split(el,"chars");if(s)el.append(s);return cs;};
const cut=[0,3,7,9,11,13],cats=["SPARK","BRAND","KINETIC TYPE","KINETIC TYPE","KINETIC TYPE","END CARD"],dark=[1,0,0,1,1,1];
cut.forEach((b,i)=>{const sn=document.createElement("span"),ct=document.createElement("span");sn.textContent="SC 0"+(i+1)+" / 06";ct.textContent=cats[i];q(".sn").append(sn);q(".cat").append(ct);
tl.set([sn,ct],{autoAlpha:1},at(b));if(i){tl.set([sn.previousSibling,ct.previousSibling],{autoAlpha:0},at(b));tl.set(".s"+i,{autoAlpha:1},at(b));tl.set(".s"+(i-1),{autoAlpha:0},at(b));}
tl.set(".hud",{"--hud":dark[i]?"var(--mg-fg)":"var(--mg-bg)"},at(b));});
const tc=q(".tc"),o={t:0};tl.to(o,{t:D,duration:D,ease:"none",onUpdate:()=>{const f=Math.floor(o.t*30);tc.textContent="00:0"+Math.floor(f/30)+":"+String(f%30).padStart(2,"0");}},0);
let s=7;for(let b=0;b<16;b++){s=(s*16807)%2147483647;tl.set(".grain",{xPercent:(s%14)-7,yPercent:((s>>4)%14)-7},at(b));}
tl.from(".dot0",{scale:0,duration:B/2,ease:"back.out(3)"},0.05).to(".dot0",{scale:1.8,duration:B/4},at(1)).to(".dot0",{scale:1,duration:B/4,ease:"power2.in"},at(1.25)).set(".dot0",{autoAlpha:0},at(1.5));
tl.fromTo(".bar",{scaleX:0},{scaleX:1,duration:B/2},at(1.5)).from(".tag",{yPercent:100,autoAlpha:0,duration:B/2},at(1.75));
tl.to(".bar",{rotation:-1.5,duration:B/4,ease:"power1.out"},at(2)).to(".bar",{rotation:0,duration:B/2,ease:"elastic.out(1,0.3)"},at(2.25));
tl.to(".bar",{scaleY:400,duration:B/2,ease:"power3.in"},at(2.5));
tl.from(".kick",{yPercent:100,autoAlpha:0,duration:B/2},at(3));
tl.from(chars(q(".s1 .name")),{yPercent:110,duration:B,ease:"power4.out",stagger:B/4},at(3.25));
tl.from(".s1 .stop",{scale:0,duration:B/2,ease:"back.out(3)"},at(5)).to(".s1 .stop",{scale:140,duration:B,ease:"power3.in"},at(6));
tl.from(".s2 .word",{xPercent:-30,autoAlpha:0,duration:B/2,ease:"back.out(2.4)"},at(7)).to(".s2 .word",{xPercent:4,duration:B*1.5,ease:"none"},at(7.5));
tl.from(".s3 .word",{scale:1.4,autoAlpha:0,duration:B/2,ease:"back.out(2.4)"},at(9)).fromTo(".pat",{xPercent:-4},{xPercent:4,duration:B*2,ease:"none"},at(9));
tl.from(".s4 .word",{yPercent:60,autoAlpha:0,duration:B/2,ease:"back.out(2.4)"},at(11)).from(".vf",{scale:1.15,duration:B/2},at(11));
tl.to(".s4 .word",{scale:14,duration:B/2,ease:"power3.in"},at(12.5));
tl.from(chars(q(".n2")),{yPercent:110,duration:B,ease:"power4.out",stagger:B/8},at(13));
tl.from(".s5 .stop",{scale:0,duration:B/2,ease:"back.out(3)"},at(13.75));
tl.from(split(q(".url"),"chars"),{autoAlpha:0,duration:0.01,ease:"none",stagger:B/12},at(14));
tl.from(".rule",{scaleX:0,duration:B},at(14.25));
tl.set({},{},D);
window.__timelines=window.__timelines||{};
window.__timelines["mg"]=tl;
})();</script>
</body>
</html>
```
