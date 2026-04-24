---
{
  "title": "Agent Knowledge Service — kb-go Binary Wrapper",
  "summary": "`knowledge.py` is a Python service that gives each agent a private knowledge base by delegating to the `kb` Go binary for compilation, indexing, and search, while handling heavyweight extraction (PDF, DOCX, URL, images) in Python before piping the plain text to `kb` via stdin.",
  "concepts": [
    "kb-go",
    "knowledge base",
    "BM25 search",
    "agent scoping",
    "trafilatura",
    "pypdf",
    "OCR",
    "subprocess",
    "text extraction",
    "context injection",
    "POCKETPAW_KB_BIN"
  ],
  "categories": [
    "knowledge management",
    "agents",
    "integrations"
  ],
  "source_docs": [
    "e5f99f50b1772b50"
  ],
  "backlinks": null,
  "word_count": 447,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Architecture Decision: Python + Go Split

The `kb-go` binary (github.com/qbtrix/kb-go) handles everything that benefits from compiled performance: BM25 indexing, LLM-compiled article storage, and fast search. Python handles the extraction layer where the ecosystem is richer: `trafilatura` for URL article extraction, `pypdf` for PDF text, `python-docx` for Word documents, and `pytesseract`+`Pillow` for OCR.

This split was introduced in 2026-04-07, replacing a pure-Python `knowledge_base` package. The motivation is that the Go binary is already installed for the kb-go skill, so reusing it avoids maintaining a parallel Python indexer.

## The `_kb()` Dispatcher

All operations go through a single internal function:

```python
def _kb(*args: str, input_text: str | None = None, timeout: int = 120) -> dict | list | str:
    cmd = [KB_BIN, *args, "--json"]
    result = subprocess.run(cmd, input=input_text, capture_output=True, text=True, timeout=timeout)
    ...
    return json.loads(result.stdout)
```

The `--json` flag is always passed so the output is machine-parseable. If the binary is not found, the `FileNotFoundError` is caught and re-raised with an actionable install message. Non-zero exit codes surface `stderr` (truncated to 200 chars) so the caller gets a useful error rather than a silent failure.

## Agent Scoping

Every `kb` invocation passes `--scope agent:{agent_id}`. This gives each agent an isolated knowledge namespace — one agent's ingested documents cannot leak into another's search results. The scope string follows the same hierarchical format used by the broader scope-picker system.

## Extraction Layer

**URL ingestion** (`ingest_url`): fetches the page with `httpx` (async, follows redirects) then extracts the main article text with `trafilatura`. If `trafilatura` is not installed, falls back to raw HTML (first 10 000 chars). Errors are returned as a dict rather than raised, so batch URL ingestion can continue past individual failures.

**File ingestion** (`ingest_file`): routes by extension. For `.pdf`, `.docx`, `.doc`, `.png`, `.jpg`, `.jpeg`, it extracts text in Python first, then pipes to `kb`. Plain text and code files go directly to `kb ingest` without pre-processing.

**OCR path**: requires `pytesseract` and `Pillow`. Missing dependencies raise `RuntimeError` with the install command, rather than silently producing empty text.

## Context Injection Mode

`search_context()` calls `kb search --context` which returns a pre-formatted context block ready for injection into an agent's system prompt. This avoids the caller having to format search results manually.

## Known Gaps

- `KB_BIN` is resolved at import time from `POCKETPAW_KB_BIN` env var (defaulting to `"kb"`). If the binary is not on `$PATH`, every operation fails. There is no health-check or fallback.
- The `timeout=120` default is generous but not configurable per-call. Large PDF files or slow URLs could still time out.
- `ingest_url` swallows all exceptions and returns an error dict. This means silent failures in batch ingestion are only visible if the caller inspects each result.