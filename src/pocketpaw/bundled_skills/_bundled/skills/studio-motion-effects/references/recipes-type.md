# Type recipes

Every recipe here assumes the clock helpers (`mv`, `hold`, `span`, `back`, `beat`) from [setup.md](setup.md). Snippets show only what is specific to the recipe.

## Contents

- [09 masked type](#09-masked-type)
- [10 elastic type](#10-elastic-type)
- [11 text to layout](#11-text-to-layout)
- [12 shutter reveal](#12-shutter-reveal)

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
