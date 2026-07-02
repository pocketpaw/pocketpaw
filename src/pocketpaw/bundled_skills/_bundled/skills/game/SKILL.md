---
name: game
description: |
  Compose a living game world from a text description of its vibe. Invoke
  when the user describes a world they want to exist: "a cozy cliffside
  tea town", "a rain-slick noir city where everyone lies", "make me a
  game about...", or any describe-to-world request (especially on the
  /game surface). This is a CREATION surface, not a play surface — you do
  NOT build a dashboard or a ui-spec and you do NOT use the pocket
  specialist. You parse the vibe into seven feel dials, sketch a small
  cast of Souls and a few zones, then call one deterministic tool
  (create_game_world) that persists the world as a Pocket type="game".
  NPCs are Souls: they carry persistent memory, relationships, and
  grudges, so the world remembers. Loading this skill keeps the chat
  agent's always-on system prompt small while still delivering the full
  vibe→world flow when creation is actually requested.
---

# Game — the creation-first world brain

You're on **Game**: the user describes a living world and you **compose**
it. The deliverable is a **Pocket of `type="game"`** — a world spec with a
small cast of Souls, a few zones, and seven feel dials — persisted by one
deterministic tool. This is **not** a dashboard and **not** a play
session: you do **not** compose a rippleSpec, you do **not** hand-build
widgets, and you do **not** call the pocket specialist. You shape the
mind of the world; **code** persists it.

NPCs are **Souls**. Each carries persistent memory, relationships, and
grudges — what happens in the world stays with them. Creation-first: your
job ends when the world exists and you've described it back.

## The SEVEN dials (0.0–1.0 each)

Every world gets seven feel dials, set from the vibe:

- **Challenge** — how hard the world pushes back: friction, stakes, difficulty.
- **Progress** — how visibly effort compounds: growth, unlocks, mastery.
- **Choice** — how much the player steers: autonomy, forks that matter.
- **Bonds** — how deep relationships with the Souls run and how much they remember.
- **Mark** — how permanently the player's actions change the world.
- **Pulse** — the world's tempo and pressure: pacing, urgency, events firing.
- **Spark** — novelty and surprise: secrets, curiosity, the unexpected.

**JUICE is platform-provided** — the feedback/feel layer (sound, motion,
celebration) is not a dial you set; the platform supplies it.

## v0 flow

1. **Parse the vibe.** Read the user's description and name its dominant
   mood in one word if you can: cozy, tense, mystery, or sandbox.
2. **Pick a dial PRESET** from the table below. Unknown or mixed vibe →
   the balanced default. Only override individual dials when the user
   explicitly asks (e.g. "brutal difficulty" → challenge 0.9).

   | Vibe        | challenge | progress | choice | bonds | mark | pulse | spark |
   |-------------|-----------|----------|--------|-------|------|-------|-------|
   | **cozy**    | 0.2       | 0.5      | 0.6    | 0.9   | 0.7  | 0.3   | 0.5   |
   | **tense**   | 0.8       | 0.6      | 0.5    | 0.4   | 0.5  | 0.9   | 0.4   |
   | **mystery** | 0.6       | 0.7      | 0.7    | 0.5   | 0.4  | 0.5   | 0.9   |
   | **sandbox** | 0.4       | 0.4      | 0.9    | 0.5   | 0.8  | 0.3   | 0.7   |
   | *(default)* | 0.5       | 0.5      | 0.5    | 0.5   | 0.5  | 0.5   | 0.5   |

3. **Sketch the world spec.** A small cast (see the foreground-cast rule),
   3–5 zones, and the vibe sentence. You may omit `dials` entirely — the
   tool fills them from the same preset table by matching the vibe.
4. **Call the tool** (see below) and **report the world**: its name, who
   lives there, where, and its mood. Never narrate a pocket or a dashboard.

## The foreground-cast rule: 3–6 Souls

Keep the cast **few — 3 to 6 Souls**. Every Soul carries live memory and
relationships, and a large cast dilutes them into extras nobody remembers.
A small foreground cast stays vivid: each Soul gets a name, an archetype,
a one-line persona, and an OCEAN sketch. Background crowds are scenery —
don't give them Souls.

## Calling the tool

```
mcp__pocketpaw_game__create_game_world(
  name = "Saltwind Terrace",
  vibe = "a cozy cliffside tea town where the fog gossips",
  world_spec = {
    "cast": [
      {"name": "Mirren", "archetype": "keeper", "persona": "runs the tea house, forgets nothing",
       "ocean": {"openness": 0.7, "conscientiousness": 0.8, "extraversion": 0.4,
                 "agreeableness": 0.9, "neuroticism": 0.2}},
      {"name": "Osk",   "archetype": "rival",  "persona": "undercuts everyone, secretly lonely", "ocean": {}},
      {"name": "Petal", "archetype": "wanderer", "persona": "arrives with the fog, leaves with secrets", "ocean": {}}
    ],
    "zones": ["the tea house", "the cliff stairs", "the fog market"],
    "dials": {"challenge": 0.2, "progress": 0.5, "choice": 0.6, "bonds": 0.9,
              "mark": 0.7, "pulse": 0.3, "spark": 0.5},
    "vibe": "a cozy cliffside tea town where the fog gossips"
  }
)
```

The tool validates the spec, fills missing dials from the vibe preset,
persists the pocket stamped `type="game"` + `pattern="living-world"`, and
returns `{ok, pocket_id, pocket}`. The canvas opens automatically — you
don't create or merge a pocket yourself.

## Relay the result (or the error)

- On success: briefly describe the world that now exists — cast, zones,
  mood (e.g. "Saltwind Terrace is alive — Mirren keeps the tea house,
  Osk schemes, Petal drifts in with the fog").
- On `ok: false`: **relay the error message plainly** (an invalid
  world_spec, or a plan that doesn't include /game) and do **not** claim
  a phantom world. Fix the spec and retry when the error names the
  problem; don't invent a success.
