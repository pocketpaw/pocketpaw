# test_chain.py — ExtractionChain routing tests.
# Created: 2026-04-30 — Phase 1 of "Files as Knowledge" plan, Stage 1.A.
# Pins the chain's selection algorithm: per-MIME override wins, otherwise
# first matching adapter, with offline detection and fallback on failure.
"""Tests for `ee.cloud.extraction.chain.ExtractionChain` and `build_chain`.

The chain owns three orthogonal pieces of behaviour:

  1. Selection — per-MIME override beats chain order; chain is searched in
     registration order using `supports_mimes` (with "*" as a wildcard).
  2. Offline detection — adapters with `requires_network = True` are
     skipped when `_is_online()` returns False.
  3. Failure recovery — adapter exceptions log and fall through to the
     offline fallback. The fallback's exceptions propagate.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ee.cloud.extraction import (
    ExtractionChain,
    ExtractionResult,
    build_chain,
)
from ee.cloud.extraction import chain as chain_mod


class _StubAdapter:
    """Minimal adapter for testing — records calls, returns canned text."""

    def __init__(
        self,
        name: str,
        mimes: set[str],
        requires_network: bool = False,
        raise_on_extract: Exception | None = None,
        text: str | None = None,
    ) -> None:
        self.name = name
        self.supports_mimes = mimes
        self.requires_network = requires_network
        self._raise = raise_on_extract
        self._text = text if text is not None else f"text-from-{name}"
        self.calls: list[tuple[Path, str]] = []

    async def extract(self, path: Path, mime: str) -> ExtractionResult:
        self.calls.append((path, mime))
        if self._raise is not None:
            raise self._raise
        return ExtractionResult(text=self._text, backend=self.name)


@pytest.fixture(autouse=True)
def _force_online(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests opt in to offline behaviour explicitly."""
    monkeypatch.setattr(chain_mod, "_is_online", lambda: True)


@pytest.fixture
def fallback() -> _StubAdapter:
    return _StubAdapter("local", {"*"})


def _settings(
    extraction_chain: list[str],
    extraction_per_mime: dict[str, str] | None = None,
    gemini_api_key: str | None = None,
) -> Any:
    return SimpleNamespace(
        extraction_chain=extraction_chain,
        extraction_per_mime=extraction_per_mime or {},
        extraction_offline_fallback="local",
        gemini_api_key=gemini_api_key,
    )


async def test_per_mime_override_wins(fallback: _StubAdapter) -> None:
    primary = _StubAdapter("primary", {"*"})
    override = _StubAdapter("special", {"image/png"})
    chain = ExtractionChain(
        adapters=[primary, override],
        offline_fallback=fallback,
        per_mime_override={"image/png": "special"},
    )

    result = await chain.run(Path("/tmp/x.png"), "image/png")

    assert result.backend == "special"
    assert override.calls == [(Path("/tmp/x.png"), "image/png")]
    assert primary.calls == []


async def test_first_matching_adapter_wins(fallback: _StubAdapter) -> None:
    a = _StubAdapter("first", {"image/png"})
    b = _StubAdapter("second", {"image/png"})
    chain = ExtractionChain(adapters=[a, b], offline_fallback=fallback)

    result = await chain.run(Path("/tmp/x.png"), "image/png")

    assert result.backend == "first"
    assert b.calls == []


async def test_wildcard_supports_unmatched_mimes(fallback: _StubAdapter) -> None:
    catchall = _StubAdapter("catch", {"*"})
    chain = ExtractionChain(adapters=[catchall], offline_fallback=fallback)

    result = await chain.run(Path("/tmp/x.weird"), "application/x-weird")

    assert result.backend == "catch"


async def test_unmatched_mime_falls_through_to_fallback(
    fallback: _StubAdapter,
) -> None:
    narrow = _StubAdapter("narrow", {"image/png"})
    chain = ExtractionChain(adapters=[narrow], offline_fallback=fallback)

    result = await chain.run(Path("/tmp/x.pdf"), "application/pdf")

    assert result.backend == "local"
    assert narrow.calls == []


async def test_offline_skips_network_adapter(
    monkeypatch: pytest.MonkeyPatch, fallback: _StubAdapter
) -> None:
    monkeypatch.setattr(chain_mod, "_is_online", lambda: False)
    cloud = _StubAdapter("cloud", {"image/png"}, requires_network=True)
    chain = ExtractionChain(adapters=[cloud], offline_fallback=fallback)

    result = await chain.run(Path("/tmp/x.png"), "image/png")

    assert result.backend == "local"
    assert cloud.calls == []


async def test_failing_adapter_falls_through(fallback: _StubAdapter) -> None:
    flaky = _StubAdapter(
        "flaky", {"image/png"}, raise_on_extract=RuntimeError("boom")
    )
    chain = ExtractionChain(adapters=[flaky], offline_fallback=fallback)

    result = await chain.run(Path("/tmp/x.png"), "image/png")

    assert result.backend == "local"
    assert flaky.calls == [(Path("/tmp/x.png"), "image/png")]


async def test_fallback_failure_propagates() -> None:
    bad_fallback = _StubAdapter(
        "local", {"*"}, raise_on_extract=RuntimeError("fallback failed")
    )
    chain = ExtractionChain(adapters=[], offline_fallback=bad_fallback)

    with pytest.raises(RuntimeError, match="fallback failed"):
        await chain.run(Path("/tmp/x"), "text/plain")


async def test_per_mime_override_with_unknown_name_falls_through(
    fallback: _StubAdapter,
) -> None:
    primary = _StubAdapter("primary", {"image/png"})
    chain = ExtractionChain(
        adapters=[primary],
        offline_fallback=fallback,
        per_mime_override={"image/png": "no-such-adapter"},
    )

    result = await chain.run(Path("/tmp/x.png"), "image/png")

    # Unknown override → search the chain → primary matches.
    assert result.backend == "primary"


# --- build_chain factory --------------------------------------------------


def test_build_chain_local_only() -> None:
    settings = _settings(extraction_chain=["local"])
    chain = build_chain(settings)

    assert len(chain._adapters) == 1
    assert chain._adapters[0].name == "local"
    assert chain._offline_fallback.name == "local"


def test_build_chain_skips_gemini_when_no_api_key() -> None:
    settings = _settings(extraction_chain=["gemini-flash", "local"])
    chain = build_chain(settings)

    # gemini-flash is silently dropped (no key) but local is included.
    names = [a.name for a in chain._adapters]
    assert "gemini-flash" not in names
    assert "local" in names


def test_build_chain_includes_gemini_with_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Patch GeminiFlashExtractor so we don't need a real google.genai Client.
    from ee.cloud.extraction import gemini_flash as gem_mod

    sentinel = _StubAdapter("gemini-flash", {"image/png"}, requires_network=True)
    monkeypatch.setattr(
        gem_mod, "GeminiFlashExtractor", lambda api_key, **kw: sentinel
    )

    settings = _settings(
        extraction_chain=["gemini-flash", "local"], gemini_api_key="fake-key"
    )
    chain = build_chain(settings)

    names = [a.name for a in chain._adapters]
    assert names == ["gemini-flash", "local"]


def test_build_chain_unknown_adapter_raises() -> None:
    settings = _settings(extraction_chain=["bogus-name"])

    with pytest.raises(ValueError, match="unknown extraction adapter"):
        build_chain(settings)


def test_build_chain_propagates_per_mime_override() -> None:
    settings = _settings(
        extraction_chain=["local"],
        extraction_per_mime={"image/png": "local"},
    )
    chain = build_chain(settings)
    assert chain._per_mime == {"image/png": "local"}
