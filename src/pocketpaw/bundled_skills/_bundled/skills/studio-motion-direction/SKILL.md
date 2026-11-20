---
name: studio-motion-direction
description: |
  Picks the look before you write a motion graphic on /studio/editor: six
  distinct worlds (calm product demo, one continuous take, beat-cut, editorial
  collage, launch beats, data morph), how to choose between them, and what must
  change every time so two graphics never come out alike. Load it FIRST for any
  new motion graphic that is more than a plain house title, statement, logo,
  stat or chart card: an intro, explainer, launch teaser, reel opener, feature
  demo, milestone. Then load `studio-motion-effects` for what happens on screen
  and `studio-motion` + `hyperframes-core` for the render contract.
---

# Studio Motion Direction

`studio-motion` is the render contract and the five house presets. This skill
decides what a graphic looks like before any HTML exists. Skipping it is why
every graphic used to come out as a centred title on a flat background.

## When the house presets are enough

Use a `studio-motion` preset, in the timeline's Graphic style, when the user
asks for one plain card (a title, a two-line statement, a logo, one number,
one bar chart), picked a preset from the Graphics gallery, or is editing a
graphic that carries `data-preset`. Everything else starts here.

## Step 1: read the request

Settle five things, in a sentence each, before choosing:

- **Subject.** What is being shown: a product, a number, an idea, an event.
- **The one idea.** The single thing a viewer must leave with. If you cannot
  say it in eight words, ask the user.
- **Pace.** Calm and readable, or fast and cut to a beat.
- **Frame.** The project's size from the timeline block. Vertical frames get
  the safe zone below.
- **Facts.** Which numbers and names are real (from the user or their media).
  Never invent a figure. A number you were not given is labelled
  "illustrative" on screen, or left out.

When the user points at a reference ("like this", a link, a clip), break it
into five lines first: pace, what changes on screen, type, colour, the one
idea. Match those, not its surface.

## Step 2: weigh, then pick a world

Name two worlds that could carry the request and one reason against each,
then pick. Tell the user which world you chose in one short line, so they can
redirect you.

| world | feel | good for | read |
| --- | --- | --- | --- |
| `calm-demo` | airy, slow push-ins, the product filmed up close, highlighter on the key word | product walkthroughs, onboarding, feature explainers | [references/world-calm-demo.md](references/world-calm-demo.md) |
| `one-take` | one continuous shot, a camera gliding across a stage of UI stations, a hero object that travels | how something works end to end, a flow or pipeline, playful explainers | [references/world-one-take.md](references/world-one-take.md) |
| `beat-cut` | 128 BPM, hard cuts on the beat, a shape that floods the frame between scenes | hype openers, short social hooks, launch countdowns | [references/world-beat-cut.md](references/world-beat-cut.md) |
| `editorial-collage` | paper, halftone, condensed type, animated on twos, sourced facts | stories, news, opinion, history, "why this matters" | [references/world-editorial-collage.md](references/world-editorial-collage.md) |
| `launch-beats` | one sentence split across beats, glass cards, typing in the product's own input | launches, announcements, feature drops | [references/world-launch-beats.md](references/world-launch-beats.md) |
| `data-morph` | one chart that keeps transforming, nothing fades and gets replaced | metrics, results, comparisons, milestones | [references/world-data-morph.md](references/world-data-morph.md) |

**Variety across a timeline.** The MOTION GRAPHICS block shows each earlier
graphic's source; its root carries `data-world`. For a new standalone piece,
prefer a world this timeline has not used. When the user wants a matching
series (chapter cards, a recurring stat), keep the world and change
everything on its "must change" list.

**Same request twice.** If the user asks again for the same thing ("another
one", "try again"), keep the world only if they liked it, and change at least
three of: layout, hero device, palette, entry move, end frame.

## Step 3: decide before you write

Open the world's file and read its rules and its "Must change between uses"
list. Then write down, in your head or to the user, five choices:

1. **Layout**: where the eye starts and how the frame is divided.
2. **Hero device**: the one object or move the piece is remembered by.
3. **Palette**: from the world's guidance or the user's brand. The six house
   palettes in `studio-motion` belong to the house presets; do not reach for
   them here unless the user asks.
4. **Entry move**: how the first thing arrives.
5. **End frame**: what is on screen in the last second (a hold, a resolve, an
   exit), never a cut to black mid-motion.

Each world file ends with one full example. It is one instance. Do not reuse
its layout, copy or palette; your five choices must differ from it.

## Craft rules for every world

- **Type.** At most two families through one Google Fonts link. One idea per
  screen; no paragraph is ever on screen.
- **Timing.** Give each beat time to be read: about 0.3 s per word for text
  that must be read, never under 1.2 s for a line. Hold the key frame.
- **Vertical frames (9:16).** Keep text between 19% and 74% of the height and
  clear of the right 11%, where platform buttons sit.
- **Size.** Aim for about 12 KB of HTML. Every graphic on the timeline shares
  one 100 KB budget in your context, and a bloated one can no longer be edited
  in place.
- **Root.** Set `data-world="<id>"` on the root next to the other `data-*`
  attributes, and keep the seven `--mg-*` variables so a restyle can swap the
  palette later.

## Before you call add_motion_graphic

- [ ] The world is named to the user, with a short reason.
- [ ] Layout, hero device, palette, entry move and end frame are your own,
      not the example's.
- [ ] Every number is real or labelled illustrative.
- [ ] The contract in `studio-motion` holds: one root with every `data-*`,
      one clip, one paused timeline on `window.__timelines["mg"]`, no canvas,
      no audio, absolute URLs only, deterministic.
- [ ] You tell the user it is rendering, not that it is done.
