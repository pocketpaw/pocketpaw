# Tests for OSS/local-mode artifact parity (ART-OSS).
# Covers three seams of the new pipeline:
#   1. uploads.artifact_delivery.upload_local_artifact — a produced local file
#      lands in the shared uploads store and returns {file_id,name,mime,size},
#      with the relaxed mime gate accepting a non-default type and best-effort
#      None on a missing path.
#   2. AgentLoop — a turn that produces media emits one ``artifact`` SystemEvent
#      per file and persists {type:"artifact", meta} attachments on the assistant
#      message, INCLUDING an artifact-only (empty-text) turn.
#   3. _APISessionBridge — an ``artifact`` SystemEvent is forwarded as an
#      ``artifact`` SSE event carrying just the frozen meta.

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketpaw.agents.loop import AgentLoop
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.bus import Channel, InboundMessage


# ── 1. upload_local_artifact ────────────────────────────────────────────────
class TestUploadLocalArtifact:
    @pytest.mark.asyncio
    async def test_uploads_and_returns_meta(self, tmp_path, monkeypatch):
        from pocketpaw.uploads import artifact_delivery

        store_root = tmp_path / "uploads"
        monkeypatch.setattr(artifact_delivery, "_UPLOADS_ROOT", store_root)
        monkeypatch.setattr(artifact_delivery, "_UPLOADS_INDEX", store_root / "_idx.jsonl")

        produced = tmp_path / "report.csv"
        produced.write_text("a,b,c\n1,2,3\n")

        meta = await artifact_delivery.upload_local_artifact(str(produced))

        assert meta is not None
        assert meta["name"] == "report.csv"
        assert meta["mime"] == "text/csv"
        assert meta["size"] == produced.stat().st_size
        assert meta["file_id"]
        # The row landed in the SAME index the /uploads router serves from, so
        # the file_id is resolvable through the client's grant/download flow.
        assert (store_root / "_idx.jsonl").exists()

    @pytest.mark.asyncio
    async def test_relaxed_mime_accepts_non_default_type(self, tmp_path, monkeypatch):
        from pocketpaw.uploads import artifact_delivery

        store_root = tmp_path / "uploads"
        monkeypatch.setattr(artifact_delivery, "_UPLOADS_ROOT", store_root)
        monkeypatch.setattr(artifact_delivery, "_UPLOADS_INDEX", store_root / "_idx.jsonl")

        # .bin → application/octet-stream, which is NOT in DEFAULT_ALLOWED_MIMES.
        # The per-call relaxed allowlist (the file's own guessed mime) must accept
        # it — a first-party artifact can be any type the agent produced.
        produced = tmp_path / "bundle.bin"
        produced.write_bytes(b"\x00\x01\x02binary-blob\x03")

        meta = await artifact_delivery.upload_local_artifact(str(produced))

        assert meta is not None
        assert meta["name"] == "bundle.bin"
        assert meta["mime"] == "application/octet-stream"

    @pytest.mark.asyncio
    async def test_missing_path_returns_none(self, tmp_path, monkeypatch):
        from pocketpaw.uploads import artifact_delivery

        monkeypatch.setattr(artifact_delivery, "_UPLOADS_ROOT", tmp_path / "uploads")
        monkeypatch.setattr(
            artifact_delivery, "_UPLOADS_INDEX", tmp_path / "uploads" / "_idx.jsonl"
        )

        assert await artifact_delivery.upload_local_artifact(str(tmp_path / "nope.png")) is None


# ── loop harness ────────────────────────────────────────────────────────────
@pytest.fixture
def mock_bus():
    bus = MagicMock()
    bus.consume_inbound = AsyncMock()
    bus.publish_outbound = AsyncMock()
    bus.publish_system = AsyncMock()
    return bus


@pytest.fixture
def mock_memory():
    mem = MagicMock()
    mem.add_to_session = AsyncMock(return_value="entry-id")
    mem.get_session_history = AsyncMock(return_value=[])
    mem.get_compacted_history = AsyncMock(return_value=[])
    mem.resolve_session_key = AsyncMock(side_effect=lambda k: k)
    return mem


def _settings() -> MagicMock:
    s = MagicMock()
    s.agent_backend = "claude_agent_sdk"
    s.max_concurrent_conversations = 5
    s.welcome_hint_enabled = False
    s.injection_scan_enabled = False
    s.pii_scan_enabled = False
    s.pii_scan_memory = False
    s.soul_enabled = False
    s.voice_reply_enabled = False
    s.tool_profile = "full"
    return s


async def _run_loop_with(router, mock_bus, mock_memory, artifact_meta):
    """Drive one message through AgentLoop with ``_deliver_oss_artifact`` stubbed
    to return ``artifact_meta`` (a dict) for every produced path. Returns the loop
    so the caller can inspect the mocked bus/memory."""
    with (
        patch("pocketpaw.agents.loop.get_message_bus", return_value=mock_bus),
        patch("pocketpaw.agents.loop.get_memory_manager", return_value=mock_memory),
        patch("pocketpaw.agents.loop.AgentContextBuilder") as builder_cls,
        patch("pocketpaw.agents.loop.AgentRouter", return_value=router),
        patch("pocketpaw.agents.loop.get_settings", return_value=_settings()),
        patch("pocketpaw.agents.loop.Settings") as settings_cls,
    ):
        settings_cls.load.return_value = _settings()
        builder_cls.return_value.build_system_prompt = AsyncMock(return_value="SP")

        loop = AgentLoop()
        loop._deliver_oss_artifact = AsyncMock(return_value=artifact_meta)

        msg = InboundMessage(
            channel=Channel.WEBSOCKET,
            sender_id="user1",
            chat_id="chat1",
            content="make me a file",
        )
        await loop._process_message(msg)
        return loop


