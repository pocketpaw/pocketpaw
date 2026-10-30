# Studio Motion presets

One complete composition per `data-preset`, exactly as the editor's Graphics gallery emits it at 1920x1080, with line breaks added. Set `data-width` / `data-height` and the viewport meta to the project's frame size and the fluid layout does the rest. Any preset takes any style.

## Contents

- [title](#title): swiss-pulse, rise effect.
- [statement](#statement): velvet.
- [logo](#logo): maximalist, wordmark only.
- [stat](#stat): data-drift.
- [chart](#chart): soft-signal.

## title

Swap the `.mg-head` tween for another effect from the SKILL.md. For mask-up, also wrap the headline and the subline in `.mg-mask`.

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=1920, height=1080">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter+Tight:wght@800&family=Inter:wght@400;600&display=swap">
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  html,body{width:100%;height:100%;overflow:hidden}
  #root{position:relative;width:100%;height:100%;overflow:hidden;background:var(--mg-bg);color:var(--mg-fg);font-family:var(--mg-font-body)}
  .clip{position:absolute;inset:0;display:grid;place-items:center;padding:8vmin}
  .mg-stage{width:100%;display:grid;justify-items:center;gap:3vmin;text-align:center}
  .mg-display{font-family:var(--mg-font-display);font-weight:var(--mg-display-weight);line-height:1.04;max-width:92%}
  .mg-sub{color:var(--mg-muted);font-size:clamp(3.2vmin,4.2vw,5vmin);line-height:1.3;max-width:80%}
  .mg-accent{color:var(--mg-accent)}
  .mg-mask{overflow:hidden;padding:0.06em 0}
  .mg-w{display:inline-block;white-space:nowrap}
  .mg-c{display:inline-block}
  .mg-rule{width:10vmin;height:1vmin;background:var(--mg-accent)}
  .mg-head{font-size:clamp(8vmin,11vw,15vmin)}
</style>
</head>
<body><div id="root" data-composition-id="mg" data-start="0" data-width="1920" data-height="1080" data-duration="4" data-preset="title" data-style="swiss-pulse"
  style="--mg-bg:#f4f4f0;--mg-fg:#111111;--mg-accent:#e63312;--mg-muted:#6b6b6b;--mg-font-display:'Inter Tight',sans-serif;--mg-font-body:'Inter',sans-serif;--mg-display-weight:800">
<section class="clip" data-start="0" data-duration="4">
<div class="mg-stage">
<div class="mg-rule"></div>
<h1 class="mg-display mg-head">Make it move</h1>
<p class="mg-sub">A short line underneath</p></div></section>
</div>
<script>(()=>{
const D=4;
const tl=gsap.timeline({paused:true,defaults:{ease:"power3.out",duration:0.7}});
const q=(s)=>document.querySelector(s);
const qa=(s)=>[...document.querySelectorAll(s)];
const split=(el,by)=>{const words=el.textContent.split(/\s+/).filter(Boolean);el.textContent="";const out=[];words.forEach((word,i)=>{if(i)el.append(" ");const w=document.createElement("span");w.className="mg-w";el.append(w);if(by==="words"){w.textContent=word;out.push(w);return;}for(const ch of word){const c=document.createElement("span");c.className="mg-c";c.textContent=ch;w.append(c);out.push(c);}});return out;};
const fmt=(v,dp)=>v.toLocaleString("en-US",{minimumFractionDigits:dp,maximumFractionDigits:dp});
const count=(el,at,dur)=>{const to=Number(el.dataset.to)||0,dp=Number(el.dataset.decimals)||0,o={v:0};el.textContent=fmt(0,dp);tl.to(o,{v:to,duration:dur,ease:"power2.out",onUpdate:()=>{el.textContent=fmt(o.v,dp);}},at);};
tl.from(".mg-rule",{scaleX:0,duration:0.6},0.1);
tl.from(split(q(".mg-head"),"chars"),{yPercent:80,autoAlpha:0,stagger:0.03},0.3);
if(q(".mg-sub"))tl.from(".mg-sub",{yPercent:40,autoAlpha:0},"-=0.3");
tl.to(".mg-stage",{autoAlpha:0,duration:0.4,ease:"power1.in"},Math.max(0,D-0.45));
tl.set({},{},D);
window.__timelines=window.__timelines||{};
window.__timelines["mg"]=tl;
})();</script>
</body>
</html>
```

## statement

Two to four `.mg-line` elements. The slots divide the duration evenly, so adding a line needs no script change.

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=1920, height=1080">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@600&family=Inter:wght@400;600&display=swap">
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  html,body{width:100%;height:100%;overflow:hidden}
  #root{position:relative;width:100%;height:100%;overflow:hidden;background:var(--mg-bg);color:var(--mg-fg);font-family:var(--mg-font-body)}
  .clip{position:absolute;inset:0;display:grid;place-items:center;padding:8vmin}
  .mg-stage{width:100%;display:grid;justify-items:center;gap:3vmin;text-align:center}
  .mg-display{font-family:var(--mg-font-display);font-weight:var(--mg-display-weight);line-height:1.04;max-width:92%}
  .mg-sub{color:var(--mg-muted);font-size:clamp(3.2vmin,4.2vw,5vmin);line-height:1.3;max-width:80%}
  .mg-accent{color:var(--mg-accent)}
  .mg-mask{overflow:hidden;padding:0.06em 0}
  .mg-w{display:inline-block;white-space:nowrap}
  .mg-c{display:inline-block}
  .mg-lines{display:grid;place-items:center;width:100%}
  .mg-line{grid-area:1/1;font-size:clamp(7vmin,10vw,13vmin)}
</style>
</head>
<body><div id="root" data-composition-id="mg" data-start="0" data-width="1920" data-height="1080" data-duration="6" data-preset="statement" data-style="velvet"
  style="--mg-bg:#14110f;--mg-fg:#f3ece2;--mg-accent:#c9a86a;--mg-muted:#8c8378;--mg-font-display:'Playfair Display',sans-serif;--mg-font-body:'Inter',sans-serif;--mg-display-weight:600">
<section class="clip" data-start="0" data-duration="6">
<div class="mg-stage">
<div class="mg-lines">
<p class="mg-display mg-line">Ideas are cheap.</p>
<p class="mg-display mg-line">Shipping is hard.</p>
<p class="mg-display mg-line mg-accent">We ship.</p></div></div></section>
</div>
<script>(()=>{
const D=6;
const tl=gsap.timeline({paused:true,defaults:{ease:"power2.inOut",duration:0.7}});
const q=(s)=>document.querySelector(s);
const qa=(s)=>[...document.querySelectorAll(s)];
const split=(el,by)=>{const words=el.textContent.split(/\s+/).filter(Boolean);el.textContent="";const out=[];words.forEach((word,i)=>{if(i)el.append(" ");const w=document.createElement("span");w.className="mg-w";el.append(w);if(by==="words"){w.textContent=word;out.push(w);return;}for(const ch of word){const c=document.createElement("span");c.className="mg-c";c.textContent=ch;w.append(c);out.push(c);}});return out;};
const fmt=(v,dp)=>v.toLocaleString("en-US",{minimumFractionDigits:dp,maximumFractionDigits:dp});
const count=(el,at,dur)=>{const to=Number(el.dataset.to)||0,dp=Number(el.dataset.decimals)||0,o={v:0};el.textContent=fmt(0,dp);tl.to(o,{v:to,duration:dur,ease:"power2.out",onUpdate:()=>{el.textContent=fmt(o.v,dp);}},at);};
const ls=qa(".mg-line"),slot=(D-0.8)/Math.max(1,ls.length);ls.forEach((line,i)=>{const at=0.3+i*slot,ws=split(line,"words");
tl.from(ws,{yPercent:50,autoAlpha:0,duration:0.5,stagger:Math.min(0.14,(slot*0.35)/ws.length)},at);if(i<ls.length-1)tl.to(line,{yPercent:-15,autoAlpha:0,duration:0.3,ease:"power1.in"},at+slot-0.35);});
tl.to(".mg-stage",{autoAlpha:0,duration:0.4,ease:"power1.in"},Math.max(0,D-0.45));
tl.set({},{},D);
window.__timelines=window.__timelines||{};
window.__timelines["mg"]=tl;
})();</script>
</body>
</html>
```

## logo

To add a mark, put `<img class="mg-mark" src="https://…" alt="">` before the mask and remove `is-wordmark` from the stage.

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=1920, height=1080">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Anton:wght@400&family=Inter:wght@400;600&display=swap">
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  html,body{width:100%;height:100%;overflow:hidden}
  #root{position:relative;width:100%;height:100%;overflow:hidden;background:var(--mg-bg);color:var(--mg-fg);font-family:var(--mg-font-body)}
  .clip{position:absolute;inset:0;display:grid;place-items:center;padding:8vmin}
  .mg-stage{width:100%;display:grid;justify-items:center;gap:3vmin;text-align:center}
  .mg-display{font-family:var(--mg-font-display);font-weight:var(--mg-display-weight);line-height:1.04;max-width:92%}
  .mg-sub{color:var(--mg-muted);font-size:clamp(3.2vmin,4.2vw,5vmin);line-height:1.3;max-width:80%}
  .mg-accent{color:var(--mg-accent)}
  .mg-mask{overflow:hidden;padding:0.06em 0}
  .mg-w{display:inline-block;white-space:nowrap}
  .mg-c{display:inline-block}
  .mg-mark{max-width:40%;max-height:24vmin;object-fit:contain}
  .mg-name{font-size:clamp(7vmin,9vw,11vmin)}
  .is-wordmark .mg-name{font-size:clamp(9vmin,13vw,16vmin)}
</style>
</head>
<body><div id="root" data-composition-id="mg" data-start="0" data-width="1920" data-height="1080" data-duration="4" data-preset="logo" data-style="maximalist"
  style="--mg-bg:#ffde00;--mg-fg:#0a0a0a;--mg-accent:#ff2d55;--mg-muted:#333333;--mg-font-display:'Anton',sans-serif;--mg-font-body:'Inter',sans-serif;--mg-display-weight:400">
<section class="clip" data-start="0" data-duration="4">
<div class="mg-stage is-wordmark">
<div class="mg-mask">
<h1 class="mg-display mg-name">Northwind</h1></div>
<p class="mg-sub mg-tag">Built for the long run</p></div></section>
</div>
<script>(()=>{
const D=4;
const tl=gsap.timeline({paused:true,defaults:{ease:"back.out(1.6)",duration:0.7}});
const q=(s)=>document.querySelector(s);
const qa=(s)=>[...document.querySelectorAll(s)];
const split=(el,by)=>{const words=el.textContent.split(/\s+/).filter(Boolean);el.textContent="";const out=[];words.forEach((word,i)=>{if(i)el.append(" ");const w=document.createElement("span");w.className="mg-w";el.append(w);if(by==="words"){w.textContent=word;out.push(w);return;}for(const ch of word){const c=document.createElement("span");c.className="mg-c";c.textContent=ch;w.append(c);out.push(c);}});return out;};
const fmt=(v,dp)=>v.toLocaleString("en-US",{minimumFractionDigits:dp,maximumFractionDigits:dp});
const count=(el,at,dur)=>{const to=Number(el.dataset.to)||0,dp=Number(el.dataset.decimals)||0,o={v:0};el.textContent=fmt(0,dp);tl.to(o,{v:to,duration:dur,ease:"power2.out",onUpdate:()=>{el.textContent=fmt(o.v,dp);}},at);};
const mark=q(".mg-mark");if(mark)tl.from(mark,{scale:0.6,autoAlpha:0,duration:1,ease:"back.out(1.4)"},0.2);
tl.from(split(q(".mg-name"),"chars"),{yPercent:110,duration:0.8,stagger:0.025},mark?0.8:0.3);
if(q(".mg-tag"))tl.from(".mg-tag",{yPercent:30,autoAlpha:0,duration:0.8},"-=0.2");
tl.to(".mg-stage",{autoAlpha:0,duration:0.4,ease:"power1.in"},Math.max(0,D-0.45));
tl.set({},{},D);
window.__timelines=window.__timelines||{};
window.__timelines["mg"]=tl;
})();</script>
</body>
</html>
```

## stat

The number is `data-to` with `data-decimals` places; prefix and suffix are their own accent spans.

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=1920, height=1080">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;600;700&display=swap">
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  html,body{width:100%;height:100%;overflow:hidden}
  #root{position:relative;width:100%;height:100%;overflow:hidden;background:var(--mg-bg);color:var(--mg-fg);font-family:var(--mg-font-body)}
  .clip{position:absolute;inset:0;display:grid;place-items:center;padding:8vmin}
  .mg-stage{width:100%;display:grid;justify-items:center;gap:3vmin;text-align:center}
  .mg-display{font-family:var(--mg-font-display);font-weight:var(--mg-display-weight);line-height:1.04;max-width:92%}
  .mg-sub{color:var(--mg-muted);font-size:clamp(3.2vmin,4.2vw,5vmin);line-height:1.3;max-width:80%}
  .mg-accent{color:var(--mg-accent)}
  .mg-mask{overflow:hidden;padding:0.06em 0}
  .mg-w{display:inline-block;white-space:nowrap}
  .mg-c{display:inline-block}
  .mg-num{font-size:clamp(12vmin,18vw,24vmin);line-height:1;white-space:nowrap;font-variant-numeric:tabular-nums}
</style>
</head>
<body><div id="root" data-composition-id="mg" data-start="0" data-width="1920" data-height="1080" data-duration="4" data-preset="stat" data-style="data-drift"
  style="--mg-bg:#05060f;--mg-fg:#e8f0ff;--mg-accent:#00e5ff;--mg-muted:#6c7a99;--mg-font-display:'Space Grotesk',sans-serif;--mg-font-body:'Space Grotesk',sans-serif;--mg-display-weight:700">
<section class="clip" data-start="0" data-duration="4">
<div class="mg-stage">
<p class="mg-display mg-num"><span class="mg-accent">$</span><span class="mg-count" data-to="2.4" data-decimals="1">0</span><span class="mg-accent">M</span></p>
<p class="mg-sub mg-label">raised from 1,200 backers</p></div></section>
</div>
<script>(()=>{
const D=4;
const tl=gsap.timeline({paused:true,defaults:{ease:"expo.out",duration:0.7}});
const q=(s)=>document.querySelector(s);
const qa=(s)=>[...document.querySelectorAll(s)];
const split=(el,by)=>{const words=el.textContent.split(/\s+/).filter(Boolean);el.textContent="";const out=[];words.forEach((word,i)=>{if(i)el.append(" ");const w=document.createElement("span");w.className="mg-w";el.append(w);if(by==="words"){w.textContent=word;out.push(w);return;}for(const ch of word){const c=document.createElement("span");c.className="mg-c";c.textContent=ch;w.append(c);out.push(c);}});return out;};
const fmt=(v,dp)=>v.toLocaleString("en-US",{minimumFractionDigits:dp,maximumFractionDigits:dp});
const count=(el,at,dur)=>{const to=Number(el.dataset.to)||0,dp=Number(el.dataset.decimals)||0,o={v:0};el.textContent=fmt(0,dp);tl.to(o,{v:to,duration:dur,ease:"power2.out",onUpdate:()=>{el.textContent=fmt(o.v,dp);}},at);};
tl.from(".mg-num",{scale:0.85,autoAlpha:0,duration:0.6},0.2);count(q(".mg-count"),0.3,Math.min(2.2,D*0.5));
if(q(".mg-label"))tl.from(".mg-label",{yPercent:60,autoAlpha:0},"-=0.4");
tl.to(".mg-stage",{autoAlpha:0,duration:0.4,ease:"power1.in"},Math.max(0,D-0.45));
tl.set({},{},D);
window.__timelines=window.__timelines||{};
window.__timelines["mg"]=tl;
})();</script>
</body>
</html>
```

## chart

Each row's `--w` is its value as a percentage of the largest, and the largest row has `is-max`. Recompute both when the data changes.

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=1920, height=1080">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:wght@600&family=DM+Sans:wght@400;600&display=swap">
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  html,body{width:100%;height:100%;overflow:hidden}
  #root{position:relative;width:100%;height:100%;overflow:hidden;background:var(--mg-bg);color:var(--mg-fg);font-family:var(--mg-font-body)}
  .clip{position:absolute;inset:0;display:grid;place-items:center;padding:8vmin}
  .mg-stage{width:100%;display:grid;justify-items:center;gap:3vmin;text-align:center}
  .mg-display{font-family:var(--mg-font-display);font-weight:var(--mg-display-weight);line-height:1.04;max-width:92%}
  .mg-sub{color:var(--mg-muted);font-size:clamp(3.2vmin,4.2vw,5vmin);line-height:1.3;max-width:80%}
  .mg-accent{color:var(--mg-accent)}
  .mg-mask{overflow:hidden;padding:0.06em 0}
  .mg-w{display:inline-block;white-space:nowrap}
  .mg-c{display:inline-block}
  .is-chart{justify-items:stretch;text-align:left;gap:4vmin}
  .mg-title{font-size:clamp(5vmin,7vw,9vmin)}
  .mg-rows{display:grid;gap:2.4vmin}
  .mg-row{display:grid;grid-template-columns:minmax(0,28%) 1fr 16%;align-items:center;gap:2vmin;font-size:clamp(2.6vmin,4vw,4.4vmin)}
  .mg-track{height:4.5vmin}
  .mg-bar{display:block;height:100%;width:var(--w);background:var(--mg-muted);transform-origin:left center;border-radius:0.6vmin}
  .is-max .mg-bar{background:var(--mg-accent)}
  .mg-val{text-align:right;font-weight:600;font-variant-numeric:tabular-nums}
</style>
</head>
<body><div id="root" data-composition-id="mg" data-start="0" data-width="1920" data-height="1080" data-duration="5" data-preset="chart" data-style="soft-signal"
  style="--mg-bg:#fbf3ec;--mg-fg:#2b2420;--mg-accent:#e07a5f;--mg-muted:#8a7d74;--mg-font-display:'Fraunces',sans-serif;--mg-font-body:'DM Sans',sans-serif;--mg-display-weight:600">
<section class="clip" data-start="0" data-duration="5">
<div class="mg-stage is-chart">
<h2 class="mg-display mg-title">Where the hours go</h2>
<div class="mg-rows">
<div class="mg-row" style="--w:61.76%"><span class="mg-lbl">Design</span><span class="mg-track"><span class="mg-bar"></span></span><span class="mg-val" data-to="42" data-decimals="0">0</span></div>
<div class="mg-row is-max" style="--w:100%"><span class="mg-lbl">Build</span><span class="mg-track"><span class="mg-bar"></span></span><span class="mg-val" data-to="68" data-decimals="0">0</span></div>
<div class="mg-row" style="--w:35.29%"><span class="mg-lbl">Test</span><span class="mg-track"><span class="mg-bar"></span></span><span class="mg-val" data-to="24" data-decimals="0">0</span></div>
<div class="mg-row" style="--w:17.65%"><span class="mg-lbl">Ship</span><span class="mg-track"><span class="mg-bar"></span></span><span class="mg-val" data-to="12" data-decimals="0">0</span></div></div></div></section>
</div>
<script>(()=>{
const D=5;
const tl=gsap.timeline({paused:true,defaults:{ease:"sine.out",duration:0.7}});
const q=(s)=>document.querySelector(s);
const qa=(s)=>[...document.querySelectorAll(s)];
const split=(el,by)=>{const words=el.textContent.split(/\s+/).filter(Boolean);el.textContent="";const out=[];words.forEach((word,i)=>{if(i)el.append(" ");const w=document.createElement("span");w.className="mg-w";el.append(w);if(by==="words"){w.textContent=word;out.push(w);return;}for(const ch of word){const c=document.createElement("span");c.className="mg-c";c.textContent=ch;w.append(c);out.push(c);}});return out;};
const fmt=(v,dp)=>v.toLocaleString("en-US",{minimumFractionDigits:dp,maximumFractionDigits:dp});
const count=(el,at,dur)=>{const to=Number(el.dataset.to)||0,dp=Number(el.dataset.decimals)||0,o={v:0};el.textContent=fmt(0,dp);tl.to(o,{v:to,duration:dur,ease:"power2.out",onUpdate:()=>{el.textContent=fmt(o.v,dp);}},at);};
tl.from(".mg-title",{yPercent:40,autoAlpha:0},0.2);
qa(".mg-row").forEach((row,i)=>{const at=0.6+i*0.15;
tl.from(row.querySelector(".mg-lbl"),{autoAlpha:0,duration:0.5},at);
tl.from(row.querySelector(".mg-bar"),{scaleX:0,duration:1},at);count(row.querySelector(".mg-val"),at,1);});
tl.to(".mg-stage",{autoAlpha:0,duration:0.4,ease:"power1.in"},Math.max(0,D-0.45));
tl.set({},{},D);
window.__timelines=window.__timelines||{};
window.__timelines["mg"]=tl;
})();</script>
</body>
</html>
```
