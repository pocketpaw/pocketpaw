# tests/cloud/chat/test_attachment_not_on_disk_notice.py
# Created: 2026-09-14 (fix/attachment-not-on-disk) — pins that the
# ``<uploaded-files>`` block tells the agent its contents are INLINED and that
# there is no filesystem copy to go looking for.
#
# The bug this file exists for: "document upload works on pydantic-ai but the
# claude_agent_sdk backend says there is no file."
#
# Both backends receive the SAME assembled system prompt — the attachment text
# is inlined into ``knowledge_context`` by ``_build_attachments_block`` and
# reaches every backend through the shared prompt-layer assembler. What differs
# is the TOOL SURFACE:
#
#   * ``pydantic_ai`` is dispatch-only — it executes no local fs tools, so the
#     inlined text is the only copy of the file it can possibly read. It works.
#   * ``claude_sdk`` grants Bash / Read / Write / Edit / Glob / Grep and runs
#     the CLI in a real ``cwd`` jail (``_resolve_cwd``). Asked about "the file I
#     uploaded", a filesystem-native agent reaches for Read/Glob, finds an empty
#     jail, and reports back that no such file exists — while the extracted text
#     sits in its own system prompt.
#
# The block told the model to "treat them as part of the user's message, not as
# external reference", which settles how to WEIGH the text. It never said the
# files have no path on disk, so the one backend equipped to go looking did.
#
# What this proves: the block names the inlined-not-on-disk contract explicitly,
# so the tool-equipped backend has a reason not to search for a path that was
# never written.

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
    def __init__(self, texts: dict[str, str]) -> None:
        self._texts = texts

    def open_local_for_url(self, url, *, workspace):  # noqa: ARG002
        text = self._texts[url]

        @contextlib.asynccontextmanager
        async def _cm():
            yield (
                FakeRec(
                    id=url, filename="quarterly-report.pdf", mime="application/pdf", size=len(text)
                ),
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
async def test_block_says_the_contents_are_inlined_not_on_disk(monkeypatch) -> None:
    """The tool-equipped backend must be told there is no path to Read."""
    _install(monkeypatch, {"upload://r1": "Revenue grew 12% quarter over quarter."})

    block = await agent_service._build_attachments_block(
        FakeCtx(), [{"url": "upload://r1"}], surface="chat"
    )

    assert "<uploaded-files>" in block
    # The extracted text is there — that half always worked.
    assert "Revenue grew 12%" in block

    lowered = block.lower()
    # The contract the filesystem-capable backend was missing: these files have
    # no path, so do not go looking for one.
    assert "not on the filesystem" in lowered or "no copy on disk" in lowered, (
        "block must state the files are not on disk"
    )
    # And it must name the tools the agent would otherwise reach for, because a
    # generic 'inlined' claim did not stop it — it has Read/Glob and a real cwd.
    assert "read" in lowered and "glob" in lowered, (
        "block must tell the agent not to use Read/Glob to find the upload"
    )
