"""SR-1 — real per-engine publish cost for Paw Sites.

Measures what a publish actually costs per engine (react, svelte, ripple-static,
ripple-dynamic) by generating a real project from paw-sites' own fixtures and
running the real ``bun install`` + ``bun run build``: cold and warm totals, the
install vs build split, peak RSS, and cores saturated.

react is the reason this exists — the 45-60s static / 4m47s dynamic figures on
record are ripple/svelte only, and react (a Vite SSG: three vite passes plus a
prerender step) had never been measured.

Read-only with respect to paw-sites; all work happens in a temp dir.
"""
