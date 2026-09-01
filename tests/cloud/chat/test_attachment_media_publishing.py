# tests/cloud/chat/test_attachment_media_publishing.py — the surface gate on
# republishing a chat attachment to the WORLD-READABLE bucket.
# Created 2026-08-31 (feat/sites-public-asset-uploads). New file.
#
# Under test: ``chat.agent_service._publish_media_attachment``.
#
# WHY THIS IS ITS OWN FILE. Every other attachment in this codebase lands on the
# private, auth-gated rail. This function is the one place that copies a user's
# upload somewhere anyone with the URL can read it, forever. That is a privacy
# boundary, and the thing guarding it is a single frozenset of surface names — so
# it gets asserted directly rather than incidentally.
#
# What these prove:
#   * only a sites surface publishes; /files, /studio, an unknown surface and a
#     MISSING surface all decline (the last matters most — a new caller that
#     forgets to thread `surface` must fail closed, not publish);
#   * a document is never published even on sites, so a PRD attached while
#     describing a site cannot end up on an unauthenticated URL;
#   * a pre-create send (no pocket yet) still scopes its key to the tenant
#     rather than dropping into a shared prefix;
#   * an unconfigured deployment TELLS the agent so, instead of leaving it to
#     invent a URL;
#   * a storage failure degrades to "no public url" and never breaks the turn.

from __future__ import annotations

from dataclasses import dataclass

import pytest
from pocketpaw_ee.cloud.chat import agent_service
from pocketpaw_ee.sites.public_assets import PublicAsset


@dataclass
class FakeRec:
    filename: str
    mime: str
    size: int


@dataclass
class FakeCtx:
    workspace_id: str = "ws1"
    user_id: str = "u1"
    pocket_id: str | None = "pk1"


class FakeStore:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def put(self, data, *, filename, workspace_id, pocket_id):
        self.calls.append(
            {"filename": filename, "workspace_id": workspace_id, "pocket_id": pocket_id}
        )
        return PublicAsset(
            key=f"sites-assets/{workspace_id}/{pocket_id}/abc-{filename}",
            url=f"https://cdn.test/sites-assets/{workspace_id}/{pocket_id}/abc-{filename}",
            mime="image/png",
            size=len(data),
            filename=filename,
            sha256="a" * 16,
            kind="image",
        )


@pytest.fixture
def png(tmp_path):
    p = tmp_path / "logo.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    return p


def _install(monkeypatch, store):
    monkeypatch.setattr(
        "pocketpaw_ee.sites.public_assets.public_asset_store", lambda: store, raising=False
    )


# ── The surface gate ────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["files", "studio", "code", "pockets", "", None])
async def test_non_sites_surfaces_never_publish(monkeypatch, png, surface) -> None:
    """A chat attachment stays private everywhere except sites.

    ``None`` is in this list deliberately: a future caller that forgets to
    thread ``surface`` must fail closed rather than start publishing.
    """
    store = FakeStore()
    _install(monkeypatch, store)

    out = await agent_service._publish_media_attachment(
        FakeCtx(), FakeRec("logo.png", "image/png", 40), png, surface=surface
    )

    assert out is None
    assert store.calls == [], "nothing may be written to the public bucket"


@pytest.mark.asyncio
async def test_a_sites_image_is_published_with_an_embeddable_instruction(monkeypatch, png) -> None:
    store = FakeStore()
    _install(monkeypatch, store)

    out = await agent_service._publish_media_attachment(
        FakeCtx(), FakeRec("logo.png", "image/png", 40), png, surface="sites"
    )

    assert out is not None
    assert "PUBLIC URL: https://cdn.test/" in out
    assert "<img src=" in out
    # The agent must be told not to swap it for stock — that is the failure the
    # whole feature exists to prevent.
    assert "do not substitute a stock asset" in out
    assert store.calls[0]["workspace_id"] == "ws1"


