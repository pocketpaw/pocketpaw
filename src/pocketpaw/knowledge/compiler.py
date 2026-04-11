"""Knowledge compiler — uses LLM to transform raw documents into structured wiki articles.

The compile step is what makes this a knowledge engine, not just RAG:
raw text → LLM → structured article with title, summary, concepts, categories.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

from pocketpaw.knowledge.models import RawDoc, WikiArticle

logger = logging.getLogger(__name__)

_COMPILE_PROMPT = """\
You are a knowledge compiler. \
Transform the raw document into a structured wiki article.

Raw document (source: {source}):
---
{raw_text}
---

Output EXACTLY this JSON format (no markdown fences, just raw JSON):
{{
  "title": "Concise descriptive title",
  "summary": "2-3 sentence summary of the key information",
  "content": "Full well-structured markdown article with headers \
(## sections). Preserve all important facts, numbers, names, dates. \
Organize logically.",
  "concepts": ["key entity 1", "key entity 2", "important term"],
  "categories": ["broad topic 1", "broad topic 2"]
}}

Rules:
- Title should be specific and descriptive (not generic)
- Summary captures the most important takeaways
- Content is well-structured markdown with ## headers for sections
- Concepts are specific entities, terms, names, metrics mentioned (5-15 items)
- Categories are 1-3 broad topic areas
- Preserve all factual information — do not hallucinate or add information not in the source
- If the source is short, the article can be short. Don't pad."""


def _slugify(title: str) -> str:
    """Convert title to URL-friendly slug."""
    slug = title.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s-]+", "-", slug)
    return slug[:80].strip("-") or "untitled"


async def compile_article(
    raw_doc: RawDoc,
    model: str = "",
    backend: str = "",
) -> WikiArticle:
    """Use LLM to compile a raw document into a structured wiki article."""
    from pocketpaw.agents.registry import get_backend_class
    from pocketpaw.config import Settings

    settings = Settings.load()

    # Use specified backend/model or fall back to defaults
    backend_name = backend or settings.agent_backend
    if model:
        if "claude" in backend_name:
            settings.claude_sdk_model = model
        elif "openai" in backend_name:
            settings.openai_model = model

    backend_cls = get_backend_class(backend_name)
    if not backend_cls:
        raise RuntimeError(f"Backend '{backend_name}' not available for compilation")

    agent = backend_cls(settings)

    # Truncate very long docs to avoid context overflow
    raw_text = raw_doc.raw_text[:50_000]

    prompt = _COMPILE_PROMPT.format(
        source=raw_doc.source,
        raw_text=raw_text,
    )

    # Run LLM
    result_text = ""
    try:
        async for event in agent.run(
            prompt, system_prompt="You are a knowledge compiler. Output only valid JSON."
        ):
            if event.type == "message":
                result_text += event.content
            if event.type == "done":
                break
    finally:
        await agent.stop()

    # Parse LLM output
    article_data = _parse_llm_output(result_text)

    slug = _slugify(article_data.get("title", raw_doc.filename or raw_doc.source))

    return WikiArticle(
        id=slug,
        title=article_data.get("title", slug),
        summary=article_data.get("summary", ""),
        content=article_data.get("content", raw_text[:5000]),
        concepts=article_data.get("concepts", []),
        categories=article_data.get("categories", []),
        source_docs=[raw_doc.id],
        word_count=len(article_data.get("content", "").split()),
        compiled_at=datetime.now(UTC),
        compiled_with=f"{backend_name}/{model}" if model else backend_name,
    )


def _parse_llm_output(text: str) -> dict:
    """Extract JSON from LLM output, handling markdown fences and partial output."""
    # Try direct JSON parse
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code fence
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Try finding any JSON object in the text
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    # Last resort: create a basic article from the raw text
    logger.warning("Failed to parse LLM compilation output, using raw text")
    return {
        "title": "Untitled",
        "summary": text[:200] if text else "",
        "content": text,
        "concepts": [],
        "categories": [],
    }
