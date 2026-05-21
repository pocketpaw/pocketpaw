---
{
  "title": "Sales Pipeline Dashboard — pipeline-dashboard composed widget recipe",
  "summary": "Single pipeline-dashboard widget at the root with all data passed as props (quota / funnel / conversion / leaderboard / deals / ticker). Use whenever the user asks for a sales pipeline, quota tracker, or revenue dashboard. The widget composes the funnel + leaderboard + deals table + activity ticker internally — do NOT rebuild from primitives.",
  "concepts": [
    "sales-pipeline",
    "pipeline-dashboard",
    "quota-tracker",
    "revenue-dashboard",
    "deal-funnel",
    "rep-leaderboard",
    "conversion-rate",
    "crm-dashboard",
    "renewals-pipeline",
    "recruiting-pipeline",
    "customer-success-pipeline",
    "composed-widget",
    "funnel-prop",
    "deals-table"
  ],
  "categories": [
    "recipe",
    "dashboard-pattern",
    "sales-revenue",
    "composed-widgets"
  ],
  "source_path": "sales-pipeline-dashboard.md",
  "source_docs": [
    "e1b0e99c21d4c030"
  ],
  "backlinks": null,
  "word_count": 366,
  "compiled_at": "2026-05-14T08:42:35Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

# Sales Pipeline Dashboard

## When this recipe applies

A brief that maps to a sales / revenue dashboard. Markers: 'sales pipeline', 'quota tracker', 'revenue dashboard', 'deal funnel', 'rep leaderboard', 'CRM dashboard', 'sales metrics for Q[N]'.

## Why this composition specifically

The LLM's failure mode without this recipe: it sees 'sales dashboard' and reaches for grid + stat tiles + a chart + a deals table, composing the whole layout from primitives. That reads as 'a generic dashboard with sales numbers' — not 'a sales pipeline'. Each tile floats on its own; the funnel-leaderboard-deals composition is lost.

The pipeline-dashboard composed widget exists precisely for this domain. It takes structured props (quota, funnel.stages, conversion, leaderboard.items, deals.rows, ticker) and renders the canonical sales-pipeline visualisation — funnel on the left, quota and leaderboard on the right, deals table below, activity ticker at the bottom. The composition is encoded in the widget; the LLM's job is to supply the data.

This is the case where the polished widget beats a hand-rolled layout by the largest margin. The hand-rolled version takes ~15 widgets; this is one.

## Anti-patterns this recipe replaces

- Building from scratch with grid + stat tiles + chart + table → use pipeline-dashboard
- Using flex as root with funnel / leaderboard / deals as siblings → use pipeline-dashboard
- Hand-rolling a funnel as inverted bar-chart → use the funnel prop
- Empty quota / placeholder numbers → realistic mock data ($1.8M / $2.5M, 28 days remaining, named reps)

## Adjacent domains (variations of the same shape)

- **Smaller team (≤4 reps)**: drop ticker and conversion, keep quota + funnel + leaderboard + deals
- **Solo SDR**: replace leaderboard with a single quota block; keep funnel + deals
- **Renewals dashboard**: rename 'Pipeline funnel' → 'Renewal funnel', stages = [Up for renewal, Engaged, Quote sent, Closed]
- **Recruiting pipeline**: stages = [Sourced, Phone screen, Onsite, Offer, Hired]; leaderboard = recruiters
- **Customer success pipeline**: stages = [Onboarding, Adoption, Renewal-due, Renewed]

## Known gaps

The mock data uses static names (Globex, Stark Industries) borrowed from the Ripple showcase. In a live deployment the agent should fabricate names that match the user's business context — same shape, contextual content.