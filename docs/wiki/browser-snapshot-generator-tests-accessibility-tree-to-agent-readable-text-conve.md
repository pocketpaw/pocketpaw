---
{
  "title": "Browser Snapshot Generator Tests: Accessibility Tree to Agent-Readable Text Conversion",
  "summary": "This test suite validates `SnapshotGenerator`, which converts Playwright's accessibility tree JSON into a compact, ref-tagged text representation that AI agents can read and reason about. Tests cover the full rendering pipeline: interactive element ref assignment, non-interactive element handling, heading/list/image rendering, hidden element filtering, password field masking, and the `RefMap` reference number system.",
  "concepts": [
    "SnapshotGenerator",
    "RefMap",
    "AccessibilityNode",
    "accessibility tree",
    "Playwright",
    "interactive elements",
    "hidden elements",
    "password fields",
    "checkbox",
    "combobox",
    "ref tags"
  ],
  "categories": [
    "browser automation",
    "testing",
    "accessibility",
    "snapshot generation",
    "test"
  ],
  "source_docs": [
    "e37efef2e0c92e60"
  ],
  "backlinks": null,
  "word_count": 463,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

When a browser agent navigates to a page, it reads the page through a text snapshot of the accessibility tree. `SnapshotGenerator` converts the raw JSON accessibility tree from Playwright's `page.accessibility.snapshot()` into a structured text format:

```
Page: Example Domain
URL: https://example.com

- heading "Example Domain" [level=1]
- paragraph "This domain is for use in illustrative examples."
- link "More information..." [ref=1]
- button "Submit" [ref=2]
```

The `[ref=N]` tags on interactive elements allow agents to reference specific elements in subsequent actions (e.g., `click(ref=1)`).

## `RefMap` — Reference Number Registry

```python
class TestRefMap:
    def test_add_ref(self):
        refmap = RefMap()
        ref = refmap.add("button#submit")
        assert ref == 1
        assert refmap.refs[1] == "button#submit"
        assert refmap.next_ref == 2
```

`RefMap` provides the bidirectional mapping between numeric ref IDs (what the agent sees in the snapshot) and Playwright locator strings (what the driver uses to find elements). The sequential numbering starting at 1 is intentional — it makes snapshots easier for agents to parse and reference.

## `SnapshotGenerator` — Rendering Rules

```python
class TestSnapshotGenerator:
    def test_generate_interactive_elements_get_refs(self):
        # buttons, links, inputs get [ref=N] tags

    def test_generate_non_interactive_elements_no_refs(self):
        # paragraphs, divs do not get refs

    def test_skip_hidden_elements(self):
        # aria-hidden elements omitted entirely

    def test_truncate_long_names(self):
        # names longer than threshold are truncated
```

The distinction between interactive and non-interactive elements is critical for agent usability: an agent should only be able to `click` or `type` on elements that have refs. Including refs on non-interactive elements would pollute the ref namespace and confuse the agent. Hidden element filtering prevents the snapshot from including screen-reader-only content that the agent couldn't meaningfully interact with. Name truncation prevents excessively long element names from overwhelming the snapshot and hitting token limits.

## Sensitive Field Handling

```python
def test_generate_password_field(self):
    # password inputs rendered as "password [ref=N]" without showing value
```

Password fields appear in the snapshot with their type label but no current value displayed, preventing the snapshot from leaking credentials into agent logs or LLM context.

## Stateful Form Controls

```python
def test_generate_checkbox(self):
    # checked state shown: - checkbox "Accept terms" [checked] [ref=1]

def test_generate_combobox(self):
    # selected value shown: - combobox "Country" "United States" [ref=1]
```

Stateful form controls include their current state in the snapshot, allowing agents to reason about what's already selected before making changes.

## Playwright Tree Conversion

`TestSnapshotFromPlaywrightTree` tests the translation layer from Playwright's raw JSON to PocketPaw's internal `AccessibilityNode` dataclass. This boundary is where most snapshot rendering bugs originate, since Playwright's accessibility tree format can vary across browser versions and site implementations.

## Known Gaps

The `test_selector_generation_for_button` test verifies selector generation for buttons, but no test covers the selector format for all interactive element types (inputs, selects, textareas). Complex ARIA roles not covered by the explicit tests would fall through to a default rendering path that may or may not be correct.