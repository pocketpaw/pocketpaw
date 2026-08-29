# test_media_duration.py — T2 "Audio/video transcription at ingest".
# Created: 2026-08-29 (T2).
#
# ``media_duration`` is a SPEND GATE, so the property under test is asymmetric:
# a wrong-but-large answer costs nothing (we skip a file we could have paid
# for), a wrong-but-small answer is a bill. Every "cannot parse" case therefore
# asserts ``None`` — which the caller reads as "unknown", never as "short" —
# and the happy paths assert the real number.
#
# The containers here are BUILT BYTE BY BYTE rather than shipped as fixtures,
# so the tests are hermetic and each one names the exact field it is about
# (a 64-bit duration, a zero timescale, a moov after the mdat).
#
# They were also checked against ground truth. ``probe_duration_seconds`` was
# run over twelve real ffmpeg-produced files and compared with ``ffprobe``
# (2026-08-29): mp3 CBR, mp3 VBR/Xing, a 15.8-minute mp3, mp4 with moov at the
# end, mp4 +faststart, mov, m4a and wav all landed within 0.27%, and ogg/opus
# and webm returned ``None`` as designed. ``test_real_files_match_ffprobe``
# below re-runs a slice of that comparison wherever ffmpeg is installed.
"""T2: how long is this recording, read from its own header, before the spend."""

from __future__ import annotations

import shutil
import struct
import subprocess

import pytest
from pocketpaw_ee.cloud.uploads.media_duration import probe_duration_seconds

# --- container builders ----------------------------------------------------


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + kind + payload


def _mvhd_v0(timescale: int, duration: int) -> bytes:
    # version+flags, creation, modification, timescale, duration, + the rest of
    # a real mvhd (rate/volume/matrix/next-track-id) as padding.
    return _box(
        b"mvhd",
        b"\x00\x00\x00\x00" + b"\x00" * 8 + struct.pack(">II", timescale, duration) + b"\x00" * 80,
    )


def _mvhd_v1(timescale: int, duration: int) -> bytes:
    return _box(
        b"mvhd",
        b"\x01\x00\x00\x00" + b"\x00" * 16 + struct.pack(">IQ", timescale, duration) + b"\x00" * 80,
    )


def _mp4(mvhd: bytes, *, moov_last: bool = False, mdat_size: int = 4096) -> bytes:
    ftyp = _box(b"ftyp", b"isom" + b"\x00\x00\x02\x00" + b"isomiso2mp41")
    moov = _box(b"moov", mvhd)
    mdat = _box(b"mdat", b"\x00" * mdat_size)
    return ftyp + (mdat + moov if moov_last else moov + mdat)