@pytest.mark.asyncio
async def test_a_document_is_never_published_even_on_sites(monkeypatch, tmp_path) -> None:
    """THE leak guard: a PRD is agent input, not site content."""
    doc = tmp_path / "prd.pdf"
    doc.write_bytes(b"%PDF-1.7\n")
    store = FakeStore()
    _install(monkeypatch, store)

    out = await agent_service._publish_media_attachment(
        FakeCtx(), FakeRec("prd.pdf", "application/pdf", 9), doc, surface="sites"
    )

    assert out is None
    assert store.calls == []


@pytest.mark.asyncio
async def test_a_pre_create_send_still_scopes_the_key_to_the_tenant(monkeypatch, png) -> None:
    """Attaching a logo while DESCRIBING a site happens before a pocket exists."""
    store = FakeStore()
    _install(monkeypatch, store)

    await agent_service._publish_media_attachment(
        FakeCtx(pocket_id=None), FakeRec("logo.png", "image/png", 40), png, surface="sites"
    )

    assert store.calls[0]["workspace_id"] == "ws1"
    assert store.calls[0]["pocket_id"] == "_user-u1", "must not land in a shared prefix"


@pytest.mark.asyncio
async def test_an_unconfigured_deployment_says_so_rather_than_going_quiet(monkeypatch, png) -> None:
    """Silence here is what makes an agent invent a URL."""
    monkeypatch.setattr(
        "pocketpaw_ee.sites.public_assets.public_asset_store", lambda: None, raising=False
    )

    out = await agent_service._publish_media_attachment(
        FakeCtx(), FakeRec("logo.png", "image/png", 40), png, surface="sites"
    )

    assert out is not None
    assert "no public asset storage" in out
    assert "inventing a URL" in out


@pytest.mark.asyncio
async def test_publishing_does_not_depend_on_text_extraction(monkeypatch, png) -> None:
    """ORDERING GUARD — publishing must happen BEFORE the extraction chain runs.

    Measured 2026-09-01 against a real 200 KB jpeg: the local extraction adapter
    reaches for OCR and raises TesseractNotFoundError on any box without
    tesseract. The attachment loop catches that and `continue`s, so a publish
    placed after it never executes — the user attached a logo and the agent was
    told nothing at all.

    This drives the whole block with a chain that always raises, and asserts the
    image still comes back with its public URL. Move the publish call back below
    the extraction call and this fails.
    """
    store = FakeStore()
    _install(monkeypatch, store)

    rec = FakeRec("logo.png", "image/png", 40)

    class Resolved:
        async def __aenter__(self):
            return (rec, png)

        async def __aexit__(self, *_exc):
            return False

    class Resolver:
        def open_local_for_url(self, *_a, **_kw):
            return Resolved()

    class ExplodingChain:
        async def run(self, *_a, **_kw):
            raise RuntimeError("tesseract is not installed or it's not in your PATH")

    # Both are imported INSIDE the function, so patch them at their source.
    monkeypatch.setattr("pocketpaw_ee.cloud.uploads.resolver.default_resolver", lambda: Resolver())
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.extraction.build_chain", lambda *_a, **_kw: ExplodingChain()
    )

    block = await agent_service._build_attachments_block(
        FakeCtx(), [{"url": "/api/v1/uploads/abc"}], surface="sites"
    )

    assert "PUBLIC URL:" in block, "an image must publish even when extraction cannot run"
    assert store.calls, "the bytes must reach the public bucket"


@pytest.mark.asyncio
async def test_a_storage_failure_degrades_and_never_breaks_the_turn(monkeypatch, png) -> None:
    class Boom:
        async def put(self, *_a, **_kw):
            raise RuntimeError("bucket unreachable")

    _install(monkeypatch, Boom())

    out = await agent_service._publish_media_attachment(
        FakeCtx(), FakeRec("logo.png", "image/png", 40), png, surface="sites"
    )

    assert out is None, "falls back to the ordinary extracted-text entry"
