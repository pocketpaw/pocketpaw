# Browser DOM snapshot generator
# Changes: 2026-09-06 (BR-1, feat/browser-surface-server) — REWRITTEN off
#   Playwright's accessibility API. ``page.accessibility`` was REMOVED from
#   Playwright (gone on the installed 1.58: ``'Page' object has no attribute
#   'accessibility'``), which made the old AccessibilityNode / SnapshotGenerator
#   pair dead code — every call raised. Python's ``Locator.aria_snapshot()`` is
#   not a replacement either: it has no ``ref=True`` mode (JS-only), so it
#   cannot hand the agent clickable refs.
#   Replacement is ``SNAPSHOT_JS``: one ``page.evaluate`` that walks the VISIBLE
#   DOM, stamps ``data-paw-ref="N"`` on each interactive element, and returns
#   ``{text, count, title, url}``. Refs then resolve as the plain CSS selector
#   ``[data-paw-ref="N"]`` — verified against example.com (1 ref) and
#   news.ycombinator.com (230 refs), and a click through ref=1 navigated.
#   ``RefMap`` and its ``refs`` / ``get_selector()`` contract are unchanged;
#   ``AccessibilityNode`` + ``SnapshotGenerator`` are deleted rather than left
#   orphaned.
"""DOM-walk snapshot: semantic page text with ``[ref=N]`` markers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Walks the visible DOM once, stamping data-paw-ref="N" on interactive elements.
# Anything the LLM can act on gets a ref; headings and leaf text come through
# unreffed so the page still reads as a page. Password inputs are flagged
# SENSITIVE inline — the hard refusal lives in the MCP tool, this is only a hint.
SNAPSHOT_JS = r"""
() => {
  const INTERACTIVE = new Set(['A','BUTTON','INPUT','TEXTAREA','SELECT','SUMMARY','OPTION']);
  let ref = 0;
  const out = [];
  const nameOf = (el) => (
    el.getAttribute('aria-label') ||
    el.getAttribute('placeholder') ||
    el.getAttribute('title') ||
    (el.innerText || el.value || '').trim().slice(0, 80)
  ).replace(/\s+/g, ' ').trim();
  const roleOf = (el) => {
    const r = el.getAttribute('role');
    if (r) return r;
    const t = el.tagName;
    if (t === 'A') return 'link';
    if (t === 'BUTTON') return 'button';
    if (t === 'INPUT') return (el.type === 'submit' || el.type === 'button') ? 'button' : 'textbox';
    if (t === 'TEXTAREA') return 'textbox';
    if (t === 'SELECT') return 'combobox';
    if (/^H[1-6]$/.test(t)) return 'heading';
    return t.toLowerCase();
  };
  const visible = (el) => {
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const walk = (node, depth) => {
    for (const el of node.children) {
      if (['SCRIPT','STYLE','NOSCRIPT','SVG'].includes(el.tagName)) continue;
      if (!visible(el)) continue;
      const interactive = INTERACTIVE.has(el.tagName) || el.hasAttribute('role')
        || el.isContentEditable;
      const heading = /^H[1-6]$/.test(el.tagName);
      const name = nameOf(el);
      if (interactive) {
        ref += 1;
        el.setAttribute('data-paw-ref', String(ref));
        const sensitive = el.type === 'password' ? ' SENSITIVE' : '';
        const extra = el.tagName === 'INPUT' ? ` type=${el.type}${sensitive}` : '';
        out.push(`${'  '.repeat(depth)}- ${roleOf(el)} "${name}" [ref=${ref}]${extra}`);
      } else if (heading && name) {
        out.push(`${'  '.repeat(depth)}- ${roleOf(el)} "${name}"`);
      } else if (el.children.length === 0 && name) {
        out.push(`${'  '.repeat(depth)}- text: ${name}`);
      }
      walk(el, depth + (interactive || heading ? 1 : 0));
    }
  };
  document.querySelectorAll('[data-paw-ref]').forEach(e => e.removeAttribute('data-paw-ref'));
  if (document.body) walk(document.body, 0);
  return { text: out.join('\n'), count: ref, title: document.title, url: location.href };
}
"""

# A snapshot of a big page (HN is ~230 refs and thousands of text leaves) can be
# tens of KB. Cap it so one navigation can't blow the agent's context window.
MAX_SNAPSHOT_CHARS = 20000


@dataclass
class RefMap:
    """Maps reference numbers to element selectors.

    The LLM uses [ref=N] in the snapshot to identify elements,
    and we use this mapping to find the actual element for interaction.
    """

    refs: dict[int, str] = field(default_factory=dict)
    next_ref: int = 1

    def get_selector(self, ref: int) -> str | None:
        """Get selector by reference number."""
        return self.refs.get(ref)


def render_snapshot(result: dict[str, Any]) -> tuple[str, RefMap]:
    """Turn the ``SNAPSHOT_JS`` payload into snapshot text plus a ``RefMap``.

    Every ref 1..count was stamped by the same evaluate that produced ``text``,
    so the map is built straight from the count — no second DOM pass.
    """
    count = int(result.get("count") or 0)
    refmap = RefMap(
        refs={i: f'[data-paw-ref="{i}"]' for i in range(1, count + 1)},
        next_ref=count + 1,
    )

    body = str(result.get("text") or "")
    truncated = len(body) > MAX_SNAPSHOT_CHARS
    if truncated:
        body = body[:MAX_SNAPSHOT_CHARS]

    lines = [f"Page: {result.get('title') or ''}", f"URL: {result.get('url') or ''}", ""]
    lines.append(body)
    if truncated:
        lines.append(f"... [snapshot truncated at {MAX_SNAPSHOT_CHARS} chars]")

    return "\n".join(lines), refmap


__all__ = ["MAX_SNAPSHOT_CHARS", "SNAPSHOT_JS", "RefMap", "render_snapshot"]
