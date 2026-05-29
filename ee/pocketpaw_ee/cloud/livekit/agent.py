"""LiveKit Call Agent — meeting notes bot with Deepgram STT.

Architecture
------------
This module provides a lightweight agent that connects to a LiveKit room
as a silent listener participant and transcribes the conversation using
Deepgram's speech-to-text API.

1. Connects to the LiveKit room via ``livekit.rtc.Room`` (WebRTC)
2. Subscribes to all remote audio tracks
3. Pipes each track through Deepgram STT streaming for real-time transcription
4. Accumulates transcript segments with speaker identification
5. Detects when the room empties (via polling + room events)
6. Generates a meeting summary using the configured LLM
7. Posts the meeting notes to the group chat

Usage
-----
The agent is started automatically by the ``LiveKitService`` when a room
is created, or can be run as a standalone script:

    python -m ee.cloud.livekit.agent --room group-call-abc123

Deepgram Configuration
----------------------
Requires the ``DEEPGRAM_API_KEY`` environment variable (set in ``.env``).
The Deepgram STT uses Nova-3 with ``language="multi"`` for multilingual
transcription, supporting all Deepgram languages simultaneously.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# How long to wait (seconds) after the last participant leaves before ending
_CALL_END_GRACE_SECONDS = 5

# How often (seconds) to poll room state
_MONITOR_POLL_INTERVAL = 5


# ---------------------------------------------------------------------------
# Meeting Notes Agent
# ---------------------------------------------------------------------------


class CallMeetingAgent:
    """An agent that listens to a LiveKit call and generates meeting notes.

    Connects to the LiveKit room as a silent listener, transcribes all
    participants' speech via Deepgram STT, and posts a summary to the
    group when the call ends.
    """

    def __init__(
        self,
        group_id: str,
        room_name: str,
        bot_token: str,
        livekit_url: str = "",
    ) -> None:
        self.group_id = group_id
        self.room_name = room_name
        self.bot_token = bot_token
        self.livekit_url = livekit_url

        # Accumulated transcript segments
        self.transcript_segments: list[dict[str, Any]] = []
        self._participant_identities: set[str] = set()
        self._has_ever_had_humans: bool = False
        self._call_start_time: float = 0.0

        # Tasks
        self._running = False
        self._monitor_task: asyncio.Task | None = None
        self._transcribe_task: asyncio.Task | None = None

        # LiveKit RTC room (set when connected)
        self._rtc_room: Any = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the meeting agent.

        Connects to the LiveKit room as a listener, begins Deepgram
        transcription of all participants, and starts monitoring for
        room emptiness.
        """
        self._running = True
        self._call_start_time = time.time()

        logger.info(
            "CallMeetingAgent started for room %s (group %s)",
            self.room_name,
            self.group_id,
        )

        # Monitor room emptiness (polling-based)
        self._monitor_task = asyncio.create_task(self._monitor_room())

        # Connect to the LiveKit room and begin transcription
        self._transcribe_task = asyncio.create_task(self._connect_and_transcribe())

    async def stop(self) -> None:
        """Stop the agent and generate meeting notes."""
        self._running = False

        # Disconnect RTC room first (stops audio streams)
        await self._disconnect_rtc()

        # Cancel monitor task
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        # Cancel transcribe task
        if self._transcribe_task:
            self._transcribe_task.cancel()
            try:
                await self._transcribe_task
            except asyncio.CancelledError:
                pass

        await self._finalize_notes()

        logger.info(
            "CallMeetingAgent stopped for room %s (group %s)",
            self.room_name,
            self.group_id,
        )

    def add_transcript_segment(
        self,
        speaker: str,
        text: str,
        timestamp: float | None = None,
    ) -> None:
        """Add a transcribed speech segment."""
        self.transcript_segments.append(
            {
                "speaker": speaker,
                "text": text,
                "timestamp": timestamp or time.time(),
            }
        )

    # ------------------------------------------------------------------
    # Internal: LiveKit RTC connection + Deepgram transcription
    # ------------------------------------------------------------------

    async def _connect_and_transcribe(self) -> None:
        """Connect to the LiveKit room and transcribe all audio via Deepgram.

        Architecture
        ------------
        This method connects to the LiveKit room as a silent listener using
        the ``livekit.rtc.Room`` WebRTC participant. Instead of relying on
        the ``track_subscribed`` event (which may not fire reliably for
        agent-type participants), it uses ``AudioStream.from_participant()``
        which tells the LiveKit FFI layer to subscribe directly to a
        participant's microphone audio source.

        For each remote participant:
        1. ``AudioStream.from_participant(participant, SOURCE_MICROPHONE)``
           subscribes at the FFI level and returns an async iterable of
           ``AudioFrame`` objects.
        2. The audio frames are piped into a ``DeepgramSTT.stream()`` for
           real-time transcription.
        3. ``FINAL_TRANSCRIPT`` events are collected and stored as transcript
           segments for the meeting notes.
        """
        import aiohttp

        try:
            from livekit.plugins.deepgram import STT as DeepgramSTT
            from livekit.rtc import (
                AudioStream,
                RemoteParticipant,
                Room,
                RoomOptions,
                TrackSource,
            )

            room_opts = RoomOptions(auto_subscribe=True)
            room = Room(loop=asyncio.get_event_loop())
            self._rtc_room = room

            # Track STT streams keyed by participant identity
            stt_streams: dict[str, Any] = {}
            pipe_tasks: dict[str, asyncio.Task] = {}

            # Shared HTTP session for Deepgram (avoids "outside job context" error)
            http_session = aiohttp.ClientSession()

            # ------------------------------------------------------------------
            # Helper: create an AudioStream + Deepgram STT pipe for a participant
            # ------------------------------------------------------------------
            async def _setup_audio_pipe_for_participant(
                participant: RemoteParticipant,
            ) -> None:
                """Subscribe to a participant's mic and pipe audio to Deepgram.

                Uses ``AudioStream.from_participant()`` which handles the
                subscription at the FFI level — no need to wait for a
                ``track_subscribed`` event.
                """
                pid = participant.identity
                if pid == "call-bot" or pid in stt_streams:
                    return  # already have a stream for this participant

                pname = participant.name or pid
                self._participant_identities.add(pid)
                logger.info("Agent: setting up audio pipe for %s (from_participant)", pname)

                try:
                    # 1. Create Deepgram STT stream
                    stt = DeepgramSTT(
                        language="multi",
                        interim_results=False,
                        punctuate=True,
                        smart_format=True,
                        sample_rate=16000,
                        http_session=http_session,
                    )
                    stt_stream = stt.stream()
                    stt_streams[pid] = stt_stream

                    # 2. Create AudioStream directly from the participant's mic
                    #    This tells the FFI to subscribe to the audio track
                    #    — no need for track_subscribed event!
                    audio_stream = AudioStream.from_participant(
                        participant=participant,
                        track_source=TrackSource.SOURCE_MICROPHONE,
                        sample_rate=16000,
                        num_channels=1,
                    )

                    # 3. Pipe audio frames -> Deepgram
                    pipe_tasks[pid] = asyncio.create_task(
                        self._pipe_audio_to_stt(
                            audio_stream,
                            stt_stream,
                            pid,
                            pname,
                        )
                    )

                    # 4. Collect transcription results
                    asyncio.create_task(self._collect_stt_results(stt_stream, pid, pname))

                    logger.info(
                        "Agent: audio pipe established for %s via from_participant",
                        pname,
                    )

                except Exception as exc:
                    logger.error(
                        "Failed to create audio pipe for %s: %s",
                        pid,
                        exc,
                    )

            # ------------------------------------------------------------------
            # Event handlers (set up BEFORE connect to avoid race conditions)
            # ------------------------------------------------------------------

            @room.on("participant_connected")
            def on_participant_connected(participant: RemoteParticipant) -> None:
                """Handle a participant joining the room after the agent."""
                pid = participant.identity
                if pid == "call-bot":
                    return
                logger.info(
                    "Agent: participant connected after agent: %s (%s)",
                    participant.name or pid,
                    pid,
                )
                asyncio.create_task(_setup_audio_pipe_for_participant(participant))

            @room.on("participant_disconnected")
            def on_participant_disconnected(participant: RemoteParticipant) -> None:
                """Clean up when a participant leaves."""
                pid = participant.identity
                stt_streams.pop(pid, None)
                pipe_tasks.pop(pid, None)
                logger.info(
                    "Agent: participant disconnected: %s",
                    pid,
                )

            @room.on("track_subscribed")
            def on_track_subscribed(
                track: Any,
                publication: Any,
                participant: Any,
            ) -> None:
                """Fallback: handle any tracks that get auto-subscribed.

                This is a safety net in case ``from_participant`` doesn't
                cover all scenarios. We only act if we don't already have
                a pipe for this participant.
                """
                if track.kind != "audio":
                    return
                pid = participant.identity if participant else ""
                if not pid or pid == "call-bot":
                    return
                if pid in stt_streams:
                    return  # already set up via from_participant
                pname = participant.name or pid
                logger.info(
                    "Agent: track_subscribed fallback for %s",
                    pname,
                )
                asyncio.create_task(_setup_audio_pipe_for_participant(participant))

            # ------------------------------------------------------------------
            # Connect to the LiveKit room
            # ------------------------------------------------------------------

            if not self.livekit_url:
                logger.warning("No livekit_url provided, skipping RTC connection")
                await http_session.close()
                return

            logger.info("Agent connecting to LiveKit room %s", self.room_name)
            await room.connect(self.livekit_url, self.bot_token, room_opts)
            logger.info(
                "Agent connected to LiveKit room %s (participants: %d)",
                self.room_name,
                len(room.remote_participants),
            )

            # ------------------------------------------------------------------
            # Subscribe to existing participants
            # ------------------------------------------------------------------
            # Participants already in the room won't fire
            # participant_connected, so we must set them up here.

            for pid, participant in list(room.remote_participants.items()):
                if pid == "call-bot":
                    continue
                logger.info(
                    "Agent: setting up pipe for existing participant %s (%s)",
                    participant.name or pid,
                    pid,
                )
                asyncio.create_task(_setup_audio_pipe_for_participant(participant))

                # Also explicitly subscribe to any audio publications as backup
                for pub in participant.track_publications.values():
                    if pub.kind == "audio" and not pub.subscribed:
                        logger.info(
                            "Agent: explicitly subscribing to %s's audio track",
                            participant.name or pid,
                        )
                        pub.set_subscribed(True)

            logger.info(
                "Agent: transcription running for room %s (watching %d participants)",
                self.room_name,
                len(room.remote_participants),
            )

            # ------------------------------------------------------------------
            # Main loop: keep agent alive
            # ------------------------------------------------------------------
            while self._running:
                await asyncio.sleep(1)

        except ImportError as exc:
            logger.warning(
                "Cannot transcribe — livekit-rtc or deepgram not available: %s",
                exc,
            )
        except Exception as exc:
            logger.error(
                "Transcription agent error for room %s: %s",
                self.room_name,
                exc,
            )
            import traceback

            logger.error("Traceback:\n%s", traceback.format_exc())
        finally:
            # Clean up STT streams
            for pid, s in list(stt_streams.items()):
                try:
                    await s.aclose()
                except Exception:
                    pass
            # Clean up the HTTP session
            try:
                await http_session.close()
            except Exception:
                pass

    async def _pipe_audio_to_stt(
        self,
        audio_stream: Any,
        stt_stream: Any,
        participant_id: str,
        participant_name: str,
    ) -> None:
        """Read AudioFrameEvents from an AudioStream and push frames into STT.

        ``AudioStream`` yields ``AudioFrameEvent`` objects (not raw
        ``AudioFrame``), so we extract ``.frame`` before pushing into the
        Deepgram STT stream.
        """
        try:
            async for event in audio_stream:
                if not self._running:
                    break
                try:
                    stt_stream.push_frame(event.frame)
                except Exception:
                    pass  # STT stream might be closed
        except Exception as exc:
            logger.debug(
                "Audio pipe ended for %s: %s",
                participant_id,
                exc,
            )
        finally:
            try:
                stt_stream.end_input()
            except Exception:
                pass

    async def _collect_stt_results(
        self,
        stt_stream: Any,
        participant_id: str,
        participant_name: str,
    ) -> None:
        """Collect FINAL_TRANSCRIPT events from the STT stream."""
        from livekit.agents.stt import SpeechEventType

        try:
            async for event in stt_stream:
                if not self._running:
                    break
                if event.type == SpeechEventType.FINAL_TRANSCRIPT:
                    for alt in event.alternatives:
                        text = alt.text.strip()
                        if text:
                            speaker = alt.speaker_id or participant_name
                            self.add_transcript_segment(
                                speaker=speaker,
                                text=text,
                            )
                            logger.info(
                                "Transcript [%s]: %s",
                                speaker,
                                text,
                            )
        except Exception as exc:
            logger.debug(
                "STT collection ended for %s: %s",
                participant_id,
                exc,
            )

    async def _disconnect_rtc(self) -> None:
        """Disconnect from the LiveKit RTC room."""
        if self._rtc_room is not None:
            try:
                await self._rtc_room.disconnect()
            except Exception as exc:
                logger.debug("Error disconnecting RTC: %s", exc)
            self._rtc_room = None

    # ------------------------------------------------------------------
    # Internal: room monitoring
    # ------------------------------------------------------------------

    async def _monitor_room(self) -> None:
        """Poll room state to detect when the call ends."""
        from pocketpaw_ee.cloud.livekit.service import get_room_info

        empty_since: float | None = None

        while self._running:
            try:
                info = await get_room_info(self.group_id)

                # Count human participants only (exclude the call-bot agent
                # itself, since the agent is always in the room as long as
                # this subprocess is alive).
                human_count = 0
                if info and info.get("participants"):
                    for p in info["participants"]:
                        pid = p.get("identity", "")
                        if pid and pid != "call-bot":
                            human_count += 1
                            self._participant_identities.add(pid)
                            self._has_ever_had_humans = True

                if human_count == 0:
                    # Don't start the empty-room timer until at least one
                    # human has ever been in the room.  Without this guard,
                    # the 5-second grace period begins as soon as the bot
                    # joins (before participants connect), which can destroy
                    # the room while people are still joining.
                    if not self._has_ever_had_humans:
                        empty_since = None
                    elif empty_since is None:
                        empty_since = time.time()
                        logger.info(
                            "Room %s has no human participants, will end in %ds",
                            self.room_name,
                            _CALL_END_GRACE_SECONDS,
                        )
                    elif time.time() - empty_since > _CALL_END_GRACE_SECONDS:
                        logger.info(
                            "Room %s has been empty of humans for %ds, ending call",
                            self.room_name,
                            _CALL_END_GRACE_SECONDS,
                        )
                        # Disconnect the agent from the room, then clean up.
                        # NOTE: do NOT call self.stop() here — stop() cancels
                        # _monitor_task, which is the current task, causing a
                        # self-cancellation that prevents _cleanup_room() from
                        # ever running.
                        await self._disconnect_rtc()
                        self._running = False
                        if self._transcribe_task:
                            self._transcribe_task.cancel()
                            try:
                                await self._transcribe_task
                            except asyncio.CancelledError:
                                pass
                        await self._finalize_notes()
                        await self._cleanup_room()
                        return
                else:
                    empty_since = None

            except Exception as exc:
                logger.warning(
                    "Error monitoring room %s: %s",
                    self.room_name,
                    exc,
                )
                if "not found" in str(exc).lower() or "does not exist" in str(exc).lower():
                    self._running = False
                    return

            await asyncio.sleep(_MONITOR_POLL_INTERVAL)

    async def _cleanup_room(self) -> None:
        """Delete the LiveKit room after a natural call end."""
        try:
            from livekit.api import LiveKitAPI
            from livekit.protocol.room import DeleteRoomRequest

            from pocketpaw_ee.cloud.livekit.service import (
                LIVEKIT_API_KEY,
                LIVEKIT_API_SECRET,
                LIVEKIT_URL,
            )

            async with LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET) as lk:
                req = DeleteRoomRequest(room=self.room_name)
                await lk.room.delete_room(req)
                logger.info(
                    "Cleaned up LiveKit room %s after empty call",
                    self.room_name,
                )
        except Exception as exc:
            logger.warning(
                "Error cleaning up room %s: %s",
                self.room_name,
                exc,
            )

        # The active-agents registry lives in the parent process; the
        # _reap_agent_process background task in service.py will clean up
        # when this subprocess exits — nothing to do here.

    # ------------------------------------------------------------------
    # Notes generation
    # ------------------------------------------------------------------

    async def _finalize_notes(self) -> None:
        """Generate and post meeting notes to the group."""
        logger.warning("Agent: _finalize_notes called")
        duration = int(time.time() - self._call_start_time)

        # Collect participant names from segments + room info
        speakers_seen: set[str] = set()

        if self.transcript_segments:
            transcript_lines = []
            for seg in self.transcript_segments:
                speaker = seg.get("speaker", "Unknown")
                speakers_seen.add(speaker)
                transcript_lines.append(f"[{speaker}]: {seg['text']}")

            transcript_text = "\n".join(transcript_lines)

            # Generate AI summary — non-fatal if it fails; notes still post
            # with raw transcript (or a fallback message).
            try:
                summary, action_items = await self._generate_summary(
                    transcript_text,
                )
            except Exception:
                logger.exception("Agent: AI summarization failed — posting notes without summary")
                summary = "AI summarization unavailable."
                action_items = ["Transcript captured but summarization failed."]
        else:
            transcript_text = ""
            summary = "Call ended with no speech detected."
            action_items = []

        # Merge participant identities from room tracking + transcript
        all_participants = list(self._participant_identities | speakers_seen)

        # ── Emit notes payload to stdout ──
        # Instead of calling post_meeting_notes_to_group directly (which
        # requires Beanie/MongoDB initialized in this subprocess), we write
        # the payload as a JSON line to stdout.  The parent process reads
        # this line and handles the DB write where Beanie is already set up.
        payload = json.dumps(
            {
                "type": "meeting_notes",
                "group_id": self.group_id,
                "transcript": transcript_text,
                "summary": summary,
                "action_items": action_items,
                "participants": all_participants,
                "duration_seconds": duration,
            }
        )
        print(payload, flush=True)
        logger.info(
            "Agent: meeting notes payload written to stdout for group %s",
            self.group_id,
        )

    # ------------------------------------------------------------------
    # AI summarization
    # ------------------------------------------------------------------

    async def _generate_summary(
        self,
        transcript: str,
    ) -> tuple[str, list[str]]:
        """Generate a meeting summary and action items from transcript.

        Uses Anthropic Claude by default, falls back to OpenAI.
        Falls back to heuristic extraction if no LLM is configured.
        """
        if not transcript:
            return "No speech detected during the call.", []

        # Try Anthropic first
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if api_key:
            try:
                return await self._summarize_with_anthropic(transcript, api_key)
            except Exception as exc:
                logger.warning("Anthropic summarization failed: %s", exc)

        # Fall back to OpenAI
        openai_key = os.environ.get("OPENAI_API_KEY", "")
        if openai_key:
            try:
                return await self._summarize_with_openai(transcript, openai_key)
            except Exception as exc:
                logger.warning("OpenAI summarization failed: %s", exc)

        # Final fallback: simple heuristic
        return self._summarize_heuristic(transcript)

    async def _summarize_with_anthropic(
        self,
        transcript: str,
        api_key: str,
    ) -> tuple[str, list[str]]:
        """Use Anthropic Claude to summarize the transcript."""
        import httpx

        prompt = (
            "You are a meeting notes assistant. Given the following transcript of a group call, "
            "provide:\n"
            "1. A concise summary (2-3 paragraphs) of what was discussed\n"
            "2. A list of action items / decisions made\n\n"
            "Format your response as JSON with keys 'summary' (string) and "
            "'action_items' (list of strings).\n\n"
            f"Transcript:\n{transcript}"
        )

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 2000,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            data = resp.json()

        content = data.get("content", [{}])[0].get("text", "")
        return self._parse_summary_json(content)

    async def _summarize_with_openai(
        self,
        transcript: str,
        api_key: str,
    ) -> tuple[str, list[str]]:
        """Use OpenAI GPT to summarize the transcript."""
        import httpx

        prompt = (
            "You are a meeting notes assistant. Given the following transcript of a group call, "
            "provide:\n"
            "1. A concise summary (2-3 paragraphs) of what was discussed\n"
            "2. A list of action items / decisions made\n\n"
            "Format your response as JSON with keys 'summary' (string) and "
            "'action_items' (list of strings).\n\n"
            f"Transcript:\n{transcript}"
        )

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 2000,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return self._parse_summary_json(content)

    def _summarize_heuristic(
        self,
        transcript: str,
    ) -> tuple[str, list[str]]:
        """Simple heuristic summarization fallback."""
        lines = transcript.strip().split("\n")
        summary_lines = lines[:5] if len(lines) > 5 else lines
        summary = " | ".join(summary_lines)
        return summary, []

    def _parse_summary_json(self, content: str) -> tuple[str, list[str]]:
        """Parse JSON summary response from LLM."""
        content = content.strip()
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()

        try:
            parsed = json.loads(content)
            summary = parsed.get("summary", content)
            action_items = parsed.get("action_items", [])
            return summary, action_items
        except (json.JSONDecodeError, KeyError):
            return content, []


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    """Run the LiveKit meeting agent standalone.

    Usage:
        python -m ee.cloud.livekit.agent --group GROUP_ID --room ROOM_NAME --token BOT_TOKEN

    This is useful for production deployments where the agent runs as a
    separate process managed by a process supervisor (e.g., systemd,
    supervisord, Docker).
    """
    import argparse
    import signal as _signal

    parser = argparse.ArgumentParser(description="LiveKit Meeting Notes Agent")
    parser.add_argument("--group", required=True, help="Group ID to post notes to")
    parser.add_argument("--room", required=True, help="LiveKit room name to join")
    parser.add_argument("--token", required=True, help="LiveKit bot token for authentication")
    parser.add_argument("--url", default="", help="LiveKit WebSocket URL")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    async def _main():
        agent = CallMeetingAgent(
            group_id=args.group,
            room_name=args.room,
            bot_token=args.token,
            livekit_url=args.url,
        )

        # ── SIGTERM handler ──
        # The parent server sends SIGTERM when end_room() is called manually.
        # Gracefully finalise notes so the meeting summary is posted before
        # we exit.  The parent (end_room) handles room deletion; we must NOT
        # call _cleanup_room here or it races with the parent's DeleteRoomRequest.
        _sigterm_received = False

        def _on_sigterm() -> None:
            nonlocal _sigterm_received
            _sigterm_received = True
            logger.info("Agent: SIGTERM received — finalising notes")

        _signal.signal(_signal.SIGTERM, lambda sig, frame: _on_sigterm())

        await agent.start()
        while agent._running:
            if _sigterm_received:
                logger.info("Agent: SIGTERM — finalising notes")
                agent._running = False
                await agent._disconnect_rtc()
                if agent._transcribe_task:
                    agent._transcribe_task.cancel()
                    try:
                        await agent._transcribe_task
                    except asyncio.CancelledError:
                        pass
                await agent._finalize_notes()
                # Do NOT clean up the room here — the parent process
                # (end_room) handles room deletion after the agent exits.
                # Calling _cleanup_room would race with the parent's
                # DeleteRoomRequest.
                break

            await asyncio.sleep(1)

        # Wait for the monitor task (natural end) to finish its finalise
        # + cleanup chain before exiting. Without this, asyncio.run()
        # cancels pending tasks the moment _main() returns, which kills
        # the monitor task mid-_finalize_notes().
        if agent._monitor_task and not agent._monitor_task.done():
            try:
                await agent._monitor_task
            except asyncio.CancelledError:
                pass

    asyncio.run(_main())