def _artifact_events(mock_bus) -> list[dict]:
    return [
        c.args[0].data
        for c in mock_bus.publish_system.call_args_list
        if c.args and getattr(c.args[0], "event_type", None) == "artifact"
    ]


def _assistant_call(mock_memory):
    for c in mock_memory.add_to_session.call_args_list:
        if c.kwargs.get("role") == "assistant":
            return c
    return None


# ── 2. AgentLoop artifact emission + persistence ─────────────────────────────
class TestLoopArtifactEmission:
    @pytest.mark.asyncio
    async def test_media_tag_emits_event_and_persists_attachment(self, mock_bus, mock_memory):
        meta = {"file_id": "fid1", "name": "chart.png", "mime": "image/png", "size": 42}

        async def run(message, *, system_prompt=None, history=None, session_key=None):
            yield AgentEvent(type="message", content="Here is your chart.")
            yield AgentEvent(
                type="tool_result",
                content="<!-- media:/tmp/chart.png -->",
                metadata={"name": "deliver_artifact"},
            )
            yield AgentEvent(type="done", content="")

        router = MagicMock()
        router.run = run
        router.stop = AsyncMock()

        await _run_loop_with(router, mock_bus, mock_memory, meta)

        events = _artifact_events(mock_bus)
        assert len(events) == 1
        assert events[0]["file_id"] == "fid1"
        assert events[0]["name"] == "chart.png"
        assert events[0]["session_key"].endswith(":chat1")

        assistant = _assistant_call(mock_memory)
        assert assistant is not None
        attachments = (assistant.kwargs.get("metadata") or {}).get("attachments")
        assert attachments == [{"type": "artifact", "meta": meta}]

    @pytest.mark.asyncio
    async def test_artifact_only_turn_persists_assistant_message(self, mock_bus, mock_memory):
        # The agent delivered a file with NO prose — the assistant message must
        # still be persisted so its artifact attachment survives a reload.
        meta = {"file_id": "fid2", "name": "export.zip", "mime": "application/zip", "size": 9}

        async def run(message, *, system_prompt=None, history=None, session_key=None):
            yield AgentEvent(
                type="tool_result",
                content="<!-- media:/tmp/export.zip -->",
                metadata={"name": "deliver_artifact"},
            )
            yield AgentEvent(type="done", content="")

        router = MagicMock()
        router.run = run
        router.stop = AsyncMock()

        await _run_loop_with(router, mock_bus, mock_memory, meta)

        assert len(_artifact_events(mock_bus)) == 1
        assistant = _assistant_call(mock_memory)
        assert assistant is not None
        assert assistant.kwargs.get("content") == ""
        attachments = (assistant.kwargs.get("metadata") or {}).get("attachments")
        assert attachments == [{"type": "artifact", "meta": meta}]

    @pytest.mark.asyncio
    async def test_no_media_no_artifact_and_no_attachment(self, mock_bus, mock_memory):
        async def run(message, *, system_prompt=None, history=None, session_key=None):
            yield AgentEvent(type="message", content="Just a plain answer.")
            yield AgentEvent(type="done", content="")

        router = MagicMock()
        router.run = run
        router.stop = AsyncMock()

        await _run_loop_with(router, mock_bus, mock_memory, {"file_id": "x"})

        assert _artifact_events(mock_bus) == []
        assistant = _assistant_call(mock_memory)
        assert assistant is not None
        # No attachments → metadata omitted (None) on a plain text turn.
        assert assistant.kwargs.get("metadata") is None


# ── 3. bridge translation ────────────────────────────────────────────────────
class TestBridgeArtifactEvent:
    @pytest.mark.asyncio
    async def test_artifact_system_event_becomes_sse_event(self):
        from pocketpaw.api.v1.chat import _APISessionBridge
        from pocketpaw.bus import get_message_bus
        from pocketpaw.bus.events import SystemEvent

        bridge = _APISessionBridge("chatX")
        await bridge.start()
        try:
            await get_message_bus().publish_system(
                SystemEvent(
                    event_type="artifact",
                    data={
                        "file_id": "fid9",
                        "name": "doc.pdf",
                        "mime": "application/pdf",
                        "size": 123,
                        "session_key": "websocket:chatX",
                        "trace_id": "t1",
                    },
                )
            )
            item = bridge.queue.get_nowait()
        finally:
            await bridge.stop()

        assert item["event"] == "artifact"
        assert item["data"] == {
            "file_id": "fid9",
            "name": "doc.pdf",
            "mime": "application/pdf",
            "size": 123,
        }

    @pytest.mark.asyncio
    async def test_artifact_event_for_other_session_is_filtered(self):
        from pocketpaw.api.v1.chat import _APISessionBridge
        from pocketpaw.bus import get_message_bus
        from pocketpaw.bus.events import SystemEvent

        bridge = _APISessionBridge("mine")
        await bridge.start()
        try:
            await get_message_bus().publish_system(
                SystemEvent(
                    event_type="artifact",
                    data={"file_id": "f", "session_key": "websocket:other"},
                )
            )
            assert bridge.queue.empty()
        finally:
            await bridge.stop()
