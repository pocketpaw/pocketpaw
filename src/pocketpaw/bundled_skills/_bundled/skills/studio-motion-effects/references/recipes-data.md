# Data and system recipes

Every recipe here assumes the clock helpers (`mv`, `hold`, `span`, `back`, `beat`) from [setup.md](setup.md). Snippets show only what is specific to the recipe.

## Contents

- [05 chart morph](#05-chart-morph)
- [06 dashboard zoom](#06-dashboard-zoom)
- [07 spring stack](#07-spring-stack)
- [08 magnetic dock](#08-magnetic-dock)
- [13 layer spread](#13-layer-spread)
- [14 lens focus](#14-lens-focus)
- [15 flowing paths](#15-flowing-paths)
- [16 tile assemble](#16-tile-assemble)

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
