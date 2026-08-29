# media_duration.py — how long is this media file, read from its own header,
# BEFORE anyone pays to transcribe it.
#
# Created 2026-08-29 (T2, "Audio/video transcription at ingest").
#
# WHY A PARSER AND NOT A TIMEOUT. Transcription is billed by the length of the
# audio, so a 3-hour video is a bill, not a delay. A timeout discovers that
# fact after the meter has already run; this module answers it from the first
# few kilobytes of the file, for free, before the spend. That is the whole
# reason it exists — ``transcription.py`` calls it as a gate, not as metadata.
#
# WHY NOT ffprobe / mutagen. The production image installs tesseract and
# Playwright's shared libs and nothing else (see ``Dockerfile``) — there is no
# ffmpeg in it. Shelling out to a binary that is absent would raise inside the
# caller's except and read as "transcription is switched off" while every media
# file silently skipped. A new Python dependency would have to survive the same
# journey through ``[all]`` / ``[extraction]`` that pypdf did not (it was
# missing from the image for months). Stdlib parsing has no such failure mode:
# if the code is deployed, the probe works.
#
# WHAT IT READS, and how sure it is:
#   * ISO base media (mp4, m4a, mov, 3gp) — the ``mvhd`` box's
#     ``duration / timescale``. EXACT. Handles the 64-bit v1 box and a ``moov``
#     placed at the END of the file (which is what a non-faststart muxer
#     produces, i.e. most camera and screen-recorder output).
#   * WAV (RIFF) — the ``data`` chunk size over the ``fmt `` byte rate. EXACT
#     for PCM.
#   * MP3 — the Xing/Info VBR frame count when present (EXACT), otherwise the
#     first frame's bitrate over the remaining bytes (accurate for CBR, an
#     estimate for headerless VBR).
#
# WHAT IT DOES NOT READ: Ogg/Opus, WebM/Matroska, FLAC, AVI. It returns
# ``None`` for those, and ``None`` means "I do not know" — never "it is short".
# The caller must have a second, size-based ceiling for exactly this reason;
# see ``transcription.py``, which does. The residual exposure is stated there
# in money.
#
# EVERY failure returns ``None``. A truncated file, a nonsense box size, a zero
# timescale, an unreadable path — all of them are "I do not know", because a
# parser that guesses a small number for a file it did not understand would
# open the exact hole this module was written to close.

from __future__ import annotations

import logging
import struct
from pathlib import Path

logger = logging.getLogger(__name__)

# A container walk should never need more than this many boxes/chunks to reach
# the header it wants. The bound exists so a malformed file cannot spin here.
_MAX_BOXES = 512

# How far in we look for an MP3's first frame sync (past the ID3v2 tag, which
# we skip exactly; this is slack for padding and junk).
_MP3_SCAN_BYTES = 64 * 1024

# ── ISO base media (mp4 / m4a / mov / 3gp) ───────────────────────────────────

_ISO_BRANDS = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"pnot"}


def _read_box_header(fh, limit: int) -> tuple[bytes, int, int] | None:
    """Return ``(type, payload_offset, payload_size)`` for the box at the cursor.

    ``None`` at EOF, on a truncated header, or on a size that cannot be real.
    """
    start = fh.tell()
    head = fh.read(8)
    if len(head) < 8:
        return None
    size = int.from_bytes(head[0:4], "big")
    btype = head[4:8]
    header_len = 8
    if size == 1:
        ext = fh.read(8)
        if len(ext) < 8:
            return None
        size = int.from_bytes(ext, "big")
        header_len = 16
    elif size == 0:
        # "to end of file" — legal for the last box.
        size = limit - start
    if size < header_len or start + size > limit:
        return None
    return btype, start + header_len, size - header_len


def _parse_mvhd(payload: bytes) -> float | None:
    """``duration / timescale`` out of an ``mvhd`` payload, in seconds."""
    if len(payload) < 4:
        return None
    version = payload[0]
    if version == 1:
        # creation(8) modification(8) timescale(4) duration(8)
        if len(payload) < 4 + 8 + 8 + 4 + 8:
            return None
        timescale, duration = struct.unpack(">IQ", payload[20:32])
    elif version == 0:
        # creation(4) modification(4) timescale(4) duration(4)
        if len(payload) < 4 + 4 + 4 + 4 + 4:
            return None
        timescale, duration = struct.unpack(">II", payload[12:20])
        if duration == 0xFFFFFFFF:  # documented "unknown"
            return None
    else:
        return None
    if timescale <= 0 or duration <= 0:
        return None
    return duration / timescale


def _iso_duration(path: Path, size: int) -> float | None:
    with path.open("rb") as fh:
        # Walk the top level for ``moov``. We SEEK past every other box rather
        # than reading it, so a 2 GB ``mdat`` costs nothing and a ``moov`` at
        # the end of the file (the common camera/recorder layout) is found.
        for _ in range(_MAX_BOXES):
            box = _read_box_header(fh, size)
            if box is None:
                return None
            btype, payload_at, payload_size = box
            if btype == b"moov":
                fh.seek(payload_at)
                moov_end = payload_at + payload_size
                for _ in range(_MAX_BOXES):
                    child = _read_box_header(fh, moov_end)
                    if child is None:
                        return None
                    ctype, cpayload_at, cpayload_size = child
                    if ctype == b"mvhd":
                        fh.seek(cpayload_at)
                        # An mvhd is ~100 bytes; the cap is paranoia, not need.
                        return _parse_mvhd(fh.read(min(cpayload_size, 4096)))
                    fh.seek(cpayload_at + cpayload_size)
                return None
            fh.seek(payload_at + payload_size)
    return None


