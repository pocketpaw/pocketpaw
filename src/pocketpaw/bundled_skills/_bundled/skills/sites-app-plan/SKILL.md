---
name: sites-app-plan
description: |
  Plan an APP or GAME on a Paw Site before building it, then build it in
  milestones you check by looking and playing. Invoke when the site is an app
  rather than a page: a game (2D, 3D, WebGL, three.js, React Three Fiber,
  canvas), a simulation, an editor, a tool, a calculator or configurator, a
  dashboard, or any multi-view app with real state. It makes you write PLAN.md
  (concept, core loop, a small complete v1, screens and states, controls, tech
  with versions from current docs, assets, milestones), confirm it with the
  user, then build every milestone back to back in one turn, with a build, a
  preview and a play-test after each. It also holds the 3D and game quality rules: assets,
  lighting, atmosphere, frame budget, start screen, code layout. Not for content
  pages (landing, portfolio, brochure): those skip planning.
---

# Sites: plan the app, then build it in milestones

An app or game written in one pass comes out big and half-built: a scene with
crude models, an unfinished world and no reason to keep playing. The fix is a
small plan the user agreed to, then milestones that each end in something that
works and that you have looked at.

This skill sits on top of `pocketpaw-site` (the template, the loop, the file
tools). The facts rule and the stored-data rule from your instructions still
apply unchanged: no invented business facts, and data other people must see
later (shared high scores, saved boards, accounts) goes in the site's database,
never localStorage.

## 1. Write PLAN.md before any UI or game file

Right after `start_site_from_template`, and before you write a component, a
scene, a page or a game file, write `PLAN.md` at the repo root with
`write_site_files`. Keep it to one screen. Sections:

1. **Concept.** One sentence: what it is and who it is for.
2. **Core loop** (games) or **core flow** (apps). What the player or user does
   every few seconds, the goal, the win / lose or "done" states, and why it is
   fun or useful. If you cannot say why it is fun in one line, the loop is not
   ready; fix that before anything else.
3. **v1 scope and later.** Cut hard. v1 is the smallest thing that is complete:
   one level done well beats five levels sketched. Everything else goes under
   "Later" as a list, not as stubs in the code.
4. **Screens and states.** Games: start, play, pause, game over / win, loading,
   error (WebGL missing, asset failed). Apps: empty, loading, populated, error,
   and each view.
5. **Controls.** Keyboard, mouse, touch and small screens. Name every binding.
   Say what a phone user does when there is no keyboard (on-screen controls, a
   tap scheme, or a clear "best on desktop" screen that still shows something).
6. **Tech.** The template and why, and each library with its version (for
   example `three`, or `@react-three/fiber` + `@react-three/drei`, a physics
   library such as `@react-three/rapier`, `postprocessing`). Take every version
   and API from the current docs via web search; never guess one from memory.
   Note the import paths the docs show, because they change between majors.
