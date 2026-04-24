---
{
  "title": "Knowledge Linter: LLM-Powered Wiki Health Auditor",
  "summary": "The knowledge linter uses an LLM to audit the wiki for inconsistencies, coverage gaps, missing cross-links, stale content, and uncompiled raw documents. It produces structured `LintIssue` objects with severity levels and actionable suggestions, functioning as an automated knowledge quality reviewer.",
  "concepts": [
    "LintIssue",
    "knowledge linter",
    "lint_knowledge",
    "inconsistency detection",
    "coverage gaps",
    "_parse_lint_output",
    "_LINT_PROMPT",
    "KnowledgeIndex",
    "wiki health",
    "LLM auditing"
  ],
  "categories": [
    "knowledge",
    "quality assurance",
    "LLM"
  ],
  "source_docs": [
    "2092b84d3ac178c8"
  ],
  "backlinks": null,
  "word_count": 381,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/knowledge/linter.py` adds a quality assurance layer on top of the knowledge engine. While compilation and indexing handle structure, the linter handles accuracy and completeness — problems that only become apparent when viewing the knowledge base as a whole.

## Why LLM-Powered Linting?

Consistency checks between articles (e.g., article A says revenue was $4M, article B says $4.2M) require semantic understanding. A rule-based linter can check formatting and structure but cannot detect factual contradictions between documents written months apart. An LLM can.

## The Lint Prompt

The linter sends all article summaries and uncompiled raw document titles to the LLM in a single prompt, requesting findings in five categories:

1. **INCONSISTENCY**: Two articles contradict each other on facts, numbers, or dates
2. **GAP**: A topic is frequently mentioned but has no dedicated article
3. **CONNECTION**: Two related articles do not reference each other
4. **STALE**: An article references seemingly outdated information
5. **UNCOMPILED**: A raw document exists that should be compiled into an article

The prompt uses summaries rather than full article content to fit within the LLM context window.

## Issue Types and Severities

```python
@dataclass
class LintIssue:
    type: str        # inconsistency | gap | connection | stale | uncompiled
    severity: str    # info | warning | error
    message: str
    article_id: str | None
    suggestion: str
```

Severity levels allow the UI to filter and triage: errors (factual contradictions) are shown prominently; info issues (suggested new articles) can be dismissed.

## Parsing LLM Output

`_parse_lint_output` handles markdown fences and validates that the LLM returned a JSON array. Invalid entries (missing required fields) are logged and skipped rather than raising — partial lint results are more useful than a full failure.

## Integration with KnowledgeEngine

The linter is called via `engine.lint()`. Results are returned to the caller; no automatic remediation is performed. The user or an agent decides which issues to act on.

## Known Gaps

- **Summaries only, not full content**: Subtle inconsistencies buried in article bodies not surfaced in summaries will be missed.
- **No historical comparison**: The linter has no memory of previous lint runs and cannot report whether issues are being resolved over time.
- **Single LLM call for all articles**: For large knowledge bases, the articles summary may exceed the context window, causing silent truncation.