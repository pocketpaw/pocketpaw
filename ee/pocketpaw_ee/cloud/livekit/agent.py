"""LiveKit call bot: transcribes a group call, writes meeting notes, voices Ask Paw.

Runs as a subprocess (``python -m pocketpaw_ee.cloud.livekit.agent``) that
``service.py`` spawns per call, so WebRTC and Deepgram stay out of the API event
loop. It joins with ``auto_subscribe=False`` and subscribes to microphone audio
only (``_should_subscribe``): decoding everyone's video made big calls unstable.
Each participant's mic is piped into Deepgram STT (Nova-3, ``language="multi"``,
needs ``DEEPGRAM_API_KEY``); final segments are kept in memory, summarised every
5 minutes into ``livekit:progressive:{group_id}``, and merged into the notes
printed to stdout as one JSON line when the room empties or SIGTERM arrives.

Ask Paw: every final segment is mirrored to ``livekit:transcript:{group_id}``
(cleared at start). The bot publishes one microphone track ("paw-voice") and
takes commands from ``assistant.py`` on ``livekit:bot:cmd:{group_id}``: ``say``
(voice a sentence via Deepgram TTS), ``say_end``, ``stop``, ``listen_start`` /
``listen_end`` (capture the asker's spoken question onto
``livekit:ask:q:{ask_id}``; an unclosed window hands back "" after 20s). When an
answer has played out, or was cut by ``stop``, it pushes to
``livekit:ask:done:{ask_id}``. Paw's lines stay in the notes transcript, but Paw
is never listed as a participant. Redis failures never interrupt transcription.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

from pocketpaw_ee.cloud.livekit.llm import call_llm_text
from pocketpaw_ee.cloud.livekit.prompts import (
    ANTHROPIC_SUMMARY_PROMPT,
    DEEPSEEK_SUMMARY_PROMPT,
    MERGE_SUMMARY_PROMPT,
    OPENAI_SUMMARY_PROMPT,
    PROGRESSIVE_SUMMARY_PROMPT,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# How long to wait (seconds) after the last participant leaves before ending
_CALL_END_GRACE_SECONDS = 5

# How often (seconds) to poll room state
_MONITOR_POLL_INTERVAL = 5

# How often (seconds) to run progressive summarization of accumulated transcript
_PROGRESSIVE_INTERVAL = 300  # 5 minutes

# Max transcript chars in the stdout JSON payload. The parent reads via
# StreamReader.readline() which has a default 64 KiB limit. The rest of the
# payload (summary, participants, etc.) takes ~10-14 KiB, so cap transcript at
# 40 KiB to keep the total comfortably under 64 KiB.
_MAX_STDOUT_TRANSCRIPT_CHARS = 40_000

# Redis key prefix for progressive summary storage
_REDIS_PROGRESSIVE_KEY = "livekit:progressive:{group_id}"
# TTL for the progressive summaries Redis key (6 hours — plenty for any call)
_REDIS_PROGRESSIVE_TTL = 6 * 3600

# Ask Paw Redis contract, shared with assistant.py (the API-process side).
REDIS_TRANSCRIPT_KEY = "livekit:transcript:{group_id}"
REDIS_TRANSCRIPT_TTL = 6 * 3600
REDIS_BOT_CMD_CHANNEL = "livekit:bot:cmd:{group_id}"
REDIS_ASK_QUESTION_KEY = "livekit:ask:q:{ask_id}"
REDIS_ASK_QUESTION_TTL = 120
REDIS_ASK_DONE_KEY = "livekit:ask:done:{ask_id}"
REDIS_ASK_DONE_TTL = 120

BOT_IDENTITY = "call-bot"
PAW_SPEAKER = "Paw"

# Deepgram finalizes a flushed utterance shortly after the push-to-talk release;
# segments arriving inside this grace still belong to the question.
_LISTEN_END_GRACE_SECONDS = 1.5
# A push-to-talk window nobody closes is dropped after this, handing back "".
_LISTEN_WINDOW_SECONDS = 20.0

_SUBSCRIBE_BACKOFF_SECONDS = (1, 2, 5, 10)

_TTS_MODEL = "aura-2-andromeda-en"
_TTS_SAMPLE_RATE = 24000


# ---------------------------------------------------------------------------
# Track subscription policy
# ---------------------------------------------------------------------------


def _should_subscribe(publication: Any) -> bool:
    """Decide whether the call bot should subscribe to a remote publication.

    The meeting-notes bot only transcribes participant microphone audio, so it
    subscribes to microphone audio publications and nothing else — never video
    (camera or screenshare) and never screenshare system audio. Combined with
    ``auto_subscribe=False`` on connect, this keeps the bot's per-participant
    load flat instead of pulling and decoding every participant's video.

    Kept as a small pure function (it only reads ``publication.kind`` and
    ``publication.source``) so the subscribe decision is unit-testable without
    standing up the whole LiveKit SDK.
    """
    # ``kind`` / ``source`` are protobuf int enums (TrackKind.ValueType /
    # TrackSource.ValueType), NOT strings — comparing against "audio" silently
    # never matches.
    from livekit.rtc import TrackKind, TrackSource

    if getattr(publication, "kind", None) != TrackKind.KIND_AUDIO:
        return False
    # ``source`` may be SOURCE_UNKNOWN when the publisher didn't tag the track;
    # treat untagged audio as a microphone (the common plain-audio publish) so
    # we don't drop real speech. Screenshare system audio is excluded.
    source = getattr(publication, "source", TrackSource.SOURCE_UNKNOWN)
    return source in (TrackSource.SOURCE_MICROPHONE, TrackSource.SOURCE_UNKNOWN)


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
        self._participant_info: dict[str, str] = {}  # identity → display_name
        self._has_ever_had_humans: bool = False
        self._call_start_time: float = 0.0

        # Tasks
        self._running = False
        self._monitor_task: asyncio.Task | None = None
        self._transcribe_task: asyncio.Task | None = None
        self._progressive_task: asyncio.Task | None = None

        # LiveKit RTC room (set when connected)
        self._rtc_room: Any = None

        # ── Progressive summarization state ──
        # How many transcript segments have been consumed by progressive summaries
        self._progressive_last_idx: int = 0
        # In-memory fallback list of progressive summaries (dicts)
        self._progressive_summaries: list[dict[str, Any]] = []
        # Cached Redis client (created lazily)
        self._redis: Any = None

        # ── Ask Paw voice state ──
        self._stt_streams: dict[str, Any] = {}
        self._audio_source: Any = None
        self._tts: Any = None
        self._say_queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()
        self._speaking_task: asyncio.Task | None = None
        self._speaking_ask_id: str = ""
        self._listen_windows: dict[str, tuple[str, float]] = {}
        self._bg_tasks: set[asyncio.Task] = set()

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

        await self._clear_live_transcript()

        logger.info(
            "CallMeetingAgent started for room %s (group %s)",
            self.room_name,
            self.group_id,
        )

        # Monitor room emptiness (polling-based)
        self._monitor_task = asyncio.create_task(self._monitor_room())

        # Connect to the LiveKit room and begin transcription
        self._transcribe_task = asyncio.create_task(self._connect_and_transcribe())

        # Progressive summarization (runs every 5 min during the call)
        self._progressive_task = asyncio.create_task(self._progressive_worker())

    async def stop(self) -> None:
        """Stop the agent and generate meeting notes."""
        self._running = False

        # Disconnect RTC room first (stops audio streams)
        await self._disconnect_rtc()

        # Cancel monitor task — do NOT await; the task's CancelledError
        # propagates at its next await point and asyncio.run() handles
        # cleanup.  Avoiding the await here means _finalize_notes() is
        # called promptly even when Deepgram/AudioStream cleanup is slow.
        if self._monitor_task:
            self._monitor_task.cancel()

        # Cancel transcribe task — same rationale: don't block on
        # AudioStream/Deepgram pipe cleanup, especially for long meetings
        # with many participants where cleanup can take many seconds.
        if self._transcribe_task:
            self._transcribe_task.cancel()

        # Cancel progressive summarization so it doesn't fire mid-finalize
        if self._progressive_task:
            self._progressive_task.cancel()

        await self._finalize_notes()

        # Brief drain for cancelled tasks so they don't leak
        if self._transcribe_task and not self._transcribe_task.done():
            try:
                await asyncio.wait_for(self._transcribe_task, timeout=3)
            except (asyncio.CancelledError, TimeoutError):
                pass
        if self._monitor_task and not self._monitor_task.done():
            try:
                await asyncio.wait_for(self._monitor_task, timeout=1)
            except (asyncio.CancelledError, TimeoutError):
                pass
        if self._progressive_task and not self._progressive_task.done():
            try:
                await asyncio.wait_for(self._progressive_task, timeout=3)
            except (asyncio.CancelledError, TimeoutError):
                pass

        # Close Redis if we opened it
        await self._close_redis()

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
        *,
        identity: str | None = None,
    ) -> None:
        """Add a transcribed speech segment and mirror it to the live transcript."""
        segment: dict[str, Any] = {
            "speaker": speaker,
            "text": text,
            "timestamp": timestamp or time.time(),
        }
        if identity:
            segment["identity"] = identity
        self.transcript_segments.append(segment)
        self._spawn_bg(self._mirror_segment(segment))

    def _spawn_bg(self, coro: Any) -> None:
        """Run ``coro`` fire-and-forget, holding a reference until it finishes."""
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _mirror_segment(self, segment: dict[str, Any]) -> None:
        redis = await self._get_redis()
        if redis is None:
            return
        try:
            key = REDIS_TRANSCRIPT_KEY.format(group_id=self.group_id)
            payload = json.dumps(
                {
                    "identity": segment.get("identity", ""),
                    "speaker": segment["speaker"],
                    "text": segment["text"],
                    "ts": segment["timestamp"],
                }
            )
            await redis.rpush(key, payload)
            await redis.expire(key, REDIS_TRANSCRIPT_TTL)
        except Exception as exc:
            logger.debug("Agent: live transcript mirror failed: %s", exc)

    async def _clear_live_transcript(self) -> None:
        """Drop a previous call's live transcript so Ask Paw never reads it."""
        redis = await self._get_redis()
        if redis is None:
            return
        try:
            await redis.delete(REDIS_TRANSCRIPT_KEY.format(group_id=self.group_id))
        except Exception as exc:
            logger.debug("Agent: could not clear live transcript: %s", exc)

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

        # Declared before the try so the finally block can always clean them
        # up — even if the livekit import below fails.
        stt_streams: dict[str, Any] = {}  # participant identity → Deepgram stream
        self._stt_streams = stt_streams
        pipe_tasks: dict[str, asyncio.Task] = {}  # participant identity → audio pipe task
        voice_tasks: list[asyncio.Task] = []
        http_session: Any = None

        try:
            from livekit.plugins.deepgram import STT as DeepgramSTT
            from livekit.rtc import (
                AudioStream,
                RemoteParticipant,
                Room,
                RoomOptions,
                TrackSource,
            )

            # auto_subscribe=False: do NOT pull every participant's tracks.
            # The bot opts in to microphone audio only (see _should_subscribe),
            # so it never decodes the video/screenshare it has no use for —
            # which is what made the call unstable past a few participants.
            room_opts = RoomOptions(auto_subscribe=False)
            room = Room(loop=asyncio.get_event_loop())
            self._rtc_room = room

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
                self._participant_info[pid] = pname
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
            # Helper: opt in to a participant's microphone publications
            # ------------------------------------------------------------------
            def _subscribe_mic_publications(participant: RemoteParticipant) -> None:
                """Subscribe to a participant's mic audio publications only.

                With ``auto_subscribe=False`` nothing is subscribed by default,
                so we explicitly opt in to microphone audio (and never video /
                screenshare) via ``_should_subscribe``.
                """
                for pub in participant.track_publications.values():
                    if _should_subscribe(pub) and not pub.subscribed:
                        logger.info(
                            "Agent: subscribing to %s's mic audio track",
                            participant.name or participant.identity,
                        )
                        pub.set_subscribed(True)

            # ------------------------------------------------------------------
            # Helper: close a Deepgram STT stream (fire-and-forget on disconnect)
            # ------------------------------------------------------------------
            async def _aclose_stt_stream(stt_stream: Any, pid: str) -> None:
                """Close a Deepgram WebSocket stream so it doesn't leak."""
                try:
                    await stt_stream.aclose()
                except Exception as exc:
                    logger.debug("Error closing STT stream for %s: %s", pid, exc)

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
                # Opt in to any mic audio they already have published; tracks
                # published later are picked up by the track_published handler.
                _subscribe_mic_publications(participant)
                asyncio.create_task(_setup_audio_pipe_for_participant(participant))

            @room.on("track_published")
            def on_track_published(publication: Any, participant: Any) -> None:
                """Subscribe to newly published microphone audio only.

                With ``auto_subscribe=False`` the server forwards a track only
                once we subscribe, so this is where we opt in to mic audio (and
                skip video / screenshare entirely).
                """
                pid = participant.identity if participant else ""
                if not pid or pid == "call-bot":
                    return
                if _should_subscribe(publication) and not publication.subscribed:
                    logger.info(
                        "Agent: subscribing to %s's published mic audio track",
                        participant.name or pid,
                    )
                    publication.set_subscribed(True)

            @room.on("participant_disconnected")
            def on_participant_disconnected(participant: RemoteParticipant) -> None:
                """Clean up when a participant leaves.

                Cancels the audio pipe task and closes the participant's
                Deepgram WebSocket so it doesn't leak for the rest of a long
                call.
                """
                pid = participant.identity
                stt_stream = stt_streams.pop(pid, None)
                pipe_task = pipe_tasks.pop(pid, None)
                if pipe_task is not None:
                    pipe_task.cancel()
                if stt_stream is not None:
                    asyncio.create_task(_aclose_stt_stream(stt_stream, pid))
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
                """Safety net: ensure a pipe exists for a subscribed mic track.

                With ``auto_subscribe=False`` this fires only for tracks we
                explicitly subscribed to (mic audio), but we still guard with
                ``_should_subscribe`` and skip if a pipe already exists.
                """
                if not _should_subscribe(publication):
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

            voice_tasks.extend(await self._start_voice(room, http_session))

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

                # Explicitly opt in to their mic audio publications (needed
                # under auto_subscribe=False so the server forwards the track).
                _subscribe_mic_publications(participant)

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
            for t in voice_tasks:
                t.cancel()
            if voice_tasks:
                await asyncio.gather(*voice_tasks, return_exceptions=True)
            # Cancel any still-running audio pipe tasks
            for t in pipe_tasks.values():
                t.cancel()
            # Clean up STT streams
            for s in list(stt_streams.values()):
                try:
                    await s.aclose()
                except Exception:
                    pass
            # Clean up the HTTP session (may be None if the import failed)
            if http_session is not None:
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
                                identity=participant_id,
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

    # ------------------------------------------------------------------
    # Internal: Ask Paw voice (speaking + command channel)
    # ------------------------------------------------------------------

    async def _start_voice(self, room: Any, http_session: Any) -> list[asyncio.Task]:
        """Publish the bot's voice track and start the command/speaker tasks.

        Each step fails soft: without a publish grant or a Deepgram key the bot
        stays a silent transcriber, and the command listener still runs so
        spoken-question capture keeps working.
        """
        try:
            from livekit import rtc

            source = rtc.AudioSource(_TTS_SAMPLE_RATE, 1)
            track = rtc.LocalAudioTrack.create_audio_track("paw-voice", source)
            options = rtc.TrackPublishOptions()
            options.source = rtc.TrackSource.SOURCE_MICROPHONE
            await room.local_participant.publish_track(track, options)
            self._audio_source = source
        except Exception as exc:
            logger.warning("Agent: could not publish voice track, Paw stays silent: %s", exc)

        if self._audio_source is not None:
            try:
                from livekit.plugins.deepgram.tts import TTS as DeepgramTTS

                self._tts = DeepgramTTS(
                    model=_TTS_MODEL,
                    sample_rate=_TTS_SAMPLE_RATE,
                    http_session=http_session,
                )
            except Exception as exc:
                logger.warning("Agent: Deepgram TTS unavailable, Paw stays silent: %s", exc)

        return [
            asyncio.create_task(self._command_listener()),
            asyncio.create_task(self._speaker_worker()),
        ]

    def _get_command_redis(self) -> Any:
        """Client for the pub/sub subscription (parks a connection, so the blocking pool)."""
        from pocketpaw_ee.cloud._core.redis_client import get_blocking_redis

        return get_blocking_redis()

    async def _command_listener(self) -> None:
        """Consume Ask Paw commands from ``livekit:bot:cmd:{group_id}``.

        SUBSCRIBE is retried with backoff for as long as the bot runs, so a
        Redis blip at join time doesn't leave Paw deaf for the whole call.
        """
        channel = REDIS_BOT_CMD_CHANNEL.format(group_id=self.group_id)
        attempt = 0
        pubsub: Any = None
        while self._running:
            try:
                pubsub = self._get_command_redis().pubsub()
                await pubsub.subscribe(channel)
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                backoff = _SUBSCRIBE_BACKOFF_SECONDS
                delay = backoff[min(attempt, len(backoff) - 1)]
                attempt += 1
                logger.warning(
                    "Agent: Ask Paw subscribe failed (attempt %d), retrying in %ss: %s",
                    attempt,
                    delay,
                    exc,
                )
                pubsub = None
                await asyncio.sleep(delay)
        if pubsub is None:
            return
        try:
            logger.info("Agent: listening for Ask Paw commands on %s", channel)
            while self._running:
                try:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Agent: command channel read failed: %s", exc)
                    await asyncio.sleep(1)
                    continue
                if message and message.get("type") == "message":
                    self._handle_command(message.get("data"))
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.aclose()
            except Exception:
                pass

    def _handle_command(self, raw: Any) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            cmd = json.loads(raw)
        except (TypeError, ValueError):
            logger.debug("Agent: ignoring malformed command %r", raw)
            return
        if not isinstance(cmd, dict):
            return
        kind = cmd.get("type")
        ask_id = str(cmd.get("ask_id") or "")
        if kind == "say":
            text = str(cmd.get("text") or "").strip()
            if text:
                self._say_queue.put_nowait((ask_id, text))
        elif kind == "say_end":
            if ask_id:
                self._say_queue.put_nowait((ask_id, None))
        elif kind == "stop":
            self._stop_speaking()
        elif kind == "listen_start":
            identity = str(cmd.get("identity") or "")
            if ask_id and identity:
                self._listen_windows[ask_id] = (identity, time.time())
                self._spawn_bg(self._expire_listen(ask_id))
        elif kind == "listen_end":
            if ask_id:
                self._spawn_bg(self._finish_listen(ask_id))
        else:
            logger.debug("Agent: unknown command type %r", kind)

    def _stop_speaking(self) -> None:
        """Drop queued sentences, cut the one being spoken, and report those asks done."""
        stopped: set[str] = set()
        while not self._say_queue.empty():
            ask_id, _ = self._say_queue.get_nowait()
            stopped.add(ask_id)
        if self._speaking_task is not None and not self._speaking_task.done():
            stopped.add(self._speaking_ask_id)
            self._speaking_task.cancel()
        if self._audio_source is not None:
            try:
                self._audio_source.clear_queue()
            except Exception:
                pass
        for ask_id in stopped - {""}:
            self._spawn_bg(self._signal_done(ask_id))

    async def _speaker_worker(self) -> None:
        """Voice queued sentences one at a time; a ``None`` text marks an answer's end."""
        while self._running:
            ask_id, text = await self._say_queue.get()
            if text is None:
                await self._wait_for_playout()
                await self._signal_done(ask_id)
                continue
            if self._tts is None or self._audio_source is None:
                logger.info("Agent: no voice available, dropping Ask Paw sentence (%s)", ask_id)
                continue
            self._speaking_ask_id = ask_id
            self._speaking_task = asyncio.create_task(self._speak(text))
            try:
                await self._speaking_task
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                logger.info("Agent: Ask Paw speech stopped (%s)", ask_id)
            except Exception as exc:
                logger.warning("Agent: TTS failed for Ask Paw sentence (%s): %s", ask_id, exc)
            finally:
                self._speaking_task = None
                self._speaking_ask_id = ""

    async def _speak(self, text: str) -> None:
        async with self._tts.synthesize(text) as stream:
            async for audio in stream:
                await self._audio_source.capture_frame(audio.frame)
        self.add_transcript_segment(speaker=PAW_SPEAKER, text=text, identity=BOT_IDENTITY)

    async def _wait_for_playout(self) -> None:
        if self._audio_source is None:
            return
        try:
            await self._audio_source.wait_for_playout()
        except Exception as exc:
            logger.debug("Agent: wait_for_playout failed: %s", exc)

    async def _signal_done(self, ask_id: str) -> None:
        """Tell the API process an answer has finished playing (or was cut)."""
        await self._push_ask_key(REDIS_ASK_DONE_KEY.format(ask_id=ask_id), "1", REDIS_ASK_DONE_TTL)

    async def _push_ask_key(self, key: str, value: str, ttl: int) -> None:
        redis = await self._get_redis()
        if redis is None:
            return
        try:
            await redis.rpush(key, value)
            await redis.expire(key, ttl)
        except Exception as exc:
            logger.warning("Agent: could not write %s: %s", key, exc)

    async def _expire_listen(self, ask_id: str) -> None:
        await asyncio.sleep(_LISTEN_WINDOW_SECONDS)
        if self._listen_windows.pop(ask_id, None) is not None:
            logger.info("Agent: listen window %s expired without an end", ask_id)
            await self._push_ask_key(
                REDIS_ASK_QUESTION_KEY.format(ask_id=ask_id), "", REDIS_ASK_QUESTION_TTL
            )

    async def _finish_listen(self, ask_id: str) -> None:
        """Close a push-to-talk window and hand the asker's words back to the API process.

        Only the window's own identity is captured; whoever sent ``listen_end``
        cannot redirect it to another participant.
        """
        window = self._listen_windows.pop(ask_id, None)
        text = ""
        if window is not None:
            identity, started_at = window
            stream = self._stt_streams.get(identity)
            if stream is not None:
                try:
                    stream.flush()
                except Exception as exc:
                    logger.debug("Agent: STT flush failed for %s: %s", identity, exc)
            await asyncio.sleep(_LISTEN_END_GRACE_SECONDS)
            text = " ".join(
                seg["text"]
                for seg in self.transcript_segments
                if seg.get("identity") == identity and seg["timestamp"] >= started_at
            ).strip()
        else:
            logger.info("Agent: listen_end for %s without a listen window", ask_id)
        await self._push_ask_key(
            REDIS_ASK_QUESTION_KEY.format(ask_id=ask_id), text, REDIS_ASK_QUESTION_TTL
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
                            self._participant_info[pid] = p.get("name", "") or pid
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
                        if self._progressive_task:
                            self._progressive_task.cancel()
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
        """Generate and post meeting notes to the group.

        Uses progressive summaries (accumulated every 5 min during the call)
        when available. Falls back to sending the raw (truncated) transcript
        if the call was too short for any progressive summary.
        """
        logger.warning("Agent: _finalize_notes called")
        duration = int(time.time() - self._call_start_time)

        # Build the full transcript text (used as fallback + for the stdout payload)
        speakers_seen: set[str] = set()
        transcript_text = ""
        if self.transcript_segments:
            transcript_lines = []
            for seg in self.transcript_segments:
                speaker = seg.get("speaker", "Unknown")
                if seg.get("identity") != BOT_IDENTITY:
                    speakers_seen.add(speaker)
                transcript_lines.append(f"[{speaker}]: {seg['text']}")
            transcript_text = "\n".join(transcript_lines)

        # ── Try progressive merge path ──
        # Fetch progressive summaries that were accumulated every 5 min.
        # Each covers a small chunk of the call so the merge LLM gets full
        # context without truncation.
        progressive_summaries = await self._fetch_progressive_summaries()
        if progressive_summaries:
            logger.info(
                "Agent: merging %d progressive summaries for final notes",
                len(progressive_summaries),
            )
            try:
                summary, action_items = await self._merge_progressive_summaries(
                    progressive_summaries,
                    transcript_text,
                )
            except Exception:
                logger.exception("Agent: progressive merge failed — falling back to direct summary")
                summary = ""
                action_items = []
        else:
            summary = ""
            action_items = []

        # ── Fallback: direct summarization if progressive path yielded nothing ──
        if not summary and transcript_text:
            logger.info("Agent: falling back to direct transcript summarization")
            # Truncate for the LLM so the API call finishes within the
            # process-grace-period window (~5K chars / ~1K tokens).
            llm_transcript = transcript_text
            if len(transcript_text) > 5000:
                llm_transcript = (
                    transcript_text[:2500]
                    + "\n\n[... transcript truncated at 5000 chars ...]\n\n"
                    + transcript_text[-2500:]
                )
            try:
                summary, action_items = await self._generate_summary(llm_transcript)
            except Exception:
                logger.exception("Agent: AI summarization failed")
                summary = "AI summarization unavailable."
                action_items = ["Transcript captured but summarization failed."]

        if not summary:
            # No speech at all
            try:
                summary, action_items = await self._generate_summary(
                    "(no speech captured)",
                )
            except Exception:
                summary = "Call ended with no speech detected."
                action_items = []

        # ── Build participant info ──
        all_participants_set: set[str] = set()
        for pid in sorted(self._participant_identities):
            display_name = self._participant_info.get(pid, pid)
            all_participants_set.add(display_name)
        for s in speakers_seen:
            all_participants_set.add(s)
        all_participants = sorted(all_participants_set)

        participant_map = [
            {"identity": pid, "name": self._participant_info.get(pid, pid)}
            for pid in sorted(self._participant_identities)
        ]

        # ── Emit notes payload to stdout ──
        # The parent reads this via proc.stdout.readline() which has a
        # default 64 KiB limit (asyncio.StreamReader._DEFAULT_LIMIT).
        # If the JSON line exceeds that, Python raises LimitOverrunError
        # ("Separator is found, but chunk is longer than limit") and the
        # meeting notes are silently dropped.
        # Truncate the transcript to keep the total well under 64 KiB.
        _stdout_transcript = transcript_text
        if len(_stdout_transcript) > _MAX_STDOUT_TRANSCRIPT_CHARS:
            logger.info(
                "Agent: truncating transcript for stdout payload (%d chars -> %d)",
                len(_stdout_transcript),
                _MAX_STDOUT_TRANSCRIPT_CHARS,
            )
            # The truncation message adds chars, so subtract its length
            # from the available budget before splitting evenly.
            _TRUNC_MSG = "\n\n[... transcript truncated for stdout ...]\n\n"
            available = _MAX_STDOUT_TRANSCRIPT_CHARS - len(_TRUNC_MSG)
            midpoint = available // 2
            _stdout_transcript = (
                _stdout_transcript[:midpoint] + _TRUNC_MSG + _stdout_transcript[-midpoint:]
            )

        payload = json.dumps(
            {
                "type": "meeting_notes",
                "group_id": self.group_id,
                "transcript": _stdout_transcript,
                "summary": summary,
                "action_items": action_items,
                "participants": all_participants,
                "participant_map": participant_map,
                "duration_seconds": duration,
            }
        )
        print(payload, flush=True)
        logger.info(
            "Agent: meeting notes payload written to stdout for group %s",
            self.group_id,
        )

    # ------------------------------------------------------------------
    # Progressive summarization
    # ------------------------------------------------------------------

    async def _get_redis(self) -> Any | None:
        """Get the shared Redis client, or None if Redis is unavailable."""
        if self._redis is not None:
            return self._redis
        try:
            from pocketpaw_ee.cloud._core.redis_client import get_redis

            self._redis = get_redis()
            return self._redis
        except (RuntimeError, ImportError, Exception):
            logger.debug("Agent: Redis unavailable — progressive summaries in memory only")
            return None

    async def _close_redis(self) -> None:
        """Close the Redis client if it was opened."""
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass
            self._redis = None

    async def _progressive_worker(self) -> None:
        """Background worker that summarises transcript chunks every 5 minutes.

        Every ``_PROGRESSIVE_INTERVAL`` seconds, collects transcript segments
        accumulated since the last run, sends them to the LLM for a compact
        progressive summary, and stores the result in Redis (with an in-memory
        fallback).  The final merge in ``_finalize_notes`` combines all
        progressive summaries into the final meeting notes.
        """
        # Wait for transcription to actually start producing segments
        await asyncio.sleep(_PROGRESSIVE_INTERVAL * 0.5)  # 2.5 min initial delay

        while self._running:
            try:
                now = time.time()
                elapsed = now - self._call_start_time

                # Skip if too early in the call
                if elapsed < 30:
                    await asyncio.sleep(15)
                    continue

                # Collect segments since the last progressive run
                new_segments = self.transcript_segments[self._progressive_last_idx :]
                if len(new_segments) < 3:
                    # Not enough speech yet
                    await asyncio.sleep(_PROGRESSIVE_INTERVAL)
                    continue

                logger.info(
                    "Agent: progressive summarising %d new transcript segments "
                    "(elapsed %ds, call %s)",
                    len(new_segments),
                    int(elapsed),
                    self.group_id,
                )

                summary_dict = await self._generate_progressive_summary(new_segments)
                if summary_dict:
                    await self._store_progressive_summary(summary_dict)
                    self._progressive_last_idx = len(self.transcript_segments)
                    logger.info(
                        "Agent: stored progressive summary %d for group %s",
                        len(self._progressive_summaries),
                        self.group_id,
                    )
                else:
                    logger.warning(
                        "Agent: progressive summary returned empty — retrying next cycle"
                    )

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(
                    "Agent: progressive summarization error: %s",
                    exc,
                )

            # Wait for the next tick. Use a tight loop with short sleeps so
            # CancelledError is noticed promptly when the call ends.
            for _ in range(_PROGRESSIVE_INTERVAL):
                if not self._running:
                    return
                await asyncio.sleep(1)

    async def _generate_progressive_summary(
        self,
        segments: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Send a chunk of transcript segments to the LLM for a compact summary.

        Returns a dict with keys: segment_summary, action_items, topics, participants.
        Returns None if no LLM is configured or all calls fail.
        """
        if not segments:
            return None

        # Format into flat text (same as finalize does for full transcript)
        lines = []
        speakers_in_chunk: set[str] = set()
        for seg in segments:
            speaker = seg.get("speaker", "Unknown")
            speakers_in_chunk.add(speaker)
            lines.append(f"[{speaker}]: {seg['text']}")
        transcript = "\n".join(lines)

        # Truncate very long chunks (shouldn't happen with 5-min intervals,
        # but guard against it)
        if len(transcript) > 15_000:
            transcript = transcript[:7500] + "\n\n[...]\n\n" + transcript[-7500:]

        prompt = PROGRESSIVE_SUMMARY_PROMPT.format(transcript=transcript)

        # Try DeepSeek first, then Anthropic, then OpenAI
        deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if deepseek_key:
            try:
                content = await self._call_llm_json(
                    provider="deepseek",
                    api_key=deepseek_key,
                    prompt=prompt,
                    model="deepseek-chat",
                    max_tokens=1024,
                    timeout=60,
                )
                if content:
                    return self._parse_progressive_json(content, speakers_in_chunk)
            except Exception as exc:
                logger.warning("DeepSeek progressive summary failed: %s", exc)

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if api_key:
            try:
                content = await self._call_llm_json(
                    provider="anthropic",
                    api_key=api_key,
                    prompt=prompt,
                    model="claude-sonnet-4-20250514",
                    max_tokens=1024,
                    timeout=60,
                )
                if content:
                    return self._parse_progressive_json(content, speakers_in_chunk)
            except Exception as exc:
                logger.warning("Anthropic progressive summary failed: %s", exc)

        openai_key = os.environ.get("OPENAI_API_KEY", "")
        if openai_key:
            try:
                content = await self._call_llm_json(
                    provider="openai",
                    api_key=openai_key,
                    prompt=prompt,
                    model="gpt-4o-mini",
                    max_tokens=1024,
                    timeout=60,
                )
                if content:
                    return self._parse_progressive_json(content, speakers_in_chunk)
            except Exception as exc:
                logger.warning("OpenAI progressive summary failed: %s", exc)

        return None

    async def _call_llm_json(
        self,
        provider: str,
        api_key: str,
        prompt: str,
        model: str,
        max_tokens: int,
        timeout: int,
    ) -> str:
        """Call an LLM provider and return the raw response text (see ``llm.call_llm_text``)."""
        return await call_llm_text(
            provider=provider,
            api_key=api_key,
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            timeout=timeout,
        )

    def _parse_progressive_json(
        self,
        content: str,
        speakers_in_chunk: set[str],
    ) -> dict[str, Any]:
        """Parse the JSON response from a progressive summary LLM call.

        Falls back gracefully: if JSON parsing fails, builds a minimal
        summary from the raw response text and the segment metadata.
        """
        content = content.strip()
        # Strip markdown code fences if present
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()

        try:
            parsed = json.loads(content)
            return {
                "segment_summary": parsed.get("segment_summary", content[:500]),
                "action_items": parsed.get("action_items", []),
                "topics": parsed.get("topics", []),
                "participants": list(speakers_in_chunk),
                "timestamp": time.time(),
            }
        except (json.JSONDecodeError, Exception):
            logger.warning(
                "Agent: failed to parse progressive summary JSON — using raw text fallback"
            )
            return {
                "segment_summary": content[:500],
                "action_items": [],
                "topics": [],
                "participants": list(speakers_in_chunk),
                "timestamp": time.time(),
            }

    async def _store_progressive_summary(
        self,
        summary_dict: dict[str, Any],
    ) -> None:
        """Store a progressive summary to Redis (with in-memory fallback).

        Always stores in the in-memory list (``_progressive_summaries``)
        regardless of Redis availability so the merge path always has data.
        """
        # Always keep an in-memory copy (fallback if Redis is down at call-end)
        self._progressive_summaries.append(summary_dict)

        # Also persist to Redis for durability across restarts
        redis = await self._get_redis()
        if redis is not None:
            try:
                key = _REDIS_PROGRESSIVE_KEY.format(group_id=self.group_id)
                serialized = json.dumps(summary_dict)
                await redis.rpush(key, serialized)
                # Reset TTL on each push so the key lives long enough for
                # the call to end, but doesn't accumulate orphaned keys
                await redis.expire(key, _REDIS_PROGRESSIVE_TTL)
            except Exception as exc:
                logger.warning(
                    "Agent: failed to store progressive summary in Redis: %s",
                    exc,
                )

    async def _fetch_progressive_summaries(
        self,
    ) -> list[dict[str, Any]]:
        """Fetch all progressive summaries for this group.

        Tries Redis first; falls back to the in-memory list if Redis is
        unavailable or the call was short (no Redis entries).

        The Redis key is deleted as soon as it has been read
        (consume-once) so a back-to-back meeting on the same group never
        picks up the previous call's stale summaries.  The in-memory list
        still covers this agent for the rest of its lifecycle.
        """
        redis = await self._get_redis()
        if redis is not None:
            try:
                key = _REDIS_PROGRESSIVE_KEY.format(group_id=self.group_id)
                raw_list = await redis.lrange(key, 0, -1)
                # Consume-once: drop the key immediately after reading it
                # so the next meeting on this group starts clean.
                await redis.delete(key)
                logger.info("Agent: cleared progressive summaries key %s after fetch", key)
                if raw_list:
                    summaries = []
                    for raw in raw_list:
                        try:
                            summaries.append(json.loads(raw))
                        except (json.JSONDecodeError, Exception):
                            pass
                    if summaries:
                        logger.info(
                            "Agent: fetched %d progressive summaries from Redis",
                            len(summaries),
                        )
                        return summaries
            except Exception as exc:
                logger.warning(
                    "Agent: failed to fetch progressive summaries from Redis: %s",
                    exc,
                )

        # Fall back to in-memory
        if self._progressive_summaries:
            logger.info(
                "Agent: using %d in-memory progressive summaries",
                len(self._progressive_summaries),
            )
            return self._progressive_summaries

        return []

    async def _merge_progressive_summaries(
        self,
        progressive_summaries: list[dict[str, Any]],
        full_transcript: str,
    ) -> tuple[str, list[str]]:
        """Merge N progressive summaries into a comprehensive final meeting note.

        Sends the progressive summaries (as JSON) plus the full transcript to the
        LLM with the merge prompt. Falls back to ``_generate_summary`` if the
        merge call fails.
        """
        summaries_json = json.dumps(progressive_summaries, indent=2)

        # Truncate if the merge prompt would be too large (shouldn't happen
        # but guard against it)
        if len(summaries_json) > 25_000:
            summaries_json = summaries_json[:25_000] + "\n...]"

        # Truncate the full transcript to keep the merge prompt manageable
        merge_transcript = full_transcript
        if len(merge_transcript) > 10_000:
            merge_transcript = merge_transcript[:5000] + "\n\n[...]\n\n" + merge_transcript[-5000:]

        prompt = MERGE_SUMMARY_PROMPT.format(
            summaries_json=summaries_json,
            transcript=merge_transcript,
        )

        # Use the same LLM fallback chain as _generate_summary, but with
        # the MERGE prompt. Try DeepSeek first (markdown output).
        deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if deepseek_key:
            try:
                content = await self._call_llm_json(
                    provider="deepseek",
                    api_key=deepseek_key,
                    prompt=prompt,
                    model="deepseek-chat",
                    max_tokens=4096,
                    timeout=120,
                )
                if content:
                    return self._parse_summary_json(content)
            except Exception as exc:
                logger.warning("DeepSeek merge failed: %s", exc)

        # Fallback to Anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if api_key:
            try:
                content = await self._call_llm_json(
                    provider="anthropic",
                    api_key=api_key,
                    prompt=prompt,
                    model="claude-sonnet-4-20250514",
                    max_tokens=4096,
                    timeout=120,
                )
                if content:
                    return self._parse_summary_json(content)
            except Exception as exc:
                logger.warning("Anthropic merge failed: %s", exc)

        # Fallback to OpenAI
        openai_key = os.environ.get("OPENAI_API_KEY", "")
        if openai_key:
            try:
                content = await self._call_llm_json(
                    provider="openai",
                    api_key=openai_key,
                    prompt=prompt,
                    model="gpt-4o-mini",
                    max_tokens=4096,
                    timeout=120,
                )
                if content:
                    return self._parse_summary_json(content)
            except Exception as exc:
                logger.warning("OpenAI merge failed: %s", exc)

        # Final fallback: just use the first progressive summary
        if progressive_summaries:
            first = progressive_summaries[0]
            return (
                first.get("segment_summary", "Meeting summary unavailable."),
                first.get("action_items", []),
            )

        return "AI summarization unavailable.", ["Summarization failed."]

    # ------------------------------------------------------------------
    # AI summarization
    # ------------------------------------------------------------------

    async def _generate_summary(
        self,
        transcript: str,
    ) -> tuple[str, list[str]]:
        """Generate a meeting summary and action items from transcript.

        Uses DeepSeek by default (OpenAI-compatible API), falls back to
        Anthropic Claude, then OpenAI. Falls back to heuristic extraction
        if no LLM is configured.
        """
        if not transcript:
            return "No speech detected during the call.", []

        # Try DeepSeek first (OpenAI-compatible API)
        deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if deepseek_key:
            try:
                return await self._summarize_with_deepseek(transcript, deepseek_key)
            except Exception as exc:
                logger.warning("DeepSeek summarization failed: %s", exc)

        # Fall back to Anthropic
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

        prompt = ANTHROPIC_SUMMARY_PROMPT.format(transcript=transcript)

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

    async def _summarize_with_deepseek(
        self,
        transcript: str,
        api_key: str,
    ) -> tuple[str, list[str]]:
        """Use DeepSeek (OpenAI-compatible API) to summarize the transcript."""
        import httpx

        prompt = DEEPSEEK_SUMMARY_PROMPT.format(transcript=transcript)
        logger.info("DeepSeek prompt length: %d chars", len(prompt))

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "deepseek-chat",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 4096,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return self._parse_summary_json(content)

    async def _summarize_with_openai(
        self,
        transcript: str,
        api_key: str,
    ) -> tuple[str, list[str]]:
        """Use OpenAI GPT to summarize the transcript."""
        import httpx

        prompt = OPENAI_SUMMARY_PROMPT.format(transcript=transcript)

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
        """Parse JSON summary response from LLM.

        When the LLM returns structured JSON (Anthropic / OpenAI direct
        summary), the ``action_items`` list is extracted directly.  When the
        LLM returns Markdown (DeepSeek or the progressive-merge path), JSON
        parsing fails — in that case we extract action items from the
        ``## ✅ Action Items`` or ``### ✅ Action Items`` section of the
        Markdown so the final meeting-notes payload always carries assignable
        action items regardless of which LLM produced the summary.
        """
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
            # Markdown output – extract action items from the rendered section
            action_items = self._extract_action_items_from_markdown(content)
            return content, action_items

    @staticmethod
    def _extract_action_items_from_markdown(markdown: str) -> list[str]:
        """Extract bullet-point action items from a Markdown meeting summary.

        Looks for a heading matching "Action Items" (with optional emoji
        prefix) at any heading level (``##`` or ``###``) and collects all
        following list items until the next heading or end of text.
        Strips markdown bold markers (``**``) from extracted items so the
        ``@Name:`` assignee prefix is at position 0 for downstream parsing.
        """
        import re as _re

        lines = markdown.split("\n")
        in_section = False
        items: list[str] = []

        for line in lines:
            stripped = line.strip()

            # Detect the start of the Action Items section — accepts ``##`` or
            # ``###`` headings, with or without emoji prefix.  The DeepSeek
            # + merge prompts use ``###`` level headings; the regex also
            # accepts ``##`` for backwards-compatibility with older prompt
            # versions.
            if (
                _re.match(r"^#{2,3}\s*[✅📋📌⚠️🔜❓⚙️]*\s*Action\s*Items", stripped, _re.IGNORECASE)
                or _re.match(r"^#{2,3}\s*✅\s*Action\s*Items", stripped, _re.IGNORECASE)
                or _re.match(r"^#{2,3}\s*Action\s*Items", stripped, _re.IGNORECASE)
            ):
                in_section = True
                continue

            # If we hit another heading, stop
            if in_section and stripped.startswith("##"):
                break

            if in_section:
                # Match bullet points: -, *, or numbered 1.
                m = _re.match(r"\s*[-*]\s+(.*)", stripped)
                if not m:
                    m = _re.match(r"\s*\d+[.)]\s+(.*)", stripped)
                if m:
                    text = m.group(1).strip()
                    # Strip bold markers so ``**@Name:** description``
                    # becomes ``@Name: description`` — the assignee
                    # parser in the parent process expects ``@`` at the
                    # start of the string.
                    text = text.replace("**", "")
                    if text and text != "None" and not text.startswith("```"):
                        items.append(text)

        return items


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
                # Cancel transcribe task but do NOT await it here —
                # waiting for cleanup of AudioStreams + Deepgram pipes
                # can be slow for long meetings. The parent process
                # waits up to 30s for this process to exit; we want to
                # spend that time on _finalize_notes, not on teardown.
                if agent._transcribe_task:
                    agent._transcribe_task.cancel()
                # Cancel the monitor task so it doesn't block exit with
                # its 5s sleep interval.
                if agent._monitor_task:
                    agent._monitor_task.cancel()
                # Cancel progressive summarization so it doesn't fire mid-finalize
                if agent._progressive_task:
                    agent._progressive_task.cancel()
                await agent._finalize_notes()
                # Do NOT clean up the room here — the parent process
                # (end_room) handles room deletion after the agent exits.
                # Calling _cleanup_room would race with the parent's
                # DeleteRoomRequest.
                break

            await asyncio.sleep(1)

        # Give cancelled tasks a moment to drain, then let _main()
        # return so asyncio.run() can clean up.
        if agent._transcribe_task and not agent._transcribe_task.done():
            try:
                await asyncio.wait_for(agent._transcribe_task, timeout=3)
            except (asyncio.CancelledError, TimeoutError):
                pass
        if agent._monitor_task and not agent._monitor_task.done():
            try:
                await asyncio.wait_for(agent._monitor_task, timeout=1)
            except (asyncio.CancelledError, TimeoutError):
                pass

    asyncio.run(_main())