7. **Data model** (apps). The entities, where each lives (memory, URL, the
   site's database), and what is derived.
8. **Assets.** For each model, texture, sound and font: where it comes from
   (section 4), its licence, and the fallback if it fails to load.
9. **Milestones.** A checkbox list using the four milestones in section 3,
   each with two to five concrete items and a "done when" line.

## 2. Confirm the plan, then stop

Writing PLAN.md puts it in the chat as a PLAN.md card the user can open, so
never paste the plan into a message. Share a 3 to 5 line summary (concept, core
loop, what v1 leaves out, the milestones by name) through
`mcp__pocketpaw_ask__ask_user` with options such as "Build this plan", "Change
the scope", "You decide", and point at the card for the rest. Then STOP and
wait for the click. This is the one round of questions for the site, so put any
fact you still need into the same question.

Skip the wait only when the user already said to just build it, or to not ask.
Then say in one line that you wrote PLAN.md and are building M1.

On "Change the scope", update PLAN.md and ask once more. On "You decide",
build the plan as written.

## 3. Build every milestone back to back

Once the user approves the plan (or said to just build it), build every
milestone back to back in this one turn, M1 through M4, without waiting for
the user in between. A milestone boundary is not a reason to stop: finish a
milestone, tick it, and start the next one straight away. Never ask "shall I
continue?" and never end the turn with "M1 done" while milestones are still
unticked. Each milestone ends with the app building and running, never
half-wired.

- **M1 Core loop.** The loop or flow playable end to end with greybox visuals:
  boxes, capsules, flat colours, plain UI. Controls work, the goal can be
  reached, the lose / done state triggers, restart works. No art yet.
- **M2 World and assets.** The environment, real models and materials,
  lighting, the level or views from the plan.
- **M3 Feel.** Feedback on every action (sound, hit flash, camera shake,
  easing), audio, the difficulty curve, every UI state from the plan, the juice
  that makes it satisfying.
- **M4 Polish.** Performance, mobile and touch, accessibility (focus, contrast,
  reduced motion, readable HUD, pause on blur), loading and error states.

After EACH milestone:

1. `run_site_build`. On `failed`, read the log, fix, build again.
2. `preview_site` on desktop, then `device: mobile`. Pass `path` for another
   route, and `wait_ms` so a 3D or animated scene has time to render (if the
   tool rejects either parameter, call it without). Give the game a dev URL
   flag, for example `/?scene=play`, that skips the start screen, so the
   preview can show the in-game view and not only the title.
3. Look at it. A black canvas, a missing HUD, a camera inside a wall, text
   over the scene, a broken mobile layout: fix it and look again.
4. Play it with `play_test_site`: steps that start the game (click the start
   button or press its key), use each control from the plan (`press` with
   `hold_ms` to move, `mouse_move` to look, `click` to act), then reach a lose
   or win state, with a `screenshot` at each moment that matters. Add `wait`
   steps so the scene can react. Read the report: fix every page error, every
   failed request for an asset, a blank canvas, a step that could not find its
   button. Then play again with the same steps until the run is clean.
5. Tick the milestone's items in PLAN.md with `patch_site_file` as soon as it
   is done, so the PLAN.md card in chat shows where the build is.

Never call a milestone done without having seen it and played it. Never claim
gameplay works without a passing play-test. If `play_test_site` is
unavailable, say gameplay was not play-tested.

While you work, write one short progress line as each milestone is ticked
("M1 core loop works, play-tested; starting M2"). That is a line in your
running text, not a reply that ends the turn. When every milestone is ticked,
give one final report: what each milestone added, what the play-tests showed,
and what the user should try (the keys, the goal).

End the turn before the plan is finished only for a real blocker: a secret
the app needs, a decision only the user can make, a missing fact nobody
supplied, a plan the platform refuses, or a build still failing after the
loop's three rounds. Then say what you need, what is built so far, and which
milestone is next. If the run itself is stopped by a limit, the next turn
reads PLAN.md and carries on from the first unticked milestone.

## 4. 3D and game quality rules

**Models.** Never hand-type vertex data for anything more complex than a
simple shape. Either compose the model from primitives (box, cylinder,
sphere, capsule, extruded shapes) with real PBR materials (roughness,
metalness, colour that is not pure grey), or load a glTF / GLB model.
- Owner uploads first: `list_site_assets`.
- Then CC0 sources: Kenney (kenney.nl), Quaternius (quaternius.com), Poly Pizza
  (poly.pizza, which mixes CC0 and CC-BY: check each model), Poly Haven and
  ambientCG for textures and HDRIs.
- Open the asset's page and check the licence before you use it; record it in
  PLAN.md. Load only from a URL you fetched and confirmed serves the file. If
  you cannot confirm one, compose from primitives instead of guessing a path.
- Credit CC-BY assets in the game's credits screen.

**Atmosphere beats model detail.** Lighting, shadows, fog, colour grading and
post-processing (bloom, vignette, film grain) make a scene read as finished
far more than polygon count does. Set them in M2, not at the end.

**Horror** in particular is darkness and pacing: a low ambient light, a
flashlight cone the player controls (a spotlight on the camera), fog that
hides the far walls, sound design (a drone, footsteps, sudden silence),
tension that builds and releases, and threats heard before they are seen.
A dark room with good sound is scarier than a detailed monster in full light.

**Sound.** Short procedural sound with the Web Audio API (oscillators, noise,
filters, envelopes) needs no files. Audio files only from CC0 sources (Kenney
has audio packs), licence checked.

**Frame budget.** Aim for a steady frame rate on a mid-range phone.
- Cap the pixel ratio (`Math.min(devicePixelRatio, 2)`, lower on mobile).
- Instance repeated meshes (trees, crates, tiles) instead of cloning them.
- Few real-time shadow casters; bake or fake the rest.
- Dispose geometries, materials and textures when a scene or level unloads.
- Pause the loop when the tab is hidden.

**Start screen.** Audio and pointer lock need a user gesture, so the game
opens on a start screen with a clear "Click to start" (or tap). Explain the
controls there. Handle a refused pointer lock and a lost WebGL context
without a blank page.

**Code layout.** Keep the scene graph, the game state and the UI in separate
modules: a state store (score, health, phase), systems (input, physics,
spawning, audio), scene components, and HUD / menus. No single giant file. A
game where state lives in one place is one you can fix in M3 without breaking
M1.

## 5. Apps that are not games

A dashboard, editor, tool or multi-view app follows the same steps with its own
words: the core flow instead of the core loop, the data model and views
instead of scenes, empty / loading / error states instead of game over.
- **M1** the core flow working end to end on the real data model, plain
  styling.
- **M2** every view and the real design from the art direction.
- **M3** states, feedback, validation, keyboard shortcuts.
- **M4** polish, performance, mobile, accessibility.

Charts, tables and figures show the user's data or clear placeholders, never
invented numbers presented as real.

## 6. Ready means it works

The owner hears "ready" once. Before you say it:

- **Every form saves.** Each form POSTs to a real handler: a server route or
  action that writes the entry, `data-paw-subscribe` for a mailing list, or a
  submit script that sends it. Never a bare `<form>` that reloads the page (a
  GET puts the entry in the address bar and saves nothing). After success the
  page says so ("Thanks, you're on the list"). Then run `play_test_site` with
  `exercise_forms: true` on every page with a form and fix each FAILED form
  until the run is clean. Test entries stay in the draft. If forms cannot be
  exercised there, say they were checked but not tried end to end.
- **No invented contact details.** Address, phone, WhatsApp, email and opening
  hours come from the owner. Ask for them in the plan question. If they are
  still missing, leave a marked placeholder (`[Address]`) and tell the owner
  plainly that publishing waits for it. Never make up a number: 9876543210
  belongs to someone.
- **Statuses move forward.** An order or booking flow has one direction (New,
  Packed, Delivered; or Booked, Attended). The primary button is the next
  state only; never offer "Mark packed" on a delivered order. Going back is a
  separate, quieter action (Undo, Reopen) if the owner needs it.
- **The owner's admin page is reachable** from a footer link and named in the
  final report, with what it shows.

Sharing, when the owner asks: the live address is the link. They can rename it
in Settings › General › Address › Change; the new name takes over at the next
publish and the old link stops working then, so tell them to share the new one.
The Share button copies the link. There is no QR maker for the site link (the
Discover WhatsApp QR card opens a WhatsApp chat, not the site): never point to
one or to an outside link shortener. Check the product before you name any
feature; if it does not exist, say so.

## Checklist

```
[ ] PLAN.md written before any UI or game file
[ ] Versions and APIs taken from current docs
[ ] 3 to 5 line plan summary sent with ask_user (never the whole plan), then stopped (unless "just build it")
[ ] M1 built, previewed desktop + mobile, play-tested clean, ticked
[ ] M2 built, previewed, play-tested clean, ticked
[ ] M3 built, previewed, play-tested clean, ticked
[ ] M4 built, previewed, play-tested clean, ticked
[ ] Every form exercised with play_test_site (exercise_forms) and passing
[ ] No invented address, phone, WhatsApp, email or hours; missing ones listed
[ ] Status buttons offer only the next state
[ ] No stop between milestones; short progress lines while building
[ ] One final report: what was checked and played, and what the user should try
```
