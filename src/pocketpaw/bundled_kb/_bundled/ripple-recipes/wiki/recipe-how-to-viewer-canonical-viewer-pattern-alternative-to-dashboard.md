---
{
  "title": "Recipe / How-To Viewer — canonical viewer pattern alternative to dashboard",
  "summary": "Canonical recipe for the viewer pattern: page-header + lead text + kv-table of stable facts + body paragraph. Use whenever the user wants a read-only reference page (cocktail recipe, glossary entry, runbook step, API reference, character profile, wine notes). This is the explicit non-dashboard alternative for brief that look like 'how to do X' or 'reference for Y'.",
  "concepts": [
    "viewer-pattern",
    "recipe",
    "how-to",
    "reference-page",
    "runbook",
    "glossary",
    "kv-table",
    "page-header",
    "lead-text",
    "stable-facts",
    "espresso",
    "entity-detail",
    "supporting-pane",
    "non-dashboard"
  ],
  "categories": [
    "recipe",
    "viewer-pattern",
    "reference-content",
    "read-only-pages"
  ],
  "source_path": "espresso-recipe-viewer.md",
  "source_docs": [
    "86deac00e8a48c5e"
  ],
  "backlinks": null,
  "word_count": 384,
  "compiled_at": "2026-05-14T08:42:35Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

# Recipe / How-To Viewer

## When this recipe applies

The user wants a read-only page that summarises one thing. A recipe, a how-to, a glossary entry, a runbook, an API parameter reference, a person profile, a wine note. Markers in the brief: 'how to', 'guide', 'reference', 'instructions for', 'about Y', 'notes on Y', 'walkthrough'.

This recipe is the canonical alternative to 'everything is a dashboard'. A pocket that's just a single fact + paragraph belongs here — NOT in a hero+grid layout with KPI tiles.

## Why this composition specifically

The LLM's failure mode without this recipe: it sees 'reference data' and reaches for stat widgets, turning what should be 'dose: 14g' (a fact) into a KPI tile with a fake delta. Stat widgets imply trend and change — they're for metrics, not for stable parameters.

The page-header at the top establishes context (title + subtitle). The lead text gives a one-paragraph summary the reader scans first. The kv-table holds the stable facts (measurements, defaults, parameters) in a scannable two-column layout — denser than a stat grid, more semantic than a plain table. The body text closes with prose guidance the reader applies once they've absorbed the facts.

This is the same shape Material 3 calls 'supporting-pane' and Apple HIG calls a 'reading view'. Not novel — just named consistently in the PocketPaw vocabulary.

## Anti-patterns this recipe replaces

- Wrapping facts in stat widgets (KPI grid) → kv-table
- Using hero+grid layout → flex column with page-header at the top
- Adding a chart for static reference data → no chart
- Empty text / Lorem ipsum → fabricated plausible domain content

## Adjacent domains (variations of the same shape)

- **Cocktail recipe**: same shape; swap content (gin & tonic, dose = 50ml gin / 150ml tonic / 1 lime wedge)
- **Glossary entry**: kv-table becomes the definitions table; lead text becomes the term + part-of-speech
- **Runbook**: add a checklist-layout after the lead paragraph with ordered steps
- **API reference page**: kv-table = parameter table; add a code-block body example
- **Character / profile card**: kv-table items become Role / Joined / Team / Location
- **Wine notes**: facts table = grape / vintage / region / tasting notes
- **How-to guide**: replace kv-table with steps widget for a numbered procedure