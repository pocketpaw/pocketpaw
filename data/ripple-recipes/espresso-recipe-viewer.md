---
title: Recipe / How-To Viewer
pattern: viewer
domain: knowledge-reference
keywords: [recipe, how-to, guide, instructions, reference, notes, walkthrough, runbook, glossary]
focal_widgets: [page-header, text, kv-table]
source: hand-authored canonical viewer example (espresso 101)
---

# When to use

A "build me a viewer for X" / "create a recipe / how-to for Y" /
"make me a runbook" brief. Read-only content with a title, lead
paragraph summarising the thing, a key-value table of stable facts
(measurements, parameters, defaults), and a body paragraph with
guidance.

This is the canonical alternative to "everything is a dashboard."
A pocket that's just a single fact + paragraph belongs here, NOT
in a hero+grid layout with KPI tiles.

# Composition

```json
{
  "version": "1.0",
  "ui": {
    "type": "flex",
    "props": { "direction": "column", "gap": "16px" },
    "children": [
      {
        "type": "page-header",
        "props": {
          "title": "Espresso 101",
          "subtitle": "Notes from my favorite barista"
        }
      },
      {
        "type": "text",
        "props": {
          "content": "A double shot is 14 g of finely ground coffee extracted with 36 g of water at 93 °C in 25-30 seconds. Pull too fast → tighten the grind. Pull too slow → loosen it.",
          "variant": "lead"
        }
      },
      {
        "type": "kv-table",
        "props": {
          "items": [
            { "k": "Dose",       "v": "14 g" },
            { "k": "Yield",      "v": "36 g" },
            { "k": "Water temp", "v": "93 °C" },
            { "k": "Time",       "v": "25-30 s" },
            { "k": "Grind",      "v": "fine" }
          ]
        }
      },
      {
        "type": "text",
        "props": {
          "content": "Cup before you pull. Tare the scale. Start the timer when you press the button — not when the first drop appears. Stop at yield, not at time.",
          "variant": "body"
        }
      }
    ]
  }
}
```

# Anti-patterns to avoid

- ❌ Wrapping the facts in ``stat`` widgets (KPI tile grid) — they're reference data, not metrics with deltas
- ❌ Using ``hero+grid`` layout — this is a viewer, not a dashboard
- ❌ Adding a ``chart`` — there's nothing to chart in a static reference
- ❌ Empty text fields / Lorem ipsum — the LLM should fabricate plausible domain content

# Variations

- **Cocktail recipe**: keep the shape verbatim; swap content (gin & tonic, dose = 50ml gin / 150ml tonic / 1 lime wedge)
- **Glossary entry**: ``kv-table`` becomes the definitions table; lead text becomes the term + part-of-speech
- **Runbook**: add a ``checklist-layout`` after the lead paragraph with ordered steps
- **API reference page**: ``kv-table`` becomes parameter table; add a ``code-block`` body example
- **Character / profile card**: rename ``Espresso 101`` → person's name; ``kv-table`` items become Role / Joined / Team / Location
- **Wine notes**: facts table = grape / vintage / region / tasting notes
