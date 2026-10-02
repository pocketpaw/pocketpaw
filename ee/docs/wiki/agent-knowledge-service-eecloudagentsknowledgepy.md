---
{
  "title": "Agent Knowledge Service (ee/cloud/agents/knowledge.py)",
  "summary": "The agent knowledge service over the kb-go binary. Callers pass a scope string (`agent:{id}`, `workspace:{id}`, `pocket:{id}`); kb-go compiles, stores, indexes and searches. Text, URL and file ingestion plus search.",
  "concepts": [
    "KnowledgeService",
    "kb-go",
    "RAG",
    "agent scoping",
    "text ingestion",
    "URL ingestion",
    "search"
  ],
  "categories": [
    "enterprise",
    "cloud",
    "knowledge management",
    "agents"
  ],
  "source_docs": [
    "f1827701a9a101de"
  ],
  "backlinks": null,
  "word_count": 343,
  "compiled_at": "2026-04-08T07:30:11Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Agent Knowledge Service

> Updated 2026-10-01: the in-process `pocketpaw.knowledge.KnowledgeEngine` this
> page used to describe was deleted (it had no importers). The service runs on
> the kb-go binary; `ee/pocketpaw_ee/cloud/agents/knowledge.py`'s header is the
> current reference.

## Purpose

`KnowledgeService` adapts the kb-go knowledge base for the cloud agents domain.
Every ingest goes through `ingest_text_to_scope` (one document, one article) or
`ingest_document_to_scope` (a long document, one article per section). The
caller picks the scope string, which partitions storage so one agent's or
workspace's documents never show up in another's searches.

## Search

`search_context_for_scope` returns a prompt-ready context string for a chat
turn. It fails soft: a timeout or kb error returns "" with a warning, so the KB
never stalls a turn.

## Failure mode

A document is never stored verbatim. A compile failure raises, and an old kb
binary that ignores `--article-json` raises `KnowledgeEngineUnavailable` (the
name predates the deletion; it refers to the kb binary).
