---
name: studio-motion-effects
description: |
  Interaction and transition recipes for /studio/editor motion graphics: button to panel,
  search to results, chart morph, masked type, cursor-driven UI, one morphing shape and more,
  written as DOM/SVG + GSAP for the browser renderer behind add_motion_graphic. Load it with
  studio-motion-direction when a graphic needs something to happen on screen beyond text
  appearing. It does not cover the render contract (studio-motion) or picking a look
  (studio-motion-direction).
---

# Studio Motion Effects

`studio-motion-direction` picks the look, `studio-motion` and
`hyperframes-core` hold the render contract. This skill is the middle layer:
what actually happens on screen. Every recipe here is one object turning into
another object (a button into a player, bars into a line, a pill into a card),
driven by one clock, and drawn with plain DOM, SVG and GSAP so the editor's
renderer paints it the same in every browser.

Recipes live in four files; read only the one you need, before writing:
[references/setup.md](references/setup.md) (the clock helpers every recipe
uses), [references/recipes-interfaces.md](references/recipes-interfaces.md)
(01-04, 17-20), [references/recipes-data.md](references/recipes-data.md)
(05-08, 13-16) and [references/recipes-type.md](references/recipes-type.md)
(09-12). Three full compositions: [references/examples.md](references/examples.md).

## The clock

Every recipe runs on three phases written as fractions of `D` (the root's
`data-duration`), so the same recipe works at 4 s and at 20 s. At 8 s they
come out as move 0 to 2.2 s, hold to 6.6 s, finish by 8 s.

```js
const D=8;
const mv=Math.min(3,D*0.275);     // the move ends here
const back=Math.min(1.6,D*0.175); // length of the finish
const hold=D-back;                // the hold ends, the finish starts
const span=hold-mv;               // length of the hold
const beat=mv*0.06;               // one stagger step (about 0.13 s at 8 s)
```

- **Move (0 to `mv`).** The one change. Inside it, parts start early
  (0 to `0.36*mv`), middle (`0.36*mv` to `0.64*mv`) or late (`0.64*mv` to
  `mv`). The shell moves first, its new contents arrive in the second half.
- **Hold (`mv` to `hold`).** The finished state, which is where people
  actually look. Something small must stay alive: a progress bar creeping, a
  counter, a highlight stepping down a list, a second wave of pulses. Two
  stills taken in the hold must differ. Place hold beats at fractions of
  `span` (`mv+0.25*span`), never at `mv` plus fixed seconds, and past 12 s
  repeat the beat rather than stretching the move.
- **Finish (`hold` to `D`).** Pick one ending and say which in the reply:
  - *return*: every element eases back to its exact start. The last frame
    matches the first, so the graphic loops and hands back cleanly.
  - *resolve*: the hold narrows to one answer (the chosen row lifts, the rest
    dim) and stays there.
  - *settle*: the supporting parts clear away and the hero holds as a lockup.
  Size finishing tweens as fractions of `back`; every one must end by `D`. `tl.set({},{},D)` pads a short
  timeline but never trims a long one, and a timeline longer than `D` fails.

Fixed seconds are only for short things: a press, a content swap, one typed
character. A recipe with typing or a cursor journey reads best at 6 s or
more; at 4 s, pick one move.

Write the finish as explicit `to()` tweens back to the start values. A second
`from()` or `fromTo()` on a property that already has a tween fights it,
because those render their start values the moment they are created.

## Continuity rules

1. **Morph, never cut.** One shell changes its width, height, corner radius,
   colour and position. Do not fade one picture out and another in. The
   shell tweens layout values (`width`, `height`, `borderRadius`,
   `backgroundColor`, `left`, `top`) in `vmin` and `%`.
2. **Contents swap on their own clock.** The old contents blur out fast
   (0.12 s), the new ones wait a moment (0.07 s) and blur in (0.26 s), so old
   and new text never overlap. Swap contents only once the shell is past
   halfway, so nothing is squashed.
3. **Leave a ghost.** A dashed outline of the start state stays where the
   object began (opacity about 0.35) through the hold, so the eye can follow
   where it came from.
4. **Stagger by a beat.** Rows, bars, letters and slats each start one `beat`
   after the last. Everything moving together reads as a slide transition.
5. **Shared elements travel.** A title that exists in both states moves from
   one place to the other. It is never duplicated or retyped.
6. **Values never change.** When bars become a line, the same numbers are
   drawn both ways. Sample numbers carry a visible "example" label.
7. **Fill the frame.** The effect is the frame, not a small tile in an empty
   canvas. Size the hero in `vmin` (60 to 90).

