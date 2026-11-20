# Interface and control recipes

Every recipe here assumes the clock helpers (`mv`, `hold`, `span`, `back`, `beat`) from [setup.md](setup.md). Snippets show only what is specific to the recipe.

## Contents

- [01 button to player](#01-button-to-player)
- [02 search to results](#02-search-to-results)
- [03 card to workspace](#03-card-to-workspace)
- [04 tabs to panels](#04-tabs-to-panels)
- [17 morphing shape](#17-morphing-shape)
- [18 slider drag](#18-slider-drag)
- [19 toggle and toast](#19-toggle-and-toast)
- [20 terminal typing](#20-terminal-typing)

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
