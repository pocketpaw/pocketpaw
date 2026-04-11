"""Knowledge linter — LLM-powered health checks on the wiki.

Finds:
- Inconsistencies between articles
- Gaps (topics mentioned but no dedicated article)
- Missing connections (articles that should link but don't)
- Suggestions for new articles from raw docs not yet compiled
"""

from __future__ import annotations

import json
import logging
import re

from pocketpaw.knowledge.models import KnowledgeIndex, LintIssue, WikiArticle

logger = logging.getLogger(__name__)

_LINT_PROMPT = """You are a knowledge base auditor. Review these wiki articles and find issues.

Articles in the knowledge base:
{articles_summary}

Uncompiled raw documents (not yet turned into articles):
{raw_docs_summary}

Find issues in these categories:
1. INCONSISTENCY: Two articles contradict each other on facts/numbers/dates
2. GAP: A topic is frequently mentioned across articles but has no dedicated article
3. CONNECTION: Two articles discuss related topics but don't reference each other
4. STALE: An article references information that seems outdated
5. UNCOMPILED: A raw document exists that should be compiled into an article

Output a JSON array of issues:
[
  {{
    "type": "inconsistency|gap|connection|stale|uncompiled",
    "severity": "info|warning|error",
    "message": "Clear description of the issue",
    "article_id": "affected-article-slug or null",
    "suggestion": "How to fix this"
  }}
]

If no issues found, output an empty array: []
Only output the JSON array, nothing else."""


async def lint_knowledge(
    articles: list[WikiArticle],
    raw_docs: list[dict],
    index: KnowledgeIndex,
    model: str = "",
    backend: str = "",
) -> list[LintIssue]:
    """Run LLM-powered lint checks on the knowledge base."""
    if not articles:
        return []

    # Build summaries for the prompt
    articles_summary = "\n".join(
        f"- [{a.id}] {a.title}: {a.summary} (concepts: {', '.join(a.concepts[:5])})"
        for a in articles
    )

    compiled_ids = set()
    for a in articles:
        compiled_ids.update(a.source_docs)
    uncompiled = [d for d in raw_docs if d.get("id") not in compiled_ids]
    raw_docs_summary = (
        "\n".join(
            f"- [{d.get('id', '?')}] "
            f"{d.get('filename') or d.get('source', 'unknown')} "
            f"({d.get('content_type', '?')})"
            for d in uncompiled[:20]
        )
        or "None"
    )

    prompt = _LINT_PROMPT.format(
        articles_summary=articles_summary,
        raw_docs_summary=raw_docs_summary,
    )

    # Run LLM
    from pocketpaw.agents.registry import get_backend_class
    from pocketpaw.config import Settings

    settings = Settings.load()
    backend_name = backend or settings.agent_backend
    if model:
        if "claude" in backend_name:
            settings.claude_sdk_model = model
        elif "openai" in backend_name:
            settings.openai_model = model

    backend_cls = get_backend_class(backend_name)
    if not backend_cls:
        return [
            LintIssue(
                type="error", severity="error", message=f"Backend '{backend_name}' not available"
            )
        ]

    agent = backend_cls(settings)
    result_text = ""
    try:
        async for event in agent.run(prompt, system_prompt="Output only a JSON array."):
            if event.type == "message":
                result_text += event.content
            if event.type == "done":
                break
    finally:
        await agent.stop()

    # Parse
    return _parse_lint_output(result_text)


def _parse_lint_output(text: str) -> list[LintIssue]:
    """Parse LLM lint output into LintIssue objects."""
    text = text.strip()

    # Try direct parse
    try:
        items = json.loads(text)
        if isinstance(items, list):
            return [_to_issue(i) for i in items if isinstance(i, dict)]
    except json.JSONDecodeError:
        pass

    # Try extracting JSON array
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            items = json.loads(match.group(0))
            if isinstance(items, list):
                return [_to_issue(i) for i in items if isinstance(i, dict)]
        except json.JSONDecodeError:
            pass

    logger.warning("Failed to parse lint output")
    return []


def _to_issue(data: dict) -> LintIssue:
    return LintIssue(
        type=data.get("type", "info"),
        severity=data.get("severity", "info"),
        message=data.get("message", ""),
        article_id=data.get("article_id"),
        suggestion=data.get("suggestion"),
    )
