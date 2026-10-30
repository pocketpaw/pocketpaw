"""Multipart part-sizing math and the three new settings.

Created 2026-09-14 (feat/uploads-multipart-adapter). New file.

The failure these exist to catch is the expensive one: S3 enforces its
10000-part ceiling at COMPLETE time, so a sizing bug is only discovered after
every byte of a multi-GB file has already been transferred. The boundary cases
(exactly 10000 parts, one byte past it) are asserted directly, and a sweep
checks the invariant across magnitudes.
"""

from __future__ import annotations

import pytest

from pocketpaw.uploads.config import (
    MAX_MULTIPART_PARTS,
    MIN_MULTIPART_PART_BYTES,
    UploadSettings,
    part_count_for,
    part_size_for,
    validate_part_number,
)
from pocketpaw.uploads.errors import InvalidPart

MIB = 1024 * 1024
DEFAULT_PART = 8 * MIB


class TestPartSizeFor:
    def test_small_file_gets_the_baseline(self):
        assert part_size_for(1) == DEFAULT_PART
        assert part_size_for(0) == DEFAULT_PART
        assert part_size_for(5 * MIB) == DEFAULT_PART

    def test_exactly_ten_thousand_parts_stays_at_the_baseline(self):
        """The largest file that fits without scaling: 10000 x 8 MiB."""
        size = MAX_MULTIPART_PARTS * DEFAULT_PART
        part = part_size_for(size)
        assert part == DEFAULT_PART
        assert part_count_for(size, part) == MAX_MULTIPART_PARTS

    def test_one_byte_past_the_boundary_scales_up(self):
        """One byte more must grow the part, not spill into part 10001."""
        size = MAX_MULTIPART_PARTS * DEFAULT_PART + 1
        part = part_size_for(size)
        assert part > DEFAULT_PART
        assert part_count_for(size, part) <= MAX_MULTIPART_PARTS

    def test_large_file_forces_a_part_above_the_baseline(self):
        size = 100 * 1024 * MIB  # 100 GiB
        part = part_size_for(size)
        assert part == 11 * MIB
        assert part_count_for(size, part) <= MAX_MULTIPART_PARTS

    def test_result_is_always_a_whole_mib(self):
        for size in (1, 17, 999 * MIB, 3 * 1024 * MIB, 700 * 1024 * MIB):
            assert part_size_for(size) % MIB == 0

    def test_part_count_never_exceeds_the_ceiling(self):
        """The invariant the whole helper exists for, swept across magnitudes."""
        size = 1
        while size < 1024 * 1024 * MIB:  # up to 1 TiB
            part = part_size_for(size)
            assert part_count_for(size, part) <= MAX_MULTIPART_PARTS, size
            assert part >= MIN_MULTIPART_PART_BYTES, size
            size *= 3

    def test_base_override_is_honoured(self):
        assert part_size_for(1, base=16 * MIB) == 16 * MIB

    def test_base_is_rounded_up_to_a_whole_mib(self):
        """A configured 7 MB (not MiB) becomes 7 MiB — still over S3's 5 MiB floor."""
        part = part_size_for(1, base=7_000_000)
        assert part == 7 * MIB
        assert part >= MIN_MULTIPART_PART_BYTES

    def test_scaling_beats_a_base_that_is_too_small(self):
        size = MAX_MULTIPART_PARTS * 20 * MIB
        assert part_size_for(size, base=1 * MIB) == 20 * MIB


class TestPartCountFor:
    def test_empty_file_is_one_part(self):
        assert part_count_for(0, DEFAULT_PART) == 1

    def test_partial_last_part_counts(self):
        assert part_count_for(DEFAULT_PART + 1, DEFAULT_PART) == 2

    def test_exact_multiple(self):
        assert part_count_for(DEFAULT_PART * 4, DEFAULT_PART) == 4

    def test_non_positive_part_size_is_rejected(self):
        with pytest.raises(ValueError):
            part_count_for(100, 0)


class TestValidatePartNumber:
    @pytest.mark.parametrize("number", [1, 2, 9999, MAX_MULTIPART_PARTS])
    def test_accepts_in_range(self, number: int):
        assert validate_part_number(number) == number

    @pytest.mark.parametrize("number", [0, -1, MAX_MULTIPART_PARTS + 1])
    def test_rejects_out_of_range(self, number: int):
        with pytest.raises(InvalidPart):
            validate_part_number(number)

    @pytest.mark.parametrize("number", ["1", "../../etc/passwd", 3.0, None, b"1"])
    def test_rejects_non_ints(self, number: object):
        with pytest.raises(InvalidPart):
            validate_part_number(number)

    def test_rejects_bool(self):
        """``True`` passes isinstance(int) and would silently address part 1."""
        with pytest.raises(InvalidPart):
            validate_part_number(True)

    def test_error_carries_the_wire_code(self):
        with pytest.raises(InvalidPart) as exc:
            validate_part_number(0)
        assert exc.value.code == "multipart.invalid"


class TestMultipartSettings:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch):
        for var in (
            "POCKETPAW_MAX_LARGE_FILE_BYTES",
            "POCKETPAW_MULTIPART_PART_BYTES",
            "POCKETPAW_MULTIPART_TTL_HOURS",
        ):
            monkeypatch.delenv(var, raising=False)
        cfg = UploadSettings()
        assert cfg.max_large_file_bytes == 5 * 1024 * MIB
        assert cfg.multipart_part_bytes == DEFAULT_PART
        assert cfg.multipart_ttl_hours == 168

    def test_env_overrides(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("POCKETPAW_MAX_LARGE_FILE_BYTES", str(10 * 1024 * MIB))
        monkeypatch.setenv("POCKETPAW_MULTIPART_PART_BYTES", str(16 * MIB))
        monkeypatch.setenv("POCKETPAW_MULTIPART_TTL_HOURS", "24")
        cfg = UploadSettings()
        assert cfg.max_large_file_bytes == 10 * 1024 * MIB
        assert cfg.multipart_part_bytes == 16 * MIB
        assert cfg.multipart_ttl_hours == 24

    @pytest.mark.parametrize("bad", ["not-a-number", "0", "-5", "8 MiB"])
    def test_malformed_falls_back_to_the_default(self, monkeypatch: pytest.MonkeyPatch, bad: str):
        """A typo must not read as "unlimited" — it falls back and warns."""
        monkeypatch.setenv("POCKETPAW_MULTIPART_PART_BYTES", bad)
        assert UploadSettings().multipart_part_bytes == DEFAULT_PART

    def test_max_file_bytes_is_untouched_by_the_multipart_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """body_limit derives the global ASGI ceiling from max_file_bytes; the
        5 GiB multipart cap must never leak into it."""
        monkeypatch.delenv("POCKETPAW_UPLOAD_MAX_BYTES", raising=False)
        monkeypatch.setenv("POCKETPAW_MAX_LARGE_FILE_BYTES", str(5 * 1024 * MIB))
        cfg = UploadSettings()
        assert cfg.max_file_bytes == 25 * MIB
