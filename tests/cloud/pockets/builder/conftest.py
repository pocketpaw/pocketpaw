# Pockets builder — shared test fixtures.
#
# Created 2026-05-01.  ``FakeStructuredCallProvider`` is the standard
# stub: monkeypatches ``ee.cloud.pockets.builder.providers.structured_call``
# (and the import inside ``service.py``) with a queue-driven async fake so
# tests don't need real provider keys.

from __future__ import annotations

from collections import deque
from typing import Any

import pytest
from pydantic import BaseModel

from ee.cloud.pockets.builder.providers import ProviderError


class FakeStructuredCallProvider:
    """Drop-in replacement for ``providers.structured_call``.

    Tests configure ``returns`` with a list of ``BaseModel`` instances or
    ``ProviderError`` exceptions; each call pops the next item.  The fake
    raises ``RuntimeError`` if the list is exhausted unexpectedly so silent
    over-calling fails loudly.
    """

    def __init__(self) -> None:
        self.returns: deque[BaseModel | Exception] = deque()
        self.calls: list[dict[str, Any]] = []

    def configure(self, items: list[BaseModel | Exception]) -> None:
        self.returns = deque(items)

    async def __call__(
        self,
        provider: str,
        schema: type[BaseModel],
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        settings: Any = None,
    ) -> BaseModel:
        self.calls.append(
            {
                "provider": provider,
                "schema": schema,
                "messages": messages,
                "model": model,
            }
        )
        if not self.returns:
            raise RuntimeError(
                "FakeStructuredCallProvider exhausted — test set up too few "
                f"return values for provider={provider} schema={schema.__name__}"
            )
        item = self.returns.popleft()
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def fake_provider(monkeypatch: pytest.MonkeyPatch) -> FakeStructuredCallProvider:
    """Patch ``structured_call`` everywhere it's used inside the builder."""
    fake = FakeStructuredCallProvider()
    # Patch in providers module (where it's defined).
    monkeypatch.setattr(
        "ee.cloud.pockets.builder.providers.structured_call", fake
    )
    # service.py imports the symbol at module-load — patch that binding too.
    monkeypatch.setattr(
        "ee.cloud.pockets.builder.service.structured_call", fake
    )
    return fake


# Re-export ProviderError for tests that want to enqueue a failure.
__all__ = ["FakeStructuredCallProvider", "ProviderError"]