## Placing things the cursor touches

Layout stays fluid (`%`, `vmin`, `clamp()`), so the script cannot hard-code
pixel targets. Put a **mark** wherever something lands or gets clicked: an
empty `<i class="mk">` that is `position:absolute`, zero-sized and placed with
`left`/`top` in `%` on the stage. A `@media (max-aspect-ratio:1/1)` block can
move marks for portrait frames. Read them once, before building any tween:

```js
const at=s=>{const e=q(s);return {left:e.offsetLeft,top:e.offsetTop};};
```

Objects that grow around a point sit in a **pin**: a zero-sized absolute
flex box with `align-items:center;justify-content:center`, so its child
stays centred on the point while its width and height tween. Never centre
with `translate(-50%,-50%)` on something GSAP also moves.

## The cursor

- An SVG arrow in `.fx-cur`, absolutely positioned so its tip is the
  element's top-left corner, constant size (about 3.2 vmin), dark fill with a
  light outline so it reads on any ground. It lives on the same stage as the
  marks.
- **Eased path.** Tween `left` and `top` at the same time with different eases
  (`power2.inOut` and `power3.inOut`); the mismatch bends the path into a
  slight human arc. One hop takes 0.6 to 0.9 s.
- **Arrive, then act.** It reaches the target 0.15 to 0.3 s before the
  click. It never teleports, never clicks empty space and never leaves the
  frame, including while the thing it clicked is growing.
- **Press squash.** 0.07 s before the click the cursor scales to 0.86 and the
  target to 0.965; both spring back after it.
- **Click ripple.** A ring on the stage at the click mark scales from 0 to 1
  and fades out over 0.45 s. One ring element per click.
- **Drags** hold the squash for the whole drag, and the dragged thing follows
  the same tween as the cursor.

```js
const go=(t,s,d=0.7)=>{const p=at(s);tl.to(cur,{left:p.left,duration:d,ease:"power2.inOut"},t).to(cur,{top:p.top,duration:d,ease:"power3.inOut"},t);};
const click=(t,target,ring)=>{
  const hit=[cur,q(target)];
  tl.to(hit,{scale:(i)=>i?0.965:0.86,duration:0.07,ease:"power2.in"},t-0.07)
    .to(hit,{scale:1,duration:0.35,ease:spring(0.72)},t+0.035);
  if(ring)tl.fromTo(ring,{scale:0,autoAlpha:0.5},{scale:1,autoAlpha:0,duration:0.45,ease:"power2.out",immediateRender:false},t);
};
```

`cur` is the `.fx-cur` element; `target` and `ring` are selectors. Place
each ring with `left`/`top` on its click mark in CSS.

## Spring feel in GSAP terms

The feel is springs with at most a tiny overshoot, and leading and trailing
edges on different springs so indicators stretch. Stock eases cover most of
it:

| job | ease | duration |
| --- | --- | --- |
| a shell changing shape | `spring(0.8)` or `back.out(0.6)` (about 1% overshoot) | 0.9 to 1.3 s |
| a leading edge, a fast follower | `expo.out` | 0.35 to 0.5 s |
| a trailing edge, a slow follower | `power2.out` | 0.6 to 0.8 s |
| a card with weight swinging into place | `spring(0.6)` or `back.out(1.5)` (about 8%) | 0.8 to 1.2 s |
| press release | `spring(0.72)` | 0.35 s |
| camera push, page dim | `power3.out` or `sine.inOut` | 0.8 to 1.4 s |
| content blur-in | `power3.out` | 0.26 s |
| the finish (back to start) | `power3.inOut` | up to `back` |

`spring(z)` is a damped spring as a GSAP ease function. `z` is damping: 0.8
barely overshoots, 0.6 swings once and settles. It returns exactly 1 at the
end of the tween:

```js
const spring=(z=0.8)=>p=>{if(p>=1)return 1;const d=(7/z)*Math.sqrt(1-z*z);return 1-Math.exp(-7*p)*(Math.cos(d*p)+7/d*Math.sin(d*p));};
```

Overshoot belongs to things with weight (cards, shells, the press). Text
never overshoots.

## What the renderer does with CSS

Tested through the editor's capture path in Chromium and WebKit:

- **Safe in both:** `filter: blur()`, `clip-path: inset()` (tweenable),
  `mask-image` with a linear gradient, `background-clip: text`,
  `-webkit-text-stroke`, SVG `pathLength` with dash tweens, SVG `clipPath`,
  `vector-effect: non-scaling-stroke`, conic gradients, dashed borders,
  `var()` inside SVG `style`, GSAP tweens of `attr` on SVG.
