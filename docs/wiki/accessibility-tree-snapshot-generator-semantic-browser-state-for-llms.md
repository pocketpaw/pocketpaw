---
{
  "title": "Accessibility Tree Snapshot Generator — Semantic Browser State for LLMs",
  "summary": "snapshot.py converts Playwright's raw accessibility tree into a compact, indented text representation with [ref=N] markers that an LLM can read to understand page structure and reference specific elements for interaction. RefMap maintains the mapping from integer references back to element selectors, and SnapshotGenerator handles recursive tree traversal, role filtering, name truncation, and selector generation.",
  "concepts": [
    "SnapshotGenerator",
    "AccessibilityNode",
    "RefMap",
    "accessibility tree",
    "semantic snapshot",
    "ref markers",
    "role filtering",
    "name truncation",
    "Playwright",
    "selector generation",
    "LLM browser control"
  ],
  "categories": [
    "browser",
    "accessibility",
    "snapshot",
    "agent-tools"
  ],
  "source_docs": [
    "0000000000000009"
  ],
  "backlinks": null,
  "word_count": 469,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Why Accessibility Trees Instead of HTML?

Raw HTML is verbose: a typical page has thousands of DOM nodes, CSS classes, script tags, and attributes that are irrelevant to the task at hand. Injecting raw HTML into a model prompt would consume most of the context window while providing little navigable signal.

The browser's accessibility tree is a semantic, role-based view of the page that screen readers use. It contains only the nodes that are meaningful to a user: buttons, links, inputs, headings, and text. `SnapshotGenerator` converts this tree into plain text that is typically 90% smaller than the equivalent HTML while preserving all the information needed for agent navigation.

## RefMap

`RefMap` is a simple counter-based mapping:

```python
@dataclass
class RefMap:
    refs: dict[int, str]
    next_ref: int = 1

    def add(self, selector: str) -> int:
        ref = self.next_ref
        self.refs[ref] = selector
        self.next_ref += 1
        return ref
```

When `SnapshotGenerator` encounters an interactive element (button, link, input), it calls `refmap.add(selector)` to register a selector and receive an integer reference. The snapshot text then includes `[ref=N]` next to that element. When the agent later calls `driver.click(ref=5)`, the driver looks up `refmap.get_selector(5)` to find the actual selector.

Using integers rather than raw selectors in the prompt keeps the snapshot compact. Selectors can be long XPath expressions; replacing them with a single digit reduces token count substantially on pages with many interactive elements.

## AccessibilityNode

`AccessibilityNode` is PocketPaw's internal representation of an accessibility tree node. It is converted from Playwright's raw dict format via `from_playwright_dict()`, which extracts a fixed list of known properties (`level`, `focused`, `disabled`, `checked`, `expanded`, `selected`, `pressed`, `required`, `readonly`, `hidden`, `type`, `value`, `valuetext`, `description`) and recursively converts children.

The fixed property list is intentional: unknown properties from Playwright's format are silently ignored, making the converter resilient to Playwright version changes that add new accessibility attributes.

## SnapshotGenerator

`SnapshotGenerator` performs a recursive depth-first traversal of the `AccessibilityNode` tree, writing an indented text representation to an internal buffer. Key behaviours:

- **Role filtering** — Certain roles (e.g., `generic`, `none`, `presentation`) are skipped because they add structural noise without semantic content.
- **Name truncation** — Long element names (e.g., a paragraph of body text captured as a link's aria-label) are truncated via `_truncate_name()` to prevent single elements from dominating the snapshot.
- **Property annotation** — Relevant properties like `[disabled]`, `[checked]`, `[expanded=false]` are appended inline to give the model accurate state information without a separate data structure.
- **Selector generation** — `_generate_selector()` creates a Playwright-compatible selector string for each interactive node, which is what gets stored in `RefMap`.

## Known Gaps

The snapshot is regenerated on every call to `_take_snapshot()` in the driver, which means the `RefMap` is also rebuilt. References from a previous snapshot become invalid after any navigation or interaction — the agent must always use references from the most recent snapshot.