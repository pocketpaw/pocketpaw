# Shared setup

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
