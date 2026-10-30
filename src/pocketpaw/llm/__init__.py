"""LLM package for PocketPaw.

Updated 2026-06-26 (MCG-11): exports the universal prompt-caching helper
(``build_cacheable`` / ``report_savings`` / ``CacheSavings``) from
``pocketpaw.llm.caching`` so any backend — OSS or EE — can build a
provider-correct cacheable prefix and measure the savings.
"""

from pocketpaw.llm.caching import (
    CacheSavings,
    build_cacheable,
    report_savings,
)
from pocketpaw.llm.client import LLMClient, resolve_llm_client
from pocketpaw.llm.router import LLMRouter

__all__ = [
    "CacheSavings",
    "LLMClient",
    "LLMRouter",
    "build_cacheable",
    "report_savings",
    "resolve_llm_client",
]
