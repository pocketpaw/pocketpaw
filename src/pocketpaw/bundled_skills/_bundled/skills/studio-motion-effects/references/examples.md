# Full compositions

Three complete graphics combining the recipes. Each is one instance: do not reuse its layout, copy or palette.

## Contents

- [Example A, cursor opens a player](#example-a-cursor-opens-a-player)
- [Example B, search lands on one answer](#example-b-search-lands-on-one-answer)
- [Example C, flows fill a word](#example-c-flows-fill-a-word)

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
