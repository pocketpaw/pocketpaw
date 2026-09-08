# tests/cloud/chat/test_attachment_inline_budget.py
# Created: 2026-09-08 (fix/attachment-only-turns) — pins the size of a pasted
# brief that survives the trip into the prompt intact.
#
# Under test: ``chat.agent_service._build_attachments_block`` and the two caps
# it enforces.
#
# WHY. The composer converts any bulky paste into a ``pasted-*.txt``
# attachment, so "I pasted my brief into the chat" and "I uploaded a file" are
# the SAME code path. The per-file cap was 8000 chars (~1200 words), which a
# real landing-page brief clears easily, and the agent dutifully reported that
# the paste "was truncated mid Section 1" and reconstructed the rest. The user
# had not truncated anything; we had. The caps still exist — one huge PDF must
# not eat the window — they are just set above the size of the thing people
# actually paste.
#
# What these prove:
#   * a 30k-char brief arrives whole, with no truncation marker;
#   * something genuinely enormous IS still cut, and says so;
#   * the total cap still bounds a batch, so the per-file rise cannot be
#     multiplied by five files into an unbounded prompt.

from __future__ import annotations

import contextlib
from dataclasses import dataclass

import pytest
from pocketpaw_ee.cloud.chat import agent_service


@dataclass
class FakeRec:
    id: str
    filename: str
    mime: str
    size: int


@dataclass
class FakeCtx:
    workspace_id: str = "ws1"
    user_id: str = "u1"
    pocket_id: str | None = "pk1"


class _FakeResolver:
    """Hands back a record per url without touching S3 or the filesystem."""

    def __init__(self, texts: dict[str, str]) -> None:
        self._texts = texts

    def open_local_for_url(self, url, *, workspace):  # noqa: ARG002
        text = self._texts[url]

        @contextlib.asynccontextmanager
        async def _cm():
            yield (
                FakeRec(id=url, filename=f"{url}.txt", mime="text/plain", size=len(text)),
                url,
            )

        return _cm()


class _FakeChain:
    def __init__(self, texts: dict[str, str]) -> None:
        self._texts = texts

    async def run(self, path, _mime):
        return type("R", (), {"text": self._texts[path]})()


def _install(monkeypatch, texts: dict[str, str]) -> None:
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.uploads.resolver.default_resolver",
        lambda: _FakeResolver(texts),
        raising=False,
    )
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.extraction.build_chain",
        lambda _settings: _FakeChain(texts),
        raising=False,
    )


@pytest.mark.asyncio
async def test_a_long_pasted_brief_is_inlined_whole(monkeypatch) -> None:
    """30k chars is a big brief, not an abusive upload. It goes in intact."""
    brief = "SECTION 1. " + ("the hero copy goes here. " * 1200) + "END."
    assert len(brief) > 30_000
    _install(monkeypatch, {"upload://brief": brief})

    block = await agent_service._build_attachments_block(
        FakeCtx(), [{"url": "upload://brief"}], surface="sites"
    )

    assert "[truncated]" not in block
    assert brief in block
    assert "treat them as part of the user's message" in block


@pytest.mark.asyncio
async def test_something_genuinely_enormous_is_still_cut_and_says_so(monkeypatch) -> None:
    """The cap is raised, not removed — and the marker stays, so the agent can
    tell the user it only saw part of the file instead of quietly inventing the
    rest."""
    huge = "x" * (agent_service._ATTACHMENT_PER_FILE_CHARS + 5_000)
    _install(monkeypatch, {"upload://huge": huge})

    block = await agent_service._build_attachments_block(
        FakeCtx(), [{"url": "upload://huge"}], surface="sites"
    )

    assert "[truncated]" in block
    assert len(block) < len(huge)


@pytest.mark.asyncio
async def test_the_total_cap_still_bounds_a_batch(monkeypatch) -> None:
    """Five files at the per-file cap must not multiply into the whole window."""
    each = "y" * agent_service._ATTACHMENT_PER_FILE_CHARS
    texts = {f"upload://f{i}": each for i in range(5)}
    _install(monkeypatch, texts)

    block = await agent_service._build_attachments_block(
        FakeCtx(), [{"url": u} for u in texts], surface="sites"
    )

    # Headers and the wrapper add a little; the inlined TEXT is what is bounded.
    assert len(block) < agent_service._ATTACHMENT_TOTAL_CHARS + 2_000
