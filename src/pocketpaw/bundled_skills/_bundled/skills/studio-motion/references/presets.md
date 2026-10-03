# Studio Motion presets

One complete, renderable composition per `data-preset`. Each follows the
house contract in the `studio-motion` SKILL.md: the same head, root, clip and
timeline registration, with only the content and the script body changing.
They are written at 1920x1080; set `data-width` / `data-height` to the
project's frame size and the fluid layout does the rest.

## Contents

- [title](#title): eyebrow, headline (rise), accent rule, subtitle. swiss-pulse.
- [statement](#statement): three lines landing word by word, last in accent. velvet.
- [logo](#logo): mark settles, name masks up, tagline. data-drift.
- [stat](#stat): a number counting up with a label. maximalist.
- [chart](#chart): staggered horizontal bars with counting values. soft-signal.

Each style above is only the example's choice. Any preset takes any style:
swap the root variables, `data-style`, the font link and `EASE`.

## title

Edit the four text elements freely. To change the headline's effect, replace
its one tween with another from the SKILL.md's text effects.

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter+Tight:wght@800&family=Inter:wght@400;600&display=swap">
<style>
  html, body { margin: 0; height: 100%; background: #000; }
  #root { position: relative; width: 100%; height: 100%; overflow: hidden;
    background: var(--mg-bg); color: var(--mg-fg); font-family: var(--mg-font-body), sans-serif; }
  .clip { position: absolute; inset: 0; box-sizing: border-box; padding: 8vmin;
    display: flex; flex-direction: column; justify-content: center; }
  .w, .ch { display: inline-block; }
  .w { white-space: nowrap; }
  .stage { display: flex; flex-direction: column; align-items: flex-start; gap: 3vmin; max-width: 90%; }
  .eyebrow { margin: 0; font-size: clamp(12px, 2.4vmin, 40px); font-weight: 600;
    letter-spacing: 0.2em; text-transform: uppercase; color: var(--mg-muted); }
  .headline { margin: 0; font-family: var(--mg-font-display), sans-serif;
    font-weight: var(--mg-display-weight); font-size: clamp(36px, 12vmin, 240px);
    line-height: 0.95; letter-spacing: -0.02em; }
  .rule { width: 18%; height: 1vmin; background: var(--mg-accent); transform-origin: left center; }
  .sub { margin: 0; max-width: 70%; font-size: clamp(14px, 3.6vmin, 64px); color: var(--mg-muted); }
</style>
</head>
<body>
<div id="root" data-composition-id="mg" data-start="0" data-width="1920" data-height="1080"
  data-duration="4" data-preset="title" data-style="swiss-pulse"
  style="--mg-bg:#f4f4f0;--mg-fg:#111111;--mg-accent:#e63312;--mg-muted:#6b6b6b;--mg-font-display:'Inter Tight';--mg-font-body:'Inter';--mg-display-weight:800">
  <section class="clip" data-start="0" data-duration="4">
    <div class="stage">
      <p class="eyebrow">Product update</p>
      <h1 class="headline">Ship it faster</h1>
      <div class="rule"></div>
      <p class="sub">Everything new in the October release</p>
    </div>
  </section>
</div>
<script>
  const EASE = "power3.out";
  function split(el) {
    const words = [], chars = [];
    const list = el.textContent.trim().split(/\s+/);
    el.textContent = "";
    list.forEach((word, i) => {
      const w = el.appendChild(document.createElement("span"));
      w.className = "w";
      for (const c of word) {
        const ch = w.appendChild(document.createElement("span"));
        ch.className = "ch";
        ch.textContent = c;
        chars.push(ch);
      }
      words.push(w);
      if (i < list.length - 1) el.appendChild(document.createTextNode(" "));
    });
    return { words, chars };
  }
  const D = Number(document.getElementById("root").dataset.duration);
  const tl = gsap.timeline({ paused: true });
  tl.from(".eyebrow", { yPercent: 60, opacity: 0, duration: 0.5, ease: EASE }, 0.2);
  tl.from(split(document.querySelector(".headline")).chars,
    { yPercent: 100, opacity: 0, duration: 0.6, stagger: 0.03, ease: EASE }, 0.35);
  tl.from(".rule", { scaleX: 0, duration: 0.7, ease: EASE }, 0.9);
  tl.from(".sub", { yPercent: 40, opacity: 0, duration: 0.6, ease: EASE }, 1.1);
  tl.to(".stage", { opacity: 0, duration: 0.4, ease: "power1.in" }, D - 0.4);
  window.__timelines["mg"] = tl;
</script>
</body>
</html>
```

## statement

Two to four `.line` elements. Each lands after the one before has finished,
word by word, and the last line takes the accent through CSS. Add or remove
lines and the timing follows; if the words run past the duration, raise
`data-duration` on the root and the clip.

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@600&family=Inter:wght@400;600&display=swap">
<style>
  html, body { margin: 0; height: 100%; background: #000; }
  #root { position: relative; width: 100%; height: 100%; overflow: hidden;
    background: var(--mg-bg); color: var(--mg-fg); font-family: var(--mg-font-body), sans-serif; }
  .clip { position: absolute; inset: 0; box-sizing: border-box; padding: 8vmin;
    display: flex; flex-direction: column; justify-content: center; align-items: center; }
  .w, .ch { display: inline-block; }
  .w { white-space: nowrap; }
  .stage { display: flex; flex-direction: column; align-items: center; gap: 2.5vmin;
    max-width: 92%; text-align: center; }
  .line { margin: 0; font-family: var(--mg-font-display), sans-serif;
    font-weight: var(--mg-display-weight); font-size: clamp(28px, 8vmin, 160px); line-height: 1.05; }
  .line:last-child { color: var(--mg-accent); }
</style>
</head>
<body>
<div id="root" data-composition-id="mg" data-start="0" data-width="1920" data-height="1080"
  data-duration="6" data-preset="statement" data-style="velvet"
  style="--mg-bg:#14110f;--mg-fg:#f3ece2;--mg-accent:#c9a86a;--mg-muted:#8c8378;--mg-font-display:'Playfair Display';--mg-font-body:'Inter';--mg-display-weight:600">
  <section class="clip" data-start="0" data-duration="6">
    <div class="stage">
      <p class="line">Good work takes time.</p>
      <p class="line">Great work takes care.</p>
      <p class="line">We give it both.</p>
    </div>
  </section>
</div>
<script>
  const EASE = "power2.inOut";
  function split(el) {
    const words = [], chars = [];
    const list = el.textContent.trim().split(/\s+/);
    el.textContent = "";
    list.forEach((word, i) => {
      const w = el.appendChild(document.createElement("span"));
      w.className = "w";
      for (const c of word) {
        const ch = w.appendChild(document.createElement("span"));
        ch.className = "ch";
        ch.textContent = c;
        chars.push(ch);
      }
      words.push(w);
      if (i < list.length - 1) el.appendChild(document.createTextNode(" "));
    });
    return { words, chars };
  }
  const D = Number(document.getElementById("root").dataset.duration);
  const tl = gsap.timeline({ paused: true });
  let at = 0.3;
  document.querySelectorAll(".line").forEach((line) => {
    const { words } = split(line);
    tl.from(words, { yPercent: 40, opacity: 0, duration: 0.5, stagger: 0.09, ease: EASE }, at);
    at += 0.5 + words.length * 0.09 + 0.35;
  });
  tl.to(".stage", { opacity: 0, duration: 0.4, ease: "power1.in" }, D - 0.4);
  window.__timelines["mg"] = tl;
</script>
</body>
</html>
```

## logo

The mark here is an inline SVG placeholder; its colours come from CSS so a
restyle carries through. With a real logo, replace the `<svg>` with
`<img class="mark" src="https://…" alt="">`. With no logo at all, delete the
mark: the script checks for it and the name opens the graphic instead.

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;700&display=swap">
<style>
  html, body { margin: 0; height: 100%; background: #000; }
  #root { position: relative; width: 100%; height: 100%; overflow: hidden;
    background: var(--mg-bg); color: var(--mg-fg); font-family: var(--mg-font-body), sans-serif; }
  .clip { position: absolute; inset: 0; box-sizing: border-box; padding: 8vmin;
    display: flex; flex-direction: column; justify-content: center; align-items: center; }
  .stage { display: flex; flex-direction: column; align-items: center; gap: 3vmin; text-align: center; }
  .mark { display: block; width: clamp(48px, 18vmin, 320px); height: clamp(48px, 18vmin, 320px); }
  .mark .plate { fill: var(--mg-accent); }
  .mark .hole { fill: var(--mg-bg); }
  .mask { overflow: hidden; }
  .name { font-family: var(--mg-font-display), sans-serif; font-weight: var(--mg-display-weight);
    font-size: clamp(32px, 11vmin, 200px); line-height: 1.05; letter-spacing: -0.01em; }
  .tagline { margin: 0; font-size: clamp(14px, 3.4vmin, 60px); color: var(--mg-muted); }
</style>
</head>
<body>
<div id="root" data-composition-id="mg" data-start="0" data-width="1920" data-height="1080"
  data-duration="4" data-preset="logo" data-style="data-drift"
  style="--mg-bg:#05060f;--mg-fg:#e8f0ff;--mg-accent:#00e5ff;--mg-muted:#6c7a99;--mg-font-display:'Space Grotesk';--mg-font-body:'Space Grotesk';--mg-display-weight:700">
  <section class="clip" data-start="0" data-duration="4">
    <div class="stage">
      <svg class="mark" viewBox="0 0 100 100" aria-hidden="true">
        <rect class="plate" x="8" y="8" width="84" height="84" rx="24"></rect>
        <circle class="hole" cx="50" cy="50" r="18"></circle>
      </svg>
      <div class="mask"><div class="name">Northwind</div></div>
      <p class="tagline">Data that moves with you</p>
    </div>
  </section>
</div>
<script>
  const EASE = "expo.out";
  const D = Number(document.getElementById("root").dataset.duration);
  const tl = gsap.timeline({ paused: true });
  const mark = document.querySelector(".mark");
  const start = mark ? 0.7 : 0.2;
  if (mark) {
    tl.from(mark, { scale: 0.4, rotation: -12, opacity: 0, duration: 0.9, ease: "back.out(1.8)" }, 0.2);
  }
  tl.from(".name", { yPercent: 110, duration: 0.8, ease: EASE }, start);
  tl.from(".tagline", { yPercent: 40, opacity: 0, duration: 0.6, ease: EASE }, start + 0.5);
  tl.to(".stage", { opacity: 0, duration: 0.4, ease: "power1.in" }, D - 0.4);
  window.__timelines["mg"] = tl;
</script>
</body>
</html>
```

## stat

The number lives in `data-value`, with `data-decimals` for its precision;
prefix and suffix are their own spans. The count runs on `power2.out` rather
than `EASE` so a springy style never overshoots the real figure.

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Anton&family=Inter:wght@400;600&display=swap">
<style>
  html, body { margin: 0; height: 100%; background: #000; }
  #root { position: relative; width: 100%; height: 100%; overflow: hidden;
    background: var(--mg-bg); color: var(--mg-fg); font-family: var(--mg-font-body), sans-serif; }
  .clip { position: absolute; inset: 0; box-sizing: border-box; padding: 8vmin;
    display: flex; flex-direction: column; justify-content: center; align-items: center; }
  .stage { display: flex; flex-direction: column; align-items: center; gap: 3vmin; text-align: center; }
  .figure { display: flex; align-items: baseline; font-family: var(--mg-font-display), sans-serif;
    font-weight: var(--mg-display-weight); font-size: clamp(48px, 24vmin, 420px); line-height: 1;
    font-variant-numeric: tabular-nums; }
  .affix { color: var(--mg-accent); }
  .bar { width: 30%; height: 1.2vmin; background: var(--mg-accent); transform-origin: left center; }
  .label { margin: 0; max-width: 80%; font-size: clamp(14px, 4vmin, 72px); font-weight: 600; color: var(--mg-muted); }
</style>
</head>
<body>
<div id="root" data-composition-id="mg" data-start="0" data-width="1920" data-height="1080"
  data-duration="4" data-preset="stat" data-style="maximalist"
  style="--mg-bg:#ffde00;--mg-fg:#0a0a0a;--mg-accent:#ff2d55;--mg-muted:#333333;--mg-font-display:'Anton';--mg-font-body:'Inter';--mg-display-weight:400">
  <section class="clip" data-start="0" data-duration="4">
    <div class="stage">
      <div class="figure"><span class="affix">$</span><span class="num" data-value="2.4" data-decimals="1">0</span><span class="affix">M</span></div>
      <div class="bar"></div>
      <p class="label">raised in our first week</p>
    </div>
  </section>
</div>
<script>
  const EASE = "back.out(1.6)";
  const D = Number(document.getElementById("root").dataset.duration);
  const tl = gsap.timeline({ paused: true });
  const num = document.querySelector(".num");
  const value = Number(num.dataset.value);
  const decimals = Number(num.dataset.decimals || 0);
  const fmt = (v) => v.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
  const counter = { v: 0 };
  num.textContent = fmt(0);
  tl.from(".figure", { scale: 0.6, opacity: 0, duration: 0.6, ease: EASE }, 0.2);
  tl.to(counter, { v: value, duration: 1.6, ease: "power2.out",
    onUpdate: () => { num.textContent = fmt(counter.v); } }, 0.3);
  tl.from(".bar", { scaleX: 0, duration: 0.8, ease: EASE }, 0.9);
  tl.from(".label", { yPercent: 50, opacity: 0, duration: 0.6, ease: EASE }, 1.2);
  tl.to(".stage", { opacity: 0, duration: 0.4, ease: "power1.in" }, D - 0.4);
  window.__timelines["mg"] = tl;
</script>
</body>
</html>
```

## chart

Each `.row` carries its value in `data-value`; `data-unit` on `.rows` is the
suffix every value shares. The script sizes each bar against the largest and
gives that one the accent, so editing the data is editing attributes. Three
to six rows read well.

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:wght@600&family=DM+Sans:wght@400;600&display=swap">
<style>
  html, body { margin: 0; height: 100%; background: #000; }
  #root { position: relative; width: 100%; height: 100%; overflow: hidden;
    background: var(--mg-bg); color: var(--mg-fg); font-family: var(--mg-font-body), sans-serif; }
  .clip { position: absolute; inset: 0; box-sizing: border-box; padding: 8vmin;
    display: flex; flex-direction: column; justify-content: center; }
  .stage { display: flex; flex-direction: column; gap: 5vmin; width: 100%; }
  .title { margin: 0; font-family: var(--mg-font-display), sans-serif; font-weight: var(--mg-display-weight);
    font-size: clamp(24px, 7vmin, 120px); line-height: 1.05; }
  .rows { display: flex; flex-direction: column; gap: 2.5vmin; }
  .row { display: grid; grid-template-columns: 24% 1fr 14%; align-items: center; gap: 2vmin; }
  .label { font-size: clamp(12px, 3.2vmin, 56px); font-weight: 600; color: var(--mg-muted); }
  .track { height: 4.5vmin; }
  .bar { height: 100%; background: var(--mg-muted); border-radius: 0.6vmin; transform-origin: left center; }
  .row.top .bar { background: var(--mg-accent); }
  .val { text-align: right; font-family: var(--mg-font-display), sans-serif;
    font-weight: var(--mg-display-weight); font-size: clamp(14px, 4vmin, 72px); font-variant-numeric: tabular-nums; }
</style>
</head>
<body>
<div id="root" data-composition-id="mg" data-start="0" data-width="1920" data-height="1080"
  data-duration="6" data-preset="chart" data-style="soft-signal"
  style="--mg-bg:#fbf3ec;--mg-fg:#2b2420;--mg-accent:#e07a5f;--mg-muted:#8a7d74;--mg-font-display:'Fraunces';--mg-font-body:'DM Sans';--mg-display-weight:600">
  <section class="clip" data-start="0" data-duration="6">
    <div class="stage">
      <p class="title">Where new readers find us</p>
      <div class="rows" data-unit="%">
        <div class="row" data-value="46"><span class="label">Search</span><div class="track"><div class="bar"></div></div><span class="val">0</span></div>
        <div class="row" data-value="27"><span class="label">Social</span><div class="track"><div class="bar"></div></div><span class="val">0</span></div>
        <div class="row" data-value="18"><span class="label">Newsletter</span><div class="track"><div class="bar"></div></div><span class="val">0</span></div>
        <div class="row" data-value="9"><span class="label">Referral</span><div class="track"><div class="bar"></div></div><span class="val">0</span></div>
      </div>
    </div>
  </section>
</div>
<script>
  const EASE = "sine.out";
  const D = Number(document.getElementById("root").dataset.duration);
  const tl = gsap.timeline({ paused: true });
  const rows = [...document.querySelectorAll(".row")];
  const unit = document.querySelector(".rows").dataset.unit || "";
  const max = Math.max(...rows.map((row) => Number(row.dataset.value)));
  tl.from(".title", { yPercent: 50, opacity: 0, duration: 0.6, ease: EASE }, 0.2);
  rows.forEach((row, i) => {
    const value = Number(row.dataset.value);
    const bar = row.querySelector(".bar");
    const val = row.querySelector(".val");
    const counter = { v: 0 };
    const at = 0.6 + i * 0.18;
    bar.style.width = (value / max) * 100 + "%";
    row.classList.toggle("top", value === max);
    val.textContent = 0 + unit;
    tl.from(row, { opacity: 0, duration: 0.4, ease: EASE }, at - 0.1);
    tl.from(bar, { scaleX: 0, duration: 1, ease: EASE }, at);
    tl.to(counter, { v: value, duration: 1, ease: "power2.out",
      onUpdate: () => { val.textContent = Math.round(counter.v) + unit; } }, at);
  });
  tl.to(".stage", { opacity: 0, duration: 0.4, ease: "power1.in" }, D - 0.4);
  window.__timelines["mg"] = tl;
</script>
</body>
</html>
```