def _wav(sample_rate: int, channels: int, bits: int, seconds: float) -> bytes:
    byte_rate = sample_rate * channels * bits // 8
    data = b"\x00" * int(byte_rate * seconds)
    fmt = struct.pack("<HHIIHH", 1, channels, sample_rate, byte_rate, channels * bits // 8, bits)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


#: MPEG-1 Layer III, 128 kbps, 44.1 kHz, stereo — the header every mp3 encoder
#: in the world emits for a default encode.
_MP3_FRAME_HEADER = bytes([0xFF, 0xFB, 0x90, 0x00])


def _mp3_cbr(audio_bytes: int, *, id3: bool = False) -> bytes:
    tag = b""
    if id3:
        # 'ID3', version, flags, syncsafe size — 300 bytes of tag payload.
        tag = b"ID3\x03\x00\x00" + bytes([0, 0, 0x02, 0x2C]) + b"\x00" * 300
    return tag + _MP3_FRAME_HEADER + b"\x00" * (audio_bytes - 4)


def _mp3_xing(frames: int) -> bytes:
    # Side info for MPEG-1 stereo is 32 bytes; the Xing tag follows it.
    body = _MP3_FRAME_HEADER + b"\x00" * 32
    body += b"Xing" + struct.pack(">I", 0x1) + struct.pack(">I", frames)
    return body + b"\x00" * 4096


# --- ISO base media --------------------------------------------------------


class TestIsoBaseMedia:
    def test_reads_the_mvhd_duration(self, tmp_path):
        f = tmp_path / "clip.mp4"
        f.write_bytes(_mp4(_mvhd_v0(timescale=1000, duration=95_000)))
        assert probe_duration_seconds(f) == pytest.approx(95.0)

    def test_finds_a_moov_written_after_the_mdat(self, tmp_path):
        """The layout every camera and screen recorder produces.

        A parser that only looked at the head of the file would return None
        here and hand a three-hour recording to the byte ceiling, which cannot
        tell a long recording from a short 4K one.

        Mutation: stop seeking past a box (``fh.seek(payload_at)``) — red.
        """
        f = tmp_path / "recording.mp4"
        f.write_bytes(
            _mp4(_mvhd_v0(timescale=600, duration=600 * 10_800), moov_last=True, mdat_size=200_000)
        )
        assert probe_duration_seconds(f) == pytest.approx(10_800.0)

    def test_reads_a_64_bit_version_1_mvhd(self, tmp_path):
        """Long recordings are exactly where a v1 box shows up.

        Mutation: drop the ``version == 1`` branch — red, and the file it
        refuses to measure is a long one.
        """
        f = tmp_path / "long.mov"
        f.write_bytes(_mp4(_mvhd_v1(timescale=90_000, duration=90_000 * 7_200)))
        assert probe_duration_seconds(f) == pytest.approx(7_200.0)

    def test_a_zero_timescale_is_unknown_not_zero(self, tmp_path):
        """A division that would raise, and an answer that would read as
        'this file is 0 seconds long, transcribe it'."""
        f = tmp_path / "broken.mp4"
        f.write_bytes(_mp4(_mvhd_v0(timescale=0, duration=95_000)))
        assert probe_duration_seconds(f) is None

    def test_the_documented_unknown_duration_is_unknown(self, tmp_path):
        f = tmp_path / "live.mp4"
        f.write_bytes(_mp4(_mvhd_v0(timescale=1000, duration=0xFFFFFFFF)))
        assert probe_duration_seconds(f) is None

    def test_an_interrupted_upload_is_unknown(self, tmp_path):
        """The realistic truncation: the moov is written LAST, so a cut-short
        upload has audio and no header. That must read as unknown, not as the
        length of whatever bytes did arrive."""
        whole = _mp4(_mvhd_v0(timescale=1000, duration=95_000), moov_last=True)
        f = tmp_path / "cut.mp4"
        f.write_bytes(whole[: len(whole) // 2])
        assert probe_duration_seconds(f) is None

    def test_a_header_cut_in_half_is_unknown(self, tmp_path):
        whole = _mp4(_mvhd_v0(timescale=1000, duration=95_000))
        f = tmp_path / "half.mp4"
        f.write_bytes(whole[:60])  # ftyp, then a moov that stops mid-mvhd
        assert probe_duration_seconds(f) is None

    def test_a_lying_box_size_is_unknown(self, tmp_path):
        """A box that claims to be bigger than the file must not be trusted."""
        f = tmp_path / "evil.mp4"
        f.write_bytes(
            _box(b"ftyp", b"isom" + b"\x00" * 8)
            + struct.pack(">I", 0x7FFFFFFF)
            + b"moov"
            + b"\x00" * 64
        )
        assert probe_duration_seconds(f) is None

    def test_a_moov_with_no_mvhd_is_unknown(self, tmp_path):
        f = tmp_path / "odd.mp4"
        f.write_bytes(_mp4(_box(b"trak", b"\x00" * 64)))
        assert probe_duration_seconds(f) is None


# --- WAV -------------------------------------------------------------------


class TestWav:
    def test_reads_the_data_chunk_over_the_byte_rate(self, tmp_path):
        f = tmp_path / "voice.wav"
        f.write_bytes(_wav(sample_rate=44_100, channels=2, bits=16, seconds=12.5))
        assert probe_duration_seconds(f) == pytest.approx(12.5, rel=1e-3)

    def test_a_long_recording_is_measured_not_guessed(self, tmp_path):
        """A 90-minute 8 kHz mono wav is only 43 MB — comfortably under any
        byte ceiling. Reading its real length is the only thing that stops it."""
        f = tmp_path / "meeting.wav"
        f.write_bytes(_wav(sample_rate=8_000, channels=1, bits=8, seconds=5_400))
        assert probe_duration_seconds(f) == pytest.approx(5_400.0, rel=1e-3)

    def test_a_fmt_chunk_that_never_arrives_is_unknown(self, tmp_path):
        body = b"WAVE" + b"data" + struct.pack("<I", 16) + b"\x00" * 16
        f = tmp_path / "nofmt.wav"
        f.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
        assert probe_duration_seconds(f) is None


# --- MP3 -------------------------------------------------------------------


class TestMp3:
    def test_reads_a_constant_bitrate_stream(self, tmp_path):
        # 128 kbps = 16000 bytes a second.
        f = tmp_path / "podcast.mp3"
        f.write_bytes(_mp3_cbr(16_000 * 200))
        assert probe_duration_seconds(f) == pytest.approx(200.0, rel=1e-3)

    def test_skips_an_id3v2_tag_before_the_first_frame(self, tmp_path):
        """Album art and tags sit in front of the audio; counting them as
        audio would over-state length, which is safe — but the sync search
        must still find the frame at all or the answer is None."""
        f = tmp_path / "tagged.mp3"
        f.write_bytes(_mp3_cbr(16_000 * 60, id3=True))
        assert probe_duration_seconds(f) == pytest.approx(60.0, rel=1e-2)

    def test_prefers_the_xing_frame_count_for_vbr(self, tmp_path):
        """VBR is where a bitrate estimate is worst and the Xing count exact.

        Mutation: delete the Xing branch — the answer falls back to the first
        frame's bitrate over a file padded with 4 KB, and the assertion is red.
        """
        frames = 20_000  # 20000 * 1152 / 44100 = 522.4 s
        f = tmp_path / "vbr.mp3"
        f.write_bytes(_mp3_xing(frames))
        assert probe_duration_seconds(f) == pytest.approx(frames * 1152 / 44_100, rel=1e-3)

    def test_a_reserved_bitrate_index_is_unknown(self, tmp_path):
        f = tmp_path / "junk.mp3"
        f.write_bytes(bytes([0xFF, 0xFB, 0xF0, 0x00]) + b"\x00" * 8_000)
        assert probe_duration_seconds(f) is None


# --- what it deliberately cannot read --------------------------------------


class TestUnknownContainers:
    @pytest.mark.parametrize(
        ("name", "magic"),
        [
            ("voice.ogg", b"OggS\x00\x02" + b"\x00" * 64),
            ("recording.webm", b"\x1a\x45\xdf\xa3" + b"\x00" * 64),
            ("song.flac", b"fLaC" + b"\x00" * 64),
            ("clip.avi", b"RIFF" + struct.pack("<I", 64) + b"AVI " + b"\x00" * 64),
        ],
    )
    def test_returns_unknown_rather_than_a_guess(self, tmp_path, name, magic):
        """These fall through to the byte ceiling, which is the honest answer.

        The one thing that must never happen is a small number: 'I could not
        parse this, so call it short' is how a three-hour file gets paid for.
        """
        f = tmp_path / name
        f.write_bytes(magic)
        assert probe_duration_seconds(f) is None

    def test_an_empty_file_is_unknown(self, tmp_path):
        f = tmp_path / "empty.mp3"
        f.write_bytes(b"")
        assert probe_duration_seconds(f) is None

    def test_a_missing_file_is_unknown_not_an_exception(self, tmp_path):
        """A parser is never allowed to be the reason an upload fails."""
        assert probe_duration_seconds(tmp_path / "gone.mp4") is None

    def test_a_text_file_that_claims_to_be_audio_is_unknown(self, tmp_path):
        """Sniffing beats the declared mime: both the filename and the
        Content-Type are supplied by whoever uploaded the file."""
        f = tmp_path / "notreally.mp3"
        f.write_bytes(b"this is just prose, at length, " * 40)
        assert probe_duration_seconds(f) is None


# --- ground truth ----------------------------------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("truth.wav", ["-c:a", "pcm_s16le"]),
        ("truth.mp3", ["-c:a", "libmp3lame", "-b:a", "96k"]),
        ("truth.m4a", ["-c:a", "aac"]),
        ("truth.mp4", ["-c:a", "aac"]),
    ],
)
def test_real_files_match_ffprobe(tmp_path, name, args):
    """Synthetic containers pin the parsing; this pins the assumptions.

    Everything above is bytes this test file wrote, so it can only prove the
    parser reads what this file writes. A real encoder's output is the thing
    that finds a wrong assumption about where a box or a header actually sits.
    Skipped where ffmpeg is absent — the synthetic cases still run everywhere.
    """
    src = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=17.5", str(src)],
        check=True,
    )  # fmt: skip
    out = tmp_path / name
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), *args, str(out)], check=True
    )
    truth = float(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(out)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    )  # fmt: skip

    got = probe_duration_seconds(out)
    assert got is not None, f"could not read a real {name}"
    assert got == pytest.approx(truth, rel=0.02), f"{name}: {got} vs ffprobe {truth}"
