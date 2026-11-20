## Contents

- [About these recipes](#about-these-recipes)
- [Shared setup](#shared-setup)
- Interfaces: [01 button to player](#01-button-to-player),
  [02 search to results](#02-search-to-results),
  [03 card to workspace](#03-card-to-workspace),
  [04 tabs to panels](#04-tabs-to-panels)
- Data: [05 chart morph](#05-chart-morph),
  [06 dashboard zoom](#06-dashboard-zoom),
  [07 spring stack](#07-spring-stack),
  [08 magnetic dock](#08-magnetic-dock)
- Type: [09 masked type](#09-masked-type),
  [10 elastic type](#10-elastic-type),
  [11 text to layout](#11-text-to-layout),
  [12 shutter reveal](#12-shutter-reveal)
- Systems: [13 layer spread](#13-layer-spread),
  [14 lens focus](#14-lens-focus),
  [15 flowing paths](#15-flowing-paths),
  [16 tile assemble](#16-tile-assemble)
- Controls: [17 morphing shape](#17-morphing-shape),
  [18 slider drag](#18-slider-drag),
  [19 toggle and toast](#19-toggle-and-toast),
  [20 terminal typing](#20-terminal-typing)
- Full compositions: [Example A, cursor opens a player](#example-a-cursor-opens-a-player),
  [Example B, search lands on one answer](#example-b-search-lands-on-one-answer),
  [Example C, flows fill a word](#example-c-flows-fill-a-word)

## About these recipes

Twenty recipes on one clock, then three full compositions. Times are written
against `mv`, `hold`, `span`, `back` and `beat` from the shared setup, so
every recipe scales with `D`: move times as fractions of `mv`, hold beats as
fractions of `span`, finish durations as fractions of `back`. Fixed seconds
are only for short things (a press, a content swap, one typed character). Snippets show only what is specific to the recipe;
the root, clip, fonts link and timeline registration follow `studio-motion`.

## Shared setup

Paste this block at the top of the script and drop the helpers you do not
use. Every recipe below assumes it.

```js
const D=8;
const mv=Math.min(3,D*0.275),back=Math.min(1.6,D*0.175),hold=D-back,span=hold-mv,beat=mv*0.06;
const tl=gsap.timeline({paused:true,defaults:{ease:"power3.out",duration:0.6}});
const q=s=>document.querySelector(s),qa=s=>[...document.querySelectorAll(s)];
const R=q("#root"),v=n=>getComputedStyle(R).getPropertyValue("--mg-"+n).trim();
const at=s=>{const e=q(s);return {left:e.offsetLeft,top:e.offsetTop};};
const box=s=>{const e=q(s).getBoundingClientRect(),r=R.getBoundingClientRect(),k=R.offsetWidth/r.width;return {x:(e.left-r.left)*k,y:(e.top-r.top)*k,w:e.width*k,h:e.height*k};};
const spring=(z=0.8)=>p=>{if(p>=1)return 1;const d=(7/z)*Math.sqrt(1-z*z);return 1-Math.exp(-7*p)*(Math.cos(d*p)+7/d*Math.sin(d*p));};
const swap=(out,inn,t)=>{if(out)tl.to(out,{autoAlpha:0,filter:"blur(8px)",duration:0.12,ease:"power2.in"},t);tl.fromTo(inn,{autoAlpha:0,filter:"blur(10px)",scale:0.94},{autoAlpha:1,filter:"blur(0px)",scale:1,duration:0.26,stagger:beat*0.5},t+0.07);};
```

- `v("accent")` reads a palette value, for colour tweens (GSAP cannot
  interpolate `var()`).
- `at(sel)` reads a mark; `box(sel)` reads any box sized in `vmin` or `%`.
  Call both before the first `from()`, which moves things on creation.
  Never measure a box whose size depends on text: the webfont may not have
  loaded when the script runs.
- The cursor helpers `go` and `click` are in SKILL.md.

```css
.mk{position:absolute;width:0;height:0}
.pin{position:absolute;width:0;height:0;display:flex;align-items:center;justify-content:center}
.pin>*{flex:none}
.ghost{position:absolute;border:0.3vmin dashed var(--mg-muted);opacity:0}
.fx-cur{position:absolute;width:3.2vmin;margin:-0.4vmin 0 0 -0.6vmin;transform-origin:20% 10%;z-index:9}
.ring{position:absolute;width:9vmin;height:9vmin;margin:-4.5vmin 0 0 -4.5vmin;border-radius:50%;border:0.4vmin solid var(--mg-accent);opacity:0}
```

```xml
<div class="fx-cur"><svg viewBox="0 0 24 24"><path d="M3 2l7.5 19 2.6-7.6L21 11z" style="fill:var(--mg-fg);stroke:var(--mg-bg)" stroke-width="1.5" stroke-linejoin="round"/></svg></div>
```

Draw rings and outlines with `border`, not a `box-shadow` spread: WebKit
offsets the shadow ring on anything that is also scaled.

## 01 button to player

**Shows** a pill button growing into a video player: thumbnail, title, a
time readout and a progress bar. **Use** for a "watch the demo" moment.

```xml
<i class="mk" id="mk-btn"></i><i class="mk" id="mk-player"></i>
<div class="pin" id="g-pin"><div class="ghost pill-size"></div></div>
<div class="pin" id="s-pin"><div class="shell">
  <span class="label">Watch the tour</span>
  <div class="player"><div class="thumb"></div><h3>…</h3><span class="time">0:04 / 0:40</span><div class="bar"><i></i></div></div>
</div></div>
<div class="pin" id="icon-pin"><svg viewBox="0 0 18 20"><polygon class="pa" points="0,0 9,4.5 9,15.5 0,20"/><polygon class="pb" points="9,4.5 18,10 18,10 9,15.5"/></svg></div>
```

```js
const P=at("#mk-player"),c=0.32*mv;
tl.to("#s-pin",{left:P.left,top:P.top,duration:mv-c,ease:spring(0.8)},c)
  .to(".shell",{width:"84vmin",height:"52vmin",borderRadius:"3vmin",backgroundColor:v("fg"),duration:mv-c,ease:spring(0.8)},c)
  .to(".label",{autoAlpha:0,filter:"blur(8px)",duration:0.12,ease:"power2.in"},c);
tl.to("#g-pin .ghost",{autoAlpha:0.4,duration:0.3},c+0.1);
tl.to("#icon-pin",{left:P.left,top:P.top,duration:mv-c,ease:spring(0.8)},c);
swap(null,qa(".player>*"),c+(mv-c)*0.55);
tl.to(".pa",{attr:{points:"0,0 6,0 6,20 0,20"},duration:0.3},mv+0.1*span)
  .to(".pb",{attr:{points:"12,0 18,0 18,20 12,20"},duration:0.3},mv+0.1*span);
tl.fromTo(".bar i",{scaleX:0.08},{scaleX:0.7,transformOrigin:"0 50%",duration:hold-mv,ease:"none"},mv);
```

- `.player` has its own fixed size (the final `84vmin` by `52vmin`) and
  `.shell` has `overflow:hidden`, so the contents never squash while the
  shell grows. The play icon travels in its own pin so it can sit on top.
- Play and pause are two four-point polygons each; tweening their `points`
  is a true morph, not a swap.
- The readout ("0:04 / 0:40") counts by tweening a plain object over the
  hold and writing `m:ss` in `onUpdate`, the one place `onUpdate` is allowed
  (Example A does it).

## 02 search to results

**Shows** a short search pill widening, a query typing itself, then three
results rising one after another, each with one real metric. **Use** to show
what people find in a help centre or library.

```xml
<div class="search"><svg class="mag">…</svg><span class="ph">Search</span><span class="q">spring campaign</span></div>
<div class="results"><div class="res"><i class="tile"></i><b>Spring launch email</b><em>48% opens</em></div>…</div>
```

```css
.q{display:inline-block;overflow:hidden;white-space:nowrap;vertical-align:bottom;width:0;font-family:var(--mg-font-body);border-right:0.12em solid var(--mg-accent)}
```

```js
const n=q(".q").textContent.length,typeAt=0.34*mv,typeLen=n*0.045;
tl.to(".search",{width:"88vmin",duration:0.3*mv,ease:spring(0.85)},0.02*mv)
  .to(".ph",{autoAlpha:0,duration:0.15},typeAt-0.1)
  .to(".q",{width:n+"ch",duration:typeLen,ease:`steps(${n})`},typeAt)
  .from(qa(".res"),{yPercent:35,autoAlpha:0,duration:0.5,stagger:beat*1.5},typeAt+typeLen+0.1);
tl.to(".q",{borderRightColor:"rgba(0,0,0,0)",duration:0.01,repeat:2*Math.floor(span/0.72)-1,yoyo:true,repeatDelay:0.35},mv);
```

- Typing is a width tween in `ch` with `steps(n)`, so set the query in a
  monospace body font. One character every 0.045 s.
- The pill opens before the typing starts so the two moves never fight.
- The caret blinks a finite, odd number of repeats sized to the hold, so it
  ends visible.
- Typing reads at about 0.045 s a character, so a typed query wants `D` of
  6 s or more.

## 03 card to workspace

**Shows** a small outlined card in a corner opening into a full editor
window: title bar, sidebar with one active item, document lines and status
chips. **Use** to announce a feature.

```xml
<div class="win"><div class="titlebar"><i></i><i></i><i></i><b class="title">Q3 brief</b></div>
  <div class="card-lines">…</div><div class="inside"><nav>…</nav><article>…</article><span class="chip">Draft</span></div></div>
<div class="ghost card-size"></div>
```

```css
.win{position:absolute;left:8%;bottom:10%;width:28vmin;height:18vmin;border-radius:2vmin;overflow:hidden}
.inside{position:absolute;left:0;bottom:0;width:150vmin;height:76vmin}
```

```js
tl.to(".win",{width:"84%",height:"76%",duration:0.64*mv,ease:spring(0.8)},0.05*mv)
  .to(".card-lines",{autoAlpha:0,duration:0.15},0.05*mv)
  .to(".ghost",{autoAlpha:0.4,duration:0.3},0.1*mv);
swap(null,qa(".inside>*"),0.4*mv);
```

- The window is anchored by `left` and `bottom`, so it grows out of its own
  corner. The title sits in the title bar from the start and rides the top
  edge up: a shared element with no duplicate.
- The ghost has the card's size and position and stays through the hold.
  It comes after the window in the markup, because the window grows over
  its own start corner.

## 04 tabs to panels

**Shows** a selection pill stretching from one tab to the next while the
panel rows swap, top row first. **Use** to compare plans or channels.

```css
.tabs{position:relative;display:grid;grid-template-columns:repeat(3,1fr)}
.sel{position:absolute;top:0;bottom:0;left:0;right:66.667%;border-radius:4vmin;background:var(--mg-accent)}
.rows{display:grid}.rows>div{grid-area:1/1}
```

```js
const t=0.1*mv,tabs=qa(".tab");
tl.to(".sel",{right:"33.333%",duration:0.4,ease:"expo.out"},t)
  .to(".sel",{left:"33.333%",duration:0.7,ease:"power2.out"},t+0.06)
  .to(tabs[1],{color:v("bg"),duration:0.2},t+0.15)
  .fromTo(tabs[0],{color:v("bg")},{color:v("fg"),duration:0.2},t+0.25)
  .to(qa(".ra>*"),{xPercent:-6,autoAlpha:0,duration:0.25,stagger:beat,ease:"power2.in"},0.36*mv)
  .from(qa(".rb>*"),{xPercent:6,autoAlpha:0,duration:0.35,stagger:beat},0.36*mv+0.2);
```

- The stretch is the point: the leading edge rides a fast ease, the
  trailing edge a slow one, so the pill gets long, then short as it lands.
- For the finish, reverse each line with `to()` at `hold` (leading edge
  first again, now `left`).

## 05 chart morph

**Shows** a bar chart whose bars slim to hairlines while a line draws
through their tops, with a dot on each point and a tag on the last.
**Use** to land a weekly result.

```xml
<svg class="chart" viewBox="0 0 600 300"><path class="area"/><g class="bars"></g>
  <path class="line" pathLength="1" stroke-dasharray="1 1" stroke-dashoffset="1"/><g class="dots"></g></svg>
```

```js
const vals=[42,55,48,70,64,92],X=i=>50+i*100,Y=v=>280-v*2.5,NS="http://www.w3.org/2000/svg";
const mk=(t,a,p)=>{const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);p.append(e);return e;};
vals.forEach((n,i)=>{mk("rect",{class:"bar",x:X(i)-30,y:Y(n),width:60,height:280-Y(n)},q(".bars"));mk("circle",{class:"dot",cx:X(i),cy:Y(n),r:9},q(".dots"));});
const d="M"+vals.map((n,i)=>X(i)+" "+Y(n)).join("L");
q(".line").setAttribute("d",d);q(".area").setAttribute("d",d+`L${X(5)} 280L${X(0)} 280Z`);
tl.to(qa(".bar"),{scaleX:0.05,transformOrigin:"50% 100%",duration:0.5,stagger:beat,ease:"power2.inOut"},0.05*mv)
  .to(".line",{attr:{"stroke-dashoffset":0},duration:0.5*mv,ease:"power2.inOut"},0.38*mv)
  .to(".area",{opacity:0.15,duration:0.5*mv},0.45*mv)
  .from(qa(".dot"),{scale:0,transformOrigin:"50% 50%",duration:0.3,ease:"back.out(1.6)",stagger:0.5*mv/6},0.4*mv);
```

- The same values draw both charts; the viewer can trust it. Label sample
  values "example".
- Building the SVG from an array at load time is setup, not drawing; every
  frame after that is tweens.
- Finish: retract the line (`stroke-dashoffset` back to 1), then fatten the
  bars. Add up the stagger: six bars at `beat*0.5` plus a 0.5 s tween must
  still end by `D`.

## 06 dashboard zoom

**Shows** accent brackets snapping round one KPI tile, the board dimming,
and the tile growing from its exact place into a large card with the number,
a delta chip and a sparkline. **Use** to pull out the one figure that
matters.

```js
const a=box(".tile.pick"),b=box(".big");
tl.from(qa(".br"),{scale:1.5,autoAlpha:0,duration:0.35,stagger:0.04,ease:"power3.out"},0.05*mv)
  .to(".board",{opacity:0.25,duration:mv*0.6},0.2*mv)
  .fromTo(".big",{x:a.x-b.x,y:a.y-b.y,scaleX:a.w/b.w,scaleY:a.h/b.h,transformOrigin:"0 0"},{x:0,y:0,scaleX:1,scaleY:1,duration:0.7*mv,ease:spring(0.85)},0.3*mv);
swap(null,qa(".big>*"),0.62*mv);
```

- `.big` is the final card, laid out in place; it starts transformed onto
  the tile, so it reads as the same object coming closer. Its contents wait
  for the second half.
- Four `.br` corner brackets are small bordered squares placed on the
  tile's corners.

## 07 spring stack

**Shows** three cards stacked at one angle fanning out on springs; the top
card swings past its resting angle and settles. **Use** to show three
offers or case studies.

```css
.card{position:absolute;width:20vmin;height:26vmin;transform-origin:50% 100%}
```

```js
const cards=qa(".card").reverse();
tl.set(".ghost",{rotation:14,xPercent:42});
[[14,42,0.55],[0,0,0.7],[-14,-42,0.9]].forEach(([r,x,z],i)=>tl.to(cards[i],{rotation:r,xPercent:x,duration:1.2,ease:spring(z)},0.08*mv+i*0.05));
tl.to(".ghost",{autoAlpha:0.5,duration:0.3},0.1*mv);
tl.to(cards,{rotation:0,xPercent:0,duration:back*0.8,ease:"power3.inOut"},hold);
```

- Each card gets its own damping: the top one least, so it overshoots most.
  The finish uses a plain ease with no spring.
- Put the ghost after the cards in the markup so it draws on top while the
  top card swings through it.

## 08 magnetic dock

**Shows** a row of app icons that swell as a cursor glides over them, sized
by a bell curve of distance, with a label over the biggest. **Use** to show
tools or integrations.

```css
.dock{position:absolute;left:0;right:0;bottom:12%;display:flex;justify-content:center;align-items:flex-end;gap:1.6vmin}
.ic{width:7vmin;height:7vmin;border-radius:1.6vmin}
```

```js
const icons=qa(".ic"),cur=q(".fx-cur"),n=icons.length,base=7,peak=12,gap=1.6,vm=Math.min(R.offsetWidth,R.offsetHeight)/100;
const sizes=c=>icons.map((_,j)=>base+(peak-base)*Math.exp(-((j-c)**2)/2.4));
const xOf=(c,s)=>{let x=-(s.reduce((a,b)=>a+b,0)+gap*(n-1))/2;for(let j=0;j<c;j++)x+=s[j]+gap;return R.offsetWidth/2+(x+s[c]/2)*vm;};
[[0.1*mv,3,0.8*mv],[mv+0.15*span,1,0.3*span],[mv+0.5*span,5,0.4*span]].forEach(([t,c,d])=>{const s=sizes(c);
  icons.forEach((e,j)=>tl.to(e,{width:s[j]+"vmin",height:s[j]+"vmin",duration:d,ease:"power2.inOut"},t));
  tl.to([cur,".dock-label"],{left:xOf(c,s),duration:d,ease:"power2.inOut"},t);});
```

- No per-frame maths: each cursor stop gets one set of tweens, every icon
  to its bell-curve size for that stop, on the same time and ease as the
  cursor. Width and height (not scale) make the flex row re-centre itself.
- The dock must be centred on the frame for `xOf` to hold. Swap the label
  text with `swap()` at each stop.
- Use the user's own logos as `<img src="https://…">`; never redraw a
  brand mark.

## 09 masked type

**Shows** a fill rising through the letters of a big outlined word, with an
accent line marking the edge, then a second line sliding up from behind a
clean cut. **Use** to reveal a name, an issue or a headline.

```xml
<div class="word"><span class="outline">CURRENT</span><span class="fill" aria-hidden="true">CURRENT</span><i class="edge"></i></div>
<div class="cut"><p class="line2">in motion</p></div>
```

```css
.word{position:relative;line-height:0.9}
.outline{color:transparent;-webkit-text-stroke:0.25vmin var(--mg-fg)}
.fill{position:absolute;left:0;top:0;color:var(--mg-accent);clip-path:inset(100% 0 0 0)}
.edge{position:absolute;left:-2%;right:-2%;bottom:0;height:0.35vmin;background:var(--mg-accent)}
.cut{overflow:hidden}
```

```js
tl.to(".fill",{clipPath:"inset(0% 0 0 0)",duration:0.6*mv,ease:"power2.inOut"},0.05*mv)
  .to(".edge",{bottom:"100%",duration:0.6*mv,ease:"power2.inOut"},0.05*mv)
  .to(".edge",{autoAlpha:0,duration:0.25},0.66*mv)
  .from(".line2",{yPercent:110,duration:0.5*mv},0.43*mv);
```

- The fill and the edge share one ease and duration, so the line stays on
  the fill edge. With a tight `line-height`, heavy faces hang past the box
  and the clip cuts them flat; widen the range to `inset(120% 0 -20% 0)`
  to `inset(-20% 0 -20% 0)` (Example C). For artwork in the letters, give `.fill` a gradient
  background with `background-clip:text` (both engines draw it).

## 10 elastic type

**Shows** each letter of a word stretching taller on one locked baseline,
spreading from the middle letter out, while the word narrows. **Use** to
open an event, a webinar or an episode.

```css
.word span{display:inline-block;transform-origin:50% 100%}
.word{font-variation-settings:'wdth' 100}
```

```js
const cs=qa(".word span"),m=(cs.length-1)/2;
cs.forEach((c,i)=>{const d=Math.abs(i-m);tl.to(c,{scaleY:1.9-0.22*d,duration:mv-d*beat*2,ease:spring(0.85)},d*beat*2);});
tl.to(".word",{fontVariationSettings:"'wdth' 60",duration:mv,ease:"power2.inOut"},0);
```

- Split the word into one span per letter (the `split` helper from
  `studio-motion` in chars mode). The baseline never moves; the middle
  letter alone takes the accent colour.
- The width axis needs a font that has one, loaded with its range (for
  example `family=Anybody:wdth,wght@50..150,800`). Without one, keep only
  the `scaleY` stretch.
- A small readout ("wdth 100" to "wdth 60") explains the move: tween a
  plain object from 100 to 60 and write the rounded value in `onUpdate`.

## 11 text to layout

**Shows** one plain line of headline text in a dashed strip splitting in two
and travelling into an article page, growing as it goes, then kicker, image,
body lines and a pull quote arriving in reading order. **Use** to turn a
headline into a cover or teaser.

```xml
<div class="strip"><span class="slot s1"></span><span class="slot s2"></span></div>
<h1 class="h h1">Design that</h1><h1 class="h h2">moves people</h1>
```

```js
const fly=(from,to,t)=>{const a=box(from),b=box(to);tl.fromTo(to,{x:a.x-b.x,y:a.y-b.y,scale:a.h/b.h,transformOrigin:"0 0"},{x:0,y:0,scale:1,duration:0.6*mv,ease:spring(0.85)},t);};
fly(".s2",".h2",0.05*mv);fly(".s1",".h1",0.12*mv);
tl.to(".strip",{opacity:0.35,duration:0.3},0.3*mv);
tl.from(".kicker",{autoAlpha:0,duration:0.3},0.45*mv)
  .from(".pic",{clipPath:"inset(0 100% 0 0)",duration:0.5,ease:"power3.inOut"},0.48*mv)
  .from(qa(".body i"),{scaleX:0,transformOrigin:"0 50%",stagger:beat},0.5*mv)
  .from(".quote",{yPercent:30,autoAlpha:0},0.55*mv);
```

- The strip holds two fixed-width slots sized in `vmin`, and each headline
  half is its own absolutely placed block. Both boxes are known without
  measuring text, so the move lands exactly. Scale is the slot height over
  the line height.
- The second half moves first, so the halves never share a line on the way.

## 12 shutter reveal

**Shows** vertical slats covering an image, each narrowing to its own
centre line in turn, then a small label pill. **Use** to tease a drop or
a photo.

```css
.slats{position:absolute;inset:0;display:grid;grid-template-columns:repeat(7,1fr)}
.slat{background:var(--mg-fg);border-left:0.2vmin solid var(--mg-muted)}
```

```js
tl.to(qa(".slat"),{scaleX:0,duration:0.45*mv,ease:"power3.inOut",stagger:beat},0.05*mv)
  .from(".tag",{yPercent:40,autoAlpha:0,duration:0.4},0.48*mv);
```

- The faint edge on each slat makes it read as a physical object. The
  image is an `<img src="https://…">` or a scene drawn in CSS.

## 13 layer spread

**Shows** three flat layers of a page (data, logic, layout) sliding apart
on a diagonal, back layer first, each keeping its content, then breathing
gently in the hold. **Use** to explain the layers of a service or stack.
This replaces a 3D perspective move, which WebKit draws flat.

```js
const ly=qa(".ly");
ly.forEach((e,i)=>tl.to(e,{xPercent:(i-1)*55,yPercent:(1-i)*45,skewY:-10,scaleX:0.85,duration:0.8*mv,ease:spring(0.85)},0.05*mv+(2-i)*beat));
tl.to(ly,{yPercent:"-=6",duration:span*0.4,ease:"sine.inOut",yoyo:true,repeat:1},mv+span*0.1);
tl.to(ly,{xPercent:0,yPercent:0,skewY:0,scaleX:1,duration:back*0.7,ease:"power3.inOut",stagger:back*0.1},hold);
```

- The shared `skewY` and `scaleX` make every layer lean the same way, which
  stands in for one vanishing point. A dashed SVG guide through the layer
  corners helps the eye.

## 14 lens focus

**Shows** a report card with every row softly blurred, and a lens that
steps down the rows; only the row inside the lens is sharp and slightly
larger. **Use** to make one line of a report or price table get read.

```xml
<div class="report"><div class="soft"><div class="r">…</div>×5</div>
  <div class="lens"><div class="sharp"><div class="r">…</div>×5</div></div></div>
```

```css
.r{height:8vmin}
.soft .r{filter:blur(0.35vmin);opacity:0.65}
.lens{position:absolute;left:-2%;right:-2%;top:0;height:8vmin;overflow:hidden;border:0.3vmin solid var(--mg-accent);border-radius:2vmin;background:var(--mg-bg)}
.sharp{position:absolute;left:2.63%;width:94.74%;top:0}
```

```js
const d=Math.min(0.9,back*0.8);
[[0.25*mv,1],[mv+0.1*span,3],[mv+0.5*span,2],[hold,0]].forEach(([t,k])=>{
  tl.to(".lens",{top:k*8+"vmin",duration:d,ease:"power3.inOut"},t).to(".sharp",{top:-k*8+"vmin",duration:d,ease:"power3.inOut"},t);});
tl.to(".lens",{scale:1.06,duration:0.4},0.6*mv).to(".lens",{scale:1,duration:back*0.5},hold);
```

- The lens holds a sharp duplicate of the rows that moves the opposite
  way, so it lines up with the blurred rows beneath. `backdrop-filter` is
  not used because WebKit ignores it.

## 15 flowing paths

**Shows** pulses of light leaving one source, running along curves to
three channels, and on into one result; each node answers with a ring when
a pulse arrives. **Use** to show one piece of work feeding several outputs.

```xml
<svg class="wires"></svg>
<i class="mk" id="m-src"></i><i class="mk" id="m-a"></i>…   <!-- one mark per node, nodes sit on them -->
```

```js
const W=R.offsetWidth,H=R.offsetHeight,svg=q(".wires");svg.setAttribute("viewBox",`0 0 ${W} ${H}`);
const curve=(a,b)=>{const m=(a.left+b.left)/2;return `M${a.left} ${a.top}C${m} ${a.top} ${m} ${b.top} ${b.left} ${b.top}`;};
const link=(s,e,t)=>{const d=curve(at(s),at(e));
  svg.insertAdjacentHTML("beforeend",`<path class="wire" d="${d}" pathLength="1" stroke-dasharray="1 1" stroke-dashoffset="1"/><path class="pulse" d="${d}" pathLength="1" stroke-dasharray="0.06 2" stroke-dashoffset="0.1"/>`);
  const [w,p]=[...svg.querySelectorAll("path")].slice(-2);
  tl.to(w,{attr:{"stroke-dashoffset":0},duration:0.9,ease:"power2.inOut"},t).to(p,{attr:{"stroke-dashoffset":-1.04},duration:0.9,ease:"power1.inOut"},t);};
link("#m-src","#m-a",0.1*mv);link("#m-src","#m-b",0.1*mv+0.14);link("#m-src","#m-c",0.1*mv+0.28);
```

- The SVG gets a viewBox equal to the frame in pixels, and the curves are
  written from the marks at load. Each link is two paths: a faint wire that
  draws on, and a short dash that travels along it (`stroke-dasharray`
  of `0.06 2`, `stroke-dashoffset` from 0.1 to -1.04, `pathLength="1"`).
  Those offsets keep the dash fully off both ends at rest; a dash parked
  exactly on an end leaves a round-cap dot behind.
- The pulse draws the line; it never just switches on. A second wave in the
  hold, on new pulse paths, keeps it alive.

## 16 tile assemble

**Shows** a few dozen small tiles flying in from a seeded scatter, left
ones first, and locking onto the grid of a simple mark, with a label that
turns from "assembling" to "assembled". **Use** to close on a logo. This
replaces a particle swarm; there are no trails and no glow.

```js
const MAP=["0011100","0111110","1100011","1100011","1100011","0111110","0011100"];
const rnd=(s=>()=>(s=(s*1664525+1013904223)%4294967296)/4294967296)(7);
MAP.forEach((r,y)=>[...r].forEach((b,x)=>{if(b==="1"){const t=document.createElement("i");t.className="tile";
  t.style.left=(x-3.5)*4.6+"vmin";t.style.top=(y-3.5)*4.6+"vmin";t.dataset.x=x;q(".mark").append(t);}}));
const tiles=qa(".tile").sort((a,b)=>a.dataset.x-b.dataset.x);
tl.from(tiles,{xPercent:()=>-900+rnd()*500,yPercent:()=>(rnd()-0.5)*900,rotation:()=>(rnd()-0.5)*180,autoAlpha:0,duration:0.9,ease:"power3.out",stagger:(0.5*mv)/tiles.length},0.05*mv);
```

- The map is the mark's real shape on a grid, so the finished logo is
  crisp. The seeded `rnd` makes every render identical.

## 17 morphing shape

**Shows** one shape that never cuts: a pill becomes a card, the card becomes
a control, its contents blur-swapping at each change, each change triggered
by a click. **Use** to carry a short story in a single object.

```xml
<div class="pin" id="s"><div class="shape"><div class="L L1">…</div><div class="L L2">…</div><div class="L L3">…</div></div></div>
```

```css
.shape{width:34vmin;height:9vmin;border-radius:4.5vmin;overflow:hidden;position:relative}
.L{position:absolute;inset:0;display:grid;place-items:center}.L2,.L3{opacity:0;visibility:hidden}
```

```js
const state=(t,w,h,r,bg)=>tl.to(".shape",{width:w,height:h,borderRadius:r,backgroundColor:bg,duration:0.9,ease:spring(0.8)},t);
go(0.02*mv,"#mk-shape",0.5);click(0.3*mv,".shape",".ring");state(0.3*mv,"70vmin","44vmin","3vmin",v("fg"));swap(".L1",".L2",0.3*mv+0.35);
go(mv+0.1*span,"#mk-shape2",0.6);click(mv+0.3*span,".shape",".ring2");state(mv+0.3*span,"50vmin","14vmin","7vmin",v("accent"));swap(".L2",".L3",mv+0.3*span+0.35);
```

- Pick each state's size so it fills the frame: pills small and high
  contrast, cards large. Space changes 0.4 to 1.2 s apart.
- A tween's ease must not overshoot past zero size: keep `spring(0.8)` or
  softer on shells.

## 18 slider drag

**Shows** the cursor grabbing a knob and dragging it along a track; a result
builds piece by piece as the value passes each threshold. **Use** to show
effort, level, quality or scale.

```js
const t1=0.25*mv,t2=mv+0.15*span,ez="sine.inOut",E=gsap.parseEase(ez);
const tAt=p=>{let u=0;while(u<1&&E(u)<p)u+=0.005;return t1+u*(t2-t1);};
go(0.05*mv,"#mk-knob",0.6);
tl.to(cur,{scale:0.86,duration:0.07},t1-0.07).to(cur,{scale:1,duration:0.3,ease:spring(0.72)},t2);
tl.to([cur,".knob"],{left:at("#mk-end").left,duration:t2-t1,ease:ez},t1).to(".fill",{width:"100%",duration:t2-t1,ease:ez},t1);
qa(".piece").forEach((e,i,a)=>tl.from(e,{scaleY:0,transformOrigin:"50% 100%",duration:0.4,ease:spring(0.62)},tAt((i+0.5)/a.length)));
```

- `tAt` finds when the eased drag crosses a threshold, so each piece lands
  exactly as the knob passes it. The squash holds for the whole drag.

## 19 toggle and toast

**Shows** the cursor flipping a switch, the track filling with the accent,
and a toast sliding in with a check that draws itself; the thing the switch
controls changes behind it. **Use** to show a setting taking effect.

```js
const t=0.3*mv;go(0.02*mv,"#mk-switch",0.6);click(t,".switch",".ring");
tl.to(".thumb",{left:"55%",duration:0.45,ease:spring(0.75)},t+0.03)
  .to(".switch",{backgroundColor:v("accent"),duration:0.25},t+0.03)
  .fromTo(".toast",{yPercent:140,autoAlpha:0},{yPercent:0,autoAlpha:1,duration:0.6,ease:spring(0.8)},t+0.3)
  .to(".tick",{attr:{"stroke-dashoffset":0},duration:0.35,ease:"power2.out"},t+0.6);
```

- The check is an SVG path with `pathLength="1"`, `stroke-dasharray="1 1"`
  and `stroke-dashoffset="1"`. The toast leaves before the finish; the
  switch stays on if the ending is resolve.

## 20 terminal typing

**Shows** a command typing at a prompt, output lines streaming in one per
beat, and a status pill turning from "running" to "done". **Use** to show a
CLI or an agent run.

```css
.cmd{display:inline-block;overflow:hidden;white-space:nowrap;vertical-align:bottom;width:0;border-right:0.25em solid var(--mg-accent)}
.out p{opacity:0}
```

```js
const n=q(".cmd").textContent.length,tEnd=0.1*mv+n*0.045;
tl.to(".cmd",{width:n+"ch",duration:n*0.045,ease:`steps(${n})`},0.1*mv)
  .to(qa(".out p"),{opacity:1,duration:0.01,stagger:beat*2},tEnd+0.2);
swap(".st-run",".st-done",tEnd+0.2+beat*2*qa(".out p").length);
```

- Output lines appear whole, never typed. Use real commands and real
  output, or label them as examples.

## Full compositions

Three working 8-second graphics. Each is one instance, shown so you can see
the recipes fitted together under the real contract: marks, pins, the clock,
a live hold and a deliberate finish. Do not reuse their layout, palette or
copy; change the hero device, the palette, the type pairing and the ending
for every new graphic.

### Example A, cursor opens a player

Recipes 01 and 17 with the cursor rules: the cursor clicks a pill, the pill
travels and grows into a player while the play icon rides to the thumbnail
and morphs to pause. A ghost of the pill stays behind. The hold keeps the
progress bar and the timer running and swaps the chapter chip. Ending:
return. Palette: putty, ink and cobalt. Type: a text serif with real italics
for the display and a geometric sans for the interface. Portrait frames move the marks in one
media query.

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=1920, height=1080">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Newsreader:ital,wght@0,500;1,500&family=Manrope:wght@500;700&display=swap">
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  html,body{width:100%;height:100%;overflow:hidden}
  #root{position:relative;width:100%;height:100%;overflow:hidden;background:var(--mg-bg);color:var(--mg-fg);font-family:var(--mg-font-body)}
  .clip{position:absolute;inset:0}
  .mk{position:absolute;width:0;height:0}
  .pin{position:absolute;width:0;height:0;display:flex;align-items:center;justify-content:center}
  .pin>*{flex:none}
  .grid{position:absolute;inset:0;background:linear-gradient(var(--mg-muted) 0.12vmin,transparent 0) 0 0/100% 12vmin;opacity:0.18}
  .cap{position:absolute;left:7%;top:9%;max-width:42%}
  .kick{font-size:clamp(1.6vmin,1.4vw,2.2vmin);font-weight:700;letter-spacing:0.18em;text-transform:uppercase;color:var(--mg-accent)}
  .head{font-family:var(--mg-font-display);font-weight:var(--mg-display-weight);font-size:clamp(6vmin,6vw,9vmin);line-height:1;margin-top:2vmin}
  .head em{color:var(--mg-muted)}
  #mk-btn,.ring,#g-pin,#s-pin{left:24%;top:76%}
  #mk-ic0,#i-pin{left:calc(24% - 10.5vmin);top:76%}
  #mk-player{left:63%;top:54%}
  #mk-ic1{left:63%;top:calc(54% - 6vmin)}
  #mk-c0,.fx-cur{left:90%;top:90%}
  #mk-rest{left:86%;top:84%}
  .ghost{width:30vmin;height:9vmin;border-radius:4.5vmin;border:0.3vmin dashed var(--mg-muted);opacity:0}
  .shell{position:relative;width:30vmin;height:9vmin;border-radius:4.5vmin;background:var(--mg-accent);overflow:hidden}
  .label{position:absolute;inset:0;display:flex;align-items:center;padding-left:8.5vmin;font-size:2.8vmin;font-weight:700;color:var(--mg-bg);white-space:nowrap}
  .player{position:absolute;left:50%;top:50%;width:70vmin;height:46vmin;margin:-23vmin 0 0 -35vmin}
  .thumb{position:absolute;left:2.5vmin;right:2.5vmin;top:2.5vmin;height:29vmin;border-radius:1.6vmin;background:var(--mg-accent);overflow:hidden}
  .sun{position:absolute;right:14%;top:16%;width:9vmin;height:9vmin;border-radius:50%;background:var(--mg-bg)}
  .hill{position:absolute;left:-10%;width:70%;height:30vmin;top:60%;border-radius:50%;background:var(--mg-fg);opacity:0.55}
  .hill.b{left:40%;width:80%;top:72%;opacity:0.8}
  .title{position:absolute;left:2.5vmin;top:33vmin;font-family:var(--mg-font-display);font-size:4vmin;color:var(--mg-bg);white-space:nowrap}
  .chap{position:absolute;right:2.5vmin;top:33.6vmin;display:grid;font-size:1.9vmin;font-weight:700;color:var(--mg-bg)}
  .chap span{grid-area:1/1;justify-self:end;padding:0.6vmin 1.4vmin;border-radius:2vmin;border:0.25vmin solid var(--mg-muted)}
  .chap .c2{opacity:0;visibility:hidden}
  .time{position:absolute;left:2.5vmin;bottom:2.4vmin;font-size:2vmin;color:var(--mg-muted);font-variant-numeric:tabular-nums}
  .bar{position:absolute;left:14vmin;right:2.5vmin;bottom:3.1vmin;height:0.8vmin;border-radius:0.4vmin;background:var(--mg-muted)}
  .bar i{position:absolute;inset:0;border-radius:0.4vmin;background:var(--mg-bg);transform-origin:0 50%}
  .disc{position:absolute;width:12vmin;height:12vmin;border-radius:50%;background:var(--mg-fg)}
  .ico{position:relative;width:3.4vmin;height:3.8vmin}
  .ico polygon{fill:var(--mg-bg);stroke:var(--mg-bg);stroke-width:0.5;stroke-linejoin:round}
  .ring{position:absolute;width:12vmin;height:12vmin;margin:-6vmin 0 0 -6vmin;border-radius:50%;border:0.35vmin solid var(--mg-fg);opacity:0}
  .fx-cur{position:absolute;width:3.4vmin;margin:-0.4vmin 0 0 -0.6vmin;transform-origin:20% 10%;z-index:9}
  @media (max-aspect-ratio:1/1){
    .cap{max-width:86%}
    #mk-btn,.ring,#g-pin,#s-pin{left:34%;top:84%}
    #mk-ic0,#i-pin{left:calc(34% - 10.5vmin);top:84%}
    #mk-player{left:50%;top:55%}
    #mk-ic1{left:50%;top:calc(55% - 6vmin)}
  }
</style>
</head>
<body><div id="root" data-composition-id="mg" data-start="0" data-width="1920" data-height="1080" data-duration="8" data-world="effects"
  style="--mg-bg:#dcd5c9;--mg-fg:#1a1816;--mg-accent:#2f4fe0;--mg-muted:#857d72;--mg-font-display:'Newsreader',serif;--mg-font-body:'Manrope',sans-serif;--mg-display-weight:500">
<section class="clip" data-start="0" data-duration="8">
<div class="grid"></div>
<div class="cap"><p class="kick">Product tour</p><h1 class="head">The new editor, <em>in forty seconds.</em></h1></div>
<i class="mk" id="mk-btn"></i><i class="mk" id="mk-ic0"></i><i class="mk" id="mk-player"></i><i class="mk" id="mk-ic1"></i><i class="mk" id="mk-c0"></i><i class="mk" id="mk-rest"></i>
<div class="pin" id="g-pin"><div class="ghost"></div></div>
<div class="pin" id="s-pin"><div class="shell">
<span class="label">Watch the tour</span>
<div class="player"><div class="thumb"><i class="sun"></i><i class="hill"></i><i class="hill b"></i></div>
<p class="title">Timeline, graphics, export</p>
<p class="chap"><span class="c1">01 Timeline</span><span class="c2">02 Graphics</span></p>
<p class="time">0:04 / 0:40</p><div class="bar"><i></i></div></div>
</div></div>
<div class="pin" id="i-pin"><i class="disc"></i><svg class="ico" viewBox="0 0 18 20"><polygon class="pa" points="0,0 9,4.5 9,15.5 0,20"/><polygon class="pb" points="9,4.5 18,10 18,10 9,15.5"/></svg></div>
<i class="ring"></i>
<div class="fx-cur"><svg viewBox="0 0 24 24"><path d="M3 2l7.5 19 2.6-7.6L21 11z" style="fill:var(--mg-fg);stroke:var(--mg-bg)" stroke-width="1.5" stroke-linejoin="round"/></svg></div>
</section>
</div>
<script>(()=>{
const D=8;
const mv=Math.min(3,D*0.275),back=Math.min(1.6,D*0.175),hold=D-back,beat=mv*0.06;
const tl=gsap.timeline({paused:true,defaults:{ease:"power3.out",duration:0.6}});
const q=s=>document.querySelector(s),qa=s=>[...document.querySelectorAll(s)];
const R=q("#root"),v=n=>getComputedStyle(R).getPropertyValue("--mg-"+n).trim();
const at=s=>{const e=q(s);return {left:e.offsetLeft,top:e.offsetTop};};
const spring=(z=0.8)=>p=>{if(p>=1)return 1;const d=(7/z)*Math.sqrt(1-z*z);return 1-Math.exp(-7*p)*(Math.cos(d*p)+7/d*Math.sin(d*p));};
const swap=(out,inn,t)=>{if(out)tl.to(out,{autoAlpha:0,filter:"blur(8px)",duration:0.12,ease:"power2.in"},t);tl.fromTo(inn,{autoAlpha:0,filter:"blur(10px)",scale:0.94},{autoAlpha:1,filter:"blur(0px)",scale:1,duration:0.26,stagger:beat*0.5},t+0.07);};
const cur=q(".fx-cur");
const go=(t,s,d=0.7)=>{const p=at(s);tl.to(cur,{left:p.left,duration:d,ease:"power2.inOut"},t).to(cur,{top:p.top,duration:d,ease:"power3.inOut"},t);};
const click=(t,target,ring)=>{const hit=[cur,q(target)];tl.to(hit,{scale:(i)=>i?0.965:0.86,duration:0.07,ease:"power2.in"},t-0.07).to(hit,{scale:1,duration:0.35,ease:spring(0.72)},t+0.035);if(ring)tl.fromTo(ring,{scale:0,autoAlpha:0.5},{scale:1,autoAlpha:0,duration:0.45,ease:"power2.out",immediateRender:false},t);};
const P=at("#mk-player"),B=at("#mk-btn"),I0=at("#mk-ic0"),I1=at("#mk-ic1"),c=0.34*mv,m=mv-c,span=hold-mv,b=back;
tl.from([".kick",".head"],{yPercent:40,autoAlpha:0,duration:0.7,stagger:beat},0);
go(0.03*mv,"#mk-btn",c*0.85);click(c,".shell",".ring");
tl.to("#s-pin",{left:P.left,top:P.top,duration:m,ease:spring(0.8)},c+0.04)
  .to(".shell",{width:"70vmin",height:"46vmin",borderRadius:"3vmin",backgroundColor:v("fg"),duration:m,ease:spring(0.8)},c+0.04)
  .to(".label",{autoAlpha:0,filter:"blur(8px)",duration:0.12,ease:"power2.in"},c+0.04)
  .to(".ghost",{autoAlpha:0.5,duration:0.3},c+0.2)
  .to("#i-pin",{left:I1.left,top:I1.top,duration:m,ease:spring(0.8)},c+0.04)
  .to(".ico",{scale:1.5,duration:m,ease:spring(0.8)},c+0.04)
  .fromTo(".disc",{scale:0},{scale:1,duration:0.5,ease:spring(0.75)},c+m*0.55);
go(c+0.25,"#mk-rest",0.9);
swap(null,qa(".player>*"),c+m*0.5);
tl.to(".pa",{attr:{points:"0,0 6,0 6,20 0,20"},duration:0.3,ease:"power2.inOut"},mv+0.1*span)
  .to(".pb",{attr:{points:"12,0 18,0 18,20 12,20"},duration:0.3,ease:"power2.inOut"},mv+0.1*span)
  .fromTo(".bar i",{scaleX:0.1},{scaleX:0.65,duration:hold-mv,ease:"none"},mv);
const tm=q(".time"),o={s:4};
tl.to(o,{s:26,duration:hold-mv,ease:"none",onUpdate:()=>{tm.textContent="0:"+String(Math.floor(o.s)).padStart(2,"0")+" / 0:40";}},mv);
swap(".c1",".c2",(mv+hold)/2);
tl.to([".pa",".pb"],{attr:{points:(i)=>i?"9,4.5 18,10 18,10 9,15.5":"0,0 9,4.5 9,15.5 0,20"},duration:0.18*b,ease:"power2.inOut"},hold);
tl.to(qa(".player>*"),{autoAlpha:0,filter:"blur(8px)",duration:0.1*b,ease:"power2.in"},hold+0.07*b)
  .to(".disc",{scale:0,duration:0.2*b,ease:"power2.in"},hold+0.1*b)
  .to("#s-pin",{left:B.left,top:B.top,duration:0.7*b,ease:"power3.inOut"},hold+0.18*b)
  .to(".shell",{width:"30vmin",height:"9vmin",borderRadius:"4.5vmin",backgroundColor:v("accent"),duration:0.7*b,ease:"power3.inOut"},hold+0.18*b)
  .to("#i-pin",{left:I0.left,top:I0.top,duration:0.7*b,ease:"power3.inOut"},hold+0.18*b)
  .to(".ico",{scale:1,duration:0.7*b,ease:"power3.inOut"},hold+0.18*b)
  .to(".label",{autoAlpha:1,filter:"blur(0px)",duration:0.15*b},hold+0.78*b)
  .to(".ghost",{autoAlpha:0,duration:0.2*b},hold+0.65*b);
go(hold+0.15*b,"#mk-c0",0.7*b);
tl.set({},{},D);
window.__timelines=window.__timelines||{};
window.__timelines["mg"]=tl;
})();</script>
</body>
</html>
```

### Example B, search lands on one answer

Recipes 02 and 04 with no cursor: the pill widens, the query types, three
results rise a beat apart. In the hold a highlight ring walks the list with
its leading edge on a fast ease and its trailing edge on a slow one, so it
stretches between rows. Ending: resolve. The other rows dim, the chosen
row's chip fills, and a preview card wipes open beside it (below it in
portrait). Palette: green-black, mint and pale grey. Type: a geometric sans
display with a monospace body, which the `ch` typing needs.

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=1920, height=1080">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Outfit:wght@600&family=DM+Mono:wght@400;500&display=swap">
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  html,body{width:100%;height:100%;overflow:hidden}
  #root{position:relative;width:100%;height:100%;overflow:hidden;background:var(--mg-bg);color:var(--mg-fg);font-family:var(--mg-font-body)}
  .clip{position:absolute;inset:0}
  .dots{position:absolute;inset:0;background:radial-gradient(var(--mg-muted) 0.14vmin,transparent 0.2vmin) 0 0/4vmin 4vmin;opacity:0.22}
  .cap{position:absolute;left:7%;top:8%}
  .kick{font-size:1.9vmin;letter-spacing:0.16em;text-transform:uppercase;color:var(--mg-accent)}
  .head{font-family:var(--mg-font-display);font-weight:var(--mg-display-weight);font-size:clamp(4.6vmin,4.4vw,6.4vmin);line-height:1.05;margin-top:1.4vmin;letter-spacing:-0.02em}
  .search{position:absolute;left:7%;top:31%;width:24vmin;height:8vmin;border-radius:4vmin;background:var(--mg-fg);color:var(--mg-bg);display:flex;align-items:center;padding:0 2.6vmin;gap:1.6vmin;overflow:hidden;font-size:2.6vmin}
  .search svg{flex:none;width:3vmin;height:3vmin}
  .ph{position:absolute;left:7.2vmin;color:var(--mg-muted);white-space:nowrap}
  .q{display:inline-block;overflow:hidden;white-space:nowrap;vertical-align:bottom;width:0;border-right:0.12em solid var(--mg-bg)}
  .results{position:absolute;left:7%;top:44%;width:86vmin;height:34vmin}
  .hl{position:absolute;left:-1.2vmin;right:-1.2vmin;top:0;bottom:24vmin;border-radius:2vmin;border:0.3vmin solid var(--mg-accent);opacity:0}
  .res{position:absolute;left:0;right:0;height:10vmin;display:flex;align-items:center;gap:2.4vmin;padding:0 2.4vmin;border-radius:1.6vmin;background:color-mix(in srgb,var(--mg-fg) 7%,transparent)}
  .res:nth-of-type(1){top:0}.res:nth-of-type(2){top:12vmin}.res:nth-of-type(3){top:24vmin}
  .tile{flex:none;width:5.4vmin;height:5.4vmin;border-radius:1.2vmin;background:var(--mg-accent);color:var(--mg-bg);display:grid;place-items:center;font-family:var(--mg-font-display);font-size:2.6vmin}
  .res b{display:block;font-family:var(--mg-font-display);font-weight:var(--mg-display-weight);font-size:2.9vmin}
  .res small{font-size:1.9vmin;color:var(--mg-muted)}
  .chip{margin-left:auto;padding:0.8vmin 1.8vmin;border-radius:3vmin;border:0.25vmin solid var(--mg-muted);font-size:1.8vmin;white-space:nowrap}
  .peek{position:absolute;left:calc(7% + 90vmin);right:7%;top:44%;height:34vmin;border-radius:2vmin;background:var(--mg-fg);color:var(--mg-bg);padding:3vmin;display:grid;align-content:start;gap:2vmin;clip-path:inset(0 100% 0 0)}
  .peek h3{font-family:var(--mg-font-display);font-weight:var(--mg-display-weight);font-size:3vmin}
  .peek p{font-size:2.1vmin;display:flex;gap:1.6vmin;align-items:center}
  .peek i{flex:none;width:3.6vmin;height:3.6vmin;border-radius:50%;background:var(--mg-accent);display:grid;place-items:center;font-style:normal;font-size:1.8vmin}
  @media (max-aspect-ratio:1/1){
    .cap{top:12%}.search{top:27%}.results{top:34%}
    .peek{left:7%;right:auto;width:86vmin;top:calc(34% + 38vmin);height:30vmin;clip-path:inset(0 0 100% 0)}
  }
</style>
</head>
<body><div id="root" data-composition-id="mg" data-start="0" data-width="1920" data-height="1080" data-duration="8" data-world="effects"
  style="--mg-bg:#0e1513;--mg-fg:#e6eee9;--mg-accent:#7ef0b2;--mg-muted:#6f857b;--mg-font-display:'Outfit',sans-serif;--mg-font-body:'DM Mono',monospace;--mg-display-weight:600">
<section class="clip" data-start="0" data-duration="8">
<div class="dots"></div>
<div class="cap"><p class="kick">Help centre</p><h1 class="head">Ask once. Land on the answer.</h1></div>
<div class="search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg><span class="ph">Search help</span><span class="q">refund a team plan</span></div>
<div class="results"><i class="hl"></i>
<div class="res"><span class="tile">R</span><span><b>Refund a team plan</b><small>Billing, 2 min read</small></span><span class="chip">Billing</span></div>
<div class="res"><span class="tile">S</span><span><b>Change seats mid-cycle</b><small>Billing, 3 min read</small></span><span class="chip">Seats</span></div>
<div class="res"><span class="tile">K</span><span><b>Cancel and keep your data</b><small>Account, 4 min read</small></span><span class="chip">Account</span></div>
</div>
<div class="peek"><h3>Refund a team plan</h3><p><i>1</i>Open Billing in settings</p><p><i>2</i>Pick the plan to refund</p><p><i>3</i>Send the request</p></div>
</section>
</div>
<script>(()=>{
const D=8;
const mv=Math.min(3,D*0.275),back=Math.min(1.6,D*0.175),hold=D-back,beat=mv*0.06;
const tl=gsap.timeline({paused:true,defaults:{ease:"power3.out",duration:0.6}});
const q=s=>document.querySelector(s),qa=s=>[...document.querySelectorAll(s)];
const R=q("#root"),v=n=>getComputedStyle(R).getPropertyValue("--mg-"+n).trim();
const spring=(z=0.8)=>p=>{if(p>=1)return 1;const d=(7/z)*Math.sqrt(1-z*z);return 1-Math.exp(-7*p)*(Math.cos(d*p)+7/d*Math.sin(d*p));};
const rows=qa(".res"),chips=qa(".chip"),span=hold-mv;
const n=q(".q").textContent.length,typeAt=0.34*mv,typeLen=n*0.045;
tl.from([".kick",".head"],{yPercent:40,autoAlpha:0,duration:0.7,stagger:beat},0);
tl.to(".search",{width:"86vmin",duration:0.3*mv,ease:spring(0.85)},0.04*mv)
  .to(".ph",{autoAlpha:0,duration:0.15},typeAt-0.1)
  .to(".q",{width:n+"ch",duration:typeLen,ease:`steps(${n})`},typeAt)
  .from(rows,{yPercent:35,autoAlpha:0,duration:0.5,stagger:beat*1.5},typeAt+typeLen+0.1)
  .to(".q",{borderRightColor:"rgba(0,0,0,0)",duration:0.01,repeat:2*Math.floor(span/0.72)-1,yoyo:true,repeatDelay:0.35},mv);
const at=k=>({top:k*12+"vmin",bottom:(2-k)*12+"vmin"});
let k0=0;
const step=(t,k)=>{const dn=k>k0,lead=dn?"bottom":"top",trail=dn?"top":"bottom",p=at(k);k0=k;
  tl.to(".hl",{[lead]:p[lead],duration:0.35,ease:"expo.out"},t).to(".hl",{[trail]:p[trail],duration:0.65,ease:"power2.out"},t+0.06);};
tl.to(".hl",{autoAlpha:1,duration:0.3},mv+0.2);
step(mv+0.25*span,1);step(mv+0.5*span,2);step(mv+0.75*span,0);
tl.to([rows[1],rows[2]],{opacity:0.35,duration:0.3*back},hold)
  .to(chips[0],{backgroundColor:v("accent"),borderColor:v("accent"),color:v("bg"),duration:0.2*back},hold+0.05*back)
  .to(".peek",{clipPath:"inset(0% 0% 0% 0%)",duration:0.55*back,ease:spring(0.9)},hold+0.08*back)
  .from(qa(".peek>*"),{autoAlpha:0,filter:"blur(8px)",duration:0.2*back,stagger:0.05*back},hold+0.3*back);
tl.set({},{},D);
window.__timelines=window.__timelines||{};
window.__timelines["mg"]=tl;
})();</script>
</body>
</html>
```

### Example C, flows fill a word

Recipes 15 and 09: three source pills send pulses along curves that land
vertically on the top edge of a big outlined word; the fill rises through
the letters with an edge marker riding it, and the second line slides up
from behind a cut. Two more waves of pulses keep the hold alive. Ending:
settle. The wires retract, the sources leave, and the filled word holds as
a lockup. Palette: deep navy, amber and cream. Type: a heavy condensed
display with a plain grotesque. Its glyphs hang below the tight line box,
so the fill clip and the edge marker run past the box at both ends. The wires are written from marks at load,
so the same script draws them in landscape and portrait.

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=1920, height=1080">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Oswald:wght@700&family=Work+Sans:wght@400;600&display=swap">
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  html,body{width:100%;height:100%;overflow:hidden}
  #root{position:relative;width:100%;height:100%;overflow:hidden;background:var(--mg-bg);color:var(--mg-fg);font-family:var(--mg-font-body)}
  .clip{position:absolute;inset:0}
  .mk{position:absolute;width:0;height:0}
  .pin{position:absolute;width:0;height:0;display:flex;align-items:center;justify-content:center}
  .pin>*{flex:none}
  .wires{position:absolute;inset:0;width:100%;height:100%}
  .wire{fill:none;stroke:var(--mg-muted);stroke-width:0.28vmin}
  .pulse{fill:none;stroke:var(--mg-accent);stroke-width:0.9vmin;stroke-linecap:round}
  .kick{position:absolute;left:7%;top:8%;font-size:2vmin;font-weight:600;letter-spacing:0.2em;text-transform:uppercase;color:var(--mg-muted)}
  .kick b{color:var(--mg-accent)}
  .src{padding:1.4vmin 2.8vmin;border-radius:4vmin;border:0.3vmin solid var(--mg-muted);background:var(--mg-bg);font-size:2.6vmin;font-weight:600;white-space:nowrap}
  #mk-a,#p-a{left:14%;top:24%}#mk-b,#p-b{left:14%;top:38%}#mk-c,#p-c{left:14%;top:52%}
  .w-mk,.ring{top:calc(80% - 21.6vmin)}
  #mk-1,.r1{left:calc(94% - 56vmin)}#mk-2,.r2{left:calc(94% - 37vmin)}#mk-3,.r3{left:calc(94% - 18vmin)}
  .word{position:absolute;right:6%;bottom:20%;font-family:var(--mg-font-display);font-weight:var(--mg-display-weight);font-size:24vmin;line-height:0.9;letter-spacing:0.01em}
  .outline{color:transparent;-webkit-text-stroke:0.3vmin var(--mg-fg)}
  .fill{position:absolute;left:0;top:0;color:var(--mg-accent);clip-path:inset(120% 0 -20% 0)}
  .edge{position:absolute;left:-3%;right:-3%;bottom:-10%;height:0.4vmin;background:var(--mg-accent)}
  .edge::after{content:"";position:absolute;right:0;top:-0.9vmin;width:2.2vmin;height:2.2vmin;background:var(--mg-accent)}
  .cut{position:absolute;right:6%;top:83.5%;overflow:hidden}
  .line2{font-size:3vmin;color:var(--mg-fg)}
  .ring{position:absolute;width:7vmin;height:7vmin;margin:-3.5vmin 0 0 -3.5vmin;border-radius:50%;border:0.35vmin solid var(--mg-accent);opacity:0}
  @media (max-aspect-ratio:1/1){
    #mk-a,#p-a{left:80%;top:26%}#mk-b,#p-b{left:50%;top:32%}#mk-c,#p-c{left:20%;top:26%}
    .word{bottom:34%}.w-mk,.ring{top:calc(66% - 21.6vmin)}.cut{top:68%}
  }
</style>
</head>
<body><div id="root" data-composition-id="mg" data-start="0" data-width="1920" data-height="1080" data-duration="8" data-world="effects"
  style="--mg-bg:#13233f;--mg-fg:#f3ede2;--mg-accent:#ffb43a;--mg-muted:#7e8aa4;--mg-font-display:'Oswald',sans-serif;--mg-font-body:'Work Sans',sans-serif;--mg-display-weight:700">
<section class="clip" data-start="0" data-duration="8">
<svg class="wires"></svg>
<p class="kick">The monthly read <b>/</b> Issue 12</p>
<i class="mk" id="mk-a"></i><i class="mk" id="mk-b"></i><i class="mk" id="mk-c"></i>
<i class="mk w-mk" id="mk-1"></i><i class="mk w-mk" id="mk-2"></i><i class="mk w-mk" id="mk-3"></i>
<div class="pin" id="p-a"><span class="src">Notes</span></div>
<div class="pin" id="p-b"><span class="src">Calls</span></div>
<div class="pin" id="p-c"><span class="src">Reads</span></div>
<div class="word"><span class="outline">DIGEST</span><span class="fill" aria-hidden="true">DIGEST</span><i class="edge"></i></div>
<div class="cut"><p class="line2">What moved this month, in one place</p></div>
<i class="ring r1"></i><i class="ring r2"></i><i class="ring r3"></i>
</section>
</div>
<script>(()=>{
const D=8;
const mv=Math.min(3,D*0.275),back=Math.min(1.6,D*0.175),hold=D-back,beat=mv*0.06;
const tl=gsap.timeline({paused:true,defaults:{ease:"power3.out",duration:0.6}});
const q=s=>document.querySelector(s),qa=s=>[...document.querySelectorAll(s)];
const R=q("#root"),at=s=>{const e=q(s);return {left:e.offsetLeft,top:e.offsetTop};};
const span=hold-mv,wd=Math.min(0.9,0.3*span),svg=q(".wires");svg.setAttribute("viewBox",`0 0 ${R.offsetWidth} ${R.offsetHeight}`);
const curve=(a,b)=>`M${a.left} ${a.top}C${b.left} ${a.top} ${b.left} ${a.top} ${b.left} ${b.top}`;
const path=(cls,d,dash,off)=>{svg.insertAdjacentHTML("beforeend",`<path class="${cls}" d="${d}" pathLength="1" stroke-dasharray="${dash}" stroke-dashoffset="${off}"/>`);return svg.lastElementChild;};
const L=[["#mk-a","#mk-3",".r3"],["#mk-b","#mk-2",".r2"],["#mk-c","#mk-1",".r1"]].map(([s,e,r])=>({d:curve(at(s),at(e)),r}));
const wires=L.map(l=>path("wire",l.d,"1 1",1));
const wave=(t,draw)=>L.forEach((l,i)=>{const s=t+i*0.14,p=path("pulse",l.d,"0.06 2",0.1);
  if(draw)tl.to(wires[i],{attr:{"stroke-dashoffset":0},duration:wd,ease:"power1.inOut"},s);
  tl.to(p,{attr:{"stroke-dashoffset":-1.04},duration:wd,ease:"power1.inOut"},s)
    .fromTo(l.r,{scale:0,autoAlpha:0.7},{scale:1,autoAlpha:0,duration:0.4,ease:"power2.out",immediateRender:false},s+wd-0.05);});
tl.from(".kick",{autoAlpha:0,yPercent:40,duration:0.5},0)
  .from(qa(".src"),{autoAlpha:0,xPercent:-30,duration:0.5,stagger:beat},0.02*mv)
  .from(".outline",{autoAlpha:0,duration:0.5},0.05*mv);
wave(0.12*mv,true);
const f=0.55*mv;
tl.to(".fill",{clipPath:"inset(-20% 0 -20% 0)",duration:0.62*mv,ease:"power2.inOut"},f)
  .to(".edge",{bottom:"110%",duration:0.62*mv,ease:"power2.inOut"},f)
  .to([".edge",".outline"],{autoAlpha:0,duration:0.25},f+0.62*mv)
  .from(".line2",{yPercent:110,duration:0.6},f+0.4*mv);
const k=Math.max(1,Math.round(span/2.2));for(let i=0;i<k;i++)wave(mv+span*(i+0.15)/k,false);
tl.to(wires,{attr:{"stroke-dashoffset":-1},duration:0.55*back,ease:"power2.in",stagger:0.1*back},hold)
  .to(qa(".src"),{autoAlpha:0,xPercent:-30,duration:0.3*back,ease:"power2.in",stagger:0.05*back},hold+0.15*back)
  .to(".word",{xPercent:-2,duration:0.85*back,ease:"power3.inOut"},hold);
tl.set({},{},D);
window.__timelines=window.__timelines||{};
window.__timelines["mg"]=tl;
})();</script>
</body>
</html>
```