- **Broken in WebKit:** `backdrop-filter` (no blur, only the tint) and 3D
  transforms with `perspective` (drawn flat). Use a sharp duplicate inside an
  `overflow:hidden` box instead of glass, and 2D skew plus offset instead of
  depth.
- **Heavier in WebKit:** `box-shadow`, and a spread-only ring is drawn
  offset once the element is scaled. Keep shadows to one soft layer and draw
  rings with `border`.
- **Round-capped dashes** parked exactly on a path end leave a dot. Keep a
  travelling dash fully off both ends at rest (see recipe 15).

## Banned moves

- A crossfade from one picture to another, or a hard cut inside a graphic.
- Everything moving at once; a hold where nothing moves.
- Bouncy easing: `elastic`, `bounce`, `back.out` above 1.6, wobble on UI
  chrome or text.
- Particles, glows, lens flares, gradients on UI chrome, mixed icon stroke
  weights, emoji as icons.
- `backdrop-filter`, 3D perspective, `<canvas>`, CSS `@keyframes` or
  `transition`, `will-change` on anything that scales (text blurs).
- `onUpdate` drawing. Animate real elements; the editor builds its layer
  list from tween targets. The only exception is a number counting up.
- Invented numbers without an "example" label.
- A centred title on a flat background as the whole graphic.

## Recipes

Full recipes in the four reference files named at the top; the number tells you which.

| # | recipe | what happens | use it to |
| --- | --- | --- | --- |
| 01 | button to player | a pill grows into a video player; play morphs to pause | add a "watch the demo" moment |
| 02 | search to results | a search pill widens, a query types, results rise one by one | show what people find |
| 03 | card to workspace | a small card opens from its corner into a full editor window | announce a feature |
| 04 | tabs to panels | a selection pill stretches to the next tab, rows swap | compare plans or channels |
| 05 | chart morph | bars slim to hairlines and a line draws through their tops | land a weekly result |
| 06 | dashboard zoom | brackets snap round one tile, it grows out of the dimmed board | pull out the one KPI |
| 07 | spring stack | a stack of cards fans out on springs, the top one overshoots | show three offers in turn |
| 08 | magnetic dock | icons swell as a cursor passes, a label rides the biggest | show tools or integrations |
| 09 | masked type | a fill rises through an outlined word, a second line slides up | reveal a name or headline |
| 10 | elastic type | letters stretch from the middle out on a locked baseline | open an event or episode |
| 11 | text to layout | one line of text splits and travels into an article page | turn a headline into a cover |
| 12 | shutter reveal | vertical slats narrow one by one to uncover an image | tease a drop or a photo |
| 13 | layer spread | flat layers slide apart on a 2D diagonal, back layer first | explain the layers of a stack |
| 14 | lens focus | a lens slides down a soft report and only its row is sharp | make one line get read |
| 15 | flowing paths | pulses run along curves from one source to channels to a result | show one thing feeding many |
| 16 | tile assemble | seeded tiles fly in and lock into a mark on a grid | close on a logo |
| 17 | morphing shape | one shape goes pill, card, control; contents blur-swap | carry a whole story in one object |
| 18 | slider drag | the cursor drags a knob, a result builds as it passes thresholds | show effort, level or scale |
| 19 | toggle and toast | a switch flips, a toast slides in with a check | show a setting taking effect |
| 20 | terminal typing | a command types, output lines stream in, a status pill turns | show a CLI or an agent run |

**Changed from the source sets, and why:**

- *Perspective shift* became **layer spread**: 3D perspective is drawn flat
  in WebKit, so depth is a 2D diagonal offset with a light skew.
- *Glass focus* became **lens focus**: `backdrop-filter` does nothing in
  WebKit, so the lens holds a sharp duplicate of the rows; the tinted
  chromatic copies are dropped.
- *Particle logo* became **tile assemble**: no particles and no trails. A
  few dozen DOM tiles from a seeded scatter lock onto the mark's grid.
- Motion blur from sub-frame rendering, sound cues and talking-head cutaway
  timing are not part of this renderer and are dropped.

## Building one

1. Pick the recipe (or two that combine: a cursor plus a morph, a search plus
   a stagger). Get the real words and numbers from the user, or label them
   as examples.
2. Set the clock from `D`, place marks and pins, write the start state in
   CSS, then the move, the hold detail and the finish.
3. Check by reading: every tween ends by `D`, nothing overlaps on the same
   property, the hold has a live detail, two different stills would read in
   the hold, and the layout works in portrait.
4. Do not reuse an example's layout, palette or copy; they show the
   mechanics only.
