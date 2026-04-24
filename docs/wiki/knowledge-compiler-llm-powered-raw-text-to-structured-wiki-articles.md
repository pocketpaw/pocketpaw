---
{
  "title": "Knowledge Compiler: LLM-Powered Raw Text to Structured Wiki Articles",
  "summary": "The knowledge compiler is the intelligence layer of PocketPaw's knowledge engine — it sends raw documents to an LLM with a strict prompt that elicits a structured JSON article with title, summary, content, concepts, and categories. Robust JSON parsing handles markdown fences and partial LLM output that would otherwise cause silent data loss.",
  "concepts": [
    "knowledge compiler",
    "LLM compilation",
    "WikiArticle",
    "_parse_llm_output",
    "JSON parsing",
    "slug generation",
    "_COMPILE_PROMPT",
    "RawDoc",
    "markdown fences",
    "structured extraction"
  ],
  "categories": [
    "knowledge",
    "AI pipeline",
    "LLM"
  ],
  "source_docs": [
    "cc4093c7839358d2"
  ],
  "backlinks": null,
  "word_count": 412,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/knowledge/compiler.py` transforms raw text into structured `WikiArticle` objects. This compilation step is what distinguishes PocketPaw's knowledge engine from basic RAG: rather than chunking and embedding raw text, an LLM reads the document and produces a curated, well-structured article that is far more searchable and navigable.

## The Compile Prompt

The prompt instructs the LLM to output a specific JSON schema:

```python
_COMPILE_PROMPT = """
Output EXACTLY this JSON format (no markdown fences, just raw JSON):
{
  "title": "Concise descriptive title",
  "summary": "2-3 sentence summary of the key information",
  "content": "Full well-structured markdown article with ## headers",
  "concepts": ["key entity 1", "key entity 2"],
  "categories": ["broad topic 1"]
}
Rules:
- Preserve all factual information — do not hallucinate or add information not in the source
- If the source is short, the article can be short. Do not pad.
"""
```

The explicit "no markdown fences" instruction addresses a common LLM behavior. The `_parse_llm_output` function still handles this case as a fallback, because prompt instructions are not always followed.

## Robust JSON Parsing

```python
def _parse_llm_output(text: str) -> dict:
    """Extract JSON from LLM output, handling markdown fences and partial output."""
```

This function handles three common LLM output failure modes:

1. **Markdown fences**: The LLM wraps JSON in triple backticks. Strip the fences.
2. **Trailing text**: The LLM adds a comment after the JSON. Find the closing brace by scanning.
3. **Partial output**: The LLM is cut off mid-JSON at a token limit. Attempt to close open braces.

Without this defensive parsing, any of these cases would raise `json.JSONDecodeError` and lose the compiled article entirely.

## Slug Generation

```python
def _slugify(title: str) -> str:
    slug = title.lower().strip()
    slug = re.sub(r"[^a-z0-9-]", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug
```

Article IDs are derived from titles via slugification. A title change produces a new ID and orphans the old article — there is no rename logic.

## Article Assembly

After the LLM response is parsed, the compiler assembles a `WikiArticle` with `source_docs=[raw_doc.id]`, linking the article back to the raw document it was compiled from and enabling recompilation when the prompt improves.

## Known Gaps

- **No compile failure recovery**: If the LLM returns unparseable output after all fallbacks, the compile fails with an exception. There is no fallback stub article.
- **No model selection at call site**: The model used for compilation is determined by backend configuration, not by the caller. High-volume ingestion and quality-sensitive compilation share the same model.