# ── RIFF / WAV ───────────────────────────────────────────────────────────────


def _wav_duration(path: Path, size: int) -> float | None:
    with path.open("rb") as fh:
        header = fh.read(12)
        if len(header) < 12 or header[0:4] != b"RIFF" or header[8:12] != b"WAVE":
            return None
        byte_rate = 0
        for _ in range(_MAX_BOXES):
            head = fh.read(8)
            if len(head) < 8:
                return None
            cid = head[0:4]
            csize = int.from_bytes(head[4:8], "little")
            if csize < 0 or fh.tell() + csize > size:
                # A truncated recording: the data chunk claims more than the
                # file holds. Fall back to what is actually there.
                csize = max(0, size - fh.tell())
            if cid == b"fmt ":
                fmt = fh.read(min(csize, 64))
                if len(fmt) < 16:
                    return None
                byte_rate = int.from_bytes(fmt[8:12], "little")
                fh.seek(fh.tell() + csize - len(fmt))
            elif cid == b"data":
                if byte_rate <= 0:
                    return None
                return csize / byte_rate
            else:
                fh.seek(fh.tell() + csize + (csize % 2))  # chunks are word-aligned
    return None


# ── MP3 ──────────────────────────────────────────────────────────────────────

_MPEG_BITRATES_V1_L3 = (
    0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0,
)  # fmt: skip
_MPEG_BITRATES_V2_L3 = (
    0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0,
)  # fmt: skip
_SAMPLE_RATES = {
    3: (44100, 48000, 32000),  # MPEG-1
    2: (22050, 24000, 16000),  # MPEG-2
    0: (11025, 12000, 8000),  # MPEG-2.5
}


def _id3v2_len(head: bytes) -> int:
    """Bytes to skip for a leading ID3v2 tag (0 when there is none)."""
    if len(head) < 10 or head[0:3] != b"ID3":
        return 0
    # syncsafe: 7 bits per byte
    b = head[6:10]
    if any(x & 0x80 for x in b):
        return 0
    return 10 + ((b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3])


def _mp3_duration(path: Path, size: int) -> float | None:
    with path.open("rb") as fh:
        head = fh.read(10)
        offset = _id3v2_len(head)
        fh.seek(offset)
        buf = fh.read(_MP3_SCAN_BYTES)
        if len(buf) < 4:
            return None

        for i in range(len(buf) - 4):
            if buf[i] != 0xFF or (buf[i + 1] & 0xE0) != 0xE0:
                continue
            h1, h2, h3 = buf[i + 1], buf[i + 2], buf[i + 3]
            version_bits = (h1 >> 3) & 0x03  # 3=MPEG1, 2=MPEG2, 0=MPEG2.5
            layer_bits = (h1 >> 1) & 0x03
            if version_bits == 1 or layer_bits != 0x01:  # reserved / not Layer III
                continue
            bitrate_idx = (h2 >> 4) & 0x0F
            rate_idx = (h2 >> 2) & 0x03
            if bitrate_idx in (0, 15) or rate_idx == 3:
                continue
            table = _MPEG_BITRATES_V1_L3 if version_bits == 3 else _MPEG_BITRATES_V2_L3
            bitrate = table[bitrate_idx] * 1000
            sample_rate = _SAMPLE_RATES[version_bits][rate_idx]
            if bitrate <= 0 or sample_rate <= 0:
                continue
            samples_per_frame = 1152 if version_bits == 3 else 576

            # Xing/Info sits inside the first frame, after the side info. Its
            # frame COUNT is exact for VBR, which the bitrate estimate is not.
            channel_mode = (h3 >> 6) & 0x03
            if version_bits == 3:
                side = 17 if channel_mode == 3 else 32
            else:
                side = 9 if channel_mode == 3 else 17
            tag_at = i + 4 + side
            if tag_at + 12 <= len(buf) and buf[tag_at : tag_at + 4] in (b"Xing", b"Info"):
                flags = int.from_bytes(buf[tag_at + 4 : tag_at + 8], "big")
                if flags & 0x1:
                    frames = int.from_bytes(buf[tag_at + 8 : tag_at + 12], "big")
                    if frames > 0:
                        return frames * samples_per_frame / sample_rate

            # CBR: what is left of the file at this frame's bitrate.
            audio_bytes = size - offset - i
            if audio_bytes <= 0:
                return None
            return audio_bytes * 8 / bitrate
    return None


# ── Public entry point ───────────────────────────────────────────────────────


def probe_duration_seconds(path: Path) -> float | None:
    """Seconds of media in ``path``, or ``None`` when it cannot be determined.

    ``None`` is NOT "short". Callers must treat it as "unknown" and fall back
    to a size-based ceiling — see ``transcription.py``.

    The container is chosen by SNIFFING the first bytes, not by the file
    extension or the declared mime: an upload's name and its ``Content-Type``
    are both attacker- and browser-supplied, and this function is a spend gate.
    """
    try:
        size = path.stat().st_size
        if size <= 0:
            return None
        with path.open("rb") as fh:
            magic = fh.read(16)
        if len(magic) < 12:
            return None

        if magic[0:4] == b"RIFF" and magic[8:12] == b"WAVE":
            return _wav_duration(path, size)
        if magic[4:8] in _ISO_BRANDS:
            return _iso_duration(path, size)
        if magic[0:3] == b"ID3" or (magic[0] == 0xFF and (magic[1] & 0xE0) == 0xE0):
            return _mp3_duration(path, size)
    except Exception:
        # A parser is not allowed to be the reason an upload fails. Unknown.
        logger.debug("media-duration: could not probe %s", path, exc_info=True)
        return None

    return None


__all__ = ["probe_duration_seconds"]
